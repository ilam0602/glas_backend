"""Payout provider seam for the non-custodial rebuild.

Custody boundary: glas MUST NOT hold keys/user crypto, convert fiat<->crypto
itself, or sign a user withdrawal. A licensed PAYOUT PROVIDER (e.g. Bridge)
does the actual transmission to the user's own wallet; glas only calls
`deliver()` and records the result on the earnings ledger.

Kept deliberately free of Flask/Firestore imports so it can be imported by
scripts and tests with no side effects (no network calls at import time).
"""

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
import uuid

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class PayoutProvider:
    """Abstract payout provider: moves USD-denominated earnings to a user's
    own wallet address as an on-chain asset (e.g. USDC)."""

    def deliver(
        self,
        to_address: str,
        usd_amount: float,
        asset: str = "USDC",
        on_behalf_of: str = None,
    ) -> dict:
        """Deliver `usd_amount` USD worth of `asset` to `to_address`.

        `on_behalf_of` is the provider's own customer id for the creator
        being paid out (e.g. a Bridge `customer_id`); providers that don't
        need it (like the fake) ignore it.

        Returns a dict shaped:
            {
                "providerRef": str,        # provider's own reference/id for this transfer
                "chain": str,              # e.g. "base"
                "asset": str,              # e.g. "USDC"
                "amount": float,           # amount of `asset` delivered
                "marketRateUsd": float,    # USD price used for the conversion
                "settlementTxHash": str,   # on-chain settlement tx hash
            }
        """
        raise NotImplementedError


class FakePayoutProvider(PayoutProvider):
    """Deterministic fake provider — no network, no real money movement.
    Used until real Stripe/Bridge accounts + legal are in place. Concrete
    real-provider wiring (Stripe/Bridge) is gated on that and is NOT
    implemented here.
    """

    def deliver(
        self,
        to_address: str,
        usd_amount: float,
        asset: str = "USDC",
        on_behalf_of: str = None,
    ) -> dict:
        # on_behalf_of is intentionally ignored — the fake provider never
        # talks to a real payout network.
        ref = uuid.uuid4().hex[:16]
        return {
            "providerRef": f"fake_{ref}",
            "chain": "base",
            "asset": asset,
            "amount": usd_amount,
            "marketRateUsd": 1.0,
            "settlementTxHash": f"0xFAKE{ref}",
        }


class BridgePayoutProvider(PayoutProvider):
    """Real payout provider backed by Bridge (https://bridge.xyz).

    IMPORTANT — sandbox vs. production:
      - Sandbox (`BRIDGE_BASE_URL=https://api.sandbox.bridge.xyz`, `sk-test-`
        keys) is SCHEMA-ONLY: it validates request/response shape but does
        NOT move real money, does NOT touch a real testnet, and does NOT
        fire webhooks. It's fine for exercising this adapter's wiring, but
        it proves nothing about real settlement.
      - Bridge transfers are ASYNC: the sync POST /v0/transfers response
        never includes the on-chain settlement tx hash — that only arrives
        later via a webhook (NOT implemented here; follow-on task). So
        `deliver()` always returns `settlementTxHash: None` (pending) plus
        a `status` field taken from Bridge's `state`.
      - Before this can move real funds in production: (1) glas must
        pre-fund the `BRIDGE_SOURCE_WALLET_ID` Bridge wallet with USDC,
        (2) each creator must be onboarded as a Bridge customer and pass
        KYC (creator -> Bridge-customer/KYC onboarding is a separate
        follow-on task — `on_behalf_of` is threaded through in
        anticipation of it), and (3) the settlement webhook handler that
        back-fills `settlementTxHash` must be built.
    """

    def __init__(self):
        self.api_key = os.getenv("BRIDGE_API_KEY")
        self.base_url = os.getenv("BRIDGE_BASE_URL", "https://api.sandbox.bridge.xyz")
        self.chain = os.getenv("BRIDGE_CHAIN", "base")
        self.source_wallet_id = os.getenv("BRIDGE_SOURCE_WALLET_ID")

    def deliver(
        self,
        to_address: str,
        usd_amount: float,
        asset: str = "USDC",
        on_behalf_of: str = None,
    ) -> dict:
        idempotency_key = str(uuid.uuid4())
        body = {
            "amount": str(usd_amount),
            "on_behalf_of": on_behalf_of,
            "developer_fee": "0.0",
            "source": {
                "payment_rail": "bridge_wallet",
                "currency": "usdc",
                "bridge_wallet_id": self.source_wallet_id,
            },
            "destination": {
                "payment_rail": self.chain,
                "currency": "usdc",
                "to_address": to_address,
            },
        }
        headers = {
            "Api-Key": self.api_key,
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }
        resp = requests.post(f"{self.base_url}/v0/transfers", json=body, headers=headers)
        if not resp.ok:
            raise RuntimeError(
                f"Bridge transfer failed ({resp.status_code}): {resp.text}"
            )
        payload = resp.json()
        return {
            "providerRef": payload["id"],
            "chain": self.chain,
            "asset": "USDC",
            "amount": usd_amount,
            "marketRateUsd": 1.0,
            # The on-chain tx hash is not available synchronously — it is
            # back-filled later by the (not-yet-built) settlement webhook.
            "settlementTxHash": None,
            "status": payload.get("state") or "pending",
        }


