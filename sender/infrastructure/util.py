from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests


def json_or_none(response: requests.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        return None


def write_json_atomic(path, data) -> None:
    """Atomically write *data* as JSON to *path*.

    The temp file is a pid-qualified sibling (``.<name>.<pid>.tmp``), so two
    concurrent writers cannot clobber each other's temp file, and ``os.replace``
    makes the final swap atomic. The temp file is removed on failure.

    Durability: the temp file is ``fsync``ed before the swap and the directory
    is ``fsync``ed after it, so a power loss cannot leave the rename durable but
    the data not — the failure mode that silently reset the poll state to empty
    and re-seeded a whole customer history.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def write_text_atomic(path, text: str) -> None:
    """Atomically write *text* to *path*, with the same durability as above.

    Used for the generated HTML reports: a reader following a link must never
    catch a half-written page, and a crash mid-regeneration must leave the
    previous day's page intact rather than truncating it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_dir(directory: Path) -> None:
    """Make the rename durable by syncing the directory entry itself.

    A directory fsync is not portable (Windows rejects it), so a platform that
    refuses one is logged away silently — the file was already synced, and the
    directory sync is the belt-and-braces half of the durability pair.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)