"""
Migrate Firestore hqMediaUrl and thumbnailUrl fields from absolute URLs to relative paths.

Before: https://glas-backend-486202920754.us-central1.run.app/media/hq_media/18.mp4
After:   /media/hq_media/18.mp4

This is safe to run multiple times — it skips posts that are already relative
and skips encrypted (private) URLs.

Usage:
    cd authensnap_server
    source venv/bin/activate
    python migrate_media_urls.py          # dry run (default)
    python migrate_media_urls.py --apply  # actually write changes
"""

import json
import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()

FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
if not FIREBASE_SERVICE_ACCOUNT_JSON:
    print("ERROR: FIREBASE_SERVICE_ACCOUNT_JSON not set in .env")
    sys.exit(1)

cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
firebase_admin.initialize_app(cred)
db = firestore.client()

DRY_RUN = "--apply" not in sys.argv


def strip_origin(url: str) -> str | None:
    """
    Convert an absolute media URL to a relative path.
    Returns None if the URL should be left alone (already relative, empty, or encrypted).
    """
    if not url:
        return None
    # Already a relative path
    if url.startswith("/"):
        return None
    # Encrypted URLs won't parse as normal URLs — skip them
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    # Only strip URLs that point to /media/
    if not parsed.path.startswith("/media/"):
        return None
    return parsed.path


def main():
    if DRY_RUN:
        print("=== DRY RUN (pass --apply to write changes) ===\n")

    posts_ref = db.collection("posts")
    docs = posts_ref.stream()

    total = 0
    updated = 0

    for doc in docs:
        total += 1
        data = doc.to_dict()
        token_id = data.get("tokenId", doc.id)

        changes = {}

        hq = data.get("hqMediaUrl", "")
        new_hq = strip_origin(hq)
        if new_hq is not None:
            changes["hqMediaUrl"] = new_hq

        thumb = data.get("thumbnailUrl", "")
        new_thumb = strip_origin(thumb)
        if new_thumb is not None:
            changes["thumbnailUrl"] = new_thumb

        if not changes:
            continue

        updated += 1
        for field, new_val in changes.items():
            old_val = data.get(field, "")
            print(f"  tokenId={token_id}  {field}")
            print(f"    old: {old_val}")
            print(f"    new: {new_val}")

        if not DRY_RUN:
            doc.reference.update(changes)

    print(f"\nScanned {total} posts, {'would update' if DRY_RUN else 'updated'} {updated}.")
    if DRY_RUN and updated > 0:
        print("Run with --apply to write changes.")


if __name__ == "__main__":
    main()
