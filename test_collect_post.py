"""Route-level tests for POST /collect-post (server.collect_post)."""
from types import SimpleNamespace

import server

from fakes_common import (
    FakeFirestoreDb,
    auth_headers,
    fake_transactional,
    install_auth,
)

COLLECTOR_ID = "collector-1"
CREATOR_ID = "creator-1"
TOKEN_ID = "55"


def _post(client, body, headers=None):
    return client.post("/collect-post", json=body, headers=headers)


def _install_firestore(
    monkeypatch, *, collector_purchased=10, collector_earned=0, post_data=None
):
    db = FakeFirestoreDb()
    db.collection("tokenBalances").seed(
        COLLECTOR_ID,
        {"purchasedBalance": collector_purchased, "earnedBalance": collector_earned},
    )
    if post_data is not None:
        db.collection("posts").seed(TOKEN_ID, post_data)
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(
        server, "fb_firestore", SimpleNamespace(transactional=fake_transactional)
    )
    monkeypatch.setattr(server, "_sync_creator_balance_async", lambda *a, **k: None)
    return db


def test_no_token_is_401():
    client = server.app.test_client()
    resp = _post(
        client,
        {"tokenId": TOKEN_ID, "collectorId": COLLECTOR_ID, "creatorId": CREATOR_ID},
    )
    assert resp.status_code == 401


def test_firebase_not_configured_returns_500(monkeypatch):
    install_auth(monkeypatch, server, COLLECTOR_ID)
    monkeypatch.setattr(server, "firestore_db", None)
    client = server.app.test_client()
    resp = _post(
        client,
        {"tokenId": TOKEN_ID, "collectorId": COLLECTOR_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Firebase not configured on server"}


def test_missing_fields_returns_400(monkeypatch):
    install_auth(monkeypatch, server, COLLECTOR_ID)
    _install_firestore(monkeypatch)
    client = server.app.test_client()
    resp = _post(client, {"collectorId": COLLECTOR_ID}, headers=auth_headers())
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "tokenId, collectorId, creatorId are required"}


def test_mismatched_uid_is_403(monkeypatch):
    install_auth(monkeypatch, server, "someone-else")
    _install_firestore(monkeypatch)
    client = server.app.test_client()
    resp = _post(
        client,
        {"tokenId": TOKEN_ID, "collectorId": COLLECTOR_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "Forbidden: caller must be the collector"}


def test_duplicate_collect_returns_collected_false(monkeypatch):
    install_auth(monkeypatch, server, COLLECTOR_ID)
    db = _install_firestore(
        monkeypatch, post_data={"collectPrice": 5, "collectEnabled": True}
    )
    db.collection("collections").seed(
        f"{TOKEN_ID}_{COLLECTOR_ID}", {"tokenId": TOKEN_ID}
    )
    client = server.app.test_client()

    resp = _post(
        client,
        {"tokenId": TOKEN_ID, "collectorId": COLLECTOR_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"collected": False}


def test_collect_disabled_returns_400(monkeypatch):
    install_auth(monkeypatch, server, COLLECTOR_ID)
    _install_firestore(monkeypatch, post_data={"collectEnabled": False})
    client = server.app.test_client()

    resp = _post(
        client,
        {"tokenId": TOKEN_ID, "collectorId": COLLECTOR_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Collecting is turned off for this post"}


def test_insufficient_balance_returns_400(monkeypatch):
    install_auth(monkeypatch, server, COLLECTOR_ID)
    _install_firestore(
        monkeypatch,
        collector_purchased=1,
        post_data={"collectPrice": 5, "collectEnabled": True},
    )
    client = server.app.test_client()

    resp = _post(
        client,
        {"tokenId": TOKEN_ID, "collectorId": COLLECTOR_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Insufficient token balance"}


def test_happy_path_uses_default_price_when_post_missing(monkeypatch):
    """Post doc doesn't exist (post_data=None default) -> falls back to
    DEFAULT_COLLECT_PRICE (5) rather than erroring."""
    install_auth(monkeypatch, server, COLLECTOR_ID)
    db = _install_firestore(monkeypatch, collector_purchased=10)
    client = server.app.test_client()

    resp = _post(
        client,
        {"tokenId": TOKEN_ID, "collectorId": COLLECTOR_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"collected": True}

    # collect_split(5) -> platform_cut=min(0.5,5)=0.5, creator_earns=4.5
    collector_doc = db.collection("tokenBalances").document(COLLECTOR_ID).get().to_dict()
    assert collector_doc["purchasedBalance"] == 5  # 10 - 5

    creator_doc = db.collection("tokenBalances").document(CREATOR_ID).get().to_dict()
    assert creator_doc["earnedBalance"] == 4.5

    platform_doc = db.collection("tokenBalances").document("PLATFORM").get().to_dict()
    assert platform_doc["balance"] == 0.5

    collections = db.collection("collections").stream()
    assert len(collections) == 1
    assert collections[0].to_dict() == {
        "tokenId": TOKEN_ID,
        "collectorId": COLLECTOR_ID,
        "creatorId": CREATOR_ID,
        "createdAt": collections[0].to_dict()["createdAt"],
    }

    post_doc = db.collection("posts").document(TOKEN_ID).get().to_dict()
    assert post_doc["collectsCount"] == 1

    ledger_rows = db.collection("tokenTransactions").stream()
    reasons = sorted(row.to_dict()["reason"] for row in ledger_rows)
    assert reasons == ["collect_earning", "collect_spend"]


def test_custom_collect_price_is_honored(monkeypatch):
    install_auth(monkeypatch, server, COLLECTOR_ID)
    db = _install_firestore(
        monkeypatch,
        collector_purchased=20,
        post_data={"collectPrice": 10, "collectEnabled": True},
    )
    client = server.app.test_client()

    resp = _post(
        client,
        {"tokenId": TOKEN_ID, "collectorId": COLLECTOR_ID, "creatorId": CREATOR_ID},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"collected": True}
    # collect_split(10) -> platform_cut=min(0.5,10)=0.5, creator_earns=9.5
    collector_doc = db.collection("tokenBalances").document(COLLECTOR_ID).get().to_dict()
    assert collector_doc["purchasedBalance"] == 10  # 20 - 10
    creator_doc = db.collection("tokenBalances").document(CREATOR_ID).get().to_dict()
    assert creator_doc["earnedBalance"] == 9.5
