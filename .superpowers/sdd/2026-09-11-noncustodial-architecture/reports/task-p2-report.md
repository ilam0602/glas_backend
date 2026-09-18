# Task P2 report: Bridge customer/KYC onboarding

## `create_bridge_kyc_link(full_name, email)` — `payout_provider.py`

Module-level function (next to `get_payout_provider`), env-driven the same
way as `BridgePayoutProvider` (`BRIDGE_API_KEY`, `BRIDGE_BASE_URL`, read
lazily inside the function body — no network at import). It:

- Generates a fresh `Idempotency-Key` (`uuid.uuid4()`) per call.
- POSTs `{base_url}/v0/kyc_links` with body
  `{"full_name": full_name, "email": email, "type": "individual"}` and
  headers `Api-Key`, `Idempotency-Key`, `Content-Type: application/json`.
- Raises `RuntimeError` with the response body on any non-2xx status
  (mirrors `BridgePayoutProvider.deliver`'s error style).
- Maps the response to `{"customerId": payload["customer_id"], "kycLink":
  payload["kyc_link"], "kycStatus": payload["kyc_status"]}`.

## Endpoints — `server.py`

Added directly above `/view/balance`, right after `/earnings/cash-out`:

- **`POST /payout/onboard`** (auth via `verify_firebase_token`): body
  `{fullName, email}`; `email` defaults to `_user_email(uid)` (existing
  helper — checks `users/{uid}.email` then falls back to
  `firebase_auth.get_user(uid).email`) when omitted. Calls
  `create_bridge_kyc_link(...)`, then `users/{uid}.set({bridgeCustomerId,
  payoutKycStatus}, merge=True)`, and returns `{kycLink, kycStatus}`.
  Returns 400 if `fullName`/`email` end up empty, 502 if the Bridge call
  raises.
- **`GET /payout/status`** (auth): returns `{payoutKycStatus, hasWallet}`
  read straight from `users/{uid}` (`hasWallet = bool(walletAddress)`).

### kyc_status -> payoutKycStatus mapping note (as required)

`/payout/onboard` stores Bridge's **raw** `kyc_status`
(`not_started|under_review|incomplete|approved|rejected`) into
`payoutKycStatus` — it never writes the literal string `"verified"`, even
if Bridge already reports `"approved"` at link-creation time. The
`/earnings/cash-out` gate checks `payoutKycStatus == "verified"` exactly;
only the not-yet-built KYC webhook (task P3) is allowed to make that
`"approved" -> "verified"` translation, after independently confirming
approval. This is called out in both `payout_provider.py`'s docstring and
an inline comment on `/payout/onboard` in `server.py`, specifically so a
future edit doesn't "helpfully" short-circuit the gate by mapping
`"approved"` straight to `"verified"` inline.

## Tests — `test_bridge_kyc.py`

Same monkeypatch-`requests.post` style as the existing
`test_payout_provider.py` (`_FakeResponse` stand-in, `payout_provider.requests`
patched, no real network):

- Asserts the POST URL, headers (`Api-Key`, `Idempotency-Key` present,
  `Content-Type`), and body (`full_name`/`email`/`type`) are correct, and
  the response maps to `{customerId, kycLink, kycStatus}`.
- Asserts two calls use distinct `Idempotency-Key`s.
- Asserts a non-2xx response raises `RuntimeError` containing the response
  body.

No endpoint-level (`/payout/onboard`, `/payout/status`) tests were added —
out of scope per the deliverable list, which only specifies testing
`create_bridge_kyc_link`. If desired, `test_report_post.py`'s
`FakeFirestoreDb`/`monkeypatch server.firebase_auth.verify_id_token`
pattern would be the natural template for those.

## Verification

- `python -m pytest test_bridge_kyc.py -v` — 3 passed.
- `python -c "import server"` — clean (only pre-existing GCP ADC
  UserWarning, unrelated).
- `python -c "import payout_provider"` — clean, no network at import.
- `python -m pytest -q` — 80 passed, whole suite green.

## Concerns

- `/payout/onboard` is not idempotent against re-onboarding: calling it
  again for a user who already has a `bridgeCustomerId` will create a
  **second** Bridge customer/KYC link and overwrite the stored
  `bridgeCustomerId`/`payoutKycStatus`. Not required by this task's spec,
  but worth flagging for the client/product layer (e.g. check
  `/payout/status` first, or the endpoint could short-circuit if a
  customer already exists) before this ships to real users.
- No rate limiting/replay protection on `/payout/onboard` beyond Firebase
  auth — a malicious client could spam Bridge KYC-link creation for
  themselves. Low risk (self-serve, self-targeting) but noting it.
- The webhook that flips `payoutKycStatus` to `"verified"` (task P3) still
  does not exist, so KYC'd creators cannot yet actually cash out — expected,
  per the task's own framing.