class CrossmintPayoutProvider(PayoutProvider):
    """Real payout provider backed by Crossmint (https://crossmint.com).

    Env-driven the same way as `BridgePayoutProvider`: `CROSSMINT_API_KEY`,
    `CROSSMINT_BASE_URL` (default `https://staging.crossmint.com/api/2025-06-09`
    — staging vs. production is the same "schema-only, no real money moves"
    caveat as Bridge's sandbox), `CROSSMINT_CHAIN` (default `base`), and
    `CROSSMINT_TREASURY_WALLET` (the sender/treasury wallet locator glas pays
    out FROM). All env is read lazily inside `deliver()` so importing this
    module never touches the environment or the network.

    Wire-up caveat — recipient KYC: unlike Bridge (where `on_behalf_of` is a
    pre-onboarded Bridge customer id checked before the transfer call),
    Crossmint's `transactionType: "regulated-transfer"` triggers KYC/AML
    checks on the RECIPIENT at transfer time via Crossmint's own flow, not
    via a value glas passes in. `on_behalf_of` is accepted for interface
    parity with `PayoutProvider.deliver()` but is UNUSED here. Onboarding a
    creator through Crossmint's Users REST API (`crossmint_register_user` +
    `crossmint_create_recipient_wallet` below) happens ahead of time via
    `/payout/onboard` so the recipient is already a Crossmint user/wallet
    owner by the time a transfer is attempted — this reduces (but per
    Crossmint's own model does not eliminate) the odds of an
    `awaiting-approval`/KYC-pending transfer, which is why `deliver()` below
    retries on a KYC-pending response instead of failing immediately.
    """

    def deliver(
        self,
        to_address: str,
        usd_amount: float,
        asset: str = "USDC",
        on_behalf_of: str = None,
    ) -> dict:
        # on_behalf_of is unused — see class docstring: Crossmint handles
        # recipient KYC at transfer time, not via a passed-in customer id.
        api_key = os.getenv("CROSSMINT_API_KEY")
        base_url = os.getenv("CROSSMINT_BASE_URL", "https://staging.crossmint.com/api/2025-06-09")
        chain = os.getenv("CROSSMINT_CHAIN", "base")
        treasury_wallet = os.getenv("CROSSMINT_TREASURY_WALLET")

        body = {
            "recipient": to_address,
            "amount": str(round(usd_amount, 2)),
            "transactionType": "regulated-transfer",
        }
        headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        }
        url = f"{base_url}/wallets/{treasury_wallet}/tokens/{chain}:usdc/transfers"

        # KYC-pending retry: Crossmint runs recipient KYC/AML at transfer
        # time (see class docstring). A transfer attempted while that check
        # is still in flight comes back as an error whose body mentions the
        # check is processing, NOT a hard failure — Crossmint's own
        # quickstart handles this by retrying rather than surfacing it to
        # the caller. We mirror that: up to 5 attempts total, ~3s apart,
        # only for responses that look KYC-pending; any other error (bad
        # request, insufficient treasury balance, etc.) raises immediately
        # on the first attempt.
        kyc_pending_markers = ("kyc", "processing", "in progress", "retry")
        max_attempts = 5
        resp = None
        for attempt in range(1, max_attempts + 1):
            resp = requests.post(url, json=body, headers=headers)
            if resp.ok:
                break
            body_text_lower = (resp.text or "").lower()
            is_kyc_pending = any(marker in body_text_lower for marker in kyc_pending_markers)
            if not is_kyc_pending or attempt == max_attempts:
                raise RuntimeError(
                    f"Crossmint transfer failed ({resp.status_code}): {resp.text}"
                )
            time.sleep(3)

        payload = resp.json()
        on_chain = payload.get("onChain") or {}
        return {
            "provider": "crossmint",
            "providerRef": payload["id"],
            "chain": chain,
            "asset": "USDC",
            "amount": usd_amount,
            "marketRateUsd": 1.0,
            # None until Crossmint confirms the transfer on-chain — the
            # not-yet-built Crossmint webhook handler back-fills this later.
            "settlementTxHash": on_chain.get("txId"),
            "status": payload.get("status"),
        }


