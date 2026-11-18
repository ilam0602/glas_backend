"""Pre-storage CSAM screening: provider-agnostic hash-match screening plus
the quarantine -> incident -> admin-alert match path.

See docs/specs/2026-10-09-csam-screening-quarantine-report-design.md for the
full design and the legal rationale (18 U.S.C. 2258A preserve/report duties).
This is Phase 1: one interim provider (WebPurify + its PhotoDNA add-on),
manual NCMEC reporting, "disabled" by default so the feature ships dark
until a vendor is signed and the account exists.

This module takes Firestore/GCS client handles as plain parameters rather
than importing server.py, so it has zero import-time side effects (no live
Firebase/GCS/web3 clients get created just by `import csam_screening`) and
is fully unit-testable in isolation. server.py wires it up with its own
`firestore_db` / `gcs_client` / `gcs_bucket` globals.
"""
import hashlib
import json
import logging
import math
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config (env-driven module attributes; tests monkeypatch these directly,
# e.g. `monkeypatch.setattr(csam_screening, "CSAM_SCREEN_PROVIDER",
# "webpurify")`, the same convention server.py uses for its own env-derived
# globals -- see MAX_VIDEO_BYTES/MINT_WORKER_SECRET in server.py).
# ---------------------------------------------------------------------------

CSAM_SCREEN_PROVIDER = os.getenv("CSAM_SCREEN_PROVIDER", "disabled")
CSAM_FAIL_MODE = os.getenv("CSAM_FAIL_MODE", "open")  # "open" | "closed"
CSAM_QUARANTINE_BUCKET = os.getenv("CSAM_QUARANTINE_BUCKET", "glas-csam-quarantine")
CSAM_RETENTION_DAYS = int(os.getenv("CSAM_RETENTION_DAYS", "120"))
WEBPURIFY_API_KEY = os.getenv("WEBPURIFY_API_KEY")
CSAM_MAX_VIDEO_FRAMES = int(os.getenv("CSAM_MAX_VIDEO_FRAMES", "60"))
CSAM_POLL_SECONDS = int(os.getenv("CSAM_POLL_SECONDS", "20"))

# Reused (not duplicated) from server.py's own admin allow-list env var --
# this module reads the env directly rather than importing server.py's
# `_get_admin_emails` to avoid a circular import.
MODERATION_ADMIN_EMAILS_ENV = "MODERATION_ADMIN_EMAILS"

# ---------------------------------------------------------------------------
# WebPurify REST API constants.
#
# VERIFIED from WebPurify's PUBLIC image-moderation documentation as of
# 2026-10 (no account exists yet -- RE-VERIFY every value here against the
# account dashboard docs once the account is created, before flipping
# CSAM_SCREEN_PROVIDER to "webpurify" in any real environment):
#   - REST endpoint + `format=json` + `api_key` query param.
#   - Images are submitted BY URL (`imgurl`), method `webpurify.live.imgcheck`,
#     which returns an image id ("img_id") used to poll status.
#   - Status is polled via `webpurify.live.imgstatus` -> "pending" / "approved"
#     / "declined", and with the PhotoDNA add-on enabled, a known-hash match
#     additionally returns "flagged".
# The response-shape helpers below (_extract_webpurify_img_id/_status) are
# intentionally tolerant of both a `{"rsp": {...}}` wrapper and a flat
# top-level shape, since the exact envelope is one of the things to
# re-verify.
# ---------------------------------------------------------------------------

WEBPURIFY_BASE_URL = "https://im-api1.webpurify.com/services/rest/"
WEBPURIFY_FORMAT = "json"
WEBPURIFY_SUBMIT_METHOD = "webpurify.live.imgcheck"
WEBPURIFY_STATUS_METHOD = "webpurify.live.imgstatus"
WEBPURIFY_STATUS_PENDING = "pending"
WEBPURIFY_STATUS_APPROVED = "approved"
WEBPURIFY_STATUS_DECLINED = "declined"
WEBPURIFY_STATUS_FLAGGED = "flagged"  # PhotoDNA known-CSAM-hash match add-on
WEBPURIFY_POLL_INTERVAL_SECONDS = 2
WEBPURIFY_SIGNED_URL_TTL_SECONDS = 15 * 60
WEBPURIFY_REQUEST_TIMEOUT_SECONDS = 10


def _redact_secrets(text: str) -> str:
    """Defensive backstop: strip the WebPurify API key out of any string bound
    for a log line or a persisted provider_ref. WEBPURIFY_API_KEY is sent as a
    query param (the vendor's required auth shape -- see the WebPurify adapter
    below), so a requests exception's message embeds it in the request URL, and
    provider_ref is written to Firestore (csamIncidents/csamScreenTickets).
    Never let the key reach either. Reads the module global at call time so it
    honours a monkeypatched key in tests."""
    if not text:
        return text
    key = WEBPURIFY_API_KEY
    if key:
        text = text.replace(key, "[REDACTED]")
    return text


def _exc_reason(e: Exception) -> str:
    """A secret-safe one-line description of an exception for logs/provider_ref.
    A requests exception embeds the full request URL in str(e) -- and that URL
    carries BOTH the api_key query param AND the short-lived signed GCS URL
    (which has its own credentials in its query string) -- so this never
    stringifies the raw exception into anything persisted or logged. It reports
    the exception class plus an HTTP status code when available, then redacts
    the api_key as a final backstop."""
    name = type(e).__name__
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    reason = f"{name} (HTTP {status})" if status is not None else name
    return _redact_secrets(reason)


def _redact_payload(payload):
    """Run a raw vendor response (kept in provider_ref as evidence) through
    _redact_secrets. The WebPurify response shape is unverified pre-account;
    some APIs echo the request back in the body, which would carry the
    api_key. JSON round-trip so every nested string is covered; on any
    serialization surprise, drop the payload rather than risk persisting an
    unredacted secret."""
    try:
        return json.loads(_redact_secrets(json.dumps(payload)))
    except Exception:
        return {"reason": "payload withheld (unserializable)"}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ScreenResult:
    """Outcome of screening one image or video.

    verdict:
      "clear"        -- no known-hash match found.
      "known_match"  -- a hash match (treated as certain CSAM).
      "pending"      -- the vendor hasn't resolved the check yet (async
                        human/PhotoDNA review); caller may proceed to publish
                        but MUST persist a ticket for the reconcile sweep.
      "error"        -- the provider call itself failed/timed out; caller
                        applies CSAM_FAIL_MODE.
    """

    verdict: str
    provider: str
    provider_ref: dict = field(default_factory=dict)
    frames_screened: int = 0

    @property
    def is_match(self) -> bool:
        return self.verdict == "known_match"


def _disabled_result() -> ScreenResult:
    return ScreenResult(verdict="clear", provider="disabled")


def should_fail_closed() -> bool:
    """Whether a provider "error" verdict should abort the upload (True) or
    be logged loudly and allowed to proceed (False), per CSAM_FAIL_MODE."""
    return CSAM_FAIL_MODE == "closed"


def is_enabled() -> bool:
    """Cheap pre-check so hot-path callers (e.g. the mint pipeline, which may
    be handling a 150MB video) can skip ALL work -- base64 decoding, frame
    extraction, building a context dict -- before even constructing the
    bytes to screen, when the feature is shipping dark.
    screen_image_bytes/screen_video_bytes still independently no-op if called
    anyway; this is purely a perf guard for callers that would otherwise do
    real work just to hand it to a function that immediately discards it."""
    return CSAM_SCREEN_PROVIDER != "disabled"


# ---------------------------------------------------------------------------
# Public screening API
# ---------------------------------------------------------------------------


def screen_image_bytes(
    image_bytes: bytes,
    content_type: str,
    context: Optional[dict] = None,
    *,
    gcs_bucket=None,
    sign_url_fn: Optional[Callable[[Any, str, timedelta], str]] = None,
    existing_blob_path: Optional[str] = None,
) -> ScreenResult:
    """Screen one image for a known-CSAM hash match.

    `context` carries at least {"uid": ..., "source": "mint"|"story"|
    "avatar"|...} for incident/ticket records -- it is opaque to this
    function otherwise.

    `gcs_bucket` / `sign_url_fn` are only used by the "webpurify" provider,
    which needs a short-lived, fetchable URL for the image: `gcs_bucket` is a
    google-cloud-storage Bucket-like object (`.blob(path)` ->
    `.upload_from_string()`/`.delete()`), and `sign_url_fn(blob, method,
    expiration) -> str` mirrors server.py's `generate_signed_url`. Both are
    ignored (never touched) when CSAM_SCREEN_PROVIDER is "disabled", so the
    no-op path never requires real GCS wiring.

    `existing_blob_path`: when the exact `image_bytes` being screened already
    sit at this path in `gcs_bucket` (as the correct content-type, fetchable
    as-is), pass it so the webpurify adapter signs that object directly
    instead of uploading a redundant `csam-screen-tmp/` copy. The existing
    object is NOT deleted afterward (it is the caller's real data, not a
    temp object this function created) -- only the caller owns its lifecycle.
    """
    provider = CSAM_SCREEN_PROVIDER
    if provider == "disabled":
        return _disabled_result()
    if provider == "webpurify":
        return _screen_image_webpurify(
            image_bytes, content_type, gcs_bucket, sign_url_fn, existing_blob_path
        )
    logger.error("[csam] unknown CSAM_SCREEN_PROVIDER=%r; treating as error", provider)
    return ScreenResult(
        verdict="error", provider=provider,
        provider_ref={"reason": f"unknown provider {provider!r}"},
    )


def screen_video_bytes(
    video_bytes: bytes,
    content_type: str,
    context: Optional[dict] = None,
    *,
    gcs_bucket=None,
    sign_url_fn: Optional[Callable[[Any, str, timedelta], str]] = None,
) -> ScreenResult:
    """Screen a video via frame sampling (image-only providers have no
    native video hash matching -- see spec Sec 3 "Video handling (interim)").

    Samples at 1fps plus the first and last frame, capped at
    CSAM_MAX_VIDEO_FRAMES (spread evenly across the duration when the cap
    binds). Any single frame matching = the whole video matches. If no frame
    matches but at least one frame is still "pending", the whole video is
    "pending" (its provider_ref lists exactly the unresolved frames, so a
    pending ticket only needs to re-poll those).
    """
    provider = CSAM_SCREEN_PROVIDER
    if provider == "disabled":
        return _disabled_result()

    try:
        frames = _extract_video_frames(video_bytes)
    except Exception as e:
        logger.error("[csam] video frame extraction failed: %s", e)
        return ScreenResult(
            verdict="error", provider=provider,
            provider_ref={"reason": f"frame extraction failed: {e}"},
        )
    if not frames:
        return ScreenResult(
            verdict="error", provider=provider,
            provider_ref={"reason": "no frames extracted"},
        )

    pending_frames = []
    screened = 0
    for index, frame_bytes in enumerate(frames):
        result = screen_image_bytes(
            frame_bytes, "image/jpeg", context,
            gcs_bucket=gcs_bucket, sign_url_fn=sign_url_fn,
        )
        screened += 1
        if result.verdict == "known_match":
            return ScreenResult(
                verdict="known_match", provider=result.provider,
                provider_ref={"matchedFrameIndex": index, "frame": result.provider_ref},
                frames_screened=screened,
            )
        if result.verdict == "error":
            return ScreenResult(
                verdict="error", provider=result.provider,
                provider_ref={"failedFrameIndex": index, "frame": result.provider_ref},
                frames_screened=screened,
            )
        if result.verdict == "pending":
            pending_frames.append({"frameIndex": index, "ref": result.provider_ref})
        # "clear" -> keep going; a later frame could still match.

    if pending_frames:
        return ScreenResult(
            verdict="pending", provider=provider,
            provider_ref={"pendingFrames": pending_frames},
            frames_screened=screened,
        )
    return ScreenResult(verdict="clear", provider=provider, frames_screened=screened)


def extract_pending_img_ids(screen_result: ScreenResult) -> list:
    """Pull the WebPurify ticket id(s) needed to later re-poll a "pending"
    ScreenResult: a single image's `imgId`, or a video's unresolved frames'
    `imgId`s. Isolates WebPurify's response shape inside this module so
    server.py (the ticket writer) stays provider-agnostic."""
    ref = screen_result.provider_ref or {}
    if "pendingFrames" in ref:
        return [
            pf["ref"]["imgId"]
            for pf in ref["pendingFrames"]
            if pf.get("ref", {}).get("imgId")
        ]
    img_id = ref.get("imgId")
    return [img_id] if img_id else []


# ---------------------------------------------------------------------------
# WebPurify adapter
# ---------------------------------------------------------------------------


def _screen_image_webpurify(
    image_bytes, content_type, gcs_bucket, sign_url_fn, existing_blob_path=None
) -> ScreenResult:
    if gcs_bucket is None or sign_url_fn is None:
        return ScreenResult(
            verdict="error", provider="webpurify",
            provider_ref={"reason": "gcs_bucket/sign_url_fn not configured"},
        )
    if not WEBPURIFY_API_KEY:
        return ScreenResult(
            verdict="error", provider="webpurify",
            provider_ref={"reason": "WEBPURIFY_API_KEY not configured"},
        )

    if existing_blob_path is not None:
        # The bytes already live in gcs_bucket at this path (as the correct
        # content-type) -- sign that object directly rather than uploading a
        # redundant csam-screen-tmp/ copy. It is the caller's real data, so
        # it is never deleted here (contrast the temp-object branch below).
        blob = gcs_bucket.blob(existing_blob_path)
        try:
            signed_url = sign_url_fn(
                blob, "GET", timedelta(seconds=WEBPURIFY_SIGNED_URL_TTL_SECONDS)
            )
            return _webpurify_submit_and_poll(signed_url)
        except Exception as e:
            reason = _exc_reason(e)
            logger.error("[csam] webpurify screening (existing blob) failed: %s", reason)
            return ScreenResult(verdict="error", provider="webpurify", provider_ref={"reason": reason})

    tmp_path = f"csam-screen-tmp/{uuid.uuid4().hex}"
    blob = gcs_bucket.blob(tmp_path)
    try:
        blob.upload_from_string(image_bytes, content_type=content_type)
        signed_url = sign_url_fn(
            blob, "GET", timedelta(seconds=WEBPURIFY_SIGNED_URL_TTL_SECONDS)
        )
        return _webpurify_submit_and_poll(signed_url)
    except Exception as e:
        reason = _exc_reason(e)
        logger.error("[csam] webpurify screening failed: %s", reason)
        return ScreenResult(verdict="error", provider="webpurify", provider_ref={"reason": reason})
    finally:
        try:
            blob.delete()
        except Exception as e:
            # Never let a cleanup failure mask the real screening result --
            # just log it loudly (a stray temp object is a minor cost;
            # dropping a screening result would not be). Redact defensively:
            # a GCS error can echo back the signed URL / object path.
            logger.error(
                "[csam] failed to delete temp screening object %s: %s",
                tmp_path, _redact_secrets(str(e)),
            )


def _webpurify_submit_and_poll(image_url: str) -> ScreenResult:
    """Submit `image_url` via webpurify.live.imgcheck, then poll
    webpurify.live.imgstatus every WEBPURIFY_POLL_INTERVAL_SECONDS up to
    CSAM_POLL_SECONDS. PhotoDNA matching is automated and should resolve
    fast; the timeout is only a guard against a stuck/slow vendor queue."""
    submit_resp = requests.get(
        WEBPURIFY_BASE_URL,
        params={
            "api_key": WEBPURIFY_API_KEY,
            "method": WEBPURIFY_SUBMIT_METHOD,
            "imgurl": image_url,
            "format": WEBPURIFY_FORMAT,
        },
        timeout=WEBPURIFY_REQUEST_TIMEOUT_SECONDS,
    )
    submit_resp.raise_for_status()
    submit_data = submit_resp.json()
    img_id = _extract_webpurify_img_id(submit_data)
    if not img_id:
        return ScreenResult(
            verdict="error", provider="webpurify",
            provider_ref={"submit": _redact_payload(submit_data), "reason": "no img_id returned"},
        )

    deadline = time.monotonic() + CSAM_POLL_SECONDS
    while True:
        status, raw = _webpurify_status(img_id)
        if status == WEBPURIFY_STATUS_FLAGGED:
            return ScreenResult(
                verdict="known_match", provider="webpurify",
                provider_ref={"imgId": img_id, "status": status, "raw": _redact_payload(raw)},
            )
        if status in (WEBPURIFY_STATUS_APPROVED, WEBPURIFY_STATUS_DECLINED):
            # "declined" is a POLICY rejection (nudity/violence/etc), not a
            # CSAM match -- OpenAI moderation already handles that signal, so
            # it is "clear" from this screen's point of view.
            return ScreenResult(
                verdict="clear", provider="webpurify",
                provider_ref={"imgId": img_id, "status": status, "raw": _redact_payload(raw)},
            )
        if time.monotonic() >= deadline:
            return ScreenResult(
                verdict="pending", provider="webpurify",
                provider_ref={"imgId": img_id, "status": status, "raw": _redact_payload(raw)},
            )
        time.sleep(WEBPURIFY_POLL_INTERVAL_SECONDS)


def _webpurify_status(img_id: str):
    """One imgstatus call. Returns (status, raw_json)."""
    resp = requests.get(
        WEBPURIFY_BASE_URL,
        params={
            "api_key": WEBPURIFY_API_KEY,
            "method": WEBPURIFY_STATUS_METHOD,
            "img_id": img_id,
            "format": WEBPURIFY_FORMAT,
        },
        timeout=WEBPURIFY_REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    raw = resp.json()
    return _extract_webpurify_status(raw), raw


def _extract_webpurify_img_id(payload: dict) -> Optional[str]:
    rsp = payload.get("rsp", payload) if isinstance(payload, dict) else {}
    img_id = rsp.get("img_id") or (payload.get("img_id") if isinstance(payload, dict) else None)
    return str(img_id) if img_id else None


def _extract_webpurify_status(payload: dict) -> str:
    rsp = payload.get("rsp", payload) if isinstance(payload, dict) else {}
    status = rsp.get("status") or (payload.get("status") if isinstance(payload, dict) else None) or ""
    return str(status).lower()


def reconcile_pending_ticket(img_ids: list) -> str:
    """One-shot (non-blocking) imgstatus re-check for a previously-pending
    ticket's WebPurify image id(s), for the periodic sweep (NOT the blocking
    poll loop used at submission time). Returns:
      "known_match" -- at least one id now reports "flagged".
      "clear"       -- every id resolved to approved/declined.
      "pending"     -- at least one id is still pending (or a check errored,
                       which is treated as still-pending so the sweep retries
                       next run rather than silently dropping the ticket).
    """
    if not img_ids:
        return "pending"
    statuses = []
    for img_id in img_ids:
        try:
            status, _raw = _webpurify_status(img_id)
        except Exception as e:
            logger.error("[csam] sweep status check failed for %s: %s", img_id, _exc_reason(e))
            status = WEBPURIFY_STATUS_PENDING
        if status == WEBPURIFY_STATUS_FLAGGED:
            return "known_match"
        statuses.append(status)
    if all(s in (WEBPURIFY_STATUS_APPROVED, WEBPURIFY_STATUS_DECLINED) for s in statuses):
        return "clear"
    return "pending"


# ---------------------------------------------------------------------------
# Video frame sampling
# ---------------------------------------------------------------------------


def _compute_frame_timestamps(duration: float, max_frames: int) -> list:
    """1fps sampling plus explicit first (0) and last (duration) frame,
    capped at `max_frames` by spreading samples evenly across the duration --
    the same math `-vf fps=N` (N = max_frames / duration) would produce,
    computed here as explicit timestamps so the first/last frame are always
    exactly included even when the cap binds."""
    if duration <= 0:
        return [0.0]
    natural = [float(t) for t in range(0, math.floor(duration) + 1)]
    if natural[-1] < duration:
        natural.append(duration)
    timestamps = sorted(set(natural))
    if len(timestamps) <= max_frames:
        return timestamps
    if max_frames <= 1:
        return [0.0]
    step = duration / (max_frames - 1)
    return [round(i * step, 3) for i in range(max_frames)]


def _get_video_duration_seconds(video_path: str) -> Optional[float]:
    """ffprobe duration lookup. Deliberately independent of
    server.get_video_duration_seconds (same idiom, no import) so this module
    has no import-time coupling to server.py."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, timeout=15, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.error("[csam] ffprobe duration check failed: %s", result.stderr)
            return None
        return float(result.stdout.strip())
    except Exception as e:
        logger.error("[csam] ffprobe duration check failed: %s", e)
        return None


def _extract_frame_at(video_path: str, timestamp: float) -> bytes:
    """Extract one JPEG frame at `timestamp` seconds. Mirrors the ffmpeg
    idiom server.py already uses for video thumbnails
    (generate_video_thumbnail_bytes): -ss seek + single-frame mjpeg pipe."""
    result = subprocess.run(
        [
            "ffmpeg", "-ss", str(timestamp), "-i", video_path,
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ],
        capture_output=True, timeout=15,
    )
    if result.returncode != 0 or not result.stdout:
        stderr_tail = (result.stderr or b"").decode(errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg frame extraction failed at {timestamp}s: {stderr_tail}")
    return result.stdout


def _extract_video_frames(video_bytes: bytes) -> list:
    """Sample frames for screening per _compute_frame_timestamps. Raises on
    failure -- callers (screen_video_bytes) map that to an "error" verdict."""
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    try:
        tmp.write(video_bytes)
        tmp.close()
        duration = _get_video_duration_seconds(tmp.name)
        if not duration or duration <= 0:
            duration = 1.0  # best-effort: still screen at least the one frame at t=0
        timestamps = _compute_frame_timestamps(duration, CSAM_MAX_VIDEO_FRAMES)
        return [_extract_frame_at(tmp.name, t) for t in timestamps]
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Match path: quarantine -> incident record -> admin alert
# ---------------------------------------------------------------------------


def handle_match(
    db,
    storage_client,
    media_bytes: Optional[bytes],
    content_type: Optional[str],
    screen_result: ScreenResult,
    context: Optional[dict] = None,
    send_admin_alert_fn: Optional[Callable[[str, Optional[str], str, ScreenResult], None]] = None,
) -> dict:
    """Run the full match path for a `known_match` ScreenResult: quarantine
    the bytes + an evidence manifest, write the Firestore incident record,
    and best-effort alert admins. Never raises -- every step is independently
    best-effort and logs CRITICALLY on failure, because a quarantine/alert
    failure must never be allowed to block the caller from aborting the
    publish (the caller fails the upload regardless of what this returns).

    `media_bytes` may be None (the sweep's reconcile path: the media was
    published before the match was discovered and could not be re-fetched).
    This NEVER fabricates an empty-bytes "quarantine" -- a None media_bytes
    always produces quarantined=False with a `mediaUnavailable` reason on the
    incident record, rather than a false evidence-preservation record. The
    incident is still recorded and admins are still alerted either way.

    `context`: {"uid": ..., "source": "mint"|"story"|"avatar"|..., ...}.
    `send_admin_alert_fn(incident_id, uid, source, screen_result)` is
    injected by server.py (it owns firebase_auth + the push/FCM stack); if
    None, alerting is skipped (logged CRITICALLY) rather than erroring.

    Returns {"incidentId": str, "quarantined": bool}.
    """
    context = context or {}
    incident_id = uuid.uuid4().hex
    uid = context.get("uid")
    source = context.get("source", "unknown")

    if media_bytes is None:
        logger.critical(
            "[csam][CRITICAL] media bytes unavailable for incident %s "
            "(uid=%s source=%s) -- recording incident WITHOUT a quarantine "
            "copy (mediaUnavailable)", incident_id, uid, source,
        )
        quarantined = False
        media_path = None
    else:
        manifest = {
            "incidentId": incident_id,
            "uid": uid,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(media_bytes).hexdigest(),
            "md5": hashlib.md5(media_bytes).hexdigest(),
            "provider": screen_result.provider,
            "verdict": screen_result.verdict,
            "providerRef": screen_result.provider_ref,
        }
        media_path = f"incidents/{incident_id}/media"
        quarantined = _write_quarantine(
            storage_client, media_bytes, content_type, manifest, media_path, incident_id
        )
        if not quarantined:
            media_path = None

    _write_incident_record(
        db, incident_id, uid, source, screen_result, media_path,
        media_unavailable=media_bytes is None,
    )

    logger.critical(
        "[csam][CRITICAL] CSAM match detected: incident=%s uid=%s source=%s "
        "provider=%s quarantined=%s",
        incident_id, uid, source, screen_result.provider, quarantined,
    )
    if send_admin_alert_fn is not None:
        try:
            send_admin_alert_fn(incident_id, uid, source, screen_result)
        except Exception as e:
            logger.error("[csam] admin alert callback failed for %s: %s", incident_id, e)
    else:
        logger.critical(
            "[csam][CRITICAL] no admin alert channel configured for incident %s",
            incident_id,
        )

    return {"incidentId": incident_id, "quarantined": quarantined}


def _write_quarantine(storage_client, media_bytes, content_type, manifest, media_path, incident_id) -> bool:
    """Writes the quarantined bytes + manifest. Only ever called with real
    (non-None) media_bytes -- handle_match short-circuits to
    quarantined=False itself when bytes are unavailable, rather than letting
    this write an empty-bytes object and falsely report success."""
    if storage_client is None:
        logger.critical(
            "[csam][CRITICAL] no quarantine storage client configured; "
            "incident %s media NOT quarantined", incident_id,
        )
        return False
    try:
        bucket = storage_client.bucket(CSAM_QUARANTINE_BUCKET)
        bucket.blob(media_path).upload_from_string(media_bytes, content_type=content_type)
        bucket.blob(f"incidents/{incident_id}/manifest.json").upload_from_string(
            json.dumps(manifest, default=str), content_type="application/json"
        )
        return True
    except Exception as e:
        logger.critical(
            "[csam][CRITICAL] quarantine write failed for incident %s: %s", incident_id, e,
        )
        return False


def _write_incident_record(
    db, incident_id, uid, source, screen_result, quarantine_path, media_unavailable=False,
):
    if db is None:
        logger.critical(
            "[csam][CRITICAL] no Firestore client configured; incident %s "
            "NOT recorded (uid=%s source=%s)", incident_id, uid, source,
        )
        return
    try:
        db.collection("csamIncidents").document(incident_id).set({
            "status": "detected",
            "uid": uid,
            "source": source,
            "createdAt": SERVER_TIMESTAMP,
            "provider": screen_result.provider,
            "verdict": screen_result.verdict,
            "quarantinePath": quarantine_path,
            "mediaUnavailable": media_unavailable,
        })
    except Exception as e:
        logger.critical(
            "[csam][CRITICAL] incident record write failed for %s: %s", incident_id, e,
        )


# ---------------------------------------------------------------------------
# Pending tickets (async-review reconciliation)
# ---------------------------------------------------------------------------


def write_pending_ticket(db, screen_result: ScreenResult, context: Optional[dict] = None) -> Optional[str]:
    """Persist a "pending" ScreenResult as a csamScreenTickets doc so
    /csam/sweep can re-poll the provider later and take the content down if
    it resolves to a match after publish. Best-effort: logs and returns None
    on failure rather than raising -- a missing ticket means that one upload
    just doesn't get reconciled, it must never block the (already-decided)
    publish.

    `context` fields (uid, source, plus whatever caller-specific linkage it
    carries, e.g. itemIndex/storyId) are stored flat on the ticket doc.
    """
    if db is None:
        logger.error("[csam] no Firestore client; cannot persist pending ticket")
        return None
    ticket_id = uuid.uuid4().hex
    doc = {
        "status": "pending",
        "provider": screen_result.provider,
        "imgIds": extract_pending_img_ids(screen_result),
        "createdAt": SERVER_TIMESTAMP,
    }
    doc.update(context or {})
    try:
        db.collection("csamScreenTickets").document(ticket_id).set(doc)
        return ticket_id
    except Exception as e:
        logger.error("[csam] failed to persist pending ticket: %s", e)
        return None


def attach_ticket_linkage(db, ticket_id: Optional[str], **fields):
    """Best-effort merge extra fields (e.g. tokenId, once minted) onto an
    already-written pending ticket. No-op if `ticket_id` is falsy (e.g. the
    ticket write itself failed) or `db` is unavailable."""
    if db is None or not ticket_id:
        return
    try:
        db.collection("csamScreenTickets").document(ticket_id).update(fields)
    except Exception as e:
        logger.error("[csam] failed to attach linkage to ticket %s: %s", ticket_id, e)


def sweep_reconcile_pending_tickets(
    db,
    storage_client,
    fetch_media_fn: Optional[Callable[[dict], Optional[tuple]]] = None,
    block_fn: Optional[Callable[[dict], None]] = None,
    send_admin_alert_fn: Optional[Callable[[str, Optional[str], str, ScreenResult], None]] = None,
) -> dict:
    """Re-poll every pending csamScreenTickets doc. Per spec Sec 6, a ticket
    that now resolves "flagged" gets quarantine-copy + incident record FIRST
    (via handle_match), and only THEN the public takedown (`block_fn`) --
    never delete-first. A ticket that resolves clear is marked done; a
    ticket still pending is left alone for the next sweep.

    `fetch_media_fn(ticket_data) -> (bytes, content_type) | None` retrieves
    the now-published media so a quarantine copy can still be made (Phase 1
    best-effort: if it can't be found, the incident is still recorded --
    stopping further distribution via `block_fn` matters more than a
    quarantine copy that can't be produced).
    `block_fn(ticket_data)` performs the actual takedown.

    Returns {"checked": n, "flagged": n, "cleared": n}.
    """
    if db is None:
        return {"checked": 0, "flagged": 0, "cleared": 0}

    checked = flagged = cleared = 0
    tickets = db.collection("csamScreenTickets").where("status", "==", "pending").stream()
    for snap in tickets:
        data = snap.to_dict() or {}
        checked += 1
        try:
            verdict = reconcile_pending_ticket(data.get("imgIds") or [])
        except Exception as e:
            logger.error("[csam] sweep reconcile failed for ticket %s: %s", snap.id, e)
            continue

        if verdict == "known_match":
            flagged += 1
            screen_result = ScreenResult(
                verdict="known_match",
                provider=data.get("provider", "webpurify"),
                provider_ref={"imgIds": data.get("imgIds"), "reconciledFrom": "sweep"},
            )
            context = {"uid": data.get("uid"), "source": data.get("source", "unknown")}
            media_bytes, content_type = None, None
            if fetch_media_fn is not None:
                try:
                    fetched = fetch_media_fn(data)
                    if fetched:
                        media_bytes, content_type = fetched
                except Exception as e:
                    logger.error("[csam] sweep media fetch failed for ticket %s: %s", snap.id, e)
            # media_bytes/content_type are passed through as-is (None when
            # fetch_media_fn couldn't locate the published media) --
            # handle_match itself refuses to fabricate a quarantine record
            # from empty bytes; it records quarantined=False +
            # mediaUnavailable instead of a false evidence-preservation claim.
            incident = handle_match(
                db, storage_client,
                media_bytes, content_type,
                screen_result, context, send_admin_alert_fn=send_admin_alert_fn,
            )
            snap.reference.update({"status": "flagged", "incidentId": incident["incidentId"]})
            if block_fn is not None:
                try:
                    block_fn(data)
                except Exception as e:
                    logger.error("[csam] sweep takedown failed for ticket %s: %s", snap.id, e)
        elif verdict == "clear":
            cleared += 1
            snap.reference.update({"status": "resolved_clear"})
        # else "pending" -- leave for next sweep.

    return {"checked": checked, "flagged": flagged, "cleared": cleared}


def sweep_purge_expired_incidents(db, storage_client) -> int:
    """Delete quarantined evidence (bytes + manifest) for csamIncidents whose
    status is "reported" and older than CSAM_RETENTION_DAYS, then mark them
    "purged". Incidents NOT yet reported are a legal hold under 18 U.S.C.
    2258A and are NEVER purged here regardless of age. The incident doc
    itself (metadata only, no imagery) is retained permanently as the audit
    trail -- only the quarantined blobs are deleted."""
    if db is None:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=CSAM_RETENTION_DAYS)
    purged = 0
    for snap in db.collection("csamIncidents").where("status", "==", "reported").stream():
        data = snap.to_dict() or {}
        created_at = data.get("createdAt")
        if created_at is None or created_at > cutoff:
            continue
        incident_id = snap.id
        if storage_client is not None:
            try:
                bucket = storage_client.bucket(CSAM_QUARANTINE_BUCKET)
                bucket.blob(f"incidents/{incident_id}/media").delete()
                bucket.blob(f"incidents/{incident_id}/manifest.json").delete()
            except Exception as e:
                logger.error("[csam] purge delete failed for incident %s: %s", incident_id, e)
                continue  # don't mark purged if the blob delete failed
        snap.reference.update({"status": "purged"})
        purged += 1
    return purged
