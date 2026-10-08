import server
from server import app, resolve_stitch_params


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


class _FakeBlob:
    def __init__(self, path, registry):
        self._path = path
        self._registry = registry

    def exists(self):
        return True

    def download_to_filename(self, path):
        # No-op: ffmpeg is mocked, so the local file never needs real bytes.
        pass

    def delete(self):
        self._registry.append(self._path)


class _FakeBucket:
    def __init__(self, registry):
        self._registry = registry

    def blob(self, path):
        return _FakeBlob(path, self._registry)


def test_stitch_endpoint_success_cleans_up_gcs_clips(monkeypatch):
    """Drives stitch_videos end-to-end on the mint=false path with mocked
    ffmpeg + GCS. Asserts the response is 200 and that the finally-block
    cleanup deletes each uploaded clip blob. This exercises the shared `p`/
    `params` variable across the download loop, the unchanged ffmpeg body
    (which rebinds `p` in `for p in input_paths`), and the finally cleanup —
    the path that a pure resolve_stitch_params unit test cannot cover."""
    deleted = []
    monkeypatch.setattr(server, "gcs_bucket", _FakeBucket(deleted))

    def fake_run(cmd, **kwargs):
        # ffmpeg writes its output to the last positional arg; create it so the
        # subsequent `open(output_path, "rb")` read succeeds.
        with open(cmd[-1], "wb") as f:
            f.write(b"STITCHED")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    r = app.test_client().post(
        "/stitch",
        json={"stitchId": "st9", "clipCount": 3, "userId": "u1", "mint": "false"},
    )

    assert r.status_code == 200
    assert deleted == [
        "stitch_uploads/st9/0.mp4",
        "stitch_uploads/st9/1.mp4",
        "stitch_uploads/st9/2.mp4",
    ]
