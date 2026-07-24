#!/usr/bin/env python3
"""
Seed 120 demo posts from demo_posts JSON into the platform.
Each post is fully minted on-chain (Sepolia) and saved to Firestore with comments.

Usage:
    cd authensnap_server
    source venv/bin/activate
    python seed_demo_posts.py /path/to/demo_posts.json

Resume support: posts whose demo_id already exists in Firestore are skipped.
"""

import sys
import os
import json
import base64
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from web3 import Web3
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore

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

for name in ("SEPOLIA_RPC_URL", "PRIVATE_KEY", "CONTRACT_ADDRESS",
             "PINATA_API_KEY", "PINATA_SECRET_API_KEY", "FIREBASE_SERVICE_ACCOUNT_JSON"):
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

# ── Ranking constants (match server.py) ──────────────────────────────────────

COMMENT_WEIGHT = 3.0
GRAVITY = 1.5


def compute_rank_score(likes_count, comments_count, created_at_seconds):
    engagement = likes_count + (comments_count * COMMENT_WEIGHT)
    now = time.time()
    age_hours = max(0, (now - created_at_seconds) / 3600.0)
    return (engagement + 1) / ((age_hours + 2) ** GRAVITY)


# ── Helpers (same logic as server.py) ────────────────────────────────────────

def compute_user_id_hash(user_id: str) -> bytes:
    return Web3.keccak(text=user_id)


def pin_file_to_pinata(base64_image_str):
    file_data = base64.b64decode(base64_image_str)
    files = {"file": ("nft_image.png", file_data)}
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


def pin_metadata_to_pinata(image_ipfs_url, token_name,
                           description="Photo minted on AuthenSnap"):
    gateway_url = image_ipfs_url.replace(
        "ipfs://", "https://gateway.pinata.cloud/ipfs/"
    )
    metadata = {
        "name": token_name,
        "description": description,
        "image": gateway_url,
    }
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
    """Return set of demo_id values already seeded in Firestore."""
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
    print(f"Step 1: Creating {len(users)} demo users in Firestore")
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


def seed_posts(posts):
    print(f"\n{'='*60}")
    print(f"Step 2: Minting & saving {len(posts)} demo posts")
    print(f"{'='*60}\n")

    existing = get_existing_demo_ids()
    print(f"Found {len(existing)} already-seeded posts in Firestore.\n")

    success = 0
    skipped = 0
    failed = 0

    for i, post in enumerate(posts):
        demo_id = post["id"]
        label = f"[{i+1}/{len(posts)}] {demo_id}"

        if demo_id in existing:
            print(f"{label} — SKIP (already seeded)")
            skipped += 1
            continue

        print(f"{label} by {post['username']}")
        try:
            # 1. Download image
            image_url = post["images"][0]
            print(f"  Downloading {image_url}")
            img_resp = requests.get(image_url, timeout=30)
            if img_resp.status_code != 200:
                raise Exception(f"Image download failed: HTTP {img_resp.status_code}")
            image_b64 = base64.b64encode(img_resp.content).decode("utf-8")

            # 2. Upload image to Pinata/IPFS
            print("  Uploading image to IPFS...")
            ipfs_url = pin_file_to_pinata(image_b64)
            print(f"  Image IPFS: {ipfs_url}")

            # 3. Pin metadata JSON
            print("  Pinning metadata...")
            metadata_ipfs_url = pin_metadata_to_pinata(ipfs_url, "AuthenSnap Photo")
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

            # 5. Save post to Firestore
            created_at = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
            created_at_seconds = created_at.timestamp()
            rank_score = compute_rank_score(
                post["like_count"], post["comment_count"], created_at_seconds
            )

            post_doc = {
                "tokenId": token_id,
                "walletAddress": "",
                "ipfsUrl": ipfs_url,
                "userId": user_id,
                "userEmail": post["username"],
                "caption": post.get("caption", ""),
                "likesCount": post["like_count"],
                "commentsCount": post["comment_count"],
                "mediaType": "photo",
                "createdAt": created_at,
                "transactionHash": tx_hash.hex(),
                "isPrivate": False,
                "flagged": False,
                "rankScore": rank_score,
                "demoId": demo_id,
            }

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
            print(f"  ✓ Done\n")

        except Exception as e:
            failed += 1
            print(f"  ✗ FAILED: {e}\n")
            # Wait a moment before retrying next post
            time.sleep(2)

    print(f"\n{'='*60}")
    print(f"Seeding complete: {success} minted, {skipped} skipped, {failed} failed")
    print(f"{'='*60}")


def main():
    if len(sys.argv) < 2:
        json_path = os.path.join(os.path.dirname(__file__), "demo_posts.json")
        if not os.path.exists(json_path):
            sys.exit("Usage: python seed_demo_posts.py <path/to/demo_posts.json>")
    else:
        json_path = sys.argv[1]

    if not os.path.exists(json_path):
        sys.exit(f"File not found: {json_path}")

    with open(json_path, "r") as f:
        data = json.load(f)

    print(f"Loaded {len(data['users'])} users and {len(data['posts'])} posts")

    seed_users(data["users"])
    seed_posts(data["posts"])


if __name__ == "__main__":
    main()
