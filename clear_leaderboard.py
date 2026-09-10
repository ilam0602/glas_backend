"""
Clear the referral-points leaderboard.

The leaderboard is simply `users/{uid}.points` ordered descending, and every
credit that ever moved that number has a matching row in the `pointTransactions`
ledger (written atomically in `_award_points`). This script resets the board to
a clean slate WITHOUT losing that audit trail:

  1. Copies every `pointTransactions` row into `pointTransactions_archive/<run>/rows/<id>`
  2. Deletes the live `pointTransactions` rows (so daily/one-time dedup ids are
     released and everyone can earn again from scratch)
  3. Sets `users/*.points = 0`

Referral identity (`referredBy`, `referralCodes`) is deliberately KEPT — those
are relationships, not score.

SAFE BY DEFAULT: a dry run that writes nothing and prints what it *would* do,
plus a ledger-vs-points consistency report (a cheap exploit check). Pass
`--commit` to actually perform the reset.

Usage (from authensnap_server/, with the same .env the server uses):

    python clear_leaderboard.py            # dry run + consistency report
    python clear_leaderboard.py --commit   # perform the reset

Auth: reuses FIREBASE_SERVICE_ACCOUNT_JSON from .env, exactly like server.py,
so it targets the same Firestore project. Double-check which project that env
points at before running with --commit.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

# Firestore commits are capped at 500 writes per batch.
BATCH_LIMIT = 500

USERS = "users"
LEDGER = "pointTransactions"
ARCHIVE = "pointTransactions_archive"


def init_firestore():
    """Initialise firebase-admin from the same env var the server uses."""
    load_dotenv()
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not service_account_json:
        sys.exit(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not set. Run this from "
            "authensnap_server/ with the server's .env present."
        )
    cred = credentials.Certificate(json.loads(service_account_json))
    app = firebase_admin.initialize_app(cred)
    project = getattr(cred, "project_id", None) or "<unknown>"
    print(f"Connected to Firestore project: {project}")
    return fb_firestore.client(app), project


def load_ledger(db):
    """Return every ledger doc as (doc_id, dict). Streams the whole collection."""
    rows = []
    for doc in db.collection(LEDGER).stream():
        rows.append((doc.id, doc.to_dict() or {}))
    return rows


def consistency_report(db, ledger_rows):
    """
    Sum ledger deltas per user and compare to the stored users.points. Any
    mismatch means points moved without a matching ledger row (or vice versa) —
    i.e. a bug or an exploit worth investigating before wiping the evidence.
    """
    ledger_totals = defaultdict(int)
    for _id, data in ledger_rows:
        uid = data.get("userId")
        if uid:
            ledger_totals[uid] += data.get("delta", 0) or 0

    mismatches = []
    seen_users = set()
    users_with_points = 0
    for doc in db.collection(USERS).stream():
        seen_users.add(doc.id)
        data = doc.to_dict() or {}
        stored = data.get("points", 0) or 0
        if stored:
            users_with_points += 1
        expected = ledger_totals.get(doc.id, 0)
        if stored != expected:
            mismatches.append((doc.id, stored, expected))

    # A user could also have ledger rows but a missing/absent user doc.
    orphan_ledger = sorted(u for u in ledger_totals if u not in seen_users)

    print("\n=== Ledger vs. points consistency ===")
    print(f"Users with non-zero points : {users_with_points}")
    print(f"Ledger rows                : {len(ledger_rows)}")
    if not mismatches and not orphan_ledger:
        print("OK — every users.points equals the sum of its ledger deltas.")
    else:
        if mismatches:
            print(f"\n!! {len(mismatches)} user(s) where points != sum(ledger deltas):")
            for uid, stored, expected in mismatches[:50]:
                print(f"   {uid}: points={stored} ledger_sum={expected} (diff {stored - expected:+d})")
            if len(mismatches) > 50:
                print(f"   ...and {len(mismatches) - 50} more")
            print("   Investigate these BEFORE clearing — they indicate points that")
            print("   moved without a matching ledger row (bug or exploit).")
        if orphan_ledger:
            print(f"\n!! {len(orphan_ledger)} userId(s) have ledger rows but no users doc:")
            for uid in orphan_ledger[:50]:
                print(f"   {uid}: ledger_sum={ledger_totals[uid]}")
    print("=====================================\n")
    return mismatches, orphan_ledger, users_with_points


def archive_and_clear(db, ledger_rows, archive_root):
    """Archive the ledger to a run-scoped subcollection, then delete the originals."""
    rows_col = archive_root.collection("rows")

    # 1. Archive (copy) every ledger row under this run.
    written = 0
    batch = db.batch()
    ops = 0
    for doc_id, data in ledger_rows:
        batch.set(rows_col.document(doc_id), data)
        ops += 1
        written += 1
        if ops >= BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            ops = 0
    if ops:
        batch.commit()
    print(f"Archived {written} ledger rows -> {ARCHIVE}/{archive_root.id}/rows")

    # 2. Delete the live ledger rows.
    deleted = 0
    batch = db.batch()
    ops = 0
    for doc_id, _data in ledger_rows:
        batch.delete(db.collection(LEDGER).document(doc_id))
        ops += 1
        deleted += 1
        if ops >= BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            ops = 0
    if ops:
        batch.commit()
    print(f"Deleted {deleted} live ledger rows from {LEDGER}")


def snapshot_and_zero_points(db, archive_root):
    """
    Snapshot every user's current points into the archive, THEN zero them. The
    snapshot captures the full before-state — including legacy pre-ledger points
    that have no matching row in `pointTransactions` — so nothing is lost.
    """
    snap_col = archive_root.collection("points_snapshot")
    zeroed = 0
    batch = db.batch()
    ops = 0
    for doc in db.collection(USERS).stream():
        data = doc.to_dict() or {}
        points = data.get("points", 0) or 0
        if points == 0:
            continue
        # Preserve the pre-zero value, then reset it — two writes, same batch.
        batch.set(snap_col.document(doc.id), {"points": points})
        batch.set(doc.reference, {"points": 0}, merge=True)
        ops += 2
        zeroed += 1
        if ops >= BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            ops = 0
    if ops:
        batch.commit()
    print(f"Snapshotted + zeroed points on {zeroed} user doc(s)")


def main():
    parser = argparse.ArgumentParser(description="Clear the referral-points leaderboard.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually perform the reset. Without this flag the script only reports.",
    )
    parser.add_argument(
        "--allow-mismatch",
        action="store_true",
        help=(
            "Proceed with --commit even if the consistency report shows "
            "points != ledger. Use only after reviewing the mismatches (e.g. "
            "known legacy pre-ledger points). The pre-zero snapshot preserves them."
        ),
    )
    args = parser.parse_args()

    db, project = init_firestore()
    ledger_rows = load_ledger(db)
    mismatches, orphan_ledger, users_to_zero = consistency_report(db, ledger_rows)

    if not args.commit:
        print("[DRY RUN] Nothing was written. This run WOULD:")
        print(f"  - archive {len(ledger_rows)} ledger rows into {ARCHIVE}/<run>/rows")
        print(f"  - delete {len(ledger_rows)} rows from {LEDGER}")
        print(f"  - set points=0 on {users_to_zero} user doc(s)")
        print("  - KEEP referredBy and referralCodes untouched")
        print("\nRe-run with --commit to perform the reset.")
        return

    if (mismatches or orphan_ledger) and not args.allow_mismatch:
        print(
            "Refusing to --commit while the consistency report shows mismatches.\n"
            "Investigate them first (they may be evidence of an exploit). If they\n"
            "are expected (e.g. legacy pre-ledger points), re-run with\n"
            "--commit --allow-mismatch; the pre-zero snapshot preserves the values."
        )
        sys.exit(1)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = db.collection(ARCHIVE).document(run_id)
    archive_root.set(
        {
            "clearedAt": datetime.now(timezone.utc),
            "ledgerRowCount": len(ledger_rows),
            "hadMismatches": bool(mismatches or orphan_ledger),
            "note": "Snapshot taken by clear_leaderboard.py before resetting the leaderboard.",
        }
    )
    print(f"Committing reset (run id {run_id}) on project {project} ...")
    archive_and_clear(db, ledger_rows, archive_root)
    snapshot_and_zero_points(db, archive_root)
    print(f"\nDone. Leaderboard cleared. Full before-state preserved at {ARCHIVE}/{run_id}.")


if __name__ == "__main__":
    main()
