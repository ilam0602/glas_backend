"""Route-level tests for POST /charge-view (server.charge_view)."""
import server

from fakes_common import (
    FakeFirestoreDb,
    auth_headers,
    fake_transactional,
    install_auth,
)
from types import SimpleNamespace

VIEWER_ID = "viewer-1"
CREATOR_ID = "creator-1"


def _post(client, body, headers=None):
    return client.post("/charge-view", json=body, headers=headers)


def _install_firestore(monkeypatch, *, viewer_purchased=10, viewer_earned=0):
    db = FakeFirestoreDb()
    db.collection("tokenBalances").seed(
        VIEWER_ID, {"purchasedBalance": viewer_purchased, "earnedBalance": viewer_earned}
    )
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(
        server, "fb_firestore", SimpleNamespace(transactional=fake_transactional)
    )
    monkeypatch.setattr(server, "_sync_creator_balance_async", lambda *a, **k: None)
    return db


def test_no_token_is_401():
    client = server.app.test_client()
    resp = _post(client, {"viewerId": VIEWER_ID, "creatorId": CREATOR_ID})
    assert resp.status_code == 401


def test_firebase_not_configured_returns_500(monkeypatch):
    install_auth(monkeypatch, server, VIEWER_ID)
    monkeypatch.setattr(server, "firestore_db", None)
    client = server.app.test_client()
    resp = _post(
        client,
        {"viewerId": VIEWER_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Firebase not configured on server"}


def test_missing_fields_returns_400(monkeypatch):
    install_auth(monkeypatch, server, VIEWER_ID)
    _install_firestore(monkeypatch)
    client = server.app.test_client()
    resp = _post(client, {"viewerId": VIEWER_ID}, headers=auth_headers())
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "viewerId and creatorId are required"}


def test_mismatched_uid_is_403(monkeypatch):
    install_auth(monkeypatch, server, "someone-else")
    _install_firestore(monkeypatch)
    client = server.app.test_client()
    resp = _post(
        client,
        {"viewerId": VIEWER_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "Forbidden: caller must be the viewer"}


def test_insufficient_balance_returns_400(monkeypatch):
    install_auth(monkeypatch, server, VIEWER_ID)
    _install_firestore(monkeypatch, viewer_purchased=0)
    client = server.app.test_client()
    resp = _post(
        client,
        {"viewerId": VIEWER_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Insufficient token balance"}


def test_happy_path_response_shape(monkeypatch):
    install_auth(monkeypatch, server, VIEWER_ID)
    db = _install_firestore(monkeypatch, viewer_purchased=10)
    client = server.app.test_client()

    resp = _post(
        client,
        {"viewerId": VIEWER_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    # VIEW_PRICE=1, VIEW_EARNING=0.5
    assert resp.get_json() == {"viewerBalance": 9, "creatorBalance": 0.5}

    viewer_doc = db.collection("tokenBalances").document(VIEWER_ID).get().to_dict()
    assert viewer_doc["purchasedBalance"] == 9

    creator_doc = db.collection("tokenBalances").document(CREATOR_ID).get().to_dict()
    assert creator_doc["earnedBalance"] == 0.5
    assert creator_doc["purchasedBalance"] == 0

    platform_doc = db.collection("tokenBalances").document("PLATFORM").get().to_dict()
    assert platform_doc["balance"] == 0.5  # VIEW_PRICE - VIEW_EARNING

    ledger_rows = db.collection("tokenTransactions").stream()
    reasons = sorted(row.to_dict()["reason"] for row in ledger_rows)
    assert reasons == ["view_earning", "view_spend"]


def test_creator_with_existing_balance_accumulates(monkeypatch):
    install_auth(monkeypatch, server, VIEWER_ID)
    db = _install_firestore(monkeypatch, viewer_purchased=10)
    db.collection("tokenBalances").seed(
        CREATOR_ID, {"purchasedBalance": 3, "earnedBalance": 7, "lastOnChainBalance": 5}
    )
    client = server.app.test_client()

    resp = _post(
        client,
        {"viewerId": VIEWER_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"viewerBalance": 9, "creatorBalance": 7.5}
    creator_doc = db.collection("tokenBalances").document(CREATOR_ID).get().to_dict()
    assert creator_doc["purchasedBalance"] == 3  # untouched (keystone invariant)
    assert creator_doc["earnedBalance"] == 7.5
    assert creator_doc["lastOnChainBalance"] == 5
