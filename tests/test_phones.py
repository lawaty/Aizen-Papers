"""Phone normalization rules for Egyptian numbers."""

from sender.domain.phones import normalize_phone


def test_local_number_gets_default_country_prefix():
    assert normalize_phone("01027693262", "20") == "201027693262"


def test_e164_number_is_idempotent():
    assert normalize_phone("201027693262", "20") == "201027693262"


def test_plus_prefixed_e164_is_reduced_to_bare_digits():
    assert normalize_phone("+201027693262", "20") == "201027693262"