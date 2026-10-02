"""Offline specs for the CLI builder resolution and the send-retry helpers."""

import pytest

from sender.domain.errors import WhatsAppApiError
from sender.domain.templates import (
    CleanTextTemplateBuilder,
    LegacyInvoiceTemplateBuilder,
    PaymentTemplateBuilder,
)
from sender.infrastructure.config import Settings
from sender.presentation.cli import (
    _build_parser,
    _is_auto_builder,
    _is_param_mismatch,
    _resolve_builder,
)
from sender.presentation.stubs import make_stub_payment


def cli_parser():
    """The parser, via the module, so these tests see exactly what the CLI sees."""
    from sender.presentation import cli as cli_module

    return _build_parser()


class _Args:
    def __init__(self, builder=None) -> None:
        self.builder = builder


def _settings(**over) -> Settings:
    values = dict(
        daftra_api_key="k",
        wa_access_token="t",
        wa_phone_number_id="p",
        daftra_base_url="https://acme.daftra.com/api2",
        default_country_code="20",
        wa_template_name="aizen_invoice",
        wa_template_lang="en",
        wa_waba_id="waba123",
        wa_template_builder="auto",
        wa_template_cache="template_state.json",
        wa_template_ttl=300.0,
    )
    values.update(over)
    return Settings(**values)


def test_resolve_env_pin_wins_without_a_flag_override():
    settings = _settings(wa_template_builder="new")
    assert isinstance(_resolve_builder(settings, None), CleanTextTemplateBuilder)


def test_resolve_env_pin_to_legacy_without_a_flag_override():
    settings = _settings(wa_template_builder="legacy")
    assert isinstance(_resolve_builder(settings, None), LegacyInvoiceTemplateBuilder)


def test_resolve_flagged_override_beats_the_env_pin():
    settings = _settings(wa_template_builder="legacy")
    assert isinstance(_resolve_builder(settings, "new"), CleanTextTemplateBuilder)


def test_resolve_auto_without_waba_id_falls_back_to_legacy():
    settings = _settings(wa_waba_id="")
    assert isinstance(_resolve_builder(settings, None), LegacyInvoiceTemplateBuilder)


def test_is_auto_builder_can_disable_the_auto_switch():
    settings = _settings(wa_template_builder="legacy")
    assert _is_auto_builder(_Args(), settings) is False
    assert _is_auto_builder(_Args("auto"), settings) is True
    assert _is_auto_builder(_Args(None), _settings()) is True


def test_is_auto_builder_normalizes_the_mode_before_comparing():
    # "AUTO" is the same request as "auto"; compared raw it silently turned the
    # builder-retry path off.
    assert _is_auto_builder(_Args(None), _settings(wa_template_builder="AUTO")) is True
    assert _is_auto_builder(_Args(" AUTO "), _settings(wa_template_builder="legacy")) is True
    assert _is_auto_builder(_Args("New"), _settings(wa_template_builder="auto")) is False
    assert _is_auto_builder(_Args(None), _settings(wa_template_builder=" NEW ")) is False


def test_is_param_mismatch_matches_the_132000_family():
    error = WhatsAppApiError(400, "mismatch", {"error": {"code": 132012}})
    assert _is_param_mismatch(error) is True


def test_is_param_mismatch_ignores_other_error_codes():
    error = WhatsAppApiError(400, "x", {"error": {"code": 131047}})
    assert _is_param_mismatch(error) is False


def test_invalid_builder_env_value_is_rejected():
    import pytest

    with pytest.raises(RuntimeError, match="WHATSAPP_TEMPLATE_BUILDER"):
        Settings.from_env({"WHATSAPP_TEMPLATE_BUILDER": "nwe"}, require_whatsapp=False, require_daftra=False)


