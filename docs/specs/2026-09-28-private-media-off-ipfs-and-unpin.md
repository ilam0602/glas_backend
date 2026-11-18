# Spec: Make private posts genuinely private + best-effort IPFS unpin

**Date:** 2026-09-28
**Status:** IMPLEMENTED (uncommitted) — compiles/typechecks; NEEDS staging end-to-end test
before deploy. See "Implemented" + "Remaining / follow-ups" at bottom.
**Blast radius:** HIGH — touches `run_mint_pipeline` / `process_mint_media_item`, which mint
BOTH public and private posts. A bug breaks all posting. Test end-to-end on staging (real
mint → chain → IPFS → GCS → client display) before production deploy.

## Problem (confirmed in code, 2026-09-28)

"Private" posts are not actually private:

1. Media bytes are uploaded to public IPFS in **plaintext for public AND private posts**
   (`process_mint_media_item` → `pin_file_to_ipfs`, `server.py:1188`). Only the *URL
   reference string* stored in Firestore is Fernet-encrypted (`server.py:1740`). Anyone
   with the CID can fetch a "private" post's media from any public gateway.
2. The plaintext media gateway URL is embedded in the **on-chain NFT metadata**
   (`pin_metadata_to_ipfs`, called at `server.py:1660` with the plaintext URL, before the
   encryption step). `is_private` is never passed to metadata creation. Anyone reading the
   chain can follow tokenURI → metadata JSON → the media file.
3. Deletion never unpins: `delete_post` (`server.py:3851`) only does `recursive_delete`;
   there is zero unpin code anywhere in the server.

## Decisions (from product owner, 2026-09-28)

- **Private media OFF public IPFS**, and its URL NOT in on-chain metadata. Private media
  lives only in access-controlled cloud storage (GCS), served via the existing
  `/unlock-post` endpoint. An NFT is still minted (tokenId is the universal key), but its
  public metadata uses a **placeholder image**, not the private media.
- **Best-effort unpin on delete**, with a "cannot guarantee deletion" caveat (already
  written into the Privacy Policy §7/§12 and Terms §11).

## Server changes

### 1. Thread `is_private` into media processing
- `process_mint_media_item(base64, media_type, is_private=False)`.
- When `is_private`: **skip** `pin_file_to_ipfs`. Set `ipfs_url = None`. Still produce
  `hq_bytes` for the GCS upload (that becomes the real media, encrypted-referenced as
  today).

### 2. Placeholder metadata for private posts
- In `run_mint_pipeline`, when `is_private`, call `pin_metadata_to_ipfs` with a fixed
  placeholder image URI (e.g. a bundled "This post is private" asset pinned once, or a
  static gateway URL) — never `primary["ipfs_url"]`.
- Do not include a `media` array of private items.

### 3. Response / Firestore for private posts
- For private posts `ipfs_uri` is None; the real media is `hqMediaUrl` (GCS), stored
  Fernet-encrypted as today. Ensure `save_post` / the mint response make `hqMediaUrl` the
  authoritative media source when `ipfsUrl` is absent.
- `/unlock-post` already returns the decrypted `hqMediaUrl` to owner/followers — verify it
  still works when `ipfsUrl` is absent.

### 4. Persist Filebase object key (REQUIRED for unpin)
- `_filebase_put` currently returns only the CID; the object **key** (`nft/{uuid}.{ext}`)
  is discarded. Change it to return `(cid, key)` (or store the key alongside).
- Persist the key(s) on the post doc (e.g. `_ipfsKeys: [...]`, `_metadataKey: ...`) so
  delete can unpin. NOTE: only posts minted AFTER this change will be unpinnable.

### 5. Unpin on delete
- Add `unpin_from_ipfs(post_data)`:
  - Filebase: `client.delete_object(Bucket, Key=stored_key)` for each stored key.
  - Pinata fallback: `DELETE {PINATA}/pinning/unpin/{CID}` (Pinata unpins by CID, which we
    have).
  - Best-effort: swallow errors, log them; never fail the delete on an unpin error.
- Call it from `delete_post`, and ideally from `delete-story`, `delete_studio_item`, and
  `delete-account` for consistency.

