from flask import Flask, request, jsonify
from flask_cors import CORS
from web3 import Web3
from web3.datastructures import AttributeDict
import os
import io
import json
import base64
import re
import hashlib
import subprocess
import secrets
import tempfile
import shutil
import uuid
import threading
import time
from datetime import datetime, timezone
import requests
import stripe
from dotenv import load_dotenv
from hexbytes import HexBytes
from openai import OpenAI
from PIL import Image as PILImage
import firebase_admin
from firebase_admin import credentials, storage, firestore as fb_firestore
from firebase_admin import auth as firebase_auth
from firebase_admin import messaging
from google.cloud.firestore_v1.transforms import Increment as FirestoreIncrement
from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from google.api_core.exceptions import AlreadyExists, NotFound
from cryptography.fernet import Fernet
from google.cloud import storage as gcs_storage

# Load environment variables from .env file
load_dotenv()

RPC_URL = os.getenv("SEPOLIA_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")
TOKEN_CONTRACT_ADDRESS = os.getenv("TOKEN_CONTRACT_ADDRESS")
GLAS_CONTRACT_ADDRESS = os.getenv("GLAS_CONTRACT_ADDRESS")

# Pinata API keys — legacy IPFS pinning, used only as a fallback when Filebase
# is not configured.
PINATA_API_KEY = os.getenv("PINATA_API_KEY")
PINATA_SECRET_API_KEY = os.getenv("PINATA_SECRET_API_KEY")

# Filebase — primary IPFS pinning (IPFS-backed S3, cheaper + higher request
# limits than Pinata). When all three are set, all new pins go to Filebase and
# Pinata is bypassed. Content addressing means the ipfs:// CIDs are identical
# regardless of provider, so tokenURIs stay standard and portable.
FILEBASE_KEY = os.getenv("FILEBASE_KEY")
FILEBASE_SECRET = os.getenv("FILEBASE_SECRET")
FILEBASE_BUCKET = os.getenv("FILEBASE_BUCKET")
FILEBASE_ENDPOINT = os.getenv("FILEBASE_ENDPOINT", "https://s3.filebase.com")
FILEBASE_ENABLED = bool(FILEBASE_KEY and FILEBASE_SECRET and FILEBASE_BUCKET)
# Gateway used to build https URLs inside ERC-721 metadata JSON (provider-neutral;
# override with your Filebase dedicated gateway for faster marketplace previews).
IPFS_GATEWAY = os.getenv("IPFS_GATEWAY", "https://ipfs.io/ipfs/")

# Stripe for token purchases
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# OpenAI for content moderation
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Shared secret for the Cloud Scheduler -> /cleanup-expired-stories call
CLEANUP_SECRET = os.getenv("CLEANUP_SECRET")

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
# Locally: set GCS_SERVICE_ACCOUNT_JSON in .env with the backend-gcs key.
# Cloud Run: leave unset — the attached service account is used automatically.
GCS_SERVICE_ACCOUNT_JSON = os.getenv("GCS_SERVICE_ACCOUNT_JSON")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "glas-hidef")
try:
    if GCS_SERVICE_ACCOUNT_JSON:
        from google.oauth2 import service_account as sa_auth
        gcs_credentials = sa_auth.Credentials.from_service_account_info(
            json.loads(GCS_SERVICE_ACCOUNT_JSON)
        )
        gcs_client = gcs_storage.Client(credentials=gcs_credentials, project=gcs_credentials.project_id)
    else:
        gcs_client = gcs_storage.Client()
    gcs_bucket = gcs_client.bucket(GCS_BUCKET_NAME)
    print(f"GCS client initialized with bucket: {GCS_BUCKET_NAME}")
except Exception as e:
    gcs_client = None
    gcs_bucket = None
    print(f"WARNING: GCS client init failed: {e}. HQ media upload disabled.")

# Cloud Tasks: async minting. When all TASKS_* are set, /mint/async enqueues a
# task that calls /mint/process (so the mint survives the app closing); if not
# set, /mint/async runs the mint inline so the app still works pre-infra.
TASKS_PROJECT = os.getenv("TASKS_PROJECT")
TASKS_LOCATION = os.getenv("TASKS_LOCATION")
TASKS_QUEUE = os.getenv("TASKS_QUEUE")
TASKS_TARGET_URL = os.getenv("TASKS_TARGET_URL")  # full URL to /mint/process
TASKS_INVOKER_SA_EMAIL = os.getenv("TASKS_INVOKER_SA_EMAIL")
MINT_WORKER_SECRET = os.getenv("MINT_WORKER_SECRET")

_tasks_client = None


def _get_tasks_client():
    global _tasks_client
    if _tasks_client is None:
        from google.cloud import tasks_v2
        _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


def tasks_configured() -> bool:
    return all(
        [
            TASKS_PROJECT,
            TASKS_LOCATION,
            TASKS_QUEUE,
            TASKS_TARGET_URL,
            TASKS_INVOKER_SA_EMAIL,
        ]
    )


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


if not (RPC_URL and PRIVATE_KEY and CONTRACT_ADDRESS):
    raise Exception("Missing one or more required environment variables.")

# Need at least one IPFS pinning provider configured.
if not (FILEBASE_ENABLED or (PINATA_API_KEY and PINATA_SECRET_API_KEY)):
    raise Exception(
        "No IPFS pinning provider configured: set FILEBASE_KEY/FILEBASE_SECRET/"
        "FILEBASE_BUCKET (preferred) or PINATA_API_KEY/PINATA_SECRET_API_KEY."
    )

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
DEFAULT_COLLECT_PRICE = 5  # default price (VW) to collect a post
PLATFORM_COLLECT_CUT = 0.5  # platform's cut of a collect, mirrors client collectSplit()
SYNC_THRESHOLD = 1000  # creator earnings delta that triggers an on-chain balance sync
STABLE_TOKEN_USD_RATE = 1000  # 1000 stable tokens = $1
WITHDRAWAL_FEE_PERCENT = 0.05  # 5% fee
DAILY_WITHDRAWAL_CAP_USD = 50.0  # $50/day per user
POOL_DRAIN_CAP_PERCENT = 0.10  # max 10% of pool per withdrawal
BURN_ADDRESS = "0x000000000000000000000000000000000000dEaD"


def collect_split(price):
    """Collect price -> (effective_price, creator_earns, platform_cut).
    Mirrors the client collectSplit(): p=max(0,price); platform=min(0.5,p); creator=p-platform."""
    p = max(0, price)
    platform_cut = min(PLATFORM_COLLECT_CUT, p)
    creator_earns = p - platform_cut
    return (p, creator_earns, platform_cut)

# ============================================================
# Explore Feed Ranking Constants
# ============================================================
COMMENT_WEIGHT = 3.0
GRAVITY = 1.5
INITIAL_RANK_SCORE = 0.354  # 1 / 2^1.5
# Related-feed candidate pool: how many top-ranked posts we scan when building
# a post's "related" list. Related-by-tag posts are surfaced from within this
# pool, so it also bounds how deep the related feed can page. Fine for the
# current dataset; revisit (composite index + rankScore cursor) as posts grow.
RELATED_POOL_SIZE = 300


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


PHONE_HASH_SALT = os.environ.get("PHONE_HASH_SALT", "")
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_phone_e164(raw: str):
    """Strip spaces/dashes/parens and return an E.164 string, or None if invalid."""
    if not raw:
        return None
    cleaned = re.sub(r"[\s\-()]", "", raw)
    return cleaned if _E164_RE.match(cleaned) else None


def hash_phone_number(e164: str, salt: str) -> str:
    """Salted SHA-256 of an E.164 phone number — the uniqueness index key."""
    return hashlib.sha256((salt + e164).encode()).hexdigest()


# Alphabet excludes visually ambiguous characters (0/O, 1/I/L).
REFERRAL_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_LENGTH = 7
REFERRAL_CODE_MAX_ATTEMPTS = 10


def generate_referral_code() -> str:
    """Generate a random 7-char referral code from the collision-safe alphabet."""
    return "".join(
        secrets.choice(REFERRAL_CODE_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH)
    )


def assign_referral_code(user_id: str) -> str:
    """
    Generate and atomically reserve a unique referral code for user_id,
    then persist it onto their own user doc (users/{uid}.referralCode) so
    the mobile client can read it back. Uses Firestore's create() (fails
    if the doc already exists) as the uniqueness gate instead of a full
    transaction, retrying on collision. Raises RuntimeError if no free
    code is found within the attempt budget.
    """
    for _ in range(REFERRAL_CODE_MAX_ATTEMPTS):
        code = generate_referral_code()
        code_ref = firestore_db.collection("referralCodes").document(code)
        try:
            code_ref.create({"userId": user_id})
            firestore_db.collection("users").document(user_id).set(
                {"referralCode": code}, merge=True
            )
            return code
        except AlreadyExists:
            continue
    raise RuntimeError(
        f"Could not generate a unique referral code after {REFERRAL_CODE_MAX_ATTEMPTS} attempts"
    )


def ensure_referral_code(user_id: str, user_data=None):
    """
    Return an existing referral code for user_id or assign one if missing.
    Also repairs the referralCodes reverse lookup when possible. Returns
    (code, created), where created is True only when a new code was assigned.
    """
    if user_data is None:
        user_doc = firestore_db.collection("users").document(user_id).get()
        user_data = user_doc.to_dict() if user_doc.exists else {}

    existing_code = user_data.get("referralCode")
    if existing_code:
        code_ref = firestore_db.collection("referralCodes").document(existing_code)
        try:
            code_ref.create({"userId": user_id})
        except AlreadyExists:
            code_doc = code_ref.get()
            code_owner = code_doc.to_dict().get("userId") if code_doc.exists else None
            if code_owner and code_owner != user_id:
                new_code = assign_referral_code(user_id)
                return new_code, True
        return existing_code, False

    return assign_referral_code(user_id), True


def _award_points(
    user_id: str,
    delta: int,
    reason: str,
    dedup_id: str,
    related_user_id=None,
    token_id=None,
) -> bool:
    """
    Idempotently credit `delta` points to user_id AND write an audit row to
    `pointTransactions`, together in one transaction. `dedup_id` is the
    deterministic ledger doc id — its existence is the once-only guard (daily
    caps encode the UTC date in the id; one-time credits omit it). Returns True
    if credited, False if this exact award already happened (dedup hit). Never
    raises for the dedup case — callers treat False as a silent no-op.

    The ledger is the reconciliation source of truth: every point ever granted
    has a row here with who/why/when/related-user/tokenId.
    """
    if delta <= 0 or not firestore_db:
        return False

    ledger_ref = firestore_db.collection("pointTransactions").document(dedup_id)
    user_ref = firestore_db.collection("users").document(user_id)
    now = datetime.now(timezone.utc)

    @fb_firestore.transactional
    def _run(txn):
        if ledger_ref.get(transaction=txn).exists:
            return False
        txn.set(
            ledger_ref,
            {
                "userId": user_id,
                "delta": delta,
                "reason": reason,
                "relatedUserId": related_user_id,
                "tokenId": token_id,
                "createdAt": now,
            },
        )
        txn.set(user_ref, {"points": FirestoreIncrement(delta)}, merge=True)
        return True

    return _run(firestore_db.transaction())


def post_point_value(media_type: str, duration_seconds) -> int:
    """Points a single post earns: +1 photo, +2 video >= 30s, 0 shorter video."""
    if media_type == "video":
        return (
            2
            if duration_seconds is not None
            and duration_seconds >= LONG_VIDEO_THRESHOLD_SECONDS
            else 0
        )
    return 1


def autoflag_should_flag(reports_count, likes_count, moderation_status):
    """User-report auto-flag rule. Only the server calls this.

    Flags when there are at least 5 reports AND reports strictly exceed
    10% of likes. Posts a moderator already approved are immune so a
    standing report count cannot instantly re-flag them.
    """
    if moderation_status == "approved":
        return False
    reports_count = reports_count or 0
    likes_count = likes_count or 0
    return reports_count >= 5 and reports_count > likes_count * 0.10


def redeem_referral_code(code: str, new_user_id: str):
    """
    Look up a referral code and link the new account to its owner.
    Returns the referrer's userId on success, or None if the code is
    missing/unknown/self-referential — signup must never fail because of
    an invalid code, so callers should treat None as a silent no-op.
    """
    if not code:
        return None

    code_doc = firestore_db.collection("referralCodes").document(code).get()
    if not code_doc.exists:
        return None

    referrer_id = code_doc.to_dict().get("userId")
    if not referrer_id or referrer_id == new_user_id:
        return None

    # Link the referee -> referrer (idempotent), then credit the referrer +1
    # once EVER for this pair via the ledger. The deterministic refsignup id is
    # the once-only guard, so concurrent/retried /create-account calls can't
    # double-credit (the old batch had no such guard).
    firestore_db.collection("users").document(new_user_id).set(
        {"referredBy": referrer_id}, merge=True
    )
    _award_points(
        referrer_id,
        1,
        "referral_signup",
        f"refsignup_{referrer_id}_{new_user_id}",
        related_user_id=new_user_id,
    )
    return referrer_id


LONG_VIDEO_THRESHOLD_SECONDS = 30


def credit_post_points(user_id: str, media_type: str, duration_seconds, token_id=None) -> int:
    """
    Credit the poster for a SAVED post, at most once per UTC day PER MEDIA TYPE
    (+1 photo, +2 video >= 30s). Idempotent + logged via the ledger. Returns the
    points credited this call (the value if it landed, 0 if the daily cap was
    already hit or the post doesn't qualify).
    """
    value = post_point_value(media_type, duration_seconds)
    if value <= 0:
        return 0
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    credited = _award_points(
        user_id,
        value,
        f"post_{media_type}",
        f"post_{user_id}_{media_type}_{today}",
        token_id=token_id,
    )
    return value if credited else 0


def credit_referral_bonus(poster_id: str, points_type: str, token_id=None) -> bool:
    """
    If poster_id was referred, credit that referrer +1 (picture) / +2 (video),
    at most once per UTC calendar day per (referrer, referee) pair. Idempotent +
    logged. Returns True if credited this call. points_type is 'picture'/'video'.
    """
    poster_doc = firestore_db.collection("users").document(poster_id).get()
    if not poster_doc.exists:
        return False

    referrer_id = poster_doc.to_dict().get("referredBy")
    if not referrer_id:
        return False

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    bonus = 2 if points_type == "video" else 1
    return _award_points(
        referrer_id,
        bonus,
        "referral_post_bonus",
        f"refbonus_{referrer_id}_{poster_id}_{today}",
        related_user_id=poster_id,
        token_id=token_id,
    )


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


