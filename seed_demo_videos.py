#!/usr/bin/env python3
"""
Seed demo VIDEO posts (type=video and type=reel) from demo_posts JSON.
Videos are fetched from Pexels API, uploaded to IPFS, minted on-chain,
and saved to Firestore with comments.

Usage:
    cd authensnap_server
    source venv/bin/activate
    python seed_demo_videos.py /path/to/demo_posts.json

Requires PEXELS_API_KEY in .env (free at https://www.pexels.com/api/).
Resume support: posts whose demoId already exists in Firestore are skipped.
"""

import sys
import os
import json
import base64
import time
import random
from datetime import datetime

import requests
from dotenv import load_dotenv
from web3 import Web3
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore
from google.cloud import storage as gcs_storage

load_dotenv()

# ── Environment ──────────────────────────────────────────────────────────────

RPC_URL = os.getenv("SEPOLIA_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")
PINATA_API_KEY = os.getenv("PINATA_API_KEY")
PINATA_SECRET_API_KEY = os.getenv("PINATA_SECRET_API_KEY")
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
FIREBASE_STORAGE_BUCKET = os.getenv(
    "FIREBASE_STORAGE_BUCKET", "authensnapmobiletest.firebasestorage.app"
)
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
GCS_SERVICE_ACCOUNT_JSON = os.getenv("GCS_SERVICE_ACCOUNT_JSON")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "glas-hidef")

for name in ("SEPOLIA_RPC_URL", "PRIVATE_KEY", "CONTRACT_ADDRESS",
             "PINATA_API_KEY", "PINATA_SECRET_API_KEY",
             "FIREBASE_SERVICE_ACCOUNT_JSON", "PEXELS_API_KEY"):
    if not os.getenv(name):
        sys.exit(f"Missing required env var: {name}")

# ── Web3 setup ───────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    sys.exit("Could not connect to Ethereum node.")

with open("AuthenSnap.json", "r") as f:
    abi = json.load(f)["abi"]

contract = w3.eth.contract(
    address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=abi
)
account = w3.eth.account.from_key(PRIVATE_KEY)

# ── Firebase setup ───────────────────────────────────────────────────────────

if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
    firebase_admin.initialize_app(cred, {"storageBucket": FIREBASE_STORAGE_BUCKET})

db = fb_firestore.client()

# ── GCS setup (for fast HQ video serving) ────────────────────────────────────

gcs_bucket = None
try:
    if GCS_SERVICE_ACCOUNT_JSON:
        from google.oauth2 import service_account as sa_auth
        gcs_credentials = sa_auth.Credentials.from_service_account_info(
            json.loads(GCS_SERVICE_ACCOUNT_JSON)
        )
        gcs_client = gcs_storage.Client(
            credentials=gcs_credentials, project=gcs_credentials.project_id
        )
    else:
        gcs_client = gcs_storage.Client()
    gcs_bucket = gcs_client.bucket(GCS_BUCKET_NAME)
    print(f"GCS client initialized with bucket: {GCS_BUCKET_NAME}")
except Exception as e:
    print(f"WARNING: GCS init failed: {e}. Videos will load from IPFS only.")


def upload_to_gcs(file_bytes, filename, content_type):
    """Upload file bytes to GCS hq_media/ bucket. Returns relative media path."""
    if not gcs_bucket:
        return None
    try:
        blob_path = f"hq_media/{filename}"
        blob = gcs_bucket.blob(blob_path)
        blob.upload_from_string(file_bytes, content_type=content_type)
        media_path = f"/media/{blob_path}"
        print(f"  GCS upload: {blob_path} -> {media_path}")
        return media_path
    except Exception as e:
        print(f"  GCS upload failed (non-blocking): {e}")
        return None


# ── Ranking constants (match server.py) ──────────────────────────────────────

COMMENT_WEIGHT = 3.0
GRAVITY = 1.5


def compute_rank_score(likes_count, comments_count, created_at_seconds):
    engagement = likes_count + (comments_count * COMMENT_WEIGHT)
    now = time.time()
    age_hours = max(0, (now - created_at_seconds) / 3600.0)
    return (engagement + 1) / ((age_hours + 2) ** GRAVITY)


# ── Pexels video fetching ────────────────────────────────────────────────────

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_HEADERS = {"Authorization": PEXELS_API_KEY}

LIFESTYLE_KEYWORDS = [
    "friends laughing", "coffee shop", "hiking trail", "city walk",
    "cooking at home", "beach sunset", "yoga morning", "dog park",
    "picnic outdoors", "road trip", "concert crowd", "farmers market",
    "reading book cozy", "birthday party", "friends dinner", "morning run",
    "plant care", "board game night", "thrift shopping", "bike ride city",
]


