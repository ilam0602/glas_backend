import base64
import hashlib
import hmac
import time

import pytest

import payout_provider
from payout_provider import CrossmintPayoutProvider, verify_crossmint_webhook


class _FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def _crossmint_env(monkeypatch):
    monkeypatch.setenv("CROSSMINT_API_KEY", "sk_staging_abc123")
    monkeypatch.setenv("CROSSMINT_BASE_URL", "https://staging.crossmint.com/api/2025-06-09")
    monkeypatch.setenv("CROSSMINT_CHAIN", "base")
    monkeypatch.setenv("CROSSMINT_TREASURY_WALLET", "treasury_wallet_1")


def test_crossmint_payout_provider_deliver_success(monkeypatch):
    _crossmint_env(monkeypatch)

    captured = {}

    def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(
            201,
            {
                "id": "transfer_test_1",
                "status": "pending",
                "onChain": {"userOperationHash": "0xUSEROP", "txId": "0xTXHASH"},
            },
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = CrossmintPayoutProvider()
    result = provider.deliver("0xabc123", 12.345, "USDC")

    # No real network call happened — `captured` was only populated by our
    # fake_post stand-in.
    assert (
        captured["url"]
        == "https://staging.crossmint.com/api/2025-06-09/wallets/treasury_wallet_1/tokens/base:usdc/transfers"
    )

    headers = captured["headers"]
    assert headers["X-API-KEY"] == "sk_staging_abc123"
    assert headers["Content-Type"] == "application/json"

    body = captured["json"]
    assert body == {
        "recipient": "0xabc123",
        "amount": "12.35",  # round(12.345, 2) -> banker's-adjacent float repr
        "transactionType": "regulated-transfer",
    }

    assert result["providerRef"] == "transfer_test_1"
    assert result["chain"] == "base"
    assert result["asset"] == "USDC"
    assert result["amount"] == 12.345
    assert result["marketRateUsd"] == 1.0
    assert result["settlementTxHash"] == "0xTXHASH"
    assert result["status"] == "pending"


def test_crossmint_payout_provider_settlement_tx_hash_none_when_unconfirmed(monkeypatch):
    _crossmint_env(monkeypatch)

    def fake_post(url, json=None, headers=None):
        return _FakeResponse(
            201,
            {
                "id": "transfer_test_2",
                "status": "awaiting-approval",
                "onChain": {"userOperationHash": "0xUSEROP", "txId": None},
            },
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = CrossmintPayoutProvider()
    result = provider.deliver("0xabc123", 5.0)
    assert result["settlementTxHash"] is None
    assert result["status"] == "awaiting-approval"


def test_crossmint_payout_provider_raises_on_error_response(monkeypatch):
    _crossmint_env(monkeypatch)

    def fake_post(url, json=None, headers=None):
        return _FakeResponse(422, {"error": "invalid_amount"}, text='{"error": "invalid_amount"}')

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = CrossmintPayoutProvider()
    with pytest.raises(RuntimeError) as exc_info:
        provider.deliver("0xabc", 1.0)
    assert "invalid_amount" in str(exc_info.value)


def test_get_payout_provider_crossmint_selects_crossmint_provider(monkeypatch):
    _crossmint_env(monkeypatch)
    monkeypatch.setenv("PAYOUT_PROVIDER", "crossmint")
    provider = payout_provider.get_payout_provider()
    assert isinstance(provider, CrossmintPayoutProvider)


# --- verify_crossmint_webhook -------------------------------------------

SAMPLE_BODY = (
    b'{"type": "transfer.updated", "data": '
    b'{"id": "transfer_test_1", "status": "success", "onChain": {"txId": "0xTX"}}}'
)


def _make_secret():
    key_bytes = b"0123456789abcdef0123456789abcdef"
    secret = "whsec_" + base64.b64encode(key_bytes).decode("utf-8")
    return secret, key_bytes


def _sign(key_bytes: bytes, svix_id: str, svix_timestamp: str, body: bytes) -> str:
    signed_content = f"{svix_id}.{svix_timestamp}.".encode("utf-8") + body
    digest = hmac.new(key_bytes, signed_content, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _headers(svix_id, svix_timestamp, svix_signature):
    return {
        "svix-id": svix_id,
        "svix-timestamp": svix_timestamp,
        "svix-signature": svix_signature,
    }


def test_valid_crossmint_signature_is_accepted(monkeypatch):
    secret, key_bytes = _make_secret()
    monkeypatch.setenv("CROSSMINT_WEBHOOK_SECRET", secret)

    svix_id = "msg_test1"
    svix_timestamp = str(int(time.time()))
    sig = _sign(key_bytes, svix_id, svix_timestamp, SAMPLE_BODY)

    headers = _headers(svix_id, svix_timestamp, f"v1,{sig}")
    assert verify_crossmint_webhook(SAMPLE_BODY, headers) is True


def test_multiple_signatures_one_match_is_accepted(monkeypatch):
    secret, key_bytes = _make_secret()
    monkeypatch.setenv("CROSSMINT_WEBHOOK_SECRET", secret)

    svix_id = "msg_test1"
    svix_timestamp = str(int(time.time()))
    sig = _sign(key_bytes, svix_id, svix_timestamp, SAMPLE_BODY)

    headers = _headers(svix_id, svix_timestamp, f"v1,bogus_sig_here {sig.replace('=', '')}== v1,{sig}")
    assert verify_crossmint_webhook(SAMPLE_BODY, headers) is True


def test_tampered_body_is_rejected(monkeypatch):
    secret, key_bytes = _make_secret()
    monkeypatch.setenv("CROSSMINT_WEBHOOK_SECRET", secret)

    svix_id = "msg_test1"
    svix_timestamp = str(int(time.time()))
    sig = _sign(key_bytes, svix_id, svix_timestamp, SAMPLE_BODY)

    tampered_body = SAMPLE_BODY.replace(b"success", b"failed")
    headers = _headers(svix_id, svix_timestamp, f"v1,{sig}")
    assert verify_crossmint_webhook(tampered_body, headers) is False


def test_bad_signature_is_rejected(monkeypatch):
    secret, _ = _make_secret()
    monkeypatch.setenv("CROSSMINT_WEBHOOK_SECRET", secret)

    svix_id = "msg_test1"
    svix_timestamp = str(int(time.time()))
    bogus_sig = base64.b64encode(b"not-a-real-signature").decode("utf-8")

    headers = _headers(svix_id, svix_timestamp, f"v1,{bogus_sig}")
    assert verify_crossmint_webhook(SAMPLE_BODY, headers) is False


def test_missing_headers_are_rejected(monkeypatch):
    secret, key_bytes = _make_secret()
    monkeypatch.setenv("CROSSMINT_WEBHOOK_SECRET", secret)

    svix_id = "msg_test1"
    svix_timestamp = str(int(time.time()))
    sig = _sign(key_bytes, svix_id, svix_timestamp, SAMPLE_BODY)

    assert verify_crossmint_webhook(SAMPLE_BODY, {}) is False
    assert verify_crossmint_webhook(
        SAMPLE_BODY, {"svix-timestamp": svix_timestamp, "svix-signature": f"v1,{sig}"}
    ) is False
    assert verify_crossmint_webhook(
        SAMPLE_BODY, {"svix-id": svix_id, "svix-signature": f"v1,{sig}"}
    ) is False
    assert verify_crossmint_webhook(
        SAMPLE_BODY, {"svix-id": svix_id, "svix-timestamp": svix_timestamp}
    ) is False


def test_stale_timestamp_is_rejected(monkeypatch):
    secret, key_bytes = _make_secret()
    monkeypatch.setenv("CROSSMINT_WEBHOOK_SECRET", secret)

    svix_id = "msg_test1"
    stale_timestamp = str(int(time.time()) - 400)  # >300s old
    sig = _sign(key_bytes, svix_id, stale_timestamp, SAMPLE_BODY)

    headers = _headers(svix_id, stale_timestamp, f"v1,{sig}")
    assert verify_crossmint_webhook(SAMPLE_BODY, headers) is False


def test_future_timestamp_is_rejected(monkeypatch):
    secret, key_bytes = _make_secret()
    monkeypatch.setenv("CROSSMINT_WEBHOOK_SECRET", secret)

    svix_id = "msg_test1"
    future_timestamp = str(int(time.time()) + 400)  # >300s in the future
    sig = _sign(key_bytes, svix_id, future_timestamp, SAMPLE_BODY)

    headers = _headers(svix_id, future_timestamp, f"v1,{sig}")
    assert verify_crossmint_webhook(SAMPLE_BODY, headers) is False


def test_unset_secret_env_fails_closed(monkeypatch):
    monkeypatch.delenv("CROSSMINT_WEBHOOK_SECRET", raising=False)

    svix_id = "msg_test1"
    svix_timestamp = str(int(time.time()))
    # No real secret to sign with — any signature must be rejected because
    # the env is unset, regardless of whether it happens to "match".
    headers = _headers(svix_id, svix_timestamp, "v1,irrelevant")
    assert verify_crossmint_webhook(SAMPLE_BODY, headers) is False


def test_malformed_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("CROSSMINT_WEBHOOK_SECRET", "not-whsec-prefixed")

    svix_id = "msg_test1"
    svix_timestamp = str(int(time.time()))
    headers = _headers(svix_id, svix_timestamp, "v1,irrelevant")
    assert verify_crossmint_webhook(SAMPLE_BODY, headers) is False
