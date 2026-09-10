#!/usr/bin/env python3
"""One-off migration: move reward fields off the public `users/{uid}` doc into the
owner-only subdoc `users/{uid}/private/rewards` (audit finding #6).

Two phases — run them separately:

    python migrate_rewards.py copy                # phase 1 (now): copy fields into the
                                                  #   subdoc. Non-destructive + idempotent.
    python migrate_rewards.py delete --yes        # phase 2 (LATER): strip the fields from
                                                  #   the top-level user doc. Run only AFTER
                                                  #   the new client build is confirmed working.

Add --dry-run to either phase to preview without writing anything.

Reuses the server's Firebase Admin credentials (FIREBASE_SERVICE_ACCOUNT_JSON in .env),
so it bypasses Firestore rules — no deployed endpoint, ID token, or env allow-list needed.
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore
from google.cloud.firestore_v1 import DELETE_FIELD

# Must match REWARDS_FIELDS in server.py.
REWARDS_FIELDS = ("points", "referralCode", "referredBy", "phoneVerified", "phoneVerifiedAt")


def _init_db():
    load_dotenv()
    sa = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not sa:
        sys.exit("FIREBASE_SERVICE_ACCOUNT_JSON not set in .env — cannot connect to Firestore.")
    firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
    return fb_firestore.client()


def run(mode, dry_run):
    db = _init_db()
    scanned = touched = 0
    field_counts = {f: 0 for f in REWARDS_FIELDS}

    for doc in db.collection("users").stream():
        scanned += 1
        data = doc.to_dict() or {}
        present = {f: data[f] for f in REWARDS_FIELDS if f in data}
        if not present:
            continue
        for f in present:
            field_counts[f] += 1

        if mode == "copy":
            rewards_ref = doc.reference.collection("private").document("rewards")
            if not dry_run:
                rewards_ref.set(present, merge=True)
            print(f"  copy   {doc.id}: {list(present.keys())}")
        else:  # delete
            if not dry_run:
                doc.reference.set({f: DELETE_FIELD for f in present}, merge=True)
            print(f"  strip  {doc.id}: {list(present.keys())}")
        touched += 1

    verb = "would " if dry_run else ""
    print("\n--- summary ---")
    print(f"mode={mode}  dry_run={dry_run}")
    print(f"users scanned:            {scanned}")
    print(f"users {verb}{'copied' if mode == 'copy' else 'stripped'}: {touched}")
    print(f"per-field counts:         {field_counts}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["copy", "delete"])
    ap.add_argument("--dry-run", action="store_true", help="preview only, no writes")
    ap.add_argument("--yes", action="store_true", help="required to actually run delete")
    args = ap.parse_args()

    if args.mode == "delete" and not args.dry_run and not args.yes:
        sys.exit(
            "Refusing to strip top-level fields without --yes.\n"
            "Run this ONLY after the new client build is confirmed working.\n"
            "Preview first with:  python migrate_rewards.py delete --dry-run"
        )

    run(args.mode, args.dry_run)


if __name__ == "__main__":
    main()
