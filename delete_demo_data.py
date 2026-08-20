#!/usr/bin/env python3
"""Delete injected demo posts (+subcollections) and demo users from Firestore.

Targets:
  - /posts/{tokenId} where demoId != ''   (+ their subcollections: likes, comments)
  - /users/{userId}  where isDemo == True (+ their subcollections)
"""
import os, json
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

load_dotenv()
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")))
    firebase_admin.initialize_app(cred)
db = fb_firestore.client()


def delete_doc_recursive(doc_ref):
    """Delete all subcollections under a doc, then the doc itself."""
    for sub in doc_ref.collections():
        for child in sub.stream():
            delete_doc_recursive(child.reference)
    doc_ref.delete()


# ── Posts ────────────────────────────────────────────────────────────────────
demo_posts = list(db.collection("posts").where("demoId", "!=", "").stream())
print(f"Deleting {len(demo_posts)} demo posts (+subcollections)...")
for d in demo_posts:
    delete_doc_recursive(d.reference)
    print(f"  deleted post tokenId={d.id} demoId={d.to_dict().get('demoId')}")

# ── Users ────────────────────────────────────────────────────────────────────
demo_users = list(db.collection("users").where("isDemo", "==", True).stream())
print(f"\nDeleting {len(demo_users)} demo users (+subcollections)...")
for u in demo_users:
    delete_doc_recursive(u.reference)
    print(f"  deleted user {u.id}")

# ── Verify ───────────────────────────────────────────────────────────────────
remaining_posts = len(list(db.collection("posts").where("demoId", "!=", "").stream()))
remaining_users = len(list(db.collection("users").where("isDemo", "==", True).stream()))
total_posts = len(list(db.collection("posts").select([]).stream()))
print(f"\n--- VERIFY ---")
print(f"demo posts remaining: {remaining_posts}")
print(f"demo users remaining: {remaining_users}")
print(f"TOTAL posts remaining: {total_posts}")
