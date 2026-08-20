import hashlib
from server import normalize_phone_e164, hash_phone_number


def test_normalize_accepts_valid_e164():
    assert normalize_phone_e164("+14155552671") == "+14155552671"


def test_normalize_strips_spaces_and_dashes():
    assert normalize_phone_e164("+1 415-555-2671") == "+14155552671"


def test_normalize_rejects_missing_plus():
    assert normalize_phone_e164("14155552671") is None


def test_normalize_rejects_too_short():
    assert normalize_phone_e164("+123") is None


def test_normalize_rejects_junk():
    assert normalize_phone_e164("not-a-phone") is None


def test_hash_is_stable_and_salted():
    salt = "s3cr3t"
    h = hash_phone_number("+14155552671", salt)
    assert h == hashlib.sha256((salt + "+14155552671").encode()).hexdigest()
    # Different salt -> different hash
    assert h != hash_phone_number("+14155552671", "other")
