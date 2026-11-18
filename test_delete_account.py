"""Route-level tests for POST /delete-account (server.delete_account).

The route is "best-effort": every step is wrapped in its own try/except that
appends to an `errors[]` list and continues, and the response is ALWAYS
`{"deleted": True, "errors": [...]}` once auth/firestore-configured checks
pass (pinning today's behavior, not a redesign). These tests cover the two
early-return guards, resilience to a failing step (burnVirtual raising still
lets later cleanup run and still returns 200), and the concrete side effects
of a representative happy path (own post + username + token balance +
Firebase Auth user all deleted).
"""
import server

from fakes_common import (
    FakeContract,
    FakeFirestoreDb,
    auth_headers,
    fake_send_contract_transaction,
    install_auth,
)

UID = "user-1"


def _post(client, headers=None):
    return client.post("/delete-account", json={}, headers=headers)


def _install_chain(monkeypatch, *, virtual_tokens=None):
    monkeypatch.setattr(
        server, "contract", FakeContract(getVirtualTokens=list(virtual_tokens or []))
    )
    send_fn = fake_send_contract_transaction()
    monkeypatch.setattr(server, "send_contract_transaction", send_fn)
    return send_fn


def _install_firestore(monkeypatch):
    db = FakeFirestoreDb()
    monkeypatch.setattr(server, "firestore_db", db)
    # Force GCS off regardless of what real creds server.py loaded at import
    # time - never let a real GCS client get touched by this route.
    monkeypatch.setattr(server, "gcs_bucket", None)
    return db


def _install_auth_delete(monkeypatch, recorder=None):
    recorder = recorder if recorder is not None else []
    monkeypatch.setattr(
        server.firebase_auth, "delete_user", lambda uid: recorder.append(uid)
    )
    return recorder


def test_no_token_is_401():
    client = server.app.test_client()
    resp = _post(client)
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Missing or invalid authorization token"}


def test_firebase_not_configured_returns_500(monkeypatch):
    install_auth(monkeypatch, server, UID)
    monkeypatch.setattr(server, "firestore_db", None)
    client = server.app.test_client()
    resp = _post(client, headers=auth_headers())
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Firebase not configured on server"}


def test_empty_account_deletes_cleanly(monkeypatch):
    install_auth(monkeypatch, server, UID)
    db = _install_firestore(monkeypatch)
    _install_chain(monkeypatch, virtual_tokens=[])
    auth_deletes = _install_auth_delete(monkeypatch)
    client = server.app.test_client()

    resp = _post(client, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"] is True
    # Only the (expected, documented) gcs-not-configured note - no other
    # step should fail against an empty but well-formed fake Firestore.
    assert body["errors"] == ["gcs not configured, skipped blob cleanup"]
    assert auth_deletes == [UID]
    assert db.collection("users").document(UID).get().exists is False


def test_body_is_ignored_uid_comes_from_token_only(monkeypatch):
    """The docstring says 'any request body is ignored' - verify a body
    claiming a different userId has no effect (no 403, no cross-user action);
    the route only ever acts on the token's own uid."""
    install_auth(monkeypatch, server, UID)
    _install_firestore(monkeypatch)
    _install_chain(monkeypatch, virtual_tokens=[])
    auth_deletes = _install_auth_delete(monkeypatch)
    client = server.app.test_client()

    resp = client.post(
        "/delete-account", json={"userId": "someone-else"}, headers=auth_headers()
    )

    assert resp.status_code == 200
    assert auth_deletes == [UID]  # the TOKEN's uid, not the body's userId


def test_username_and_token_balance_are_deleted(monkeypatch):
    install_auth(monkeypatch, server, UID)
    db = _install_firestore(monkeypatch)
    db.collection("users").seed(UID, {"username": "alice"})
    db.collection("usernames").seed("alice", {"userId": UID})
    db.collection("tokenBalances").seed(UID, {"purchasedBalance": 10, "earnedBalance": 5})
    _install_chain(monkeypatch, virtual_tokens=[])
    _install_auth_delete(monkeypatch)
    client = server.app.test_client()

    resp = _post(client, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"] is True
    assert "delete usernames/alice" not in " ".join(body["errors"])
    assert db.collection("usernames").document("alice").get().exists is False
    assert db.collection("tokenBalances").document(UID).get().exists is False
    assert db.collection("users").document(UID).get().exists is False


def test_own_posts_are_deleted(monkeypatch):
    install_auth(monkeypatch, server, UID)
    db = _install_firestore(monkeypatch)
    db.collection("posts").seed("200", {"userId": UID, "mediaType": "photo"})
    db.collection("posts").seed("201", {"userId": "someone-else", "mediaType": "photo"})
    _install_chain(monkeypatch, virtual_tokens=[])
    _install_auth_delete(monkeypatch)
    client = server.app.test_client()

    resp = _post(client, headers=auth_headers())

    assert resp.status_code == 200
    assert db.collection("posts").document("200").get().exists is False
    # Another user's post is left untouched.
    assert db.collection("posts").document("201").get().exists is True


def test_burn_failure_is_recorded_but_deletion_still_completes(monkeypatch):
    """burnVirtual raising for one token must not abort the rest of the
    best-effort pipeline - it's appended to errors[] and everything else
    (including the final Firebase Auth delete) still runs, per the route's
    documented contract."""
    install_auth(monkeypatch, server, UID)
    _install_firestore(monkeypatch)
    monkeypatch.setattr(server, "contract", FakeContract(getVirtualTokens=[7]))

    def _boom(fn_call, gas_limit):
        raise RuntimeError("chain is down")

    monkeypatch.setattr(server, "send_contract_transaction", _boom)
    auth_deletes = _install_auth_delete(monkeypatch)
    client = server.app.test_client()

    resp = _post(client, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"] is True
    assert any("burnVirtual 7" in e for e in body["errors"])
    # Best-effort: later cleanup steps (Firebase Auth delete) still ran.
    assert auth_deletes == [UID]


def test_get_virtual_tokens_failure_is_recorded_and_pipeline_continues(monkeypatch):
    install_auth(monkeypatch, server, UID)
    db = _install_firestore(monkeypatch)
    db.collection("tokenBalances").seed(UID, {"purchasedBalance": 1})

    class _BoomFunctions:
        def getVirtualTokens(self, *_args, **_kwargs):
            raise RuntimeError("rpc timeout")

    monkeypatch.setattr(server, "contract", type("C", (), {"functions": _BoomFunctions()})())
    _install_auth_delete(monkeypatch)
    client = server.app.test_client()

    resp = _post(client, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"] is True
    assert any("getVirtualTokens" in e for e in body["errors"])
    # Firestore cleanup still proceeded despite the chain read failing.
    assert db.collection("tokenBalances").document(UID).get().exists is False


def test_auth_delete_user_failure_is_recorded(monkeypatch):
    install_auth(monkeypatch, server, UID)
    _install_firestore(monkeypatch)
    _install_chain(monkeypatch, virtual_tokens=[])

    def _boom(uid):
        raise RuntimeError("auth backend down")

    monkeypatch.setattr(server.firebase_auth, "delete_user", _boom)
    client = server.app.test_client()

    resp = _post(client, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"] is True
    assert any("auth delete_user" in e for e in body["errors"])