def test_poll_builder_flag_defaults_to_none_so_the_env_pin_wins():
    from sender.presentation.cli import _build_parser

    # send/preview default to None; poll used to hard-default to "auto", which
    # overrode WHATSAPP_TEMPLATE_BUILDER for that command only.
    args = _build_parser().parse_args(["poll"])
    assert args.builder is None
    assert _is_auto_builder(args, _settings(wa_template_builder="new")) is False
    assert isinstance(_resolve_builder(_settings(wa_template_builder="new"), args.builder), CleanTextTemplateBuilder)
    assert _build_parser().parse_args(["poll", "--builder", "legacy"]).builder == "legacy"


def test_an_interrupted_once_run_exits_non_zero(monkeypatch, tmp_path):
    """A ``poll --once`` cycle killed mid-run must not look like success to cron:
    the scheduler has to be able to tell "completed" from "killed"."""
    from sender.presentation import cli as cli_module

    class _InterruptingPoller:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "InvoicePoller", _InterruptingPoller)
    settings = _settings(
        stub_invoices_path=str(tmp_path / "stub.json"),
        poll_stub_state_path=str(tmp_path / "poll.json"),
    )
    once = cli_module._build_parser().parse_args(["poll", "--once", "--invoice-stub", "--meta-stub"])
    assert cli_module._run_poll(once, settings) == 1
    # A daemon-mode interrupt is the normal way to stop, so it stays exit 0.
    daemon = cli_module._build_parser().parse_args(["poll", "--invoice-stub", "--meta-stub"])
    assert cli_module._run_poll(daemon, settings) == 0


# --- cron contract: main() exit codes -------------------------------------------


def _main_env(monkeypatch, tmp_path, **extra):
    """Pin a hermetic environment and neuter .env loading for a cli.main() test.

    Both halves of the dotenv step are stubbed, not just ``load_dotenv``: the
    lookup is what decides *which* file to read, and neutering only the reader
    would leave a test one refactor away from loading the developer's real
    credentials.

    Every pipeline's state and fixture path is redirected into *tmp_path*. That is
    not tidiness: a relative default would write ``poll_payments_state.stub.json``
    and ``stub_payments.json`` into the repository working directory, so a test run
    would leave behind a state file that silently marks real records as handled.
    """
    from sender.presentation import cli as cli_module

    monkeypatch.setattr(cli_module, "find_dotenv", lambda *a, **k: "")
    monkeypatch.setattr(cli_module, "load_dotenv", lambda *a, **k: None)
    for key, value in {
        "WHATSAPP_ACCESS_TOKEN": "token",
        "WHATSAPP_PHONE_NUMBER_ID": "phone-id",
        "DAFTRA_API_KEY": "key",
        "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
        "POLL_STATE_PATH": str(tmp_path / "poll_state.json"),
        "STUB_INVOICES_PATH": str(tmp_path / "stub.json"),
        "POLL_STUB_STATE_PATH": str(tmp_path / "poll_stub.json"),
        "POLL_PAYMENTS_STATE_PATH": str(tmp_path / "poll_payments_state.json"),
        "POLL_PAYMENTS_STUB_STATE_PATH": str(tmp_path / "poll_payments_state.stub.json"),
        "STUB_PAYMENTS_PATH": str(tmp_path / "stub_payments.json"),
        "POLL_CUSTOMERS_STATE_PATH": str(tmp_path / "poll_customers_state.json"),
        "POLL_CUSTOMERS_STUB_STATE_PATH": str(tmp_path / "poll_customers_state.stub.json"),
        "STUB_CUSTOMERS_PATH": str(tmp_path / "stub_customers.json"),
        "REPORT_DATA_DIR": str(tmp_path / "reports/data"),
        "REPORT_STUB_DIR": str(tmp_path / "reports.stub"),
        "REPORT_DIR": str(tmp_path / "reports"),
        **extra,
    }.items():
        monkeypatch.setenv(key, value)


def test_main_poll_once_stub_dry_run_exits_zero(monkeypatch, tmp_path, capsys):
    """The exact cron shape: one cycle, summary on stdout, exit 0."""
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    code = cli_module.main(["poll", "--once", "--invoice-stub", "--meta-stub", "--dry-run"])
    assert code == 0
    assert "stub:" in capsys.readouterr().out


