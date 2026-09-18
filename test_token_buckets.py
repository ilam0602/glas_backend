import pytest
from server import (
    read_balances, spend_purchased, credit_earned, debit_earned,
    token_ledger_row, InsufficientFunds,
)

def test_read_balances_new_shape():
    assert read_balances({"purchasedBalance": 30, "earnedBalance": 12}) == (30, 12)

def test_read_balances_legacy_fallback():
    # legacy doc: only `balance` -> all purchased, earned 0
    assert read_balances({"balance": 42}) == (42, 0)

def test_read_balances_empty():
    assert read_balances({}) == (0, 0)
    assert read_balances(None) == (0, 0)

def test_read_balances_mixed_legacy_plus_earned():
    # C1 regression: a legacy doc that already received an earning credit
    # (earnedBalance present, purchasedBalance still absent) must NOT strand
    # the legacy `balance` as purchased=0.
    assert read_balances({"balance": 500, "earnedBalance": 0.5}) == (500, 0.5)

def test_spend_debits_purchased_only():
    assert spend_purchased(10, 5, 4) == (6, 5)  # earned untouched

def test_spend_insufficient_raises_and_never_touches_earned():
    with pytest.raises(InsufficientFunds):
        spend_purchased(3, 100, 4)  # cannot dip into earned

def test_credit_earned_never_touches_purchased():
    assert credit_earned(10, 5, 0.5) == (10, 5.5)

def test_debit_earned_for_withdrawal_only():
    assert debit_earned(10, 5, 5) == (10, 0)

def test_debit_earned_insufficient_raises_and_never_touches_purchased():
    with pytest.raises(InsufficientFunds):
        debit_earned(100, 3, 4)  # cannot withdraw purchased

def test_ledger_row_shape():
    row = token_ledger_row("u1", "earned", 0.5, "view_earning", related_user_id="u2", token_id="7")
    assert row["userId"] == "u1"
    assert row["bucket"] == "earned"
    assert row["delta"] == 0.5
    assert row["reason"] == "view_earning"
    assert row["relatedUserId"] == "u2"
    assert row["tokenId"] == "7"
    assert "createdAt" in row