def compress_image(base64_str, target_width):
    """
    Decode base64 image, resize so its WIDTH is at most target_width
    (matching Instagram's baseline, which caps images at 1080px wide),
    maintaining aspect ratio, re-encode as high-quality JPEG. Returns
    base64 string.

    Width, not height, is the governing axis: the app shows media full
    screen-width, so a portrait photo capped by height came out too narrow
    (~864px for a 4:5) and got upscaled on Retina screens. Capping width
    keeps portraits at 1080px wide (e.g. 1080x1350), matching Instagram.

    Encode settings matter as much as size: quality 92 with 4:4:4 chroma
    (subsampling=0) avoids the ringing/color-bleed on high-contrast edges
    (text, line art, logos) that quality-85 + default 4:2:0 produced. This is
    the last lossy pass, so we keep it clean rather than compounding artifacts.
    """
    img_data = base64.b64decode(base64_str)
    img = PILImage.open(io.BytesIO(img_data))

    # Only downscale, never upscale
    if img.width > target_width:
        ratio = target_width / img.width
        new_height = int(img.height * ratio)
        img = img.resize((target_width, new_height), PILImage.LANCZOS)

    # Convert to RGB if needed (e.g. RGBA PNGs)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, subsampling=0, optimize=True)
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
            "-movflags",
            "+faststart",
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
    Returns a relative media path (e.g. /media/hq_media/18.mp4).
    """
    if not gcs_bucket:
        print("GCS not configured, skipping HQ upload")
        return None

    try:
        blob_path = f"hq_media/{filename}"
        blob = gcs_bucket.blob(blob_path)
        blob.upload_from_string(file_bytes, content_type=content_type)
        # Return a relative path — the client prepends its own SERVER_URL
        media_path = f"/media/{blob_path}"
        print(f"GCS upload success: {blob_path} -> {media_path}")
        return media_path
    except Exception as e:
        print(f"GCS upload failed: {e}")
        return None


def get_video_duration_seconds(video_bytes):
    """
    Return the video's duration in seconds via ffprobe, or None if the
    duration can't be determined (corrupt/unsupported file, ffprobe
    missing, temp file creation failure, etc). Callers must treat None as
    "not long enough to qualify for the 30s+ bonus" rather than raising —
    this function must never raise past its own boundary.
    """
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(video_bytes)
        tmp.close()
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                tmp.name,
            ],
            capture_output=True,
            timeout=15,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print(f"ffprobe duration check failed: {result.stderr}")
            return None
        return float(result.stdout.strip())
    except Exception as e:
        print(f"ffprobe duration check failed: {e}")
        return None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def process_mint_media_item(base64_data, media_type):
    """
    Compress one media item (720p for IPFS, 1080p for GCS HQ), pin the 720p
    version to Pinata, and run AI moderation on it. Returns a dict consumed
    by /mint. Raises if the Pinata upload fails; compression failures fall
    back to the original bytes (same behavior as the old single-media path).
    """
    raw_bytes = base64.b64decode(base64_data)

    duration_seconds = None
    if media_type == "video":
        duration_seconds = get_video_duration_seconds(raw_bytes)

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
            lq_base64 = compress_image(base64_data, target_width=720)
            hq_base64 = compress_image(base64_data, target_width=1080)
            hq_bytes = base64.b64decode(hq_base64)
    except Exception as e:
        print(f"Compression failed, using original: {e}")
        lq_base64 = base64_data
        hq_bytes = raw_bytes

    ipfs_url = pin_file_to_ipfs(lq_base64, media_type=media_type)
    print(
        f"{'Video' if media_type == 'video' else 'Image'} (720p) uploaded to IPFS: {ipfs_url}"
    )

    if media_type == "video":
        analysis = analyze_video(raw_bytes)
    else:
        analysis = analyze_image(base64_data)

    return {
        "media_type": media_type,
        "raw_bytes": raw_bytes,
        "hq_bytes": hq_bytes,
        "ipfs_url": ipfs_url,
        "analysis": analysis,
        "duration_seconds": duration_seconds,
    }


def generate_video_thumbnail_bytes(video_bytes):
    """Extract a 480p JPEG poster frame from a video. Returns bytes or None."""
    try:
        thumb_tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        thumb_tmp.write(video_bytes)
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
            return thumb_result.stdout
    except Exception as e:
        print(f"Video thumbnail generation failed (non-blocking): {e}")
    return None


def _ipfs_to_gateway(ipfs_url):
    """ipfs://<cid>[/path] -> https gateway URL, using the configured gateway."""
    return ipfs_url.replace("ipfs://", IPFS_GATEWAY, 1)


_filebase_client = None


def _get_filebase_client():
    """Lazily build the S3 client for Filebase (path-style addressing)."""
    global _filebase_client
    if _filebase_client is None:
        import boto3
        from botocore.config import Config

        _filebase_client = boto3.client(
            "s3",
            endpoint_url=FILEBASE_ENDPOINT,
            aws_access_key_id=FILEBASE_KEY,
            aws_secret_access_key=FILEBASE_SECRET,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _filebase_client


def _filebase_put(key, data, content_type):
    """
    Upload bytes to the Filebase IPFS bucket and return the resulting IPFS CID.
    Filebase surfaces the CID as object metadata (x-amz-meta-cid); boto3 exposes
    it under head_object()['Metadata']['cid']. Retries with exponential backoff
    so a transient throttle/network blip doesn't fail a mint.
    """
    client = _get_filebase_client()
    last_err = None
    for attempt in range(4):
        try:
            client.put_object(
                Bucket=FILEBASE_BUCKET, Key=key, Body=data, ContentType=content_type
            )
            head = client.head_object(Bucket=FILEBASE_BUCKET, Key=key)
            cid = (head.get("Metadata") or {}).get("cid")
            if not cid:
                # Fall back to the raw response header if the parsed key is absent.
                cid = (
                    head.get("ResponseMetadata", {})
                    .get("HTTPHeaders", {})
                    .get("x-amz-meta-cid")
                )
            if not cid:
                raise Exception("Filebase did not return a CID for the upload")
            return cid
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
    raise Exception(f"Filebase upload failed after retries: {last_err}")


def _pin_bytes_to_pinata(file_data, filename, content_type="application/octet-stream"):
    """Legacy fallback: pin raw bytes to Pinata, returning the CID (no ipfs:// prefix)."""
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {
        "pinata_api_key": PINATA_API_KEY,
        "pinata_secret_api_key": PINATA_SECRET_API_KEY,
    }
    last_err = None
    for attempt in range(4):
        response = requests.post(
            url, files={"file": (filename, file_data, content_type)}, headers=headers
        )
        if response.status_code == 200:
            return response.json()["IpfsHash"]
        last_err = response.text
        # Only bother retrying transient throttling/5xx.
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        break
    raise Exception(f"Pinata upload error: {last_err}")


def pin_file_to_ipfs(base64_image_str, media_type="photo"):
    """
    Upload media to IPFS and return an ipfs:// URI. Uses Filebase when configured,
    otherwise falls back to Pinata. The CID (and thus the ipfs:// URI) is the same
    across providers because it is derived from the content itself.
    """
    file_data = base64.b64decode(base64_image_str)
    is_video = media_type == "video"
    filename = "nft_video.mp4" if is_video else "nft_image.png"
    content_type = "video/mp4" if is_video else "image/png"

    if FILEBASE_ENABLED:
        ext = "mp4" if is_video else "png"
        cid = _filebase_put(f"nft/{uuid.uuid4().hex}.{ext}", file_data, content_type)
        return f"ipfs://{cid}"

    return f"ipfs://{_pin_bytes_to_pinata(file_data, filename, content_type)}"


def pin_metadata_to_ipfs(
    image_ipfs_url,
    token_name,
    description="Photo minted on AuthenSnap",
    media_type="photo",
    media_items=None,
):
    """
    Build the ERC-721 metadata JSON and pin it to IPFS (Filebase when configured,
    else Pinata). Returns the ipfs:// URI of the metadata JSON (the tokenURI).

    OpenSea (and other marketplaces) expect tokenURI to point to a JSON file like:
    {
      "name": "AuthenSnap #1",
      "description": "...",
      "image": "ipfs://QmImageHash"
    }

    media_items: optional list of (ipfs_url, media_type) tuples for carousel
    posts. When given, the metadata also carries a "media" array listing every
    item; "image" (and "animation_url" for a leading video) still point at the
    first item so OpenSea previews keep working.
    """
    gateway_url = _ipfs_to_gateway(image_ipfs_url)
    metadata = {
        "name": token_name,
        "description": description,
        "image": gateway_url,
    }

    if media_type == "video":
        metadata["animation_url"] = gateway_url
        metadata["media_type"] = "video"

    if media_items:
        metadata["media"] = [
            {
                "uri": _ipfs_to_gateway(item_ipfs),
                "media_type": item_type,
            }
            for (item_ipfs, item_type) in media_items
        ]

    if FILEBASE_ENABLED:
        body = json.dumps(metadata).encode("utf-8")
        cid = _filebase_put(
            f"nft/metadata/{uuid.uuid4().hex}.json", body, "application/json"
        )
        return f"ipfs://{cid}"

    # Pinata fallback: pinJSONToIPFS.
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {
        "pinata_api_key": PINATA_API_KEY,
        "pinata_secret_api_key": PINATA_SECRET_API_KEY,
        "Content-Type": "application/json",
    }
    last_err = None
    for attempt in range(4):
        response = requests.post(url, json=metadata, headers=headers)
        if response.status_code == 200:
            return f"ipfs://{response.json()['IpfsHash']}"
        last_err = response.text
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        break
    raise Exception(f"Pinata metadata upload error: {last_err}")


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


# One server account signs every transaction (regular mints, burns, background
# story mints), so nonce management must be serialized. The lock must be held
# through mining (not just nonce-fetch -> sign -> send): get_nonce() CANCELS any
# pending unmined transactions it sees, so a second caller entering before the
# first tx mines would cancel the first tx.
tx_lock = threading.Lock()


def send_contract_transaction(fn_call, gas_limit):
    """
    Thread-safe get_nonce() + build_transaction + send_transaction for a bound
    contract function (e.g. contract.functions.mintToVirtual(hash, uri)).
    Returns (tx_hash, receipt). Raises on failure, like send_transaction.
    """
    with tx_lock:
        nonce = get_nonce()
        max_fee, max_priority_fee = get_gas_params()
        print(f"Building transaction with nonce: {nonce}")
        print(f"  max_fee: {w3.from_wei(max_fee, 'gwei'):.2f} gwei")
        print(f"  max_priority_fee: {w3.from_wei(max_priority_fee, 'gwei'):.2f} gwei")
        txn = fn_call.build_transaction(
            {
                "chainId": w3.eth.chain_id,
                "gas": gas_limit,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": nonce,
            }
        )
        return send_transaction(txn)


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


class MintError(Exception):
    """Raised by run_mint_pipeline on failure. Carries tx_hash when a mint
    transaction was sent but confirmation failed, so callers can surface it."""

    def __init__(self, message, tx_hash=None):
        super().__init__(message)
        self.tx_hash = tx_hash


def run_mint_pipeline(items_in, user_id, wallet_address, is_private, is_carousel):
    """
    Core mint: compress+pin+moderate each item, pin metadata, mint on-chain,
    upload HQ media. Returns the response_data dict. Raises MintError on failure.

    Does NOT create the Firestore post or credit points — callers own that:
    the sync /mint keeps the legacy client-driven savePost; the async worker
    (/mint/process) does it server-side via save_post_server + _credit_post.
    """
    # 1. Compress + pin to IPFS + moderate every item (order preserved)
    processed = []
    for index, item in enumerate(items_in):
        try:
            processed.append(process_mint_media_item(item["image"], item["mediaType"]))
        except Exception as e:
            print(f"Item {index} Pinata upload failed: {e}")
            raise MintError(f"Error uploading item {index} to Pinata: {e}")

    primary = processed[0]
    media_type = primary["media_type"]

    # 1c. Combine per-item AI analysis: flagged if ANY item flags.
    flagged_analyses = [p["analysis"] for p in processed if p["analysis"].get("flagged")]
    all_tags = []
    for p in processed:
        for tag in p["analysis"].get("tags", []):
            if tag not in all_tags:
                all_tags.append(tag)
    analysis = {
        "flagged": bool(flagged_analyses),
        "reason": flagged_analyses[0].get("reason", "") if flagged_analyses else "",
        "tags": all_tags[:5],
        "description": primary["analysis"].get("description", ""),
    }

    # 2. Create + pin ERC-721 metadata JSON
    try:
        token_name = "AuthenSnap Video" if media_type == "video" else "AuthenSnap Photo"
        metadata_ipfs_url = pin_metadata_to_ipfs(
            primary["ipfs_url"],
            token_name,
            media_type=media_type,
            media_items=(
                [(p["ipfs_url"], p["media_type"]) for p in processed]
                if is_carousel
                else None
            ),
        )
        print(f"Metadata uploaded to IPFS: {metadata_ipfs_url}")
    except Exception as e:
        print(e)
        raise MintError(f"Error uploading metadata to Pinata: {e}")

    # 3. Build + send the mint transaction
    try:
        if user_id:
            user_id_hash = compute_user_id_hash(user_id)
            mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
        else:
            recipient = Web3.to_checksum_address(wallet_address)
            mint_fn = contract.get_function_by_signature("mint(address,string)")
            mint_fn = mint_fn(recipient, metadata_ipfs_url)
    except Exception as e:
        print(e)
        raise MintError(f"Error building transaction: {e}")

    try:
        tx_hash, receipt = send_contract_transaction(mint_fn, 500000)
        if user_id:
            token_id = extract_virtual_mint_token_id(receipt, contract)
        else:
            token_id = extract_token_id_from_receipt(receipt, contract)
    except Exception as e:
        print(f"Error details: {e}")
        raise MintError(str(e), tx_hash=tx_hash.hex() if "tx_hash" in locals() else None)

    # Record the server-computed point value keyed by tokenId (legacy claim flow).
    if user_id and firestore_db:
        try:
            firestore_db.collection("mintResults").document(str(token_id)).set(
                {
                    "userId": user_id,
                    "mediaType": media_type,
                    "durationSeconds": primary["duration_seconds"],
                    "pointValue": post_point_value(media_type, primary["duration_seconds"]),
                    "createdAt": datetime.now(timezone.utc),
                }
            )
        except Exception as e:
            print(f"mintResults write failed (non-blocking): {e}")

    # Upload 1080p HQ versions + video thumbnails (now that we have tokenId).
    media_items_out = []
    for index, p in enumerate(processed):
        base_name = f"{token_id}_{index}" if is_carousel else f"{token_id}"
        if p["media_type"] == "video":
            ext, content_type = "mp4", "video/mp4"
        else:
            ext, content_type = "jpg", "image/jpeg"

        item_hq_url = upload_to_firebase_storage(
            p["hq_bytes"], f"{base_name}.{ext}", content_type
        )
        if item_hq_url:
            print(f"HQ media (1080p) uploaded to Firebase Storage: {item_hq_url}")

        item_thumb_url = None
        if p["media_type"] == "video":
            thumb_bytes = generate_video_thumbnail_bytes(p["raw_bytes"])
            if thumb_bytes:
                item_thumb_url = upload_to_firebase_storage(
                    thumb_bytes, f"{base_name}_thumb.jpg", "image/jpeg"
                )
                if item_thumb_url:
                    print(f"Video thumbnail uploaded: {item_thumb_url}")

        # For private posts, encrypt URLs before returning.
        item_ipfs = p["ipfs_url"]
        if is_private:
            item_ipfs = encrypt_url(item_ipfs)
            if item_hq_url:
                item_hq_url = encrypt_url(item_hq_url)

        media_items_out.append(
            {
                "ipfs_uri": item_ipfs,
                "hqMediaUrl": item_hq_url,
                "thumbnailUrl": item_thumb_url,
                "mediaType": p["media_type"],
            }
        )

    first = media_items_out[0]
    response_data = {
        "transaction_hash": tx_hash.hex(),
        "token_id": token_id,
        "ipfs_uri": first["ipfs_uri"],
        "metadata_uri": metadata_ipfs_url,
        "mediaType": media_type,
        "isPrivate": is_private,
        "flagged": analysis["flagged"],
        "flag_reason": analysis["reason"],
        "tags": analysis["tags"],
        "description": analysis["description"],
        "media_items": media_items_out,
        "mediaCount": len(media_items_out),
        "receipt": make_json_serializable(receipt),
        # Extra (harmless to old clients): the async worker uses these to credit.
        "duration_seconds": primary["duration_seconds"],
    }
    if first["hqMediaUrl"]:
        response_data["hqMediaUrl"] = first["hqMediaUrl"]
    if first["thumbnailUrl"]:
        response_data["thumbnailUrl"] = first["thumbnailUrl"]
    if user_id:
        response_data["user_id"] = user_id
    else:
        response_data["wallet_address"] = Web3.to_checksum_address(wallet_address)
    return response_data


def _user_email(user_id: str) -> str:
    try:
        doc = firestore_db.collection("users").document(user_id).get()
        if doc.exists and doc.to_dict().get("email"):
            return doc.to_dict()["email"]
    except Exception:
        pass
    try:
        return firebase_auth.get_user(user_id).email or ""
    except Exception:
        return ""


def _normalize_collect_price(value):
    """Whole VW, at least 1 (the flat 0.5 platform cut must always fit)."""
    try:
        return max(1, int(round(float(value))))
    except (TypeError, ValueError):
        return DEFAULT_COLLECT_PRICE


def save_post_server(
    token_id,
    response_data,
    user_id,
    is_private,
    caption,
    circle_slug,
    collect_enabled=True,
    collect_price=None,
):
    """Server-side equivalent of the client savePost — writes /posts/{token_id}
    with the same schema, so the async flow doesn't depend on the client staying
    alive to create the post."""
    if not firestore_db:
        return
    media_items = response_data.get("media_items", [])
    post = {
        "tokenId": token_id,
        "walletAddress": "",
        "ipfsUrl": response_data.get("ipfs_uri", ""),
        "userId": user_id,
        "userEmail": _user_email(user_id),
        "createdAt": SERVER_TIMESTAMP,
        "transactionHash": response_data.get("transaction_hash", ""),
        "likesCount": 0,
        "commentsCount": 0,
        "mediaType": response_data.get("mediaType", "photo"),
        "isPrivate": bool(is_private),
        "collectEnabled": bool(collect_enabled),
        "collectPrice": _normalize_collect_price(
            collect_price if collect_price is not None else DEFAULT_COLLECT_PRICE
        ),
        "rankScore": 0.354,
        "flagged": bool(response_data.get("flagged")),
    }
    if response_data.get("hqMediaUrl"):
        post["hqMediaUrl"] = response_data["hqMediaUrl"]
    if response_data.get("thumbnailUrl"):
        post["thumbnailUrl"] = response_data["thumbnailUrl"]
    if caption:
        post["caption"] = caption
    if circle_slug:
        post["circleSlug"] = circle_slug
    if len(media_items) > 1:
        post["media"] = [
            {
                "ipfsUrl": m.get("ipfs_uri", ""),
                "mediaType": m.get("mediaType", "photo"),
                **({"hqMediaUrl": m["hqMediaUrl"]} if m.get("hqMediaUrl") else {}),
                **({"thumbnailUrl": m["thumbnailUrl"]} if m.get("thumbnailUrl") else {}),
            }
            for m in media_items
        ]
        post["mediaCount"] = len(media_items)
    if response_data.get("flagged"):
        post["flagReason"] = response_data.get("flag_reason", "")
        post["flaggedAt"] = SERVER_TIMESTAMP
        post["flagSource"] = "ai"
        post["moderationStatus"] = "pending"
    if response_data.get("tags"):
        post["tags"] = response_data["tags"]
    if response_data.get("description"):
        post["description"] = response_data["description"]
    firestore_db.collection("posts").document(str(token_id)).set(post)


def _credit_post(user_id, response_data):
    """Credit the poster + their referrer for a saved post (daily-capped,
    idempotent, ledgered). Non-blocking."""
    token_id = response_data.get("token_id")
    media_type = response_data.get("mediaType", "photo")
    duration = response_data.get("duration_seconds")
    try:
        earned = credit_post_points(user_id, media_type, duration, token_id)
        if earned > 0:
            credit_referral_bonus(
                user_id, "video" if media_type == "video" else "picture", token_id
            )
    except Exception as e:
        print(f"points crediting failed (non-blocking): {e}")


def _stash_mint_upload(job_id, index, b64):
    gcs_bucket.blob(f"mint_uploads/{job_id}/{index}").upload_from_string(
        b64, content_type="text/plain"
    )


def _read_mint_upload(job_id, index):
    return gcs_bucket.blob(f"mint_uploads/{job_id}/{index}").download_as_text()


def _cleanup_mint_upload(job_id):
    try:
        for blob in gcs_client.list_blobs(
            GCS_BUCKET_NAME, prefix=f"mint_uploads/{job_id}/"
        ):
            blob.delete()
    except Exception as e:
        print(f"mint upload cleanup failed (non-blocking): {e}")


def enqueue_mint_job(job_id):
    from google.cloud import tasks_v2

    client = _get_tasks_client()
    parent = client.queue_path(TASKS_PROJECT, TASKS_LOCATION, TASKS_QUEUE)
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": TASKS_TARGET_URL,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"jobId": job_id}).encode(),
            "oidc_token": {
                "service_account_email": TASKS_INVOKER_SA_EMAIL,
                "audience": TASKS_TARGET_URL,
            },
        }
    }
    client.create_task(request={"parent": parent, "task": task})


