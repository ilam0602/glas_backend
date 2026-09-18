# Task report: Crossmint recipient onboarding (users + wallet)

## Why
Crossmint's `regulated-transfer` transactionType rejects raw addresses — it
only pays a Crossmint-managed wallet OWNED by a registered Crossmint user.
This adds the two-step onboarding (register user, create owned wallet) and
wires it into `/payout/onboard`, plus a KYC-pending retry on `deliver()`
since Crossmint KYCs the recipient at transfer time.

## Files changed
- `payout_provider.py`
- `server.py`
- `test_crossmint_users.py` (new)

## 1. New functions in `payout_provider.py`

### `crossmint_register_user(email, first_name, last_name, dob, country="US")`
`PUT {CROSSMINT_BASE_URL}/users/{urlencode("email:"+email)}` with body
`{"userDetails": {"firstName","lastName","dateOfBirth","countryOfResidence"}}`,
header `X-API-KEY` from `CROSSMINT_API_KEY`. Idempotent per Crossmint's PUT
contract. Raises `RuntimeError` (with response body) on non-2xx. All env
reads are lazy (inside the function), so importing the module makes no
network call.

### `crossmint_create_recipient_wallet(email) -> str`
`POST {CROSSMINT_BASE_URL}/wallets` with minimal body
`{"chainType":"evm","type":"smart","owner":"email:"+email}`. Returns
`response["address"]`. Raises `RuntimeError` on non-2xx.

**FLAGGED (unconfirmed against real staging):** left an explicit code
comment in the docstring that if real Crossmint staging 400s requiring a
signer, the fix is adding `"config": {"adminSigner": {"type": "email"}}` to
the body — the quickstart's SDK path configures wallet recovery this way,
but the minimal REST body is what's implemented and untested against a live
staging server. Flagged, not silently assumed correct.

## 2. `CrossmintPayoutProvider.deliver()` KYC-pending retry
Added a bounded retry loop around the transfer POST: on a non-2xx response,
if the response body (lowercased) contains any of `"kyc"`, `"processing"`,
`"in progress"`, `"retry"`, sleep `time.sleep(3)` and retry, up to 5 total
attempts. Any other error (or the 5th consecutive KYC-pending failure)
raises `RuntimeError` immediately/finally with the response body. Mirrors
Crossmint's own quickstart pattern (KYC runs at transfer time, not
onboarding time).

## 3. `server.py` `/payout/onboard` branch
Branches on `PAYOUT_PROVIDER`:
- **`crossmint`**: requires `{email, firstName, lastName, dob, country?}`
  (country defaults `"US"`, falls back to server-known email via
  `_user_email(uid)` if `email` omitted). Calls
  `crossmint_register_user(...)` then `crossmint_create_recipient_wallet(email)`.
  Persists on `users/{uid}`: `walletAddress` (the new recipient wallet
  address), `crossmintUserLocator = "email:"+email`, and
  `payoutKycStatus = "verified"`. Returns
  `{walletAddress, status: "onboarded"}`. Any exception from either
  Crossmint call → `502 {"error": "crossmint_onboarding_failed: ..."}`.
- **default/`bridge`**: unchanged — existing `create_bridge_kyc_link` flow,
  byte-for-byte preserved.
- `GET /payout/status` untouched.

**RULING (flagged in code comment above the route):** unlike Bridge (which
has a distinct "kyc approved" webhook-driven signal), Crossmint enforces
KYC/AML on the recipient at *transfer* time, not at onboarding time — there
is no onboarding-time "approved" state to wait on. So glas's own
`payoutKycStatus == "verified"` gate is treated as secondary for this
provider: it's set to `"verified"` immediately after successful Crossmint
user + wallet creation purely so `/earnings/cash-out`'s pre-existing gate
doesn't block a creator who's done everything glas controls. Real
enforcement is Crossmint's own KYC check at transfer time, backstopped by
`deliver()`'s KYC-pending retry.

## Test approach (`test_crossmint_users.py`, 8 tests, all passing)
No real network — `payout_provider.requests.put`/`.post` monkeypatched
(same `_FakeResponse` shim pattern as `test_bridge_kyc.py` /
`test_payout_provider.py`):
- `crossmint_register_user`: asserts PUT URL is correctly urlencoded
  (`/users/email%3Ajane%40example.com`), body shape, `X-API-KEY` header,
  country defaulting, and `RuntimeError` on 400.
- `crossmint_create_recipient_wallet`: asserts POST URL/body
  (`{chainType,type,owner}`), returns `address` from the 201 payload, and
  `RuntimeError` on error response.
