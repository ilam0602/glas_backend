"""Route-level tests for POST /withdraw-tokens (server.withdraw_tokens).

Covers auth (401/403), validation (400s), the two-bucket earned-balance
guardrail, the daily USD cap, the pool-drain cap, and the exact success
response shape - pinning today's behavior, not a rewrite.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import server

from fakes_common import (
    FakeContract,
    FakeFirestoreDb,
    auth_headers,
    fake_send_contract_transaction,
    fake_transactional,
    install_auth,
)

USER_ID = "user-1"


def _install_contracts(monkeypatch, *, glas_price_usd=0.01, pool_balance=100000.0):
    """Wire up token/glas contracts with values that make the arithmetic in
    withdraw_tokens land on round numbers:
      - glasPriceUsd (wei) -> glas_price_usd
      - getPoolBalance (wei) -> pool_balance
      - getVirtualBalance (wei) -> huge, so the on-chain sync step never fires
    """
    glas_price_wei = server.w3.to_wei(glas_price_usd, "ether")
    pool_balance_wei = server.w3.to_wei(pool_balance, "ether")
    monkeypatch.setattr(
        server,
        "glas_contract",
        FakeContract(glasPriceUsd=glas_price_wei, getPoolBalance=pool_balance_wei),
    )
    monkeypatch.setattr(
        server,
        "token_contract",
        FakeContract(getVirtualBalance=server.w3.to_wei(10_000_000, "ether")),
    )
    send_fn = fake_send_contract_transaction()
    monkeypatch.setattr(server, "send_contract_transaction", send_fn)
    return send_fn


def _install_firestore(monkeypatch, *, purchased=0, earned=1000):
    db = FakeFirestoreDb()
    db.collection("tokenBalances").seed(
        USER_ID, {"purchasedBalance": purchased, "earnedBalance": earned}
    )
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(
        server, "fb_firestore", SimpleNamespace(transactional=fake_transactional)
    )
    return db


def _post(client, body, headers=None):
    return client.post("/withdraw-tokens", json=body, headers=headers)


def test_rejects_when_token_contract_not_configured(monkeypatch):
    monkeypatch.setattr(server, "token_contract", None)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "amount": 100})
    assert resp.status_code == 500
    assert "Token contract" in resp.get_json()["error"]


def test_rejects_when_glas_contract_not_configured(monkeypatch):
    monkeypatch.setattr(server, "token_contract", FakeContract())
    monkeypatch.setattr(server, "glas_contract", None)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "amount": 100})
    assert resp.status_code == 500
    assert "Glas contract" in resp.get_json()["error"]


def test_missing_fields_returns_400(monkeypatch):
    _install_contracts(monkeypatch)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Missing userId or amount"}


def test_no_token_is_401(monkeypatch):
    _install_contracts(monkeypatch)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "amount": 100})
    assert resp.status_code == 401


def test_mismatched_uid_is_403(monkeypatch):
    _install_contracts(monkeypatch)
    install_auth(monkeypatch, server, "someone-else")
    client = server.app.test_client()
    resp = _post(
        client, {"userId": USER_ID, "amount": 100}, headers=auth_headers()
    )
    assert resp.status_code == 403


def test_non_positive_amount_is_400(monkeypatch):
    _install_contracts(monkeypatch)
    install_auth(monkeypatch, server, USER_ID)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "amount": 0}, headers=auth_headers())
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Amount must be positive"}


def test_invalid_to_address_is_400(monkeypatch):
    _install_contracts(monkeypatch)
    install_auth(monkeypatch, server, USER_ID)
    client = server.app.test_client()
    resp = _post(
        client,
        {"userId": USER_ID, "amount": 100, "toAddress": "not-an-address"},
        headers=auth_headers(),
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Invalid toAddress format"}


def test_insufficient_earned_balance_is_400(monkeypatch):
    _install_contracts(monkeypatch)
    install_auth(monkeypatch, server, USER_ID)
    _install_firestore(monkeypatch, purchased=5000, earned=50)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "amount": 100}, headers=auth_headers())
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Insufficient earned balance"}


def test_daily_cap_exceeded_is_400(monkeypatch):
    # glas price doesn't matter for this guardrail; use defaults.
    _install_contracts(monkeypatch)
    install_auth(monkeypatch, server, USER_ID)
    db = _install_firestore(monkeypatch, earned=1_000_000)
    # stable_amount=1000 -> usd_value=1.0 -> net_usd after 5% fee = 0.95.
    # Seed $49.10 already withdrawn today so 49.10 + 0.95 > $50 cap.
    db.collection("withdrawalHistory").document(USER_ID).collection(
        "withdrawals"
    ).seed("w1", {"usdValue": 49.10, "timestamp": datetime.utcnow()})
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "amount": 1000}, headers=auth_headers())
    assert resp.status_code == 400
    body = resp.get_json()
    assert "Daily withdrawal cap exceeded" in body["error"]
    assert body["remainingUsd"] == 50.0 - 49.10


def test_pool_drain_cap_exceeded_is_400(monkeypatch):
    # Tiny pool (1 GLAS) with a cheap GLAS price so even a small withdrawal's
    # glas_amount blows past the 10%-of-pool cap.
    _install_contracts(monkeypatch, glas_price_usd=0.0001, pool_balance=1.0)
    install_auth(monkeypatch, server, USER_ID)
    _install_firestore(monkeypatch, earned=1_000_000)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "amount": 1000}, headers=auth_headers())
    assert resp.status_code == 400
    body = resp.get_json()
    assert "pool drain cap" in body["error"]
    assert body["maxGlas"] == pytest.approx(1.0 * 0.10)


def test_glas_price_not_set_is_500(monkeypatch):
    _install_contracts(monkeypatch, glas_price_usd=0)
    install_auth(monkeypatch, server, USER_ID)
    _install_firestore(monkeypatch, earned=1_000_000)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "amount": 100}, headers=auth_headers())
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "GLAS price not set"}


def test_happy_path_response_shape(monkeypatch):
    _install_contracts(monkeypatch, glas_price_usd=0.01, pool_balance=100000.0)
    install_auth(monkeypatch, server, USER_ID)
    db = _install_firestore(monkeypatch, purchased=200, earned=1000)
    client = server.app.test_client()

    resp = _post(client, {"userId": USER_ID, "amount": 100}, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    # usd_value = 100 / 1000 = 0.1; fee = 0.1 * 0.05 = 0.005; net = 0.095
    # glas_amount = 0.095 / 0.01 = 9.5
    assert body.pop("success") is True
    assert body.pop("stableBurned") == pytest.approx(100.0)
    assert body.pop("feeDeducted") == pytest.approx(0.005)
    assert body.pop("usdValue") == pytest.approx(0.095)
    assert body.pop("glasReceived") == pytest.approx(9.5)
    assert body.pop("glasPriceUsd") == pytest.approx(0.01)
    assert body == {}  # no other keys (e.g. no exportTransactionHash)

    # Earned bucket debited, purchased untouched (keystone invariant).
    balance_doc = db.collection("tokenBalances").document(USER_ID).get().to_dict()
    assert balance_doc["purchasedBalance"] == 200
    assert balance_doc["earnedBalance"] == 1000 - 100.0

    # A withdrawal ledger entry was recorded for the daily-cap query to see.
    withdrawals = (
        db.collection("withdrawalHistory")
        .document(USER_ID)
        .collection("withdrawals")
        .stream()
    )
    assert len(withdrawals) == 1
    assert withdrawals[0].to_dict()["usdValue"] == pytest.approx(0.095)


def test_happy_path_with_export_includes_transaction_hash(monkeypatch):
    _install_contracts(monkeypatch, glas_price_usd=0.01, pool_balance=100000.0)
    install_auth(monkeypatch, server, USER_ID)
    _install_firestore(monkeypatch, earned=1000)
    client = server.app.test_client()

    recipient = "0x000000000000000000000000000000000000dEaD"
    resp = _post(
        client,
        {"userId": USER_ID, "amount": 100, "toAddress": recipient},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["exportTransactionHash"] == "0xfake"


def test_on_chain_sync_runs_when_balance_deficit(monkeypatch):
    """When the on-chain virtual balance is below the withdrawal amount, the
    route should first mint the deficit on-chain (an extra
    send_contract_transaction call) before burning/crediting."""
    glas_price_wei = server.w3.to_wei(0.01, "ether")
    pool_balance_wei = server.w3.to_wei(100000.0, "ether")
    monkeypatch.setattr(
        server,
        "glas_contract",
        FakeContract(glasPriceUsd=glas_price_wei, getPoolBalance=pool_balance_wei),
    )
    monkeypatch.setattr(
        server, "token_contract", FakeContract(getVirtualBalance=0)
    )
    send_fn = fake_send_contract_transaction()
    monkeypatch.setattr(server, "send_contract_transaction", send_fn)
    install_auth(monkeypatch, server, USER_ID)
    _install_firestore(monkeypatch, earned=1000)
    client = server.app.test_client()

    resp = _post(client, {"userId": USER_ID, "amount": 100}, headers=auth_headers())

    assert resp.status_code == 200
    # sync mint + burn + credit = 3 on-chain calls (no export requested).
    assert len(send_fn.calls) == 3
