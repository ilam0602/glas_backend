from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import server
from server import sweep_stale_mint_jobs


def _ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


class FakeSnap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return self._data


class FakeTransaction:
    pass


class FakeJobRef:
    """Document ref whose transactional read returns `current` (the doc's
    status at flip time — may differ from the queried snapshot to simulate a
    job turning terminal between query and transaction)."""

    def __init__(self, current):
        self.current = current
        self.updates = []

    def get(self, transaction=None):
        return FakeSnap("_", self.current)


class FakeQuery:
    def __init__(self, snaps):
        self._snaps = snaps

    def stream(self):
        return iter(self._snaps)


class FakeCollection:
    def __init__(self, snaps, refs_by_id):
        self._snaps = snaps
        self._refs_by_id = refs_by_id

    def where(self, filter=None):
        return FakeQuery(self._snaps)

    def document(self, doc_id):
        return self._refs_by_id[doc_id]


class FakeFirestoreDb:
    def __init__(self, snaps, refs_by_id):
        self._collection = FakeCollection(snaps, refs_by_id)

    def collection(self, name):
        assert name == "mintJobs"
        return self._collection

    def transaction(self):
        return FakeTransaction()


class RecordingTxn:
    def __init__(self):
        self.updates = []


def _setup(monkeypatch, jobs, current_by_id=None):
    """jobs: {id: queried doc dict}. current_by_id overrides what the
    transactional re-read sees (defaults to the queried dict)."""
    current_by_id = current_by_id or {}
    snaps = [FakeSnap(i, d) for i, d in jobs.items()]
    refs = {i: FakeJobRef(current_by_id.get(i, d)) for i, d in jobs.items()}

    txn_updates = []

    def fake_transactional(fn):
        def wrapper(txn, **kwargs):
            txn.update = lambda ref, patch: txn_updates.append((ref, patch))
            return fn(txn, **kwargs)

        return wrapper

    cleaned = []
    monkeypatch.setattr(server, "firestore_db", FakeFirestoreDb(snaps, refs))
    monkeypatch.setattr(server, "fb_firestore", SimpleNamespace(transactional=fake_transactional))
    monkeypatch.setattr(server, "_cleanup_mint_upload", cleaned.append)
    return refs, txn_updates, cleaned


def test_stale_processing_job_is_failed_and_cleaned(monkeypatch):
    jobs = {"j1": {"status": "processing", "startedAt": _ago(30)}}
    refs, updates, cleaned = _setup(monkeypatch, jobs)

    assert sweep_stale_mint_jobs() == 1
    (ref, patch), = updates
    assert ref is refs["j1"]
    assert patch["status"] == "failed"
    assert patch["error"]
    assert cleaned == ["j1"]


def test_stale_queued_job_ages_from_created_at(monkeypatch):
    jobs = {"j1": {"status": "queued", "createdAt": _ago(30)}}
    _, updates, cleaned = _setup(monkeypatch, jobs)

    assert sweep_stale_mint_jobs() == 1
    assert updates[0][1]["status"] == "failed"
    assert cleaned == ["j1"]


def test_fresh_job_is_left_alone(monkeypatch):
    jobs = {"j1": {"status": "processing", "startedAt": _ago(2)}}
    _, updates, cleaned = _setup(monkeypatch, jobs)

    assert sweep_stale_mint_jobs() == 0
    assert updates == []
    assert cleaned == []


def test_job_that_turned_done_mid_sweep_is_not_flipped(monkeypatch):
    jobs = {"j1": {"status": "processing", "startedAt": _ago(30)}}
    _, updates, cleaned = _setup(
        monkeypatch, jobs, current_by_id={"j1": {"status": "done", "tokenId": 7}}
    )

    assert sweep_stale_mint_jobs() == 0
    assert updates == []
    assert cleaned == []


def test_no_firestore_is_a_noop(monkeypatch):
    monkeypatch.setattr(server, "firestore_db", None)
    assert sweep_stale_mint_jobs() == 0
