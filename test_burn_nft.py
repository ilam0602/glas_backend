"""Route-level tests for POST /burn (server.burn_nft)."""
import server

from fakes_common import (
    FakeContract,
    auth_headers,
    fake_send_contract_transaction,
    install_auth,
)

USER_ID = "user-1"
TOKEN_ID = 42


def _post(client, body, headers=None):
    return client.post("/burn", json=body, headers=headers)


def test_missing_fields_returns_400():
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Missing userId or tokenId"}


def test_no_token_is_401():
    client = server.app.test_client()
    resp = _post(client, {"userId": USER_ID, "tokenId": TOKEN_ID})
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Missing or invalid authorization token"}


def test_mismatched_uid_is_403(monkeypatch):
    install_auth(monkeypatch, server, "someone-else")
    client = server.app.test_client()
    resp = _post(
        client, {"userId": USER_ID, "tokenId": TOKEN_ID}, headers=auth_headers()
    )
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "Forbidden"}


def test_happy_path_response_shape(monkeypatch):
    install_auth(monkeypatch, server, USER_ID)
    monkeypatch.setattr(server, "contract", FakeContract())
    send_fn = fake_send_contract_transaction(
        receipt={"status": 1, "blockNumber": 123}, tx_hash="0xdeadbeef"
    )
    monkeypatch.setattr(server, "send_contract_transaction", send_fn)
    client = server.app.test_client()

    resp = _post(
        client, {"userId": USER_ID, "tokenId": TOKEN_ID}, headers=auth_headers()
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["transaction_hash"] == "0xdeadbeef"
    assert body["token_id"] == TOKEN_ID
    assert body["receipt"] == {"status": 1, "blockNumber": 123}

    # Burned via burnVirtual(userIdHash, tokenId) on the real contract object.
    (name, args, _kwargs), = server.contract.functions.calls
    assert name == "burnVirtual"
    assert args[0] == server.compute_user_id_hash(USER_ID)
    assert args[1] == TOKEN_ID
    assert len(send_fn.calls) == 1


def test_contract_error_returns_500(monkeypatch):
    install_auth(monkeypatch, server, USER_ID)
    monkeypatch.setattr(server, "contract", FakeContract())

    def _boom(fn_call, gas_limit):
        raise RuntimeError("chain is down")

    monkeypatch.setattr(server, "send_contract_transaction", _boom)
    client = server.app.test_client()

    resp = _post(
        client, {"userId": USER_ID, "tokenId": TOKEN_ID}, headers=auth_headers()
    )

    assert resp.status_code == 500
    assert "Error burning token" in resp.get_json()["error"]


def test_non_integer_token_id_returns_500(monkeypatch):
    """tokenId is coerced with int() AFTER the auth check but BEFORE the
    try/except block, so a non-numeric tokenId raises an uncaught ValueError
    that Flask turns into a generic 500 - pinning today's behavior (NOT a
    clean 400) rather than prescribing the fix."""
    install_auth(monkeypatch, server, USER_ID)
    client = server.app.test_client()
    resp = client.post(
        "/burn",
        json={"userId": USER_ID, "tokenId": "not-a-number"},
        headers=auth_headers(),
    )
    assert resp.status_code == 500
