import server
from server import autoflag_should_flag


def test_baseline_flags_low_like_post():
    assert autoflag_should_flag(5, 0, None) is True
    assert autoflag_should_flag(5, 49, None) is True


def test_ten_percent_boundary_is_strict():
    # 50 likes * 0.10 == 5.0; 5 is NOT > 5.0
    assert autoflag_should_flag(5, 50, None) is False
    # 100 likes: need > 10, so 10 no, 11 yes
    assert autoflag_should_flag(10, 100, None) is False
    assert autoflag_should_flag(11, 100, None) is True


def test_below_baseline_never_flags():
    assert autoflag_should_flag(4, 0, None) is False


def test_approved_post_is_immune():
    assert autoflag_should_flag(100, 0, "approved") is False


def test_pending_or_rejected_still_evaluated():
    assert autoflag_should_flag(5, 0, "pending") is True
    assert autoflag_should_flag(5, 0, "rejected") is True


def test_none_counts_treated_as_zero():
    assert autoflag_should_flag(None, 50, None) is False   # 0 reports -> no flag
    assert autoflag_should_flag(5, None, None) is True     # 0 likes, 5 reports -> flag


# ---------------------------------------------------------------------------
# POST /report-post endpoint tests
# ---------------------------------------------------------------------------
# `import server` connects to REAL Firebase + blockchain at import time, so
# every test here MUST monkeypatch server.firebase_auth.verify_id_token and
# server.firestore_db with fakes -- never touch production Firestore.


class FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class FakeReportDoc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class FakeReportsCollection:
    """Models post_ref.collection('reports'): .document(uid).set(...) and .stream()."""

    def __init__(self, initial_reports=None):
        # keyed by uid -> report dict, so writing the same uid again is idempotent
        self.reports = dict(initial_reports or {})

    def document(self, uid):
        return FakeReportDocRef(self, uid)

    def stream(self):
        return [FakeReportDoc(data) for data in self.reports.values()]


class FakeReportDocRef:
    def __init__(self, collection, uid):
        self.collection_ref = collection
        self.uid = uid

    def set(self, data):
        self.collection_ref.reports[self.uid] = data


class FakePostRef:
    """Models firestore_db.collection('posts').document(token_id)."""

    def __init__(self, post_data, initial_reports=None):
        self.post_data = post_data
        self.set_calls = []
        self._reports = FakeReportsCollection(initial_reports)

    def get(self):
        return FakeSnapshot(self.post_data)

    def set(self, data, merge=False):
        self.set_calls.append({"data": data, "merge": merge})
        if self.post_data is None:
            self.post_data = {}
        self.post_data.update(data)

    def collection(self, name):
        assert name == "reports"
        return self._reports


class FakePostsCollection:
    def __init__(self, post_ref):
        self.post_ref = post_ref

    def document(self, token_id):
        return self.post_ref


class FakeFirestoreDb:
    def __init__(self, post_ref):
        self.post_ref = post_ref

    def collection(self, name):
        assert name == "posts"
        return FakePostsCollection(self.post_ref)


def _auth_headers(token="valid-token"):
    return {"Authorization": f"Bearer {token}"}


def test_rejects_missing_token(monkeypatch):
    client = server.app.test_client()
    resp = client.post("/report-post", json={"tokenId": "1", "reason": "spam"})
    assert resp.status_code == 401


def test_rejects_invalid_reason(monkeypatch):
    monkeypatch.setattr(
        server.firebase_auth,
        "verify_id_token",
        lambda token: {"uid": "u1", "email": "u1@example.com"},
    )
    client = server.app.test_client()
    resp = client.post(
        "/report-post",
        json={"tokenId": "1", "reason": "banana"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


def test_unknown_post_404(monkeypatch):
    monkeypatch.setattr(
        server.firebase_auth,
        "verify_id_token",
        lambda token: {"uid": "u1", "email": "u1@example.com"},
    )
    fake_post_ref = FakePostRef(post_data=None)
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb(fake_post_ref))
    client = server.app.test_client()
    resp = client.post(
        "/report-post",
        json={"tokenId": "1", "reason": "spam"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404


def test_records_report_and_flags_at_threshold(monkeypatch):
    monkeypatch.setattr(
        server.firebase_auth,
        "verify_id_token",
        lambda token: {"uid": "u5", "email": "u5@example.com"},
    )
    existing_reports = {
        f"u{i}": {"reporterId": f"u{i}", "reason": "spam"} for i in range(1, 5)
    }
    fake_post_ref = FakePostRef(
        post_data={"likesCount": 0}, initial_reports=existing_reports
    )
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb(fake_post_ref))
    client = server.app.test_client()
    resp = client.post(
        "/report-post",
        json={"tokenId": "1", "reason": "spam"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"reportsCount": 5, "flagged": True}

    post_set_calls = [c for c in fake_post_ref.set_calls]
    assert len(post_set_calls) == 1
    written = post_set_calls[0]["data"]
    assert written["flagged"] is True
    assert written["flagSource"] == "user_report"
    assert written["moderationStatus"] == "pending"


def test_idempotent_same_reporter(monkeypatch):
    monkeypatch.setattr(
        server.firebase_auth,
        "verify_id_token",
        lambda token: {"uid": "u1", "email": "u1@example.com"},
    )
    existing_reports = {"u1": {"reporterId": "u1", "reason": "spam"}}
    fake_post_ref = FakePostRef(
        post_data={"likesCount": 0}, initial_reports=existing_reports
    )
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb(fake_post_ref))
    client = server.app.test_client()
    resp = client.post(
        "/report-post",
        json={"tokenId": "1", "reason": "spam"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["reportsCount"] == 1
