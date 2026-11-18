"""Tests for csam_screening.py (provider-agnostic screening + match path) and
its hook points in server.py (mint pipeline, /story-upload, /avatar-upload,
/csam/sweep).

All provider network calls are mocked (never send real imagery to WebPurify).
GCS/Firestore/auth are faked per fakes_common.py conventions -- never touch
real services. See docs/specs/2026-10-09-csam-screening-quarantine-report-
design.md for the design this implements.
"""
import base64
import json

import pytest
import requests

import csam_screening
import server
from fakes_common import (
    FakeContract,
    FakeFirestoreDb,
    auth_headers,
    fake_send_contract_transaction,
    install_auth,
)


# ---------------------------------------------------------------------------
# Fake GCS primitives (separate from fakes_common: these model
# google-cloud-storage's Blob/Bucket/Client shape, which fakes_common doesn't
# cover since no existing test needed real blob storage semantics).
# ---------------------------------------------------------------------------


class FakeCsamBlob:
    def __init__(self, bucket, path):
        self._bucket = bucket
        self.path = path

    def upload_from_string(self, data, content_type=None):
        self._bucket.objects[self.path] = {"data": data, "content_type": content_type}

    def delete(self):
        self._bucket.objects.pop(self.path, None)
        self._bucket.deleted.append(self.path)

    def exists(self):
        return self.path in self._bucket.objects

    def download_as_bytes(self):
        return self._bucket.objects[self.path]["data"]

    def download_to_filename(self, path):
        with open(path, "wb") as f:
            f.write(self._bucket.objects[self.path]["data"])


class FakeCsamBucket:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def blob(self, path):
        return FakeCsamBlob(self, path)


class FakeCsamStorageClient:
    """Mimics google.cloud.storage.Client's `.bucket(name)` -- used as the
    `storage_client` param to handle_match (quarantine bucket) and as
    server.gcs_client in route-level tests."""

    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, FakeCsamBucket())


def _fake_sign_url(blob, method, expiration):
    return f"https://signed.example/{blob.path}"


# ---------------------------------------------------------------------------
# Fake WebPurify HTTP layer
# ---------------------------------------------------------------------------


class FakeHttpResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def make_webpurify_get(img_id="img-123", status_sequence=("approved",)):
    """Fake `requests.get` for csam_screening's WebPurify calls: dispatches on
    the `method` query param, returning canned imgcheck/imgstatus JSON.
    `status_sequence` is consumed one status per imgstatus poll (the last
    entry repeats once exhausted, to simulate a stuck/pending vendor)."""
    statuses = list(status_sequence)
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        method = (params or {}).get("method")
        if method == csam_screening.WEBPURIFY_SUBMIT_METHOD:
            return FakeHttpResponse({"rsp": {"img_id": img_id}})
        if method == csam_screening.WEBPURIFY_STATUS_METHOD:
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            return FakeHttpResponse({"rsp": {"status": status, "img_id": params.get("img_id")}})
        raise AssertionError(f"unexpected webpurify method {method!r}")

    fake_get.calls = calls
    return fake_get


@pytest.fixture
def webpurify_enabled(monkeypatch):
    """Configure the webpurify provider with a fast poll timeout (no real
    sleeping needed: CSAM_POLL_SECONDS=0 means the very first status check
    that isn't resolved already exceeds the deadline)."""
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", "test-key")
    monkeypatch.setattr(csam_screening, "CSAM_POLL_SECONDS", 0)


# ---------------------------------------------------------------------------
# Disabled provider: pure no-op
# ---------------------------------------------------------------------------


def test_disabled_provider_image_is_clear_with_zero_side_effects(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("must not make a network call when disabled")

    monkeypatch.setattr(csam_screening.requests, "get", _boom)
    bucket = FakeCsamBucket()

    result = csam_screening.screen_image_bytes(
        b"bytes", "image/jpeg", {"uid": "u1", "source": "mint"},
        gcs_bucket=bucket, sign_url_fn=_fake_sign_url,
    )

    assert result.verdict == "clear"
    assert result.provider == "disabled"
    assert bucket.objects == {}  # no temp screening object was ever written


def test_disabled_provider_video_is_clear_with_zero_side_effects(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("must not extract frames when disabled")

    monkeypatch.setattr(csam_screening, "_extract_video_frames", _boom)

    result = csam_screening.screen_video_bytes(b"bytes", "video/mp4", {"uid": "u1"})

    assert result.verdict == "clear"
    assert result.provider == "disabled"


# ---------------------------------------------------------------------------
# WebPurify image screening
# ---------------------------------------------------------------------------


def test_webpurify_image_clear_on_approved(monkeypatch, webpurify_enabled):
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("approved",))
    )
    bucket = FakeCsamBucket()

    result = csam_screening.screen_image_bytes(
        b"bytes", "image/jpeg", {"uid": "u1"}, gcs_bucket=bucket, sign_url_fn=_fake_sign_url,
    )

    assert result.verdict == "clear"
    assert result.provider == "webpurify"
    # Temp screening object was written then cleaned up: exactly one delete,
    # of a csam-screen-tmp/ path, and nothing left behind.
    assert len(bucket.deleted) == 1
    assert bucket.deleted[0].startswith("csam-screen-tmp/")
    assert bucket.objects == {}


def test_webpurify_image_declined_is_clear_not_match(monkeypatch, webpurify_enabled):
    """'declined' is a POLICY rejection (nudity/violence/etc), not a CSAM
    hash match -- must NOT be treated as known_match (spec: OpenAI
    moderation already handles policy)."""
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("declined",))
    )

    result = csam_screening.screen_image_bytes(
        b"bytes", "image/jpeg", {}, gcs_bucket=FakeCsamBucket(), sign_url_fn=_fake_sign_url,
    )

    assert result.verdict == "clear"


def test_webpurify_image_flagged_is_known_match(monkeypatch, webpurify_enabled):
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("flagged",))
    )

    result = csam_screening.screen_image_bytes(
        b"bytes", "image/jpeg", {}, gcs_bucket=FakeCsamBucket(), sign_url_fn=_fake_sign_url,
    )

    assert result.verdict == "known_match"
    assert result.provider_ref["imgId"] == "img-123"


def test_webpurify_image_still_pending_at_timeout(monkeypatch, webpurify_enabled):
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("pending",))
    )

    result = csam_screening.screen_image_bytes(
        b"bytes", "image/jpeg", {}, gcs_bucket=FakeCsamBucket(), sign_url_fn=_fake_sign_url,
    )

    assert result.verdict == "pending"
    assert result.provider_ref["imgId"] == "img-123"
    assert csam_screening.extract_pending_img_ids(result) == ["img-123"]


def test_webpurify_missing_gcs_wiring_is_error(webpurify_enabled):
    result = csam_screening.screen_image_bytes(b"bytes", "image/jpeg", {})
    assert result.verdict == "error"


