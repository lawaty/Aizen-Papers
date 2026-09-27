"""Phone normalization rules for Egyptian numbers."""

import pytest

from sender.domain.phones import normalize_phone


def test_local_number_gets_default_country_prefix():
    assert normalize_phone("01027693262", "20") == "201027693262"


def test_e164_number_is_idempotent():
    assert normalize_phone("201027693262", "20") == "201027693262"


def test_plus_prefixed_e164_is_reduced_to_bare_digits():
    assert normalize_phone("+201027693262", "20") == "201027693262"


def test_double_zero_trunk_is_reduced():
    assert normalize_phone("00201027693262", "20") == "201027693262"


def test_a_bare_local_number_without_a_country_code_is_rejected():
    """A 10-digit number missing its leading 0 would otherwise be sent as-is and
    rejected per recipient by Meta, abandoning the invoice. It must fail loudly
    instead so it surfaces as a skipped number the operator can fix."""
    with pytest.raises(ValueError, match="country code"):
        normalize_phone("1012345678", "20")


def test_an_short_national_string_is_rejected():
    with pytest.raises(ValueError, match="country code"):
        normalize_phone("12345678", "20")


def test_a_non_default_international_number_is_still_accepted():
    """A 12-digit number that is not the default country is a genuine E.164
    number from elsewhere (e.g. a Saudi mobile): normalizing it would corrupt it."""
    assert normalize_phone("966512345678", "20") == "966512345678"