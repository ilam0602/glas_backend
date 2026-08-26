#!/usr/bin/env python3
"""Wipe ALL app data for a clean slate: every Firestore collection (recursively,
including subcollections) AND every Firebase Auth user.

This is a FULL destructive reset — unlike delete_demo_data.py, which only removes
demo-tagged docs. There is no undo.

Usage:
    python wipe_all_data.py            # DRY RUN — lists what would be deleted, deletes nothing
    python wipe_all_data.py --yes      # actually delete everything
    python wipe_all_data.py --yes --keep-auth   # wipe Firestore only, leave Auth users

Requires FIREBASE_SERVICE_ACCOUNT_JSON in authensnap_server/.env.
"""
import os
import sys
import json
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore, auth as fb_auth

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

DRY_RUN = "--yes" not in sys.argv
KEEP_AUTH = "--keep-auth" in sys.argv

if not firebase_admin._apps:
    sa = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not sa:
        sys.exit("ERROR: FIREBASE_SERVICE_ACCOUNT_JSON not set in .env")
    cred = credentials.Certificate(json.loads(sa))
    firebase_admin.initialize_app(cred)

db = fb_firestore.client()

sa_info = json.loads(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON"))
project_id = sa_info.get("project_id", "<unknown>")

mode = "DRY RUN (nothing will be deleted)" if DRY_RUN else "LIVE — DELETING"
print(f"=== WIPE ALL DATA ===")
print(f"Firebase project: {project_id}")
print(f"Mode: {mode}")
print(f"Auth users: {'KEPT' if KEEP_AUTH else ('would be deleted' if DRY_RUN else 'DELETING')}")
print()


# ── Firestore ─────────────────────────────────────────────────────────────────
def count_recursive(doc_ref):
    """Count a doc plus all descendant docs (for dry-run reporting)."""
    total = 1
    for sub in doc_ref.collections():
        for child in sub.stream():
            total += count_recursive(child.reference)
    return total


print("--- FIRESTORE COLLECTIONS ---")
total_docs = 0
for coll in db.collections():
    docs = list(coll.stream())
    # include descendants in the reported count
    coll_total = sum(count_recursive(d.reference) for d in docs)
    total_docs += coll_total
    print(f"  {coll.id}: {coll_total} docs (incl. subcollections)")
    if not DRY_RUN:
        # recursive_delete handles subcollections and batching for us
        db.recursive_delete(coll)

print(f"\nTotal Firestore docs {'to delete' if DRY_RUN else 'deleted'}: {total_docs}")


# ── Auth ──────────────────────────────────────────────────────────────────────
print("\n--- FIREBASE AUTH USERS ---")
if KEEP_AUTH:
    print("  --keep-auth set; skipping Auth deletion.")
else:
    uids = [u.uid for u in fb_auth.list_users().iterate_all()]
    print(f"  {len(uids)} auth users {'to delete' if DRY_RUN else 'deleting'}")
    if not DRY_RUN:
        deleted = 0
        for i in range(0, len(uids), 1000):  # delete_users caps at 1000/call
            batch = uids[i:i + 1000]
            result = fb_auth.delete_users(batch)
            deleted += result.success_count
            if result.failure_count:
                for err in result.errors:
                    print(f"    FAILED uid[{err.index}]: {err.reason}")
        print(f"  deleted {deleted} auth users")


# ── Verify (live only) ────────────────────────────────────────────────────────
if not DRY_RUN:
    print("\n--- VERIFY ---")
    remaining_docs = 0
    for coll in db.collections():
        remaining_docs += sum(count_recursive(d.reference) for d in coll.stream())
    remaining_auth = 0 if KEEP_AUTH is False else sum(1 for _ in fb_auth.list_users().iterate_all())
    if not KEEP_AUTH:
        remaining_auth = sum(1 for _ in fb_auth.list_users().iterate_all())
    print(f"  Firestore docs remaining: {remaining_docs}")
    print(f"  Auth users remaining: {remaining_auth}")
    print("\nDone. Clean slate.")
else:
    print("\nDRY RUN complete. Re-run with --yes to actually delete.")
