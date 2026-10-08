from server import (
    mint_upload_object_path, story_object_path, stitch_object_path,
    content_length_range_header, validate_upload_url_request, _normalize_mint_items,
)

MAX = 150 * 1024 * 1024

def test_object_paths():
    assert mint_upload_object_path("abc", 0) == "mint_uploads/abc/0.mp4"
    assert story_object_path("u1", "s1") == "stories/u1/s1.mp4"
    assert stitch_object_path("st1", 2) == "stitch_uploads/st1/2.mp4"

def test_content_length_range_header():
    assert content_length_range_header(MAX) == {"X-Goog-Content-Length-Range": f"0,{MAX}"}

def test_validate_upload_url_request_ok():
    data = {"userId": "u1", "contentType": "video/mp4", "size": 10_000_000}
    assert validate_upload_url_request(data, "u1", MAX) == (None, 200)

def test_validate_upload_url_request_uid_mismatch():
    data = {"userId": "u1", "contentType": "video/mp4", "size": 10}
    err, status = validate_upload_url_request(data, "someone_else", MAX)
    assert status == 401 and err

def test_validate_upload_url_request_bad_content_type():
    data = {"userId": "u1", "contentType": "image/png", "size": 10}
    err, status = validate_upload_url_request(data, "u1", MAX)
    assert status == 400 and err

def test_validate_upload_url_request_too_large():
    data = {"userId": "u1", "contentType": "video/mp4", "size": MAX + 1}
    err, status = validate_upload_url_request(data, "u1", MAX)
    assert status == 413 and err

def test_normalize_tags_inline_source():
    items, carousel = _normalize_mint_items({"image": "b64", "mediaType": "photo"})
    assert items == [{"image": "b64", "mediaType": "photo", "source": "inline"}]
    assert carousel is False

def test_normalize_single_gcs_video():
    items, carousel = _normalize_mint_items({"source": "gcs", "mediaType": "video"})
    assert items == [{"image": None, "mediaType": "video", "source": "gcs"}]
    assert carousel is False

def test_normalize_media_item_gcs_needs_no_image():
    items, carousel = _normalize_mint_items({"media": [{"source": "gcs", "mediaType": "video"}]})
    assert items == [{"image": None, "mediaType": "video", "source": "gcs"}]
    assert carousel is True