class StripePayoutProvider(PayoutProvider):
    """Payout provider backed by Stripe stablecoin payouts (Connect).

    ARCHITECTURE — deliberately different from Bridge/Crossmint. Stripe
    stablecoin payouts are a *Connect* feature:
      - Each creator is a Stripe **Express connected account**. They complete
        Stripe's KYC and link their OWN crypto wallet (Base/Polygon) inside
        the Stripe Express Dashboard — glas never collects the wallet address.
      - To pay out you create a **Transfer in USD** to the connected account
        (`destination=acct_...`); Stripe converts USD->USDC and pays it to the
        creator's linked wallet. The platform balance stays in fiat.

    So for THIS provider the abstraction's `to_address` argument is the
    creator's Stripe **connected-account id** (`acct_...`), NOT a wallet
    address (the wallet lives on Stripe's side). `on_behalf_of` is unused.

    GATING (Stripe private preview, as of 2026): stablecoin payouts require
    (1) a US-based Connect platform, (2) private-preview access granted by
    Stripe sales, and (3) the feature enabled + due-diligence approved. Until
    all three are true, a Transfer to a USDC-default connected account won't
    convert/settle. In Stripe **test mode** the Transfer call succeeds and is
    simulated (no real funds, no on-chain settlement).

    Async settlement: the sync Transfer response has no on-chain tx hash — it
    arrives later via Connect transfer/payout webhooks (follow-on task), so
    `settlementTxHash` is None (pending) here.
    """

    def deliver(
        self,
        to_address: str,
        usd_amount: float,
        asset: str = "USDC",
        on_behalf_of: str = None,
    ) -> dict:
        import stripe

        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        # `to_address` is the creator's Stripe connected-account id (acct_...).
        # Stripe amounts are integer minor units (cents); the account's default
        # currency (USDC) drives the fiat->stablecoin conversion.
        amount_cents = int(round(usd_amount * 100))
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency="usd",
            destination=to_address,
            idempotency_key=str(uuid.uuid4()),
        )
        return {
            "providerRef": transfer["id"],
            "chain": os.getenv("STRIPE_PAYOUT_CHAIN", "base"),
            "asset": "USDC",
            "amount": usd_amount,
            "marketRateUsd": 1.0,
            "settlementTxHash": None,
            "status": "pending",
        }


def stripe_create_connect_account(
    email: str, country: str = "US", metadata: dict = None
) -> str:
    """Create a Stripe Express connected account for a creator and return its
    id (`acct_...`). Step 1 of Stripe stablecoin payout onboarding: the
    creator then completes KYC + links their crypto wallet via an
    account-onboarding link (`stripe_create_account_link`). `transfers`
    capability is requested because stablecoin payouts settle via Transfers.
    `metadata` (e.g. {"uid": ...}) lets the account.updated webhook map the
    Stripe account back to the glas user.
    """
    import stripe

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    account = stripe.Account.create(
        type="express",
        country=country,
        email=email,
        business_type="individual",
        capabilities={"transfers": {"requested": True}},
        metadata=metadata or {},
    )
    return account["id"]


def stripe_create_account_link(
    account_id: str, refresh_url: str, return_url: str
) -> str:
    """Create a Stripe hosted account-onboarding link for a connected account.
    The creator opens this URL to complete KYC and link their payout wallet in
    the Stripe Express Dashboard. Returns the hosted URL.
    """
    import stripe

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return link["url"]


