"""
Migrate `tokenBalances` docs from the single-`balance` model to the two-bucket
model (`purchasedBalance` spend-only + `earnedBalance` withdraw-only).

All legacy `balance` becomes `purchasedBalance` (spend-only); `earnedBalance`
starts at 0. The legacy `balance` field is retained (for a rollback window).
The `PLATFORM` sentinel doc is skipped (it is server revenue, not a user wallet).

IDEMPOTENT: a doc that already has `purchasedBalance` is left unchanged, and any
`earnedBalance` already accrued is preserved. Safe to re-run.

ORDER-INDEPENDENT: the server's read-fallback treats a doc with no
`purchasedBalance` as `{purchased: balance, earned: 0}`, so this batch can run
before OR after the new server code is deployed.

SAFE BY DEFAULT: a dry run that writes nothing and prints what it *would* do.
Pass `--commit` to actually perform the migration.

Usage (from authensnap_server/, with the same .env the server uses):

    python migrate_token_buckets.py            # dry run — reports only
    python migrate_token_buckets.py --commit   # perform the migration

Auth: reuses FIREBASE_SERVICE_ACCOUNT_JSON from .env, exactly like server.py,
so it targets the same Firestore project. Double-check which project that env
points at before running with --commit.
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

# Firestore commits are capped at 500 writes per batch.
BATCH_LIMIT = 500

TOKEN_BALANCES = "tokenBalances"
PLATFORM_DOC_ID = "PLATFORM"


def migrate_balance_doc(doc_dict):
    """Legacy tokenBalances doc -> two-bucket. All legacy `balance` becomes
    purchased (spend-only); earned starts at 0. Idempotent: a doc that already
    has `purchasedBalance` is returned unchanged (earned preserved). Legacy
    `balance` is retained for a rollback window."""
    d = dict(doc_dict or {})
    if "purchasedBalance" in d:
        return d
    d["purchasedBalance"] = d.get("balance", 0)
    d["earnedBalance"] = d.get("earnedBalance", 0)
    return d


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


def plan_migration(db):
    """Scan tokenBalances and classify each doc without writing.

    Returns (to_migrate, already_migrated, platform_skipped) where to_migrate is
    a list of (doc_ref, migrated_dict) pending writes."""
    to_migrate = []
    already_migrated = 0
    platform_skipped = 0
    for snap in db.collection(TOKEN_BALANCES).stream():
        if snap.id == PLATFORM_DOC_ID:
            platform_skipped += 1
            continue
        before = snap.to_dict() or {}
        after = migrate_balance_doc(before)
        if after == before:
            already_migrated += 1
            continue
        to_migrate.append((snap.reference, after))
    return to_migrate, already_migrated, platform_skipped


def commit_migration(db, to_migrate):
    """Write the migrated docs in <=500-write batches (merge, never overwrite)."""
    written = 0
    batch = db.batch()
    ops = 0
    for ref, after in to_migrate:
        batch.set(ref, after, merge=True)
        ops += 1
        written += 1
        if ops >= BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            ops = 0
    if ops:
        batch.commit()
    return written


def main():
    parser = argparse.ArgumentParser(
        description="Migrate tokenBalances to the two-bucket (purchased/earned) model."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually perform the migration. Without this flag the script only reports.",
    )
    args = parser.parse_args()

    db, project = init_firestore()
    to_migrate, already_migrated, platform_skipped = plan_migration(db)

    print("\n=== tokenBalances migration plan ===")
    print(f"Docs needing migration : {len(to_migrate)}")
    print(f"Already migrated       : {already_migrated}")
    print(f"PLATFORM doc skipped   : {platform_skipped}")
    print("====================================\n")

    if not args.commit:
        print("[DRY RUN] Nothing was written. This run WOULD:")
        print(f"  - set purchasedBalance = legacy balance, earnedBalance = 0")
        print(f"    on {len(to_migrate)} doc(s) (legacy `balance` retained)")
        print("  - leave already-migrated docs and the PLATFORM doc untouched")
        print("\nRe-run with --commit to perform the migration.")
        return

    print(f"Committing migration on project {project} ...")
    written = commit_migration(db, to_migrate)
    print(f"\nDone. Migrated {written} tokenBalances doc(s). Idempotent — safe to re-run.")


if __name__ == "__main__":
    main()