def verify_mint_worker(req) -> bool:
    """Only Cloud Tasks (or a local caller with the shared secret) may hit
    /mint/process. Accept a matching X-Mint-Secret, or a valid OIDC token minted
    for the invoker service account."""
    if MINT_WORKER_SECRET and req.headers.get("X-Mint-Secret") == MINT_WORKER_SECRET:
        return True
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and TASKS_INVOKER_SA_EMAIL and TASKS_TARGET_URL:
        token = auth_header.split("Bearer ", 1)[1]
        try:
            from google.oauth2 import id_token as google_id_token
            from google.auth.transport import requests as google_requests

            claims = google_id_token.verify_oauth2_token(
                token, google_requests.Request(), audience=TASKS_TARGET_URL
            )
            if claims.get("email") == TASKS_INVOKER_SA_EMAIL and claims.get(
                "email_verified"
            ):
                return True
        except Exception as e:
            print(f"OIDC verify failed: {e}")
    return False


def _normalize_mint_items(data):
    """Turn either request shape into an ordered [{image, mediaType}] list, or
    raise ValueError with a client-facing message."""
    if "media" in data:
        media_param = data["media"]
        if not isinstance(media_param, list) or not (1 <= len(media_param) <= 10):
            raise ValueError("media must be a list of 1-10 items")
        items = []
        for m in media_param:
            if not isinstance(m, dict) or "image" not in m:
                raise ValueError("Each media item needs an image field")
            item_type = m.get("mediaType", "photo")
            if item_type not in ("photo", "video"):
                raise ValueError(f"Invalid mediaType: {item_type}")
            items.append({"image": m["image"], "mediaType": item_type})
        return items, True
    return [{"image": data["image"], "mediaType": data.get("mediaType", "photo")}], False


@app.route("/mint", methods=["POST"])
def mint_nft():
    """
    POST endpoint that mints ONE NFT for one post.

    Accepts EITHER the legacy single-media body:
      { image: <base64>, userId, mediaType, isPrivate }
    OR the carousel body (1-10 items, one token for all of them):
      { media: [{ image: <base64>, mediaType: 'photo'|'video' }, ...],
        userId, isPrivate }

    Also supports legacy format with walletAddress for backward compatibility.
    Every item is compressed (720p IPFS / 1080p GCS HQ), pinned, and moderated;
    the post is flagged if ANY item flags. Response keeps the legacy fields
    (populated from item 0) and adds media_items / mediaCount.
    """
    data = request.get_json()
    if not data or ("image" not in data and "media" not in data):
        return jsonify({"error": "Missing image or media in request body"}), 400

    user_id = data.get("userId")
    wallet_address = data.get("walletAddress")
    is_private = data.get("isPrivate", False)

    # Normalize both request shapes into an ordered list of {image, mediaType}.
    is_carousel = "media" in data
    if is_carousel:
        media_param = data["media"]
        if not isinstance(media_param, list) or not (1 <= len(media_param) <= 10):
            return jsonify({"error": "media must be a list of 1-10 items"}), 400
        items_in = []
        for m in media_param:
            if not isinstance(m, dict) or "image" not in m:
                return (
                    jsonify({"error": "Each media item needs an image field"}),
                    400,
                )
            item_type = m.get("mediaType", "photo")
            if item_type not in ("photo", "video"):
                return jsonify({"error": f"Invalid mediaType: {item_type}"}), 400
            items_in.append({"image": m["image"], "mediaType": item_type})
    else:
        items_in = [
            {"image": data["image"], "mediaType": data.get("mediaType", "photo")}
        ]

    if not user_id and not wallet_address:
        return (
            jsonify({"error": "Missing userId or walletAddress in request body"}),
            400,
        )

    # Only the virtual-wallet (userId) path has a real Firebase account to
    # verify against — the legacy walletAddress-only path predates Firebase
    # auth entirely and is intentionally left ungated.
    if user_id:
        uid = verify_firebase_token(request)
        if not uid or uid != user_id:
            return jsonify({"error": "Missing or invalid authorization token"}), 401

    try:
        response_data = run_mint_pipeline(
            items_in, user_id, wallet_address, is_private, is_carousel
        )
    except MintError as e:
        if e.tx_hash:
            return (
                jsonify(
                    {
                        "error": f"Transaction timeout or error: {e}",
                        "transaction_hash": e.tx_hash,
                        "check_status": f"https://sepolia.etherscan.io/tx/{e.tx_hash}",
                    }
                ),
                500,
            )
        return jsonify({"error": str(e)}), 500

    return jsonify(response_data)