def crossmint_register_user(
    email: str,
    first_name: str,
    last_name: str,
    dob: str,
    country: str = "US",
) -> dict:
    """Register (or idempotently update) a creator as a Crossmint USER, via
    `PUT {base}/users/{urlencoded "email:"+email}`.

    This is step 1 of Crossmint recipient onboarding (see module-level
    rationale in `CrossmintPayoutProvider`'s docstring): Crossmint's
    `regulated-transfer` transactionType rejects raw addresses — it can only
    pay a Crossmint-managed wallet OWNED by a registered Crossmint user, so
    the recipient must be registered as a user (this function) before a
    wallet can be created for them (`crossmint_create_recipient_wallet`
    below).

    `dob` is "YYYY-MM-DD". `country` is an ISO-3166 alpha-2 code (Crossmint
    only supports a subset of countries for regulated payouts — US or
    supported EU countries per the caller's product requirements; this
    function does not itself validate the country list, it just forwards
    whatever is passed).

    Idempotent per Crossmint's API contract (PUT on a stable locator) — safe
    to call again for the same email (e.g. re-onboarding, retries).

    Env-driven the same way as `CrossmintPayoutProvider` (`CROSSMINT_API_KEY`,
    `CROSSMINT_BASE_URL`), read lazily here so importing this module never
    makes a network call. Raises `RuntimeError` (including the response
    body) on any non-2xx response.
    """
    api_key = os.getenv("CROSSMINT_API_KEY")
    base_url = os.getenv("CROSSMINT_BASE_URL", "https://staging.crossmint.com/api/2025-06-09")

    locator = urllib.parse.quote("email:" + email, safe="")
    body = {
        "userDetails": {
            "firstName": first_name,
            "lastName": last_name,
            "dateOfBirth": dob,
            "countryOfResidence": country,
        }
    }
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    url = f"{base_url}/users/{locator}"
    resp = requests.put(url, json=body, headers=headers)
    if not resp.ok:
        raise RuntimeError(
            f"Crossmint user registration failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()


def crossmint_create_recipient_wallet(email: str) -> str:
    """Create a Crossmint-managed smart wallet OWNED by the given (already
    Crossmint-registered, see `crossmint_register_user`) user, via
    `POST {base}/wallets`.

    This is step 2 of Crossmint recipient onboarding: the resulting wallet
    address is what glas stores as the creator's `walletAddress` and pays
    `regulated-transfer`s to — Crossmint rejects transfers to raw/unlinked
    addresses, only to Crossmint-managed wallets or external wallets linked
    to a Crossmint user (see module docstring / task brief for the full
    rationale).

    FLAG (unconfirmed against real staging): the minimal body below
    (`chainType`/`type`/`owner`) is what Crossmint's REST docs show as the
    happy path, but the quickstart's SDK example also configures wallet
    recovery via `config: {adminSigner: {type: "email"}}`. If real staging
    responds with a 400 requiring a signer/recovery config, add
    `"config": {"adminSigner": {"type": "email"}}` to `body` below — this
    needs to be confirmed against a real staging call before relying on it
    in production.

    Env-driven the same way as `CrossmintPayoutProvider` (`CROSSMINT_API_KEY`,
    `CROSSMINT_BASE_URL`), read lazily here so importing this module never
    makes a network call. Raises `RuntimeError` (including the response
    body) on any non-2xx response. Returns the new wallet's `address`.
    """
    api_key = os.getenv("CROSSMINT_API_KEY")
    base_url = os.getenv("CROSSMINT_BASE_URL", "https://staging.crossmint.com/api/2025-06-09")

    body = {
        "chainType": "evm",
        "type": "smart",
        "owner": "email:" + email,
    }
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    url = f"{base_url}/wallets"
    resp = requests.post(url, json=body, headers=headers)
    if not resp.ok:
        raise RuntimeError(
            f"Crossmint wallet creation failed ({resp.status_code}): {resp.text}"
        )
    payload = resp.json()
    return payload["address"]


def create_bridge_kyc_link(full_name: str, email: str) -> dict:
    """Create a Bridge KYC link so a creator can complete individual KYC
    before they're eligible for payouts (see `BridgePayoutProvider`'s
    docstring for the custody-boundary rationale — glas never does KYC
    itself, Bridge does).

    Env-driven the same way as `BridgePayoutProvider` (`BRIDGE_API_KEY`,
    `BRIDGE_BASE_URL`), read lazily here so importing this module never
    makes a network call.

    Returns `{"customerId": str, "kycLink": str, "kycStatus": str}` mapped
    from Bridge's `POST /v0/kyc_links` response (`customer_id`, `kyc_link`,
    `kyc_status`). `kyc_status` is one of Bridge's own enum values
    (`not_started|under_review|incomplete|approved|rejected`) — callers must
    NOT treat it as equivalent to glas's own `"verified"` payout gate; only
    `"approved"` should ever be translated to `"verified"`, and that
    translation belongs to the (separate, not-yet-built) KYC webhook, not
    to this function.
    """
    api_key = os.getenv("BRIDGE_API_KEY")
    base_url = os.getenv("BRIDGE_BASE_URL", "https://api.sandbox.bridge.xyz")
    idempotency_key = str(uuid.uuid4())
    body = {"full_name": full_name, "email": email, "type": "individual"}
    headers = {
        "Api-Key": api_key,
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
    }
    resp = requests.post(f"{base_url}/v0/kyc_links", json=body, headers=headers)
    if not resp.ok:
        raise RuntimeError(
            f"Bridge kyc_link creation failed ({resp.status_code}): {resp.text}"
        )
    payload = resp.json()
    return {
        "customerId": payload.get("customer_id"),
        "kycLink": payload.get("kyc_link"),
        "kycStatus": payload.get("kyc_status"),
    }


def get_payout_provider() -> PayoutProvider:
    """Select the payout provider from env. Defaults to the fake provider.

    Set PAYOUT_PROVIDER=bridge to select the real Bridge-backed provider.
    Set PAYOUT_PROVIDER=crossmint to select the real Crossmint-backed provider.
    Set PAYOUT_PROVIDER=stripe to select the Stripe stablecoin (Connect) provider.
    """
    provider_name = os.getenv("PAYOUT_PROVIDER", "fake").strip().lower()
    if provider_name in ("", "fake"):
        return FakePayoutProvider()
    if provider_name == "bridge":
        return BridgePayoutProvider()
    if provider_name == "crossmint":
        return CrossmintPayoutProvider()
    if provider_name == "stripe":
        return StripePayoutProvider()
    raise NotImplementedError(f"Unknown PAYOUT_PROVIDER: {provider_name!r}")


# Freshness window for Bridge webhook signatures, in milliseconds. A request
# whose `t` timestamp is older than this is rejected as a possible replay.
BRIDGE_WEBHOOK_MAX_AGE_MS = 300_000


def verify_bridge_webhook(raw_body: bytes, signature_header: str) -> bool:
    """Verify a Bridge webhook request is authentic. FAIL-CLOSED.

    This is the ONLY gate between an inbound HTTP request and flipping a
    user's `payoutKycStatus` to `"verified"` (which unlocks real payouts) or
    recording a payout settlement tx hash. A forgeable webhook here is
    unlimited fraudulent payouts, so every failure path below returns
    `False` — never raises, never defaults to `True`.

    Wire format (Bridge spec): header `X-Webhook-Signature` is
    `t=<timestamp_ms>,v0=<base64_rsa_signature>`. The signed string is the
    literal bytes `f"{t}.{raw_body}"` (timestamp, ASCII dot, then the exact
    raw request body bytes — NOT a re-serialized/parsed version). Verified
    as RSA-SHA256 against the PEM public key in env `BRIDGE_WEBHOOK_PUBLIC_KEY`,
    read lazily here so importing this module never touches the environment
    at import time.

    Rejects (returns False) when:
      - `signature_header` is missing/empty or doesn't parse into `t`/`v0`.
      - `BRIDGE_WEBHOOK_PUBLIC_KEY` is unset — FAIL CLOSED: never process an
        unverified webhook just because the env isn't configured yet.
      - the RSA-SHA256 signature doesn't verify against the raw body.
      - `t` is older than `BRIDGE_WEBHOOK_MAX_AGE_MS` (300s) — replay
        protection.
    """
    try:
        if not signature_header:
            return False

        public_key_pem = os.getenv("BRIDGE_WEBHOOK_PUBLIC_KEY")
        if not public_key_pem:
            # FAIL CLOSED — never process an unverified webhook.
            return False

        parts = {}
        for piece in signature_header.split(","):
            if "=" not in piece:
                return False
            key, _, value = piece.partition("=")
            parts[key.strip()] = value.strip()

        timestamp_str = parts.get("t")
        signature_b64 = parts.get("v0")
        if not timestamp_str or not signature_b64:
            return False

        timestamp_ms = int(timestamp_str)  # ValueError -> caught below -> False
        now_ms = int(time.time() * 1000)
        if now_ms - timestamp_ms > BRIDGE_WEBHOOK_MAX_AGE_MS:
            return False

        signature = base64.b64decode(signature_b64)
        # Signed string is "{t}.{raw_body}" over the exact raw bytes Bridge
        # sent — concatenate as bytes rather than decoding raw_body to text
        # first, so this can never diverge from what was actually signed.
        signed_string = timestamp_str.encode("utf-8") + b"." + raw_body

        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        public_key.verify(
            signature,
            signed_string,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        # Any parsing/decoding/verification error is a rejection, not a
        # crash — fail-closed.
        return False


# Freshness window for Crossmint (Svix) webhook timestamps, in SECONDS (the
# Svix `svix-timestamp` header is a unix timestamp in seconds, unlike
# Bridge's millisecond `t`). A request whose timestamp is older/newer than
# this tolerance is rejected as a possible replay/clock-skew forgery.
CROSSMINT_WEBHOOK_MAX_AGE_SECONDS = 300


def verify_crossmint_webhook(raw_body: bytes, headers) -> bool:
    """Verify a Crossmint webhook request is authentic (Svix signature
    scheme). FAIL-CLOSED — same trust-boundary rationale as
    `verify_bridge_webhook` above: this is the ONLY gate before recording a
    payout settlement tx hash from an inbound HTTP request, so every failure
    path returns `False`, never raises, never defaults to `True`.

    Wire format (Svix, used by Crossmint): headers `svix-id`,
    `svix-timestamp`, `svix-signature`. `svix-signature` is a space-delimited
    list of `v1,<base64_hmac_sha256>` entries (Svix supports secret
    rotation, so multiple `v1,` sigs may be present — any match is enough).
    The signed content is the literal string
    `f"{svix_id}.{svix_timestamp}.{raw_body}"` (raw_body as the exact raw
    request body bytes — NOT a re-serialized/parsed version). The signing
    secret is env `CROSSMINT_WEBHOOK_SECRET`, of the form `whsec_<base64>`;
    the HMAC key is `base64decode(<the part after "whsec_">)`. Read lazily
    here so importing this module never touches the environment at import
    time.

    `headers` is a case-insensitive dict-like object (e.g. Flask's
    `request.headers`, or a plain dict using the lowercase Svix header
    names for tests) — accessed via `.get("svix-id")` etc.

    Rejects (returns False) when:
      - `CROSSMINT_WEBHOOK_SECRET` is unset, or isn't `whsec_`-prefixed, or
        fails to base64-decode — FAIL CLOSED: never process an unverified
        webhook just because the env isn't configured yet.
      - any of `svix-id` / `svix-timestamp` / `svix-signature` is
        missing/empty.
      - `svix-timestamp` is not a valid integer, or is more than
        `CROSSMINT_WEBHOOK_MAX_AGE_SECONDS` (300s) away from now (replay /
        clock-skew protection — checked in both directions).
      - none of the `v1,<sig>` entries in `svix-signature` match the
        computed HMAC (constant-time compared).
    """
    try:
        secret = os.getenv("CROSSMINT_WEBHOOK_SECRET")
        if not secret or not secret.startswith("whsec_"):
            # FAIL CLOSED — never process an unverified webhook.
            return False
        hmac_key = base64.b64decode(secret[len("whsec_"):])

        svix_id = headers.get("svix-id")
        svix_timestamp = headers.get("svix-timestamp")
        svix_signature = headers.get("svix-signature")
        if not svix_id or not svix_timestamp or not svix_signature:
            return False

        timestamp_seconds = int(svix_timestamp)  # ValueError -> caught below -> False
        now_seconds = int(time.time())
        if abs(now_seconds - timestamp_seconds) > CROSSMINT_WEBHOOK_MAX_AGE_SECONDS:
            return False

        signed_content = f"{svix_id}.{svix_timestamp}.".encode("utf-8") + raw_body
        expected_sig = base64.b64encode(
            hmac.new(hmac_key, signed_content, hashlib.sha256).digest()
        ).decode("utf-8")

        for entry in svix_signature.split():
            version, _, candidate_sig = entry.partition(",")
            if version != "v1" or not candidate_sig:
                continue
            if hmac.compare_digest(candidate_sig, expected_sig):
                return True
        return False
    except Exception:
        # Any parsing/decoding/verification error is a rejection, not a
        # crash — fail-closed.
        return False