- `deliver()` retry: one test fails-with-KYC-pending-body twice then
  succeeds on the 3rd attempt (asserts `time.sleep` called with `3` twice,
  final result shape correct); one test exhausts all 5 attempts on
  persistent KYC-pending and raises (asserts 4 sleeps, final error message);
  one test asserts a non-KYC error (e.g. "insufficient treasury balance")
  raises immediately with zero retries/sleeps.

## Verification run
- `python -m pytest test_crossmint_users.py -v` → 8 passed
- `python -c "import payout_provider"` → clean, no network
- `python -c "import server"` → clean
- `python -m pytest -q` (whole suite) → 108 passed

## Concerns / follow-ups
- `crossmint_create_recipient_wallet`'s `adminSigner`/recovery config is
  unconfirmed against real Crossmint staging — flagged in the docstring;
  first real staging call should be watched for a 400 requiring it.
- The `payoutKycStatus = "verified"` write on the Crossmint onboarding path
  is a deliberate policy call (see RULING above), not a technical
  necessity — worth a second look from whoever owns the compliance/legal
  posture, since it diverges from Bridge's stricter "webhook-only can flip
  to verified" invariant.
- No changes made to `/earnings/cash-out`, `/payout/status`, or the Bridge
  path — all untouched per the task's scope.

## Fix round 1

UX fix in `/earnings/cash-out`: `provider.deliver(...)` was called with no
error handling, so a Crossmint KYC-pending failure (RuntimeError raised after
`deliver()`'s retry loop exhausts) propagated as an unhandled Flask 500. It
already fails safe (this is BEFORE the earnings-settle transaction — no money
moves, no ledger write), but a 500 is a poor signal for tomorrow's testing.
Wrapped ONLY the deliver() call in try/except and mapped the failure to a
clean, actionable status. The transactional settle + ledger write below are
untouched and still run only on a successful deliver().

### Before
```python
    bridge_customer_id = user_data.get("bridgeCustomerId")
    payout_result = provider.deliver(
        wallet_address, amount_usd, "USDC", on_behalf_of=bridge_customer_id
    )
```

### After
```python
    bridge_customer_id = user_data.get("bridgeCustomerId")
    # ... comment: wrap ONLY the provider call; nothing to undo on error path ...
    try:
        payout_result = provider.deliver(
            wallet_address, amount_usd, "USDC", on_behalf_of=bridge_customer_id
        )
    except Exception as e:
        message = str(e)
        lowered = message.lower()
        if any(marker in lowered for marker in ("kyc", "processing", "in progress")):
            return jsonify({"error": "payout_pending", "detail": message}), 409
        return jsonify({"error": "payout_failed", "detail": message}), 502
```

- KYC-pending (message contains "kyc" / "processing" / "in progress",
  case-insensitive) → `409 {"error":"payout_pending","detail":<msg>}`
  (retriable "try again shortly").
- Any other provider failure → `502 {"error":"payout_failed","detail":<msg>}`.
- Everything after `deliver()` (the `@fb_firestore.transactional` settle and
  the earningsLedger write) is byte-for-byte unchanged and is reached only on
  a successful deliver().

### Verification
- `python -c "import server"` → clean
- `python -m pytest -q` → 108 passed (unchanged)
- Touched only `server.py`.

## Fix round 2

Security review flagged MEDIUM: the round-1 handler returned
`"detail": message`, echoing the raw provider/Crossmint exception text (which
can contain recipient PII, wallet details, or internal API error bodies) back
to the client. Fixed: the raw message is now logged server-side only and never
returned.

### After (final)
```python
    except Exception as e:
        # Log the full provider error server-side only. Do NOT echo it back ...
        print(f"[payout] cash-out deliver failed for {uid}: {e}", flush=True)
        lowered = str(e).lower()
        if any(marker in lowered for marker in ("kyc", "processing", "in progress")):
            return jsonify({"error": "payout_pending"}), 409
        return jsonify({"error": "payout_failed"}), 502
```

- Full error logged server-side via `print(..., flush=True)` (matches the
  file's existing logging style), keyed by `uid`.
- `str(e)` substring inspection stays server-side, used ONLY to choose the
  status code (409 KYC-pending vs 502 otherwise).
- No `detail`/raw message in either JSON response.
- Settle/ledger path unchanged (runs only on a successful deliver()).

### Verification
- `python -c "import server"` → clean
- `python -m pytest -q` → 108 passed (unchanged)
- Touched only `server.py`.
