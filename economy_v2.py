"""Pure economic logic for the non-custodial rebuild.

Two independent economic systems live here, and this module enforces that
independence structurally: view-credit functions operate ONLY on credit
balances, and earnings functions operate ONLY on USD earnings values. No
function in this module accepts both a credit balance and an earnings value,
so there is no code path here that could move value between the two ledgers.

Kept deliberately free of Flask/web3/firebase imports so it can be imported
by scripts and tests with no side effects.
"""

import math
from datetime import datetime, timezone

# --- Pricing constants -----------------------------------------------------

USD_PER_VIEW = 0.0002
CREATOR_SHARE = 0.5


def creator_earning_usd(n_views=1):
    """USD a creator earns for n_views (default 1)."""
    return USD_PER_VIEW * CREATOR_SHARE * n_views


# --- Exceptions --------------------------------------------------------------

class InsufficientCredits(Exception):
    """Raised when a view-credit spend would exceed the available balance."""


class InsufficientEarnings(Exception):
    """Raised when an earnings settlement would exceed available earnings."""


# --- View credits (spend-only, IAP-funded, never withdrawable) --------------

def spend_view_credits(balance, amount):
    """Return the new credit balance after spending `amount`.

    Raises InsufficientCredits if balance < amount. Operates only on
    view-credit balances; never touches earnings.

    Also rejects non-finite (NaN/inf) or non-positive amounts, so a bad
    amount can never corrupt the balance even if a caller forgets to
    validate (defense-in-depth: NaN compares False to everything, which
    would otherwise slip past a bare `balance < amount` check).
    """
    if not math.isfinite(amount) or amount <= 0 or balance < amount:
        raise InsufficientCredits(
            f"balance {balance} < amount {amount}"
        )
    return balance - amount


# --- Earnings (USD unit of account, settled via licensed payout provider) --

def accrue_earning_usd(available_usd, lifetime_usd, delta_usd):
    """Return (new_available_usd, new_lifetime_usd) after accruing delta_usd.

    Operates only on earnings values; never touches view credits.
    """
    return (available_usd + delta_usd, lifetime_usd + delta_usd)


def settle_earning_usd(available_usd, amount_usd):
    """Return the new available_usd after settling amount_usd via payout.

    Raises InsufficientEarnings if available_usd < amount_usd. Operates only
    on earnings values; never touches view credits.

    Also rejects non-finite (NaN/inf) or non-positive amounts. Critical:
    a NaN amount would otherwise slip past a bare `available_usd < amount_usd`
    check (NaN compares False to everything) and write NaN back to
    availableUsd, permanently breaking the balance ceiling.
    """
    if not math.isfinite(amount_usd) or amount_usd <= 0 or available_usd < amount_usd:
        raise InsufficientEarnings(
            f"available_usd {available_usd} < amount_usd {amount_usd}"
        )
    return available_usd - amount_usd


# --- Ledger row builders -----------------------------------------------------

def view_credit_row(uid, delta, reason, token_id=None, iap_transaction_id=None):
    """Build a view-credit ledger row (audit trail for the credits ledger)."""
    return {
        "uid": uid,
        "delta": delta,
        "reason": reason,
        "tokenId": token_id,
        "iapTransactionId": iap_transaction_id,
        "createdAt": datetime.now(timezone.utc),
    }


def earning_row(uid, delta_usd, reason, related_uid=None, token_id=None, payout=None):
    """Build an earnings ledger row (audit trail for the earnings ledger)."""
    return {
        "uid": uid,
        "deltaUsd": delta_usd,
        "reason": reason,
        "relatedUid": related_uid,
        "tokenId": token_id,
        "payout": payout,
        "createdAt": datetime.now(timezone.utc),
    }
