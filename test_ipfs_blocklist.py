"""Route-level tests for the IPFS blocklist feature:

- GET /ipfs/<cid> (and /ipfs/<cid>/<subpath>) -- the gateway-redirect route
  that enforces the blockedCids blocklist.
- POST /admin/block-post-media -- admin takedown (unpin + blocklist + flag).
- POST /admin/unblock-cid -- reverses a block.

`import server` connects to REAL Firebase + blockchain at import time, so
every test here monkeypatches server.firestore_db / server.firebase_auth /
server.contract with fakes -- never touch production services. Follows the
Fake*/install_auth conventions in fakes_common.py and test_report_post.py.
"""
import pytest

import server
from fakes_common import FakeContract, FakeFirestoreDb, auth_headers, install_auth

ADMIN_EMAIL = "admin@example.com"
NON_ADMIN_EMAIL = "someone-else@example.com"


@pytest.fixture(autouse=True)
def _reset_blocklist_cache():
    """The blockedCids cache is a module-level global with a 60s TTL, so it
    would otherwise leak state between tests. Force a re-query on the first
    call of every test, and again afterwards so a later test file doesn't
    inherit whatever this test left cached."""
    server._invalidate_blocked_cids_cache()
    yield
    server._invalidate_blocked_cids_cache()


# ---------------------------------------------------------------------------
# GET /ipfs/<cid>
# ---------------------------------------------------------------------------


