#!/usr/bin/env bash
# Stage a trimmed copy of the PhotoDNA EdgeHashGeneration SDK into
# photodna_sdk/ for docker builds (and for upload to the private GCS bucket
# that cloudbuild.yaml pulls from).
#
# "Trimmed" = clientlibrary/ only (python ctypes binding + native libraries).
# samples/, documents/ and webassembly/ are never needed at runtime --
# photodna_hash.py resolves <PHOTODNA_SDK_DIR>/clientlibrary/python/PhotoDna
# and the matching libEdgeHashGenerator-* for the platform.
#
# Usage:
#   ./scripts/stage_photodna_sdk.sh [path-to-extracted-sdk]
# Default source: the newest PhotoDNA.EdgeHashGeneration-* dir in the repo root.
#
# After staging, upload to GCS for Cloud Build (run manually -- see
# docs/PHOTODNA_SETUP.md for the bucket-creation runbook):
#   gsutil -m rsync -r -d photodna_sdk "gs://glas-photodna-sdk/photodna_sdk"
set -euo pipefail

cd "$(dirname "$0")/.."

SRC="${1:-}"
if [[ -z "$SRC" ]]; then
  SRC=$(ls -d PhotoDNA.EdgeHashGeneration-*/ 2>/dev/null | sort | tail -1 || true)
fi
if [[ -z "$SRC" || ! -d "$SRC/clientlibrary/python/PhotoDna" ]]; then
  echo "ERROR: extracted SDK not found (looked for PhotoDNA.EdgeHashGeneration-*/" >&2
  echo "with a clientlibrary/python/PhotoDna inside). Pass the SDK path as \$1." >&2
  exit 1
fi

echo "Staging from: $SRC"
# Preserve the committed README; replace everything else.
find photodna_sdk -mindepth 1 ! -name README.md -delete 2>/dev/null || true
mkdir -p photodna_sdk/clientlibrary

# Python binding (required on every platform).
cp -R "$SRC/clientlibrary/python" photodna_sdk/clientlibrary/python
find photodna_sdk -name __pycache__ -type d -prune -exec rm -rf {} +

# Native libraries: Linux (Cloud Run: x64 + arm64) and macOS (local dev).
# Windows .dll / .nupkg are never used by this server.
for lib in "$SRC"/clientlibrary/libEdgeHashGenerator*.so.*; do
  cp "$lib" photodna_sdk/clientlibrary/
done

echo "Staged:"
find photodna_sdk -type f | sort
echo
echo "Next (manual, needs GCS write access -- see docs/PHOTODNA_SETUP.md):"
echo "  gsutil -m rsync -r -d photodna_sdk gs://glas-photodna-sdk/photodna_sdk"