def test_main_missing_whatsapp_credentials_exits_one(monkeypatch, tmp_path, capsys):
    """A real poll requires the two WhatsApp vars; a missing one is a handled error."""
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    code = cli_module.main(["poll", "--once"])
    assert code == 1
    assert "Missing required env vars" in capsys.readouterr().err


def test_a_stub_run_needs_no_whatsapp_credentials(monkeypatch, tmp_path, capsys):
    """A ``--meta-stub`` cycle never reaches Meta, so it must not demand a token.

    Two separate fixes meet here. ``--meta-stub`` is what makes "do not send" an
    explicit, per-dependency choice, and the ``need_whatsapp`` guard now consults
    it — which is also the bug ``.opencode/context/conventions.md`` recorded as
    known: the guard used to demand live credentials for a stub run even though
    the intent was for it to be offline. The real-run check above is what must
    still fail.
    """
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    assert cli_module.main(["poll", "--once", "--invoice-stub", "--meta-stub"]) == 0
    capsys.readouterr()


def test_a_stub_payment_run_needs_no_whatsapp_credentials(monkeypatch, tmp_path, capsys):
    """The same rule for the payments pipeline, which shares the guard."""
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    assert cli_module.main(["poll-payments", "--once", "--payment-stub", "--meta-stub"]) == 0
    assert "stub:" in capsys.readouterr().out


def test_a_stubbed_source_alone_still_reaches_meta(monkeypatch, tmp_path, capsys):
    """The whole point of splitting the flags: a stubbed *source* is not "offline".

    Stubbing Daftra says nothing about the sender. This pins that the two are
    independent, because conflating them is how a rehearsal ends up messaging a
    real customer with fixture data — which happened while this pipeline was
    built, and is why ``--meta-stub`` exists as its own flag.
    """
    from sender.infrastructure.whatsapp.client import WhatsAppClient
    from sender.presentation import cli as cli_module
    from sender.presentation.stubs import StubMessageSender

    _main_env(monkeypatch, tmp_path)
    settings = cli_module.Settings.from_env(require_daftra=False)
    assert isinstance(cli_module._meta_sender(settings, meta_stub=False), WhatsAppClient)
    captured = cli_module._meta_sender(settings, meta_stub=True)
    assert isinstance(captured, StubMessageSender)
    assert captured.send({"to": "201", "type": "text", "text": {"body": "x"}}) == {
        "messages": [{"id": "wamid.STUB"}]
    }
    assert len(captured.payloads) == 1
    capsys.readouterr()


def test_main_poll_lock_contention_exits_one(monkeypatch, tmp_path, capsys):
    """Two overlapping pollers must not double-send; the loser exits 1."""
    from sender.infrastructure.state import PollStateLock
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    lock = PollStateLock(str(tmp_path / "poll_state.json"))
    lock.acquire()
    try:
        code = cli_module.main(["poll", "--once"])
    finally:
        lock.release()
    assert code == 1
    assert "another poller is already running" in capsys.readouterr().err


def test_a_failed_report_render_is_logged_and_never_propagates(monkeypatch, caplog):
    """Reporting is best-effort: a render failure must not fail the send cycle.

    This pins the whole point of the handler. It called ``log.exception`` while
    this module never binds a ``log`` (it logs through the root logger), so the
    failure raised ``NameError`` *inside the except block* — and ``NameError`` is
    not in ``main``'s except tuple, so a broken report crashed the poll command
    and destroyed the original error. Silent by omission, loud in the wrong place.
    """
    from sender.presentation import cli as cli_module

    class _UnrenderableStore:
        def __init__(self, *args, **kwargs) -> None:
            raise OSError("no space left on device")

    class _Recorder:
        dates_written = {"2026-10-02"}

    monkeypatch.setattr(cli_module, "ReportStore", _UnrenderableStore)
    with caplog.at_level("ERROR"):
        cli_module._refresh_report_pages(_settings(), _Recorder(), stub=False)
    assert "could not refresh the HTML send report" in caplog.text


