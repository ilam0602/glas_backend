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

No test framework is currently configured.

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

## Environment Variables (all required, loaded from `.env`)

- `SEPOLIA_RPC_URL` — Alchemy RPC endpoint
- `PRIVATE_KEY` — Account private key for signing
- `CONTRACT_ADDRESS` — Deployed ERC721 contract address
- `PINATA_API_KEY` / `PINATA_SECRET_API_KEY` — Pinata IPFS credentials
