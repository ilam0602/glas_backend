# PhotoDNA Cloud Service — Setup Checklist

Status (2026-11-18):
- Account **approved and enabled** by Microsoft (console login `isaacl@glassocial.com`).
- **API key works** — verified live via `verify_photodna.py` (HTTP 200, Test-hash
  match, response envelope confirmed as `{"MatchResults": [...]}` and parsed by
  the production code path).
- **SDK downloaded**: `PhotoDNA.EdgeHashGeneration-1.05.009/` at the repo root
  (zip SHA256 verified against Microsoft's published value). Gitignored —
  licensed, non-redistributable, must never be committed.
- Server integration code-complete: `csam_screening.py` provider `photodna`
  + `photodna_hash.py` (in-process SDK client library).
- Build pipeline **built** (Dockerfile + cloudbuild.yaml + staging script);
  needs the one-time cloud setup in step 2 below.
- Outstanding: macOS Gatekeeper approval for the native lib (local dev only),
  end-to-end `--image` verification, step-2 cloud setup, and the standing
  enablement gates (legal §10, quarantine bucket, sweep scheduler, NCMEC ESP).

## How it works

PhotoDNA Cloud Service **never receives images**. The server generates an
irreversible ~1.2KB "Edge Hash" locally and POSTs only the hash to
`https://api.microsoftmoderator.com/photodna/v1.0/MatchHash`
(auth = `Ocp-Apim-Subscription-Key` header, up to 5 hashes per request).
Matching is synchronous — unlike the WebPurify flow there is no signed-URL
upload, no polling, and no "pending" verdict (so no `csamScreenTickets` are
ever written by this provider; the `/csam/sweep` reconcile loop only matters
for retention purging and any legacy WebPurify tickets).

Hash generation (`photodna_hash.py`) uses the SDK's own Python client library
(`<sdk>/clientlibrary/python/PhotoDna.EdgeHashGenerator`) **in-process**: the
ctypes binding auto-selects the right native library for the platform
(`libEdgeHashGenerator-arm64-macos.so.1.05` on Apple Silicon,
`libEdgeHashGenerator.so.1.05` on x64 Linux/Cloud Run), loads once per
process, and each hash is a single native call. Pillow decodes the image
(already a server dependency; numpy is not needed).

Videos: frames are sampled (1fps + first/last, capped at
`CSAM_MAX_VIDEO_FRAMES=60`), each frame hashed locally, hashes batched 5 per
MatchHash request (60 frames = 12 requests).

## macOS dev-machine note (Gatekeeper)

The SDK's dylibs are unsigned, so the first in-process load on a Mac triggers
a Gatekeeper "Not Opened" dialog and the loading process **hangs inside the
OS call** until resolved. Already done on this machine (2026-11-18):
quarantine xattrs cleared (`xattr -dr com.apple.quarantine <sdk>`) and the
macOS dylibs ad-hoc codesigned (`codesign -s - -f`). If a dialog still
appears: click **Done** (never "Move to Trash"), then System Settings →
Privacy & Security → **Allow Anyway** for `libEdgeHashGenerator-arm64-macos`.
This is local-dev friction only — Linux/Cloud Run has no Gatekeeper.

## Remaining steps (in order)

1. **Approve the native lib on macOS** (dialog/Settings, above), then verify
   hash generation end-to-end:

   ```bash
   source venv/bin/activate
   python verify_photodna.py --image PhotoDNA.EdgeHashGeneration-1.05.009/samples/TestImages/img_130.jpg
   ```

   The SDK's sample images are in Microsoft's known-hash "Test" set, so this
   must report a match. (Keys + `PHOTODNA_SDK_DIR` are already set in `.env`.)
   The opt-in pytest also covers this:
   `RUN_PHOTODNA_SDK_TESTS=1 python -m pytest test_csam_screening.py -k real_sdk`.

2. **Cloud Run / Docker — BUILT, needs one-time cloud setup.** The pipeline
   is in place (all committed-to-be): the Dockerfile `COPY`s the trimmed
   `photodna_sdk/` staging dir and sets `PHOTODNA_SDK_DIR=/app/photodna_sdk`
   (it also now COPYs `photodna_hash.py` — without that every deploy would
   have crashed at import); `scripts/stage_photodna_sdk.sh` trims the SDK
   (clientlibrary only, ~python binding + .so files); `cloudbuild.yaml`
   fetches the staged SDK from a private GCS bucket before `docker build` and
   deploys inheriting ALL existing console-managed service config. From a
   bare checkout (today's "Dockerfile"-type trigger) builds still work — the
   image just ships without the SDK, which is safe while the feature is dark.

   One-time cloud steps (run manually — each writes to GCP):

   ```bash
   # a. Private SDK bucket (same project as glas-backend; tight access).
   gcloud storage buckets create gs://glas-photodna-sdk \
     --location=us-central1 --uniform-bucket-level-access \
     --public-access-prevention

   # b. Upload the trimmed SDK (re-run after any SDK version bump).
   ./scripts/stage_photodna_sdk.sh
   gsutil -m rsync -r -d photodna_sdk gs://glas-photodna-sdk/photodna_sdk

   # c. Let Cloud Build read it (use the build SA shown in Cloud Build settings).
   gcloud storage buckets add-iam-policy-binding gs://glas-photodna-sdk \
     --member="serviceAccount:<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com" \
     --role="roles/storage.objectViewer"

   # d. API key into Secret Manager, exposed to the service as PHOTODNA_API_KEY.
   printf '%s' '<the primary key>' | gcloud secrets create photodna-api-key --data-file=-
   # then Cloud Run -> glas-backend -> Edit & deploy new revision ->
   # Variables & Secrets -> Reference a secret: PHOTODNA_API_KEY = photodna-api-key:latest
   ```

   Then switch the deploy trigger to the new config: **Cloud Build →
   Triggers → the glas-backend trigger → Edit → Configuration: "Cloud Build
   configuration file" → `/cloudbuild.yaml`** → Save. The next push to main
   builds with the SDK baked in. (`PHOTODNA_SDK_DIR` is set by the Dockerfile;
   do NOT set it in the console.)

   Finally, confirm the Linux library actually loads in the deployed image:
   temporarily hit any screening path with `CSAM_SCREEN_PROVIDER=photodna`
   on a staging revision, or shell into the image locally
   (`docker run --rm -it <image> python -c "import photodna_hash; print(photodna_hash.generate_edge_hash(open('/app/...','rb').read()))"`)
   if/where Docker is available.

3. **Enable** (only after the standing enablement gates are met — legal §10
   sign-off, quarantine bucket `glas-csam-quarantine` provisioned, Cloud
   Scheduler job for `/csam/sweep`):

   ```bash
   CSAM_SCREEN_PROVIDER=photodna
   CSAM_FAIL_MODE=open   # or "closed" to abort uploads on provider errors
   ```

4. **NCMEC ESP registration** (required for the reporting duty once
   detection is live) — registration submitted via
   <https://esp.ncmec.org/registration>; status inquiry sent to
   `ESPteam@NCMEC.ORG` (no confirmation had been received as of 2026-11-18).
   Microsoft recommends reporting directly via NCMEC's API rather than
   through PhotoDNA's built-in report capability. Phase 1 reporting is
   manual either way. Note: the public CyberTipline
   (<https://report.cybertip.org>) is always available regardless of ESP
   status — the 2258A reporting duty does not wait for registration.

## Env var reference

| Var | Default | Meaning |
|---|---|---|
| `CSAM_SCREEN_PROVIDER` | `disabled` | `disabled` \| `photodna` \| `webpurify` |
| `PHOTODNA_API_KEY` | — | Subscription key (console, `isaacl@glassocial.com`; primary key in use, secondary kept in console as rotation spare) |
| `PHOTODNA_SDK_DIR` | — | Extracted EdgeHashGeneration SDK root (contains `clientlibrary/`) |

All other CSAM vars (`CSAM_FAIL_MODE`, `CSAM_QUARANTINE_BUCKET`,
`CSAM_RETENTION_DAYS`, `CSAM_MAX_VIDEO_FRAMES`) behave exactly as documented
in `CLAUDE.md` / the 2026-10-09 spec regardless of provider.