## Client changes (glass_mobile)
- Media resolver: a private post now has no `ipfsUrl`; it must resolve media via the
  `/unlock-post` GCS URL (owner/followers) or show the private placeholder to others.
  Verify the resolver + post/story/profile render paths handle `ipfsUrl == null`.

## Legacy / non-goals
- Already-minted private posts CANNOT be retroactively fixed: their plaintext media + CID
  are already on IPFS and on-chain (immutable). Consider a one-time script to unpin their
  Filebase objects IF keys can be recovered (they can't for old pins — keys weren't
  stored). Document this residual exposure.

## Publish gate
- Do NOT run `npm run build:legal` (which bundles the policy into the app) until this
  server change + unpin are deployed. The updated Privacy Policy / Terms already describe
  this behavior as live; publishing before deploy would make those statements untrue.

## Test checklist (staging)
- [ ] Public post mint: media on IPFS, metadata has media URL, displays in app + OpenSea.
- [ ] Private post mint: NO media on IPFS, metadata has placeholder only, media displays
      in-app for owner/follower via /unlock-post, hidden from non-followers.
- [ ] Delete public post: Filebase object + metadata unpinned (verify gone from bucket);
      Pinata unpin path if Filebase disabled.
- [ ] Delete private post: GCS media removed, no IPFS to unpin.
- [ ] Regression: carousel posts, videos (thumbnail), account deletion.

## Implemented (2026-09-28, uncommitted)

Server (`authensnap_server/server.py`):
- `PRIVATE_POST_PLACEHOLDER_IMAGE` constant (env-overridable) for private on-chain metadata.
- `_filebase_put` now returns `(cid, key)`; all callers updated (incl. `pin_file_bytes_to_ipfs`).
- `pin_file_to_ipfs` / `pin_metadata_to_ipfs` gained opt-in `return_key=` (existing callers
  unchanged — they still get a plain URI string).
- `process_mint_media_item(..., is_private=False)`: private skips the public IPFS pin
  (`ipfs_url=None`); returns `ipfs_key`.
- `run_mint_pipeline`: private uses placeholder metadata; private media-items store
  `ipfs_uri=""` + encrypted HQ; collects `_ipfs_keys`.
- `save_post_server`: persists `_ipfsKeys` on the post doc.
- `unpin_post_media()` + `_unpin_from_pinata()` + `_extract_cid()`; `delete_post` now
  best-effort unpins before deleting DB records.
- `remint_post` / `toggle_privacy`: use placeholder metadata when target is private (stops
  NEW on-chain metadata from embedding media on public→private); `remint_post` no longer
  requires ipfsUrl for private.

Client (`glass_mobile/components/PostItem.tsx`):
- Owner now also calls `/unlock-post` (created-private media has no client-decryptable URL);
  all private media (owner + followers) resolves via `unlockedUrls`; owner sees an
  "Unlocking…" state then the media.

PUBLIC POST PATH IS UNCHANGED. Legacy posts are untouched (unpin only acts on posts that
carry the new `_ipfsKeys`).

## Remaining / follow-ups (NOT done — need testing/decisions)
- **Stories** (`server.py` ~L2985 story mint): still pins private story media to public
  IPFS (same leak). Ephemeral (24h) but the IPFS copy persists. Fix mirrors the post path.
- **Profile grid thumbnails** (`ProfileView.tsx`): the OWNER's own created-private posts
  show a blank/placeholder thumbnail in the grid (grid doesn't call /unlock). The post
  opens fine in feed/detail. Add grid-level unlock or a "private" placeholder.
- **public→private on media already public**: the earlier public IPFS copy can't be
  guaranteed removed (best-effort unpin only for posts with stored keys). Legacy public
  media has no stored key → not unpinnable. Legal docs note this.
- **private→public toggle** of a CREATED-private post: media isn't on IPFS and on-chain
  metadata is placeholder (immutable). It stays viewable in-app via GCS but isn't
  (re)published to public IPFS. Decide if that's acceptable or add re-pin-on-publish.
- **Placeholder asset**: host a real image at `PRIVATE_POST_PLACEHOLDER_IMAGE`.
- **Deploy gate**: only run `npm run build:legal` after this is deployed.
