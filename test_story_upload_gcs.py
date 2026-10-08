import server
from server import app


class FakeBlob:
    def __init__(self):
        self.deleted = False

    def download_as_bytes(self):
        return b"VIDEOBYTES"

    def exists(self):
        return True

    def delete(self):
        self.deleted = True


def test_story_gcs_video_downloads_and_mints(monkeypatch):
    blob = FakeBlob()
    monkeypatch.setattr(
        server, "gcs_bucket", type("B", (), {"blob": lambda s, p: blob})()
    )
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "u1")
    monkeypatch.setattr(
        server, "analyze_video", lambda b: {"flagged": False, "reason": ""}
    )
    started = {}
    monkeypatch.setattr(
        server.threading,
        "Thread",
        lambda target, args, daemon: type(
            "T", (), {"start": lambda s: started.setdefault("ran", True)}
        )(),
    )
    r = app.test_client().post(
        "/story-upload",
        json={"storyId": "s1", "userId": "u1", "mediaType": "video", "source": "gcs"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["storyId"] == "s1"
    assert body["mediaType"] == "video"
    assert body["flagged"] is False
    assert started.get("ran") is True
    assert blob.deleted is False


def test_story_gcs_video_flagged_still_mints_and_keeps_blob(monkeypatch):
    """Matches the existing /story-upload (base64) behavior: flagged content
    is marked flagged in the response but is NOT deleted and still proceeds
    to the background mint (moderation review happens after the fact via
    the flagged/pending-review Firestore fields, not by deleting media)."""
    blob = FakeBlob()
    monkeypatch.setattr(
        server, "gcs_bucket", type("B", (), {"blob": lambda s, p: blob})()
    )
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "u1")
    monkeypatch.setattr(
        server,
        "analyze_video",
        lambda b: {"flagged": True, "reason": "nudity"},
    )
    started = {}
    monkeypatch.setattr(
        server.threading,
        "Thread",
        lambda target, args, daemon: type(
            "T", (), {"start": lambda s: started.setdefault("ran", True)}
        )(),
    )
    r = app.test_client().post(
        "/story-upload",
        json={"storyId": "s2", "userId": "u1", "mediaType": "video", "source": "gcs"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["flagged"] is True
    assert body["flagReason"] == "nudity"
    assert started.get("ran") is True
    assert blob.deleted is False


def test_story_gcs_rejects_mismatched_user(monkeypatch):
    blob = FakeBlob()
    monkeypatch.setattr(
        server, "gcs_bucket", type("B", (), {"blob": lambda s, p: blob})()
    )
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "someone-else")
    r = app.test_client().post(
        "/story-upload",
        json={"storyId": "s3", "userId": "u1", "mediaType": "video", "source": "gcs"},
    )
    assert r.status_code == 401


def test_story_gcs_missing_blob_returns_400(monkeypatch):
    class MissingBlob(FakeBlob):
        def exists(self):
            return False

    blob = MissingBlob()
    monkeypatch.setattr(
        server, "gcs_bucket", type("B", (), {"blob": lambda s, p: blob})()
    )
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "u1")
    r = app.test_client().post(
        "/story-upload",
        json={"storyId": "s4", "userId": "u1", "mediaType": "video", "source": "gcs"},
    )
    assert r.status_code == 400
