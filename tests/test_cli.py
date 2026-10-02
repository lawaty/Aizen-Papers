"""Offline specs for the CLI builder resolution and the send-retry helpers."""

from sender.domain.errors import WhatsAppApiError
from sender.domain.templates import CleanTextTemplateBuilder, LegacyInvoiceTemplateBuilder
from sender.infrastructure.config import Settings
from sender.presentation.cli import _is_auto_builder, _is_param_mismatch, _resolve_builder


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
    once = cli_module._build_parser().parse_args(["poll", "--once", "--stub"])
    assert cli_module._run_poll(once, settings) == 1
    # A daemon-mode interrupt is the normal way to stop, so it stays exit 0.
    daemon = cli_module._build_parser().parse_args(["poll", "--stub"])
    assert cli_module._run_poll(daemon, settings) == 0


# --- cron contract: main() exit codes -------------------------------------------


def _main_env(monkeypatch, tmp_path, **extra):
    """Pin a hermetic environment and neuter .env loading for a cli.main() test."""
    from sender.presentation import cli as cli_module

    monkeypatch.setattr(cli_module, "load_dotenv", lambda: None)
    for key, value in {
        "WHATSAPP_ACCESS_TOKEN": "token",
        "WHATSAPP_PHONE_NUMBER_ID": "phone-id",
        "DAFTRA_API_KEY": "key",
        "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
        "POLL_STATE_PATH": str(tmp_path / "poll_state.json"),
        "STUB_INVOICES_PATH": str(tmp_path / "stub.json"),
        "POLL_STUB_STATE_PATH": str(tmp_path / "poll_stub.json"),
        **extra,
    }.items():
        monkeypatch.setenv(key, value)


def test_main_poll_once_stub_dry_run_exits_zero(monkeypatch, tmp_path, capsys):
    """The exact cron shape: one cycle, summary on stdout, exit 0."""
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    code = cli_module.main(["poll", "--once", "--stub", "--dry-run"])
    assert code == 0
    assert "stub:" in capsys.readouterr().out


def test_main_missing_whatsapp_credentials_exits_one(monkeypatch, tmp_path, capsys):
    """poll requires the two WhatsApp vars; a missing one is a handled error."""
    from sender.presentation import cli as cli_module

    _main_env(monkeypatch, tmp_path)
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    code = cli_module.main(["poll", "--once", "--stub"])
    assert code == 1
    assert "Missing required env vars" in capsys.readouterr().err


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
