from server import autoflag_should_flag


def test_baseline_flags_low_like_post():
    assert autoflag_should_flag(5, 0, None) is True
    assert autoflag_should_flag(5, 49, None) is True


def test_ten_percent_boundary_is_strict():
    # 50 likes * 0.10 == 5.0; 5 is NOT > 5.0
    assert autoflag_should_flag(5, 50, None) is False
    # 100 likes: need > 10, so 10 no, 11 yes
    assert autoflag_should_flag(10, 100, None) is False
    assert autoflag_should_flag(11, 100, None) is True


def test_below_baseline_never_flags():
    assert autoflag_should_flag(4, 0, None) is False


def test_approved_post_is_immune():
    assert autoflag_should_flag(100, 0, "approved") is False


def test_pending_or_rejected_still_evaluated():
    assert autoflag_should_flag(5, 0, "pending") is True
    assert autoflag_should_flag(5, 0, "rejected") is True
