from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

# Prefer the account-specific root; this fallback is retained for compatibility.
DAFTRA_BASE_URL = "https://api.daftra.com/api2"
_DAFTRA_API_HOSTS = (".daftra.com", ".daftara.com")
_GENERIC_HOST_LABELS = ("api", "www", "app")

#: How the invoice PDF reaches the customer. See ``docs/guide/invoice-pdf.md``.
#: ``upload`` is the default because it is the only mode that needs nothing from
#: the operator: the sender renders the PDF and hands the bytes to Meta.
ATTACH_UPLOAD = "upload"
ATTACH_LINK = "link"
ATTACH_NONE = "none"
ATTACHMENT_MODES = (ATTACH_UPLOAD, ATTACH_LINK, ATTACH_NONE)

log = logging.getLogger(__name__)


def _validate_daftra_url(value: str, variable: str) -> None:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        scheme = parsed.scheme.lower()
    except ValueError as exc:
        raise RuntimeError(f"{variable} is not a valid URL") from exc
    if scheme != "https":
        raise RuntimeError(
            f"{variable} must use https://; refusing to send Daftra credentials over an insecure URL"
        )
    if not any(host.endswith(suffix) for suffix in _DAFTRA_API_HOSTS):
        raise RuntimeError(
            f"{variable} must use an account host ending in .daftra.com or .daftara.com; "
            "refusing to send Daftra credentials to another host. Use your account host, "
            "for example https://<account>.daftra.com/api2. If you genuinely need a different "
            "host, update the client explicitly rather than bypassing this validation."
        )


def _env_attachment(source: Mapping[str, str], key: str, default: str) -> str:
    """Read and validate an attachment mode, defaulting to *default*.

    An unset/blank value falls back, but a *recognised* value has to be spelled
    correctly: a typo like ``INVOICE_ATTACHMENT=uplod`` must not silently
    downgrade to a mode the operator did not ask for.
    """
    raw = (source.get(key) or "").strip().lower()
    if not raw:
        return default
    aliases = {
        "hosted": ATTACH_LINK,
        "url": ATTACH_LINK,
        "off": ATTACH_NONE,
        "disabled": ATTACH_NONE,
    }
    resolved = aliases.get(raw, raw)
    if resolved not in ATTACHMENT_MODES:
        raise RuntimeError(
            f"Invalid {key}: {raw!r}. Use one of {', '.join(ATTACHMENT_MODES)} "
            "(upload = render the PDF and upload it to Meta, link = point Meta at a "
            "publicly reachable PDF, none = send without an attachment)"
        )
    return resolved


def _app_name_from_url(base_url: str, fallback: str) -> str:
    """Derive a stable app name from the account subdomain, e.g.
    https://acme.daftra.com/api2 -> "acme"; the generic host falls back."""
    host = (urlsplit(base_url).hostname or "").lower()
    for suffix in _DAFTRA_API_HOSTS:
        if host.endswith(suffix):
            label = host[: -len(suffix)]
            if label and label not in _GENERIC_HOST_LABELS:
                return label
    return fallback


def _duplicate_name_hint(*slots: int) -> str:
    """The advice for a duplicated app name.

    Names are derived from the account subdomain and there is no override
    variable, so a duplicate means two apps share a subdomain. The hint names
    the DAFTRA<n>_BASE_URL the operator can change to give each account its own
    subdomain. Naming a *slot* rather than the position in the app list is what
    keeps the advice actionable once the numbering has a gap.
    """
    return (
        "app names come from the account subdomain, so give each account its "
        "own subdomain via "
        + " or ".join(
            "DAFTRA_BASE_URL" if slot == 1 else f"DAFTRA{slot}_BASE_URL"
            for slot in sorted(set(slots))
        )
    )


def _env_float(source: Mapping[str, str], key: str, default: float) -> float:
    raw = source.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {key}: {raw!r}") from exc


def _env_int(source: Mapping[str, str], key: str, default: int) -> int:
    raw = source.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {key}: {raw!r}") from exc


_BUILDER_MODES = ("auto", "legacy", "new")

