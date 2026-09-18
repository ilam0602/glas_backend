"""
Non-custodial rebuild backfill: flag legacy posts + migrate legacy
`tokenBalances` into the new `viewCredits` (spend-only) / `earnings` (USD)
collections.

Two independent things happen here, and they stay independent on purpose
(keystone constraint of this workstream: no code path may move value between
viewCredits and earnings):

1. Every existing `posts/{tokenId}` doc gets `custodyEra: "legacy"` +
   `legacyMigratedAt` so migrated (legacy) posts are always distinguishable
   from posts created under the new non-custodial flow (which stamp
   `custodyEra: "noncustodial"` at creation time — see `save_post_server` in
   server.py). Posts that already carry a `custodyEra` are left untouched.

2. Every `tokenBalances/{uid}` doc (except the `PLATFORM` sentinel) is
   snapshotted into the new collections at a fixed `--snapshot-rate` (USD per
   legacy VW):
     - `viewCredits/{uid}.balance`   = tokenBalances.purchasedBalance (spend-only, unchanged)
     - `earnings/{uid}.availableUsd` = tokenBalances.earnedBalance * snapshot_rate
     - `earnings/{uid}.lifetimeUsd`  = same, since this is the first earnings snapshot
   plus a `legacy_migration` row in `earningsLedger` and an init row in
   `viewCreditLedger`, using the same ledger-row shape the live /view/spend
   endpoint writes (see economy_v2.py). Legacy `tokenBalances` docs are left
   untouched (read-only source).

IDEMPOTENT: a post that already has `custodyEra` is skipped. A uid that
already has a `viewCredits` or `earnings` doc is treated as already migrated
and skipped (re-running never overwrites live post-migration activity).

ORDER-INDEPENDENT / SAFE BY DEFAULT: dry run (default) writes nothing and
only reports the plan. Pass `--commit` (and `--snapshot-rate`) to perform the
migration.

Usage (from authensnap_server/, with the same .env the server uses):

    python migrate_noncustodial.py                                  # dry run — reports only
    python migrate_noncustodial.py --commit --snapshot-rate 0.0001   # perform the migration

Auth: reuses FIREBASE_SERVICE_ACCOUNT_JSON from .env, exactly like server.py,
so it targets the same Firestore project. Double-check which project that env
points at before running with --commit.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore
from google.cloud.firestore_v1.transforms import Increment as FirestoreIncrement
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from economy_v2 import earning_row, view_credit_row

# Firestore commits are capped at 500 writes per batch.
BATCH_LIMIT = 500

POSTS = "posts"
TOKEN_BALANCES = "tokenBalances"
VIEW_CREDITS = "viewCredits"
EARNINGS = "earnings"
VIEW_CREDIT_LEDGER = "viewCreditLedger"
EARNINGS_LEDGER = "earningsLedger"
PLATFORM_DOC_ID = "PLATFORM"

LEGACY_MIGRATION_REASON = "legacy_migration"

# Marker field written on earnings/{uid} inside the migration transaction.
# Its presence gates idempotency: a uid whose earnings doc already carries it
# has been migrated and is never migrated again.
LEGACY_MARKER = "legacyMigratedAt"


# --- Pure functions (no Firestore/network access — safe to import & test) --

def migrate_post(doc_dict):
    """Flag a legacy post doc with `custodyEra: "legacy"`.

    Idempotent: a doc that already has a `custodyEra` (either "legacy" from a
    prior run of this script, or "noncustodial" from the new post-creation
    path) is returned unchanged.
    """
    d = dict(doc_dict or {})
    if "custodyEra" in d:
        return d
    d["custodyEra"] = "legacy"
    d["legacyMigratedAt"] = datetime.now(timezone.utc)
    return d


def migrate_balances(doc_dict, snapshot_rate):
    """Snapshot a legacy `tokenBalances` doc into the new-collection shape.

    `doc_dict` is expected to carry a `uid` key (the caller sets this from
    the Firestore doc id, since the doc body itself doesn't include it) plus
    the legacy `purchasedBalance`/`earnedBalance` fields (falling back to the
    single legacy `balance` field if the two-bucket migration hasn't run).

    Returns {uid, purchasedBalance, legacyUsd, earningsLedgerRow}:
      - purchasedBalance: legacy VW to be ADDED to viewCredits/{uid}.balance
      - legacyUsd:        earnedBalance * snapshot_rate, to be ADDED to
                          earnings/{uid}.availableUsd and .lifetimeUsd
      - earningsLedgerRow: audit row, reason="legacy_migration"

    The two balance amounts are returned as *deltas to increment*, not
    absolute values to overwrite: the commit driver applies them additively
    inside a per-uid transaction (FirestoreIncrement), so any interim
    activity from the live /view/spend endpoint is preserved rather than
    clobbered by a stale snapshot.

    Pure — does not touch Firestore, so re-running with the same inputs is
    always safe; idempotency against double-writing live data is enforced by
    the caller via a per-uid transaction gated on the legacyMigratedAt marker.
    """
    d = dict(doc_dict or {})
    uid = d.get("uid")
    purchased_balance = d.get("purchasedBalance", d.get("balance", 0))
    earned_balance = d.get("earnedBalance", 0)
    legacy_usd = earned_balance * snapshot_rate

    earnings_ledger_row = earning_row(
        uid, legacy_usd, LEGACY_MIGRATION_REASON, token_id=None
    )

    return {
        "uid": uid,
        "purchasedBalance": purchased_balance,
        "legacyUsd": legacy_usd,
        "earningsLedgerRow": earnings_ledger_row,
    }


# --- Firestore plumbing -----------------------------------------------------

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


def plan_post_migration(db):
    """Scan posts and classify each doc without writing.

    Returns (to_migrate, already_flagged) where to_migrate is a list of
    (doc_ref, migrated_dict) pending writes."""
    to_migrate = []
    already_flagged = 0
    for snap in db.collection(POSTS).stream():
        before = snap.to_dict() or {}
        after = migrate_post(before)
        if after == before:
            already_flagged += 1
            continue
        to_migrate.append((snap.reference, after))
    return to_migrate, already_flagged


def plan_balance_migration(db, snapshot_rate):
    """Scan tokenBalances and classify each doc without writing.

    Returns (to_migrate, already_migrated, platform_skipped) where
    to_migrate is a list of (uid, purchasedBalance, legacyUsd) tuples. A uid
    whose earnings/{uid} doc already carries the legacyMigratedAt marker is
    counted as already migrated. This is a REPORT-TIME classification only;
    the authoritative idempotency check happens inside each per-uid
    transaction at commit time (the marker is read again under the txn), so a
    doc that gets migrated between plan and commit is still safe.
    """
    to_migrate = []
    already_migrated = 0
    platform_skipped = 0
    for snap in db.collection(TOKEN_BALANCES).stream():
        if snap.id == PLATFORM_DOC_ID:
            platform_skipped += 1
            continue
        earnings_snap = db.collection(EARNINGS).document(snap.id).get()
        if earnings_snap.exists and (earnings_snap.to_dict() or {}).get(LEGACY_MARKER):
            already_migrated += 1
            continue
        before = dict(snap.to_dict() or {})
        before["uid"] = snap.id
        result = migrate_balances(before, snapshot_rate)
        to_migrate.append(
            (snap.id, result["purchasedBalance"], result["legacyUsd"])
        )
    return to_migrate, already_migrated, platform_skipped


def migrate_balance_txn(db, uid, purchased_balance, legacy_usd):
    """Run one atomic per-uid migration transaction.

    Money data, so correctness beats throughput: everything for this uid
    lands in a single transaction (slower than batching, but a crash can
    never leave balances written without their ledger rows). Idempotency is
    gated on the legacyMigratedAt marker READ INSIDE the transaction, and all
    balance writes are ADDITIVE (FirestoreIncrement) so any interim
    /view/spend activity is preserved rather than overwritten by a stale
    snapshot. Returns True if migrated, False if skipped (already marked).
    """
    earnings_ref = db.collection(EARNINGS).document(uid)
    view_credits_ref = db.collection(VIEW_CREDITS).document(uid)
    earnings_ledger_ref = db.collection(EARNINGS_LEDGER).document()
    view_credit_ledger_ref = db.collection(VIEW_CREDIT_LEDGER).document()

    @fb_firestore.transactional
    def run(txn):
        earnings_snap = earnings_ref.get(transaction=txn)
        if earnings_snap.exists and (earnings_snap.to_dict() or {}).get(LEGACY_MARKER):
            return False  # already migrated — atomic, idempotent skip

        # viewCredits: ADDITIVE increment (preserves any live balance).
        txn.set(
            view_credits_ref,
            {"balance": FirestoreIncrement(purchased_balance)},
            merge=True,
        )
        # earnings: ADDITIVE increments (preserve live earnings) + set the
        # idempotency marker in the same write.
        txn.set(
            earnings_ref,
            {
                "availableUsd": FirestoreIncrement(legacy_usd),
                "lifetimeUsd": FirestoreIncrement(legacy_usd),
                LEGACY_MARKER: SERVER_TIMESTAMP,
            },
            merge=True,
        )
        # Both ledger rows written in the SAME transaction as the balances.
        txn.set(
            earnings_ledger_ref,
            earning_row(uid, legacy_usd, LEGACY_MIGRATION_REASON, token_id=None),
        )
        txn.set(
            view_credit_ledger_ref,
            view_credit_row(uid, purchased_balance, LEGACY_MIGRATION_REASON),
        )
        return True

    return run(db.transaction())


def commit_migration(db, posts_to_migrate, balances_to_migrate):
    """Commit the plan.

    Posts stay batched (1 idempotent write/post, no money at stake). Balances
    run ONE per-uid transaction each (see migrate_balance_txn). Returns
    (posts_written, balances_written)."""
    batch = db.batch()
    ops = 0
    posts_written = 0
    for ref, after in posts_to_migrate:
        batch.set(ref, after, merge=True)
        ops += 1
        posts_written += 1
        if ops >= BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            ops = 0
    if ops:
        batch.commit()

    balances_written = 0
    for uid, purchased_balance, legacy_usd in balances_to_migrate:
        if migrate_balance_txn(db, uid, purchased_balance, legacy_usd):
            balances_written += 1

    return posts_written, balances_written


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Flag legacy posts (custodyEra=legacy) and backfill legacy "
            "tokenBalances into the new viewCredits/earnings collections."
        )
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually perform the migration. Without this flag the script only reports.",
    )
    parser.add_argument(
        "--snapshot-rate",
        type=float,
        default=None,
        help=(
            "USD per legacy VW, used to convert tokenBalances.earnedBalance into "
            "earnings.availableUsd. Required with --commit."
        ),
    )
    args = parser.parse_args()

    if args.commit and args.snapshot_rate is None:
        sys.exit("--snapshot-rate is required with --commit.")

    db, project = init_firestore()

    posts_to_migrate, already_flagged = plan_post_migration(db)
    preview_rate = args.snapshot_rate if args.snapshot_rate is not None else 0.0
    balances_to_migrate, already_migrated, platform_skipped = plan_balance_migration(
        db, preview_rate
    )
    projected_usd = sum(legacy_usd for _uid, _pb, legacy_usd in balances_to_migrate)

    print("\n=== non-custodial backfill migration plan ===")
    print(f"Posts needing custodyEra flag     : {len(posts_to_migrate)}")
    print(f"Posts already flagged              : {already_flagged}")
    print(f"Balances needing migration         : {len(balances_to_migrate)}")
    print(f"Balances already migrated          : {already_migrated}")
    print(f"PLATFORM doc skipped                : {platform_skipped}")
    if args.snapshot_rate is not None:
        print(f"Snapshot rate (USD/VW)             : {args.snapshot_rate}")
        print(f"Projected total earnings.availableUsd: {projected_usd}")
    else:
        print("Snapshot rate (USD/VW)             : not set (pass --snapshot-rate to preview USD amounts)")
    print("===============================================\n")

    if not args.commit:
        print("[DRY RUN] Nothing was written. This run WOULD:")
        print(f"  - set custodyEra='legacy' + legacyMigratedAt on {len(posts_to_migrate)} post(s)")
        print(
            f"  - for {len(balances_to_migrate)} uid(s), run ONE per-uid txn each: "
            f"additively increment viewCredits/{{uid}}.balance + "
            f"earnings/{{uid}}.availableUsd/lifetimeUsd, set the legacyMigratedAt "
            f"marker, and write earningsLedger + viewCreditLedger rows"
        )
        print("  - leave already-marked balances, already-flagged posts, and the PLATFORM doc untouched")
        print("\nRe-run with --commit --snapshot-rate <rate> to perform the migration.")
        return

    print(f"Committing migration on project {project} ...")
    posts_written, balances_written = commit_migration(
        db, posts_to_migrate, balances_to_migrate
    )
    print(
        f"\nDone. Flagged {posts_written} post(s), migrated {balances_written} "
        "balance doc(s). Idempotent — safe to re-run."
    )


if __name__ == "__main__":
    main()