def test_webpurify_missing_api_key_is_error(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", None)

    result = csam_screening.screen_image_bytes(
        b"bytes", "image/jpeg", {}, gcs_bucket=FakeCsamBucket(), sign_url_fn=_fake_sign_url,
    )
    assert result.verdict == "error"


def test_webpurify_temp_object_deleted_even_on_submit_failure(monkeypatch, webpurify_enabled):
    def _raise(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(csam_screening.requests, "get", _raise)
    bucket = FakeCsamBucket()

    result = csam_screening.screen_image_bytes(
        b"bytes", "image/jpeg", {}, gcs_bucket=bucket, sign_url_fn=_fake_sign_url,
    )

    assert result.verdict == "error"
    assert bucket.objects == {}  # finally block cleaned up the temp object
    assert len(bucket.deleted) == 1


def test_unknown_provider_is_error(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "hive")
    result = csam_screening.screen_image_bytes(b"bytes", "image/jpeg", {})
    assert result.verdict == "error"


def test_redact_secrets_replaces_api_key(monkeypatch):
    """_redact_secrets is the defensive backstop that strips the api_key out of
    any string headed for a log line or a persisted provider_ref."""
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", "SECRET123")
    text = "403 Client Error for url: https://wp/rest/?api_key=SECRET123&m=x"
    redacted = csam_screening._redact_secrets(text)
    assert "SECRET123" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_payload_strips_api_key_from_nested_vendor_response(monkeypatch):
    """Raw WebPurify response bodies are persisted in provider_ref as evidence.
    The response shape is unverified pre-account, and some APIs echo the
    request back -- so every payload must be redacted before storage."""
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", "SECRET123")
    payload = {
        "rsp": {
            "status": "approved",
            "echo": {"request_url": "https://wp/rest/?api_key=SECRET123&m=x"},
        }
    }
    redacted = csam_screening._redact_payload(payload)
    assert "SECRET123" not in json.dumps(redacted)
    assert redacted["rsp"]["status"] == "approved"


def test_redact_payload_withholds_unserializable_payload(monkeypatch):
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", "SECRET123")
    redacted = csam_screening._redact_payload({"bad": object()})
    assert redacted == {"reason": "payload withheld (unserializable)"}


def test_webpurify_api_key_never_leaks_into_logs_or_provider_ref(
    monkeypatch, webpurify_enabled, caplog
):
    """A requests exception embeds the full request URL -- including the
    api_key query param -- in str(e). That string must reach NEITHER the logs
    NOR provider_ref (which is persisted to Firestore). Regression guard for
    the WEBPURIFY_API_KEY leak."""
    api_key = "test-key"  # matches the webpurify_enabled fixture

    def _raise_with_key_in_url(*args, **kwargs):
        # Reproduce exactly what requests.raise_for_status() builds: an
        # HTTPError whose message contains the request URL + api_key.
        resp = requests.Response()
        resp.status_code = 403
        resp.url = (
            f"https://im-api1.webpurify.com/services/rest/?api_key={api_key}"
            "&method=webpurify.live.imgcheck&imgurl=https%3A%2F%2Fsigned"
        )
        resp.reason = "Forbidden"
        resp.raise_for_status()

    monkeypatch.setattr(csam_screening.requests, "get", _raise_with_key_in_url)

    with caplog.at_level("ERROR", logger="csam_screening"):
        result = csam_screening.screen_image_bytes(
            b"bytes", "image/jpeg", {"uid": "u1", "source": "mint"},
            gcs_bucket=FakeCsamBucket(), sign_url_fn=_fake_sign_url,
        )

    assert result.verdict == "error"
    assert api_key not in json.dumps(result.provider_ref)
    assert api_key not in caplog.text
    # The safe reason still identifies the failure by class + status code.
    assert "HTTPError" in result.provider_ref["reason"]
    assert "403" in result.provider_ref["reason"]


# ---------------------------------------------------------------------------
# Video frame sampling: cap math + any-frame-match semantics
# ---------------------------------------------------------------------------


def test_compute_frame_timestamps_under_cap_is_1fps_plus_last_frame():
    timestamps = csam_screening._compute_frame_timestamps(5.0, 60)
    assert timestamps == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


def test_compute_frame_timestamps_non_integer_duration_includes_exact_last_frame():
    timestamps = csam_screening._compute_frame_timestamps(4.7, 60)
    assert timestamps[0] == 0.0
    assert timestamps[-1] == 4.7


def test_compute_frame_timestamps_cap_binds_spreads_evenly():
    timestamps = csam_screening._compute_frame_timestamps(600.0, 10)
    assert len(timestamps) == 10
    assert timestamps[0] == 0.0
    assert timestamps[-1] == 600.0
    # Evenly spread: constant spacing (within float rounding noise).
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    assert max(gaps) - min(gaps) < 0.01


def test_compute_frame_timestamps_zero_duration_is_single_frame():
    assert csam_screening._compute_frame_timestamps(0.0, 60) == [0.0]


def test_screen_video_bytes_any_frame_match_stops_early(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(
        csam_screening, "_extract_video_frames", lambda video_bytes: [b"f0", b"f1", b"f2"]
    )

    calls = []

    def fake_screen_image(image_bytes, content_type, context, **kw):
        calls.append(image_bytes)
        if image_bytes == b"f1":
            return csam_screening.ScreenResult(
                verdict="known_match", provider="webpurify", provider_ref={"imgId": "x"}
            )
        return csam_screening.ScreenResult(verdict="clear", provider="webpurify")

    monkeypatch.setattr(csam_screening, "screen_image_bytes", fake_screen_image)

    result = csam_screening.screen_video_bytes(b"video", "video/mp4", {})

    assert result.verdict == "known_match"
    assert result.frames_screened == 2
    assert calls == [b"f0", b"f1"]  # f2 never screened -- stopped at the match


def test_screen_video_bytes_all_clear(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(
        csam_screening, "_extract_video_frames", lambda video_bytes: [b"f0", b"f1"]
    )
    monkeypatch.setattr(
        csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(verdict="clear", provider="webpurify"),
    )

    result = csam_screening.screen_video_bytes(b"video", "video/mp4", {})
    assert result.verdict == "clear"
    assert result.frames_screened == 2


def test_screen_video_bytes_pending_frames_are_aggregated(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(
        csam_screening, "_extract_video_frames", lambda video_bytes: [b"f0", b"f1"]
    )

    def fake_screen_image(image_bytes, content_type, context, **kw):
        img_id = "a" if image_bytes == b"f0" else "b"
        return csam_screening.ScreenResult(
            verdict="pending", provider="webpurify", provider_ref={"imgId": img_id}
        )

    monkeypatch.setattr(csam_screening, "screen_image_bytes", fake_screen_image)

    result = csam_screening.screen_video_bytes(b"video", "video/mp4", {})
    assert result.verdict == "pending"
    assert csam_screening.extract_pending_img_ids(result) == ["a", "b"]


def test_screen_video_bytes_frame_extraction_failure_is_error(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")

    def _boom(video_bytes):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(csam_screening, "_extract_video_frames", _boom)
    result = csam_screening.screen_video_bytes(b"video", "video/mp4", {})
    assert result.verdict == "error"


# ---------------------------------------------------------------------------
# reconcile_pending_ticket (sweep's one-shot re-poll)
# ---------------------------------------------------------------------------


def test_reconcile_pending_ticket_flagged(monkeypatch):
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", "k")
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("flagged",))
    )
    assert csam_screening.reconcile_pending_ticket(["img-1"]) == "known_match"


def test_reconcile_pending_ticket_clear(monkeypatch):
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", "k")
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("approved",))
    )
    assert csam_screening.reconcile_pending_ticket(["img-1"]) == "clear"


def test_reconcile_pending_ticket_still_pending(monkeypatch):
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", "k")
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("pending",))
    )
    assert csam_screening.reconcile_pending_ticket(["img-1"]) == "pending"


