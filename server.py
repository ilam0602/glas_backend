from flask import Flask, request, jsonify
from flask_cors import CORS
from web3 import Web3
from web3.datastructures import AttributeDict
import os
import io
import json
import base64
import subprocess
import tempfile
import shutil
import requests
import stripe
from dotenv import load_dotenv
from hexbytes import HexBytes
from openai import OpenAI
from PIL import Image as PILImage
import firebase_admin
from firebase_admin import credentials, storage, firestore as fb_firestore
from firebase_admin import auth as firebase_auth
from google.cloud.firestore_v1.transforms import Increment as FirestoreIncrement
from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from cryptography.fernet import Fernet
from google.cloud import storage as gcs_storage

# Load environment variables from .env file
load_dotenv()

RPC_URL = os.getenv("SEPOLIA_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")
TOKEN_CONTRACT_ADDRESS = os.getenv("TOKEN_CONTRACT_ADDRESS")
GLAS_CONTRACT_ADDRESS = os.getenv("GLAS_CONTRACT_ADDRESS")

# Pinata API keys (ensure these are present in your .env)
PINATA_API_KEY = os.getenv("PINATA_API_KEY")
PINATA_SECRET_API_KEY = os.getenv("PINATA_SECRET_API_KEY")

# Stripe for token purchases
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# OpenAI for content moderation
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Firebase Admin SDK for Firestore (follower lookups, etc.)
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
FIREBASE_STORAGE_BUCKET = os.getenv(
    "FIREBASE_STORAGE_BUCKET", "authensnapmobiletest.firebasestorage.app"
)

firebase_app = None
if FIREBASE_SERVICE_ACCOUNT_JSON:
    cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
    firebase_app = firebase_admin.initialize_app(
        cred,
        {
            "storageBucket": FIREBASE_STORAGE_BUCKET,
        },
    )
    print(f"Firebase Admin SDK initialized with bucket: {FIREBASE_STORAGE_BUCKET}")
else:
    print(
        "WARNING: FIREBASE_SERVICE_ACCOUNT_JSON not set in .env."
    )

# Firestore client for reading follower relationships (unlock-post)
firestore_db = fb_firestore.client() if firebase_app else None

# Google Cloud Storage client for HQ media uploads
# On Cloud Run, authenticates automatically via the service account
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "glas-hidef")
try:
    gcs_client = gcs_storage.Client()
    gcs_bucket = gcs_client.bucket(GCS_BUCKET_NAME)
    print(f"GCS client initialized with bucket: {GCS_BUCKET_NAME}")
except Exception as e:
    gcs_client = None
    gcs_bucket = None
    print(f"WARNING: GCS client init failed: {e}. HQ media upload disabled.")

# Encryption key for private post URLs
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    print(
        f"WARNING: No ENCRYPTION_KEY in .env. Generated temporary key: {ENCRYPTION_KEY}"
    )
    print(
        "Add this to your .env to persist across restarts: ENCRYPTION_KEY="
        + ENCRYPTION_KEY
    )
fernet = Fernet(
    ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY
)



def encrypt_url(url: str) -> str:
    """Encrypt a URL string using Fernet symmetric encryption."""
    return fernet.encrypt(url.encode()).decode()


def decrypt_url(encrypted: str) -> str:
    """Decrypt a Fernet-encrypted URL string."""
    return fernet.decrypt(encrypted.encode()).decode()


def verify_firebase_token(req):
    """Extract and verify Firebase ID token from Authorization header."""
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split("Bearer ")[1]
    try:
        decoded = firebase_auth.verify_id_token(token)
        return decoded["uid"]
    except Exception:
        return None


if not (
    RPC_URL
    and PRIVATE_KEY
    and CONTRACT_ADDRESS
    and PINATA_API_KEY
    and PINATA_SECRET_API_KEY
):
    raise Exception("Missing one or more required environment variables.")

# Connect to Sepolia node
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    raise Exception("Could not connect to Ethereum node.")

# Load the contract ABI
try:
    with open("AuthenSnap.json", "r") as f:
        fullJson = json.load(f)
        abi = fullJson["abi"]
except Exception as e:
    raise Exception("Could not load AuthenSnap.json: " + str(e))

# Instantiate contract (make sure the address is checksummed)
contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=abi)

# Load the Token contract ABI
token_contract = None
if TOKEN_CONTRACT_ADDRESS:
    try:
        with open("Token.json", "r") as f:
            token_json = json.load(f)
            token_abi = token_json["abi"]
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(TOKEN_CONTRACT_ADDRESS), abi=token_abi
        )
        print(f"Token contract loaded at {TOKEN_CONTRACT_ADDRESS}")
    except Exception as e:
        print(f"Warning: Could not load Token.json: {e}")

# Load the Glas contract ABI
glas_contract = None
if GLAS_CONTRACT_ADDRESS:
    try:
        with open("Glas.json", "r") as f:
            glas_json = json.load(f)
            glas_abi = glas_json["abi"]
        glas_contract = w3.eth.contract(
            address=Web3.to_checksum_address(GLAS_CONTRACT_ADDRESS), abi=glas_abi
        )
        print(f"Glas contract loaded at {GLAS_CONTRACT_ADDRESS}")
    except Exception as e:
        print(f"Warning: Could not load Glas.json: {e}")

# Get account object from the private key
account = w3.eth.account.from_key(PRIVATE_KEY)

app = Flask(__name__)
CORS(app, origins=[
    "http://localhost:3000",
    "http://localhost:3001",
    "https://glassocial.com",
    "https://www.glassocial.com",
    "https://glas-verified-social-reality-486202920754.us-central1.run.app",
])

# ============================================================
# GLAS Withdrawal Constants
# ============================================================
STABLE_TOKEN_USD_RATE = 1000  # 1000 stable tokens = $1
WITHDRAWAL_FEE_PERCENT = 0.05  # 5% fee
DAILY_WITHDRAWAL_CAP_USD = 50.0  # $50/day per user
POOL_DRAIN_CAP_PERCENT = 0.10  # max 10% of pool per withdrawal
BURN_ADDRESS = "0x000000000000000000000000000000000000dEaD"

# ============================================================
# Explore Feed Ranking Constants
# ============================================================
COMMENT_WEIGHT = 3.0
GRAVITY = 1.5
INITIAL_RANK_SCORE = 0.354  # 1 / 2^1.5


def compute_rank_score(likes_count, comments_count, created_at_seconds):
    """
    Hacker News-style ranking: (engagement + 1) / (age_hours + 2) ^ gravity
    Returns a float score.
    """
    import time

    engagement = likes_count + (comments_count * COMMENT_WEIGHT)
    now = time.time()
    age_hours = max(0, (now - created_at_seconds) / 3600.0)
    score = (engagement + 1) / ((age_hours + 2) ** GRAVITY)
    return score


def get_daily_withdrawal_usd(user_id: str) -> float:
    """Query Firestore for total USD withdrawn by user in the last 24 hours."""
    if not firestore_db:
        return 0.0
    try:
        from datetime import datetime, timedelta

        cutoff = datetime.utcnow() - timedelta(hours=24)
        withdrawals_ref = (
            firestore_db.collection("withdrawalHistory")
            .document(user_id)
            .collection("withdrawals")
            .where("timestamp", ">=", cutoff)
        )
        total = 0.0
        for doc in withdrawals_ref.stream():
            total += doc.to_dict().get("usdValue", 0.0)
        return total
    except Exception as e:
        print(f"Error querying withdrawal history: {e}")
        return 0.0


def record_withdrawal(
    user_id: str, usd_value: float, glas_amount: float, stable_burned: float
):
    """Log a withdrawal to Firestore."""
    if not firestore_db:
        return
    try:
        from datetime import datetime

        firestore_db.collection("withdrawalHistory").document(user_id).collection(
            "withdrawals"
        ).add(
            {
                "usdValue": usd_value,
                "glasAmount": glas_amount,
                "stableBurned": stable_burned,
                "timestamp": datetime.utcnow(),
            }
        )
    except Exception as e:
        print(f"Error recording withdrawal: {e}")


def compute_user_id_hash(user_id: str) -> bytes:
    """Compute keccak256 hash of userId, matching Solidity's keccak256(abi.encodePacked(userId))."""
    return Web3.keccak(text=user_id)