# --- the payments commands ---------------------------------------------------


def test_poll_payments_parses_its_own_flags():
    """It takes no ``--builder`` and no ``--attachment``: the payment template has
    one approved shape and no header document, so neither concept applies."""
    parser = cli_parser()
    args = parser.parse_args(
        ["poll-payments", "--once", "--limit", "5", "--max-sends", "0", "--payment-stub"]
    )
    assert (args.once, args.limit, args.max_sends, args.payment_stub) == (True, 5, 0, True)
    assert not hasattr(args, "builder")
    assert not hasattr(args, "attachment")


def test_the_generic_stub_flag_is_gone():
    """Each dependency is stubbed by its own flag, so a command line says exactly
    what it touches. A blanket ``--stub`` cannot come back."""
    with pytest.raises(SystemExit):
        cli_parser().parse_args(["poll", "--once", "--stub"])


def test_poll_status_payments_reads_the_payments_state_file(monkeypatch, tmp_path, capsys):
    """Payment ids and invoice ids overlap in the same account, so the two state
    files are different documents and the flag is the only safe way to say which."""
    from sender.infrastructure.state import JsonPollStateStore
    from sender.presentation import cli as cli_module

    invoice_state = tmp_path / "invoice.json"
    payment_state = tmp_path / "payments.json"
    JsonPollStateStore(str(invoice_state)).mark_seen("app1", "2")
    JsonPollStateStore(str(payment_state)).mark_seen("app1", "116")

    args = cli_parser().parse_args(["poll-status", "--payments"])
    assert cli_module._poll_state_path(
        _settings(payments_state_path=str(payment_state)), args
    ) == str(payment_state)
    cli_module._run_poll_status(args, _settings(payments_state_path=str(payment_state)))
    out = capsys.readouterr().out
    assert "seen: 1 payment(s)" in out

    args = cli_parser().parse_args(["poll-status"])
    assert cli_module._poll_state_path(_settings(), args) == "poll_state.json"


def test_poll_reset_payments_clears_the_payment_record(monkeypatch, tmp_path, capsys):
    from sender.infrastructure.state import JsonPollStateStore
    from sender.presentation import cli as cli_module

    path = tmp_path / "payments.json"
    JsonPollStateStore(str(path)).mark_seen("app1", "116")
    args = cli_parser().parse_args(
        ["poll-reset", "--app", "app1", "--payments", "--payment-id", "116", "--yes"]
    )
    code = cli_module._run_poll_reset(args, _settings(payments_state_path=str(path)))
    assert code == 0
    assert "cleared payment 116" in capsys.readouterr().out
    assert not JsonPollStateStore(str(path)).seen("app1", "116")


def test_poll_reset_still_requires_confirmation():
    from sender.presentation import cli as cli_module

    args = cli_parser().parse_args(["poll-reset", "--app", "app1", "--payments"])
    with pytest.raises(RuntimeError):
        cli_module._run_poll_reset(args, _settings())


def test_the_payment_builder_is_constructed_without_touching_the_graph_api(monkeypatch, tmp_path):
    """The payment template has one approved shape, so there is no builder to
    resolve and no reason to spend a Graph API round-trip learning that."""
    from sender.presentation import cli as cli_module

    def _explode(*a, **k):  # pragma: no cover - only runs on failure
        raise AssertionError("the payments path must not query the Graph API")

    monkeypatch.setattr(cli_module, "_template_registry", _explode)
    builder = cli_module._payment_builder(_settings())
    assert isinstance(builder, PaymentTemplateBuilder)
    assert builder.build(
        make_stub_payment(), "01027693262"
    )["template"]["name"] == "aizen_new_payment"


# --- the customers pipeline --------------------------------------------------


