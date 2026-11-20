# PhotoDNA EdgeHash SDK staging directory

This directory is a **build-time staging area** for Microsoft's PhotoDNA
EdgeHashGeneration SDK (`clientlibrary/` only: the Python ctypes binding +
native libraries). The SDK is licensed and non-redistributable, so its
contents are **gitignored** — only this README is committed, which keeps
`COPY photodna_sdk /app/photodna_sdk` in the Dockerfile working from a bare
checkout (the image then simply has no SDK, and the `photodna` screening
provider reports a clean "SDK not configured" error instead of crashing).

How it gets filled:

- **Cloud Build** (`cloudbuild.yaml`, the real deploy path): the
  `fetch-photodna-sdk` step pulls the trimmed SDK from the private GCS bucket
  into this directory before `docker build`, so the pushed image has it baked
  in at `/app/photodna_sdk`.
- **Locally**: run `scripts/stage_photodna_sdk.sh` (requires the extracted
  `PhotoDNA.EdgeHashGeneration-*/` SDK next to it).

See `docs/PHOTODNA_SETUP.md` for the full setup/runbook.
