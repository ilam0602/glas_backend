# Task P3 — Bridge webhook handler

## Status: DONE, all verification passed.

## Files changed
- `payout_provider.py` — added `verify_bridge_webhook(raw_body: bytes, signature_header: str) -> bool` and `BRIDGE_WEBHOOK_MAX_AGE_MS = 300_000`. New imports: `base64`, `time`, `cryptography.hazmat.primitives.{hashes, serialization}`, `cryptography.hazmat.primitives.asymmetric.padding`.
- `server.py` — new `POST /webhooks/bridge` route (placed just before `/view/balance`); import line updated to also pull in `verify_bridge_webhook`.
- `test_bridge_webhook.py` — new, 7 tests, all passing.

## Verify helper
`verify_bridge_webhook` is fail-closed at every branch (bare `except Exception: return False`, never raises). Order of checks: (1) header present, (2) `BRIDGE_WEBHOOK_PUBLIC_KEY` env set — else reject even before touching the header contents further, (3) header parses into `t`/`v0` via comma-split + `=`-partition, (4) `t` parses as int and `now_ms - t > 300_000` → stale rejection (only checks "too old", not future-dated, per spec wording), (5) RSA verify.

**Signed string**: built as bytes directly — `timestamp_str.encode("utf-8") + b"." + raw_body` — never decoding `raw_body` to text first, so it can't diverge from the exact bytes Bridge signed.

**Padding scheme**: chose `padding.PKCS1v15()` with `hashes.SHA256()`. The spec said "RSA-SHA256" but didn't name PKCS1v15 vs PSS explicitly — PKCS1v15 is the conventional default for "RSA-SHA256" webhook signing (Stripe-style HMAC aside, this is the standard textbook-RSA-signature convention). **Flag: confirm padding scheme against real Bridge docs/payload before going live** — if Bridge actually uses PSS, every signature will fail verification (safe failure mode, but will 401 all real webhooks until fixed).

## Handler (`POST /webhooks/bridge`)
Reads `request.get_data()` (raw bytes) before any JSON parsing, calls `verify_bridge_webhook` first — 401 on failure, before even checking Firestore is configured. Only after verification does it `json.loads(raw_body)` (400 on malformed JSON).

- `kyc_link.updated` / `customer.updated`: queries `users where bridgeCustomerId == customer_id limit 1`; only `kyc_status == "approved"` maps to `payoutKycStatus = "verified"`, every other status is written verbatim (so a later rejection downgrades a stale "verified"). No-op (still 200) if no matching user or fields missing.
- `transfer.updated`: queries `earningsLedger where payout.providerRef == <transfer id> limit 1`, updates `payout.status` and `payout.settlementTxHash` (dotted-path `.update()`, supported by the Firestore Python client for nested map fields) — only sets whichever of state/hash is present.
- Unknown types and no-match cases: 200 ack, no-op.

**Uncertainty flagged to confirm against a real Bridge webhook payload:**
1. Envelope shape — spec said "a `type`... and an object with fields" but didn't name the key. Implemented as: use `event["object"]` if it's a dict, else fall back to treating the whole event dict as the fields container (flat payload). Confirm which Bridge actually sends.
2. Transfer hash field name — tries `destination_tx_hash`, `transaction_hash` at top level and again nested under `receipt`, in that order; picks the first present. Confirm the real field name/nesting.

## Tests
`test_bridge_webhook.py` generates a fresh RSA-2048 keypair via `cryptography`, no network. 7 tests: valid signature accepted; tampered body rejected; garbage signature rejected; missing/malformed header (None/empty/no-kv/missing-t/missing-v0) all rejected; stale timestamp (>300s old) rejected; unset `BRIDGE_WEBHOOK_PUBLIC_KEY` rejected (fail-closed); signed-with-wrong-key rejected.

## Verification run
- `python -m pytest test_bridge_webhook.py -v` → 7 passed
- `python -c "import server"` → clean (only expected GCP/Firebase startup logs)
- `python -m pytest -q` (whole suite) → 87 passed

## Concerns
- Padding scheme (PKCS1v15) and event envelope/hash-field names are best-effort guesses per spec wording — must be validated against Bridge's actual webhook docs/sample payload before this can be trusted against live traffic. Everything else (fail-closed gating, raw-body verification, downgrade-on-non-approved, ledger dotted-path update) is solid.