def _customer_settings(**over):
    """Settings with the customers pipeline's knobs explicit.

    ``over`` is merged last so a test can point a state path at its own tmp file
    without colliding with the defaults named here.
    """
    values = dict(
        customers_state_path="poll_customers_state.json",
        customers_stub_state_path="poll_customers_state.stub.json",
        stub_customers_path="stub_customers.json",
        customers_limit=10,
        poll_customers_max_sends_per_run=10,
        wa_customer_template_name="aizen_new_customer",
        wa_customer_template_lang="ar_EG",
    )
    values.update(over)
    return _settings(**values)


def test_the_customer_subcommands_exist():
    from sender.presentation.cli import _build_parser

    commands = _build_parser()._subparsers._group_actions[0].choices
    for name in (
        "poll-customers",
        "customers",
        "show-customer",
        "send-customer",
        "stub-customer-add",
    ):
        assert name in commands, name


def test_poll_status_customers_reads_the_customers_state_file(monkeypatch, tmp_path, capsys):
    """Client ids and invoice ids overlap in the same account, so the three state
    files are different documents and the flag is the only safe way to say which."""
    from sender.infrastructure.state import JsonPollStateStore
    from sender.presentation import cli as cli_module

    customer_state = tmp_path / "customers.json"
    JsonPollStateStore(str(customer_state)).mark_seen("app1", "7")

    args = cli_parser().parse_args(["poll-status", "--customers"])
    assert cli_module._poll_state_path(
        _customer_settings(customers_state_path=str(customer_state)), args
    ) == str(customer_state)
    cli_module._run_poll_status(args, _customer_settings(customers_state_path=str(customer_state)))
    assert "seen: 1 customer(s)" in capsys.readouterr().out


def test_the_three_state_paths_stay_distinct(monkeypatch, tmp_path):
    """The three-way coupling is the easiest thing to get half-right, and a missed
    case means ``poll-status --customers`` inspects the invoice file."""
    from sender.presentation import cli as cli_module

    settings = _customer_settings()
    def path(argv):
        return cli_module._poll_state_path(settings, cli_parser().parse_args(argv))

    assert path(["poll-status"]) == "poll_state.json"
    assert path(["poll-status", "--payments"]) == "poll_payments_state.json"
    assert path(["poll-status", "--customers"]) == "poll_customers_state.json"
    assert path(["poll-status", "--invoice-stub"]) == "poll_state.stub.json"
    assert path(["poll-status", "--payments", "--payment-stub"]) == "poll_payments_state.stub.json"
    assert (
        path(["poll-status", "--customers", "--customer-stub"])
        == "poll_customers_state.stub.json"
    )


def test_poll_reset_customers_clears_the_customer_record(monkeypatch, tmp_path, capsys):
    from sender.infrastructure.state import JsonPollStateStore
    from sender.presentation import cli as cli_module

    customer_state = tmp_path / "customers.json"
    store = JsonPollStateStore(str(customer_state))
    store.mark_seen("app1", "116")
    settings = _customer_settings(customers_state_path=str(customer_state))

    args = cli_parser().parse_args(
        ["poll-reset", "--app", "app1", "--customers", "--customer-id", "116", "--yes"]
    )
    assert args.document_id == "116"
    assert cli_module._run_poll_reset(args, settings) == 0
    out = capsys.readouterr().out
    assert f"cleared customer 116 for app 'app1' in {customer_state}" in out
    assert not JsonPollStateStore(str(customer_state)).seen("app1", "116")


def test_the_summary_names_the_customer_kind(capsys):
    """So an operator reading the one-shot summary is not told about invoices."""
    from sender.presentation.cli import _format_poll_summary

    summary = {
        "apps": [
            {
                "app": "stub",
                "ok": True,
                "listed": 3,
                "new": 1,
                "sent": 1,
                "skipped_no_phone": 0,
                "failed": 0,
                "pending": 0,
                "abandoned": 0,
                "kind": "customer",
                "first_run": False,
                "seeded": 0,
                "invoices": [],
            }
        ]
    }
    out = _format_poll_summary(summary)
    assert "sent 1" in out
    # The nouns are the per-document words, not hardcoded invoice wording.
    assert "invoice" not in out.lower()