def make_json_serializable(data):
    """
    Recursively convert HexBytes and AttributeDict objects to JSON-serializable types.
    """
    if isinstance(data, AttributeDict):
        return make_json_serializable(dict(data))
    elif isinstance(data, dict):
        return {k: make_json_serializable(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [make_json_serializable(item) for item in data]
    elif isinstance(data, HexBytes):
        return data.hex()
    elif isinstance(data, bytes):
        return data.hex()
    else:
        return data


def cancel_pending_transactions():
    """
    Cancels all pending transactions by sending 0 ETH transactions with high gas
    to yourself for each pending nonce.
    """
    confirmed_nonce = w3.eth.get_transaction_count(account.address)
    pending_nonce = w3.eth.get_transaction_count(account.address, "pending")

    if pending_nonce > confirmed_nonce:
        print(
            f"Found {pending_nonce - confirmed_nonce} pending transactions. Cancelling..."
        )

        latest_block = w3.eth.get_block("latest")
        base_fee = latest_block["baseFeePerGas"]
        max_priority_fee = w3.to_wei(50, "gwei")
        max_fee = (base_fee * 5) + max_priority_fee

        cancelled_txs = []

        for nonce in range(confirmed_nonce, pending_nonce):
            try:
                cancel_txn = {
                    "from": account.address,
                    "to": account.address,
                    "value": 0,
                    "gas": 21000,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": max_priority_fee,
                    "nonce": nonce,
                    "chainId": w3.eth.chain_id,
                }

                signed_cancel = w3.eth.account.sign_transaction(cancel_txn, PRIVATE_KEY)
                cancel_tx_hash = w3.eth.send_raw_transaction(
                    signed_cancel.raw_transaction
                )
                cancelled_txs.append(cancel_tx_hash.hex())
                print(f"Sent cancellation for nonce {nonce}: {cancel_tx_hash.hex()}")

            except Exception as e:
                print(f"Error cancelling nonce {nonce}: {e}")

        return cancelled_txs
    else:
        print("No pending transactions to cancel.")
        return []


def analyze_image(base64_image_str):
    """
    Calls OpenAI GPT-4o Vision to perform content moderation and generate
    descriptive tags/metadata for the image in a single request.
    Returns {"flagged": bool, "reason": str, "tags": list, "description": str}
    On failure, returns safe defaults so mints are never blocked.
    """
    if not openai_client:
        print("OpenAI not configured, skipping image analysis")
        return {"flagged": False, "reason": "", "tags": [], "description": ""}

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Analyze this image and respond with ONLY valid JSON (no markdown):\n"
                                "{\n"
                                '  "flagged": true/false,\n'
                                '  "reason": "if flagged, explain why (violence, nudity, hate speech, drugs), otherwise empty string",\n'
                                '  "tags": ["up to 5 descriptive tags, e.g. nature, portrait, food, urban, sunset"],\n'
                                '  "description": "one sentence describing the image content"\n'
                                "}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image_str}",
                            },
                        },
                    ],
                }
            ],
            max_tokens=250,
        )

        result_text = response.choices[0].message.content.strip()
        # Parse JSON from response (handle markdown code blocks)
        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
            result_text = result_text.strip()
        result = json.loads(result_text)
        # Ensure expected fields exist
        result.setdefault("flagged", False)
        result.setdefault("reason", "")
        result.setdefault("tags", [])
        result.setdefault("description", "")
        print(f"Image analysis result: {result}")
        return result
    except Exception as e:
        print(f"Image analysis failed (allowing mint): {e}")
        return {"flagged": False, "reason": "", "tags": [], "description": ""}


