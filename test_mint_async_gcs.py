import server
from server import app


class FakeBlob:
    def __init__(self, exists=True, size=10):
        self._exists = exists
        self.size = size
        self.deleted = False

    def exists(self):
        return self._exists

    def delete(self):
        self.deleted = True


class FakeBucket:
    def __init__(self, blob):
        self._blob = blob

    def blob(self, path):
        return self._blob


class FakeJobRef:
    def __init__(self):
        self.created = None
        self.deleted = False

    def create(self, doc):
        self.created = doc

    def delete(self):
        self.deleted = True

    def get(self):
        created = self.created
        return type("Snap", (), {"to_dict": lambda self: created})()


class FakeCollection:
    def __init__(self, job_ref):
        self._job_ref = job_ref

    def document(self, doc_id):
        return self._job_ref


class FakeFirestoreDb:
    def __init__(self, job_ref):
        self._job_ref = job_ref

    def collection(self, name):
        return FakeCollection(self._job_ref)


class FakeGcsClient:
    def list_blobs(self, *a, **kw):
        return []


def _setup(monkeypatch, blob):
    job_ref = FakeJobRef()
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "u1")
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb(job_ref))
    monkeypatch.setattr(server, "gcs_bucket", FakeBucket(blob))
    monkeypatch.setattr(server, "gcs_client", FakeGcsClient())
    monkeypatch.setattr(server, "tasks_configured", lambda: True)
    monkeypatch.setattr(
        server,
        "_stash_mint_upload",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not stash gcs item")),
    )
    monkeypatch.setattr(server, "enqueue_mint_job", lambda jid: None)
    return job_ref


def test_gcs_video_item_verifies_blob_and_sets_sources(monkeypatch):
    blob = FakeBlob(exists=True, size=10_000_000)
    job_ref = _setup(monkeypatch, blob)
    r = app.test_client().post("/mint/async", json={
        "source": "gcs", "mediaType": "video", "userId": "u1", "uploadId": "job1"})
    assert r.status_code == 202
    assert job_ref.created["itemSources"] == ["gcs"]


def test_gcs_video_missing_blob_rejected(monkeypatch):
    blob = FakeBlob(exists=False)
    _setup(monkeypatch, blob)
    r = app.test_client().post("/mint/async", json={
        "source": "gcs", "mediaType": "video", "userId": "u1", "uploadId": "job2"})
    assert r.status_code == 400


def test_gcs_video_oversize_blob_rejected(monkeypatch):
    blob = FakeBlob(exists=True, size=10**12)
    monkeypatch.setattr(server, "MAX_VIDEO_BYTES", 1000)
    _setup(monkeypatch, blob)
    r = app.test_client().post("/mint/async", json={
        "source": "gcs", "mediaType": "video", "userId": "u1", "uploadId": "job3"})
    assert r.status_code == 400
