"""Shared Firestore + web3-contract fakes for authensnap_server route tests.

`import server` initializes REAL Firebase Admin, REAL GCS, and REAL web3
contract objects at module load time (confirmed by running it directly - it
prints "Firebase Admin SDK initialized...", "GCS client initialized...",
"Token contract loaded at ...", etc). Every test that exercises a route MUST
monkeypatch `server.firestore_db`, `server.gcs_bucket`, `server.contract`,
`server.token_contract`, `server.glas_contract`, and
`server.send_contract_transaction` with the fakes below before calling the
Flask test client - never let a request reach the real clients.

This module defines no `test_*` functions itself, so pytest collects it with
zero test items; it exists purely as scaffolding imported by test_*.py files.
Mirrors the hand-rolled Fake* classes already used in test_report_post.py and
test_mint_sweep.py (same shapes: FakeSnapshot.to_dict()/.exists,
monkeypatch.setattr(server, "fb_firestore", ...) for the `@transactional`
decorator) rather than inventing a new mocking approach.
"""
import uuid


# ---------------------------------------------------------------------------
# Firestore fakes
# ---------------------------------------------------------------------------


def _apply_field_update(current, value):
    """Resolve a single .update() field value, honoring Firestore Increment()
    the same way a real `doc_ref.update({"count": Increment(-1)})` would."""
    try:
        from google.cloud.firestore_v1.transforms import Increment
    except ImportError:  # pragma: no cover - always available in this venv
        Increment = ()
    if isinstance(value, Increment):
        return (current or 0) + value.value
    return value


def _filter_snapshots(snapshots, field, op, value):
    """Apply one Firestore-style predicate to an already-materialized list of
    FakeSnapshot objects. Supports the three operators the routes under test
    actually use: `==`, `array_contains`, `>=`."""
    matched = []
    for snap in snapshots:
        data = snap.to_dict() or {}
        field_value = data.get(field)
        if op == "==" and field_value == value:
            matched.append(snap)
        elif (
            op == "array_contains"
            and isinstance(field_value, (list, tuple))
            and value in field_value
        ):
            matched.append(snap)
        elif op == ">=" and field_value is not None and field_value >= value:
            matched.append(snap)
    return matched


class FakeSnapshot:
    """Mimics google.cloud.firestore_v1.DocumentSnapshot."""

    def __init__(self, doc_id, data, store=None, subcollections=None):
        self.id = doc_id
        self._data = dict(data) if data is not None else None
        self.exists = data is not None
        self._store = store if store is not None else {}
        self._subcollections = subcollections

    def to_dict(self):
        return self._data

    @property
    def reference(self):
        return FakeDocRef(self._store, self.id, subcollections=self._subcollections)


class FakeDocRef:
    """Mimics a Firestore DocumentReference. `store` is the dict shared with
    every other ref/snapshot/query touching the same logical collection
    (doc_id -> data, or None once deleted), so a write through one ref is
    visible to a later .get()/.stream() through a different ref.
    `subcollections` is likewise shared (keyed by name -> FakeCollectionRef)
    across every FakeDocRef built for the same doc_id, so
    `.document(id).collection("x")` called twice returns the SAME
    subcollection both times, the way real Firestore paths do."""

    def __init__(self, store, doc_id, subcollections=None):
        self.store = store
        self.id = doc_id
        self._subcollections = subcollections if subcollections is not None else {}

    def get(self, transaction=None):
        return FakeSnapshot(
            self.id,
            self.store.get(self.id),
            store=self.store,
            subcollections=self._subcollections,
        )

    def set(self, data, merge=False):
        current = self.store.get(self.id) or {}
        resolved = {key: _apply_field_update(current.get(key), value) for key, value in data.items()}
        if merge and self.store.get(self.id) is not None:
            merged = dict(current)
            merged.update(resolved)
            self.store[self.id] = merged
        else:
            self.store[self.id] = resolved

    def update(self, data):
        current = self.store.get(self.id) or {}
        updated = dict(current)
        for key, value in data.items():
            updated[key] = _apply_field_update(current.get(key), value)
        self.store[self.id] = updated

    def delete(self):
        self.store[self.id] = None

    def collection(self, name):
        if name not in self._subcollections:
            self._subcollections[name] = FakeCollectionRef(name)
        return self._subcollections[name]

    def collections(self):
        return list(self._subcollections.values())

    @property
    def reference(self):
        return self


class FakeQuery:
    """Result of a .where(...) call. Supports chaining another .where(...)
    (re-filters the already-matched set, mirroring real Firestore query
    composition) and .stream()."""

    def __init__(self, snapshots):
        self._snapshots = snapshots

    def where(self, field, op, value):
        return FakeQuery(_filter_snapshots(self._snapshots, field, op, value))

    def stream(self):
        return list(self._snapshots)


