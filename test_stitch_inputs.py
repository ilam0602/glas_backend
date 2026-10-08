from server import resolve_stitch_params


def test_resolve_stitch_params_from_json():
    params = resolve_stitch_params({
        "stitchId": "st1", "clipCount": 3, "userId": "u1",
        "textOverlay": "hi", "isPrivate": True, "caption": "c"})
    assert params["stitch_id"] == "st1"
    assert params["clip_count"] == 3
    assert params["user_id"] == "u1"
    assert params["text_overlay"] == "hi"
    assert params["is_private"] is True
    assert params["caption"] == "c"


def test_resolve_stitch_params_requires_two_clips():
    import pytest
    with pytest.raises(ValueError):
        resolve_stitch_params({"stitchId": "st1", "clipCount": 1, "userId": "u1"})
