# Task: Crossmint payout dev/test scripts

## Files added
- `authensnap_server/test_crossmint_payout.py`
- `authensnap_server/seed_test_payout_user.py`

## Script 1: `test_crossmint_payout.py`

Usage:
```
python test_crossmint_payout.py <recipient_address> <amount_usd>
# e.g.
python test_crossmint_payout.py 0xabc...def 0.01
```

- Requires both positional args; missing/invalid args (non-numeric or
  non-positive amount) print the module docstring + usage line and `exit(1)`.
- `load_dotenv()` first, then prints a pre-flight summary: `PAYOUT_PROVIDER`,
  `CROSSMINT_BASE_URL`, `CROSSMINT_CHAIN`, `CROSSMINT_TREASURY_WALLET`,
  recipient, amount — `CROSSMINT_API_KEY` is never read for printing or
  logged anywhere. Includes an explicit safety line that this sends a real
  (testnet) USDC transfer out of the treasury wallet. If `PAYOUT_PROVIDER`
  isn't `"crossmint"`, it warns that `get_payout_provider()` will actually
  return a different provider than the one described.
- Calls `get_payout_provider().deliver(recipient, float(amount))`. On
  success, prints every key/value in the returned dict, plus a note that
  `settlementTxHash` may be `None`/pending (Crossmint confirms on-chain
  asynchronously). On `RuntimeError` (Crossmint's own error body — bad
  wallet locator, insufficient balance, etc.) or any other exception, prints
  the error and `exit(1)`.
- No `--commit` flag by design — the two required positional args plus the
  provider always being testnet/staging is the guardrail; there's no way to
  invoke it accidentally.

## Script 2: `seed_test_payout_user.py`

Usage:
```
python seed_test_payout_user.py <uid> <wallet_address> [available_usd] [--commit]
# e.g.
python seed_test_payout_user.py testuser123 0xabc...def 1.00 --commit
```

- `available_usd` defaults to `1.00`. `wallet_address` is validated against
  `^0x[a-fA-F0-9]{40}$` before anything else; invalid input exits 1 with a
  clear message. Missing `uid`/`wallet_address` prints the docstring + usage
  and exits 1 (via argparse `nargs="?"` + manual check, so bare invocation
  still prints usage rather than an argparse traceback).
- **Dry-run by default**: prints the exact `users/{uid}` and `earnings/{uid}`
  dicts it *would* merge-write, and does not touch Firestore/network at all
  in this mode (so it works even without `FIREBASE_SERVICE_ACCOUNT_JSON` set).
- `--commit`: calls `init_firestore()` (same `FIREBASE_SERVICE_ACCOUNT_JSON`
  + `firebase_admin.initialize_app` pattern as `clear_leaderboard.py`, prints
  the connected project id), then merge-sets:
  - `users/{uid}`: `{walletAddress, payoutKycStatus: "verified"}`
  - `earnings/{uid}`: `{availableUsd: <amount>, lifetimeUsd: <amount>}`

  matching exactly the fields `/earnings/cash-out` in `server.py` reads
  (`walletAddress`, `payoutKycStatus == "verified"` gate, `earnings.availableUsd`).
  Prints a confirmation naming the project + uid on success.

## Verify
- `python test_crossmint_payout.py` (no args) → prints usage, `exit(1)`. Confirmed.
- `python seed_test_payout_user.py` (no args) → prints usage, `exit(1)`. Confirmed.
- `python seed_test_payout_user.py testuser123 0x1234...7890 2.50` (no
  `--commit`) → printed the dry-run summary, wrote nothing, `exit(0)`. Confirmed.
- `python -m pytest -q` → 100 passed, 2 warnings (unrelated google-auth ADC
  warning), unchanged from baseline.
- Neither script's happy path (real payout / `--commit` write) was executed,
  per instructions — only the guardrail/usage/dry-run paths were exercised.