def test_ipfs_redirect_happy_path(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()

    cid = "bafybeigdyrqlhqnhhhexamplecid1234"
    resp = client.get(f"/ipfs/{cid}", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == server.IPFS_REDIRECT_GATEWAY + cid
    assert resp.headers["Cache-Control"] == "public, max-age=300"


def test_ipfs_redirect_with_subpath(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()

    cid = "bafybeigdyrqlhqnhhhexamplecid1234"
    resp = client.get(f"/ipfs/{cid}/metadata.json", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == f"{server.IPFS_REDIRECT_GATEWAY}{cid}/metadata.json"


def test_ipfs_blocked_cid_returns_451(monkeypatch):
    db = FakeFirestoreDb()
    cid = "bafyblockedcidexample1234567890"
    db.collection("blockedCids").seed(
        cid, {"reason": "moderation", "tokenId": "1", "blockedBy": ADMIN_EMAIL}
    )
    monkeypatch.setattr(server, "firestore_db", db)
    client = server.app.test_client()

    resp = client.get(f"/ipfs/{cid}", follow_redirects=False)

    assert resp.status_code == 451
    assert resp.get_json() == {"error": "This content has been removed"}
    assert resp.headers["Cache-Control"] == "no-store"


def test_ipfs_malformed_cid_is_400(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()

    # Too short to be a real CID.
    resp = client.get("/ipfs/short", follow_redirects=False)

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /admin/block-post-media
# ---------------------------------------------------------------------------


def _block_post(client, body, headers=None):
    return client.post("/admin/block-post-media", json=body, headers=headers)


def test_block_post_media_no_token_is_401(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()
    resp = _block_post(client, {"tokenId": "1"})
    assert resp.status_code == 401


def test_block_post_media_non_admin_is_403(monkeypatch):
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "u1", email=NON_ADMIN_EMAIL)
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()

    resp = _block_post(client, {"tokenId": "1"}, headers=auth_headers())

    assert resp.status_code == 403


def test_block_post_media_requires_token_id_or_cids(monkeypatch):
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()

    resp = _block_post(client, {}, headers=auth_headers())

    assert resp.status_code == 400


def test_block_post_media_unknown_token_id_404(monkeypatch):
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()

    resp = _block_post(client, {"tokenId": "999"}, headers=auth_headers())

    assert resp.status_code == 404


def test_block_post_media_rejects_invalid_explicit_cid(monkeypatch):
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()

    resp = _block_post(client, {"cids": ["not a cid!"]}, headers=auth_headers())

    assert resp.status_code == 400


def test_block_post_media_success_unpins_blocklists_and_flags(monkeypatch):
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)

    db = FakeFirestoreDb()
    post_data = {
        "userId": "creator-1",
        "tokenId": "42",
        "ipfsUrl": "ipfs://cidprimaryimageexample1",
        "media": [{"ipfsUrl": "ipfs://cidsecondimageexample2", "mediaType": "photo"}],
        "likesCount": 3,
    }
    db.collection("posts").seed("42", post_data)
    monkeypatch.setattr(server, "firestore_db", db)
    # FakeContract() has no configured `tokenURI` return, so
    # contract.functions.tokenURI(...).call() -> None, which _extract_cid()
    # safely treats as "no metadata CID found". This also guarantees the
    # route never reaches the real chain.
    monkeypatch.setattr(server, "contract", FakeContract())

    unpin_calls = []
    monkeypatch.setattr(
        server, "unpin_post_media", lambda data: unpin_calls.append(data)
    )

    client = server.app.test_client()
    resp = _block_post(client, {"tokenId": "42"}, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["unpinned"] is True
    assert set(body["blockedCids"]) == {
        "cidprimaryimageexample1",
        "cidsecondimageexample2",
    }

    # unpin_post_media was invoked with the post's data.
    assert unpin_calls == [post_data]

    # Every discovered CID got its own blockedCids doc, with the expected
    # fields.
    for cid in ("cidprimaryimageexample1", "cidsecondimageexample2"):
        doc = db.collection("blockedCids").document(cid).get()
        assert doc.exists
        written = doc.to_dict()
        assert written["reason"] == "moderation"
        assert written["tokenId"] == "42"
        assert written["blockedBy"] == ADMIN_EMAIL
        assert written["blockedAt"] is server.SERVER_TIMESTAMP

    # The post doc is flagged/rejected, same fields the moderation client's
    # existing reject action writes, and existing fields are preserved
    # (merge=True).
    updated_post = db.collection("posts").document("42").get().to_dict()
    assert updated_post["flagged"] is True
    assert updated_post["moderationStatus"] == "rejected"
    assert updated_post["likesCount"] == 3

    # The cache invalidation took effect: GET /ipfs/<cid> for either CID now
    # 451s instead of redirecting.
    ipfs_resp = client.get(
        "/ipfs/cidprimaryimageexample1", follow_redirects=False
    )
    assert ipfs_resp.status_code == 451


def test_block_post_media_accepts_token_id_zero(monkeypatch):
    """tokenId 0 is a real token (first mint). The presence check must not
    treat a falsy-but-valid id as missing (regression: `if data.get(...)`
    turned {"tokenId": 0} into a 400)."""
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)

    db = FakeFirestoreDb()
    db.collection("posts").seed(
        "0", {"userId": "creator-0", "ipfsUrl": "ipfs://cidtokenzeroexample1"}
    )
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(server, "contract", FakeContract())
    monkeypatch.setattr(server, "unpin_post_media", lambda data: None)

    client = server.app.test_client()
    # JSON number 0, not the string "0" -- the shipped moderation client sends
    # a string, but the route contract must match /admin/unlock-post.
    resp = _block_post(client, {"tokenId": 0}, headers=auth_headers())

    assert resp.status_code == 200
    assert resp.get_json()["blockedCids"] == ["cidtokenzeroexample1"]


def test_block_post_media_non_string_cid_is_400_not_500(monkeypatch):
    """A non-string cids element must be a clean 400, not a TypeError 500
    from re.match."""
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    client = server.app.test_client()

    resp = _block_post(client, {"cids": [123]}, headers=auth_headers())
    assert resp.status_code == 400

    resp = _block_post(client, {"cids": "notalist1234"}, headers=auth_headers())
    assert resp.status_code == 400


def test_block_post_media_skips_malformed_derived_cid(monkeypatch):
    """CIDs derived from the post doc get the same CID_PATTERN check as
    explicit ones: a malformed stored URL must not become a blockedCids doc
    ID, while well-formed CIDs from the same post still get blocked."""
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)

    db = FakeFirestoreDb()
    db.collection("posts").seed(
        "7",
        {
            "userId": "creator-7",
            "ipfsUrl": "ipfs://not-a-valid-cid!!",
            "media": [{"ipfsUrl": "ipfs://cidwellformedexample9", "mediaType": "photo"}],
        },
    )
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(server, "contract", FakeContract())
    monkeypatch.setattr(server, "unpin_post_media", lambda data: None)

    client = server.app.test_client()
    resp = _block_post(client, {"tokenId": "7"}, headers=auth_headers())

    assert resp.status_code == 200
    assert resp.get_json()["blockedCids"] == ["cidwellformedexample9"]
    assert (
        db.collection("blockedCids").document("not-a-valid-cid!!").get().exists
        is False
    )


def test_block_post_media_explicit_cids_only(monkeypatch):
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)
    db = FakeFirestoreDb()
    monkeypatch.setattr(server, "firestore_db", db)

    unpin_calls = []
    monkeypatch.setattr(
        server, "unpin_post_media", lambda data: unpin_calls.append(data)
    )

    client = server.app.test_client()
    resp = _block_post(
        client,
        {"cids": ["explicitcidnopost1234"], "reason": "dmca"},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["unpinned"] is False
    assert body["blockedCids"] == ["explicitcidnopost1234"]
    # No post doc was ever loaded, so unpin_post_media must not run.
    assert unpin_calls == []

    written = (
        db.collection("blockedCids").document("explicitcidnopost1234").get().to_dict()
    )
    assert written["reason"] == "dmca"
    assert written["tokenId"] is None


# ---------------------------------------------------------------------------
# POST /admin/unblock-cid
# ---------------------------------------------------------------------------


def test_unblock_cid_success(monkeypatch):
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", ADMIN_EMAIL)
    install_auth(monkeypatch, server, "admin-uid", email=ADMIN_EMAIL)

    db = FakeFirestoreDb()
    cid = "cidtounblockexample123"
    db.collection("blockedCids").seed(cid, {"reason": "moderation"})
    monkeypatch.setattr(server, "firestore_db", db)
    client = server.app.test_client()

    # Confirm it's blocked beforehand.
    assert client.get(f"/ipfs/{cid}", follow_redirects=False).status_code == 451

    resp = client.post(
        "/admin/unblock-cid", json={"cid": cid}, headers=auth_headers()
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    assert db.collection("blockedCids").document(cid).get().exists is False

    # Cache invalidated: the CID now redirects again instead of 451ing.
    resp2 = client.get(f"/ipfs/{cid}", follow_redirects=False)
    assert resp2.status_code == 302
