"""JSON-file-backed poll state: which invoices were already handled, per app.

Written atomically (pid-qualified temp file + os.replace) so a crash mid-write
cannot corrupt the file and concurrent writers cannot clobber each other's temp
files. A corrupt or missing file starts the poller from an empty state — it must
never crash because of its own bookkeeping — but a corrupt file is *preserved*
under a sibling ``.corrupt`` name and logged at ERROR, so the evidence is not
silently replaced by a fresh empty state. A failed state write re-raises instead
of being swallowed: sending invoices whose marks were never persisted is how a
run duplicates a whole customer history without anyone noticing. The seen set is
bounded (most recent N ids per app) so a long-running poller does not grow the
file forever.

Per app the state records:
- ``seen``: invoice ids already handled (sent, skipped, or permanently abandoned);
- ``pending``: invoice ids with a retryable failure (network/429/5xx) that will
  be retried; each entry carries the attempt count, the last error, and the
  next-attempt timestamp (bounded backoff);
- ``abandoned``: invoice ids given up after a permanent failure (validation,
  missing public_url, template rejection, self-send); each entry carries the
  attempt count and the last error so an operator can see and re-drive them;
- ``delivered``: for a document with **more than one recipient** that was only
  partly delivered (the first send succeeded and the second failed, or the send
  cap ran out between them), the recipients it has already reached. Such a
  document is deliberately left unseen so a later cycle finishes it, and this is
  what stops that retry from messaging the first recipient again. Removed by
  ``mark_seen``, so a fully-delivered document leaves nothing behind;
- ``last_poll_at``: wall-clock timestamp of the last completed cycle.

Concurrency: the CLI wraps a poll run in :class:`PollStateLock` (an advisory
``flock`` on a sibling ``<state>.lock`` file) so two overlapping pollers cannot
double-send. The store itself never locks; the composition root decides.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

from sender.infrastructure.util import write_json_atomic

try:  # pragma: no cover - exercised on POSIX only
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

log = logging.getLogger(__name__)


class _PollStateBase:
    """Shared per-app seen/pending/abandoned bookkeeping; subclasses own persistence."""

    def __init__(self, max_seen: int = 500) -> None:
        self._max_seen = max_seen
        self._data: dict = {"apps": {}}
        self._defer_saves = False
        self._dirty = False

    def has_app(self, app_name: str) -> bool:
        return app_name in self._data.get("apps", {})

    def app_names(self) -> list[str]:
        return list(self._data.get("apps", {}))

    def seen(self, app_name: str, invoice_id: str) -> bool:
        return str(invoice_id) in self._entry(app_name)["seen"]

    def seen_ids(self, app_name: str) -> list[str]:
        return list(self._entry(app_name)["seen"])

    def mark_seen(self, app_name: str, invoice_id: str) -> None:
        entry = self._entry(app_name)
        sid = str(invoice_id)
        if sid not in entry["seen"]:
            entry["seen"].insert(0, sid)
            self._trim(entry)
        entry["pending"].pop(sid, None)
        entry["abandoned"].pop(sid, None)
        entry["delivered"].pop(sid, None)
        self._maybe_save()

    def mark_many_seen(self, app_name: str, invoice_ids: list[str]) -> None:
        entry = self._entry(app_name)
        existing = set(entry["seen"])
        fresh = [str(invoice_id) for invoice_id in invoice_ids if str(invoice_id) not in existing]
        if fresh:
            entry["seen"] = fresh + entry["seen"]
            self._trim(entry)
        for sid in fresh:
            entry["pending"].pop(sid, None)
            entry["abandoned"].pop(sid, None)
            entry["delivered"].pop(sid, None)
        self._maybe_save()

    def is_draining(self, app_name: str) -> bool:
        """Whether this app's last cycle hit the send cap and deferred work.

        Persisted because production runs one ``poll --once`` process per cron
        tick, so an in-memory flag would be lost exactly when the next cycle
        needs it: the deferred backlog sits behind an all-seen page and would
        never be paged to again.
        """
        return bool(self._entry(app_name).get("draining"))

    def set_draining(self, app_name: str, draining: bool) -> None:
        if draining:
            self._entry(app_name)["draining"] = True
        else:
            self._entry(app_name).pop("draining", None)
        self._maybe_save()

    def last_poll_at(self, app_name: str) -> float | None:
        value = self._entry(app_name).get("last_poll_at")
        return value if isinstance(value, (int, float)) else None

    def set_last_poll_at(self, app_name: str, timestamp: float) -> None:
        self._entry(app_name)["last_poll_at"] = timestamp
        self._maybe_save()

    def pending(self, app_name: str) -> dict:
        return self._entry(app_name)["pending"]

    def abandoned(self, app_name: str) -> dict:
        return self._entry(app_name)["abandoned"]

    def partly_delivered(self, app_name: str) -> dict:
        """Every document that reached *some* of its numbers but not all.

        The whole map, for ``poll-status``. A partly-delivered document is
        deliberately in none of the other three buckets — it is not ``seen``
        (because a number is still owed), not ``pending`` (nothing is retrying
        right now) and not ``abandoned`` (it is not given up) — so without this
        it is invisible to an operator looking at ``poll-status``, who would see
        an empty state and wrongly conclude nothing is in flight.
        """
        return dict(self._entry(app_name)["delivered"])

    def delivered(self, app_name: str, document_id: str) -> list[str]:
        """Recipients a partly-delivered document has *already* reached.

        A document can have more than one recipient (Daftra keeps two phone fields
        on a client), so it can be half-delivered: one send succeeded and the
        next failed, or the send cap ran out between them. Such a document is
        deliberately left **unseen** so a later cycle finishes it — but without
        this record the retry would send to the successful recipient a second
        time, which is the one outcome worse than a delay.
        """
        value = self._entry(app_name)["delivered"].get(str(document_id))
        return list(value) if isinstance(value, list) else []

    def record_delivered(self, app_name: str, document_id: str, recipients: list[str]) -> None:
        """Remember the recipients a document has reached, so a retry skips them.

        Merged rather than replaced, because the set grows within a cycle and the
        stored value may predate this cycle. Cleared by :meth:`mark_seen`, the only
        thing that retires a document, so a fully-delivered document leaves
        nothing behind.
        """
        entry = self._entry(app_name)
        sid = str(document_id)
        current = entry["delivered"].get(sid)
        merged = list(current) if isinstance(current, list) else []
        for recipient in recipients:
            if recipient not in merged:
                merged.append(recipient)
        if merged:
            entry["delivered"][sid] = merged
            self._trim_dict(entry["delivered"])
            self._maybe_save()

    def record_pending(
        self,
        app_name: str,
        invoice_id: str,
        *,
        error: str,
        action: str,
        count: int,
        next_attempt_at: float,
    ) -> None:
        entry = self._entry(app_name)
        sid = str(invoice_id)
        entry["pending"][sid] = {
            "count": int(count),
            "action": action,
            "error": error,
            "last_attempt_at": time.time(),
            "next_attempt_at": float(next_attempt_at),
        }
        self._trim_dict(entry["pending"])
        self._maybe_save()

    def record_abandoned(
        self,
        app_name: str,
        invoice_id: str,
        *,
        error: str,
        action: str,
        count: int,
    ) -> None:
        entry = self._entry(app_name)
        sid = str(invoice_id)
        entry["abandoned"][sid] = {
            "count": int(count),
            "action": action,
            "error": error,
            "last_attempt_at": time.time(),
        }
        self._trim_dict(entry["abandoned"])
        self._maybe_save()

    def reset_app(self, app_name: str) -> None:
        apps = self._data.get("apps", {})
        if app_name in apps:
            del apps[app_name]
            self._maybe_save()

    def clear_invoice(self, app_name: str, invoice_id: str) -> None:
        entry = self._entry(app_name)
        sid = str(invoice_id)
        removed = False
        if sid in entry["seen"]:
            entry["seen"] = [item for item in entry["seen"] if item != sid]
            removed = True
        if entry["pending"].pop(sid, None) is not None:
            removed = True
        if entry["abandoned"].pop(sid, None) is not None:
            removed = True
        if entry["delivered"].pop(sid, None) is not None:
            removed = True
        if removed:
            self._maybe_save()

    @contextmanager
    def batch(self):
        """Defer saves until the block exits; one atomic write for the whole block.

        A crash inside the block loses the deferred marks, which is the safe
        direction: already-sent invoices are re-sent (at-least-once) rather than
        silently dropped.
        """
        previous = self._defer_saves
        self._defer_saves = True
        try:
            yield
        finally:
            self._defer_saves = previous
            if not previous and self._dirty:
                self._save()

    def _entry(self, app_name: str) -> dict:
        apps = self._data.setdefault("apps", {})
        entry = apps.get(app_name)
        if entry is None:
            entry = {"seen": [], "pending": {}, "abandoned": {}, "delivered": {}, "last_poll_at": None}
            apps[app_name] = entry
        if not isinstance(entry.get("seen"), list):
            entry["seen"] = []
        if not isinstance(entry.get("pending"), dict):
            entry["pending"] = {}
        if not isinstance(entry.get("abandoned"), dict):
            entry["abandoned"] = {}
        # A state file written before multi-recipient delivery has no "delivered"
        # key at all; every document in it was single-recipient, so an absent map
        # legitimately means "nothing partly delivered" rather than "lost".
        if not isinstance(entry.get("delivered"), dict):
            entry["delivered"] = {}
        entry.pop("failed", None)  # legacy field from the pre-classification state
        return entry

    def _trim(self, entry: dict) -> None:
        if len(entry["seen"]) > self._max_seen:
            del entry["seen"][self._max_seen:]

    def _trim_dict(self, mapping: dict) -> None:
        while len(mapping) > self._max_seen:
            mapping.pop(next(iter(mapping)))

    def _maybe_save(self) -> None:
        self._dirty = True
        if not self._defer_saves:
            self._save()

    def _save(self) -> None:
        raise NotImplementedError


class JsonPollStateStore(_PollStateBase):
    def __init__(self, path: str, max_seen: int = 500) -> None:
        super().__init__(max_seen)
        self._path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {"apps": {}}
        except UnicodeDecodeError:
            # Not UTF-8 means the file holds binary garbage (a truncated write, a
            # half-copied file, something else pointed at this path). It is as
            # corrupt as unparseable JSON, and the poller must never crash because
            # of its own bookkeeping — but a corrupt file is also evidence of a
            # durability problem, so it is preserved (renamed aside) rather than
            # silently replaced, and the operator is told in no uncertain terms.
            self._preserve_corrupt("is not valid UTF-8")
            return {"apps": {}}
        try:
            data = json.loads(raw)
        except ValueError:
            self._preserve_corrupt("is corrupt")
            return {"apps": {}}
        if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
            self._preserve_corrupt("has an unexpected shape")
            return {"apps": {}}
        return data

    def _preserve_corrupt(self, reason: str) -> None:
        """Log loudly and move the corrupt state file aside for inspection.

        The poller still starts from an empty state (never crash on its own
        bookkeeping), but the original bytes are kept under a sibling name and
        the message names the backup path, so an operator can see what happened
        instead of the evidence vanishing under a fresh ``{"apps": {}}``.
        """
        log.error("poll state at %s %s; starting from an empty state", self._path, reason)
        try:
            backup = self._path.with_name(f"{self._path.name}.corrupt")
            os.replace(self._path, backup)
            log.error("the corrupt poll state was preserved at %s", backup)
        except OSError:
            log.error("could not preserve the corrupt poll state at %s", self._path)

    def _save(self) -> None:
        write_json_atomic(self._path, self._data)


class InMemoryPollStateStore(_PollStateBase):
    """Poll state kept in memory only; used by tests and one-shot offline runs."""

    def _save(self) -> None:
        pass


class PollStateLock:
    """Advisory exclusive lock guarding a poll state file against concurrent
    pollers (e.g. a stray manual run overlapping a cron/systemd ``--once`` run).

    Uses ``flock`` on a sibling ``<state>.lock`` file. ``flock`` is released by
    the OS when the process exits, so a crashed poller cannot leave a stale
    lock. On platforms without ``fcntl`` the lock degrades to a logged warning.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path).with_name(Path(path).name + ".lock")
        self._fd: int | None = None

    def acquire(self) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX platforms
            log.warning(
                "fcntl is unavailable; cannot lock %s against concurrent pollers",
                self._path,
            )
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError(
                f"another poller is already running against {self._path}; "
                "refusing to run two pollers on the same state file"
            )
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "PollStateLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()