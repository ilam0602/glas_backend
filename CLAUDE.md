# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AuthenSnap Server is a Python Flask API for minting ERC721 NFTs on Ethereum Sepolia testnet. It uploads images to IPFS via Pinata and interacts with a deployed smart contract using web3.py.

## Running the Server

```bash
source venv/bin/activate
python server.py  # Starts on 0.0.0.0:8765
```

## Dependencies

```bash
source venv/bin/activate
pip install -r requirements.txt
```

## Testing

pytest IS configured (28+ `test_*.py` files at repo root). Run it after ANY server.py change:

```bash
source venv/bin/activate
python -m pytest --ignore=test_mint_sweep.py --ignore=test_signed_url.py -q
```

Tests monkeypatch `server.firestore_db` / `server.gcs_bucket` / contracts / `server.firebase_auth` (importing server.py creates live clients, so every test must replace them — see `test_report_post.py` and `fakes_common.py` for the conventions). Route-level coverage for the money/deletion paths lives in `test_withdraw_tokens.py`, `test_burn_nft.py`, `test_toggle_privacy.py`, `test_charge_view.py`, `test_collect_post.py`, `test_delete_account.py`.

## Architecture

This is a single-file Flask application (`server.py`) with three endpoints:

- **GET /tokens/<wallet_address>** — Returns all NFTs owned by a wallet by querying the smart contract's `balanceOf` and `tokenOfOwnerByIndex`
- **POST /mint** — Accepts `{"image": "<base64>", "walletAddress": "0x..."}`, uploads the image to Pinata/IPFS, builds and signs an EIP-1559 transaction, submits it, and waits for confirmation (180s timeout)
- **POST /cancel-pending** — Cancels stuck pending transactions by sending 0-value self-transfers with higher gas

### Key Integration Points

- **Ethereum:** web3.py connects to Sepolia via Alchemy RPC. Transactions are signed server-side with a private key.
- **IPFS:** Images are uploaded to Pinata's pinning API, returning an `ipfs://` URI used as the token's image link.
- **Smart Contract:** ABI is in `AuthenSnap.json`. The contract is ERC721 Enumerable. The main method is `mint(recipient, imageLink)`.

### Transaction Flow (Minting)

1. Validate wallet address checksum
2. Upload base64-decoded image to Pinata
3. Cancel any pending transactions (auto-recovery)
4. Build EIP-1559 tx with aggressive gas pricing (3x priority fee, 2x max fee)
5. Sign and send transaction
6. Wait for receipt, extract tokenId from `Transfer` event logs

## Environment Variables (loaded from `.env`)

- `SEPOLIA_RPC_URL` — Alchemy RPC endpoint (required)
- `PRIVATE_KEY` — Account private key for signing (required)
- `CONTRACT_ADDRESS` — Deployed ERC721 contract address (required)
- `FILEBASE_KEY` / `FILEBASE_SECRET` / `FILEBASE_BUCKET` — Filebase (S3-backed
  IPFS) credentials, the sole IPFS pinning path. Required — the server raises
  `RuntimeError` at startup if any is missing.
- `PINATA_API_KEY` / `PINATA_SECRET_API_KEY` — Pinata credentials. Optional and
  **unpin-only**: used solely to unpin legacy content that was pinned to
  Pinata before Filebase existed (`_unpin_from_pinata`). Pinata is no longer a
  pinning fallback of any kind.
- `IPFS_GATEWAY` — gateway used to build https URLs inside ERC-721 metadata
  JSON (default `https://ipfs.io/ipfs/`).
- `IPFS_REDIRECT_GATEWAY` — public gateway that `GET /ipfs/<cid>` redirects to
  (default `https://gateway.pinata.cloud/ipfs/`, matching what the mobile
  client already renders).

### CSAM screening (Phase 1 — see
docs/specs/2026-10-09-csam-screening-quarantine-report-design.md)

All optional; the feature ships dark (`CSAM_SCREEN_PROVIDER=disabled`) until
a vendor is signed, so none of these need to be set today. Implemented in
`csam_screening.py`, hooked into the mint pipeline, `/story-upload`,
`/avatar-upload`, `/studio-upload`, and `/stitch` before anything is
compressed/pinned/minted, plus a `POST /csam/sweep` route (same
Cloud-Scheduler auth as `/mint/sweep`) that reconciles pending vendor tickets
and purges retention-expired quarantine evidence.

- `CSAM_SCREEN_PROVIDER` — `disabled` (default) | `webpurify`.
- `CSAM_FAIL_MODE` — `open` (default: proceed + log CRITICAL on a provider
  error) | `closed` (abort the upload with the generic error).
- `CSAM_QUARANTINE_BUCKET` — GCS bucket for quarantined evidence (default
  `glas-csam-quarantine`). Must be a separate, locked-down bucket per the
  spec (no public access, no app read path) — not yet provisioned.
- `CSAM_RETENTION_DAYS` — quarantine auto-purge window for **reported**
  incidents only (default `120`; never-reported incidents are a legal hold
  and are never purged regardless of age).
- `WEBPURIFY_API_KEY` — WebPurify REST API key (webpurify provider only).
- `CSAM_MAX_VIDEO_FRAMES` — cap on frames sampled per video (default `60`).
- `CSAM_POLL_SECONDS` — how long to poll WebPurify's async imgstatus check
  before treating a submission as "pending" (default `20`).
