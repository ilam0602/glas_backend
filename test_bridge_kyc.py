import pytest

import payout_provider
from payout_provider import create_bridge_kyc_link


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


def test_create_bridge_kyc_link_posts_correct_request_and_maps_response(monkeypatch):
    _bridge_env(monkeypatch)

    captured = {}

    def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(
            200,
            {
                "id": "kyc_link_test",
                "kyc_link": "https://kyc.bridge.xyz/abc",
                "tos_link": "https://tos.bridge.xyz/abc",
                "kyc_status": "not_started",
                "tos_status": "pending",
                "customer_id": "customer_test_1",
            },
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    result = create_bridge_kyc_link("Jane Creator", "jane@example.com")

    assert captured["url"] == "https://api.sandbox.bridge.xyz/v0/kyc_links"

    headers = captured["headers"]
    assert headers["Api-Key"] == "sk-test-abc123"
    assert "Idempotency-Key" in headers and headers["Idempotency-Key"]
    assert headers["Content-Type"] == "application/json"

    body = captured["json"]
    assert body == {
        "full_name": "Jane Creator",
        "email": "jane@example.com",
        "type": "individual",
    }

    assert result == {
        "customerId": "customer_test_1",
        "kycLink": "https://kyc.bridge.xyz/abc",
        "kycStatus": "not_started",
    }


def test_create_bridge_kyc_link_two_calls_use_distinct_idempotency_keys(monkeypatch):
    _bridge_env(monkeypatch)
    seen_keys = []

    def fake_post(url, json=None, headers=None):
        seen_keys.append(headers["Idempotency-Key"])
        return _FakeResponse(
            200,
            {
                "customer_id": "customer_test_1",
                "kyc_link": "https://kyc.bridge.xyz/abc",
                "kyc_status": "not_started",
            },
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    create_bridge_kyc_link("Jane Creator", "jane@example.com")
    create_bridge_kyc_link("Jane Creator", "jane@example.com")

    assert len(seen_keys) == 2
    assert seen_keys[0] != seen_keys[1]


def test_create_bridge_kyc_link_raises_on_error_response(monkeypatch):
    _bridge_env(monkeypatch)

    def fake_post(url, json=None, headers=None):
        return _FakeResponse(
            422, {"error": "invalid_email"}, text='{"error": "invalid_email"}'
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    with pytest.raises(RuntimeError) as exc_info:
        create_bridge_kyc_link("Jane Creator", "bad-email")
    assert "invalid_email" in str(exc_info.value)
