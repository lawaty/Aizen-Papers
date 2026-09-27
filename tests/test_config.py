"""Offline specs for Settings env parsing, including multi-app poll config."""

import pytest

from sender.infrastructure.config import Settings


def test_single_app_backward_compat():
    settings = Settings.from_env(
        {
            "DAFTRA_API_KEY": "key1",
            "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
            "DAFTRA_TIMEOUT": "30",
        }
    )
    assert settings.daftra_api_key == "key1"
    assert settings.daftra_base_url == "https://acme.daftra.com/api2"
    assert len(settings.apps) == 1
    app = settings.apps[0]
    assert app.name == "acme"
    assert app.base_url == "https://acme.daftra.com/api2"
    assert app.api_key == "key1"
    assert app.timeout == 30.0
    assert settings.primary_app is app


def test_two_apps_parse_in_order():
    settings = Settings.from_env(
        {
            "DAFTRA_API_KEY": "key1",
            "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
            "DAFTRA2_BASE_URL": "https://other.daftra.com/api2",
            "DAFTRA2_API_KEY": "key2",
            "DAFTRA2_TIMEOUT": "20",
            "DAFTRA_APP2_NAME": "other",
        }
    )
    assert [app.name for app in settings.apps] == ["acme", "other"]
    assert settings.apps[1].api_key == "key2"
    assert settings.apps[1].timeout == 20.0


def test_app_name_defaults_to_the_subdomain():
    settings = Settings.from_env(
        {
            "DAFTRA_API_KEY": "key1",
            "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
            "DAFTRA2_BASE_URL": "https://second.daftra.com/api2",
            "DAFTRA2_API_KEY": "key2",
        }
    )
    assert settings.apps[0].name == "acme"
    assert settings.apps[1].name == "second"


def test_generic_host_falls_back_to_app1():
    settings = Settings.from_env({"DAFTRA_API_KEY": "key1"})
    assert settings.apps[0].name == "app1"


def test_partially_configured_slot_is_skipped():
    settings = Settings.from_env(
        {
            "DAFTRA_API_KEY": "key1",
            "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
            "DAFTRA2_BASE_URL": "https://other.daftra.com/api2",
        }
    )
    assert len(settings.apps) == 1


def test_scanning_continues_past_empty_slots():
    settings = Settings.from_env(
        {
            "DAFTRA_API_KEY": "key1",
            "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
            "DAFTRA3_BASE_URL": "https://third.daftra.com/api2",
            "DAFTRA3_API_KEY": "key3",
        }
    )
    assert [app.name for app in settings.apps] == ["acme", "third"]


def test_gap_in_slots_warns_but_keeps_later_apps(caplog):
    with caplog.at_level("WARNING"):
        settings = Settings.from_env(
            {
                "DAFTRA_API_KEY": "key1",
                "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
                "DAFTRA3_BASE_URL": "https://third.daftra.com/api2",
                "DAFTRA3_API_KEY": "key3",
            }
        )
    assert len(settings.apps) == 2
    assert any("DAFTRA2" in record.message for record in caplog.records)


def test_duplicate_app_names_warn_because_they_share_poll_state(caplog):
    with caplog.at_level("WARNING"):
        settings = Settings.from_env(
            {
                "DAFTRA_API_KEY": "key1",
                "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
                "DAFTRA2_BASE_URL": "https://acme.daftra.com/api2",
                "DAFTRA2_API_KEY": "key2",
            }
        )
    assert len(settings.apps) == 2
    assert any("duplicate Daftra app name" in record.message for record in caplog.records)
    assert any("DAFTRA_APP2_NAME" in record.message for record in caplog.records)


def test_duplicate_name_warning_names_the_real_slots_across_a_gap(caplog):
    # The list position of the second app is 2, but its slot is 3: telling the
    # operator to set DAFTRA_APP2_NAME would name a variable nothing reads.
    with caplog.at_level("WARNING"):
        settings = Settings.from_env(
            {
                "DAFTRA_API_KEY": "key1",
                "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
                "DAFTRA3_BASE_URL": "https://acme.daftra.com/api2",
                "DAFTRA3_API_KEY": "key3",
            }
        )
    assert [app.name for app in settings.apps] == ["acme", "acme"]
    messages = [
        record.message for record in caplog.records
        if "duplicate Daftra app name" in record.message
    ]
    assert len(messages) == 1
    assert "slot 1" in messages[0]
    assert "slot 3" in messages[0]
    assert "DAFTRA_APP3_NAME" in messages[0]
    assert "DAFTRA_APP2_NAME" not in messages[0]


def test_bad_url_is_rejected():
    with pytest.raises(RuntimeError, match="https"):
        Settings.from_env(
            {
                "DAFTRA_API_KEY": "key1",
                "DAFTRA_BASE_URL": "http://acme.daftra.com/api2",
            }
        )


def test_second_app_bad_url_is_rejected():
    with pytest.raises(RuntimeError, match="DAFTRA2_BASE_URL"):
        Settings.from_env(
            {
                "DAFTRA_API_KEY": "key1",
                "DAFTRA_BASE_URL": "https://acme.daftra.com/api2",
                "DAFTRA2_BASE_URL": "http://other.daftra.com/api2",
                "DAFTRA2_API_KEY": "key2",
            }
        )


