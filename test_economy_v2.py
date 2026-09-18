import pytest
from economy_v2 import (
    spend_view_credits, accrue_earning_usd, settle_earning_usd,
    view_credit_row, earning_row, InsufficientCredits, InsufficientEarnings,
    creator_earning_usd,
)

def test_spend_view_credits_ok():
    assert spend_view_credits(10, 4) == 6

def test_spend_view_credits_insufficient():
    with pytest.raises(InsufficientCredits):
        spend_view_credits(3, 4)

def test_accrue_earning_usd():
    assert accrue_earning_usd(1.00, 5.00, 0.50) == (1.50, 5.50)

def test_settle_earning_usd_ok():
    assert settle_earning_usd(1.50, 1.50) == 0.0

def test_settle_earning_usd_insufficient():
    with pytest.raises(InsufficientEarnings):
        settle_earning_usd(0.25, 1.00)

def test_view_credit_row_shape():
    r = view_credit_row("u1", -1, "view_spend", token_id="7")
    assert r["uid"] == "u1" and r["delta"] == -1 and r["reason"] == "view_spend" and "createdAt" in r

def test_earning_row_payout_shape():
    payout = {"provider": "acme", "providerRef": "px1", "chain": "base",
              "asset": "GLAS", "amount": 123.4, "marketRateUsd": 0.0081, "settlementTxHash": "0xabc"}
    r = earning_row("u1", -1.50, "payout", payout=payout)
    assert r["deltaUsd"] == -1.50 and r["payout"]["settlementTxHash"] == "0xabc"

def test_settle_earning_usd_rejects_nan():
    with pytest.raises(InsufficientEarnings):
        settle_earning_usd(10.0, float("nan"))

def test_spend_view_credits_rejects_nan():
    with pytest.raises(InsufficientCredits):
        spend_view_credits(10, float("nan"))

def test_creator_earning_usd():
    assert creator_earning_usd(10000) == pytest.approx(2.0 * 0.5)
    assert creator_earning_usd(1) == pytest.approx(0.0001)
