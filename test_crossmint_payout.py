"""
Dev/test CLI: fire a REAL (testnet) payout through the configured payout
provider (`payout_provider.get_payout_provider()`), to validate the .env
config end-to-end (API key, base URL, chain, treasury wallet) against the
actual Crossmint API.

This is deliberately NOT wired into pytest — it makes a real network call
and moves real (testnet) USDC out of the treasury wallet. Run it by hand.

Usage:

    python test_crossmint_payout.py <recipient_address> <amount_usd>

Example:

    python test_crossmint_payout.py 0xabc...def 0.01

Both arguments are required — this is the guardrail against running it by
accident (no bare `python test_crossmint_payout.py` will do anything).

Env is loaded the same way as clear_leaderboard.py / migrate_token_buckets.py
(`load_dotenv()` reading authensnap_server/.env), and the provider is
selected exactly like the server does — via `PAYOUT_PROVIDER` in that .env.
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import os  # noqa: E402  (after load_dotenv so provider env is already loaded)

from payout_provider import get_payout_provider  # noqa: E402


def usage_and_exit():
    print(__doc__.strip())
    print()
    print("Usage: python test_crossmint_payout.py <recipient_address> <amount_usd>")
    sys.exit(1)


def main():
    if len(sys.argv) != 3:
        usage_and_exit()

    recipient = sys.argv[1]
    amount_str = sys.argv[2]

    if not recipient:
        print("ERROR: recipient_address must not be empty.")
        usage_and_exit()

    try:
        amount = float(amount_str)
    except ValueError:
        print(f"ERROR: amount_usd must be a number, got {amount_str!r}.")
        usage_and_exit()

    if amount <= 0:
        print(f"ERROR: amount_usd must be positive, got {amount}.")
        sys.exit(1)

    provider_name = os.getenv("PAYOUT_PROVIDER")
    base_url = os.getenv("CROSSMINT_BASE_URL")
    chain = os.getenv("CROSSMINT_CHAIN")
    treasury_wallet = os.getenv("CROSSMINT_TREASURY_WALLET")

    print("=== Crossmint payout pre-flight ===")
    print(f"PAYOUT_PROVIDER        : {provider_name!r}")
    print(f"CROSSMINT_BASE_URL     : {base_url!r}")
    print(f"CROSSMINT_CHAIN        : {chain!r}")
    print(f"CROSSMINT_TREASURY_WALLET : {treasury_wallet!r}")
    print(f"recipient (to_address) : {recipient}")
    print(f"amount_usd             : {amount}")
    print(
        "SAFETY: this sends a REAL (testnet) USDC transfer out of the "
        "treasury wallet above — not a dry run, not a simulation."
    )
    if provider_name != "crossmint":
        print(
            f"\nNOTE: PAYOUT_PROVIDER is {provider_name!r}, not 'crossmint' — "
            "get_payout_provider() will return a different provider than the "
            "one this script's pre-flight summary describes. Proceeding with "
            "whatever get_payout_provider() actually returns."
        )
    print("====================================\n")

    provider = get_payout_provider()

    try:
        result = provider.deliver(recipient, amount)
    except RuntimeError as exc:
        print("PAYOUT FAILED (RuntimeError from provider):")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - surface any unexpected error clearly
        print(f"PAYOUT FAILED (unexpected {type(exc).__name__}):")
        print(f"  {exc}")
        sys.exit(1)

    print("PAYOUT SUCCEEDED. Provider returned:")
    for key, value in result.items():
        print(f"  {key}: {value!r}")

    if not result.get("settlementTxHash"):
        print(
            "\nNOTE: settlementTxHash is None/empty — this is expected "
            "immediately after submission. Crossmint confirms the transfer "
            "on-chain asynchronously; the (separate) webhook handler backfills "
            "this later. Check the Crossmint dashboard/API for providerRef "
            f"{result.get('providerRef')!r} to watch it settle."
        )


if __name__ == "__main__":
    main()
