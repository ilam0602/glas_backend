import pytest
import payout_provider
from payout_provider import (
    PayoutProvider,
    FakePayoutProvider,
    BridgePayoutProvider,
    get_payout_provider,
)


def test_fake_payout_provider_shape():
    result = FakePayoutProvider().deliver("0xabc123", 12.34, "USDC")
    assert set(result.keys()) == {
        "providerRef", "chain", "asset", "amount", "marketRateUsd", "settlementTxHash",
    }


def test_fake_payout_provider_amount_and_defaults():
    result = FakePayoutProvider().deliver("0xabc123", 5.0)
    assert result["amount"] == 5.0
    assert result["asset"] == "USDC"
    assert result["chain"] == "base"
    assert result["marketRateUsd"] == 1.0


def test_fake_payout_provider_settlement_tx_hash_present():
    result = FakePayoutProvider().deliver("0xabc123", 1.0)
    assert result["settlementTxHash"].startswith("0xFAKE")
    assert result["providerRef"].startswith("fake_")


def test_fake_payout_provider_no_network_and_deterministic_shape_repeats():
    # Calling twice should not error (no network) and should preserve shape,
    # even though providerRef/settlementTxHash differ per-call.
    r1 = FakePayoutProvider().deliver("0xabc", 1.0)
    r2 = FakePayoutProvider().deliver("0xabc", 1.0)
    assert r1["providerRef"] != r2["providerRef"]
    assert r1.keys() == r2.keys()


def test_get_payout_provider_defaults_to_fake(monkeypatch):
    monkeypatch.delenv("PAYOUT_PROVIDER", raising=False)
    assert isinstance(get_payout_provider(), FakePayoutProvider)


def test_get_payout_provider_unknown_raises(monkeypatch):
    monkeypatch.setenv("PAYOUT_PROVIDER", "some_other_provider")
    with pytest.raises(NotImplementedError):
        get_payout_provider()


def test_payout_provider_abstract_deliver_raises():
    with pytest.raises(NotImplementedError):
        PayoutProvider().deliver("0xabc", 1.0)


class _FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def _bridge_env(monkeypatch):
    monkeypatch.setenv("BRIDGE_API_KEY", "sk-test-abc123")
    monkeypatch.setenv("BRIDGE_BASE_URL", "https://api.sandbox.bridge.xyz")
    monkeypatch.setenv("BRIDGE_CHAIN", "base")
    monkeypatch.setenv("BRIDGE_SOURCE_WALLET_ID", "wallet_test_1")


def test_bridge_payout_provider_deliver_success(monkeypatch):
    _bridge_env(monkeypatch)

    captured = {}

    def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200, {"id": "transfer_test", "state": "pending"})

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = BridgePayoutProvider()
    result = provider.deliver("0xabc123", 12.34, "USDC", on_behalf_of="customer_123")

    # No real network call happened — `captured` was only populated by our
    # fake_post stand-in.
    assert captured["url"] == "https://api.sandbox.bridge.xyz/v0/transfers"

    headers = captured["headers"]
    assert headers["Api-Key"] == "sk-test-abc123"
    assert "Idempotency-Key" in headers and headers["Idempotency-Key"]
    assert headers["Content-Type"] == "application/json"

    body = captured["json"]
    assert body["amount"] == "12.34"
    assert body["on_behalf_of"] == "customer_123"
    assert body["source"] == {
        "payment_rail": "bridge_wallet",
        "currency": "usdc",
        "bridge_wallet_id": "wallet_test_1",
    }
    assert body["destination"] == {
        "payment_rail": "base",
        "currency": "usdc",
        "to_address": "0xabc123",
    }

    assert result["providerRef"] == "transfer_test"
    assert result["chain"] == "base"
    assert result["asset"] == "USDC"
    assert result["amount"] == 12.34
    assert result["marketRateUsd"] == 1.0
    assert result["settlementTxHash"] is None
    assert result["status"] == "pending"


def test_bridge_payout_provider_two_calls_use_distinct_idempotency_keys(monkeypatch):
    _bridge_env(monkeypatch)
    seen_keys = []

    def fake_post(url, json=None, headers=None):
        seen_keys.append(headers["Idempotency-Key"])
        return _FakeResponse(200, {"id": "transfer_test", "state": "pending"})

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = BridgePayoutProvider()
    provider.deliver("0xabc", 1.0)
    provider.deliver("0xabc", 1.0)

    assert len(seen_keys) == 2
    assert seen_keys[0] != seen_keys[1]


def test_bridge_payout_provider_raises_on_error_response(monkeypatch):
    _bridge_env(monkeypatch)

    def fake_post(url, json=None, headers=None):
        return _FakeResponse(422, {"error": "invalid_amount"}, text='{"error": "invalid_amount"}')

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = BridgePayoutProvider()
    with pytest.raises(RuntimeError) as exc_info:
        provider.deliver("0xabc", 1.0)
    assert "invalid_amount" in str(exc_info.value)


def test_get_payout_provider_bridge_selects_bridge_provider(monkeypatch):
    _bridge_env(monkeypatch)
    monkeypatch.setenv("PAYOUT_PROVIDER", "bridge")
    provider = get_payout_provider()
    assert isinstance(provider, BridgePayoutProvider)
