from migrate_noncustodial import migrate_post, migrate_balances


def test_legacy_post_gets_flagged():
    out = migrate_post({"tokenId": "1", "userId": "u1"})
    assert out["custodyEra"] == "legacy"
    assert "legacyMigratedAt" in out
    assert out["tokenId"] == "1"  # other fields preserved
    assert out["userId"] == "u1"


def test_post_migration_is_idempotent():
    once = migrate_post({"tokenId": "1"})
    twice = migrate_post(once)
    assert twice == once


def test_already_noncustodial_post_left_unchanged():
    # A post created under the new flow already carries custodyEra —
    # migrate_post must not touch it (no legacyMigratedAt added).
    out = migrate_post({"tokenId": "2", "custodyEra": "noncustodial"})
    assert out["custodyEra"] == "noncustodial"
    assert "legacyMigratedAt" not in out


def test_empty_post_gets_flagged():
    out = migrate_post({})
    assert out["custodyEra"] == "legacy"
    assert "legacyMigratedAt" in out


def test_migrate_balances_returns_increment_deltas():
    # The pure fn returns amounts to be ADDED (via FirestoreIncrement in a
    # per-uid txn), not absolute values to overwrite: purchasedBalance is the
    # VW delta for viewCredits.balance, legacyUsd is the USD delta for both
    # earnings.availableUsd and .lifetimeUsd.
    doc = {"uid": "u1", "purchasedBalance": 100, "earnedBalance": 50}
    result = migrate_balances(doc, snapshot_rate=0.0002)

    assert result["uid"] == "u1"
    assert result["purchasedBalance"] == 100
    assert result["legacyUsd"] == 50 * 0.0002


def test_migrate_balances_falls_back_to_legacy_balance_field():
    # A doc that hasn't been through the purchased/earned two-bucket
    # migration yet only has the legacy `balance` field.
    doc = {"uid": "u2", "balance": 30}
    result = migrate_balances(doc, snapshot_rate=0.0002)

    assert result["purchasedBalance"] == 30
    assert result["legacyUsd"] == 0  # no earnedBalance yet


def test_migrate_balances_earnings_ledger_row():
    doc = {"uid": "u3", "purchasedBalance": 10, "earnedBalance": 200}
    result = migrate_balances(doc, snapshot_rate=0.0005)
    row = result["earningsLedgerRow"]

    assert row["uid"] == "u3"
    assert row["deltaUsd"] == 200 * 0.0005  # matches the additive USD delta
    assert row["deltaUsd"] == result["legacyUsd"]
    assert row["reason"] == "legacy_migration"
    assert "createdAt" in row


def test_migrate_balances_zero_rate_yields_zero_earnings():
    doc = {"uid": "u4", "purchasedBalance": 5, "earnedBalance": 999}
    result = migrate_balances(doc, snapshot_rate=0.0)

    assert result["legacyUsd"] == 0
    assert result["earningsLedgerRow"]["deltaUsd"] == 0
    assert result["purchasedBalance"] == 5  # view-credit delta still preserved


def test_migrate_balances_empty_doc():
    result = migrate_balances({}, snapshot_rate=0.0002)

    assert result["purchasedBalance"] == 0
    assert result["legacyUsd"] == 0
    assert result["earningsLedgerRow"]["uid"] is None