def test_missing_key_raises_clear_error():
    with pytest.raises(RuntimeError, match="DAFTRA_API_KEY"):
        Settings.from_env({"DAFTRA_BASE_URL": "https://acme.daftra.com/api2"})


def test_require_daftra_passes_with_a_second_app_only():
    # The multi-tenant path the docs advertise: a slot-2-only deployment is fully
    # configured, so requiring the unprefixed DAFRA_API_KEY dead-ended it.
    settings = Settings.from_env(
        {
            "DAFTRA2_BASE_URL": "https://other.daftra.com/api2",
            "DAFTRA2_API_KEY": "key2",
        },
        require_daftra=True,
    )
    assert len(settings.apps) == 1
    assert settings.primary_app.name == "other"


def test_require_daftra_still_raises_without_any_app():
    with pytest.raises(RuntimeError, match="DAFTRA2_BASE_URL"):
        Settings.from_env({"DAFTRA_BASE_URL": "https://acme.daftra.com/api2"}, require_daftra=True)


def test_require_apps_raises_when_no_app_is_configured():
    with pytest.raises(RuntimeError, match="at least one Daftra app"):
        Settings.from_env({}, require_apps=True, require_daftra=False)


def test_require_apps_passes_with_a_second_app_only():
    settings = Settings.from_env(
        {
            "DAFTRA2_BASE_URL": "https://other.daftra.com/api2",
            "DAFTRA2_API_KEY": "key2",
        },
        require_apps=True,
        require_daftra=False,
    )
    assert len(settings.apps) == 1
    assert settings.apps[0].name == "other"


def test_poll_knobs_defaults():
    settings = Settings.from_env({"DAFTRA_API_KEY": "key1"})
    assert settings.poll_interval == 60.0
    assert settings.poll_state_path == "poll_state.json"
    assert settings.poll_limit == 10
    assert settings.poll_max_seen == 500
    assert settings.poll_max_backoff == 3600.0
    assert settings.poll_max_pages == 5
    assert settings.stub_invoices_path == "stub_invoices.json"
    assert settings.poll_stub_state_path == "poll_state.stub.json"


def test_poll_knobs_from_env():
    settings = Settings.from_env(
        {
            "DAFTRA_API_KEY": "key1",
            "POLL_INTERVAL": "120",
            "POLL_STATE_PATH": "state/poll.json",
            "POLL_LIMIT": "25",
            "POLL_MAX_SEEN": "100",
            "POLL_MAX_BACKOFF": "7200",
            "POLL_MAX_PAGES": "8",
            "STUB_INVOICES_PATH": "fixtures/stub.json",
            "POLL_STUB_STATE_PATH": "state/stub.json",
        }
    )
    assert settings.poll_interval == 120.0
    assert settings.poll_state_path == "state/poll.json"
    assert settings.poll_limit == 25
    assert settings.poll_max_seen == 100
    assert settings.poll_max_backoff == 7200.0
    assert settings.poll_max_pages == 8
    assert settings.stub_invoices_path == "fixtures/stub.json"
    assert settings.poll_stub_state_path == "state/stub.json"


def test_primary_app_raises_when_nothing_is_configured():
    settings = Settings.from_env({}, require_daftra=False)
    with pytest.raises(RuntimeError, match="No Daftra app is configured"):
        _ = settings.primary_app


# --- secrets & boolean parsing ---


def test_repr_hides_every_secret():
    settings = Settings.from_env(
        {
            "DAFTRA_API_KEY": "daftra-secret",
            "WHATSAPP_ACCESS_TOKEN": "wa-secret",
            "WHATSAPP_VERIFY_TOKEN": "webhook-secret",
        }
    )
    text = repr(settings)
    assert "daftra-secret" not in text
    assert "wa-secret" not in text
    assert "webhook-secret" not in text
    assert settings.wa_verify_token == "webhook-secret"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", "ON", " on "])
def test_dry_run_accepts_every_truthy_spelling(value):
    settings = Settings.from_env({"DAFTRA_API_KEY": "key1", "WHATSAPP_DRY_RUN": value})
    assert settings.dry_run is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_dry_run_treats_everything_else_as_sending_for_real(value):
    settings = Settings.from_env({"DAFTRA_API_KEY": "key1", "WHATSAPP_DRY_RUN": value})
    assert settings.dry_run is False


def test_dry_run_typo_is_warned_about_not_parsed_silently(caplog):
    """Misreading a dry-run flag means sending invoices for real; a typo must be
    shouted about rather than silently defaulting to a real send."""
    with caplog.at_level("WARNING"):
        settings = Settings.from_env({"DAFTRA_API_KEY": "key1", "WHATSAPP_DRY_RUN": "trrue"})
    assert settings.dry_run is False
    assert any("WHATSAPP_DRY_RUN" in record.message for record in caplog.records)


def test_dry_run_defaults_to_false():
    assert Settings.from_env({"DAFTRA_API_KEY": "key1"}).dry_run is False
