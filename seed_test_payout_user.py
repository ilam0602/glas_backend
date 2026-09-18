"""
Dev-only helper: seed a Firestore test user so the FULL `/earnings/cash-out`
flow (see server.py) can be exercised end-to-end against a real payout
provider (e.g. Crossmint via `test_crossmint_payout.py`, or by hitting the
server's `/earnings/cash-out` route directly).

It sets exactly the fields `/earnings/cash-out` reads:
  - `users/{uid}.walletAddress`      (destination for the payout)
  - `users/{uid}.payoutKycStatus`    ("verified" — the cash-out gate)
  - `earnings/{uid}.availableUsd`    (balance the route debits)
  - `earnings/{uid}.lifetimeUsd`     (cumulative total)

SAFE BY DEFAULT: a dry run that only prints what it WOULD write. Pass
`--commit` to actually write to Firestore.

Usage (from authensnap_server/, with the same .env the server uses):

    # Full: set wallet + verified + earnings (raw-address / Bridge-style test)
    python seed_test_payout_user.py <uid> <wallet_address> [--usd 1.00] --commit
    # Earnings ONLY (use AFTER onboarding via the app, which already set the
    # Crossmint-managed wallet — omit wallet_address so it is NOT clobbered):
    python seed_test_payout_user.py <uid> [--usd 1.00] --commit

--usd defaults to 1.00. Omitting wallet_address seeds ONLY earnings/{uid}
and leaves users/{uid}.walletAddress + payoutKycStatus untouched.

Auth: reuses FIREBASE_SERVICE_ACCOUNT_JSON from .env, exactly like
clear_leaderboard.py, so it targets the same Firestore project as the
server. Double-check which project that env points at before --commit.
"""

import argparse
import json
import os
import re
import sys

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

WALLET_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


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


def usage_and_exit():
    print(__doc__.strip())
    print()
    print(
        "Usage: python seed_test_payout_user.py <uid> <wallet_address> "
        "[available_usd] [--commit]"
    )
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Seed a Firestore test user for the /earnings/cash-out flow.",
        add_help=True,
    )
    parser.add_argument("uid", nargs="?", help="Firestore users/{uid} to seed")
    parser.add_argument(
        "wallet_address",
        nargs="?",
        help="0x... destination wallet (OPTIONAL: omit to seed earnings only, "
        "leaving walletAddress/payoutKycStatus untouched — use after app onboarding)",
    )
    parser.add_argument(
        "--usd",
        dest="available_usd",
        type=float,
        default=1.00,
        help="availableUsd/lifetimeUsd to set (default 1.00)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to Firestore. Without this flag, only prints a dry run.",
    )
    args = parser.parse_args()

    if not args.uid:
        usage_and_exit()

    if args.wallet_address and not WALLET_ADDRESS_RE.match(args.wallet_address):
        print(
            f"ERROR: wallet_address {args.wallet_address!r} does not look like a "
            "valid 0x-prefixed 40-hex-char Ethereum address."
        )
        sys.exit(1)

    if args.available_usd < 0:
        print(f"ERROR: --usd must be >= 0, got {args.available_usd}.")
        sys.exit(1)

    # Earnings-only when no wallet is passed: leave users/{uid} untouched so the
    # Crossmint-managed wallet + payoutKycStatus set by /payout/onboard survive.
    user_updates = None
    if args.wallet_address:
        user_updates = {
            "walletAddress": args.wallet_address,
            "payoutKycStatus": "verified",
        }
    earnings_updates = {
        "availableUsd": args.available_usd,
        "lifetimeUsd": args.available_usd,
    }

    print("=== seed_test_payout_user pre-flight ===")
    print(f"uid            : {args.uid}")
    print(f"wallet_address : {args.wallet_address or '(unchanged — earnings only)'}")
    print(f"available_usd  : {args.available_usd}")
    if user_updates is not None:
        print(f"\nWould set users/{args.uid} (merge)   : {user_updates}")
    else:
        print(f"\nusers/{args.uid} : UNCHANGED (earnings-only mode)")
    print(f"Would set earnings/{args.uid} (merge) : {earnings_updates}")
    print("==========================================\n")

    if not args.commit:
        print("[DRY RUN] Nothing was written. Re-run with --commit to write these values.")
        return

    db, project = init_firestore()
    print(f"Committing to project {project} ...")

    if user_updates is not None:
        db.collection("users").document(args.uid).set(user_updates, merge=True)
    db.collection("earnings").document(args.uid).set(earnings_updates, merge=True)

    label = f"earnings/{args.uid}" + (
        f" and users/{args.uid}" if user_updates is not None else ""
    )
    print(f"Done. {label} updated on project {project}.")


if __name__ == "__main__":
    main()