def test_reconcile_pending_ticket_no_ids_is_pending():
    assert csam_screening.reconcile_pending_ticket([]) == "pending"


# ---------------------------------------------------------------------------
# handle_match: quarantine + incident + admin alert
# ---------------------------------------------------------------------------


def _match_result():
    return csam_screening.ScreenResult(
        verdict="known_match", provider="webpurify", provider_ref={"imgId": "img-1"}
    )


def test_handle_match_quarantines_and_records_incident():
    db = FakeFirestoreDb()
    storage = FakeCsamStorageClient()
    alerts = []

    out = csam_screening.handle_match(
        db, storage, b"the-bytes", "image/jpeg", _match_result(),
        {"uid": "u1", "source": "mint"},
        send_admin_alert_fn=lambda *a: alerts.append(a),
    )

    assert out["quarantined"] is True
    bucket = storage.buckets[csam_screening.CSAM_QUARANTINE_BUCKET]
    media_path = f"incidents/{out['incidentId']}/media"
    manifest_path = f"incidents/{out['incidentId']}/manifest.json"
    assert bucket.objects[media_path]["data"] == b"the-bytes"

    import hashlib
    import json as json_module

    manifest = json_module.loads(bucket.objects[manifest_path]["data"])
    assert manifest["sha256"] == hashlib.sha256(b"the-bytes").hexdigest()
    assert manifest["uid"] == "u1"
    assert manifest["verdict"] == "known_match"

    incident = db.collection("csamIncidents").document(out["incidentId"]).get().to_dict()
    assert incident["status"] == "detected"
    assert incident["uid"] == "u1"
    assert incident["source"] == "mint"
    assert incident["quarantinePath"] == media_path

    assert len(alerts) == 1
    assert alerts[0][0] == out["incidentId"]


def test_handle_match_missing_storage_client_logs_but_still_records_incident():
    db = FakeFirestoreDb()

    out = csam_screening.handle_match(
        db, None, b"bytes", "image/jpeg", _match_result(), {"uid": "u1", "source": "mint"},
    )

    assert out["quarantined"] is False
    incident = db.collection("csamIncidents").document(out["incidentId"]).get().to_dict()
    assert incident["quarantinePath"] is None
    assert incident["status"] == "detected"


def test_handle_match_missing_db_does_not_raise():
    storage = FakeCsamStorageClient()
    # Must not raise even with no Firestore client -- the caller still needs
    # to fail the upload regardless of what this returns.
    out = csam_screening.handle_match(
        None, storage, b"bytes", "image/jpeg", _match_result(), {"uid": "u1"},
    )
    assert "incidentId" in out


def test_handle_match_admin_alert_failure_does_not_raise():
    db = FakeFirestoreDb()
    storage = FakeCsamStorageClient()

    def _boom(*a):
        raise RuntimeError("fcm down")

    out = csam_screening.handle_match(
        db, storage, b"bytes", "image/jpeg", _match_result(), {"uid": "u1"},
        send_admin_alert_fn=_boom,
    )
    assert "incidentId" in out


# ---------------------------------------------------------------------------
# Pending tickets: write + linkage attach
# ---------------------------------------------------------------------------


def test_write_pending_ticket_and_attach_linkage():
    db = FakeFirestoreDb()
    result = csam_screening.ScreenResult(
        verdict="pending", provider="webpurify", provider_ref={"imgId": "img-1"}
    )
    ticket_id = csam_screening.write_pending_ticket(
        db, result, {"uid": "u1", "source": "mint", "itemIndex": 0}
    )

    assert ticket_id is not None
    doc = db.collection("csamScreenTickets").document(ticket_id).get().to_dict()
    assert doc["status"] == "pending"
    assert doc["imgIds"] == ["img-1"]
    assert doc["uid"] == "u1"
    assert doc["itemIndex"] == 0

    csam_screening.attach_ticket_linkage(db, ticket_id, tokenId="42")
    doc2 = db.collection("csamScreenTickets").document(ticket_id).get().to_dict()
    assert doc2["tokenId"] == "42"
    assert doc2["status"] == "pending"  # untouched by the linkage patch


def test_write_pending_ticket_no_db_returns_none():
    result = csam_screening.ScreenResult(verdict="pending", provider="webpurify")
    assert csam_screening.write_pending_ticket(None, result, {}) is None


def test_attach_ticket_linkage_noop_without_ticket_id():
    db = FakeFirestoreDb()
    csam_screening.attach_ticket_linkage(db, None, tokenId="1")  # must not raise


# ---------------------------------------------------------------------------
# Sweep: reconcile pending tickets
# ---------------------------------------------------------------------------


def test_sweep_reconcile_flagged_ticket_runs_match_path_then_takedown(monkeypatch):
    db = FakeFirestoreDb()
    db.collection("csamScreenTickets").seed(
        "t1", {"status": "pending", "imgIds": ["img-1"], "uid": "u1", "source": "mint", "tokenId": "7"}
    )
    monkeypatch.setattr(csam_screening, "reconcile_pending_ticket", lambda ids: "known_match")

    storage = FakeCsamStorageClient()
    block_calls = []
    alert_calls = []

    result = csam_screening.sweep_reconcile_pending_tickets(
        db, storage,
        fetch_media_fn=lambda data: (b"media", "image/jpeg"),
        block_fn=lambda data: block_calls.append(data),
        send_admin_alert_fn=lambda *a: alert_calls.append(a),
    )

    assert result == {"checked": 1, "flagged": 1, "cleared": 0}
    ticket = db.collection("csamScreenTickets").document("t1").get().to_dict()
    assert ticket["status"] == "flagged"
    assert "incidentId" in ticket
    assert len(block_calls) == 1
    assert block_calls[0]["tokenId"] == "7"
    assert len(alert_calls) == 1
    # A quarantine copy was made from the fetched media.
    bucket = storage.buckets[csam_screening.CSAM_QUARANTINE_BUCKET]
    assert bucket.objects[f"incidents/{ticket['incidentId']}/media"]["data"] == b"media"


