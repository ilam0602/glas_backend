"""Route-level tests for POST /toggle-privacy (server.toggle_privacy)."""
import server

from fakes_common import (
    FakeContract,
    FakeFirestoreDb,
    auth_headers,
    fake_send_contract_transaction,
    install_auth,
)

USER_ID = "user-1"


def _post(client, body, headers=None):
    return client.post("/toggle-privacy", json=body, headers=headers)


def _install_chain(monkeypatch, *, new_token_id=99):
    monkeypatch.setattr(server, "contract", FakeContract())
    send_fn = fake_send_contract_transaction()
    monkeypatch.setattr(server, "send_contract_transaction", send_fn)
    monkeypatch.setattr(
        server, "extract_virtual_mint_token_id", lambda receipt, contract: new_token_id
    )
    monkeypatch.setattr(
        server, "pin_metadata_to_ipfs", lambda *args, **kwargs: "ipfs://new-metadata"
    )
    return send_fn


def test_firebase_not_configured_returns_500(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", None)
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "isPrivate": True})
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Firebase not configured on server"}


def test_missing_fields_returns_400(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Missing userId or isPrivate"}


def test_no_token_is_401(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "isPrivate": True})
    assert resp.status_code == 401


def test_mismatched_uid_is_403(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    install_auth(monkeypatch, server, "someone-else")
    client = server.app.test_client()
    resp = _post(
        client, {"userId": USER_ID, "isPrivate": True}, headers=auth_headers()
    )
    assert resp.status_code == 403


def test_no_posts_returns_empty_results(monkeypatch):
    install_auth(monkeypatch, server, USER_ID)
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    _install_chain(monkeypatch)
    client = server.app.test_client()

    resp = _post(
        client, {"userId": USER_ID, "isPrivate": True}, headers=auth_headers()
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "isPrivate": True, "results": []}


def test_going_private_burns_remints_and_encrypts_urls(monkeypatch):
    install_auth(monkeypatch, server, USER_ID)
    db = FakeFirestoreDb()
    db.collection("posts").seed(
        "100",
        {
            "userId": USER_ID,
            "tokenId": 100,
            "isPrivate": False,
            "ipfsUrl": "ipfs://plain-photo",
            "hqMediaUrl": "ipfs://plain-hq",
            "mediaType": "photo",
        },
    )
    monkeypatch.setattr(server, "firestore_db", db)
    send_fn = _install_chain(monkeypatch, new_token_id=200)
    client = server.app.test_client()

    resp = _post(
        client, {"userId": USER_ID, "isPrivate": True}, headers=auth_headers()
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["isPrivate"] is True
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["oldTokenId"] == 100
    assert result["newTokenId"] == 200
    assert result["mediaType"] == "photo"
    # URLs are encrypted (no longer the plaintext ipfs:// originals) now that
    # the post is private.
    assert result["ipfsUri"] != "ipfs://plain-photo"
    assert server.decrypt_url(result["ipfsUri"]) == "ipfs://plain-photo"
    assert server.decrypt_url(result["hqMediaUrl"]) == "ipfs://plain-hq"

    # Old post doc replaced by the new tokenId; old doc gone.
    assert db.collection("posts").document("100").get().exists is False
    new_doc = db.collection("posts").document("200").get().to_dict()
    assert new_doc["isPrivate"] is True
    assert new_doc["tokenId"] == 200

    # Burn old token + mint new token = 2 on-chain calls, in that order.
    assert len(send_fn.calls) == 2
    names = [name for name, _args, _kwargs in server.contract.functions.calls]
    assert names == ["burnVirtual", "mintToVirtual"]


def test_going_public_decrypts_urls(monkeypatch):
    install_auth(monkeypatch, server, USER_ID)
    db = FakeFirestoreDb()
    encrypted_ipfs = server.encrypt_url("ipfs://secret-photo")
    db.collection("posts").seed(
        "100",
        {
            "userId": USER_ID,
            "tokenId": 100,
            "isPrivate": True,
            "ipfsUrl": encrypted_ipfs,
            "hqMediaUrl": "",
            "mediaType": "photo",
        },
    )
    monkeypatch.setattr(server, "firestore_db", db)
    _install_chain(monkeypatch, new_token_id=300)
    client = server.app.test_client()

    resp = _post(
        client, {"userId": USER_ID, "isPrivate": False}, headers=auth_headers()
    )

    assert resp.status_code == 200
    result = resp.get_json()["results"][0]
    assert result["ipfsUri"] == "ipfs://secret-photo"


def test_skips_posts_with_zero_token_id(monkeypatch):
    install_auth(monkeypatch, server, USER_ID)
    db = FakeFirestoreDb()
    db.collection("posts").seed(
        "legacy",
        {"userId": USER_ID, "tokenId": 0, "isPrivate": False, "ipfsUrl": "ipfs://x"},
    )
    monkeypatch.setattr(server, "firestore_db", db)
    _install_chain(monkeypatch)
    client = server.app.test_client()

    resp = _post(
        client, {"userId": USER_ID, "isPrivate": True}, headers=auth_headers()
    )

    assert resp.status_code == 200
    assert resp.get_json()["results"] == []
