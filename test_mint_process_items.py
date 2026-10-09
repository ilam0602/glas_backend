from server import build_mint_items


def test_build_items_mixes_inline_and_gcs():
    job = {"itemCount": 2, "itemTypes": ["photo", "video"], "itemSources": ["inline", "gcs"]}
    items = build_mint_items(
        job,
        download_bytes=lambda i: b"VIDEOBYTES",
        read_b64=lambda i: "PHOTOB64",
    )
    assert items[0] == {"image": "PHOTOB64", "mediaType": "photo"}
    assert items[1] == {"raw_bytes": b"VIDEOBYTES", "mediaType": "video"}


def test_build_items_defaults_source_inline():
    job = {"itemCount": 1, "itemTypes": ["photo"]}  # legacy job, no itemSources
    items = build_mint_items(job, download_bytes=lambda i: b"", read_b64=lambda i: "B64")
    assert items == [{"image": "B64", "mediaType": "photo"}]
