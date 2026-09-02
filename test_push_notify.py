from server import group_for_type, pref_allows


def test_group_for_type_maps_all_types():
    assert group_for_type("like") == "likes"
    assert group_for_type("comment") == "comments"
    assert group_for_type("reply") == "comments"
    assert group_for_type("comment_like") == "comments"
    assert group_for_type("mention") == "mentions"
    assert group_for_type("follow") == "follows"
    assert group_for_type("collect") == "collects"
    assert group_for_type("dm") == "dms"


def test_group_for_type_unknown_is_none():
    assert group_for_type("bogus") is None


def test_pref_allows_missing_key_defaults_true():
    assert pref_allows({}, "like") is True
    assert pref_allows({"comments": True}, "like") is True


def test_pref_allows_false_only_when_group_disabled():
    assert pref_allows({"likes": False}, "like") is False
    assert pref_allows({"comments": False}, "reply") is False
    assert pref_allows({"likes": False}, "comment") is True


def test_pref_allows_unknown_type_is_false():
    # No group -> nothing to send.
    assert pref_allows({}, "bogus") is False