def fetch_pexels_video(keyword, orientation="landscape"):
    """
    Search Pexels for a video matching the keyword.
    Returns (video_bytes, thumbnail_url) or raises on failure.
    """
    params = {"query": keyword, "orientation": orientation, "per_page": 5}
    resp = requests.get(PEXELS_SEARCH_URL, headers=PEXELS_HEADERS,
                        params=params, timeout=15)
    resp.raise_for_status()
    videos = resp.json().get("videos", [])
    if not videos:
        raise Exception(f"No Pexels results for '{keyword}'")

    video = random.choice(videos)
    thumbnail_url = video.get("image")

    # Pick a mid-range file (480-1080p) to keep sizes reasonable
    files = sorted(video.get("video_files", []),
                   key=lambda f: f.get("width") or 0)
    files = [f for f in files
             if f.get("width") and 480 <= f["width"] <= 1080] or files
    if not files:
        raise Exception(f"No suitable video files for '{keyword}'")
    chosen = files[len(files) // 2]

    # Download the actual video bytes
    dl_resp = requests.get(chosen["link"], timeout=120)
    dl_resp.raise_for_status()

    return dl_resp.content, thumbnail_url


# ── Helpers (same logic as server.py) ────────────────────────────────────────

def compute_user_id_hash(user_id: str) -> bytes:
    return Web3.keccak(text=user_id)


def pin_file_to_pinata(base64_str, media_type="video"):
    file_data = base64.b64decode(base64_str)
    if media_type == "video":
        filename = "nft_video.mp4"
    else:
        filename = "nft_image.png"
    files = {"file": (filename, file_data)}
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {
        "pinata_api_key": PINATA_API_KEY,
        "pinata_secret_api_key": PINATA_SECRET_API_KEY,
    }
    resp = requests.post(url, files=files, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Pinata upload error: {resp.text}")
    ipfs_hash = resp.json()["IpfsHash"]
    return f"ipfs://{ipfs_hash}"


def pin_metadata_to_pinata(media_ipfs_url, token_name,
                           description="Video minted on AuthenSnap",
                           media_type="video"):
    gateway_url = media_ipfs_url.replace(
        "ipfs://", "https://gateway.pinata.cloud/ipfs/"
    )
    metadata = {
        "name": token_name,
        "description": description,
        "image": gateway_url,
    }
    if media_type == "video":
        metadata["animation_url"] = gateway_url
        metadata["media_type"] = "video"

    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {
        "pinata_api_key": PINATA_API_KEY,
        "pinata_secret_api_key": PINATA_SECRET_API_KEY,
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=metadata, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Pinata metadata upload error: {resp.text}")
    ipfs_hash = resp.json()["IpfsHash"]
    return f"ipfs://{ipfs_hash}"


def get_gas_params():
    latest_block = w3.eth.get_block("latest")
    base_fee = latest_block["baseFeePerGas"]
    max_priority_fee = w3.to_wei(30, "gwei")
    max_fee = (base_fee * 5) + max_priority_fee
    min_max_fee = w3.to_wei(600, "gwei")
    max_fee = max(max_fee, min_max_fee)
    return max_fee, max_priority_fee


def cancel_pending_transactions():
    confirmed = w3.eth.get_transaction_count(account.address)
    pending = w3.eth.get_transaction_count(account.address, "pending")
    if pending <= confirmed:
        return
    print(f"  Cancelling {pending - confirmed} pending transactions...")
    latest_block = w3.eth.get_block("latest")
    base_fee = latest_block["baseFeePerGas"]
    max_priority_fee = w3.to_wei(50, "gwei")
    max_fee = (base_fee * 5) + max_priority_fee
    for nonce in range(confirmed, pending):
        cancel_txn = {
            "from": account.address,
            "to": account.address,
            "value": 0,
            "chainId": w3.eth.chain_id,
            "gas": 21000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority_fee,
            "nonce": nonce,
        }
        signed = w3.eth.account.sign_transaction(cancel_txn, PRIVATE_KEY)
        w3.eth.send_raw_transaction(signed.raw_transaction)
    time.sleep(5)


def get_nonce():
    confirmed = w3.eth.get_transaction_count(account.address)
    pending = w3.eth.get_transaction_count(account.address, "pending")
    if pending > confirmed:
        print(f"  WARNING: {pending - confirmed} pending transactions detected!")
        cancel_pending_transactions()
        time.sleep(5)
        return w3.eth.get_transaction_count(account.address, "pending")
    return pending


def send_transaction(txn):
    signed = w3.eth.account.sign_transaction(txn, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx sent: https://sepolia.etherscan.io/tx/{tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    print(f"  Mined in block {receipt['blockNumber']}")
    return tx_hash, receipt


def extract_virtual_mint_token_id(receipt):
    virtual_mint_event = contract.events.VirtualMint()
    logs = virtual_mint_event.process_receipt(receipt)
    if logs:
        return int(logs[0]["args"]["tokenId"])
    raise Exception("No VirtualMint event found in receipt")


# ── Resume support ───────────────────────────────────────────────────────────

def get_existing_demo_ids():
    existing = set()
    docs = db.collection("posts").where("demoId", "!=", "").stream()
    for doc in docs:
        demo_id = doc.to_dict().get("demoId")
        if demo_id:
            existing.add(demo_id)
    return existing


# ── Main ─────────────────────────────────────────────────────────────────────

def seed_users(users):
    print(f"\n{'='*60}")
    print(f"Creating demo users in Firestore")
    print(f"{'='*60}")
    for user in users:
        user_ref = db.collection("users").document(user["id"])
        if user_ref.get().exists:
            print(f"  [skip] {user['username']} already exists")
            continue
        user_ref.set({
            "name": user["name"],
            "username": user["username"],
            "avatarUrl": user["avatar_url"],
            "bio": user["bio"],
            "isDemo": True,
        })
        print(f"  [created] {user['username']}")
    print("Done creating users.\n")


def seed_videos(video_posts):
    print(f"\n{'='*60}")
    print(f"Minting & saving {len(video_posts)} demo video posts")
    print(f"{'='*60}\n")

    existing = get_existing_demo_ids()
    print(f"Found {len(existing)} already-seeded posts in Firestore.\n")

    # Pre-shuffle keywords so each post gets a different one
    keywords = random.sample(
        LIFESTYLE_KEYWORDS * (len(video_posts) // len(LIFESTYLE_KEYWORDS) + 1),
        k=len(video_posts),
    )

    success = 0
    skipped = 0
    failed = 0

    for i, post in enumerate(video_posts):
        demo_id = post["id"]
        post_type = post.get("type", "video")
        keyword = keywords[i]
        label = f"[{i+1}/{len(video_posts)}] {demo_id} ({post_type})"

        if demo_id in existing:
            print(f"{label} — SKIP (already seeded)")
            skipped += 1
            continue

        print(f"{label} by {post['username']}")
        try:
            # 1. Fetch video from Pexels
            orientation = "portrait" if post_type == "reel" else "landscape"
            print(f"  Searching Pexels: '{keyword}' ({orientation})")
            video_bytes, pexels_thumbnail = fetch_pexels_video(keyword, orientation)
            size_mb = len(video_bytes) / (1024 * 1024)
            print(f"  Downloaded {size_mb:.1f} MB from Pexels")

            video_b64 = base64.b64encode(video_bytes).decode("utf-8")

            # Use Pexels thumbnail, fall back to the JSON's picsum thumbnail
            thumbnail_url = pexels_thumbnail or post.get("media", {}).get("thumbnail_url")

            # 2. Upload video to Pinata/IPFS
            print("  Uploading video to IPFS...")
            video_ipfs_url = pin_file_to_pinata(video_b64, media_type="video")
            print(f"  Video IPFS: {video_ipfs_url}")

            # 3. Pin metadata JSON (with animation_url for video)
            print("  Pinning metadata...")
            metadata_ipfs_url = pin_metadata_to_pinata(
                video_ipfs_url, "AuthenSnap Video", media_type="video"
            )
            print(f"  Metadata IPFS: {metadata_ipfs_url}")

            # 4. Mint on-chain
            user_id = post["user_id"]
            user_id_hash = compute_user_id_hash(user_id)
            nonce = get_nonce()
            max_fee, max_priority_fee = get_gas_params()

            mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
            txn = mint_fn.build_transaction({
                "chainId": w3.eth.chain_id,
                "gas": 500000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            })

            print("  Minting on-chain...")
            tx_hash, receipt = send_transaction(txn)
            token_id = extract_virtual_mint_token_id(receipt)
            print(f"  Token ID: {token_id}")

            # 4b. Upload video to GCS for fast playback
            hq_media_url = upload_to_gcs(
                video_bytes, f"{token_id}.mp4", "video/mp4"
            )

            # 5. Save post to Firestore
            created_at = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
            created_at_seconds = created_at.timestamp()
            rank_score = compute_rank_score(
                post["like_count"], post["comment_count"], created_at_seconds
            )

            post_doc = {
                "tokenId": token_id,
                "walletAddress": "",
                "ipfsUrl": video_ipfs_url,
                "userId": user_id,
                "userEmail": post["username"],
                "caption": post.get("caption", ""),
                "likesCount": post["like_count"],
                "commentsCount": post["comment_count"],
                "mediaType": "video",
                "createdAt": created_at,
                "transactionHash": tx_hash.hex(),
                "isPrivate": False,
                "flagged": False,
                "rankScore": rank_score,
                "demoId": demo_id,
            }

            if hq_media_url:
                post_doc["hqMediaUrl"] = hq_media_url
            if thumbnail_url:
                post_doc["thumbnailUrl"] = thumbnail_url

            db.collection("posts").document(str(token_id)).set(post_doc)
            print(f"  Saved to Firestore: posts/{token_id}")

            # 6. Save comments
            if post.get("comments"):
                for comment in post["comments"]:
                    comment_created = datetime.fromisoformat(
                        comment["created_at"].replace("Z", "+00:00")
                    )
                    comment_doc = {
                        "tokenId": token_id,
                        "userId": comment["user_id"],
                        "userEmail": comment["username"],
                        "text": comment["text"],
                        "createdAt": comment_created,
                    }
                    db.collection("posts").document(str(token_id)).collection(
                        "comments"
                    ).document(comment["id"]).set(comment_doc)
                print(f"  Saved {len(post['comments'])} comments")

            success += 1
            print(f"  Done\n")

            # Brief pause to stay under Pexels rate limits
            time.sleep(0.3)

        except Exception as e:
            failed += 1
            print(f"  FAILED: {e}\n")
            time.sleep(2)

    print(f"\n{'='*60}")
    print(f"Seeding complete: {success} minted, {skipped} skipped, {failed} failed")
    print(f"{'='*60}")


def backfill_hq():
    """
    Find all demo video posts in Firestore that are missing hqMediaUrl,
    download their video from IPFS, upload to GCS, and update the doc.
    """
    if not gcs_bucket:
        sys.exit("GCS not configured — cannot backfill hqMediaUrl.")

    print(f"\n{'='*60}")
    print("Backfilling hqMediaUrl for existing demo video posts")
    print(f"{'='*60}\n")

    docs = db.collection("posts").where("demoId", "!=", "").stream()
    to_fix = []
    for doc_snap in docs:
        data = doc_snap.to_dict()
        if data.get("mediaType") == "video" and not data.get("hqMediaUrl"):
            to_fix.append((doc_snap.id, data))

    print(f"Found {len(to_fix)} video posts missing hqMediaUrl.\n")

    fixed = 0
    for i, (doc_id, data) in enumerate(to_fix):
        ipfs_url = data.get("ipfsUrl", "")
        print(f"[{i+1}/{len(to_fix)}] posts/{doc_id}")

        if not ipfs_url:
            print("  No ipfsUrl, skipping")
            continue

        try:
            # Convert ipfs:// to gateway URL and download
            gateway_url = ipfs_url.replace(
                "ipfs://", "https://gateway.pinata.cloud/ipfs/"
            )
            print(f"  Downloading from IPFS: {gateway_url}")
            resp = requests.get(gateway_url, timeout=120)
            resp.raise_for_status()
            video_bytes = resp.content
            size_mb = len(video_bytes) / (1024 * 1024)
            print(f"  Downloaded {size_mb:.1f} MB")

            # Upload to GCS
            hq_media_url = upload_to_gcs(
                video_bytes, f"{doc_id}.mp4", "video/mp4"
            )
            if not hq_media_url:
                print("  GCS upload returned None, skipping")
                continue

            # Update Firestore doc
            db.collection("posts").document(doc_id).update({
                "hqMediaUrl": hq_media_url,
            })
            print(f"  Updated hqMediaUrl -> {hq_media_url}")
            fixed += 1

        except Exception as e:
            print(f"  FAILED: {e}")

    print(f"\nBackfill complete: {fixed}/{len(to_fix)} posts updated.")


def main():
    # --backfill-hq mode: fix old posts that are missing hqMediaUrl
    if len(sys.argv) >= 2 and sys.argv[1] == "--backfill-hq":
        backfill_hq()
        return

    if len(sys.argv) < 2:
        sys.exit("Usage: python seed_demo_videos.py <path/to/demo_posts.json>\n"
                 "       python seed_demo_videos.py --backfill-hq")

    json_path = sys.argv[1]
    if not os.path.exists(json_path):
        sys.exit(f"File not found: {json_path}")

    with open(json_path, "r") as f:
        data = json.load(f)

    # Filter to only video and reel posts
    video_posts = [p for p in data["posts"] if p.get("type") in ("video", "reel")]
    print(f"Loaded {len(data['posts'])} total posts, {len(video_posts)} are videos/reels")

    if not video_posts:
        sys.exit("No video posts found in the JSON.")

    # Collect user IDs referenced by video posts, seed only those
    video_user_ids = {p["user_id"] for p in video_posts}
    video_users = [u for u in data["users"] if u["id"] in video_user_ids]
    seed_users(video_users)

    seed_videos(video_posts)


if __name__ == "__main__":
    main()