#: Daftra's payment ``status`` values, which ``ClientPayment`` and
#: ``InvoicePayment`` share. Used only to reject a typo in ``POLL_PAYMENTS_STATUS``
#: early; the default (completed) is the only one the payments pipeline is expected
#: to run with. Verified against both endpoints: all 109 live client payments are
#: ``1``, and filtering by it behaves identically on either resource.
PAYMENT_STATUSES = ("0", "1", "2", "3", "4", "5")

#: The spellings accepted for an on/off environment flag, the same ones the
#: live-test opt-in uses. Misreading a flag that changes what reaches a customer
#: is the one direction not worth guessing in.
TRUTHY_FLAGS = ("1", "true", "yes", "on")
FALSY_FLAGS = ("0", "false", "no", "off")


def _env_flag(source: Mapping[str, str], key: str, default: bool) -> bool:
    """Read an on/off environment flag, defaulting to *default*.

    Every common spelling of "on"/"off" counts, and an unset or blank value keeps
    the default. Anything else is a typo: it logs a WARNING and keeps the default
    rather than raising, because a bridge flag must not be the thing that stops a
    poll run from starting.
    """
    raw = (source.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in TRUTHY_FLAGS:
        return True
    if raw in FALSY_FLAGS:
        return False
    log.warning("%s=%r is not a boolean; using the default (%s)", key, raw, default)
    return default


def _env_builder(source: Mapping[str, str]) -> str:
    mode = (source.get("WHATSAPP_TEMPLATE_BUILDER") or "auto").strip().lower() or "auto"
    if mode not in _BUILDER_MODES:
        raise RuntimeError(
            f"Invalid WHATSAPP_TEMPLATE_BUILDER: {mode!r}; expected one of {', '.join(_BUILDER_MODES)}"
        )
    return mode


def _env_payment_status(source: Mapping[str, str], key: str, default: str) -> str | None:
    """Read the server-side payment-status filter, or ``None`` for no filter.

    Three distinct cases, and conflating any two of them is how a payments
    pipeline goes quiet:

    - **the variable is absent** → *default* (completed), the safe narrowing;
    - **the variable is present but blank** → ``None``, a real mode meaning
      "announce every payment row". Blank is an instruction, not an absence;
    - **the variable holds anything else** → it must be one of Daftra's status
      values, or the run raises. A typo like ``completed`` would otherwise
      narrow the listing server-side to a status that does not exist, and the
      pipeline would find nothing and say nothing about it.
    """
    if key not in source:
        return default
    raw = (source.get(key) or "").strip()
    if not raw:
        return None
    if raw not in PAYMENT_STATUSES:
        raise RuntimeError(
            f"Invalid {key}: {raw!r}; expected one of {', '.join(PAYMENT_STATUSES)} "
            "(Daftra's payment statuses: 0 not completed, 1 completed, 2 pending, "
            "3 failed, 4 overpaid, 5 draft), or blank for no filter"
        )
    return raw


@dataclass(frozen=True)
class DaftraApp:
    """One Daftra tenant: its own subdomain base URL and its own API key."""

    name: str
    base_url: str
    api_key: str = field(repr=False)
    timeout: float = 15.0


@dataclass(frozen=True)
class Settings:
    daftra_api_key: str = field(repr=False)
    wa_access_token: str = field(repr=False)
    wa_phone_number_id: str
    daftra_base_url: str = DAFTRA_BASE_URL
    daftra_timeout: float = 15.0
    default_country_code: str = "20"
    wa_api_version: str = "v25.0"
    wa_template_name: str = "aizen_invoice"
    wa_template_lang: str = "en"
    wa_timeout: float = 15.0
    #: How long a 429 ``Retry-After`` wait is honored inside one send attempt.
    #: Meta may ask for a long wait; an unbounded sleep inside a ``poll --once``
    #: run would hold the poll-state flock and block later cron invocations, so
    #: the wait is capped and the next cycle retries instead.
    wa_max_retry_wait: float = 30.0
    wa_own_number: str = ""
    wa_waba_id: str = ""
    # repr=False like the other two secrets: a Settings dump (debug log, traceback
    # context) must not print the webhook verification secret.
    wa_verify_token: str = field(default="", repr=False)
    wa_template_cache: str = "template_state.json"
    wa_template_ttl: float = 300.0
    wa_template_builder: str = "auto"
    invoice_attachment: str = ATTACH_UPLOAD
    invoice_attachment_caption: str = ""
    #: Whether a template send rejected with a 132000-series code falls back to a
    #: free-form message. Default on because the live WABA has no approved
    #: `aizen_invoice` translation yet, so without it every send fails; it is a
    #: bridge, not a permanent mode (see docs/guide/template-contract.md).
    wa_freeform_fallback: bool = True
    dry_run: bool = False
    log_level: str = "INFO"
    apps: tuple[DaftraApp, ...] = ()
    poll_interval: float = 60.0
    poll_state_path: str = "poll_state.json"
    poll_limit: int = 10
    poll_max_seen: int = 500
    poll_max_backoff: float = 3600.0
    poll_max_pages: int = 5
    stub_invoices_path: str = "stub_invoices.json"
    poll_stub_state_path: str = "poll_state.stub.json"

    # -- payments pipeline --------------------------------------------------
    #
    # A parallel pipeline with its own template, state file, lock and send cap.
    # Deliberately separate from the invoice knobs above: the two pipelines must
    # never be able to starve each other, and payment ids collide with invoice
    # ids in both tenants' number spaces, so a shared state file would have one
    # pipeline silently retiring the other's records. See docs/design.md § 11.

    #: The Meta template that carries payment confirmations. Separate from
    #: ``wa_template_name`` because it is a different approved template, in a
    #: different language, with a different body.
    wa_payment_template_name: str = "aizen_new_payment"
    #: ``ar_EG`` — the only language the payment template is approved in. Asking
    #: for anything else is a 132001 at send time.
    wa_payment_template_lang: str = "ar_EG"
    #: Where payments are marked handled. Gitignored and machine-specific for the
    #: same reason ``poll_state_path`` is: uploading one marks real payments as
    #: already announced.
    payments_state_path: str = "poll_payments_state.json"
    payments_stub_state_path: str = "poll_payments_state.stub.json"
    stub_payments_path: str = "stub_payments.json"
    #: Page size for the payment listing. Payments arrive in bursts of a different
    #: shape to invoices, so it gets its own knob rather than sharing ``POLL_LIMIT``.
    payments_limit: int = 10
    #: Which payment statuses to announce. ``"1"`` (completed) by default,
    #: because the template tells the customer their balance was updated and only
    #: a completed payment does that. ``None`` = no filter.
    payments_status_filter: str | None = "1"
    #: Hard ceiling on send attempts per payment poll cycle. A third cap, separate
    #: from the invoice one, so a payment backlog can never eat the invoice
    #: pipeline's budget. 0 disables.
    poll_payments_max_sends_per_run: int = 10

    # -- customers pipeline -------------------------------------------------
    #
    # The third pipeline, with the same separation rationale as payments (see
    # docs/design.md § 12): its own template, state file, lock and send cap, so a
    # welcome backlog can never consume the invoice or payment budgets. Client ids
    # collide with invoice and payment ids in the same account's number space, so
    # a shared state file would let one pipeline silently retire another's
    # records.

    #: The Meta template that carries new-customer welcomes.
    wa_customer_template_name: str = "aizen_new_customer"
    #: ``ar_EG`` — the only language the welcome template is approved in. Anything
    #: else is a 132001 at send time, so it is pinned by a test.
    wa_customer_template_lang: str = "ar_EG"
    customers_state_path: str = "poll_customers_state.json"
    customers_stub_state_path: str = "poll_customers_state.stub.json"
    stub_customers_path: str = "stub_customers.json"
    #: Page size for the client listing. Daftra's client endpoint answers an
    #: arbitrary order unless told otherwise, and the poller needs newest-first
    #: to walk pages safely — see ``DaftraClient.list_customers``.
    customers_limit: int = 10
    #: Hard ceiling on send attempts per customer poll cycle, separate again so
    #: none of the three pipelines can starve the others. 0 disables.
    poll_customers_max_sends_per_run: int = 10
    #: Whether a rejected welcome falls back to a free-form message. **Off by
    #: default**, and this is the one place the customers pipeline deliberately
    #: differs from the other two.
    #:
    #: A welcome goes to a brand-new number, which is by definition outside
    #: WhatsApp's 24-hour customer-service window — and that window is the only
    #: place free-form text is deliverable. So with fallback on, every template
    #: error would burn a doomed extra request and log a confusing second error.
    #: With it off, the engine's existing behaviour applies: the customer stays
    #: **pending** on the template error and flows on once the template is fixed.
    #: It also keeps marketing text off a channel with no opt-in evidence.
    wa_customer_freeform_fallback: bool = False

    #: Hard ceiling on send attempts per poll cycle, across all apps. This is
    #: deliberately separate from ``poll_limit`` (a listing page size): paging
    #: can fetch up to ``limit * max_pages`` invoices, so the page size alone is
    #: not a bound on how many messages one run can put in front of Meta. The
    #: default is far below Meta's published throughput and exists mainly to
    #: stop a pathological backlog or a runaway loop from blasting a number.
    #: 0 disables the cap.
    poll_max_sends_per_run: int = 10
    #: Write human-readable HTML pages of sent messages under ``report_dir``.
    report_enabled: bool = True
    #: Where the browsable HTML lives. Relative paths resolve against the
    #: working directory, so the cron wrapper owns the CWD; deployments under a
    #: web root set an absolute path here.
    report_dir: str = "reports"
    #: Where the append-only JSONL source of truth lives. Kept separate from the
    #: HTML so it can be moved off the web root while the pages stay browsable.
    report_data_dir: str = "reports/data"
    report_stub_dir: str = "reports.stub"
    #: Days of history to keep. 0 disables pruning.
    report_retention_days: int = 90
    #: Mask the middle of recipient numbers in the pages. On by default because
    #: these are served from a public web root and a full number is personal data
    #: that lets a reader contact the customer.
    report_obfuscate_phone: bool = True

    @property
    def primary_app(self) -> DaftraApp:
        """The first/only configured app, used by the non-poll commands."""
        if not self.apps:
            raise RuntimeError(
                "No Daftra app is configured; set DAFTRA_API_KEY "
                "(or DAFTRA2_BASE_URL + DAFTRA2_API_KEY, ...)"
            )
        return self.apps[0]

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        require_whatsapp: bool = False,
        require_daftra: bool = True,
        require_apps: bool = False,
    ) -> "Settings":
        source = os.environ if env is None else env
        if require_whatsapp:
            missing_whatsapp = [
                name
                for name in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")
                if not (source.get(name) or "").strip()
            ]
            if missing_whatsapp:
                raise RuntimeError(f"Missing required env vars: {', '.join(missing_whatsapp)}")

        daftra_api_key = (source.get("DAFTRA_API_KEY") or "").strip()
        base_url = ((source.get("DAFTRA_BASE_URL") or "").strip().rstrip("/") or DAFTRA_BASE_URL)
        _validate_daftra_url(base_url, "DAFTRA_BASE_URL")
        apps = cls._apps_from_env(source, daftra_api_key, base_url)
        # The app scan has to run *before* this check: a deployment that only
        # fills a numbered slot (DAFTRA2_BASE_URL + DAFTRA2_API_KEY) is fully
        # configured, so demanding the unprefixed DAFTRA_API_KEY dead-ended the
        # multi-tenant path the docs advertise. What the commands need is at
        # least one usable app, from either spelling.
        if (require_daftra or require_apps) and not apps:
            raise RuntimeError(
                "Missing required env vars: at least one Daftra app must be configured "
                "(DAFTRA_API_KEY, or DAFTRA2_BASE_URL + DAFTRA2_API_KEY, ...)"
            )
        return cls(
            daftra_api_key=daftra_api_key,
            wa_access_token=(source.get("WHATSAPP_ACCESS_TOKEN") or "").strip(),
            wa_phone_number_id=(source.get("WHATSAPP_PHONE_NUMBER_ID") or "").strip(),
            daftra_base_url=base_url,
            daftra_timeout=_env_float(source, "DAFTRA_TIMEOUT", 15.0),
            default_country_code=(source.get("DEFAULT_COUNTRY_CODE") or "20").strip(),
            wa_api_version=(source.get("WHATSAPP_API_VERSION") or "v25.0").strip(),
            wa_template_name=(source.get("WHATSAPP_TEMPLATE_NAME") or "aizen_invoice").strip(),
            wa_template_lang=(source.get("WHATSAPP_TEMPLATE_LANG") or "en").strip(),
            wa_timeout=_env_float(source, "WHATSAPP_TIMEOUT", 15.0),
            wa_max_retry_wait=_env_float(source, "WHATSAPP_MAX_RETRY_WAIT", 30.0),
            wa_own_number=(source.get("WA_OWN_NUMBER") or "").strip(),
            wa_waba_id=(source.get("WHATSAPP_WABA_ID") or "").strip(),
            wa_verify_token=(source.get("WHATSAPP_VERIFY_TOKEN") or "").strip(),
            wa_template_cache=(source.get("WHATSAPP_TEMPLATE_CACHE") or "template_state.json").strip(),
            wa_template_ttl=_env_float(source, "WHATSAPP_TEMPLATE_TTL", 300.0),
            wa_template_builder=_env_builder(source),
            invoice_attachment=_env_attachment(source, "INVOICE_ATTACHMENT", ATTACH_UPLOAD),
            invoice_attachment_caption=(source.get("INVOICE_ATTACHMENT_CAPTION") or "").strip(),
            wa_freeform_fallback=_env_flag(source, "WHATSAPP_FREEFORM_FALLBACK", True),
            # The warning parser is load-bearing here: misreading a dry-run flag
            # means sending invoices for real — the most dangerous direction to
            # be wrong in — so a typo must at least be shouted about, not parsed
            # silently to False.
            dry_run=_env_flag(source, "WHATSAPP_DRY_RUN", False),
            log_level=(source.get("LOG_LEVEL") or "INFO").strip(),
            apps=apps,
            poll_interval=_env_float(source, "POLL_INTERVAL", 60.0),
            poll_state_path=(source.get("POLL_STATE_PATH") or "poll_state.json").strip(),
            poll_limit=_env_int(source, "POLL_LIMIT", 10),
            poll_max_seen=_env_int(source, "POLL_MAX_SEEN", 500),
            poll_max_backoff=_env_float(source, "POLL_MAX_BACKOFF", 3600.0),
            poll_max_pages=_env_int(source, "POLL_MAX_PAGES", 5),
            stub_invoices_path=(source.get("STUB_INVOICES_PATH") or "stub_invoices.json").strip(),
            poll_stub_state_path=(source.get("POLL_STUB_STATE_PATH") or "poll_state.stub.json").strip(),
            wa_payment_template_name=(
                source.get("WHATSAPP_PAYMENT_TEMPLATE_NAME") or "aizen_new_payment"
            ).strip(),
            wa_payment_template_lang=(
                source.get("WHATSAPP_PAYMENT_TEMPLATE_LANG") or "ar_EG"
            ).strip(),
            payments_state_path=(
                source.get("POLL_PAYMENTS_STATE_PATH") or "poll_payments_state.json"
            ).strip(),
            payments_stub_state_path=(
                source.get("POLL_PAYMENTS_STUB_STATE_PATH") or "poll_payments_state.stub.json"
            ).strip(),
            stub_payments_path=(source.get("STUB_PAYMENTS_PATH") or "stub_payments.json").strip(),
            payments_limit=_env_int(source, "POLL_PAYMENTS_LIMIT", 10),
            payments_status_filter=_env_payment_status(
                source, "POLL_PAYMENTS_STATUS", "1"
            ),
            poll_payments_max_sends_per_run=_env_int(
                source, "POLL_PAYMENTS_MAX_SENDS_PER_RUN", 10
            ),
            wa_customer_template_name=(
                source.get("WHATSAPP_CUSTOMER_TEMPLATE_NAME") or "aizen_new_customer"
            ).strip(),
            wa_customer_template_lang=(
                source.get("WHATSAPP_CUSTOMER_TEMPLATE_LANG") or "ar_EG"
            ).strip(),
            customers_state_path=(
                source.get("POLL_CUSTOMERS_STATE_PATH") or "poll_customers_state.json"
            ).strip(),
            customers_stub_state_path=(
                source.get("POLL_CUSTOMERS_STUB_STATE_PATH") or "poll_customers_state.stub.json"
            ).strip(),
            stub_customers_path=(source.get("STUB_CUSTOMERS_PATH") or "stub_customers.json").strip(),
            customers_limit=_env_int(source, "POLL_CUSTOMERS_LIMIT", 10),
            poll_customers_max_sends_per_run=_env_int(
                source, "POLL_CUSTOMERS_MAX_SENDS_PER_RUN", 10
            ),
            # Default False, unlike wa_freeform_fallback: see the field comment.
            wa_customer_freeform_fallback=_env_flag(
                source, "WHATSAPP_CUSTOMER_FREEFORM_FALLBACK", False
            ),
            poll_max_sends_per_run=_env_int(source, "POLL_MAX_SENDS_PER_RUN", 10),
            report_enabled=_env_flag(source, "REPORT_ENABLED", True),
            report_dir=(source.get("REPORT_DIR") or "reports").strip(),
            report_data_dir=(source.get("REPORT_DATA_DIR") or "reports/data").strip(),
            report_stub_dir=(source.get("REPORT_STUB_DIR") or "reports.stub").strip(),
            report_retention_days=_env_int(source, "REPORT_RETENTION_DAYS", 90),
            report_obfuscate_phone=_env_flag(source, "REPORT_OBFUSCATE_PHONE", True),
        )

    @staticmethod
    def _apps_from_env(source: Mapping[str, str], daftra_api_key: str, base_url: str) -> tuple[DaftraApp, ...]:
        """App 1 uses the unprefixed vars; extra apps use numbered slots
        (DAFTRA2_*, DAFTRA3_*, ...). Slots 2..9 are scanned; an empty slot is
        skipped (with a warning if a later slot is configured) rather than
        stopping the scan, so a gap cannot silently drop a later app. Every app
        name is derived from its account subdomain; apps that end up with the
        same name are warned about because they would share poll state."""
        apps: list[DaftraApp] = []
        # The slot each app came from, tracked alongside the list: the duplicate
        # name warning has to name the DAFTRA<n>_BASE_URL the operator can
        # change, and the list position is not the slot once the numbering has
        # a gap.
        slots: list[int] = []
        if daftra_api_key:
            apps.append(
                DaftraApp(
                    name=_app_name_from_url(base_url, "app1"),
                    base_url=base_url,
                    api_key=daftra_api_key,
                    timeout=_env_float(source, "DAFTRA_TIMEOUT", 15.0),
                )
            )
            slots.append(1)
        for slot in range(2, 10):
            slot_base = (source.get(f"DAFTRA{slot}_BASE_URL") or "").strip().rstrip("/")
            slot_key = (source.get(f"DAFTRA{slot}_API_KEY") or "").strip()
            if not slot_base and not slot_key:
                continue
            if slot_base and slot_key:
                _validate_daftra_url(slot_base, f"DAFTRA{slot}_BASE_URL")
                name = _app_name_from_url(slot_base, f"app{slot}")
                apps.append(
                    DaftraApp(
                        name=name,
                        base_url=slot_base,
                        api_key=slot_key,
                        timeout=_env_float(source, f"DAFTRA{slot}_TIMEOUT", 15.0),
                    )
                )
                slots.append(slot)
            else:
                log.warning(
                    "DAFTRA%d is partially configured (needs both *_BASE_URL and *_API_KEY); skipping",
                    slot,
                )
        configured_slots = [
            slot
            for slot in range(2, 10)
            if (source.get(f"DAFTRA{slot}_BASE_URL") or "").strip()
            or (source.get(f"DAFTRA{slot}_API_KEY") or "").strip()
        ]
        for slot in range(2, 10):
            if slot in configured_slots:
                continue
            if any(later > slot for later in configured_slots):
                log.warning(
                    "DAFTRA%d is empty but a later slot is configured; the slot "
                    "numbering has a gap (the poller still picks the later app up)",
                    slot,
                )
        seen_names: dict[str, tuple[int, str]] = {}
        for app, slot in zip(apps, slots):
            if app.name in seen_names:
                first_slot, first_base = seen_names[app.name]
                log.warning(
                    "duplicate Daftra app name %r (slot %d %s and slot %d %s); apps "
                    "sharing a name share poll state — %s",
                    app.name, first_slot, first_base, slot, app.base_url,
                    _duplicate_name_hint(first_slot, slot),
                )
            else:
                seen_names[app.name] = (slot, app.base_url)
        return tuple(apps)
