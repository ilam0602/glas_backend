"""Tests for Crossmint recipient onboarding: registering a creator as a
Crossmint USER and creating a Crossmint-managed wallet OWNED by them (the
supported model for `regulated-transfer` payouts — see payout_provider.py's
module/class docstrings for the full rationale). No real network calls —
`payout_provider.requests.put`/`.post` are monkeypatched.
"""

import time

import pytest

import payout_provider
from payout_provider import (
    CrossmintPayoutProvider,
    crossmint_create_recipient_wallet,
    crossmint_register_user,
)


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def _crossmint_env(monkeypatch):
    monkeypatch.setenv("CROSSMINT_API_KEY", "sk_staging_test123")
    monkeypatch.setenv(
        "CROSSMINT_BASE_URL", "https://staging.crossmint.com/api/2025-06-09"
    )
    monkeypatch.setenv("CROSSMINT_CHAIN", "base")
    monkeypatch.setenv("CROSSMINT_TREASURY_WALLET", "treasury_wallet_test")


def test_crossmint_register_user_puts_correct_request(monkeypatch):
    _crossmint_env(monkeypatch)

    captured = {}

    def fake_put(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200, {"email": "jane@example.com"})

    monkeypatch.setattr(payout_provider.requests, "put", fake_put)

    result = crossmint_register_user(
        "jane@example.com", "Jane", "Creator", "1990-01-15", "US"
    )

    assert (
        captured["url"]
        == "https://staging.crossmint.com/api/2025-06-09/users/email%3Ajane%40example.com"
    )

    headers = captured["headers"]
    assert headers["X-API-KEY"] == "sk_staging_test123"
    assert headers["Content-Type"] == "application/json"

    body = captured["json"]
    assert body == {
        "userDetails": {
            "firstName": "Jane",
            "lastName": "Creator",
            "dateOfBirth": "1990-01-15",
            "countryOfResidence": "US",
        }
    }

    assert result == {"email": "jane@example.com"}


def test_crossmint_register_user_defaults_country_to_us_when_caller_omits(monkeypatch):
    _crossmint_env(monkeypatch)

    captured = {}

    def fake_put(url, json=None, headers=None):
        captured["json"] = json
        return _FakeResponse(200, {})

    monkeypatch.setattr(payout_provider.requests, "put", fake_put)

    # crossmint_register_user itself requires an explicit country param (the
    # "US" default lives in server.py's /payout/onboard route), so exercise
    # that explicitly here.
    crossmint_register_user("jane@example.com", "Jane", "Creator", "1990-01-15")

    assert captured["json"]["userDetails"]["countryOfResidence"] == "US"


def test_crossmint_register_user_raises_on_error_response(monkeypatch):
    _crossmint_env(monkeypatch)

    def fake_put(url, json=None, headers=None):
        return _FakeResponse(
            400, {"message": "unsupported country"}, text='{"message": "unsupported country"}'
        )

    monkeypatch.setattr(payout_provider.requests, "put", fake_put)

    with pytest.raises(RuntimeError) as exc_info:
        crossmint_register_user("jane@example.com", "Jane", "Creator", "1990-01-15", "ZZ")
    assert "unsupported country" in str(exc_info.value)


def test_crossmint_create_recipient_wallet_posts_correct_request_and_returns_address(
    monkeypatch,
):
    _crossmint_env(monkeypatch)

    captured = {}

    def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(
            201,
            {
                "address": "0xRECIPIENT123",
                "owner": "email:jane@example.com",
                "chainType": "evm",
                "type": "smart",
            },
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    address = crossmint_create_recipient_wallet("jane@example.com")

    assert captured["url"] == "https://staging.crossmint.com/api/2025-06-09/wallets"

    headers = captured["headers"]
    assert headers["X-API-KEY"] == "sk_staging_test123"
    assert headers["Content-Type"] == "application/json"

    assert captured["json"] == {
        "chainType": "evm",
        "type": "smart",
        "owner": "email:jane@example.com",
    }

    assert address == "0xRECIPIENT123"


def test_crossmint_create_recipient_wallet_raises_on_error_response(monkeypatch):
    _crossmint_env(monkeypatch)

    def fake_post(url, json=None, headers=None):
        return _FakeResponse(
            400, {"message": "adminSigner required"}, text='{"message": "adminSigner required"}'
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    with pytest.raises(RuntimeError) as exc_info:
        crossmint_create_recipient_wallet("jane@example.com")
    assert "adminSigner required" in str(exc_info.value)


def test_crossmint_deliver_retries_on_kyc_pending_then_succeeds(monkeypatch):
    _crossmint_env(monkeypatch)

    sleep_calls = []
    monkeypatch.setattr(payout_provider.time, "sleep", lambda s: sleep_calls.append(s))

    responses = [
        _FakeResponse(400, {"message": "Recipient KYC is processing"}, text="Recipient KYC is processing"),
        _FakeResponse(400, {"message": "KYC check in progress, please retry"}, text="KYC check in progress, please retry"),
        _FakeResponse(200, {"id": "transfer_ok", "status": "success", "onChain": {"txId": "0xTX"}}),
    ]
    call_count = {"n": 0}

    def fake_post(url, json=None, headers=None):
        resp = responses[call_count["n"]]
        call_count["n"] += 1
        return resp

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = CrossmintPayoutProvider()
    result = provider.deliver("0xRECIPIENT123", 12.34)

    assert call_count["n"] == 3
    assert sleep_calls == [3, 3]
    assert result["providerRef"] == "transfer_ok"
    assert result["settlementTxHash"] == "0xTX"
    assert result["status"] == "success"


def test_crossmint_deliver_raises_after_five_kyc_pending_attempts(monkeypatch):
    _crossmint_env(monkeypatch)

    sleep_calls = []
    monkeypatch.setattr(payout_provider.time, "sleep", lambda s: sleep_calls.append(s))

    def fake_post(url, json=None, headers=None):
        return _FakeResponse(
            400, {"message": "KYC processing"}, text="KYC processing"
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = CrossmintPayoutProvider()
    with pytest.raises(RuntimeError) as exc_info:
        provider.deliver("0xRECIPIENT123", 1.0)

    assert "KYC processing" in str(exc_info.value)
    # 5 attempts total -> 4 sleeps between them.
    assert len(sleep_calls) == 4


def test_crossmint_deliver_does_not_retry_non_kyc_error(monkeypatch):
    _crossmint_env(monkeypatch)

    sleep_calls = []
    monkeypatch.setattr(payout_provider.time, "sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}

    def fake_post(url, json=None, headers=None):
        call_count["n"] += 1
        return _FakeResponse(
            422, {"message": "insufficient treasury balance"}, text="insufficient treasury balance"
        )

    monkeypatch.setattr(payout_provider.requests, "post", fake_post)

    provider = CrossmintPayoutProvider()
    with pytest.raises(RuntimeError) as exc_info:
        provider.deliver("0xRECIPIENT123", 1.0)

    assert "insufficient treasury balance" in str(exc_info.value)
    assert call_count["n"] == 1
    assert sleep_calls == []
