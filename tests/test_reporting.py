from sender.domain.reporting import obfuscate_phone

def test_short_phone_is_fully_masked():
    for raw in ("12345", "123456", "1", "1234"):
        out = obfuscate_phone(raw)
        assert out == "•" * len(raw), (raw, out)

def test_normal_phone_keeps_country_and_last_four():
    assert obfuscate_phone("201027693262") == "20••••••3262"

def test_long_phone_never_leaks_a_dialable_number():
    out = obfuscate_phone("+201027693262")
    assert out.startswith("+2")
    assert out.endswith("3262")
    assert "201027693262" not in out
