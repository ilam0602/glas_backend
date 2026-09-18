# Bridge payout adapter — test-only wiring

Status: implemented, working-tree only, no deploy, no commits.

## 1. `payout_provider.py`

- Interface extended: `PayoutProvider.deliver(to_address, usd_amount, asset="USDC", on_behalf_of=None) -> dict`.
  `FakePayoutProvider` accepts and ignores `on_behalf_of`.
- Added `BridgePayoutProvider(PayoutProvider)`:
  - Reads `BRIDGE_API_KEY`, `BRIDGE_BASE_URL` (default `https://api.sandbox.bridge.xyz`),
    `BRIDGE_CHAIN` (default `base`), `BRIDGE_SOURCE_WALLET_ID` from env via `os.getenv` —
    nothing hardcoded, and the constructor does no network I/O (only `deliver()` does).
  - Uses `requests` (already a project dependency, already used elsewhere in `server.py`) to
    `POST {BRIDGE_BASE_URL}/v0/transfers` with a fresh `uuid4()` `Idempotency-Key` header,
    `Api-Key` header, and body:
    ```json
    {
      "amount": "<usd_amount as string>",
      "on_behalf_of": "<on_behalf_of>",
      "developer_fee": "0.0",
      "source": {"payment_rail": "bridge_wallet", "currency": "usdc", "bridge_wallet_id": "<BRIDGE_SOURCE_WALLET_ID>"},
      "destination": {"payment_rail": "<BRIDGE_CHAIN>", "currency": "usdc", "to_address": "<to_address>"}
    }
    ```
  - Non-2xx response → raises `RuntimeError` including Bridge's response body (`resp.text`).
  - Maps a 2xx response to:
    ```python
    {
        "providerRef": payload["id"],
        "chain": self.chain,
        "asset": "USDC",
        "amount": usd_amount,
        "marketRateUsd": 1.0,
        "settlementTxHash": None,          # always pending at call time — async, arrives via webhook (follow-on)
        "status": payload.get("state") or "pending",
    }
    ```
  - Top-of-class comment block documents: sandbox = schema-only (no real movement, no
    testnet, no webhooks); real settlement + `settlementTxHash` need production + a
    settlement-webhook handler (follow-on, not built here); glas must pre-fund
    `BRIDGE_SOURCE_WALLET_ID` before any real transfer can succeed; creator→Bridge-customer
    KYC onboarding is a separate follow-on (this is why `on_behalf_of` may legitimately be
    `None` in test).
- `get_payout_provider()`: `PAYOUT_PROVIDER=bridge` → `BridgePayoutProvider()`; unset/`fake` →
  `FakePayoutProvider()`; anything else → `NotImplementedError`.

## 2. `server.py` — `/earnings/cash-out`

- Reads `bridge_customer_id = user_data.get("bridgeCustomerId")` from the already-fetched
  `users/{uid}` doc and passes it as `provider.deliver(..., on_behalf_of=bridge_customer_id)`.
  Comment added noting the creator→Bridge-customer/KYC onboarding flow and the settlement
  webhook are separate, not-yet-built follow-on tasks — `bridgeCustomerId` may be `None` in
  test; in production Bridge itself will reject an unverified/absent `on_behalf_of`, which is
  the correct failure mode until onboarding ships.
- Ledger `payout` row now also carries `"status": payout_result.get("status", "settled")`
  (Fake provider has no `status` key, so it defaults to `"settled"`; Bridge's `"pending"`
  passes through). `settlementTxHash` is written as-is, i.e. `None` for Bridge — the
  transactional earnings-settle (`settle_earning_usd` + `earningsLedger` write) is
  unconditional and unaffected by a pending/`None` hash.
- Response JSON also now includes `"status"` alongside the existing `settlementTxHash` and
  `availableUsd`.
- No other change to the settle/transaction/keystone logic; gates (wallet address,
  `payoutKycStatus == "verified"`, finite/positive amount, sufficient balance) untouched.

## 3. Tests — `test_payout_provider.py`

Added (all HTTP mocked via `monkeypatch.setattr(payout_provider.requests, "post", fake_post)`,
no real network):
- `test_bridge_payout_provider_deliver_success` — asserts URL
  (`https://api.sandbox.bridge.xyz/v0/transfers`), headers (`Api-Key`, non-empty
  `Idempotency-Key`, `Content-Type`), body shape (`source`/`destination`/`on_behalf_of`/
  `amount` as string), and mapped result (`providerRef="transfer_test"`,
  `settlementTxHash is None`, `status="pending"`).
- `test_bridge_payout_provider_two_calls_use_distinct_idempotency_keys` — two `deliver()`
  calls produce two different `Idempotency-Key` values.
- `test_bridge_payout_provider_raises_on_error_response` — non-2xx → `RuntimeError`
  containing Bridge's error body.
- `test_get_payout_provider_bridge_selects_bridge_provider` — `PAYOUT_PROVIDER=bridge` env
  now returns a `BridgePayoutProvider` instance (replaces the old
  `test_get_payout_provider_bridge_not_implemented`, since bridge is now implemented).
- Existing `FakePayoutProvider`/abstract-class tests kept as-is.

## Verification run

```
python -m pytest test_payout_provider.py -v   → 11 passed
python -c "import payout_provider"            → clean, no network at import time
python -c "import server"                     → clean (only pre-existing GCP ADC warnings)
python -m pytest -q                           → 77 passed
```

## What the user must do to test this in Bridge's real sandbox

1. Create a Bridge sandbox account at bridge.xyz and generate a `sk-test-...` API key.
2. In `authensnap_server/.env` set: `PAYOUT_PROVIDER=bridge`, `BRIDGE_API_KEY=sk-test-...`,
   `BRIDGE_BASE_URL=https://api.sandbox.bridge.xyz` (default, can omit), `BRIDGE_CHAIN=base`,
   `BRIDGE_SOURCE_WALLET_ID=<sandbox source wallet id>`.
3. In the Bridge sandbox dashboard/API, create a sandbox customer and run their
   `simulate_kyc_approval` step so the customer has a verified status (this becomes the
   `bridgeCustomerId` you'd store on `users/{uid}` once onboarding is built).
4. Create/fund a sandbox source wallet and use its id for `BRIDGE_SOURCE_WALLET_ID` (sandbox
   funding is simulated — no real money).
5. Remember: sandbox responses are schema-only — no webhook will ever arrive, so
   `settlementTxHash` will stay `None`/pending indefinitely in sandbox by design.