class FakeCollectionRef:
    """Mimics a Firestore CollectionReference. Documents persist in `_store`
    for the lifetime of this collection object, so seed() -> document().get()
    -> where().stream() -> document().delete() all observe the same state."""

    def __init__(self, name=""):
        self.name = name
        self._store = {}
        self._subcollections_by_doc = {}

    def document(self, doc_id=None):
        doc_id = doc_id or uuid.uuid4().hex
        subs = self._subcollections_by_doc.setdefault(doc_id, {})
        return FakeDocRef(self._store, doc_id, subcollections=subs)

    def add(self, data):
        doc_id = uuid.uuid4().hex
        self._store[doc_id] = dict(data)
        return (None, self.document(doc_id))

    def where(self, field, op, value):
        return FakeQuery(_filter_snapshots(self._all_snapshots(), field, op, value))

    def stream(self):
        return self._all_snapshots()

    def _all_snapshots(self):
        return [
            FakeSnapshot(
                doc_id,
                data,
                store=self._store,
                subcollections=self._subcollections_by_doc.setdefault(doc_id, {}),
            )
            for doc_id, data in self._store.items()
            if data is not None
        ]

    def seed(self, doc_id, data):
        """Test helper: insert a document directly (bypassing .set())."""
        self._store[doc_id] = dict(data)
        return self.document(doc_id)


class FakeTransaction:
    """Passed into `@fb_firestore.transactional`-wrapped functions in place of
    a real Transaction. `.set`/`.update` apply immediately to the underlying
    fake doc store (no buffer/commit/rollback), which is fine for pinning
    today's happy-path and early-return branches - none of the routes under
    test rely on transaction rollback semantics."""

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, data):
        ref.update(data)


def fake_transactional(fn):
    """Drop-in replacement for `firebase_admin.firestore.transactional`: runs
    the wrapped function immediately against the given transaction object,
    with no retry/commit machinery. Same pattern already used for
    `server.fb_firestore` in test_mint_sweep.py."""

    def wrapper(transaction, *args, **kwargs):
        return fn(transaction, *args, **kwargs)

    return wrapper


class FakeFirestoreDb:
    """Mimics the subset of google.cloud.firestore_v1.Client the server
    uses: top-level .collection(name) (each name gets its own persistent
    store for the test's lifetime), .collection_group(name) (aggregates
    whatever a test explicitly registers via collection_group_seed() - empty
    by default), and .transaction()."""

    def __init__(self):
        self._collections = {}
        self._groups = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = FakeCollectionRef(name)
        return self._collections[name]

    def collection_group(self, name):
        return FakeQuery(list(self._groups.get(name, [])))

    def collection_group_seed(self, name, doc_id, data):
        """Test helper: register a document as visible to a collection_group
        query for `name`, independent of any specific parent document."""
        store = {doc_id: dict(data)}
        self._groups.setdefault(name, []).append(
            FakeSnapshot(doc_id, data, store=store)
        )

    def transaction(self):
        return FakeTransaction()


# ---------------------------------------------------------------------------
# web3 contract fakes
# ---------------------------------------------------------------------------


class FakeContractCall:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class FakeContractFunctions:
    """Mimics `contract.functions`: `.someMethod(*args)` returns an object
    whose `.call()` yields the pre-configured return value for `someMethod`
    (keyword args to the constructor), and every invocation is recorded on
    `.calls` for assertions."""

    def __init__(self, **returns):
        self._returns = returns
        self.calls = []

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return FakeContractCall(self._returns.get(name))

        return _method


class FakeContract:
    def __init__(self, **returns):
        self.functions = FakeContractFunctions(**returns)


class FakeTxHash:
    def __init__(self, value="0xfake"):
        self._value = value

    def hex(self):
        return self._value


def fake_send_contract_transaction(receipt=None, tx_hash="0xfake"):
    """Returns a drop-in replacement for server.send_contract_transaction
    that never touches a real chain. The returned callable records every
    (fn_call, gas_limit) pair it was invoked with on `.calls`."""

    def _send(fn_call, gas_limit):
        _send.calls.append((fn_call, gas_limit))
        return FakeTxHash(tx_hash), (receipt if receipt is not None else {})

    _send.calls = []
    return _send


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def auth_headers(token="valid-token"):
    return {"Authorization": f"Bearer {token}"}


def install_auth(monkeypatch, server_module, uid, email=None):
    """Monkeypatch firebase_auth.verify_id_token to succeed for any bearer
    token and return `uid`, matching the real _require_uid()/
    verify_firebase_token() contract (only the decoded uid is read)."""
    monkeypatch.setattr(
        server_module.firebase_auth,
        "verify_id_token",
        lambda token: {"uid": uid, "email": email or f"{uid}@example.com"},
    )
