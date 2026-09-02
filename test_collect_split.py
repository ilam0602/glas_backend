from server import collect_split, PLATFORM_COLLECT_CUT, DEFAULT_COLLECT_PRICE

def test_default_price_split():
    assert collect_split(5) == (5, 4.5, 0.5)

def test_price_below_platform_cut_gives_platform_all():
    # price 0.3 < 0.5 -> platform takes min(0.5, 0.3)=0.3, creator 0
    assert collect_split(0.3) == (0.3, 0.0, 0.3)

def test_zero_price():
    assert collect_split(0) == (0, 0.0, 0.0)

def test_negative_clamped_to_zero():
    assert collect_split(-4) == (0, 0.0, 0.0)

def test_platform_cut_constant():
    assert PLATFORM_COLLECT_CUT == 0.5
    assert DEFAULT_COLLECT_PRICE == 5
