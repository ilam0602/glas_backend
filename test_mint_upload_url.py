import json, server
from server import app

def _client():
    return app.test_client()

def test_upload_url_rejects_bad_auth(monkeypatch):
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "u1")
    r = _client().post("/mint/upload-url", json={
        "userId": "u2", "objectKind": "mint", "uploadId": "a", "index": 0,
        "contentType": "video/mp4", "size": 100})
    assert r.status_code == 401

def test_upload_url_rejects_oversize(monkeypatch):
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "u1")
    monkeypatch.setattr(server, "MAX_VIDEO_BYTES", 1000)
    r = _client().post("/mint/upload-url", json={
        "userId": "u1", "objectKind": "mint", "uploadId": "a", "index": 0,
        "contentType": "video/mp4", "size": 5000})
    assert r.status_code == 413

def test_upload_url_returns_signed_put(monkeypatch):
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "u1")
    monkeypatch.setattr(server, "MAX_VIDEO_BYTES", 150 * 1024 * 1024)
    class FakeBlob: pass
    class FakeBucket:
        def blob(self, path):
            b = FakeBlob(); b.path = path; return b
    monkeypatch.setattr(server, "gcs_bucket", FakeBucket())
    monkeypatch.setattr(server, "generate_signed_url",
                        lambda blob, method, expiration, headers=None: f"https://signed/{blob.path}?m={method}")
    r = _client().post("/mint/upload-url", json={
        "userId": "u1", "objectKind": "mint", "uploadId": "job1", "index": 0,
        "contentType": "video/mp4", "size": 10_000_000})
    assert r.status_code == 200
    body = r.get_json()
    assert body["objectPath"] == "mint_uploads/job1/0.mp4"
    assert body["uploadUrl"] == "https://signed/mint_uploads/job1/0.mp4?m=PUT"
    assert body["requiredHeaders"]["X-Goog-Content-Length-Range"].startswith("0,")