def test_sweep_reconcile_clear_ticket_marked_resolved(monkeypatch):
    db = FakeFirestoreDb()
    db.collection("csamScreenTickets").seed(
        "t1", {"status": "pending", "imgIds": ["img-1"]}
    )
    monkeypatch.setattr(csam_screening, "reconcile_pending_ticket", lambda ids: "clear")

    result = csam_screening.sweep_reconcile_pending_tickets(db, FakeCsamStorageClient())

    assert result == {"checked": 1, "flagged": 0, "cleared": 1}
    ticket = db.collection("csamScreenTickets").document("t1").get().to_dict()
    assert ticket["status"] == "resolved_clear"


def test_sweep_reconcile_still_pending_ticket_untouched(monkeypatch):
    db = FakeFirestoreDb()
    db.collection("csamScreenTickets").seed(
        "t1", {"status": "pending", "imgIds": ["img-1"]}
    )
    monkeypatch.setattr(csam_screening, "reconcile_pending_ticket", lambda ids: "pending")

    result = csam_screening.sweep_reconcile_pending_tickets(db, FakeCsamStorageClient())

    assert result == {"checked": 1, "flagged": 0, "cleared": 0}
    ticket = db.collection("csamScreenTickets").document("t1").get().to_dict()
    assert ticket["status"] == "pending"


def test_sweep_reconcile_no_db_is_noop():
    result = csam_screening.sweep_reconcile_pending_tickets(None, FakeCsamStorageClient())
    assert result == {"checked": 0, "flagged": 0, "cleared": 0}


# ---------------------------------------------------------------------------
# Sweep: purge expired incidents (retention + legal-hold gate)
# ---------------------------------------------------------------------------


from datetime import datetime, timedelta, timezone  # noqa: E402


def test_sweep_purge_deletes_reported_incidents_past_retention():
    db = FakeFirestoreDb()
    old = datetime.now(timezone.utc) - timedelta(days=csam_screening.CSAM_RETENTION_DAYS + 1)
    db.collection("csamIncidents").seed("i1", {"status": "reported", "createdAt": old})
    storage = FakeCsamStorageClient()
    bucket = storage.bucket(csam_screening.CSAM_QUARANTINE_BUCKET)
    bucket.blob("incidents/i1/media").upload_from_string(b"x")
    bucket.blob("incidents/i1/manifest.json").upload_from_string(b"{}")

    purged = csam_screening.sweep_purge_expired_incidents(db, storage)

    assert purged == 1
    assert db.collection("csamIncidents").document("i1").get().to_dict()["status"] == "purged"
    assert "incidents/i1/media" not in bucket.objects


def test_sweep_purge_never_touches_unreported_incidents_regardless_of_age():
    """Legal hold: an incident not yet reported to NCMEC must NEVER be
    purged, no matter how old -- 18 U.S.C. 2258A preservation duty."""
    db = FakeFirestoreDb()
    very_old = datetime.now(timezone.utc) - timedelta(days=10_000)
    db.collection("csamIncidents").seed("i1", {"status": "detected", "createdAt": very_old})

    purged = csam_screening.sweep_purge_expired_incidents(db, FakeCsamStorageClient())

    assert purged == 0
    assert db.collection("csamIncidents").document("i1").get().to_dict()["status"] == "detected"


def test_sweep_purge_leaves_reported_but_recent_incidents_alone():
    db = FakeFirestoreDb()
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    db.collection("csamIncidents").seed("i1", {"status": "reported", "createdAt": recent})

    purged = csam_screening.sweep_purge_expired_incidents(db, FakeCsamStorageClient())

    assert purged == 0
    assert db.collection("csamIncidents").document("i1").get().to_dict()["status"] == "reported"


# ===========================================================================
# server.py hook-point tests
# ===========================================================================


@pytest.fixture
def csam_disabled(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "disabled")


@pytest.fixture
def csam_server_webpurify(monkeypatch):
    """Wire CSAM_SCREEN_PROVIDER=webpurify for server.py-level tests, with
    server.gcs_bucket/gcs_client swapped for fakes and signing stubbed."""
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(csam_screening, "WEBPURIFY_API_KEY", "test-key")
    monkeypatch.setattr(csam_screening, "CSAM_POLL_SECONDS", 0)
    gcs_bucket = FakeCsamBucket()
    gcs_client = FakeCsamStorageClient()
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)
    monkeypatch.setattr(server, "gcs_client", gcs_client)
    monkeypatch.setattr(server, "generate_signed_url", lambda blob, method, expiration, headers=None: _fake_sign_url(blob, method, expiration))
    return gcs_bucket, gcs_client


# --- disabled provider: mint pipeline hook is a true no-op -----------------


def test_mint_pipeline_hook_disabled_never_touches_network(monkeypatch, csam_disabled):
    def _boom(*a, **kw):
        raise AssertionError("must not make a network call when disabled")

    monkeypatch.setattr(csam_screening.requests, "get", _boom)
    monkeypatch.setattr(server, "gcs_bucket", None)  # must not even be touched

    tickets = server._csam_screen_mint_item({"image": base64.b64encode(b"img").decode(), "mediaType": "photo"}, 0, "u1")

    assert tickets == []


# --- webpurify: clear -> proceeds (no raise, no ticket) --------------------


def test_mint_item_clear_proceeds(monkeypatch, csam_server_webpurify):
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("approved",))
    )
    tickets = server._csam_screen_mint_item(
        {"image": base64.b64encode(b"img-bytes").decode(), "mediaType": "photo"}, 0, "u1"
    )
    assert tickets == []


# --- webpurify: flagged -> MintError, generic message, no leak -------------


def test_mint_item_flagged_raises_generic_mint_error(monkeypatch, csam_server_webpurify):
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("flagged",))
    )
    db = FakeFirestoreDb()
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", "")

    with pytest.raises(server.MintError) as exc_info:
        server._csam_screen_mint_item(
            {"image": base64.b64encode(b"img-bytes").decode(), "mediaType": "photo"}, 0, "u1"
        )

    assert str(exc_info.value) == server.CSAM_GENERIC_UPLOAD_ERROR
    # No CSAM-specific detail (verdict, provider, imgId...) leaks into the
    # client-visible error string.
    for leaky in ("webpurify", "known_match", "flagged", "img-123", "csam"):
        assert leaky not in str(exc_info.value).lower()

    # The incident WAS recorded even though the error string is generic.
    incidents = list(db.collection("csamIncidents").stream())
    assert len(incidents) == 1
    assert incidents[0].to_dict()["verdict"] == "known_match"


