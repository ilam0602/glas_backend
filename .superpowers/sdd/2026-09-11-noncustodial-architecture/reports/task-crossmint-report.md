# Crossmint payout adapter + webhook — task report

## Provider: `CrossmintPayoutProvider` (payout_provider.py)

- Mirrors `BridgePayoutProvider`'s shape: all env (`CROSSMINT_API_KEY`,
  `CROSSMINT_BASE_URL` default `https://staging.crossmint.com/api/2025-06-09`,
  `CROSSMINT_CHAIN` default `base`, `CROSSMINT_TREASURY_WALLET`) is read
  lazily inside `deliver()` — no import-time network/env access.
- `deliver()` POSTs to
  `{base}/wallets/{treasury_wallet}/tokens/{chain}:usdc/transfers` with
  `X-API-KEY` + `Content-Type: application/json`, body
  `{"recipient", "amount": str(round(usd_amount, 2)), "transactionType": "regulated-transfer"}`.
  Non-2xx raises `RuntimeError` including the response body (matches
  Bridge's error-raising convention).
- Maps the 201 response to the shared payout dict:
  `providerRef = resp["id"]`, `chain`, `asset="USDC"`, `amount=usd_amount`,
  `marketRateUsd=1.0`, `settlementTxHash = resp["onChain"]["txId"]` (None
  until confirmed), `status = resp["status"]`.
- `on_behalf_of` is accepted for interface parity but **unused** — flagged
  in the class docstring: Crossmint's `regulated-transfer` triggers
  recipient KYC/AML at transfer time via Crossmint's own flow, not a
  passed-in customer id. Onboarding a creator via Crossmint's Users REST
  API ahead of time (so transfers don't surface as `awaiting-approval`) is
  called out as a separate, not-yet-built follow-on — same shape as
  Bridge's `create_bridge_kyc_link`.

## `get_payout_provider()`

- `PAYOUT_PROVIDER=crossmint` → `CrossmintPayoutProvider()`. Unknown values
  still raise `NotImplementedError` as before.

## Webhook verify: `verify_crossmint_webhook(raw_body, headers)`

- Implements the Svix HMAC-SHA256 scheme: `svix-id` / `svix-timestamp` /
  `svix-signature` headers (case-insensitive dict-like, `.get(...)`
  access — works with Flask's `request.headers` or a plain lowercase-key
  dict in tests). Signed content =
  `f"{svix_id}.{svix_timestamp}.".encode() + raw_body`. Secret is
  `CROSSMINT_WEBHOOK_SECRET` (`whsec_<base64>`); HMAC key = base64-decoded
  tail. `svix-signature` may contain multiple space-delimited `v1,<sig>`
  entries (secret rotation) — any constant-time match accepts.
- Fail-closed on: unset/malformed secret, missing headers, non-integer or
  stale/future timestamp (>300s either direction — `CROSSMINT_WEBHOOK_MAX_AGE_SECONDS`),
  no signature match, or any parsing exception (broad `except Exception`
  returns `False`, never raises).
- Uses only stdlib `hmac`/`hashlib`/`base64` — no new dependency.

## Endpoint: `POST /webhooks/crossmint` (server.py)

- Mirrors `/webhooks/bridge`'s structure: reads `request.get_data()` (raw
  bytes), calls `verify_crossmint_webhook(raw, request.headers)`, returns
  401 on failure. On success, parses JSON, detects a
  transfer/transaction-shaped event by loose substring match on the event
  type, and — with guards at every field access — looks up the
  `earningsLedger` doc where `payout.providerRef == <transfer id>` and
  updates `payout.settlementTxHash` / `payout.status`. Unknown event
  types or missing fields are safe no-ops (200 `{"ok": true}`), never a
  crash.
- **Flagged as TODO** (comment in the code, same pattern as the existing
  Bridge webhook's wire-up notes): Crossmint's exact webhook event
  envelope/type strings and payload field names (`data` vs. some other
  wrapper key, `id`/`transferId`/`transactionId`, `onChain.txId` vs. a
  top-level field) are **not confirmed against a real payload** — the
  transfer *response* shape is documented, the webhook *event* shape is
  not. Must be confirmed before relying on this in production.

## Tests: `test_crossmint.py` (13 tests, all passing)

- Provider: monkeypatches `payout_provider.requests.post` to assert the
  exact URL (`.../wallets/treasury_wallet_1/tokens/base:usdc/transfers`),
  `X-API-KEY` header, and body (`recipient`/`amount` rounded to 2dp/
  `transactionType`); asserts response mapping (`providerRef`,
  `settlementTxHash` from `onChain.txId`, both present and `None`-when-
  unconfirmed cases, `status`); asserts non-2xx raises `RuntimeError` with
  body text; asserts `get_payout_provider()` selects it via
  `PAYOUT_PROVIDER=crossmint`.
- Webhook verify: generates a `whsec_`-style secret, builds real Svix
  headers, HMAC-signs a sample payload — asserts ACCEPT for a valid
  signature and for a rotated-secret-style multi-signature header; asserts
  REJECT for tampered body, bad signature, each individually-missing
  header, stale timestamp, future/skewed timestamp, unset secret, and
  malformed (non-`whsec_`) secret. No network calls anywhere in the file.

## Verification run

- `python -c "import payout_provider"` — clean, no network.
- `python -c "import server"` — clean (only pre-existing GCP ADC warnings,
  unrelated to this change).
- `python -m pytest test_crossmint.py -v` — 13/13 passed.
- `python -m pytest -q` (whole suite) — 100 passed.

No files committed; no subagents used; only `payout_provider.py`,
`server.py`, and the new `test_crossmint.py` were touched.