def test_a_stub_customer_run_needs_no_whatsapp_credentials(monkeypatch, tmp_path, capsys):
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    assert (
        cli_module.main(["poll-customers", "--once", "--customer-stub", "--meta-stub"]) == 0
    )
    assert "stub:" in capsys.readouterr().out


def test_a_stubbed_source_alone_still_reaches_meta_for_customers(monkeypatch, tmp_path):
    """The customers pipeline must not weaken the rule that got this repo in
    trouble: a stubbed source is not "offline"."""
    from sender.infrastructure.whatsapp.client import WhatsAppClient
    from sender.presentation import cli as cli_module
    from sender.presentation.stubs import StubMessageSender

    _main_env(monkeypatch, tmp_path)
    settings = cli_module.Settings.from_env(require_daftra=False)
    assert isinstance(cli_module._meta_sender(settings, meta_stub=False), WhatsAppClient)
    assert isinstance(cli_module._meta_sender(settings, meta_stub=True), StubMessageSender)


def test_the_customer_source_stub_does_not_imply_a_stubbed_sender(monkeypatch, tmp_path):
    """`build_customer_service` with only ``customer_stub=True`` must still wire a
    real WhatsApp client — that is the separation that makes the flags meaningful."""
    from sender.infrastructure.whatsapp.client import WhatsAppClient
    from sender.presentation import cli as cli_module
    from sender.presentation.stubs import StubCustomerSource

    _main_env(monkeypatch, tmp_path)
    settings = cli_module.Settings.from_env(require_daftra=False)
    service = cli_module.build_customer_service(
        settings, customer_stub=True, meta_stub=False
    )
    assert isinstance(service._source, StubCustomerSource)
    assert isinstance(service._sender, WhatsAppClient)

    offline = cli_module.build_customer_service(
        settings, customer_stub=True, meta_stub=True
    )
    assert offline._sender is not None
    assert offline._sender.mode == "stub"


def test_the_customer_service_uses_the_welcome_template(monkeypatch, tmp_path):
    from sender.domain.templates import CustomerTemplateBuilder
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    settings = cli_module.Settings.from_env(require_daftra=False)
    service = cli_module.build_customer_service(
        settings, with_whatsapp=False, customer_stub=True
    )
    assert isinstance(service.builder, CustomerTemplateBuilder)
    assert service.builder._name == "aizen_new_customer"


