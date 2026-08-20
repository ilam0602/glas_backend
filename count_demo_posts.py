#!/usr/bin/env python3
"""DRY RUN: count injected/demo posts in Firestore. Deletes nothing."""
import os, json
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

load_dotenv()
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")))
    firebase_admin.initialize_app(cred)
db = fb_firestore.client()

# Demo posts are marked by a non-empty demoId field (p0001..p0120).
demo_posts = list(db.collection("posts").where("demoId", "!=", "").stream())
print(f"posts with demoId != '': {len(demo_posts)}")

# Corroborate: empty walletAddress
empty_wallet = [d for d in demo_posts if (d.to_dict().get("walletAddress") or "") == ""]
print(f"  of those, walletAddress == '': {len(empty_wallet)}")

# Demo users
demo_users = list(db.collection("users").where("isDemo", "==", True).stream())
print(f"users with isDemo == True: {len(demo_users)}")

# Total posts for context
total_posts = len(list(db.collection("posts").select([]).stream()))
print(f"TOTAL posts in collection: {total_posts}")

# Sample a few demo docs
print("\nSample demo posts:")
for d in demo_posts[:5]:
    x = d.to_dict()
    print(f"  tokenId={d.id} demoId={x.get('demoId')} userId={x.get('userId')} mediaType={x.get('mediaType')} caption={str(x.get('caption'))[:30]!r}")
