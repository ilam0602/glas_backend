import base64
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from payout_provider import verify_bridge_webhook


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_key, public_pem


def _sign(private_key, timestamp_str: str, body: bytes) -> str:
    signed_string = timestamp_str.encode("utf-8") + b"." + body
    signature = private_key.sign(signed_string, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def _header(timestamp_str: str, sig_b64: str) -> str:
    return f"t={timestamp_str},v0={sig_b64}"


@pytest.fixture
def keypair():
    return _generate_keypair()


SAMPLE_BODY = (
    b'{"type": "kyc_link.updated", "object": '
    b'{"customer_id": "customer_abc", "kyc_status": "approved"}}'
)


def test_valid_signature_is_accepted(monkeypatch, keypair):
    private_key, public_pem = keypair
    monkeypatch.setenv("BRIDGE_WEBHOOK_PUBLIC_KEY", public_pem)

    timestamp_str = str(int(time.time() * 1000))
    sig = _sign(private_key, timestamp_str, SAMPLE_BODY)

    assert verify_bridge_webhook(SAMPLE_BODY, _header(timestamp_str, sig)) is True


def test_tampered_body_is_rejected(monkeypatch, keypair):
    private_key, public_pem = keypair
    monkeypatch.setenv("BRIDGE_WEBHOOK_PUBLIC_KEY", public_pem)

    timestamp_str = str(int(time.time() * 1000))
    sig = _sign(private_key, timestamp_str, SAMPLE_BODY)

    tampered_body = SAMPLE_BODY.replace(b"approved", b"rejected")
    assert verify_bridge_webhook(tampered_body, _header(timestamp_str, sig)) is False


def test_bad_signature_is_rejected(monkeypatch, keypair):
    _, public_pem = keypair
    monkeypatch.setenv("BRIDGE_WEBHOOK_PUBLIC_KEY", public_pem)

    timestamp_str = str(int(time.time() * 1000))
    bogus_sig = base64.b64encode(b"not-a-real-signature").decode("utf-8")

    assert verify_bridge_webhook(SAMPLE_BODY, _header(timestamp_str, bogus_sig)) is False


def test_missing_or_malformed_signature_header_is_rejected(monkeypatch, keypair):
    _, public_pem = keypair
    monkeypatch.setenv("BRIDGE_WEBHOOK_PUBLIC_KEY", public_pem)

    assert verify_bridge_webhook(SAMPLE_BODY, None) is False
    assert verify_bridge_webhook(SAMPLE_BODY, "") is False
    assert verify_bridge_webhook(SAMPLE_BODY, "garbage-header-no-kv-pairs") is False
    assert verify_bridge_webhook(SAMPLE_BODY, "t=123") is False  # missing v0
    assert verify_bridge_webhook(SAMPLE_BODY, "v0=abc") is False  # missing t


def test_stale_timestamp_is_rejected(monkeypatch, keypair):
    private_key, public_pem = keypair
    monkeypatch.setenv("BRIDGE_WEBHOOK_PUBLIC_KEY", public_pem)

    stale_timestamp_str = str(int(time.time() * 1000) - 400_000)  # >300s old
    sig = _sign(private_key, stale_timestamp_str, SAMPLE_BODY)

    assert verify_bridge_webhook(SAMPLE_BODY, _header(stale_timestamp_str, sig)) is False


def test_unset_public_key_env_fails_closed(monkeypatch, keypair):
    private_key, _ = keypair
    monkeypatch.delenv("BRIDGE_WEBHOOK_PUBLIC_KEY", raising=False)

    timestamp_str = str(int(time.time() * 1000))
    sig = _sign(private_key, timestamp_str, SAMPLE_BODY)

    assert verify_bridge_webhook(SAMPLE_BODY, _header(timestamp_str, sig)) is False


def test_wrong_public_key_is_rejected(monkeypatch, keypair):
    # Signed with one keypair's private key, verified against a DIFFERENT
    # keypair's public key — must not be accepted.
    private_key, _ = keypair
    _, other_public_pem = _generate_keypair()
    monkeypatch.setenv("BRIDGE_WEBHOOK_PUBLIC_KEY", other_public_pem)

    timestamp_str = str(int(time.time() * 1000))
    sig = _sign(private_key, timestamp_str, SAMPLE_BODY)

    assert verify_bridge_webhook(SAMPLE_BODY, _header(timestamp_str, sig)) is False
