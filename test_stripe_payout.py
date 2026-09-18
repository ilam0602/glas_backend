"""
Dev-only smoke test for the Stripe stablecoin-payout (Connect) wiring.

SAFETY: refuses to run unless STRIPE_SECRET_KEY is a TEST key (sk_test_...),
so it can never touch live Connect accounts or move real money.

Two steps (Stripe onboarding is interactive, so it can't be fully automated):

  # Step 1 — create a test Express account + hosted onboarding link:
  python test_stripe_payout.py onboard
      -> prints the acct_... id and an onboarding URL. Open the URL in a
         browser and complete Stripe's TEST onboarding (use the "skip"/test
         data Stripe offers in test mode, and a test payout wallet address).

  # Step 2 — after onboarding, try a test transfer to that account:
  python test_stripe_payout.py transfer acct_XXXX 1.00
      -> checks payouts_enabled, creates a test Transfer (simulated; no real
         USDC, no on-chain tx). Needs test available balance on the platform
         (add some with a test charge, e.g. card 4000 0000 0000 0077).

Uses the same .env the server uses (STRIPE_SECRET_KEY).
"""
import sys

from dotenv import load_dotenv

load_dotenv()

import os

import stripe

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


def _guard_test_key():
    if not stripe.api_key:
        sys.exit("STRIPE_SECRET_KEY is not set (run from authensnap_server/ with .env).")
    if not stripe.api_key.startswith("sk_test_"):
        sys.exit(
            "REFUSING TO RUN: STRIPE_SECRET_KEY is not a test key (sk_test_...). "
            "This script only runs in Stripe test mode."
        )


def onboard():
    base = (os.getenv("PUBLIC_BASE_URL") or "https://example.com").rstrip("/")
    account = stripe.Account.create(
        type="express",
        country="US",
        email="stripe-payout-smoketest@example.com",
        business_type="individual",
        capabilities={"transfers": {"requested": True}},
        metadata={"smoketest": "1"},
    )
    link = stripe.AccountLink.create(
        account=account["id"],
        refresh_url=f"{base}/payout/refresh",
        return_url=f"{base}/payout/return",
        type="account_onboarding",
    )
    print("Created test Express account:", account["id"])
    print("payouts_enabled (pre-onboarding):", account.get("payouts_enabled"))
    print("\nOpen this URL and complete TEST onboarding:\n")
    print(link["url"])
    print("\nThen run:  python test_stripe_payout.py transfer", account["id"], "1.00")


def transfer(account_id, amount_usd):
    acct = stripe.Account.retrieve(account_id)
    print("payouts_enabled:", acct.get("payouts_enabled"))
    if not acct.get("payouts_enabled"):
        sys.exit(
            "Account is not payouts-enabled yet — finish the onboarding link first "
            "(and, in test mode, make sure the transfers capability is active)."
        )
    amount_cents = int(round(float(amount_usd) * 100))
    tr = stripe.Transfer.create(
        amount=amount_cents,
        currency="usd",
        destination=account_id,
    )
    print("Transfer created:", tr["id"], "| amount:", tr["amount"], "| currency:", tr["currency"])
    print("(test mode — simulated; no real USDC, no on-chain tx hash)")


def main():
    _guard_test_key()
    args = sys.argv[1:]
    if args and args[0] == "onboard":
        onboard()
    elif len(args) == 3 and args[0] == "transfer":
        transfer(args[1], args[2])
    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