def analyze_video(video_bytes):
    """
    Extracts a thumbnail and audio from a video, then sends both to OpenAI
    for content moderation and tagging (same output format as analyze_image).
    """
    if not openai_client:
        print("OpenAI not configured, skipping video analysis")
        return {"flagged": False, "reason": "", "tags": [], "description": ""}

    tmp_dir = tempfile.mkdtemp()
    try:
        input_path = os.path.join(tmp_dir, "input.mp4")
        with open(input_path, "wb") as f:
            f.write(video_bytes)

        # 1. Extract thumbnail frame at 1s
        thumbnail_bytes = None
        try:
            thumb_result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    input_path,
                    "-ss",
                    "1",
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=30,
            )
            if thumb_result.returncode == 0 and thumb_result.stdout:
                thumbnail_bytes = thumb_result.stdout
        except Exception as e:
            print(f"Thumbnail extraction failed: {e}")

        if not thumbnail_bytes:
            print("Could not extract thumbnail, skipping video analysis")
            return {"flagged": False, "reason": "", "tags": [], "description": ""}

        thumbnail_b64 = base64.b64encode(thumbnail_bytes).decode("utf-8")

        # 2. Extract audio and transcribe with Whisper
        transcript = ""
        try:
            audio_path = os.path.join(tmp_dir, "audio.mp3")
            audio_result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    input_path,
                    "-vn",
                    "-acodec",
                    "libmp3lame",
                    "-q:a",
                    "5",
                    audio_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if (
                audio_result.returncode == 0
                and os.path.exists(audio_path)
                and os.path.getsize(audio_path) > 0
            ):
                with open(audio_path, "rb") as af:
                    transcription = openai_client.audio.transcriptions.create(
                        model="whisper-1",
                        file=af,
                    )
                transcript = transcription.text.strip()
                print(f"Audio transcript: {transcript[:200]}")
        except Exception as e:
            print(f"Audio transcription failed (continuing without): {e}")

        # 3. Send thumbnail + transcript to GPT-4o Vision
        transcript_text = transcript if transcript else "No audio"
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Analyze this video thumbnail and audio transcript. "
                                "Respond with ONLY valid JSON (no markdown):\n"
                                "{\n"
                                '  "flagged": true/false,\n'
                                '  "reason": "if flagged, explain why (violence, nudity, hate speech, drugs), otherwise empty string",\n'
                                '  "tags": ["up to 5 descriptive tags, e.g. nature, portrait, food, urban, sunset"],\n'
                                '  "description": "one sentence describing the video content"\n'
                                "}\n\n"
                                f"Audio transcript: {transcript_text}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{thumbnail_b64}",
                            },
                        },
                    ],
                }
            ],
            max_tokens=250,
        )

        result_text = response.choices[0].message.content.strip()
        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
            result_text = result_text.strip()
        result = json.loads(result_text)
        result.setdefault("flagged", False)
        result.setdefault("reason", "")
        result.setdefault("tags", [])
        result.setdefault("description", "")
        print(f"Video analysis result: {result}")
        return result
    except Exception as e:
        print(f"Video analysis failed (allowing mint): {e}")
        return {"flagged": False, "reason": "", "tags": [], "description": ""}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def compress_image(base64_str, target_height):
    """
    Decode base64 image, resize to target_height maintaining aspect ratio,
    re-encode as JPEG with quality 85. Returns base64 string.
    """
    img_data = base64.b64decode(base64_str)
    img = PILImage.open(io.BytesIO(img_data))

    # Only downscale, never upscale
    if img.height > target_height:
        ratio = target_height / img.height
        new_width = int(img.width * ratio)
        img = img.resize((new_width, target_height), PILImage.LANCZOS)

    # Convert to RGB if needed (e.g. RGBA PNGs)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def compress_video(file_bytes, target_height, max_fps=30, strip_metadata=False):
    """
    Use FFmpeg to transcode video to target_height and max_fps.
    If strip_metadata is True, removes all metadata (GPS, device info, etc.).
    Returns compressed video bytes.
    """
    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, "input.mp4")
    output_path = os.path.join(tmp_dir, "output.mp4")

    try:
        with open(input_path, "wb") as f:
            f.write(file_bytes)

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-vf",
            f"scale=-2:{target_height}",
            "-r",
            str(max_fps),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
        if strip_metadata:
            cmd += ["-map_metadata", "-1"]
        cmd.append(output_path)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise Exception(f"FFmpeg compression failed: {result.stderr[-500:]}")

        with open(output_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def upload_to_firebase_storage(file_bytes, filename, content_type):
    """
    Upload file bytes to GCS under hq_media/ (private bucket).
    Returns a server media URL that redirects to a signed GCS URL.
    """
    if not gcs_bucket:
        print("GCS not configured, skipping HQ upload")
        return None

    try:
        blob_path = f"hq_media/{filename}"
        blob = gcs_bucket.blob(blob_path)
        blob.upload_from_string(file_bytes, content_type=content_type)
        # Return server URL that will generate signed URL redirects
        server_url = os.getenv("SERVER_URL", "https://glas-backend-486202920754.us-central1.run.app")
        media_url = f"{server_url}/media/{blob_path}"
        print(f"GCS upload success: {blob_path} -> {media_url}")
        return media_url
    except Exception as e:
        print(f"GCS upload failed: {e}")
        return None


def pin_file_to_pinata(base64_image_str, media_type="photo"):
    """
    Takes a base64-encoded file string, uploads it to Pinata, and returns an ipfs:// URI.
    """
    file_data = base64.b64decode(base64_image_str)

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

    response = requests.post(url, files=files, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Pinata upload error: {response.text}")

    ipfs_hash = response.json()["IpfsHash"]
    ipfs_url = f"ipfs://{ipfs_hash}"
    return ipfs_url


def pin_metadata_to_pinata(
    image_ipfs_url,
    token_name,
    description="Photo minted on AuthenSnap",
    media_type="photo",
):
    """
    Creates an ERC-721 metadata JSON with the image/video URL and pins it to Pinata.
    Returns the ipfs:// URI of the metadata JSON.

    OpenSea (and other marketplaces) expect tokenURI to point to a JSON file like:
    {
      "name": "AuthenSnap #1",
      "description": "...",
      "image": "ipfs://QmImageHash"
    }
    """
    gateway_url = image_ipfs_url.replace(
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

    response = requests.post(url, json=metadata, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Pinata metadata upload error: {response.text}")

    ipfs_hash = response.json()["IpfsHash"]
    return f"ipfs://{ipfs_hash}"


def resolve_image_from_token_uri(token_uri):
    """
    If tokenURI points to a JSON metadata file, fetch it and extract the image URL.
    If it points directly to an image (legacy), return as-is.
    """
    try:
        if token_uri.startswith("ipfs://"):
            fetch_url = "https://ipfs.io/ipfs/" + token_uri[7:]
        elif token_uri.startswith("http"):
            fetch_url = token_uri
        else:
            return token_uri

        resp = requests.get(fetch_url, timeout=10)
        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type or resp.text.strip().startswith("{"):
                metadata = resp.json()
                return metadata.get("image", token_uri)
        return token_uri
    except Exception:
        return token_uri


def extract_token_id_from_receipt(receipt, contract):
    """
    Extract the tokenId from the Transfer event in the transaction receipt.
    """
    transfer_event = contract.events.Transfer()
    logs = transfer_event.process_receipt(receipt)

    if logs:
        token_id = int(logs[0]["args"]["tokenId"])
        return token_id
    else:
        raise Exception("No Transfer event found in receipt")


def extract_virtual_mint_token_id(receipt, contract):
    """
    Extract the tokenId from the VirtualMint event in the transaction receipt.
    """
    virtual_mint_event = contract.events.VirtualMint()
    logs = virtual_mint_event.process_receipt(receipt)

    if logs:
        token_id = int(logs[0]["args"]["tokenId"])
        return token_id
    else:
        raise Exception("No VirtualMint event found in receipt")


def get_gas_params():
    """Get aggressive EIP-1559 gas parameters."""
    latest_block = w3.eth.get_block("latest")
    base_fee = latest_block["baseFeePerGas"]
    max_priority_fee = w3.to_wei(30, "gwei")
    max_fee = (base_fee * 5) + max_priority_fee
    min_max_fee = w3.to_wei(600, "gwei")
    max_fee = max(max_fee, min_max_fee)
    return max_fee, max_priority_fee


def get_nonce():
    """Get nonce, cancelling pending transactions if needed."""
    confirmed_nonce = w3.eth.get_transaction_count(account.address)
    pending_nonce = w3.eth.get_transaction_count(account.address, "pending")

    if pending_nonce > confirmed_nonce:
        print(
            f"WARNING: {pending_nonce - confirmed_nonce} pending transactions detected!"
        )
        cancel_pending_transactions()
        import time

        time.sleep(5)
        return w3.eth.get_transaction_count(account.address, "pending")
    return pending_nonce


def send_transaction(txn):
    """Sign, send, and wait for a transaction. Returns (tx_hash, receipt)."""
    signed_txn = w3.eth.account.sign_transaction(txn, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
    print(f"Transaction sent: https://sepolia.etherscan.io/tx/{tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    print(f"Transaction mined in block {receipt['blockNumber']}")
    return tx_hash, receipt


@app.route("/tokens/<wallet_address>", methods=["GET"])
def get_tokens(wallet_address):
    """
    GET endpoint that takes a wallet address in the URL and returns all token data
    (tokenId and tokenURI) for tokens owned by that wallet.
    """
    try:
        checksum_address = Web3.to_checksum_address(wallet_address)
    except Exception as e:
        print(e)
        return jsonify({"error": "Invalid wallet address format"}), 400

    try:
        balance = contract.functions.balanceOf(checksum_address).call()
    except Exception as e:
        print(e)
        return jsonify({"error": f"Error fetching balance: {e}"}), 500

    tokens = []
    for i in range(balance):
        try:
            token_id = contract.functions.tokenOfOwnerByIndex(
                checksum_address, i
            ).call()
            uri = contract.functions.tokenURI(token_id).call()
            image_url = resolve_image_from_token_uri(uri)
            tokens.append(
                {"tokenId": int(token_id), "tokenURI": image_url, "metadataURI": uri}
            )
        except Exception as e:
            print(e)
            continue

    return jsonify({"wallet": checksum_address, "tokens": tokens})


@app.route("/tokens/virtual/<user_id>", methods=["GET"])
def get_virtual_tokens(user_id):
    """
    GET endpoint that returns all tokens in a user's virtual wallet.
    """
    try:
        user_id_hash = compute_user_id_hash(user_id)
        token_ids = contract.functions.getVirtualTokens(user_id_hash).call()

        tokens = []
        for token_id in token_ids:
            try:
                uri = contract.functions.tokenURI(int(token_id)).call()
                image_url = resolve_image_from_token_uri(uri)
                tokens.append(
                    {
                        "tokenId": int(token_id),
                        "tokenURI": image_url,
                        "metadataURI": uri,
                    }
                )
            except Exception as e:
                print(f"Error fetching URI for token {token_id}: {e}")
                continue

        return jsonify(
            {
                "userId": user_id,
                "virtualBalance": len(tokens),
                "tokens": tokens,
            }
        )
    except Exception as e:
        print(e)
        return jsonify({"error": f"Error fetching virtual tokens: {e}"}), 500


@app.route("/cancel-pending", methods=["POST"])
def cancel_pending():
    """
    Endpoint to manually cancel all pending transactions.
    """
    try:
        cancelled = cancel_pending_transactions()
        return jsonify(
            {
                "success": True,
                "cancelled_transactions": cancelled,
                "count": len(cancelled),
            }
        )
    except Exception as e:
        print(e)
        return jsonify({"error": f"Error cancelling transactions: {e}"}), 500


@app.route("/mint", methods=["POST"])
def mint_nft():
    """
    POST endpoint that takes a JSON body with:
      - image: Base64-encoded image data
      - userId: the Firebase user ID (for virtual wallet minting)

    Also supports legacy format with walletAddress for backward compatibility.

    1) Decodes the image and uploads it to Pinata (IPFS)
    2) Creates ERC-721 metadata JSON and uploads to Pinata
    3) Mints with metadata URI on-chain, returns image URI to client
    """
    data = request.get_json()
    if not data or "image" not in data:
        return jsonify({"error": "Missing image in request body"}), 400

    base64_image = data["image"]
    user_id = data.get("userId")
    wallet_address = data.get("walletAddress")
    media_type = data.get("mediaType", "photo")
    is_private = data.get("isPrivate", False)

    if not user_id and not wallet_address:
        return (
            jsonify({"error": "Missing userId or walletAddress in request body"}),
            400,
        )

    # 1. Compress media for IPFS (720p) and Firebase Storage (1080p)
    hq_media_url = None
    raw_bytes = base64.b64decode(base64_image)
    try:
        if media_type == "video":
            # 720p version for IPFS/NFT metadata (strip metadata for privacy)
            lq_bytes = compress_video(
                raw_bytes, target_height=720, max_fps=30, strip_metadata=True
            )
            lq_base64 = base64.b64encode(lq_bytes).decode("utf-8")
            # 1080p version for Firebase Storage (HQ in-app display, keep metadata)
            hq_bytes = compress_video(raw_bytes, target_height=1080, max_fps=60)
        else:
            # 720p version for IPFS/NFT metadata
            lq_base64 = compress_image(base64_image, target_height=720)
            # 1080p version for Firebase Storage (HQ in-app display)
            hq_base64 = compress_image(base64_image, target_height=1080)
            hq_bytes = base64.b64decode(hq_base64)
    except Exception as e:
        print(f"Compression failed, using original: {e}")
        lq_base64 = base64_image
        hq_bytes = raw_bytes

    # 1a. Pin 720p version to IPFS
    try:
        image_ipfs_url = pin_file_to_pinata(lq_base64, media_type=media_type)
        print(
            f"{'Video' if media_type == 'video' else 'Image'} (720p) uploaded to IPFS: {image_ipfs_url}"
        )
    except Exception as e:
        print(e)
        return jsonify({"error": f"Error uploading to Pinata: {e}"}), 500

    # 1b. Upload 1080p version to Firebase Storage
    # (tokenId not known yet, will use a placeholder and rename after mint)
    # We'll upload after minting when we have the tokenId

    # 1c. Run AI analysis: moderation + content tagging
    if media_type == "video":
        analysis = analyze_video(raw_bytes)
    else:
        analysis = analyze_image(base64_image)

    # 2. Get nonce (cancel pending if needed)
    nonce = get_nonce()
    max_fee, max_priority_fee = get_gas_params()

    # 3. Create and pin ERC-721 metadata JSON (so OpenSea shows the content)
    try:
        token_name = "AuthenSnap Video" if media_type == "video" else "AuthenSnap Photo"
        metadata_ipfs_url = pin_metadata_to_pinata(
            image_ipfs_url, token_name, media_type=media_type
        )
        print(f"Metadata uploaded to IPFS: {metadata_ipfs_url}")
    except Exception as e:
        print(e)
        return jsonify({"error": f"Error uploading metadata to Pinata: {e}"}), 500

    print(f"Building transaction with nonce: {nonce}")
    print(f"  max_fee: {w3.from_wei(max_fee, 'gwei'):.2f} gwei")
    print(f"  max_priority_fee: {w3.from_wei(max_priority_fee, 'gwei'):.2f} gwei")

    # 4. Build and send transaction (store metadata URI on-chain, not the raw image)
    try:
        if user_id:
            user_id_hash = compute_user_id_hash(user_id)
            mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
        else:
            recipient = Web3.to_checksum_address(wallet_address)
            mint_fn = contract.get_function_by_signature("mint(address,string)")
            mint_fn = mint_fn(recipient, metadata_ipfs_url)

        txn = mint_fn.build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 500000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )
    except Exception as e:
        print(e)
        return jsonify({"error": f"Error building transaction: {e}"}), 500

    try:
        tx_hash, receipt = send_transaction(txn)

        if user_id:
            token_id = extract_virtual_mint_token_id(receipt, contract)
        else:
            token_id = extract_token_id_from_receipt(receipt, contract)
    except Exception as e:
        print(f"Error details: {e}")
        if "tx_hash" in locals():
            return (
                jsonify(
                    {
                        "error": f"Transaction timeout or error: {e}",
                        "transaction_hash": tx_hash.hex(),
                        "check_status": f"https://sepolia.etherscan.io/tx/{tx_hash.hex()}",
                    }
                ),
                500,
            )
        return jsonify({"error": f"Error sending transaction: {e}"}), 500

    # Upload 1080p HQ version to Firebase Storage (now that we have tokenId)
    if media_type == "video":
        ext = "mp4"
        content_type = "video/mp4"
    else:
        ext = "jpg"
        content_type = "image/jpeg"
    hq_media_url = upload_to_firebase_storage(
        hq_bytes, f"{token_id}.{ext}", content_type
    )
    if hq_media_url:
        print(f"HQ media (1080p) uploaded to Firebase Storage: {hq_media_url}")

    # Generate and upload video thumbnail
    thumbnail_url = None
    if media_type == "video":
        try:
            thumb_tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            thumb_tmp.write(raw_bytes)
            thumb_tmp.close()
            thumb_result = subprocess.run(
                [
                    "ffmpeg", "-i", thumb_tmp.name,
                    "-ss", "0", "-frames:v", "1",
                    "-vf", "scale=-1:480",
                    "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                ],
                capture_output=True,
                timeout=15,
            )
            os.unlink(thumb_tmp.name)
            if thumb_result.returncode == 0 and thumb_result.stdout:
                thumbnail_url = upload_to_firebase_storage(
                    thumb_result.stdout, f"{token_id}_thumb.jpg", "image/jpeg"
                )
                if thumbnail_url:
                    print(f"Video thumbnail uploaded: {thumbnail_url}")
        except Exception as e:
            print(f"Video thumbnail generation failed (non-blocking): {e}")

    # For private posts, encrypt URLs before returning to client
    returned_ipfs_uri = image_ipfs_url
    returned_hq_url = hq_media_url
    if is_private:
        returned_ipfs_uri = encrypt_url(image_ipfs_url)
        if hq_media_url:
            returned_hq_url = encrypt_url(hq_media_url)

    # Return the file URI (not the metadata URI) so the mobile app can display it directly
    response_data = {
        "transaction_hash": tx_hash.hex(),
        "token_id": token_id,
        "ipfs_uri": returned_ipfs_uri,
        "metadata_uri": metadata_ipfs_url,
        "mediaType": media_type,
        "isPrivate": is_private,
        "flagged": analysis.get("flagged", False),
        "flag_reason": analysis.get("reason", ""),
        "tags": analysis.get("tags", []),
        "description": analysis.get("description", ""),
        "receipt": make_json_serializable(receipt),
    }

    if returned_hq_url:
        response_data["hqMediaUrl"] = returned_hq_url
    if thumbnail_url:
        response_data["thumbnailUrl"] = thumbnail_url

    if user_id:
        response_data["user_id"] = user_id
    else:
        response_data["wallet_address"] = Web3.to_checksum_address(wallet_address)

    return jsonify(response_data)


@app.route("/media/<path:blob_path>", methods=["GET"])
def serve_media(blob_path):
    """
    Streams a file from a private GCS bucket to the client.
    Used by the mobile app to load HQ media without public bucket access.
    """
    from flask import Response

    if not gcs_bucket:
        return jsonify({"error": "Storage not configured"}), 500

    blob = gcs_bucket.blob(blob_path)
    if not blob.exists():
        return jsonify({"error": "File not found"}), 404

    content = blob.download_as_bytes()
    content_type = blob.content_type or "application/octet-stream"

    return Response(
        content,
        content_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
        },
    )


@app.route("/unlock-post", methods=["POST"])
def unlock_post():
    """
    POST endpoint to decrypt private post URLs for authorized viewers.
    Body: { tokenId, viewerId }
    Returns decrypted ipfsUrl and hqMediaUrl if viewer is the owner or a follower.
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    data = request.get_json()
    if not data or "tokenId" not in data or "viewerId" not in data:
        return jsonify({"error": "Missing tokenId or viewerId"}), 400

    token_id = str(data["tokenId"])
    viewer_id = data["viewerId"]

    try:
        # Read the post from Firestore
        post_ref = firestore_db.collection("posts").document(token_id)
        post_doc = post_ref.get()

        if not post_doc.exists:
            return jsonify({"error": "Post not found"}), 404

        post_data = post_doc.to_dict()
        owner_id = post_data.get("userId")
        is_private = post_data.get("isPrivate", False)

        if not is_private:
            # Not private, return URLs as-is (they're not encrypted)
            return jsonify(
                {
                    "ipfsUrl": post_data.get("ipfsUrl", ""),
                    "hqMediaUrl": post_data.get("hqMediaUrl", ""),
                }
            )

        # Check authorization: owner or follower
        is_owner = viewer_id == owner_id
        is_follower = False

        if not is_owner:
            follower_ref = (
                firestore_db.collection("users")
                .document(owner_id)
                .collection("followers")
                .document(viewer_id)
            )
            follower_doc = follower_ref.get()
            is_follower = follower_doc.exists

        if not is_owner and not is_follower:
            return jsonify({"error": "Not authorized to view this private post"}), 403

        # Decrypt URLs
        encrypted_ipfs = post_data.get("ipfsUrl", "")
        encrypted_hq = post_data.get("hqMediaUrl", "")

        decrypted_ipfs = ""
        decrypted_hq = ""

        try:
            if encrypted_ipfs:
                decrypted_ipfs = decrypt_url(encrypted_ipfs)
        except Exception:
            decrypted_ipfs = encrypted_ipfs  # Might be an old unencrypted URL

        try:
            if encrypted_hq:
                decrypted_hq = decrypt_url(encrypted_hq)
        except Exception:
            decrypted_hq = encrypted_hq

        return jsonify(
            {
                "ipfsUrl": decrypted_ipfs,
                "hqMediaUrl": decrypted_hq,
            }
        )

    except Exception as e:
        print(f"Unlock post error: {e}")
        return jsonify({"error": f"Error unlocking post: {e}"}), 500


@app.route("/export", methods=["POST"])
def export_token():
    """
    POST endpoint to export a token from virtual wallet to a real address.
    Body: { userId, tokenId, toAddress }
    """
    data = request.get_json()
    if (
        not data
        or "userId" not in data
        or "tokenId" not in data
        or "toAddress" not in data
    ):
        return jsonify({"error": "Missing userId, tokenId, or toAddress"}), 400

    user_id = data["userId"]
    token_id = int(data["tokenId"])
    to_address = data["toAddress"]

    try:
        recipient = Web3.to_checksum_address(to_address)
    except Exception as e:
        return jsonify({"error": "Invalid toAddress format"}), 400

    try:
        user_id_hash = compute_user_id_hash(user_id)
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        export_fn = contract.functions.exportToken(user_id_hash, token_id, recipient)
        txn = export_fn.build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)

        return jsonify(
            {
                "success": True,
                "transaction_hash": tx_hash.hex(),
                "token_id": token_id,
                "exported_to": recipient,
                "receipt": make_json_serializable(receipt),
            }
        )
    except Exception as e:
        print(f"Export error: {e}")
        return jsonify({"error": f"Error exporting token: {e}"}), 500


@app.route("/transfer-post", methods=["POST"])
def transfer_post():
    """
    POST endpoint to transfer a virtual NFT from one user to another.
    Body: { fromUserId, toUserId, tokenId }
    """
    data = request.get_json()
    if (
        not data
        or "fromUserId" not in data
        or "toUserId" not in data
        or "tokenId" not in data
    ):
        return jsonify({"error": "Missing fromUserId, toUserId, or tokenId"}), 400

    from_user_id = data["fromUserId"]
    to_user_id = data["toUserId"]
    token_id = int(data["tokenId"])

    try:
        from_hash = compute_user_id_hash(from_user_id)
        to_hash = compute_user_id_hash(to_user_id)
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        transfer_fn = contract.functions.transferVirtual(from_hash, to_hash, token_id)
        txn = transfer_fn.build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)

        return jsonify(
            {
                "success": True,
                "transaction_hash": tx_hash.hex(),
                "token_id": token_id,
                "from_user_id": from_user_id,
                "to_user_id": to_user_id,
                "receipt": make_json_serializable(receipt),
            }
        )
    except Exception as e:
        print(f"Transfer error: {e}")
        return jsonify({"error": f"Error transferring post: {e}"}), 500


@app.route("/import", methods=["POST"])
def import_token():
    """
    POST endpoint to import a token from a real address into a virtual wallet.
    The token must be approved for the contract first.
    Body: { userId, tokenId }
    """
    data = request.get_json()
    if not data or "userId" not in data or "tokenId" not in data:
        return jsonify({"error": "Missing userId or tokenId"}), 400

    user_id = data["userId"]
    token_id = int(data["tokenId"])

    try:
        user_id_hash = compute_user_id_hash(user_id)
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        import_fn = contract.functions.importToken(user_id_hash, token_id)
        txn = import_fn.build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)

        return jsonify(
            {
                "success": True,
                "transaction_hash": tx_hash.hex(),
                "token_id": token_id,
                "user_id": user_id,
                "receipt": make_json_serializable(receipt),
            }
        )
    except Exception as e:
        print(f"Import error: {e}")
        return jsonify({"error": f"Error importing token: {e}"}), 500


# ============================================================
# Token (ERC20) Endpoints
# ============================================================


@app.route("/create-account", methods=["POST"])
def create_account():
    """
    POST endpoint to create a new user account with initial token balance.
    Body: { userId }
    Mints 10,000 tokens to the user's virtual wallet.
    """
    if not token_contract:
        return jsonify({"error": "Token contract not configured"}), 500

    data = request.get_json()
    if not data or "userId" not in data:
        return jsonify({"error": "Missing userId"}), 400

    user_id = data["userId"]
    user_id_hash = compute_user_id_hash(user_id)
    amount = w3.to_wei(10000, "ether")  # 10,000 tokens

    try:
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        txn = token_contract.functions.mintToVirtual(
            user_id_hash, amount
        ).build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)

        return jsonify(
            {
                "success": True,
                "userId": user_id,
                "tokensGranted": 10000,
                "transaction_hash": tx_hash.hex(),
            }
        )
    except Exception as e:
        print(f"Create account error: {e}")
        return jsonify({"error": f"Error creating account: {e}"}), 500


@app.route("/token-balance/<user_id>", methods=["GET"])
def get_token_balance(user_id):
    """
    GET endpoint to check a user's token balance.
    Returns balance in human-readable units (not wei).
    """
    if not token_contract:
        return jsonify({"error": "Token contract not configured"}), 500

    try:
        user_id_hash = compute_user_id_hash(user_id)
        balance_wei = token_contract.functions.getVirtualBalance(user_id_hash).call()
        balance = w3.from_wei(balance_wei, "ether")

        return jsonify(
            {
                "userId": user_id,
                "balance": float(balance),
            }
        )
    except Exception as e:
        print(f"Token balance error: {e}")
        return jsonify({"error": f"Error fetching token balance: {e}"}), 500


@app.route("/sync-token-balance", methods=["POST"])
def sync_token_balance():
    """
    POST endpoint to sync accumulated Firestore token balance to blockchain.
    Called automatically when a user's off-chain balance exceeds on-chain by 1000+.
    Body: { userId, additionalAmount }
    """
    if not token_contract:
        return jsonify({"error": "Token contract not configured"}), 500

    data = request.get_json()
    if not data or "userId" not in data or "additionalAmount" not in data:
        return jsonify({"error": "Missing userId or additionalAmount"}), 400

    user_id = data["userId"]
    additional_amount = float(data["additionalAmount"])

    try:
        user_id_hash = compute_user_id_hash(user_id)
        amount_wei = w3.to_wei(additional_amount, "ether")
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        txn = token_contract.functions.mintToVirtual(
            user_id_hash, amount_wei
        ).build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)

        return jsonify(
            {
                "success": True,
                "userId": user_id,
                "additionalAmount": additional_amount,
                "transaction_hash": tx_hash.hex(),
            }
        )
    except Exception as e:
        print(f"Sync token balance error: {e}")
        return jsonify({"error": f"Error syncing token balance: {e}"}), 500


@app.route("/set-glas-price", methods=["POST"])
def set_glas_price():
    """
    POST endpoint for admin to set GLAS USD price.
    Body: { priceUsd }
    """
    if not glas_contract:
        return jsonify({"error": "Glas contract not configured"}), 500

    data = request.get_json()
    if not data or "priceUsd" not in data:
        return jsonify({"error": "Missing priceUsd"}), 400

    price_usd = float(data["priceUsd"])
    if price_usd <= 0:
        return jsonify({"error": "Price must be positive"}), 400

    try:
        price_wei = w3.to_wei(price_usd, "ether")
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        txn = glas_contract.functions.setPrice(price_wei).build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 100000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)

        return jsonify(
            {
                "success": True,
                "priceUsd": price_usd,
                "transaction_hash": tx_hash.hex(),
            }
        )
    except Exception as e:
        print(f"Set GLAS price error: {e}")
        return jsonify({"error": f"Error setting GLAS price: {e}"}), 500


@app.route("/glas-price", methods=["GET"])
def get_glas_price():
    """GET endpoint to return current GLAS price in USD."""
    if not glas_contract:
        return jsonify({"error": "Glas contract not configured"}), 500

    try:
        price_wei = glas_contract.functions.glasPriceUsd().call()
        price_usd = float(w3.from_wei(price_wei, "ether"))
        return jsonify({"glasPriceUsd": price_usd})
    except Exception as e:
        print(f"Get GLAS price error: {e}")
        return jsonify({"error": f"Error fetching GLAS price: {e}"}), 500


@app.route("/glas-balance/<user_id>", methods=["GET"])
def get_glas_balance(user_id):
    """GET endpoint to return user's virtual GLAS balance."""
    if not glas_contract:
        return jsonify({"error": "Glas contract not configured"}), 500

    try:
        user_id_hash = compute_user_id_hash(user_id)
        balance_wei = glas_contract.functions.getVirtualBalance(user_id_hash).call()
        balance = float(w3.from_wei(balance_wei, "ether"))
        return jsonify({"userId": user_id, "glasBalance": balance})
    except Exception as e:
        print(f"GLAS balance error: {e}")
        return jsonify({"error": f"Error fetching GLAS balance: {e}"}), 500


@app.route("/withdrawal-info/<user_id>", methods=["GET"])
def get_withdrawal_info(user_id):
    """GET endpoint to return withdrawal limits, conversion rate, and pool info."""
    if not glas_contract:
        return jsonify({"error": "Glas contract not configured"}), 500

    try:
        price_wei = glas_contract.functions.glasPriceUsd().call()
        glas_price_usd = float(w3.from_wei(price_wei, "ether"))

        pool_balance_wei = glas_contract.functions.getPoolBalance().call()
        pool_balance = float(w3.from_wei(pool_balance_wei, "ether"))

        withdrawn_today = get_daily_withdrawal_usd(user_id)
        remaining_usd = max(0, DAILY_WITHDRAWAL_CAP_USD - withdrawn_today)

        max_single_glas = pool_balance * POOL_DRAIN_CAP_PERCENT

        return jsonify(
            {
                "dailyCapUsd": DAILY_WITHDRAWAL_CAP_USD,
                "withdrawnTodayUsd": withdrawn_today,
                "remainingUsd": remaining_usd,
                "glasPriceUsd": glas_price_usd,
                "feePercent": WITHDRAWAL_FEE_PERCENT,
                "maxSingleWithdrawalGlas": max_single_glas,
                "poolBalance": pool_balance,
            }
        )
    except Exception as e:
        print(f"Withdrawal info error: {e}")
        return jsonify({"error": f"Error fetching withdrawal info: {e}"}), 500


@app.route("/withdraw-tokens", methods=["POST"])
def withdraw_tokens():
    """
    POST endpoint to withdraw stable tokens by converting to GLAS.
    Burns stable tokens, credits GLAS from pool, optionally exports to real address.
    Body: { userId, amount, toAddress? }
    """
    if not token_contract:
        return jsonify({"error": "Token contract not configured"}), 500
    if not glas_contract:
        return jsonify({"error": "Glas contract not configured"}), 500

    data = request.get_json()
    if not data or "userId" not in data or "amount" not in data:
        return jsonify({"error": "Missing userId or amount"}), 400

    user_id = data["userId"]
    stable_amount = float(data["amount"])
    to_address = data.get("toAddress")

    if stable_amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    # Validate optional toAddress
    recipient = None
    if to_address:
        try:
            recipient = Web3.to_checksum_address(to_address)
        except Exception:
            return jsonify({"error": "Invalid toAddress format"}), 400

    try:
        user_id_hash = compute_user_id_hash(user_id)

        # 1. Calculate USD value: stable_amount / 1000
        usd_value = stable_amount / STABLE_TOKEN_USD_RATE

        # 2. Deduct 5% fee
        fee_usd = usd_value * WITHDRAWAL_FEE_PERCENT
        net_usd = usd_value - fee_usd

        # 3. Get GLAS price
        glas_price_wei = glas_contract.functions.glasPriceUsd().call()
        glas_price_usd = float(w3.from_wei(glas_price_wei, "ether"))
        if glas_price_usd <= 0:
            return jsonify({"error": "GLAS price not set"}), 500

        # Calculate GLAS amount
        glas_amount = net_usd / glas_price_usd

        # 4. Guardrail 1: daily cap
        already_withdrawn = get_daily_withdrawal_usd(user_id)
        if already_withdrawn + net_usd > DAILY_WITHDRAWAL_CAP_USD:
            remaining = max(0, DAILY_WITHDRAWAL_CAP_USD - already_withdrawn)
            return (
                jsonify(
                    {
                        "error": f"Daily withdrawal cap exceeded. Remaining today: ${remaining:.2f}",
                        "remainingUsd": remaining,
                    }
                ),
                400,
            )

        # 5. Guardrail 2: pool cap
        pool_balance_wei = glas_contract.functions.getPoolBalance().call()
        pool_balance = float(w3.from_wei(pool_balance_wei, "ether"))
        max_glas = pool_balance * POOL_DRAIN_CAP_PERCENT
        if glas_amount > max_glas:
            return (
                jsonify(
                    {
                        "error": f"Withdrawal exceeds pool drain cap ({POOL_DRAIN_CAP_PERCENT*100:.0f}% of pool). Max: {max_glas:.2f} GLAS",
                        "maxGlas": max_glas,
                    }
                ),
                400,
            )

        # 6. Sync stable token on-chain if needed
        stable_amount_wei = w3.to_wei(stable_amount, "ether")
        on_chain_balance_wei = token_contract.functions.getVirtualBalance(
            user_id_hash
        ).call()
        if on_chain_balance_wei < stable_amount_wei:
            deficit_wei = stable_amount_wei - on_chain_balance_wei
            print(
                f"Syncing {w3.from_wei(deficit_wei, 'ether')} stable tokens on-chain first."
            )
            nonce = get_nonce()
            max_fee, max_priority_fee = get_gas_params()
            sync_txn = token_contract.functions.mintToVirtual(
                user_id_hash, deficit_wei
            ).build_transaction(
                {
                    "chainId": w3.eth.chain_id,
                    "gas": 200000,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": max_priority_fee,
                    "nonce": nonce,
                }
            )
            send_transaction(sync_txn)

        # 7. Burn stable tokens by exporting to burn address
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()
        burn_txn = token_contract.functions.exportTokens(
            user_id_hash, stable_amount_wei, Web3.to_checksum_address(BURN_ADDRESS)
        ).build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )
        send_transaction(burn_txn)

        # 8. Credit GLAS from pool to user virtual wallet
        glas_amount_wei = w3.to_wei(glas_amount, "ether")
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()
        credit_txn = glas_contract.functions.creditFromPool(
            user_id_hash, glas_amount_wei
        ).build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )
        send_transaction(credit_txn)

        # 9. (Optional) Export GLAS to real address
        export_tx_hash = None
        if recipient:
            nonce = get_nonce()
            max_fee, max_priority_fee = get_gas_params()
            export_txn = glas_contract.functions.exportTokens(
                user_id_hash, glas_amount_wei, recipient
            ).build_transaction(
                {
                    "chainId": w3.eth.chain_id,
                    "gas": 200000,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": max_priority_fee,
                    "nonce": nonce,
                }
            )
            export_hash, _ = send_transaction(export_txn)
            export_tx_hash = export_hash.hex()

        # 10. Record withdrawal in Firestore
        record_withdrawal(user_id, net_usd, glas_amount, stable_amount)

        response = {
            "success": True,
            "stableBurned": stable_amount,
            "feeDeducted": fee_usd,
            "usdValue": net_usd,
            "glasReceived": glas_amount,
            "glasPriceUsd": glas_price_usd,
        }
        if export_tx_hash:
            response["exportTransactionHash"] = export_tx_hash

        return jsonify(response)
    except Exception as e:
        print(f"Withdraw tokens error: {e}")
        return jsonify({"error": f"Error withdrawing tokens: {e}"}), 500



def extract_virtual_burn_token_id(receipt, contract):
    """
    Extract the tokenId from the VirtualBurn event in the transaction receipt.
    """
    virtual_burn_event = contract.events.VirtualBurn()
    logs = virtual_burn_event.process_receipt(receipt)

    if logs:
        token_id = int(logs[0]["args"]["tokenId"])
        return token_id
    else:
        raise Exception("No VirtualBurn event found in receipt")


def pin_file_bytes_to_pinata(file_bytes, filename="stitched.mp4"):
    """
    Upload raw file bytes to Pinata (no base64 encoding).
    Returns an ipfs:// URI.
    """
    files = {"file": (filename, file_bytes)}
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {
        "pinata_api_key": PINATA_API_KEY,
        "pinata_secret_api_key": PINATA_SECRET_API_KEY,
    }
    response = requests.post(url, files=files, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Pinata upload error: {response.text}")
    ipfs_hash = response.json()["IpfsHash"]
    return f"ipfs://{ipfs_hash}"


@app.route("/burn", methods=["POST"])
def burn_nft():
    """
    POST endpoint to burn (delete) an NFT from a virtual wallet.
    Body: { userId, tokenId }
    """
    data = request.get_json()
    if not data or "userId" not in data or "tokenId" not in data:
        return jsonify({"error": "Missing userId or tokenId"}), 400

    user_id = data["userId"]
    token_id = int(data["tokenId"])

    try:
        user_id_hash = compute_user_id_hash(user_id)
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        burn_fn = contract.functions.burnVirtual(user_id_hash, token_id)
        txn = burn_fn.build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)

        return jsonify(
            {
                "success": True,
                "transaction_hash": tx_hash.hex(),
                "token_id": token_id,
                "receipt": make_json_serializable(receipt),
            }
        )
    except Exception as e:
        print(f"Burn error: {e}")
        return jsonify({"error": f"Error burning token: {e}"}), 500


@app.route("/remint-post", methods=["POST"])
def remint_post():
    """
    POST endpoint to remint a post with a new privacy setting.
    Reuses the existing IPFS image URL (no re-upload needed).
    Body: { userId, tokenId, isPrivate, ipfsUrl, hqMediaUrl }
    """
    data = request.get_json()
    if not data or "userId" not in data or "tokenId" not in data:
        return jsonify({"error": "Missing required fields"}), 400

    user_id = data["userId"]
    token_id = int(data["tokenId"])
    is_private = data.get("isPrivate", False)
    ipfs_url = data.get("ipfsUrl", "")
    hq_media_url = data.get("hqMediaUrl", "")

    if not ipfs_url:
        return jsonify({"error": "Missing ipfsUrl"}), 400

    try:
        # Create new ERC-721 metadata JSON and pin to Pinata
        token_name = "AuthenSnap Photo"
        metadata_ipfs_url = pin_metadata_to_pinata(ipfs_url, token_name)
        print(f"Remint metadata uploaded to IPFS: {metadata_ipfs_url}")

        # Mint new token on-chain
        user_id_hash = compute_user_id_hash(user_id)
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
        txn = mint_fn.build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 500000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)
        new_token_id = extract_virtual_mint_token_id(receipt, contract)

        # Encrypt URLs if private
        returned_ipfs_url = ipfs_url
        returned_hq_url = hq_media_url
        if is_private:
            returned_ipfs_url = encrypt_url(ipfs_url)
            if hq_media_url:
                returned_hq_url = encrypt_url(hq_media_url)

        return jsonify(
            {
                "success": True,
                "token_id": new_token_id,
                "ipfs_uri": returned_ipfs_url,
                "metadata_uri": metadata_ipfs_url,
                "hqMediaUrl": returned_hq_url,
                "isPrivate": is_private,
                "transaction_hash": tx_hash.hex(),
            }
        )
    except Exception as e:
        print(f"Remint error: {e}")
        return jsonify({"error": f"Error reminting post: {e}"}), 500


@app.route("/toggle-privacy", methods=["POST"])
def toggle_privacy():
    """
    POST endpoint to toggle privacy for all of a user's posts.
    Burns all existing tokens and remints them with the new privacy setting.
    Body: { userId, isPrivate }
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    data = request.get_json()
    if not data or "userId" not in data or "isPrivate" not in data:
        return jsonify({"error": "Missing userId or isPrivate"}), 400

    user_id = data["userId"]
    is_private = data["isPrivate"]

    try:
        # Read all user's posts from Firestore
        posts_ref = firestore_db.collection("posts")
        user_posts = posts_ref.where("userId", "==", user_id).stream()

        results = []
        for post_doc in user_posts:
            post_data = post_doc.to_dict()
            old_token_id = int(post_data.get("tokenId", 0))
            if old_token_id == 0:
                continue

            # Get the real (decrypted) ipfsUrl
            raw_ipfs_url = post_data.get("ipfsUrl", "")
            raw_hq_url = post_data.get("hqMediaUrl", "")

            # If currently private, decrypt URLs to get originals
            if post_data.get("isPrivate", False):
                try:
                    raw_ipfs_url = decrypt_url(raw_ipfs_url)
                except Exception:
                    pass  # Already unencrypted
                try:
                    if raw_hq_url:
                        raw_hq_url = decrypt_url(raw_hq_url)
                except Exception:
                    pass

            # 1. Burn old token on-chain
            user_id_hash = compute_user_id_hash(user_id)
            nonce = get_nonce()
            max_fee, max_priority_fee = get_gas_params()

            burn_fn = contract.functions.burnVirtual(user_id_hash, old_token_id)
            burn_txn = burn_fn.build_transaction(
                {
                    "chainId": w3.eth.chain_id,
                    "gas": 200000,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": max_priority_fee,
                    "nonce": nonce,
                }
            )
            send_transaction(burn_txn)

            # 2. Create new metadata JSON and pin to Pinata
            token_name = "AuthenSnap Photo"
            media_type = post_data.get("mediaType", "photo")
            if media_type == "video":
                token_name = "AuthenSnap Video"
            metadata_ipfs_url = pin_metadata_to_pinata(
                raw_ipfs_url, token_name, media_type=media_type
            )

            # 3. Mint new token
            nonce = get_nonce()
            max_fee, max_priority_fee = get_gas_params()

            mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
            mint_txn = mint_fn.build_transaction(
                {
                    "chainId": w3.eth.chain_id,
                    "gas": 500000,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": max_priority_fee,
                    "nonce": nonce,
                }
            )
            tx_hash, receipt = send_transaction(mint_txn)
            new_token_id = extract_virtual_mint_token_id(receipt, contract)

            # 4. Encrypt URLs if switching to private
            returned_ipfs_url = raw_ipfs_url
            returned_hq_url = raw_hq_url
            if is_private:
                returned_ipfs_url = encrypt_url(raw_ipfs_url)
                if raw_hq_url:
                    returned_hq_url = encrypt_url(raw_hq_url)

            results.append(
                {
                    "oldTokenId": old_token_id,
                    "newTokenId": new_token_id,
                    "ipfsUri": returned_ipfs_url,
                    "hqMediaUrl": returned_hq_url,
                    "metadataUri": metadata_ipfs_url,
                    "mediaType": media_type,
                }
            )

        return jsonify(
            {
                "success": True,
                "isPrivate": is_private,
                "results": results,
            }
        )
    except Exception as e:
        print(f"Toggle privacy error: {e}")
        return jsonify({"error": f"Error toggling privacy: {e}"}), 500


@app.route("/stitch", methods=["POST"])
def stitch_videos():
    """
    POST endpoint that accepts multipart form-data with:
      - videos[]: multiple video files
      - userId: Firebase user ID
      - mint: "true" or "false" (optional, default "true")
      - text_overlay: optional text to burn into the video (static, full duration)
      - text_font_size: optional font size for overlay (default 48)
      - text_color: optional color for overlay (default "white")

    Stitches videos with FFmpeg concat, optionally mints the result as an NFT.
    """
    uploaded_files = request.files.getlist("videos[]")
    user_id = request.form.get("userId")
    should_mint = request.form.get("mint", "true").lower() == "true"
    text_overlay = request.form.get("text_overlay", "").strip()
    text_font_size = request.form.get("text_font_size", "48")
    text_color = request.form.get("text_color", "white")

    if not uploaded_files or len(uploaded_files) < 2:
        return jsonify({"error": "At least 2 video files are required"}), 400

    if should_mint and not user_id:
        return jsonify({"error": "userId is required when minting"}), 400

    tmp_dir = tempfile.mkdtemp()
    try:
        # Save uploaded files to temp directory
        input_paths = []
        for i, f in enumerate(uploaded_files):
            path = os.path.join(tmp_dir, f"clip_{i}.mp4")
            f.save(path)
            input_paths.append(path)

        # Write FFmpeg concat list
        list_path = os.path.join(tmp_dir, "list.txt")
        with open(list_path, "w") as lf:
            for p in input_paths:
                lf.write(f"file '{p}'\n")

        output_path = os.path.join(tmp_dir, "output.mp4")

        # Run FFmpeg concat
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c",
                "copy",
                output_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            print(f"FFmpeg stderr: {result.stderr}")
            return jsonify({"error": f"FFmpeg failed: {result.stderr[-500:]}"}), 500

        # Apply text overlay if provided
        if text_overlay:
            overlay_input = output_path
            output_path = os.path.join(tmp_dir, "output_text.mp4")
            escaped_text = text_overlay.replace("'", "\\'").replace(":", "\\:")
            drawtext_filter = (
                f"drawtext=text='{escaped_text}'"
                f":fontsize={text_font_size}"
                f":fontcolor={text_color}"
                f":x=(w-text_w)/2:y=(h-text_h)/2"
                f":borderw=2:bordercolor=black@0.6"
            )
            text_result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", overlay_input,
                    "-vf", drawtext_filter,
                    "-codec:a", "copy",
                    output_path,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if text_result.returncode != 0:
                print(f"FFmpeg text overlay stderr: {text_result.stderr}")
                return jsonify({"error": f"Text overlay failed: {text_result.stderr[-500:]}"}), 500

        with open(output_path, "rb") as of:
            stitched_bytes = of.read()

        if not should_mint:
            # Return stitched video as file download
            from flask import send_file
            import io

            return send_file(
                io.BytesIO(stitched_bytes),
                mimetype="video/mp4",
                as_attachment=True,
                download_name="stitched.mp4",
            )

        # Compress stitched video: 720p for IPFS, 1080p for Firebase Storage
        try:
            lq_bytes = compress_video(
                stitched_bytes, target_height=720, max_fps=30, strip_metadata=True
            )
            hq_bytes = compress_video(stitched_bytes, target_height=1080, max_fps=60)
        except Exception as e:
            print(f"Stitch compression failed, using original: {e}")
            lq_bytes = stitched_bytes
            hq_bytes = stitched_bytes

        # Mint flow: pin 720p to IPFS → create metadata → mint on-chain
        video_ipfs_url = pin_file_bytes_to_pinata(lq_bytes, "stitched.mp4")
        print(f"Stitched video (720p) uploaded to IPFS: {video_ipfs_url}")

        metadata_ipfs_url = pin_metadata_to_pinata(
            video_ipfs_url, "AuthenSnap Video", media_type="video"
        )
        print(f"Metadata uploaded to IPFS: {metadata_ipfs_url}")

        # Mint on-chain
        user_id_hash = compute_user_id_hash(user_id)
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()

        mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
        txn = mint_fn.build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": 500000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )

        tx_hash, receipt = send_transaction(txn)
        token_id = extract_virtual_mint_token_id(receipt, contract)

        # Upload 1080p HQ version to Firebase Storage
        hq_media_url = upload_to_firebase_storage(
            hq_bytes, f"{token_id}.mp4", "video/mp4"
        )
        if hq_media_url:
            print(
                f"HQ stitched video (1080p) uploaded to Firebase Storage: {hq_media_url}"
            )

        response_data = {
            "transaction_hash": tx_hash.hex(),
            "token_id": token_id,
            "ipfs_uri": video_ipfs_url,
            "metadata_uri": metadata_ipfs_url,
            "mediaType": "video",
            "user_id": user_id,
            "receipt": make_json_serializable(receipt),
        }

        if hq_media_url:
            response_data["hqMediaUrl"] = hq_media_url

        return jsonify(response_data)

    except subprocess.TimeoutExpired:
        return jsonify({"error": "FFmpeg processing timed out"}), 500
    except Exception as e:
        print(f"Stitch error: {e}")
        return jsonify({"error": f"Error stitching videos: {e}"}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# Explore Feed Endpoints
# ============================================================


@app.route("/explore", methods=["GET"])
def explore_feed():
    """
    GET endpoint for ranked explore feed.
    Query params: limit (default 8), cursor (base64-encoded tokenId for pagination)
    Returns { posts: [...], nextCursor: string|null }
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    limit = request.args.get("limit", 8, type=int)
    cursor = request.args.get("cursor", None)

    try:
        posts_ref = firestore_db.collection("posts")
        q = (
            posts_ref.where("flagged", "==", False)
            .where("isPrivate", "==", False)
            .order_by("rankScore", direction="DESCENDING")
            .limit(limit + 1)
        )

        # Apply cursor-based pagination
        if cursor:
            try:
                cursor_token_id = base64.b64decode(cursor).decode("utf-8")
                cursor_doc = (
                    firestore_db.collection("posts").document(cursor_token_id).get()
                )
                if cursor_doc.exists:
                    q = q.start_after(cursor_doc)
            except Exception as e:
                print(f"Invalid cursor: {e}")

        docs = list(q.stream())

        # Determine if there's a next page
        has_next = len(docs) > limit
        if has_next:
            docs = docs[:limit]

        # Lazy refresh: recompute scores and batch-write if changed
        import time

        now = time.time()
        batch = firestore_db.batch()
        batch_count = 0
        posts_result = []

        for doc_snap in docs:
            data = doc_snap.to_dict()

            # Recompute score
            created_at = data.get("createdAt")
            if created_at:
                created_at_seconds = created_at.timestamp()
            else:
                created_at_seconds = now

            new_score = compute_rank_score(
                data.get("likesCount", 0),
                data.get("commentsCount", 0),
                created_at_seconds,
            )

            old_score = data.get("rankScore", 0)
            if abs(new_score - old_score) > 0.001:
                batch.update(doc_snap.reference, {"rankScore": new_score})
                batch_count += 1
                data["rankScore"] = new_score

            # Serialize post for response
            post_out = {}
            for k, v in data.items():
                if hasattr(v, "timestamp"):
                    post_out[k] = int(v.timestamp() * 1000)  # millis for JS
                else:
                    post_out[k] = v
            posts_result.append(post_out)

        if batch_count > 0:
            batch.commit()
            print(f"Lazy refresh: updated {batch_count} scores")

        # Build next cursor
        next_cursor = None
        if has_next and docs:
            last_token_id = str(docs[-1].to_dict().get("tokenId", docs[-1].id))
            next_cursor = base64.b64encode(last_token_id.encode("utf-8")).decode(
                "utf-8"
            )

        return jsonify({"posts": posts_result, "nextCursor": next_cursor})

    except Exception as e:
        print(f"Explore feed error: {e}")
        return jsonify({"error": f"Error fetching explore feed: {e}"}), 500


@app.route("/refresh-scores", methods=["POST"])
def refresh_scores():
    """
    POST endpoint to recompute rankScore for all posts from the last 7 days.
    Also backfills missing isPrivate and rankScore fields on older posts.
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    try:
        import time
        from datetime import datetime, timedelta

        now = time.time()
        cutoff = datetime.utcnow() - timedelta(days=7)

        posts_ref = firestore_db.collection("posts")
        updated_count = 0

        # Process recent posts (last 7 days)
        recent_query = posts_ref.where("createdAt", ">=", cutoff).stream()
        batch = firestore_db.batch()
        batch_size = 0

        for doc_snap in recent_query:
            data = doc_snap.to_dict()
            created_at = data.get("createdAt")
            if created_at:
                created_at_seconds = created_at.timestamp()
            else:
                created_at_seconds = now

            new_score = compute_rank_score(
                data.get("likesCount", 0),
                data.get("commentsCount", 0),
                created_at_seconds,
            )

            update_data = {"rankScore": new_score}

            # Backfill isPrivate if missing
            if "isPrivate" not in data:
                update_data["isPrivate"] = False

            batch.update(doc_snap.reference, update_data)
            batch_size += 1
            updated_count += 1

            # Firestore batch limit is 500
            if batch_size >= 500:
                batch.commit()
                batch = firestore_db.batch()
                batch_size = 0

        # Also backfill older posts that are missing rankScore or isPrivate
        all_docs = posts_ref.stream()
        for doc_snap in all_docs:
            data = doc_snap.to_dict()
            needs_update = False
            update_data = {}

            if "rankScore" not in data:
                created_at = data.get("createdAt")
                if created_at:
                    created_at_seconds = created_at.timestamp()
                else:
                    created_at_seconds = now
                update_data["rankScore"] = compute_rank_score(
                    data.get("likesCount", 0),
                    data.get("commentsCount", 0),
                    created_at_seconds,
                )
                needs_update = True

            if "isPrivate" not in data:
                update_data["isPrivate"] = False
                needs_update = True

            if needs_update:
                batch.update(doc_snap.reference, update_data)
                batch_size += 1
                updated_count += 1

                if batch_size >= 500:
                    batch.commit()
                    batch = firestore_db.batch()
                    batch_size = 0

        if batch_size > 0:
            batch.commit()

        print(f"Refresh scores: updated {updated_count} posts")
        return jsonify({"postsUpdated": updated_count})

    except Exception as e:

        print(f"Refresh scores error: {e}")
        return jsonify({"error": f"Error refreshing scores: {e}"}), 500


# ============================================================
# Stripe Token Purchase Endpoints
# ============================================================


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """
    POST endpoint to create a Stripe Checkout Session for token purchases.
    Requires Firebase ID token in Authorization header.
    Body: { tokens, successUrl, cancelUrl }
    """
    if not stripe.api_key:
        return jsonify({"error": "Stripe not configured"}), 500

    # 1. Verify Firebase ID token
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    data = request.get_json()
    if not data or "tokens" not in data:
        return jsonify({"error": "Missing tokens in request body"}), 400

    tokens = data["tokens"]
    success_url = data.get("successUrl")
    cancel_url = data.get("cancelUrl")

    if not success_url or not cancel_url:
        return jsonify({"error": "Missing successUrl or cancelUrl"}), 400

    # 2. Validate token amount (1,000–1,000,000, multiples of 1,000)
    if (
        not isinstance(tokens, int)
        or tokens < 1000
        or tokens > 1000000
        or tokens % 1000 != 0
    ):
        return jsonify(
            {"error": "Invalid token amount. Must be between 1,000 and 1,000,000, in multiples of 1,000."}
        ), 400

    # 3. Calculate price: tokens / 1000 * 100 cents
    amount_cents = (tokens // 1000) * 100

    # 4. Create Stripe Checkout Session
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"{tokens:,} Glas Tokens",
                        },
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            metadata={
                "userId": uid,
                "tokens": str(tokens),
            },
            success_url=success_url,
            cancel_url=cancel_url,
        )

        return jsonify({"url": session.url})
    except Exception as e:
        print(f"Stripe checkout error: {e}")
        return jsonify({"error": "Failed to create checkout session"}), 500


@app.route("/verify-payment", methods=["POST"])
def verify_payment():
    """
    POST endpoint to verify a Stripe payment and credit tokens.
    Body: { sessionId }
    """
    if not stripe.api_key:
        return jsonify({"error": "Stripe not configured"}), 500
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    data = request.get_json()
    if not data or "sessionId" not in data:
        return jsonify({"error": "Missing sessionId"}), 400

    session_id = data["sessionId"]
    if not isinstance(session_id, str) or not session_id:
        return jsonify({"error": "Invalid sessionId"}), 400

    try:
        # 1. Idempotency check
        completed_ref = firestore_db.collection("completedPurchases").document(session_id)
        completed_doc = completed_ref.get()

        if completed_doc.exists:
            doc_data = completed_doc.to_dict()
            return jsonify({
                "success": True,
                "tokens": doc_data.get("tokens", 0),
                "alreadyProcessed": True,
            })

        # 2. Retrieve Stripe session
        session = stripe.checkout.Session.retrieve(session_id)

        if session.payment_status != "paid":
            return jsonify({"error": "Payment not completed"}), 400

        # 3. Read metadata
        user_id = session.metadata.get("userId") if session.metadata else None
        tokens_str = session.metadata.get("tokens", "0") if session.metadata else "0"
        tokens = int(tokens_str)

        if not user_id or not tokens:
            return jsonify({"error": "Invalid session metadata"}), 400

        # 4. Atomic Firestore batch write
        batch = firestore_db.batch()

        balance_ref = firestore_db.collection("tokenBalances").document(user_id)
        batch.set(
            balance_ref,
            {"balance": FirestoreIncrement(tokens)},
            merge=True,
        )

        batch.set(completed_ref, {
            "userId": user_id,
            "tokens": tokens,
            "processedAt": SERVER_TIMESTAMP,
            "stripeSessionId": session_id,
        })

        batch.commit()

        # 5. On-chain sync (non-fatal)
        try:
            if token_contract:
                user_id_hash = compute_user_id_hash(user_id)
                amount_wei = w3.to_wei(tokens, "ether")
                nonce = get_nonce()
                max_fee, max_priority_fee = get_gas_params()

                txn = token_contract.functions.mintToVirtual(
                    user_id_hash, amount_wei
                ).build_transaction(
                    {
                        "chainId": w3.eth.chain_id,
                        "gas": 200000,
                        "maxFeePerGas": max_fee,
                        "maxPriorityFeePerGas": max_priority_fee,
                        "nonce": nonce,
                    }
                )
                send_transaction(txn)

                # Update last on-chain balance
                balance_ref.set(
                    {"lastOnChainBalance": FirestoreIncrement(tokens)},
                    merge=True,
                )
                print(f"On-chain sync succeeded for user {user_id}: {tokens} tokens")
            else:
                print("Token contract not configured, skipping on-chain sync")
        except Exception as sync_err:
            print(f"On-chain sync failed (non-fatal): {sync_err}")

        return jsonify({"success": True, "tokens": tokens})
    except Exception as e:
        print(f"Verify payment error: {e}")
        return jsonify({"error": "Failed to verify payment"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    app.run(host="0.0.0.0", port=port)
