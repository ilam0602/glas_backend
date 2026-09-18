"""
Reconciliation / audit script for the non-custodial economy (View Credits + Earnings).

Per the custody boundary in the non-custodial rebuild, `viewCredits` and
`earnings` are separate systems that must never move value between each
other, and every mutation to either balance must have a matching ledger row:

  - viewCredits/{uid}.balance     <-> sum of viewCreditLedger deltas for uid
  - earnings/{uid}.availableUsd   <-> sum of earningsLedger deltaUsd for uid
                                      (net of payouts — a payout row is just
                                      another ledger row with deltaUsd < 0,
                                      so summing ALL deltaUsd for a uid already
                                      nets them out)

This script is READ-ONLY / REPORT-ONLY. It streams both ledgers and both
balance collections, aggregates ledger deltas per user, and prints a
consistency report of any mismatches. It performs ZERO writes to Firestore —
there is no --commit flag and no code path that calls .set/.update/.delete
on any Firestore reference.

On-chain settlement verification (resolving each earningsLedger payout row's
`settlementTxHash` against the chain to confirm the payout actually landed in
the user's wallet) is explicitly OUT OF SCOPE for this script — it's a
follow-up task once payouts actually exist in the ledger.

Usage (from authensnap_server/, with the same .env the server uses):

    python audit_economy_v2.py            # run the report
    python audit_economy_v2.py --help      # usage

Auth: reuses FIREBASE_SERVICE_ACCOUNT_JSON from .env, exactly like
clear_leaderboard.py / server.py, so it targets the same Firestore project.
"""

import argparse
import json
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

VIEW_CREDITS = "viewCredits"
VIEW_CREDIT_LEDGER = "viewCreditLedger"
EARNINGS = "earnings"
EARNINGS_LEDGER = "earningsLedger"

PRINT_CAP = 50


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


def load_ledger(db, collection_name, uid_field, delta_field):
    """Stream a ledger collection and sum its delta field per uid.

    Returns (totals: dict[uid -> sum], row_count: int).
    """
    totals = defaultdict(float)
    row_count = 0
    for doc in db.collection(collection_name).stream():
        row_count += 1
        data = doc.to_dict() or {}
        uid = data.get(uid_field)
        if not uid:
            continue
        totals[uid] += data.get(delta_field, 0) or 0
    return totals, row_count


def load_balances(db, collection_name, balance_field):
    """Stream a balance collection, returning dict[uid -> stored balance]."""
    balances = {}
    for doc in db.collection(collection_name).stream():
        data = doc.to_dict() or {}
        balances[doc.id] = data.get(balance_field, 0) or 0
    return balances


def _isclose(a, b, tol=1e-6):
    return abs(a - b) <= tol


def reconcile(label, ledger_totals, balances):
    """
    Compare ledger_totals (uid -> summed delta) against balances (uid -> stored
    balance). Returns (mismatches, orphan_ledger_uids) where mismatches is a
    list of (uid, stored, expected) and orphan_ledger_uids is uids that have
    ledger rows but no balance doc.
    """
    mismatches = []
    seen = set()
    for uid, stored in balances.items():
        seen.add(uid)
        expected = ledger_totals.get(uid, 0)
        if not _isclose(stored, expected):
            mismatches.append((uid, stored, expected))

    orphan_ledger = sorted(u for u in ledger_totals if u not in seen)

    print(f"\n=== {label}: ledger vs. balance consistency ===")
    print(f"Users with a balance doc : {len(balances)}")
    print(f"Users with ledger rows   : {len(ledger_totals)}")
    if not mismatches and not orphan_ledger:
        print("OK — every balance equals the sum of its ledger deltas.")
    else:
        if mismatches:
            print(f"\n!! {len(mismatches)} user(s) where balance != sum(ledger deltas):")
            for uid, stored, expected in mismatches[:PRINT_CAP]:
                print(f"   {uid}: balance={stored} ledger_sum={expected} (diff {stored - expected:+g})")
            if len(mismatches) > PRINT_CAP:
                print(f"   ...and {len(mismatches) - PRINT_CAP} more")
        if orphan_ledger:
            print(f"\n!! {len(orphan_ledger)} uid(s) have ledger rows but no {label} balance doc:")
            for uid in orphan_ledger[:PRINT_CAP]:
                print(f"   {uid}: ledger_sum={ledger_totals[uid]}")
            if len(orphan_ledger) > PRINT_CAP:
                print(f"   ...and {len(orphan_ledger) - PRINT_CAP} more")
    print("=" * (len(label) + 34))
    return mismatches, orphan_ledger


def run_report(db):
    # --- View Credits ---
    vc_ledger_totals, vc_row_count = load_ledger(
        db, VIEW_CREDIT_LEDGER, uid_field="uid", delta_field="delta"
    )
    vc_balances = load_balances(db, VIEW_CREDITS, balance_field="balance")
    vc_mismatches, vc_orphans = reconcile("viewCredits", vc_ledger_totals, vc_balances)

    # --- Earnings ---
    earn_ledger_totals, earn_row_count = load_ledger(
        db, EARNINGS_LEDGER, uid_field="uid", delta_field="deltaUsd"
    )
    earn_balances = load_balances(db, EARNINGS, balance_field="availableUsd")
    earn_mismatches, earn_orphans = reconcile("earnings", earn_ledger_totals, earn_balances)

    print("\n=== Totals ===")
    print(f"viewCredits balance docs : {len(vc_balances)}")
    print(f"viewCreditLedger rows    : {vc_row_count}")
    print(f"earnings balance docs    : {len(earn_balances)}")
    print(f"earningsLedger rows      : {earn_row_count}")
    total_mismatches = len(vc_mismatches) + len(vc_orphans) + len(earn_mismatches) + len(earn_orphans)
    print(f"Total mismatches found   : {total_mismatches}")
    print("===============\n")

    print(
        "NOTE: on-chain settlement verification (resolving each earningsLedger\n"
        "payout row's settlementTxHash against the chain to confirm funds landed\n"
        "in the user's wallet) is NOT performed by this script. That is a\n"
        "follow-up task once payouts actually exist in the ledger.\n"
    )

    return {
        "view_credits": {
            "mismatches": vc_mismatches,
            "orphan_ledger": vc_orphans,
            "balance_docs": len(vc_balances),
            "ledger_rows": vc_row_count,
        },
        "earnings": {
            "mismatches": earn_mismatches,
            "orphan_ledger": earn_orphans,
            "balance_docs": len(earn_balances),
            "ledger_rows": earn_row_count,
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Report-only reconciliation of the non-custodial economy: "
            "viewCredits/earnings balances vs. their ledgers. Never writes "
            "to Firestore."
        )
    )
    # No flags beyond --help: this script only ever reports, by design.
    parser.parse_args()

    db, project = init_firestore()
    print(f"Auditing project: {project} (report-only, no writes)")
    run_report(db)


if __name__ == "__main__":
    main()