def test_run_mint_pipeline_match_aborts_before_any_compression_or_mint(monkeypatch, csam_server_webpurify):
    """The GENERIC MintError from a match must reach the caller (e.g.
    /mint/async, /mint/process) completely untouched, and nothing
    downstream of screening (compression, IPFS pin, on-chain mint) may run."""
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("flagged",))
    )
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb())
    monkeypatch.setenv("MODERATION_ADMIN_EMAILS", "")

    def _must_not_run(*a, **kw):
        raise AssertionError("downstream mint step ran despite a CSAM match")

    monkeypatch.setattr(server, "process_mint_media_item", _must_not_run)
    monkeypatch.setattr(server, "pin_metadata_to_ipfs", _must_not_run)
    monkeypatch.setattr(server, "send_contract_transaction", _must_not_run)

    items = [{"image": base64.b64encode(b"img-bytes").decode(), "mediaType": "photo"}]

    with pytest.raises(server.MintError) as exc_info:
        server.run_mint_pipeline(items, "u1", None, False, False)

    assert str(exc_info.value) == server.CSAM_GENERIC_UPLOAD_ERROR


# --- webpurify: pending -> publishes + ticket, tokenId attached after mint -


def test_run_mint_pipeline_pending_publishes_and_tickets_get_token_id(monkeypatch, csam_server_webpurify):
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("pending",))
    )
    db = FakeFirestoreDb()
    monkeypatch.setattr(server, "firestore_db", db)

    monkeypatch.setattr(
        server, "process_mint_media_item",
        lambda item, media_type, is_private: {
            "media_type": "photo",
            "raw_bytes": b"raw",
            "hq_bytes": b"hq",
            "ipfs_url": "ipfs://cid123",
            "ipfs_key": "key",
            "analysis": {"flagged": False, "tags": [], "reason": "", "description": ""},
            "duration_seconds": None,
        },
    )
    monkeypatch.setattr(
        server, "pin_metadata_to_ipfs",
        lambda *a, **kw: ("ipfs://metadata123", "metaKey"),
    )
    monkeypatch.setattr(server, "contract", FakeContract())
    monkeypatch.setattr(
        server, "send_contract_transaction", fake_send_contract_transaction(receipt={})
    )
    monkeypatch.setattr(server, "extract_virtual_mint_token_id", lambda receipt, contract: 99)
    monkeypatch.setattr(server, "upload_to_firebase_storage", lambda *a, **kw: None)

    items = [{"image": base64.b64encode(b"img-bytes").decode(), "mediaType": "photo"}]
    response = server.run_mint_pipeline(items, "u1", None, False, False)

    assert response["token_id"] == 99

    tickets = list(db.collection("csamScreenTickets").stream())
    assert len(tickets) == 1
    ticket_data = tickets[0].to_dict()
    assert ticket_data["status"] == "pending"
    assert ticket_data["tokenId"] == "99"  # attached after mint succeeded
    assert ticket_data["source"] == "mint"


# --- provider error: fail-open vs fail-closed -------------------------------


def test_mint_item_provider_error_fails_open_by_default(monkeypatch, csam_server_webpurify):
    def _raise(*a, **kw):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(csam_screening.requests, "get", _raise)
    monkeypatch.setattr(csam_screening, "CSAM_FAIL_MODE", "open")

    tickets = server._csam_screen_mint_item(
        {"image": base64.b64encode(b"img-bytes").decode(), "mediaType": "photo"}, 0, "u1"
    )
    assert tickets == []  # proceeded despite the error


def test_mint_item_provider_error_fails_closed_when_configured(monkeypatch, csam_server_webpurify):
    def _raise(*a, **kw):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(csam_screening.requests, "get", _raise)
    monkeypatch.setattr(csam_screening, "CSAM_FAIL_MODE", "closed")

    with pytest.raises(server.MintError) as exc_info:
        server._csam_screen_mint_item(
            {"image": base64.b64encode(b"img-bytes").decode(), "mediaType": "photo"}, 0, "u1"
        )
    assert str(exc_info.value) == server.CSAM_GENERIC_UPLOAD_ERROR


# --- story/avatar hook on match: generic error, nothing written to GCS -----