@app.route("/mint/async", methods=["POST"])
def mint_async():
    """
    Async post: the client uploads the image + metadata and gets an instant 202
    so it can lock/close the phone; the slow on-chain mint runs server-side via
    Cloud Tasks (/mint/process). If Cloud Tasks isn't configured, falls back to
    running inline so the app still works before the infra is wired up.
    """
    data = request.get_json()
    if not data or ("image" not in data and "media" not in data):
        return jsonify({"error": "Missing image or media in request body"}), 400

    user_id = data.get("userId")
    is_private = data.get("isPrivate", False)
    caption = (data.get("caption") or "").strip() or None
    circle_slug = data.get("circleSlug") or None
    collect_enabled = bool(data.get("collectEnabled", True))
    collect_price = _normalize_collect_price(data.get("collectPrice", DEFAULT_COLLECT_PRICE))

    try:
        items_in, is_carousel = _normalize_mint_items(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not user_id:
        return jsonify({"error": "Missing userId"}), 400
    uid = verify_firebase_token(request)
    if not uid or uid != user_id:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    # Idempotency: the client sends a stable uploadId, which IS the mintJobs doc
    # id. Only the first request for an uploadId creates the job — a retry with
    # the same id returns the existing job instead of minting again, so a post
    # can NEVER be created twice (even across app restarts / lost acks).
    upload_id = data.get("uploadId") or uuid.uuid4().hex
    job_ref = firestore_db.collection("mintJobs").document(upload_id)

    def _existing_job_response():
        d = job_ref.get().to_dict() or {}
        out = {"jobId": upload_id, "status": d.get("status", "queued")}
        if d.get("tokenId") is not None:
            out["token_id"] = d["tokenId"]
        return out

    # Fallback: no queue configured (or no storage) → run inline so posting
    # still works; it just won't survive the app closing mid-mint.
    if not tasks_configured() or not gcs_bucket:
        try:
            job_ref.create(
                {
                    "status": "processing",
                    "userId": user_id,
                    "isPrivate": bool(is_private),
                    "caption": caption,
                    "circleSlug": circle_slug,
                    "collectEnabled": collect_enabled,
                    "collectPrice": collect_price,
                    "createdAt": SERVER_TIMESTAMP,
                }
            )
        except AlreadyExists:
            return jsonify(_existing_job_response()), 200
        try:
            response_data = run_mint_pipeline(
                items_in, user_id, None, is_private, is_carousel
            )
        except MintError as e:
            job_ref.update({"status": "failed", "error": str(e)})
            return jsonify({"error": str(e)}), 500
        token_id = response_data["token_id"]
        save_post_server(
            token_id, response_data, user_id, is_private, caption, circle_slug,
            collect_enabled, collect_price
        )
        _credit_post(user_id, response_data)
        job_ref.update(
            {"status": "done", "tokenId": token_id, "finishedAt": SERVER_TIMESTAMP}
        )
        return (
            jsonify({"jobId": upload_id, "status": "done", "token_id": token_id}),
            200,
        )

    # Async: dedup-create the job, stash the upload, enqueue the worker.
    try:
        job_ref.create(
            {
                "status": "queued",
                "userId": user_id,
                "isPrivate": bool(is_private),
                "caption": caption,
                "circleSlug": circle_slug,
                "collectEnabled": collect_enabled,
                "collectPrice": collect_price,
                "isCarousel": is_carousel,
                "itemCount": len(items_in),
                "itemTypes": [it["mediaType"] for it in items_in],
                "createdAt": SERVER_TIMESTAMP,
            }
        )
    except AlreadyExists:
        return jsonify(_existing_job_response()), 200
    try:
        for i, it in enumerate(items_in):
            _stash_mint_upload(upload_id, i, it["image"])
        enqueue_mint_job(upload_id)
    except Exception as e:
        print(f"[mint/async] enqueue failed: {e}")
        # Roll back so a retry can cleanly re-create the job.
        try:
            job_ref.delete()
        except Exception:
            pass
        _cleanup_mint_upload(upload_id)
        return jsonify({"error": f"Could not queue upload: {e}"}), 500
    return jsonify({"jobId": upload_id, "status": "queued"}), 202


@app.route("/mint/process", methods=["POST"])
def mint_process():
    """
    Cloud Tasks worker: runs the slow mint for a queued job, then creates the
    post + credits points server-side. Only Cloud Tasks (OIDC) or a caller with
    the shared secret may invoke it. Never retried (returns 200 even on failure)
    so a re-delivered task can't double-mint.
    """
    if not verify_mint_worker(request):
        return jsonify({"error": "Unauthorized"}), 403
    if not firestore_db or not gcs_bucket:
        return jsonify({"error": "Server not configured"}), 500

    data = request.get_json(silent=True) or {}
    job_id = data.get("jobId")
    if not job_id:
        return jsonify({"error": "Missing jobId"}), 400

    job_ref = firestore_db.collection("mintJobs").document(job_id)

    # Claim the job transactionally so a re-delivered task can't double-mint.
    @fb_firestore.transactional
    def _claim(txn):
        snap = job_ref.get(transaction=txn)
        if not snap.exists:
            return None
        d = snap.to_dict()
        if d.get("status") != "queued":
            return False
        txn.update(job_ref, {"status": "processing", "startedAt": SERVER_TIMESTAMP})
        return d

    job = _claim(firestore_db.transaction())
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    if job is False:
        return jsonify({"status": "skipped"}), 200

    user_id = job["userId"]
    is_private = job.get("isPrivate", False)
    caption = job.get("caption")
    circle_slug = job.get("circleSlug")
    collect_enabled = job.get("collectEnabled", True)
    collect_price = job.get("collectPrice", DEFAULT_COLLECT_PRICE)
    is_carousel = job.get("isCarousel", False)
    item_count = job.get("itemCount", 1)
    item_types = job.get("itemTypes", ["photo"])

    try:
        items_in = []
        for i in range(item_count):
            b64 = _read_mint_upload(job_id, i)
            mtype = item_types[i] if i < len(item_types) else "photo"
            items_in.append({"image": b64, "mediaType": mtype})
        response_data = run_mint_pipeline(
            items_in, user_id, None, is_private, is_carousel
        )
        token_id = response_data["token_id"]
        save_post_server(
            token_id, response_data, user_id, is_private, caption, circle_slug,
            collect_enabled, collect_price
        )
        _credit_post(user_id, response_data)
        job_ref.update(
            {"status": "done", "tokenId": token_id, "finishedAt": SERVER_TIMESTAMP}
        )
    except Exception as e:
        print(f"[mint/process] job {job_id} failed: {e}")
        job_ref.update(
            {"status": "failed", "error": str(e), "finishedAt": SERVER_TIMESTAMP}
        )
        _cleanup_mint_upload(job_id)
        return jsonify({"status": "failed", "error": str(e)}), 200

    _cleanup_mint_upload(job_id)
    return jsonify({"status": "done", "tokenId": token_id}), 200


@app.route("/media/<path:blob_path>", methods=["GET", "HEAD"])
def serve_media(blob_path):
    """
    Serves media from GCS. Tries signed URL redirect first (Cloud Run),
    falls back to proxying bytes (local dev where signing isn't available).
    """
    from flask import Response, redirect
    from datetime import timedelta

    if not gcs_bucket:
        return jsonify({"error": "Storage not configured"}), 500

    blob = gcs_bucket.blob(blob_path)

    if not blob.exists():
        return jsonify({"error": "File not found"}), 404

    # Generate a signed URL and redirect.
    # On Cloud Run (no key file), the SA impersonates itself to access
    # the IAM signBlob API. Locally with a key file, signs directly.
    # Ref: https://bluerider.software/presigning-gcs-urls-for-cloud-run-app-with-an-attached-service-account/
    try:
        import google.auth
        from google.auth import impersonated_credentials as imp_creds

        credentials = gcs_client._credentials
        if not hasattr(credentials, "sign_bytes"):
            # Compute/metadata credentials — self-impersonate to get signing
            target_principal = getattr(credentials, "service_account_email", None)
            if not target_principal:
                raise RuntimeError("Current GCS credentials cannot sign URLs locally")

            signing_credentials = imp_creds.Credentials(
                source_credentials=credentials,
                target_principal=target_principal,
                target_scopes=["https://www.googleapis.com/auth/devstorage.read_only"],
            )
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=1),
                method="GET",
                credentials=signing_credentials,
            )
        else:
            # Key file credentials — sign locally
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=1),
                method="GET",
            )
        return redirect(signed_url)
    except Exception as e:
        print(f"Signed URL failed, falling back to proxy: {e}")

    # Fallback: proxy the bytes (local dev with user credentials)
    blob.reload()
    file_size = blob.size
    content_type = blob.content_type or "application/octet-stream"
    range_header = request.headers.get("Range")

    if request.method == "HEAD":
        return Response(
            b"",
            status=200,
            content_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            },
        )

    if range_header:
        range_match = range_header.replace("bytes=", "").split("-", 1)
        start = int(range_match[0] or 0)
        end = int(range_match[1]) if len(range_match) > 1 and range_match[1] else file_size - 1
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            return Response(
                b"",
                status=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    "Accept-Ranges": "bytes",
                },
            )
        length = end - start + 1

        content = blob.download_as_bytes(start=start, end=end)

        return Response(
            content,
            status=206,
            content_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Cache-Control": "public, max-age=86400",
            },
        )
    else:
        content = blob.download_as_bytes()
        return Response(
            content,
            content_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
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
            response = {
                "ipfsUrl": post_data.get("ipfsUrl", ""),
                "hqMediaUrl": post_data.get("hqMediaUrl", ""),
            }
            if post_data.get("media"):
                response["media"] = post_data["media"]
            return jsonify(response)

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

        # Carousel posts: decrypt every item in the media array too
        unlocked_media = []
        for item in post_data.get("media") or []:
            entry = dict(item)
            for key in ("ipfsUrl", "hqMediaUrl"):
                value = entry.get(key)
                if value:
                    try:
                        entry[key] = decrypt_url(value)
                    except Exception:
                        pass  # old/unencrypted value — return as stored
            unlocked_media.append(entry)

        response = {
            "ipfsUrl": decrypted_ipfs,
            "hqMediaUrl": decrypted_hq,
        }
        if unlocked_media:
            response["media"] = unlocked_media
        return jsonify(response)

    except Exception as e:
        print(f"Unlock post error: {e}")
        return jsonify({"error": f"Error unlocking post: {e}"}), 500


def _get_admin_emails():
    """Comma-separated allow-list of moderator emails from MODERATION_ADMIN_EMAILS."""
    raw = os.getenv("MODERATION_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


@app.route("/admin/stats", methods=["GET"])
def admin_stats():
    """
    Aggregate platform stats for the admin dashboard. Admin-only (same
    MODERATION_ADMIN_EMAILS gate as /admin/unlock-post). Returns totals for
    posts, users, and engagement, plus a combined interactions total.
    Uses Firestore count() aggregations (O(1), not full reads).
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    # Verify the caller is an authenticated admin.
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing bearer token"}), 401
    try:
        decoded = firebase_auth.verify_id_token(auth_header.split("Bearer ")[1])
    except Exception:
        return jsonify({"error": "Invalid or expired token"}), 401
    admin_emails = _get_admin_emails()
    caller_email = (decoded.get("email") or "").lower()
    if not admin_emails:
        return jsonify({"error": "Server has no MODERATION_ADMIN_EMAILS configured"}), 500
    if caller_email not in admin_emails:
        return jsonify({"error": "Not authorized"}), 403

    def _count(query):
        try:
            return query.count().get()[0][0].value
        except Exception as e:
            print(f"[admin/stats] count failed: {e}")
            return None

    posts = _count(firestore_db.collection("posts"))
    users = _count(firestore_db.collection("users"))
    # collection_group("likes") counts post likes AND comment likes (both are
    # subcollections named "likes") — acceptable as "all engagement" for now.
    likes = _count(firestore_db.collection_group("likes"))
    comments = _count(firestore_db.collection_group("comments"))
    reactions = _count(firestore_db.collection_group("reactions"))
    collects = _count(firestore_db.collection("collections"))
    follows = _count(firestore_db.collection_group("following"))
    stories = _count(firestore_db.collection("stories"))
    story_views = _count(firestore_db.collection_group("views"))
    dm_threads = _count(firestore_db.collection("dmThreads"))
    messages = _count(firestore_db.collection_group("messages"))

    total_interactions = sum(
        v for v in (likes, comments, reactions, collects, follows) if isinstance(v, int)
    )

    # Daily active (proxy): distinct users who created a post or story in the
    # last 24h. There is no per-user lastActive field yet, so this counts
    # content creators only, not viewers/likers. Upgrade to true DAU by adding
    # a lastActive timestamp on users/{uid} and counting it here.
    daily_active = None
    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        active_ids = set()
        for coll in ("posts", "stories"):
            for d in firestore_db.collection(coll).where("createdAt", ">=", cutoff).stream():
                uid_v = d.to_dict().get("userId")
                if uid_v:
                    active_ids.add(uid_v)
        daily_active = len(active_ids)
    except Exception as e:
        print(f"[admin/stats] dailyActive failed: {e}")

    return jsonify({
        "posts": posts,
        "users": users,
        "interactions": total_interactions,
        "dailyActive": daily_active,
        "stories": stories,
        "storyViews": story_views,
        "dmThreads": dm_threads,
        "messages": messages,
        "breakdown": {
            "likes": likes,
            "comments": comments,
            "reactions": reactions,
            "collects": collects,
            "follows": follows,
        },
    })


@app.route("/admin/top-performers", methods=["GET"])
def admin_top_performers():
    """
    All-time top performers, ranked by interactions received. Admin-only.
    - topPosts: posts ranked by (likes + comments + reactions + collects).
    - topProfiles: users ranked by the sum of interactions across their posts.
    Scans the full posts collection (using the denormalized counter fields),
    so it's heavier than /admin/stats — call it on demand, not on a poll.
    ?limit=N (default 10).
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing bearer token"}), 401
    try:
        decoded = firebase_auth.verify_id_token(auth_header.split("Bearer ")[1])
    except Exception:
        return jsonify({"error": "Invalid or expired token"}), 401
    admin_emails = _get_admin_emails()
    caller_email = (decoded.get("email") or "").lower()
    if not admin_emails:
        return jsonify({"error": "Server has no MODERATION_ADMIN_EMAILS configured"}), 500
    if caller_email not in admin_emails:
        return jsonify({"error": "Not authorized"}), 403

    limit = request.args.get("limit", default=10, type=int)

    def _post_interactions(data):
        likes = data.get("likesCount", 0) or 0
        comments = data.get("commentsCount", 0) or 0
        collects = data.get("collectsCount", 0) or 0
        rc = data.get("reactionCounts")
        reactions = (
            sum(v for v in rc.values() if isinstance(v, (int, float)))
            if isinstance(rc, dict) else 0
        )
        return int(likes + comments + collects + reactions)

    top_posts = []
    profile_totals = {}  # userId -> {"interactions": n, "posts": n}
    try:
        for d in firestore_db.collection("posts").stream():
            data = d.to_dict()
            score = _post_interactions(data)
            uid_v = data.get("userId")
            top_posts.append({
                "tokenId": data.get("tokenId"),
                "userId": uid_v,
                "userEmail": data.get("userEmail", ""),
                "interactions": score,
                "likes": data.get("likesCount", 0) or 0,
                "comments": data.get("commentsCount", 0) or 0,
            })
            if uid_v:
                t = profile_totals.setdefault(uid_v, {"interactions": 0, "posts": 0})
                t["interactions"] += score
                t["posts"] += 1
    except Exception as e:
        return jsonify({"error": f"Failed to scan posts: {e}"}), 500

    top_posts.sort(key=lambda p: p["interactions"], reverse=True)
    top_posts = top_posts[:limit]

    top_profiles = [{"userId": uid_v, **vals} for uid_v, vals in profile_totals.items()]
    top_profiles.sort(key=lambda p: p["interactions"], reverse=True)
    top_profiles = top_profiles[:limit]

    # Resolve display names for the top results only (<= limit lookups each).
    name_cache = {}

    def _username(uid_v):
        if not uid_v:
            return None
        if uid_v in name_cache:
            return name_cache[uid_v]
        name = uid_v
        try:
            snap = firestore_db.collection("users").document(uid_v).get()
            if snap.exists:
                dd = snap.to_dict()
                name = dd.get("username") or dd.get("email") or uid_v
        except Exception:
            pass
        name_cache[uid_v] = name
        return name

    for p in top_posts:
        p["username"] = _username(p.get("userId"))
    for p in top_profiles:
        p["username"] = _username(p.get("userId"))

    # Top referral codes: rank referrers by how many users signed up via their
    # code. On a user doc, `referredBy` stores the REFERRER's uid; the user's
    # own code is `referralCode`. One pass over users groups + counts them.
    referred_count = {}       # referrer_uid -> number of users they referred
    referrer_info = {}        # uid -> {"username", "referralCode"}
    try:
        for d in firestore_db.collection("users").stream():
            data = d.to_dict()
            referrer_info[d.id] = {
                "username": data.get("username") or data.get("email"),
                "referralCode": data.get("referralCode"),
            }
            rb = data.get("referredBy")
            if rb:
                referred_count[rb] = referred_count.get(rb, 0) + 1
    except Exception as e:
        print(f"[admin/top-performers] referral scan failed: {e}")

    top_referrers = [
        {
            "userId": ref_uid,
            "username": referrer_info.get(ref_uid, {}).get("username") or ref_uid,
            "referralCode": referrer_info.get(ref_uid, {}).get("referralCode"),
            "referredUsers": count,
        }
        for ref_uid, count in referred_count.items()
    ]
    top_referrers.sort(key=lambda r: r["referredUsers"], reverse=True)
    top_referrers = top_referrers[:limit]

    return jsonify({
        "topPosts": top_posts,
        "topProfiles": top_profiles,
        "topReferrers": top_referrers,
    })


@app.route("/admin/unlock-post", methods=["POST"])
def admin_unlock_post():
    """
    POST endpoint for the moderation client to decrypt private post URLs.
    Auth: Firebase ID token (Authorization: Bearer <token>) whose email is in
    the MODERATION_ADMIN_EMAILS allow-list. Unlike /unlock-post this does not
    require ownership/following -- it is for moderators reviewing any post.
    Body: { tokenId }
    Returns decrypted ipfsUrl, hqMediaUrl, thumbnailUrl, mediaType (+ media[]).
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    # Verify the caller is an authenticated admin.
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing bearer token"}), 401
    try:
        decoded = firebase_auth.verify_id_token(auth_header.split("Bearer ")[1])
    except Exception:
        return jsonify({"error": "Invalid or expired token"}), 401

    admin_emails = _get_admin_emails()
    caller_email = (decoded.get("email") or "").lower()
    if not admin_emails:
        return jsonify({"error": "Server has no MODERATION_ADMIN_EMAILS configured"}), 500
    if caller_email not in admin_emails:
        return jsonify({"error": "Not authorized"}), 403

    data = request.get_json()
    if not data or "tokenId" not in data:
        return jsonify({"error": "Missing tokenId"}), 400
    token_id = str(data["tokenId"])

    def _maybe_decrypt(value):
        if not value:
            return ""
        try:
            return decrypt_url(value)
        except Exception:
            return value  # old/unencrypted value -- return as stored

    try:
        post_doc = firestore_db.collection("posts").document(token_id).get()
        if not post_doc.exists:
            return jsonify({"error": "Post not found"}), 404
        post_data = post_doc.to_dict()

        response = {
            "ipfsUrl": _maybe_decrypt(post_data.get("ipfsUrl", "")),
            "hqMediaUrl": _maybe_decrypt(post_data.get("hqMediaUrl", "")),
            "thumbnailUrl": _maybe_decrypt(post_data.get("thumbnailUrl", "")),
            "mediaType": post_data.get("mediaType", "photo"),
        }
        unlocked_media = []
        for item in post_data.get("media") or []:
            entry = dict(item)
            for key in ("ipfsUrl", "hqMediaUrl", "thumbnailUrl"):
                if entry.get(key):
                    entry[key] = _maybe_decrypt(entry[key])
            unlocked_media.append(entry)
        if unlocked_media:
            response["media"] = unlocked_media
        return jsonify(response)
    except Exception as e:
        print(f"Admin unlock post error: {e}")
        return jsonify({"error": f"Error unlocking post: {e}"}), 500


VALID_REPORT_REASONS = {"spam", "nudity", "violence", "harassment", "other"}


@app.route("/report-post", methods=["POST"])
def report_post():
    """
    POST endpoint for a mobile-app user to report a post.
    Auth: Firebase ID token (Authorization: Bearer <token>).
    Body: { tokenId, reason }, reason one of VALID_REPORT_REASONS.
    Records the reporter's report in /posts/{tokenId}/reports/{uid} (one doc
    per uid so a reporter can't inflate the count), recounts authoritatively
    from the subcollection, and applies autoflag_should_flag. Only the server
    ever writes `flagged`.
    """
    # --- auth: Bearer Firebase ID token ---
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "missing bearer token"}), 401
    id_token = auth_header.split(" ", 1)[1]
    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except Exception:
        return jsonify({"error": "invalid token"}), 401
    uid = decoded["uid"]
    email = decoded.get("email", "")

    # --- validate body ---
    data = request.get_json(silent=True) or {}
    token_id = data.get("tokenId")
    reason = data.get("reason")
    if token_id is None or reason not in VALID_REPORT_REASONS:
        return jsonify({"error": "invalid tokenId or reason"}), 400
    token_id = str(token_id)

    post_ref = firestore_db.collection("posts").document(token_id)
    snap = post_ref.get()
    if not snap.exists:
        return jsonify({"error": "post not found"}), 404
    post = snap.to_dict() or {}

    # --- record this reporter's report (one doc per uid) ---
    post_ref.collection("reports").document(uid).set({
        "reporterId": uid,
        "reporterEmail": email,
        "reason": reason,
        "createdAt": SERVER_TIMESTAMP,
    })

    # --- authoritative recount from the subcollection ---
    reason_counts = {}
    total = 0
    for rdoc in post_ref.collection("reports").stream():
        r = rdoc.to_dict() or {}
        rk = r.get("reason", "other")
        reason_counts[rk] = reason_counts.get(rk, 0) + 1
        total += 1

    likes_count = post.get("likesCount", 0) or 0
    moderation_status = post.get("moderationStatus")

    update = {"reportsCount": total, "reportReasonCounts": reason_counts}
    flagged = bool(post.get("flagged"))
    if not flagged and autoflag_should_flag(total, likes_count, moderation_status):
        flagged = True
        update.update({
            "flagged": True,
            "flagSource": "user_report",
            "moderationStatus": "pending",
            "flaggedAt": SERVER_TIMESTAMP,
            "flagReason": f"Auto-flagged: {total} user reports",
        })

    post_ref.set(update, merge=True)
    return jsonify({"reportsCount": total, "flagged": flagged}), 200


def mint_story_nft_background(story_id, user_id, media_b64, media_type):
    """
    Daemon-thread mint of a story NFT, run after /story-upload has responded.
    Accepted failure mode: the story still works as an ephemeral story --
    log and drop, no retry queue. The NFT (and its IPFS pin) intentionally
    outlives the 24h story expiry; the cleanup sweep only removes GCS +
    Firestore, and the token keeps showing in /tokens/virtual/<uid>.
    """
    try:
        if not firestore_db:
            print(f"Story mint {story_id}: Firestore not configured, skipping")
            return

        # 1. Account privacy flag (default False). The flag itself is owned by
        #    the settings/account-privacy section -- we only consume it here.
        is_private = False
        try:
            user_doc = firestore_db.collection("users").document(user_id).get()
            if user_doc.exists:
                is_private = bool((user_doc.to_dict() or {}).get("isPrivate", False))
        except Exception as e:
            print(f"Story mint {story_id}: isPrivate read failed, defaulting to public: {e}")

        # 2. Pin the already-compressed media, then the metadata JSON.
        #    pin_metadata_to_ipfs sets image = gateway URL, and for video
        #    also animation_url + media_type (server.py:876).
        image_ipfs_url = pin_file_to_ipfs(media_b64, media_type=media_type)
        metadata_ipfs_url = pin_metadata_to_ipfs(
            image_ipfs_url,
            "AuthenSnap Story",
            description="Story minted on AuthenSnap",
            media_type=media_type,
        )
        print(f"Story {story_id}: media {image_ipfs_url}, metadata {metadata_ipfs_url}")

        # 3. Mint on-chain -- serialized with every other tx via tx_lock.
        user_id_hash = compute_user_id_hash(user_id)
        mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
        tx_hash, receipt = send_contract_transaction(mint_fn, 500000)
        token_id = extract_virtual_mint_token_id(receipt, contract)
        print(f"Story {story_id}: minted tokenId {token_id}")

        # 4. Patch the story doc; skip silently if it no longer exists
        #    (expired/cleaned up, or the client never wrote it).
        stored_ipfs_url = encrypt_url(image_ipfs_url) if is_private else image_ipfs_url
        story_ref = firestore_db.collection("stories").document(story_id)
        if not story_ref.get().exists:
            print(f"Story {story_id}: doc already deleted, skipping patch")
            return
        try:
            story_ref.update(
                {
                    "tokenId": token_id,
                    "transactionHash": tx_hash.hex(),
                    "isPrivate": is_private,
                    "ipfsUrl": stored_ipfs_url,
                    "mintedAt": datetime.now(timezone.utc),
                }
            )
        except NotFound:
            print(f"Story {story_id}: doc deleted mid-patch, skipping")
    except Exception as e:
        print(f"Story mint failed for {story_id} (story remains ephemeral): {e}")


@app.route("/story-upload", methods=["POST"])
def story_upload():
    """
    POST endpoint to upload ephemeral story media to GCS.
    Body: { image: <base64>, userId, mediaType: "photo"|"video" }
    No IPFS pin, no NFT mint -- stories are not minted.
    Returns { storyId, mediaPath, mediaType, flagged, flagReason }.
    """
    data = request.get_json()
    if not data or "image" not in data or "userId" not in data:
        return jsonify({"error": "Missing image or userId in request body"}), 400

    base64_media = data["image"]
    user_id = data["userId"]
    media_type = data.get("mediaType", "photo")

    raw_bytes = base64.b64decode(base64_media)
    story_id = str(uuid.uuid4())

    try:
        if media_type == "video":
            hq_bytes = compress_video(raw_bytes, target_height=1080, max_fps=60)
            ext, content_type = "mp4", "video/mp4"
        else:
            hq_base64 = compress_image(base64_media, target_width=1080)
            hq_bytes = base64.b64decode(hq_base64)
            ext, content_type = "jpg", "image/jpeg"
    except Exception as e:
        print(f"Story compression failed, using original: {e}")
        hq_bytes = raw_bytes
        ext, content_type = ("mp4", "video/mp4") if media_type == "video" else ("jpg", "image/jpeg")

    if media_type == "video":
        analysis = analyze_video(raw_bytes)
    else:
        analysis = analyze_image(base64_media)

    if not gcs_bucket:
        return jsonify({"error": "Storage not configured"}), 500

    try:
        blob_path = f"stories/{user_id}/{story_id}.{ext}"
        blob = gcs_bucket.blob(blob_path)
        blob.upload_from_string(hq_bytes, content_type=content_type)
        media_path = f"/media/{blob_path}"
    except Exception as e:
        print(f"Story GCS upload failed: {e}")
        return jsonify({"error": f"Error uploading story: {e}"}), 500

    # Fire-and-forget NFT mint. The response below returns immediately; the
    # daemon thread pins + mints + patches stories/{storyId} on its own time.
    threading.Thread(
        target=mint_story_nft_background,
        args=(
            story_id,
            user_id,
            base64.b64encode(hq_bytes).decode("utf-8"),
            media_type,
        ),
        daemon=True,
    ).start()

    return jsonify({
        "storyId": story_id,
        "mediaPath": media_path,
        "mediaType": media_type,
        "flagged": analysis.get("flagged", False),
        "flagReason": analysis.get("reason", ""),
    })


@app.route("/avatar-upload", methods=["POST"])
def avatar_upload():
    """
    Upload a cropped profile image (avatar or cover) to GCS -- the same bucket
    as all other media. Replaces the old client Firebase Storage path.
    Body: { image: <base64>, shape: "circle"|"cover" }. Returns { mediaPath }.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if not gcs_bucket:
        return jsonify({"error": "Storage not configured"}), 500

    data = request.get_json(silent=True) or {}
    base64_media = data.get("image")
    shape = data.get("shape", "circle")
    if not base64_media:
        return jsonify({"error": "Missing image"}), 400
    if shape not in ("circle", "cover"):
        return jsonify({"error": "Invalid shape"}), 400

    try:
        hq_base64 = compress_image(base64_media, target_width=1080)
        hq_bytes = base64.b64decode(hq_base64)
    except Exception as e:
        print(f"Avatar compression failed, using original: {e}")
        hq_bytes = base64.b64decode(base64_media)

    try:
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        blob_path = f"avatars/{uid}/{shape}-{ts}.jpg"
        blob = gcs_bucket.blob(blob_path)
        blob.upload_from_string(hq_bytes, content_type="image/jpeg")
        media_path = f"/media/{blob_path}"
    except Exception as e:
        print(f"Avatar GCS upload failed: {e}")
        return jsonify({"error": f"Error uploading avatar: {e}"}), 500

    return jsonify({"mediaPath": media_path})


def story_blob_path(media_path: str) -> str:
    """GCS blob path for a story's media, from its stored mediaPath."""
    return media_path.replace("/media/", "", 1)


def is_story_owner(story_data: dict, uid: str) -> bool:
    """True iff uid is non-empty and owns the story."""
    return bool(uid) and story_data.get("userId") == uid


@app.route("/cleanup-expired-stories", methods=["POST"])
def cleanup_expired_stories():
    """
    Expired stories are now retained (owner-only, surfaced in the profile
    Stories tab) rather than hard-deleted. This endpoint is kept for the
    existing Cloud Scheduler call but is now a no-op. Permanent deletion
    is explicit via /delete-story, and account deletion still sweeps a
    user's stories.
    """
    if not CLEANUP_SECRET or request.headers.get("X-Cleanup-Secret") != CLEANUP_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    # Stories are now retained after expiry (owner-only, surfaced in the
    # profile Stories tab). Expiry no longer deletes anything; the client
    # tray query filters expired stories out (expiresAt > now). Permanent
    # deletion is explicit via /delete-story, and account deletion still
    # sweeps a user's stories.
    return jsonify({"deletedCount": 0, "retained": True})


@app.route("/delete-story", methods=["POST"])
def delete_story():
    """
    Permanently delete one of the caller's own stories: GCS blob + the
    views subcollection + the Firestore doc. Requires a Firebase ID token;
    only the story owner may delete it.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    data = request.get_json(silent=True) or {}
    story_id = data.get("storyId")
    if not story_id:
        return jsonify({"error": "storyId is required"}), 400

    story_ref = firestore_db.collection("stories").document(story_id)
    snapshot = story_ref.get()
    if not snapshot.exists:
        return jsonify({"error": "Story not found"}), 404

    story_data = snapshot.to_dict()
    if not is_story_owner(story_data, uid):
        return jsonify({"error": "Forbidden"}), 403

    try:
        blob_path = story_blob_path(story_data.get("mediaPath", ""))
        if gcs_bucket and blob_path:
            blob = gcs_bucket.blob(blob_path)
            if blob.exists():
                blob.delete()

        for view_doc in story_ref.collection("views").stream():
            view_doc.reference.delete()

        story_ref.delete()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"success": True})


_TYPE_TO_GROUP = {
    "like": "likes",
    "comment": "comments",
    "reply": "comments",
    "comment_like": "comments",
    "mention": "mentions",
    "follow": "follows",
    "collect": "collects",
    "dm": "dms",
}


def group_for_type(ntype: str):
    """Preference group a notification type belongs to, or None if unknown."""
    return _TYPE_TO_GROUP.get(ntype)


def pref_allows(prefs: dict, ntype: str) -> bool:
    """True if the recipient's prefs permit this type. Missing key = enabled."""
    group = group_for_type(ntype)
    if group is None:
        return False
    return prefs.get(group, True) is not False


def _push_copy(ntype: str, actor_name: str) -> str:
    """Notification body text for a type. Title is the app name (OS-provided)."""
    return {
        "like": f"{actor_name} liked your photo",
        "comment": f"{actor_name} commented on your photo",
        "reply": f"{actor_name} replied to you",
        "comment_like": f"{actor_name} liked your comment",
        "follow": f"{actor_name} started following you",
        "collect": f"{actor_name} collected your photo",
        "mention": f"{actor_name} mentioned you",
        "dm": f"{actor_name} sent you a message",
    }.get(ntype, f"{actor_name} sent you a notification")


@app.route("/push/notify", methods=["POST"])
def push_notify():
    """
    Send a push to the recipient's iOS devices for one notification event.
    Called fire-and-forget by the actor's client right after it writes the
    in-app notification doc. Enforces the recipient's per-group preference.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    data = request.get_json(silent=True) or {}
    recipient_id = data.get("recipientId")
    ntype = data.get("type")
    print(f"[push] notify recipient={recipient_id} type={ntype} actor={uid}")
    # The actor is ALWAYS the authenticated caller. Trusting a client-supplied
    # actorId would let any authenticated user spoof a push as someone else
    # (e.g. "<other user> liked your photo"). A mismatch is a spoof attempt.
    actor_id = data.get("actorId")
    if actor_id is not None and actor_id != uid:
        return jsonify({"error": "Forbidden: actor must be the authenticated user"}), 403
    actor_id = uid
    if not recipient_id or not ntype:
        return jsonify({"error": "recipientId and type are required"}), 400
    if recipient_id == actor_id:
        return jsonify({"sent": 0, "skipped": "self"})

    # Preference gate.
    recipient_snap = firestore_db.collection("users").document(recipient_id).get()
    recipient = recipient_snap.to_dict() if recipient_snap.exists else {}
    if not pref_allows(recipient.get("notificationPrefs", {}) or {}, ntype):
        print(f"[push] skipped: pref_off recipient={recipient_id} type={ntype}")
        return jsonify({"sent": 0, "skipped": "pref_off"})

    # Tokens.
    tokens_ref = firestore_db.collection("users").document(recipient_id).collection("pushTokens")
    token_docs = list(tokens_ref.stream())
    if not token_docs:
        print(f"[push] no tokens for recipient={recipient_id} — device never registered")
        return jsonify({"sent": 0})

    # Actor display name.
    actor_name = "Someone"
    if actor_id:
        actor_snap = firestore_db.collection("users").document(actor_id).get()
        if actor_snap.exists:
            actor_name = actor_snap.to_dict().get("username") or actor_name

    body = _push_copy(ntype, actor_name)
    payload = {k: str(v) for k, v in {
        "type": ntype,
        "actorId": actor_id,
        "postId": data.get("postId"),
        "threadId": data.get("threadId"),
        "commentId": data.get("commentId"),
    }.items() if v is not None}

    # App-icon badge = recipient's unread notification count. The client writes
    # every notification with readAt=null; markAllRead sets it to a Timestamp,
    # so this count is exactly the unread ones. (The app clears the icon badge
    # to 0 whenever it's opened.)
    badge_count = None
    try:
        unread = (
            firestore_db.collection("users")
            .document(recipient_id)
            .collection("notifications")
            .where("readAt", "==", None)
            .get()
        )
        badge_count = len(unread)
    except Exception as e:
        print(f"[push] unread-count failed recipient={recipient_id}: {e}")

    messages = [
        messaging.Message(
            token=d.id,
            notification=messaging.Notification(title="Glass", body=body),
            data=payload,
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=badge_count)
                ),
            ),
        )
        for d in token_docs
    ]

    resp = messaging.send_each(messages)

    pruned = 0
    for i, r in enumerate(resp.responses):
        if not r.success:
            exc = r.exception
            # Log the REAL reason a send failed. A ThirdPartyAuthError /
            # APNS auth failure here almost always means the APNs Auth Key
            # (.p8) is missing or misconfigured in the Firebase console for
            # this project (Project Settings -> Cloud Messaging -> Apple).
            print(
                f"[push] send failed recipient={recipient_id} "
                f"exc={type(exc).__name__}: {exc}"
            )
            # Unregistered / invalid token -> remove it.
            if isinstance(exc, (messaging.UnregisteredError, ValueError)) or "not a valid FCM" in str(exc):
                token_docs[i].reference.delete()
                pruned += 1

    print(
        f"[push] done recipient={recipient_id} type={ntype} "
        f"tokens={len(messages)} sent={resp.success_count} pruned={pruned}"
    )
    return jsonify({"sent": resp.success_count, "pruned": pruned})


@app.route("/studio-items", methods=["GET"])
def list_studio_items():
    """
    List the current user's private studio gallery items.
    Requires Firebase ID token in Authorization header.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    try:
        docs = (
            firestore_db.collection("users")
            .document(uid)
            .collection("studioItems")
            .order_by("createdAt", direction="DESCENDING")
            .stream()
        )
        items = []
        for item_doc in docs:
            data = item_doc.to_dict() or {}
            items.append(
                {
                    "id": item_doc.id,
                    "type": data.get("type", "video"),
                    "filename": data.get("filename", ""),
                    "mediaPath": data.get("mediaPath", ""),
                    "thumbnailPath": data.get("thumbnailPath", ""),
                    "durationMs": data.get("durationMs", 0),
                    "createdAt": data.get("createdAtMs", 0),
                    "source": data.get("source", "recorded"),
                    "parentIds": data.get("parentIds"),
                    "fileSizeBytes": data.get("fileSizeBytes", 0),
                }
            )
        return jsonify({"items": items})
    except Exception as e:
        print(f"Studio list failed: {e}")
        return jsonify({"error": f"Error listing studio items: {e}"}), 500


@app.route("/studio-upload", methods=["POST"])
def studio_upload():
    """
    Upload a private studio gallery item to GCS and save metadata to Firestore.
    Requires Firebase ID token in Authorization header.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    if not gcs_bucket:
        return jsonify({"error": "Storage not configured"}), 500

    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    is_multipart = bool(request.files)
    data = request.form if is_multipart else request.get_json()
    media_file = request.files.get("media") if is_multipart else None

    if not data:
        return jsonify({"error": "Missing request body"}), 400
    if is_multipart and not media_file:
        return jsonify({"error": "Missing media file"}), 400
    if not is_multipart and "media" not in data:
        return jsonify({"error": "Missing media in request body"}), 400

    media_type = data.get("mediaType", "video")
    if media_type not in ["photo", "video"]:
        return jsonify({"error": "mediaType must be photo or video"}), 400

    item_id = str(uuid.uuid4())
    ext, content_type = ("mp4", "video/mp4") if media_type == "video" else ("jpg", "image/jpeg")
    filename = f"{item_id}.{ext}"
    blob_path = f"studio/{uid}/{filename}"

    try:
        media_bytes = media_file.read() if media_file else base64.b64decode(data["media"])
        media_blob = gcs_bucket.blob(blob_path)
        media_blob.upload_from_string(media_bytes, content_type=content_type)
        media_path = f"/media/{blob_path}"

        thumbnail_path = ""
        thumbnail_file = request.files.get("thumbnail") if is_multipart else None
        thumbnail_base64 = data.get("thumbnail") if not is_multipart else None
        if thumbnail_file or thumbnail_base64:
            thumbnail_bytes = thumbnail_file.read() if thumbnail_file else base64.b64decode(thumbnail_base64)
            thumbnail_blob_path = f"studio/{uid}/thumbnails/{item_id}.jpg"
            thumbnail_blob = gcs_bucket.blob(thumbnail_blob_path)
            thumbnail_blob.upload_from_string(thumbnail_bytes, content_type="image/jpeg")
            thumbnail_path = f"/media/{thumbnail_blob_path}"
        elif media_type == "photo":
            thumbnail_path = media_path

        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        parent_ids = data.get("parentIds")
        if isinstance(parent_ids, str):
            try:
                parent_ids = json.loads(parent_ids)
            except Exception:
                parent_ids = None
        item = {
            "type": media_type,
            "filename": filename,
            "mediaPath": media_path,
            "thumbnailPath": thumbnail_path,
            "durationMs": int(data.get("durationMs") or 0),
            "createdAt": now,
            "createdAtMs": now_ms,
            "source": data.get("source", "recorded"),
            "parentIds": parent_ids,
            "fileSizeBytes": int(data.get("fileSizeBytes") or len(media_bytes)),
        }
        (
            firestore_db.collection("users")
            .document(uid)
            .collection("studioItems")
            .document(item_id)
            .set(item)
        )

        return jsonify(
            {
                "id": item_id,
                "type": item["type"],
                "filename": item["filename"],
                "mediaPath": item["mediaPath"],
                "thumbnailPath": item["thumbnailPath"],
                "durationMs": item["durationMs"],
                "createdAt": item["createdAtMs"],
                "source": item["source"],
                "parentIds": item["parentIds"],
                "fileSizeBytes": item["fileSizeBytes"],
            }
        )
    except Exception as e:
        print(f"Studio upload failed: {e}")
        return jsonify({"error": f"Error uploading studio item: {e}"}), 500


@app.route("/studio-items/<item_id>", methods=["DELETE"])
def delete_studio_item(item_id):
    """
    Delete a private studio gallery item and its GCS blobs.
    Requires Firebase ID token in Authorization header.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    item_ref = (
        firestore_db.collection("users")
        .document(uid)
        .collection("studioItems")
        .document(item_id)
    )
    item_doc = item_ref.get()
    if not item_doc.exists:
        return jsonify({"success": True})

    data = item_doc.to_dict() or {}
    paths = {
        path
        for path in [data.get("mediaPath"), data.get("thumbnailPath")]
        if path
    }
    for media_path in paths:
        blob_path = media_path.replace("/media/", "", 1)
        try:
            if gcs_bucket and blob_path:
                blob = gcs_bucket.blob(blob_path)
                if blob.exists():
                    blob.delete()
        except Exception as e:
            print(f"Studio blob delete failed for {blob_path}: {e}")

    item_ref.delete()
    return jsonify({"success": True})


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

        export_fn = contract.functions.exportToken(user_id_hash, token_id, recipient)
        tx_hash, receipt = send_contract_transaction(export_fn, 200000)

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
    to_user_email = data.get("toUserEmail")

    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if uid != from_user_id:
        return jsonify({"error": "Forbidden"}), 403

    try:
        from_hash = compute_user_id_hash(from_user_id)
        to_hash = compute_user_id_hash(to_user_id)

        transfer_fn = contract.functions.transferVirtual(from_hash, to_hash, token_id)
        tx_hash, receipt = send_contract_transaction(transfer_fn, 200000)

        if firestore_db:
            firestore_db.collection("posts").document(str(token_id)).set(
                {"userId": to_user_id, "userEmail": to_user_email or ""}, merge=True
            )

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

        import_fn = contract.functions.importToken(user_id_hash, token_id)
        tx_hash, receipt = send_contract_transaction(import_fn, 200000)

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
    Body: { userId, referralCode? }
    Mints 10,000 tokens to the user's virtual wallet, assigns the new
    account its own referral code, and (if a valid referralCode was
    passed) links referredBy and credits the referrer +1 point.
    """
    data = request.get_json()
    if not data or "userId" not in data:
        return jsonify({"error": "Missing userId"}), 400

    user_id = data["userId"]
    submitted_referral_code = data.get("referralCode")

    uid = verify_firebase_token(request)
    if not uid or uid != user_id:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    response_data = {
        "success": True,
        "userId": user_id,
    }

    # Referral setup is additive and runs before token minting so a chain/RPC
    # failure cannot leave an otherwise-created Firebase user without a code.
    # It is idempotent: /create-account can be retried by the client and must
    # not generate a second referral code or double-credit a referrer.
    if firestore_db:
        try:
            user_doc = firestore_db.collection("users").document(user_id).get()
            user_data = user_doc.to_dict() if user_doc.exists else {}

            own_code, _ = ensure_referral_code(user_id, user_data)
            response_data["referralCode"] = own_code

            if "referredBy" in user_data:
                response_data["referralLinked"] = True
            elif user_data.get("phoneVerified") is True:
                referrer_id = redeem_referral_code(submitted_referral_code, user_id)
                response_data["referralLinked"] = referrer_id is not None
            else:
                # No verified phone yet -> no referral credit. This is the
                # anti-fraud gate: leaderboard points require a verified phone.
                response_data["referralLinked"] = False
        except Exception as e:
            print(f"Referral setup failed (non-blocking): {e}")

    if not token_contract:
        return jsonify({"error": "Token contract not configured"}), 500

    user_id_hash = compute_user_id_hash(user_id)
    amount = w3.to_wei(10000, "ether")  # 10,000 tokens

    try:
        mint_fn = token_contract.functions.mintToVirtual(user_id_hash, amount)
        tx_hash, receipt = send_contract_transaction(mint_fn, 200000)

        response_data["tokensGranted"] = 10000
        response_data["transaction_hash"] = tx_hash.hex()

        if firestore_db:
            balance_ref = firestore_db.collection("tokenBalances").document(user_id)
            if not balance_ref.get().exists:
                balance_ref.set({"balance": 10000, "lastOnChainBalance": 10000})

        return jsonify(response_data)
    except Exception as e:
        print(f"Create account error: {e}")
        return jsonify({"error": f"Error creating account: {e}"}), 500


@app.route("/phone/verify", methods=["POST"])
def phone_verify():
    """
    Record an SMS-verified phone number for the authenticated account and
    enforce one-account-per-phone.
    Authorization: Bearer <account id token> (the email/password account).
    Body: { phoneIdToken } — an ID token from a native phone sign-in whose
    verified phone_number we trust.
    """
    account_uid = verify_firebase_token(request)
    if not account_uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    data = request.get_json() or {}
    phone_id_token = data.get("phoneIdToken")
    if not phone_id_token:
        return jsonify({"error": "Missing phoneIdToken"}), 400

    try:
        phone_decoded = firebase_auth.verify_id_token(phone_id_token)
    except Exception:
        return jsonify({"error": "Invalid phone token"}), 401

    provider = (phone_decoded.get("firebase") or {}).get("sign_in_provider")
    raw_phone = phone_decoded.get("phone_number")
    phone_uid = phone_decoded.get("uid")
    if provider != "phone" or not raw_phone:
        return jsonify({"error": "Phone token is not a verified phone sign-in"}), 400

    e164 = normalize_phone_e164(raw_phone)
    if not e164:
        return jsonify({"error": "Unparseable phone number"}), 400

    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    phone_hash = hash_phone_number(e164, PHONE_HASH_SALT)
    phone_ref = firestore_db.collection("phoneNumbers").document(phone_hash)
    user_ref = firestore_db.collection("users").document(account_uid)

    transaction = firestore_db.transaction()

    @fb_firestore.transactional
    def claim(txn):
        snap = phone_ref.get(transaction=txn)
        if snap.exists:
            owner = snap.to_dict().get("uid")
            if owner and owner != account_uid:
                return False  # phone already registered to another account
        txn.set(phone_ref, {"uid": account_uid, "createdAt": SERVER_TIMESTAMP})
        txn.set(
            user_ref,
            {"phoneVerified": True, "phoneVerifiedAt": SERVER_TIMESTAMP},
            merge=True,
        )
        return True

    claimed = claim(transaction)
    if not claimed:
        return (
            jsonify(
                {"error": "This phone number is already in use by another account"}
            ),
            409,
        )

    # Delete the throwaway phone-only Firebase user so phone accounts don't
    # accumulate. Never delete the real account.
    if phone_uid and phone_uid != account_uid:
        try:
            firebase_auth.delete_user(phone_uid)
        except Exception as e:
            print(f"Could not delete throwaway phone user {phone_uid}: {e}")

    return jsonify({"ok": True})


@app.route("/posts/<token_id>/claim-points", methods=["POST"])
def claim_post_points(token_id):
    """
    Credit points for a post that has ACTUALLY been saved. The client calls this
    after it writes the post to Firestore. We verify the post exists and is owned
    by the caller, then credit (once/day per media type, idempotent, logged):
      - the poster: the server-computed value recorded in mintResults/<tokenId>
      - their referrer (if any): the referral-post bonus
    Safe to call repeatedly — daily/idempotent dedup means no double credit, so a
    lost-connection retry can't farm points.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    token_id = str(token_id)
    post_doc = firestore_db.collection("posts").document(token_id).get()
    if not post_doc.exists:
        return jsonify({"error": "Post not found"}), 404
    if post_doc.to_dict().get("userId") != uid:
        return jsonify({"error": "Not your post"}), 403

    mint_doc = firestore_db.collection("mintResults").document(token_id).get()
    if not mint_doc.exists:
        # Nothing to credit (post predates this system, or mint record missing).
        return jsonify({"credited": {"self": False, "referral": False}}), 200
    mint = mint_doc.to_dict()
    if mint.get("userId") != uid:
        return jsonify({"error": "Mint owner mismatch"}), 403

    media_type = mint.get("mediaType", "photo")
    duration_seconds = mint.get("durationSeconds")

    credited_self = credit_post_points(uid, media_type, duration_seconds, token_id) > 0
    credited_referral = False
    if post_point_value(media_type, duration_seconds) > 0:
        credited_referral = credit_referral_bonus(
            uid, "video" if media_type == "video" else "picture", token_id
        )

    return (
        jsonify({"credited": {"self": credited_self, "referral": credited_referral}}),
        200,
    )


@app.route("/posts/<token_id>", methods=["DELETE"])
def delete_post(token_id):
    """
    Delete a post the caller owns. Owner is verified server-side (defense in
    depth beyond Firestore rules). Recursively removes the post doc and its
    subcollections (likes, comments). The on-chain NFT and IPFS media are
    permanent and are intentionally left as-is; earned points stay too (the
    pointTransactions ledger keeps the audit trail).
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    token_id = str(token_id)
    post_ref = firestore_db.collection("posts").document(token_id)
    snap = post_ref.get()
    if not snap.exists:
        return jsonify({"error": "Post not found"}), 404
    if snap.to_dict().get("userId") != uid:
        return jsonify({"error": "Not your post"}), 403

    firestore_db.recursive_delete(post_ref)
    return jsonify({"success": True}), 200


@app.route("/referral/leaderboard", methods=["GET"])
def referral_leaderboard():
    """
    GET endpoint for the all-time referral points leaderboard.
    Query params: limit (default 50, capped at 100)
    Returns { leaderboard: [{ userId, email, displayName, points, rank }] }
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    limit = min(request.args.get("limit", 50, type=int), 100)

    try:
        query = (
            firestore_db.collection("users")
            .order_by("points", direction="DESCENDING")
            .limit(limit)
        )
        leaderboard = []
        for rank, doc in enumerate(query.stream(), start=1):
            data = doc.to_dict()
            email = data.get("email") or ""
            if not email:
                post_query = (
                    firestore_db.collection("posts")
                    .where("userId", "==", doc.id)
                    .limit(1)
                )
                for post_doc in post_query.stream():
                    email = post_doc.to_dict().get("userEmail") or ""
                    break
            display_name = email or data.get("displayName") or f"user_{doc.id[:6]}"
            leaderboard.append(
                {
                    "userId": doc.id,
                    "email": email,
                    "displayName": display_name,
                    "points": data.get("points", 0),
                    "rank": rank,
                }
            )
        return jsonify({"leaderboard": leaderboard})
    except Exception as e:
        print(f"Leaderboard error: {e}")
        return jsonify({"error": f"Error fetching leaderboard: {e}"}), 500


@app.route("/referral/code", methods=["POST"])
def ensure_current_user_referral_code():
    """
    Authenticated repair endpoint for the current user's referral code.
    Used by the mobile profile sheet so a missing legacy code can be fixed
    on demand without exposing the admin backfill endpoint to the client.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401

    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    try:
        user_doc = firestore_db.collection("users").document(uid).get()
        user_data = user_doc.to_dict() if user_doc.exists else {}
        code, created = ensure_referral_code(uid, user_data)

        if "points" not in user_data:
            firestore_db.collection("users").document(uid).set(
                {"points": 0}, merge=True
            )

        return jsonify(
            {
                "success": True,
                "referralCode": code,
                "points": user_data.get("points", 0),
                "created": created,
            }
        )
    except Exception as e:
        print(f"Ensure referral code error: {e}")
        return jsonify({"error": f"Error ensuring referral code: {e}"}), 500


@app.route("/backfill-referral-codes", methods=["POST"])
def backfill_referral_codes():
    """
    Maintenance endpoint to assign referral codes to existing users that
    predate the referral system or missed setup after a non-blocking signup
    failure. Auth uses the same shared secret header as story cleanup.
    Body: { dryRun?: bool, limit?: number }
    """
    if not CLEANUP_SECRET or request.headers.get("X-Cleanup-Secret") != CLEANUP_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dryRun", False))
    limit = data.get("limit")
    if limit is not None:
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be a number"}), 400

    users_query = firestore_db.collection("users")
    if limit:
        users_query = users_query.limit(limit)

    scanned_count = 0
    assigned_count = 0
    missing_count = 0
    errors = []

    for user_doc in users_query.stream():
        scanned_count += 1
        user_data = user_doc.to_dict() or {}
        if not user_data.get("referralCode"):
            missing_count += 1

        if dry_run:
            continue

        try:
            _code, created = ensure_referral_code(user_doc.id, user_data)
            if created:
                assigned_count += 1
            if "points" not in user_data:
                user_doc.reference.set({"points": 0}, merge=True)
        except Exception as e:
            errors.append({"userId": user_doc.id, "error": str(e)})

    return jsonify(
        {
            "success": True,
            "dryRun": dry_run,
            "scannedCount": scanned_count,
            "missingCount": missing_count,
            "assignedCount": assigned_count,
            "errorCount": len(errors),
            "errors": errors,
        }
    )


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

        mint_fn = token_contract.functions.mintToVirtual(user_id_hash, amount_wei)
        tx_hash, receipt = send_contract_transaction(mint_fn, 200000)

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

        price_fn = glas_contract.functions.setPrice(price_wei)
        tx_hash, receipt = send_contract_transaction(price_fn, 100000)

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

    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if uid != user_id:
        return jsonify({"error": "Forbidden"}), 403

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
            sync_fn = token_contract.functions.mintToVirtual(
                user_id_hash, deficit_wei
            )
            send_contract_transaction(sync_fn, 200000)

        # 7. Burn stable tokens by exporting to burn address
        burn_fn = token_contract.functions.exportTokens(
            user_id_hash, stable_amount_wei, Web3.to_checksum_address(BURN_ADDRESS)
        )
        send_contract_transaction(burn_fn, 200000)

        # 8. Credit GLAS from pool to user virtual wallet
        glas_amount_wei = w3.to_wei(glas_amount, "ether")
        credit_fn = glas_contract.functions.creditFromPool(
            user_id_hash, glas_amount_wei
        )
        send_contract_transaction(credit_fn, 200000)

        # 9. (Optional) Export GLAS to real address
        export_tx_hash = None
        if recipient:
            export_fn = glas_contract.functions.exportTokens(
                user_id_hash, glas_amount_wei, recipient
            )
            export_hash, _ = send_contract_transaction(export_fn, 200000)
            export_tx_hash = export_hash.hex()

        # 10. Record withdrawal in Firestore
        record_withdrawal(user_id, net_usd, glas_amount, stable_amount)

        if firestore_db:
            balance_ref = firestore_db.collection("tokenBalances").document(user_id)
            bal_snap = balance_ref.get()
            if bal_snap.exists:
                bal = bal_snap.to_dict()
                balance_ref.set(
                    {
                        "balance": bal.get("balance", 0) - stable_amount,
                        "lastOnChainBalance": max(
                            0, bal.get("lastOnChainBalance", 0) - stable_amount
                        ),
                    },
                    merge=True,
                )

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


def pin_file_bytes_to_ipfs(file_bytes, filename="stitched.mp4", content_type="video/mp4"):
    """
    Upload raw file bytes (no base64) to IPFS and return an ipfs:// URI.
    Uses Filebase when configured, else Pinata. Used by the video-stitch flow.
    """
    if FILEBASE_ENABLED:
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        cid = _filebase_put(f"nft/{uuid.uuid4().hex}.{ext}", file_bytes, content_type)
        return f"ipfs://{cid}"
    return f"ipfs://{_pin_bytes_to_pinata(file_bytes, filename, content_type)}"


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

        burn_fn = contract.functions.burnVirtual(user_id_hash, token_id)
        tx_hash, receipt = send_contract_transaction(burn_fn, 200000)

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
        metadata_ipfs_url = pin_metadata_to_ipfs(ipfs_url, token_name)
        print(f"Remint metadata uploaded to IPFS: {metadata_ipfs_url}")

        # Mint new token on-chain
        user_id_hash = compute_user_id_hash(user_id)

        mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
        tx_hash, receipt = send_contract_transaction(mint_fn, 500000)
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

    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if uid != user_id:
        return jsonify({"error": "Forbidden"}), 403

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

            burn_fn = contract.functions.burnVirtual(user_id_hash, old_token_id)
            send_contract_transaction(burn_fn, 200000)

            # 2. Create new metadata JSON and pin to Pinata
            token_name = "AuthenSnap Photo"
            media_type = post_data.get("mediaType", "photo")
            if media_type == "video":
                token_name = "AuthenSnap Video"
            metadata_ipfs_url = pin_metadata_to_ipfs(
                raw_ipfs_url, token_name, media_type=media_type
            )

            # 3. Mint new token
            mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
            tx_hash, receipt = send_contract_transaction(mint_fn, 500000)
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

        for r in results:
            old_id = str(r["oldTokenId"])
            new_id = str(r["newTokenId"])
            old_ref = firestore_db.collection("posts").document(old_id)
            old_snap = old_ref.get()
            if not old_snap.exists:
                continue
            old_data = old_snap.to_dict()
            new_ref = firestore_db.collection("posts").document(new_id)
            # copy likes + comments (top-level docs only, matching the client)
            for like in old_ref.collection("likes").stream():
                new_ref.collection("likes").document(like.id).set(like.to_dict())
            for c in old_ref.collection("comments").stream():
                new_ref.collection("comments").document(c.id).set(c.to_dict())
            new_ref.set(
                {
                    **old_data,
                    "tokenId": r["newTokenId"],
                    "ipfsUrl": r.get("ipfsUri"),
                    "hqMediaUrl": r.get("hqMediaUrl") or old_data.get("hqMediaUrl", ""),
                    "isPrivate": is_private,
                    "mediaType": r.get("mediaType") or old_data.get("mediaType", "photo"),
                }
            )
            for like in old_ref.collection("likes").stream():
                like.reference.delete()
            for c in old_ref.collection("comments").stream():
                c.reference.delete()
            old_ref.delete()

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


def delete_doc_recursive(doc_ref):
    """
    Delete a Firestore document and ALL of its subcollections, depth-first.
    (Firestore never cascades subcollection deletes; the Admin SDK's
    doc_ref.collections() lets us enumerate them.)
    """
    for sub_collection in doc_ref.collections():
        for sub_doc in sub_collection.stream():
            delete_doc_recursive(sub_doc.reference)
    doc_ref.delete()


@app.route("/delete-account", methods=["POST"])
def delete_account():
    """
    Full account wipe. Auth via Firebase ID token; the uid comes from the
    token ONLY - any request body is ignored. Steps run in order, each
    best-effort: failures are appended to errors[] and later steps still run.
    Returns {"deleted": true, "errors": [...]}.
    """
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    errors = []
    user_id_hash = compute_user_id_hash(uid)
    user_ref = firestore_db.collection("users").document(uid)

    # -- Gather everything later steps need BEFORE deleting anything --
    username = None
    try:
        user_doc = user_ref.get()
        if user_doc.exists:
            username = (user_doc.to_dict() or {}).get("username")
    except Exception as e:
        errors.append(f"read users/{uid}: {e}")

    hq_token_ids = set()  # for hq_media/ GCS cleanup in step 3

    # 1. Burn all virtual-wallet NFTs (sequential; tx_lock serializes nonces
    #    against concurrent regular mints and background story mints).
    token_ids = []
    try:
        token_ids = [
            int(t) for t in contract.functions.getVirtualTokens(user_id_hash).call()
        ]
    except Exception as e:
        errors.append(f"getVirtualTokens: {e}")
    for token_id in token_ids:
        hq_token_ids.add(token_id)
        try:
            burn_fn = contract.functions.burnVirtual(user_id_hash, token_id)
            send_contract_transaction(burn_fn, 200000)
        except Exception as e:
            errors.append(f"burnVirtual {token_id}: {e}")

    # 2a. Own posts, including likes/comments (and any other) subcollections.
    own_post_ids = set()
    try:
        for post_doc in (
            firestore_db.collection("posts").where("userId", "==", uid).stream()
        ):
            own_post_ids.add(post_doc.id)
            try:
                hq_token_ids.add(int(post_doc.id))  # post doc id == tokenId
            except ValueError:
                pass
            try:
                delete_doc_recursive(post_doc.reference)
            except Exception as e:
                errors.append(f"delete post {post_doc.id}: {e}")
    except Exception as e:
        errors.append(f"query own posts: {e}")

    # 2b. Likes/comments this user left on OTHER posts (collection-group on
    #     the userId field both doc types carry); decrement parent counters
    #     best-effort, skipping posts we already deleted in 2a.
    for group_name, count_field in (("likes", "likesCount"), ("comments", "commentsCount")):
        try:
            group_query = firestore_db.collection_group(group_name).where(
                "userId", "==", uid
            )
            for item_doc in group_query.stream():
                post_ref = item_doc.reference.parent.parent
                try:
                    item_doc.reference.delete()
                    if post_ref is not None and post_ref.id not in own_post_ids:
                        post_ref.update({count_field: FirestoreIncrement(-1)})
                except Exception as e:
                    errors.append(f"delete {group_name} {item_doc.reference.path}: {e}")
        except Exception as e:
            errors.append(f"collection-group {group_name}: {e}")

    # 2b2. Reactions this user left on OTHER posts (collection-group on the
    #      userId field — postService.ts:363,376,389). Parent counter is a
    #      per-emoji MAP (posts/{id}.reactionCounts.{type} — postService.ts:54,
    #      371,394), NOT a single number, so we DO NOT decrement it here; we
    #      only delete the reaction doc. Reactions on the user's OWN posts are
    #      already gone via delete_doc_recursive in 2a.
    try:
        reactions_query = firestore_db.collection_group("reactions").where(
            "userId", "==", uid
        )
        for reaction_doc in reactions_query.stream():
            try:
                reaction_doc.reference.delete()
            except Exception as e:
                errors.append(f"delete reaction {reaction_doc.reference.path}: {e}")
    except Exception as e:
        errors.append(f"collection-group reactions: {e}")

    # 2c. Stories: GCS blob + views subcollection + doc (same treatment as
    #     /cleanup-expired-stories, line ~1688).
    try:
        for story_doc in (
            firestore_db.collection("stories").where("userId", "==", uid).stream()
        ):
            story_data = story_doc.to_dict() or {}
            blob_path = story_data.get("mediaPath", "").replace("/media/", "", 1)
            try:
                if gcs_bucket and blob_path:
                    blob = gcs_bucket.blob(blob_path)
                    if blob.exists():
                        blob.delete()
                delete_doc_recursive(story_doc.reference)
            except Exception as e:
                errors.append(f"delete story {story_doc.id}: {e}")
    except Exception as e:
        errors.append(f"query stories: {e}")

    # 2d. Follow/block mirror docs on OTHER users. Must run before 2i wipes
    #     our own subcollections (the doc ids ARE the other users' uids -
    #     postService.ts:768-779, blockService.ts:26-27). blocked/blockedBy
    #     may not exist yet (built by the block-users section); streaming an
    #     absent subcollection just yields nothing.
    mirror_map = (
        ("followers", "following"),   # users/{F}/following/{uid}
        ("following", "followers"),   # users/{T}/followers/{uid}
        ("blocked", "blockedBy"),     # users/{T}/blockedBy/{uid}
        ("blockedBy", "blocked"),     # users/{B}/blocked/{uid}
    )
    for own_sub, mirror_sub in mirror_map:
        try:
            for edge_doc in user_ref.collection(own_sub).stream():
                try:
                    (
                        firestore_db.collection("users")
                        .document(edge_doc.id)
                        .collection(mirror_sub)
                        .document(uid)
                        .delete()
                    )
                except Exception as e:
                    errors.append(f"mirror {mirror_sub} on {edge_doc.id}: {e}")
        except Exception as e:
            errors.append(f"list {own_sub}: {e}")

    # 2e. Username reservation (usernames/{name}, profileService.ts:37).
    if username:
        try:
            firestore_db.collection("usernames").document(username).delete()
        except Exception as e:
            errors.append(f"delete usernames/{username}: {e}")

    # 2f. Token balance doc (tokenBalances/{uid} — tokenService.ts, server.py:3429).
    try:
        firestore_db.collection("tokenBalances").document(uid).delete()
    except Exception as e:
        errors.append(f"delete tokenBalances/{uid}: {e}")

    # 2g. DM threads containing the user, incl. messages subcollections
    #     (dmThreads.participantIds — dmService.ts:167).
    try:
        dm_query = firestore_db.collection("dmThreads").where(
            "participantIds", "array_contains", uid
        )
        for thread_doc in dm_query.stream():
            try:
                delete_doc_recursive(thread_doc.reference)
            except Exception as e:
                errors.append(f"delete dmThread {thread_doc.id}: {e}")
    except Exception as e:
        errors.append(f"query dmThreads: {e}")

    # 2h. Notifications the user SENT (actorId field, collection-group —
    #     notificationService.ts:60,70). Notifications TO the user live at
    #     users/{uid}/notifications and are removed with the user doc in 2i.
    try:
        notif_query = firestore_db.collection_group("notifications").where(
            "actorId", "==", uid
        )
        for notif_doc in notif_query.stream():
            try:
                notif_doc.reference.delete()
            except Exception as e:
                errors.append(f"delete notification {notif_doc.reference.path}: {e}")
    except Exception as e:
        errors.append(f"collection-group notifications: {e}")

    # 2i. Collected-shelf docs (top-level "collections", field collectorId —
    #     postService.ts:493-494). One doc per post the user collected.
    try:
        collections_query = firestore_db.collection("collections").where(
            "collectorId", "==", uid
        )
        for coll_doc in collections_query.stream():
            try:
                coll_doc.reference.delete()
            except Exception as e:
                errors.append(f"delete collection {coll_doc.id}: {e}")
    except Exception as e:
        errors.append(f"query collections: {e}")

    # 2j. Circle memberships (top-level "circleMembers", field userId, doc id
    #     "{slug}_{uid}", each carries a slug — postService.ts:533,539,582-583).
    #     Best-effort decrement the owning circles/{slug}.memberCount, which IS
    #     a simple numeric field (postService.ts:65,543,560).
    try:
        members_query = firestore_db.collection("circleMembers").where(
            "userId", "==", uid
        )
        for member_doc in members_query.stream():
            slug = (member_doc.to_dict() or {}).get("slug")
            try:
                member_doc.reference.delete()
                if slug:
                    firestore_db.collection("circles").document(slug).update(
                        {"memberCount": FirestoreIncrement(-1)}
                    )
            except Exception as e:
                errors.append(f"delete circleMember {member_doc.id}: {e}")
    except Exception as e:
        errors.append(f"query circleMembers: {e}")

    # 2k. Finally the user doc + ALL its subcollections (following, followers,
    #     blocked, blockedBy, notifications, studioItems, ...).
    try:
        delete_doc_recursive(user_ref)
    except Exception as e:
        errors.append(f"delete users/{uid}: {e}")

    # 3. GCS cleanup: story blobs (stories/{uid}/, server.py:1710), studio
    #    blobs incl. thumbnails (studio/{uid}/, server.py:1808/1821), and
    #    hq_media/{tokenId}[.jpg|.mp4|_thumb.jpg] (server.py:796, 1285, 1310).
    if gcs_bucket:
        for prefix in (f"stories/{uid}/", f"studio/{uid}/"):
            try:
                for blob in gcs_bucket.list_blobs(prefix=prefix):
                    blob.delete()
            except Exception as e:
                errors.append(f"gcs prefix {prefix}: {e}")
        for token_id in hq_token_ids:
            for name in (
                f"hq_media/{token_id}.jpg",
                f"hq_media/{token_id}.mp4",
                f"hq_media/{token_id}_thumb.jpg",
            ):
                try:
                    blob = gcs_bucket.blob(name)
                    if blob.exists():
                        blob.delete()
                except Exception as e:
                    errors.append(f"gcs {name}: {e}")
    else:
        errors.append("gcs not configured, skipped blob cleanup")

    # 4. Firebase Auth account (Admin SDK).
    try:
        firebase_auth.delete_user(uid)
    except Exception as e:
        errors.append(f"auth delete_user: {e}")

    print(f"Account {uid} deleted with {len(errors)} errors")
    return jsonify({"deleted": True, "errors": errors})


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
        video_ipfs_url = pin_file_bytes_to_ipfs(lq_bytes, "stitched.mp4")
        print(f"Stitched video (720p) uploaded to IPFS: {video_ipfs_url}")

        metadata_ipfs_url = pin_metadata_to_ipfs(
            video_ipfs_url, "AuthenSnap Video", media_type="video"
        )
        print(f"Metadata uploaded to IPFS: {metadata_ipfs_url}")

        # Mint on-chain
        user_id_hash = compute_user_id_hash(user_id)

        mint_fn = contract.functions.mintToVirtual(user_id_hash, metadata_ipfs_url)
        tx_hash, receipt = send_contract_transaction(mint_fn, 500000)
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


@app.route("/related/<token_id>", methods=["GET"])
def related_feed(token_id):
    """
    GET endpoint for a post's "related" feed.

    Ordering (all public, non-flagged posts, by descending rankScore within
    each group):
      1. the anchor post itself (so the client always renders it at the top)
      2. posts sharing >=1 AI tag with the anchor  (topical match)
      3. everything else                            (algo-score fallback)

    Matched + fallback posts are drawn from the top-`RELATED_POOL_SIZE` ranked
    posts, then offset-paginated. The anchor is fetched by id directly, so it is
    included even if private/flagged (the user already opened it).

    Query params: limit (default 8), cursor (base64-encoded integer offset)
    Returns { posts: [...], nextCursor: string|null }
    """
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    limit = request.args.get("limit", 8, type=int)
    cursor = request.args.get("cursor", None)

    # Cursor is a base64-encoded offset into the ordered result list.
    offset = 0
    if cursor:
        try:
            offset = max(0, int(base64.b64decode(cursor).decode("utf-8")))
        except Exception as e:
            print(f"Invalid related cursor: {e}")
            offset = 0

    try:
        # Anchor post (fetched by id, independent of the public filter).
        anchor_snap = firestore_db.collection("posts").document(str(token_id)).get()
        anchor = anchor_snap.to_dict() if anchor_snap.exists else None
        anchor_tags = set(anchor.get("tags", []) or []) if anchor else set()

        # Candidate pool: top-ranked public, non-flagged posts. Reuses the same
        # composite index as /explore (no new index required).
        pool_docs = list(
            firestore_db.collection("posts")
            .where("flagged", "==", False)
            .where("isPrivate", "==", False)
            .order_by("rankScore", direction="DESCENDING")
            .limit(RELATED_POOL_SIZE)
            .stream()
        )

        # Partition the pool into tag-matches and the rest, each preserving the
        # rankScore order from the stream.
        matched = []
        others = []
        for doc_snap in pool_docs:
            if doc_snap.id == str(token_id):
                continue  # anchor is prepended separately
            data = doc_snap.to_dict()
            post_tags = set(data.get("tags", []) or [])
            if anchor_tags and (post_tags & anchor_tags):
                matched.append(data)
            else:
                others.append(data)

        ordered = matched + others
        full = ([anchor] + ordered) if anchor is not None else ordered

        page = full[offset : offset + limit]
        has_next = len(full) > offset + limit

        posts_result = []
        for data in page:
            post_out = {}
            for k, v in data.items():
                if hasattr(v, "timestamp"):
                    post_out[k] = int(v.timestamp() * 1000)  # millis for JS
                else:
                    post_out[k] = v
            posts_result.append(post_out)

        next_cursor = None
        if has_next:
            next_offset = offset + limit
            next_cursor = base64.b64encode(
                str(next_offset).encode("utf-8")
            ).decode("utf-8")

        return jsonify({"posts": posts_result, "nextCursor": next_cursor})

    except Exception as e:
        print(f"Related feed error: {e}")
        return jsonify({"error": f"Error fetching related feed: {e}"}), 500


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

        # 3. Read metadata (use bracket access for stripe SDK compatibility)
        try:
            user_id = session["metadata"]["userId"]
            tokens = int(session["metadata"]["tokens"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Invalid session metadata"}), 400

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

                mint_fn = token_contract.functions.mintToVirtual(
                    user_id_hash, amount_wei
                )
                send_contract_transaction(mint_fn, 200000)

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
        import traceback
        traceback.print_exc()
        print(f"Verify payment error: {e}", flush=True)
        return jsonify({"error": f"Failed to verify payment: {e}"}), 500


def _sync_creator_balance_async(creator_id, creator_balance, creator_last_on_chain):
    """Best-effort background on-chain reconcile once creator earnings cross the
    threshold (mirrors the client's old non-blocking post-commit sync)."""
    def run():
        try:
            delta = creator_balance - creator_last_on_chain
            if delta < SYNC_THRESHOLD or not token_contract:
                return
            creator_hash = compute_user_id_hash(creator_id)
            # Same amount->wei conversion and mintToVirtual call as /sync-token-balance:
            amount_wei = w3.to_wei(delta, "ether")
            send_contract_transaction(
                token_contract.functions.mintToVirtual(creator_hash, amount_wei), 300000
            )
            firestore_db.collection("tokenBalances").document(creator_id).set(
                {"lastOnChainBalance": creator_balance}, merge=True
            )
        except Exception as e:
            print(f"[economy sync] non-fatal for {creator_id}: {e}")
    threading.Thread(target=run, daemon=True).start()


@app.route("/collect-post", methods=["POST"])
def collect_post():
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    data = request.get_json(silent=True) or {}
    token_id = data.get("tokenId")
    collector_id = data.get("collectorId")
    creator_id = data.get("creatorId")
    if token_id is None or not collector_id or not creator_id:
        return jsonify({"error": "tokenId, collectorId, creatorId are required"}), 400
    if uid != collector_id:
        return jsonify({"error": "Forbidden: caller must be the collector"}), 403

    token_id_str = str(token_id)
    collection_ref = firestore_db.collection("collections").document(f"{token_id_str}_{collector_id}")
    post_ref = firestore_db.collection("posts").document(token_id_str)
    collector_ref = firestore_db.collection("tokenBalances").document(collector_id)
    creator_ref = firestore_db.collection("tokenBalances").document(creator_id)
    platform_ref = firestore_db.collection("tokenBalances").document("PLATFORM")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    @fb_firestore.transactional
    def run(txn):
        if collection_ref.get(transaction=txn).exists:
            return {"status": "dup"}
        post_snap = post_ref.get(transaction=txn)
        post_data = post_snap.to_dict() if post_snap.exists else {}
        if post_data.get("collectEnabled") is False:
            return {"status": "disabled"}
        raw_price = post_data.get("collectPrice")
        price = raw_price if isinstance(raw_price, (int, float)) else DEFAULT_COLLECT_PRICE
        p, creator_earns, platform_cut = collect_split(price)

        collector_snap = collector_ref.get(transaction=txn)
        collector_balance = collector_snap.to_dict().get("balance", 0) if collector_snap.exists else 0
        if collector_balance < p:
            return {"status": "insufficient"}

        creator_snap = creator_ref.get(transaction=txn)
        creator_data = creator_snap.to_dict() if creator_snap.exists else {"balance": 0, "lastOnChainBalance": 0}
        platform_snap = platform_ref.get(transaction=txn)
        platform_data = platform_snap.to_dict() if platform_snap.exists else {"balance": 0, "lastOnChainBalance": 0}

        new_creator = creator_data.get("balance", 0) + creator_earns
        new_platform = platform_data.get("balance", 0) + platform_cut

        txn.set(collector_ref, {"balance": collector_balance - p}, merge=True)
        txn.set(creator_ref, {"balance": new_creator, "lastOnChainBalance": creator_data.get("lastOnChainBalance", 0)}, merge=True)
        txn.set(platform_ref, {"balance": new_platform, "lastOnChainBalance": platform_data.get("lastOnChainBalance", 0)}, merge=True)
        txn.set(collection_ref, {"tokenId": token_id, "collectorId": collector_id, "creatorId": creator_id, "createdAt": now_ms})
        txn.set(post_ref, {"collectsCount": FirestoreIncrement(1)}, merge=True)
        return {"status": "ok", "creatorBalance": new_creator, "creatorLastOnChain": creator_data.get("lastOnChainBalance", 0)}

    result = run(firestore_db.transaction())
    status = result["status"]
    if status == "dup":
        return jsonify({"collected": False})
    if status == "disabled":
        return jsonify({"error": "Collecting is turned off for this post"}), 400
    if status == "insufficient":
        return jsonify({"error": "Insufficient token balance"}), 400
    _sync_creator_balance_async(creator_id, result["creatorBalance"], result["creatorLastOnChain"])
    return jsonify({"collected": True})


@app.route("/charge-view", methods=["POST"])
def charge_view():
    uid = verify_firebase_token(request)
    if not uid:
        return jsonify({"error": "Missing or invalid authorization token"}), 401
    if not firestore_db:
        return jsonify({"error": "Firebase not configured on server"}), 500

    data = request.get_json(silent=True) or {}
    viewer_id = data.get("viewerId")
    creator_id = data.get("creatorId")
    if not viewer_id or not creator_id:
        return jsonify({"error": "viewerId and creatorId are required"}), 400
    if uid != viewer_id:
        return jsonify({"error": "Forbidden: caller must be the viewer"}), 403

    VIEW_PRICE = 1
    VIEW_EARNING = 0.5
    viewer_ref = firestore_db.collection("tokenBalances").document(viewer_id)
    creator_ref = firestore_db.collection("tokenBalances").document(creator_id)
    platform_ref = firestore_db.collection("tokenBalances").document("PLATFORM")

    @fb_firestore.transactional
    def run(txn):
        viewer_snap = viewer_ref.get(transaction=txn)
        viewer_balance = viewer_snap.to_dict().get("balance", 0) if viewer_snap.exists else 0
        if viewer_balance < VIEW_PRICE:
            return {"status": "insufficient"}
        creator_snap = creator_ref.get(transaction=txn)
        creator_data = creator_snap.to_dict() if creator_snap.exists else {"balance": 0, "lastOnChainBalance": 0}
        platform_snap = platform_ref.get(transaction=txn)
        platform_data = platform_snap.to_dict() if platform_snap.exists else {"balance": 0, "lastOnChainBalance": 0}

        new_viewer = viewer_balance - VIEW_PRICE
        new_creator = creator_data.get("balance", 0) + VIEW_EARNING
        new_platform = platform_data.get("balance", 0) + (VIEW_PRICE - VIEW_EARNING)

        txn.set(viewer_ref, {"balance": new_viewer}, merge=True)
        txn.set(creator_ref, {"balance": new_creator, "lastOnChainBalance": creator_data.get("lastOnChainBalance", 0)}, merge=True)
        txn.set(platform_ref, {"balance": new_platform, "lastOnChainBalance": platform_data.get("lastOnChainBalance", 0)}, merge=True)
        return {"status": "ok", "viewerBalance": new_viewer, "creatorBalance": new_creator, "creatorLastOnChain": creator_data.get("lastOnChainBalance", 0)}

    result = run(firestore_db.transaction())
    if result["status"] == "insufficient":
        return jsonify({"error": "Insufficient token balance"}), 400
    _sync_creator_balance_async(creator_id, result["creatorBalance"], result["creatorLastOnChain"])
    return jsonify({"viewerBalance": result["viewerBalance"], "creatorBalance": result["creatorBalance"]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    app.run(host="0.0.0.0", port=port, threaded=True)
