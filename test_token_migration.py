from migrate_token_buckets import migrate_balance_doc


def test_legacy_doc_becomes_all_purchased():
    out = migrate_balance_doc({"balance": 42, "lastOnChainBalance": 30})
    assert out["purchasedBalance"] == 42
    assert out["earnedBalance"] == 0
    assert out["lastOnChainBalance"] == 30  # preserved
    assert out["balance"] == 42            # retained for rollback window


def test_migration_is_idempotent():
    once = migrate_balance_doc({"balance": 42})
    twice = migrate_balance_doc(once)
    assert twice == once


def test_already_migrated_earned_preserved():
    out = migrate_balance_doc({"purchasedBalance": 10, "earnedBalance": 7, "balance": 10})
    assert out["purchasedBalance"] == 10
    assert out["earnedBalance"] == 7  # must NOT be reset to 0


def test_empty_doc():
    out = migrate_balance_doc({})
    assert out["purchasedBalance"] == 0
    assert out["earnedBalance"] == 0


def test_mixed_legacy_plus_earned_preserved():
    # C1 regression: a legacy doc that already received an earning credit
    # (earnedBalance present, purchasedBalance absent) must migrate the legacy
    # `balance` to purchased WITHOUT wiping the already-written earnedBalance.
    out = migrate_balance_doc({"balance": 500, "earnedBalance": 0.5})
    assert out["purchasedBalance"] == 500
    assert out["earnedBalance"] == 0.5