def test_send_customer_dry_run_prints_the_payload_without_calling_meta(
    monkeypatch, tmp_path, capsys
):
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    assert (
        cli_module.main(
            [
                "send-customer",
                "--customer-id",
                "1",
                "--dry-run",
                "--customer-stub",
                "--meta-stub",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert '"aizen_new_customer"' in out
    assert '"ar_EG"' in out


def test_show_customer_raw_prints_the_client_envelope(monkeypatch, tmp_path, capsys):
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    assert (
        cli_module.main(["show-customer", "--customer-id", "1", "--raw", "--customer-stub"]) == 0
    )
    out = capsys.readouterr().out
    assert '"Client"' in out


def test_poll_customers_seeds_without_sending_on_the_first_run(
    monkeypatch, tmp_path, capsys
):
    """The highest-risk behaviour: a fresh state must not welcome every customer
    the account has ever had."""
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    assert (
        cli_module.main(["poll-customers", "--once", "--customer-stub", "--meta-stub"]) == 0
    )
    out = capsys.readouterr().out
    assert "sent 0" in out
    # Seeded, so the state file exists — pinned into tmp_path, never the repo.
    assert (tmp_path / "poll_customers_state.stub.json").exists()
    assert not (tmp_path / "poll_customers_state.json").exists()


def test_a_cap_deferral_is_not_reported_as_a_failure(capsys):
    """Regression: ``deferred_by_cap`` had no branch in the summary formatter and
    fell through to the ``else``, so a document that was merely throttled — never
    attempted, never failed, queued for the next cycle — was printed to the
    operator as "failed". That is the reading that sends someone hunting for a
    delivery problem that does not exist, and it is indistinguishable from a real
    send failure at a glance."""
    from sender.presentation.cli import _format_poll_summary

    summary = {
        "apps": [
            {
                "app": "stub",
                "ok": True,
                "listed": 2,
                "new": 2,
                "sent": 1,
                "skipped_no_phone": 0,
                "failed": 0,
                "pending": 0,
                "abandoned": 0,
                "kind": "customer",
                "first_run": False,
                "seeded": 0,
                "invoices": [
                    {"number": "000002", "status": "sent", "to": "201234567890"},
                    {
                        "number": "000001",
                        "status": "deferred_by_cap",
                        "to": "201027693262",
                        "delivered": ["201027693262"],
                        "remaining": ["2011155566677"],
                    },
                ],
            }
        ]
    }
    out = _format_poll_summary(summary)
    assert "deferred (per-run send cap)" in out
    assert "still to send 2011155566677" in out
    # The per-row lines must not claim a failure; only the `failed 0` counter
    # contains the word.
    row_lines = [ln for ln in out.splitlines() if ln.startswith("  0000")]
    assert all("failed" not in ln for ln in row_lines), out


def test_a_cap_deferral_with_no_progress_still_renders(capsys):
    """The deferral can land on the *first* recipient, when the cap was already
    spent by earlier documents. Then nothing was delivered and there is nothing to
    list, which must not crash the formatter."""
    from sender.presentation.cli import _format_poll_summary

    summary = {
        "apps": [
            {
                "app": "stub", "ok": True, "listed": 1, "new": 1, "sent": 0,
                "skipped_no_phone": 0, "failed": 0, "pending": 0, "abandoned": 0,
                "kind": "invoice", "first_run": False, "seeded": 0,
                "invoices": [{"number": "INV-001", "status": "deferred_by_cap"}],
            }
        ]
    }
    out = _format_poll_summary(summary)
    assert "deferred (per-run send cap)" in out


def test_a_partly_delivered_document_is_visible_in_poll_status(tmp_path, capsys, monkeypatch):
    """Regression: a partly-delivered document sits in none of seen / pending /
    abandoned, so ``poll-status`` showed nothing at all and an operator would
    conclude nothing was in flight while a customer was still owed a message."""
    from sender.infrastructure.state import JsonPollStateStore
    from sender.presentation import cli as cli_module

    state_path = tmp_path / "poll_customers_state.stub.json"
    store = JsonPollStateStore(str(state_path))
    store.record_delivered("app1", "7", ["201027693262"])

    args = cli_parser().parse_args(["poll-status", "--customers", "--customer-stub"])
    settings = _settings(customers_stub_state_path=str(state_path))
    assert cli_module._run_poll_status(args, settings) == 0

    out = capsys.readouterr().out
    assert "partly delivered" in out
    assert "customer 7" in out
    assert "201027693262" in out


def test_poll_status_is_quiet_when_nothing_is_partly_delivered(tmp_path, capsys):
    """The new section must not add noise to the normal, healthy case."""
    from sender.infrastructure.state import JsonPollStateStore
    from sender.presentation import cli as cli_module

    state_path = tmp_path / "poll_state.stub.json"
    JsonPollStateStore(str(state_path)).mark_seen("app1", "2")

    args = cli_parser().parse_args(["poll-status", "--invoice-stub"])
    settings = _settings(poll_stub_state_path=str(state_path))
    assert cli_module._run_poll_status(args, settings) == 0
    assert "partly delivered" not in capsys.readouterr().out


class TestPollStatusAll:
    """``--all`` answers "is every pipeline still cycling?" in one run.

    Bare ``poll-status`` reports invoices only. All three pipelines run every
    few minutes against three separate state files, so reporting one of them
    makes an idle pipeline indistinguishable from a dead one.
    """

    def test_all_three_pipelines_are_reported(self, tmp_path, capsys):
        from sender.infrastructure.state import JsonPollStateStore
        from sender.presentation import cli as cli_module

        invoice_state = tmp_path / "invoice.json"
        payment_state = tmp_path / "payments.json"
        customer_state = tmp_path / "customers.json"
        JsonPollStateStore(str(invoice_state)).mark_seen("app1", "2")
        JsonPollStateStore(str(payment_state)).mark_seen("app1", "116")
        JsonPollStateStore(str(customer_state)).mark_seen("app1", "7")

        settings = _settings(
            poll_state_path=str(invoice_state),
            payments_state_path=str(payment_state),
            **{
                "customers_state_path": str(customer_state),
            },
        )
        args = cli_parser().parse_args(["poll-status", "--all"])
        assert cli_module._run_poll_status(args, settings) == 0

        out = capsys.readouterr().out
        assert "seen: 1 invoice(s)" in out
        assert "seen: 1 payment(s)" in out
        assert "seen: 1 customer(s)" in out
        for label in ("--- invoices", "--- payments", "--- customers"):
            assert label in out

    def test_each_heading_names_the_file_it_read(self, tmp_path, capsys):
        from sender.infrastructure.state import JsonPollStateStore
        from sender.presentation import cli as cli_module

        invoice_state = tmp_path / "invoice.json"
        JsonPollStateStore(str(invoice_state)).mark_seen("app1", "2")
        args = cli_parser().parse_args(["poll-status", "--all"])
        cli_module._run_poll_status(
            args, _settings(poll_state_path=str(invoice_state))
        )
        out = capsys.readouterr().out
        assert str(invoice_state) in out

    def test_a_pipeline_with_no_state_says_so_rather_than_going_missing(
        self, tmp_path, capsys
    ):
        from sender.infrastructure.state import JsonPollStateStore
        from sender.presentation import cli as cli_module

        invoice_state = tmp_path / "invoice.json"
        JsonPollStateStore(str(invoice_state)).mark_seen("app1", "2")
        args = cli_parser().parse_args(["poll-status", "--all"])
        cli_module._run_poll_status(
            args,
            _settings(
                poll_state_path=str(invoice_state),
                payments_state_path=str(tmp_path / "absent-payments.json"),
            ),
        )
        out = capsys.readouterr().out
        assert "--- payments" in out
        assert "no poll state recorded yet" in out

    def test_partly_delivered_is_surfaced_under_all(self, tmp_path, capsys):
        from sender.infrastructure.state import JsonPollStateStore
        from sender.presentation import cli as cli_module

        invoice_state = tmp_path / "invoice.json"
        store = JsonPollStateStore(str(invoice_state))
        store.mark_seen("app1", "2")
        store.record_delivered("app1", "2", ["201022322634"])

        args = cli_parser().parse_args(["poll-status", "--all"])
        cli_module._run_poll_status(
            args, _settings(poll_state_path=str(invoice_state))
        )
        out = capsys.readouterr().out
        assert "partly delivered" in out
        assert "already reached 201022322634" in out

    def test_combining_all_with_a_pipeline_flag_is_refused(self):
        import pytest

        from sender.presentation import cli as cli_module

        for flag in ("--payments", "--customers", "--invoice-stub"):
            args = cli_parser().parse_args(["poll-status", "--all", flag])
            with pytest.raises(RuntimeError, match="--all reports every pipeline"):
                cli_module._run_poll_status(args, _settings())

    def test_single_pipeline_flags_still_work_unchanged(self, tmp_path, capsys):
        from sender.infrastructure.state import JsonPollStateStore
        from sender.presentation import cli as cli_module

        payment_state = tmp_path / "payments.json"
        JsonPollStateStore(str(payment_state)).mark_seen("app1", "116")
        args = cli_parser().parse_args(["poll-status", "--payments"])
        cli_module._run_poll_status(
            args, _settings(payments_state_path=str(payment_state))
        )
        out = capsys.readouterr().out
        assert "seen: 1 payment(s)" in out
        assert "invoice(s)" not in out
        assert "customer(s)" not in out