def test_story_upload_match_returns_generic_error_and_writes_nothing(monkeypatch):
    install_auth(monkeypatch, server, "u1")
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(
        server.csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(verdict="known_match", provider="webpurify"),
    )
    incidents_written = []
    monkeypatch.setattr(
        server.csam_screening, "handle_match",
        lambda *a, **kw: incidents_written.append(1) or {"incidentId": "i1", "quarantined": True},
    )
    gcs_bucket = FakeCsamBucket()
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)

    client = server.app.test_client()
    resp = client.post(
        "/story-upload",
        json={
            "image": base64.b64encode(b"photo-bytes").decode(),
            "userId": "u1",
            "mediaType": "photo",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "Upload could not be processed."
    assert "csam" not in body["error"].lower()
    assert gcs_bucket.objects == {}  # nothing persisted
    assert len(incidents_written) == 1


def test_avatar_upload_match_returns_generic_error_and_writes_nothing(monkeypatch):
    install_auth(monkeypatch, server, "u1")
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(
        server.csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(verdict="known_match", provider="webpurify"),
    )
    monkeypatch.setattr(
        server.csam_screening, "handle_match",
        lambda *a, **kw: {"incidentId": "i1", "quarantined": True},
    )
    gcs_bucket = FakeCsamBucket()
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)

    client = server.app.test_client()
    resp = client.post(
        "/avatar-upload",
        json={"image": base64.b64encode(b"photo-bytes").decode(), "shape": "circle"},
        headers=auth_headers(),
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Upload could not be processed."
    assert gcs_bucket.objects == {}


def test_avatar_upload_clear_proceeds_as_before(monkeypatch):
    """Sanity check: with screening wired but resolving clear, the existing
    avatar-upload behavior is unaffected."""
    install_auth(monkeypatch, server, "u1")
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(
        server.csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(verdict="clear", provider="webpurify"),
    )
    gcs_bucket = FakeCsamBucket()
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)

    client = server.app.test_client()
    resp = client.post(
        "/avatar-upload",
        json={"image": base64.b64encode(b"photo-bytes").decode(), "shape": "circle"},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    assert "mediaPath" in resp.get_json()
    assert len(gcs_bucket.objects) == 1


def test_story_upload_gcs_variant_match_deletes_already_uploaded_blob(monkeypatch):
    """The GCS variant receives bytes the client already uploaded directly --
    on a match, that blob must be cleaned up (in addition to the separate
    quarantine copy handle_match makes)."""
    install_auth(monkeypatch, server, "u1")
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    gcs_bucket = FakeCsamBucket()
    blob_path = server.story_object_path("u1", "story1")
    gcs_bucket.blob(blob_path).upload_from_string(b"video-bytes")
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)

    monkeypatch.setattr(
        server.csam_screening, "screen_video_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(verdict="known_match", provider="webpurify"),
    )
    monkeypatch.setattr(
        server.csam_screening, "handle_match",
        lambda *a, **kw: {"incidentId": "i1", "quarantined": True},
    )

    client = server.app.test_client()
    resp = client.post(
        "/story-upload",
        json={"userId": "u1", "storyId": "story1", "mediaType": "video", "source": "gcs"},
        headers=auth_headers(),
    )

    assert resp.status_code == 400
    assert blob_path not in gcs_bucket.objects


# --- /csam/sweep route wiring ------------------------------------------------


def test_csam_sweep_requires_auth(monkeypatch):
    monkeypatch.setattr(server, "MINT_WORKER_SECRET", "shh")
    client = server.app.test_client()
    resp = client.post("/csam/sweep")
    assert resp.status_code == 403


def test_csam_sweep_route_invokes_both_jobs(monkeypatch):
    monkeypatch.setattr(server, "MINT_WORKER_SECRET", "shh")
    monkeypatch.setattr(
        server.csam_screening, "sweep_reconcile_pending_tickets",
        lambda *a, **kw: {"checked": 2, "flagged": 1, "cleared": 1},
    )
    monkeypatch.setattr(server.csam_screening, "sweep_purge_expired_incidents", lambda *a, **kw: 3)

    client = server.app.test_client()
    resp = client.post("/csam/sweep", headers={"X-Mint-Secret": "shh"})

    assert resp.status_code == 200
    assert resp.get_json() == {"checked": 2, "flagged": 1, "cleared": 1, "purged": 3}


def test_csam_takedown_from_ticket_uses_shared_block_logic(monkeypatch):
    db = FakeFirestoreDb()
    db.collection("posts").seed("7", {"userId": "creator", "ipfsUrl": "ipfs://cidexample123"})
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(server, "contract", FakeContract())
    unpin_calls = []
    monkeypatch.setattr(server, "unpin_post_media", lambda data: unpin_calls.append(data))

    server._csam_takedown_from_ticket({"tokenId": "7"})

    post = db.collection("posts").document("7").get().to_dict()
    assert post["flagged"] is True
    assert post["moderationStatus"] == "rejected"
    assert len(unpin_calls) == 1


def test_csam_takedown_from_ticket_without_any_linkage_is_noop():
    # No tokenId AND no mediaPath -- must not raise, just log and skip.
    server._csam_takedown_from_ticket({"source": "avatar"})


def test_csam_takedown_from_ticket_deletes_media_path_for_non_mint_source(monkeypatch):
    """Fixes the stale claim that avatars/story/studio media has 'no
    equivalent public-content takedown' -- it IS served publicly and
    unauthenticated via GET /media/<path>, so deleting the GCS object at
    mediaPath must be (and now is) the takedown for non-mint tickets."""
    bucket = FakeCsamBucket()
    bucket.blob("avatars/u1/circle-123.jpg").upload_from_string(b"avatar-bytes")
    monkeypatch.setattr(server, "gcs_bucket", bucket)

    server._csam_takedown_from_ticket(
        {"source": "avatar", "mediaPath": "/media/avatars/u1/circle-123.jpg"}
    )

    assert "avatars/u1/circle-123.jpg" not in bucket.objects


def test_csam_fetch_ticket_media_uses_media_path_for_non_mint_tickets(monkeypatch):
    bucket = FakeCsamBucket()
    bucket.blob("studio/u1/item1.jpg").upload_from_string(b"studio-bytes", content_type="image/jpeg")
    monkeypatch.setattr(server, "gcs_bucket", bucket)
    monkeypatch.setattr(server, "firestore_db", None)

    result = server._csam_fetch_ticket_media(
        {"source": "studio", "mediaPath": "/media/studio/u1/item1.jpg"}
    )

    assert result == (b"studio-bytes", "image/jpeg")


def test_csam_fetch_ticket_media_returns_none_when_blob_missing(monkeypatch):
    bucket = FakeCsamBucket()
    monkeypatch.setattr(server, "gcs_bucket", bucket)
    assert server._csam_fetch_ticket_media({"mediaPath": "/media/missing/path.jpg"}) is None


# ---------------------------------------------------------------------------
# CRITICAL #1 regression: /stitch pending-ticket linkage (tokenId attached
# after mint, same as run_mint_pipeline).
# ---------------------------------------------------------------------------


def test_stitch_pending_ticket_gets_token_id_attached_after_mint(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    db = FakeFirestoreDb()
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(server, "verify_firebase_token", lambda req: "u1")

    gcs_bucket = FakeCsamBucket()
    for i in range(2):
        gcs_bucket.blob(f"stitch_uploads/st-pending/{i}.mp4").upload_from_string(b"clip")
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)

    def fake_run(cmd, **kwargs):
        # ffmpeg writes its output to the last positional arg; create it so
        # the subsequent `open(output_path, "rb")` read succeeds.
        with open(cmd[-1], "wb") as f:
            f.write(b"STITCHED")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    monkeypatch.setattr(
        server.csam_screening, "screen_video_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(
            verdict="pending", provider="webpurify", provider_ref={"imgId": "img-pending-1"}
        ),
    )
    monkeypatch.setattr(server, "pin_file_bytes_to_ipfs", lambda *a, **kw: "ipfs://stitched123")
    monkeypatch.setattr(server, "pin_metadata_to_ipfs", lambda *a, **kw: "ipfs://metadata123")
    monkeypatch.setattr(server, "contract", FakeContract())
    monkeypatch.setattr(
        server, "send_contract_transaction", fake_send_contract_transaction(receipt={})
    )
    monkeypatch.setattr(server, "extract_virtual_mint_token_id", lambda receipt, contract: 555)
    monkeypatch.setattr(server, "upload_to_firebase_storage", lambda *a, **kw: None)
    monkeypatch.setattr(server, "save_post_server", lambda *a, **kw: None)

    resp = server.app.test_client().post(
        "/stitch",
        json={"stitchId": "st-pending", "clipCount": 2, "userId": "u1"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["token_id"] == 555

    tickets = list(db.collection("csamScreenTickets").stream())
    assert len(tickets) == 1
    ticket = tickets[0].to_dict()
    assert ticket["status"] == "pending"
    assert ticket["tokenId"] == "555"  # attached post-mint -- the actual fix
    assert ticket["source"] == "stitch"
    assert ticket["stitchId"] == "st-pending"


# ---------------------------------------------------------------------------
# CRITICAL #2 regression: non-mint tickets carry a mediaPath (up front when
# known, attached post-hoc otherwise), and the sweep uses it to quarantine
# REAL bytes (never a fabricated empty-bytes record) and delete the object.
# ---------------------------------------------------------------------------


def test_story_upload_pending_ticket_has_media_path_up_front(monkeypatch):
    install_auth(monkeypatch, server, "u1")
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    db = FakeFirestoreDb()
    monkeypatch.setattr(server, "firestore_db", db)
    gcs_bucket = FakeCsamBucket()
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)
    monkeypatch.setattr(
        server.csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(
            verdict="pending", provider="webpurify", provider_ref={"imgId": "img-1"}
        ),
    )
    monkeypatch.setattr(server, "analyze_image", lambda *a, **kw: {"flagged": False, "reason": ""})

    resp = server.app.test_client().post(
        "/story-upload",
        json={
            "image": base64.b64encode(b"photo-bytes").decode(),
            "userId": "u1",
            "mediaType": "photo",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    story_id = resp.get_json()["storyId"]

    tickets = list(db.collection("csamScreenTickets").stream())
    assert len(tickets) == 1
    ticket = tickets[0].to_dict()
    assert ticket["mediaPath"] == f"/media/stories/u1/{story_id}.jpg"
    assert ticket["source"] == "story"


def test_avatar_upload_pending_ticket_gets_media_path_attached_after_upload(monkeypatch):
    install_auth(monkeypatch, server, "u1")
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    db = FakeFirestoreDb()
    monkeypatch.setattr(server, "firestore_db", db)
    gcs_bucket = FakeCsamBucket()
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)
    monkeypatch.setattr(
        server.csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(
            verdict="pending", provider="webpurify", provider_ref={"imgId": "img-1"}
        ),
    )

    resp = server.app.test_client().post(
        "/avatar-upload",
        json={"image": base64.b64encode(b"photo-bytes").decode(), "shape": "circle"},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    media_path = resp.get_json()["mediaPath"]

    tickets = list(db.collection("csamScreenTickets").stream())
    assert len(tickets) == 1
    ticket = tickets[0].to_dict()
    # Not known at ticket-creation time (the filename has a write-time
    # timestamp) -- attach_ticket_linkage patches it in after the upload.
    assert ticket["mediaPath"] == media_path
    assert ticket["source"] == "avatar"


def test_studio_upload_pending_ticket_has_media_path_up_front(monkeypatch):
    db = FakeFirestoreDb()
    monkeypatch.setattr(server, "firestore_db", db)
    install_auth(monkeypatch, server, "u1")
    gcs_bucket = FakeCsamBucket()
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(
        server.csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(
            verdict="pending", provider="webpurify", provider_ref={"imgId": "img-1"}
        ),
    )

    resp = server.app.test_client().post(
        "/studio-upload",
        json={
            "media": base64.b64encode(b"photo-bytes").decode(),
            "mediaType": "photo",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    media_path = resp.get_json()["mediaPath"]

    tickets = list(db.collection("csamScreenTickets").stream())
    assert len(tickets) == 1
    ticket = tickets[0].to_dict()
    assert ticket["mediaPath"] == media_path
    assert ticket["source"] == "studio"


def test_sweep_reconcile_non_mint_ticket_quarantines_real_bytes_and_deletes_media(monkeypatch):
    """The core CRITICAL #2 regression: a story/avatar/studio 'pending'
    ticket (no tokenId) must be locatable via mediaPath at reconcile time,
    with REAL bytes quarantined (never a fabricated empty-bytes record) and
    the public GCS object actually deleted."""
    db = FakeFirestoreDb()
    db.collection("csamScreenTickets").seed("t1", {
        "status": "pending", "imgIds": ["img-1"], "uid": "u1",
        "source": "avatar", "mediaPath": "/media/avatars/u1/circle-99.jpg",
    })
    gcs_bucket = FakeCsamBucket()
    gcs_bucket.blob("avatars/u1/circle-99.jpg").upload_from_string(
        b"the-real-avatar-bytes", content_type="image/jpeg"
    )
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(csam_screening, "reconcile_pending_ticket", lambda ids: "known_match")

    storage = FakeCsamStorageClient()
    alert_calls = []

    result = csam_screening.sweep_reconcile_pending_tickets(
        db, storage,
        fetch_media_fn=server._csam_fetch_ticket_media,
        block_fn=server._csam_takedown_from_ticket,
        send_admin_alert_fn=lambda *a: alert_calls.append(a),
    )

    assert result == {"checked": 1, "flagged": 1, "cleared": 0}
    ticket = db.collection("csamScreenTickets").document("t1").get().to_dict()
    incident = db.collection("csamIncidents").document(ticket["incidentId"]).get().to_dict()

    assert incident["quarantinePath"] is not None
    assert incident["mediaUnavailable"] is False

    quarantine_bucket = storage.buckets[csam_screening.CSAM_QUARANTINE_BUCKET]
    assert quarantine_bucket.objects[incident["quarantinePath"]]["data"] == b"the-real-avatar-bytes"

    # The actual takedown for non-mint media: the public GCS object is gone.
    assert "avatars/u1/circle-99.jpg" not in gcs_bucket.objects
    assert len(alert_calls) == 1


def test_sweep_reconcile_media_unavailable_does_not_fabricate_quarantine(monkeypatch):
    """When the published media can no longer be located, the sweep must
    record quarantined=False + mediaUnavailable=True -- never an empty-bytes
    quarantine object that falsely claims evidence was preserved."""
    db = FakeFirestoreDb()
    db.collection("csamScreenTickets").seed("t1", {
        "status": "pending", "imgIds": ["img-1"], "uid": "u1", "source": "story",
        "mediaPath": "/media/stories/u1/missing.mp4",
    })
    gcs_bucket = FakeCsamBucket()  # the blob was never uploaded -- fetch fails
    monkeypatch.setattr(server, "gcs_bucket", gcs_bucket)
    monkeypatch.setattr(server, "firestore_db", db)
    monkeypatch.setattr(csam_screening, "reconcile_pending_ticket", lambda ids: "known_match")

    storage = FakeCsamStorageClient()
    result = csam_screening.sweep_reconcile_pending_tickets(
        db, storage,
        fetch_media_fn=server._csam_fetch_ticket_media,
        block_fn=server._csam_takedown_from_ticket,
        send_admin_alert_fn=lambda *a: None,
    )

    assert result["flagged"] == 1
    ticket = db.collection("csamScreenTickets").document("t1").get().to_dict()
    incident = db.collection("csamIncidents").document(ticket["incidentId"]).get().to_dict()

    assert incident["quarantinePath"] is None
    assert incident["mediaUnavailable"] is True
    # No quarantine bucket write happened at all -- not even an empty-bytes one.
    assert storage.buckets.get(csam_screening.CSAM_QUARANTINE_BUCKET) is None


def test_handle_match_media_unavailable_does_not_fabricate_quarantine():
    """Direct unit test of the handle_match guard (module-level, no sweep
    plumbing involved): media_bytes=None must never produce a fabricated
    empty-bytes quarantine object."""
    db = FakeFirestoreDb()
    storage = FakeCsamStorageClient()

    out = csam_screening.handle_match(
        db, storage, None, None, _match_result(), {"uid": "u1", "source": "story"},
    )

    assert out["quarantined"] is False
    incident = db.collection("csamIncidents").document(out["incidentId"]).get().to_dict()
    assert incident["quarantinePath"] is None
    assert incident["mediaUnavailable"] is True
    assert csam_screening.CSAM_QUARANTINE_BUCKET not in storage.buckets


# ---------------------------------------------------------------------------
# HIGH #3 regression: disabled mode does zero decode work.
# ---------------------------------------------------------------------------


def test_mint_item_disabled_never_decodes_base64(monkeypatch, csam_disabled):
    def _boom(*a, **kw):
        raise AssertionError("must not decode base64 when disabled")

    monkeypatch.setattr(base64, "b64decode", _boom)

    tickets = server._csam_screen_mint_item(
        {"image": "aW52YWxpZA==", "mediaType": "photo"}, 0, "u1"
    )
    assert tickets == []


def test_csam_screen_or_reject_disabled_is_noop(monkeypatch, csam_disabled):
    def _boom(*a, **kw):
        raise AssertionError("must not screen when disabled")

    monkeypatch.setattr(server.csam_screening, "screen_image_bytes", _boom)
    monkeypatch.setattr(server.csam_screening, "screen_video_bytes", _boom)

    guard, ticket_id = server._csam_screen_or_reject(
        b"bytes", "image/jpeg", {"uid": "u1", "source": "avatar"}, "generic",
    )
    assert guard is None
    assert ticket_id is None


# ---------------------------------------------------------------------------
# HIGH #4 regression: a non-CSAM error (malformed base64) must surface as a
# clean MintError, not an unhandled exception / raw 500, and must never be
# confused with a known_match rejection.
# ---------------------------------------------------------------------------


def test_mint_item_malformed_base64_raises_clean_mint_error(monkeypatch, csam_server_webpurify):
    def _boom(*a, **kw):
        raise AssertionError("must not reach the network on a decode failure")

    monkeypatch.setattr(csam_screening.requests, "get", _boom)

    with pytest.raises(server.MintError) as exc_info:
        server._csam_screen_mint_item({"image": "abc", "mediaType": "photo"}, 0, "u1")

    assert str(exc_info.value).startswith("Error uploading item 0 to IPFS")
    # Never mistaken for (or worded like) a CSAM rejection.
    assert exc_info.value.args[0] != server.CSAM_GENERIC_UPLOAD_ERROR


def test_mint_malformed_base64_is_clean_mint_error_not_raw_500(monkeypatch, csam_server_webpurify):
    def _boom(*a, **kw):
        raise AssertionError("must not reach the network on a decode failure")

    monkeypatch.setattr(csam_screening.requests, "get", _boom)

    resp = server.app.test_client().post("/mint", json={
        "image": "abc",
        "walletAddress": "0x0000000000000000000000000000000000000001",
        "mediaType": "photo",
    })

    assert resp.status_code == 500
    body = resp.get_json()
    assert body is not None
    assert "error" in body
    assert "Error uploading item 0 to IPFS" in body["error"]


# ---------------------------------------------------------------------------
# WARNING #5 regression: both the known_match and fail-closed-error branches
# of _csam_screen_or_reject honor the caller's generic_status.
# ---------------------------------------------------------------------------


def test_csam_screen_or_reject_known_match_honors_generic_status(monkeypatch, csam_server_webpurify):
    monkeypatch.setattr(
        server.csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(verdict="known_match", provider="webpurify"),
    )
    monkeypatch.setattr(server.csam_screening, "handle_match", lambda *a, **kw: {"incidentId": "i1", "quarantined": True})

    with server.app.app_context():
        guard, ticket_id = server._csam_screen_or_reject(
            b"bytes", "image/jpeg", {"uid": "u1", "source": "avatar"}, "nope", generic_status=418,
        )
        assert guard is not None
        response, status = guard
        assert status == 418
        assert response.get_json()["error"] == "nope"
    assert ticket_id is None


def test_csam_screen_or_reject_fail_closed_honors_generic_status(monkeypatch):
    monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER", "webpurify")
    monkeypatch.setattr(csam_screening, "CSAM_FAIL_MODE", "closed")
    monkeypatch.setattr(
        server.csam_screening, "screen_image_bytes",
        lambda *a, **kw: csam_screening.ScreenResult(verdict="error", provider="webpurify"),
    )

    with server.app.app_context():
        guard, ticket_id = server._csam_screen_or_reject(
            b"bytes", "image/jpeg", {"uid": "u1", "source": "avatar"},
            "Upload could not be processed.", generic_status=418,
        )

        assert guard is not None
        _response, status = guard
        assert status == 418
    assert ticket_id is None


# ---------------------------------------------------------------------------
# WARNING #6 regression: an existing GCS object can be signed directly for
# webpurify screening instead of always copying to a new temp object.
# ---------------------------------------------------------------------------


def test_screen_image_existing_blob_path_skips_temp_upload_and_is_not_deleted(monkeypatch, webpurify_enabled):
    monkeypatch.setattr(
        csam_screening.requests, "get", make_webpurify_get(status_sequence=("approved",))
    )
    bucket = FakeCsamBucket()
    bucket.blob("mint_uploads/job1/0.mp4").upload_from_string(b"real-bytes", content_type="video/mp4")

    result = csam_screening.screen_image_bytes(
        b"real-bytes", "video/mp4", {},
        gcs_bucket=bucket, sign_url_fn=_fake_sign_url,
        existing_blob_path="mint_uploads/job1/0.mp4",
    )

    assert result.verdict == "clear"
    # No new csam-screen-tmp/ object was created...
    assert all(not p.startswith("csam-screen-tmp/") for p in bucket.objects)
    # ...and the existing object was signed directly and left untouched.
    assert "mint_uploads/job1/0.mp4" in bucket.objects
    assert bucket.deleted == []


def test_screen_image_existing_blob_path_sign_failure_is_error(webpurify_enabled):
    bucket = FakeCsamBucket()

    def _boom_sign(blob, method, expiration):
        raise RuntimeError("sign failed")

    result = csam_screening.screen_image_bytes(
        b"bytes", "image/jpeg", {}, gcs_bucket=bucket, sign_url_fn=_boom_sign,
        existing_blob_path="some/path.jpg",
    )

    assert result.verdict == "error"
    assert bucket.deleted == []
