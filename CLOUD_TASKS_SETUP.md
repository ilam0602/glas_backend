# Async minting — Cloud Run + Cloud Tasks setup (GUI)

Goal: uploads survive the app being closed / phone locked. The client uploads the
image and gets an instant ack; the slow on-chain mint runs server-side via a
**Cloud Tasks** queue that calls back into this same Cloud Run service. No second
service, no CPU-always-on, can scale to zero, and it deploys on push.

Flow:
```
app → POST /mint  (stash image to GCS + write mintJobs/{id}, enqueue task) → 202 {jobId}
                                   │
                          Cloud Tasks queue  ──OIDC-authed POST──►  POST /mint/process
                                                                     (mint + save post + points)
```

The server code reads these env vars (set in step D):

| Env var | Example | What it is |
|---|---|---|
| `TASKS_PROJECT` | `guardianapp-1` | GCP project that owns the queue |
| `TASKS_LOCATION` | `us-central1` | Queue region (match the queue) |
| `TASKS_QUEUE` | `mint-queue` | Queue name |
| `TASKS_TARGET_URL` | `https://glas-backend-xxxx.run.app/mint/process` | Where Cloud Tasks POSTs the job |
| `TASKS_INVOKER_SA_EMAIL` | `mint-invoker@<proj>.iam.gserviceaccount.com` | SA the queue uses to call Cloud Run (OIDC) |
| `MINT_WORKER_SECRET` | (a long random string) | Extra shared-secret check on `/mint/process` |

---

## A. Raise the Cloud Run request timeout (so a long mint fits)
Console → **Cloud Run** → your `glas-backend` service → **Edit & deploy new revision** →
**Container(s) → Settings** → **Request timeout** → set to **3600** (seconds) → Deploy.
(The mint runs inside the `/mint/process` request, so it needs a long timeout.
`/mint` itself returns in ~1s.)

## B. Create the Cloud Tasks queue
Console → search **Cloud Tasks** → **Create queue**.
- Name: `mint-queue`
- Region: **same region as your Cloud Run service** (e.g. `us-central1`)
- Leave defaults. (Optional, recommended for safety: after creating, **Edit** the
  queue and set **Max attempts = 1** — the mint is on-chain, so we don't want
  automatic retries double-minting. The app just re-posts if a job fails.)
- Create.

## C. Service account for the queue → allow it to call Cloud Run (OIDC)
1. Console → **IAM & Admin → Service Accounts → Create service account**.
   - Name: `mint-invoker` → Create.
   - Note its email: `mint-invoker@<project>.iam.gserviceaccount.com`.
2. Give it permission to invoke the Cloud Run service:
   Console → **Cloud Run → glas-backend → Security** tab (or **Permissions**) →
   **Add principal** → paste `mint-invoker@…` → Role **Cloud Run Invoker** → Save.
3. Let the *running* service create tasks that mint OIDC tokens as that SA:
   the service's own runtime service account (Cloud Run → service → **Security →
   Service account**, note it) needs **Service Account User** on `mint-invoker`.
   Console → **IAM → Service Accounts → mint-invoker → Permissions → Grant access** →
   principal = the Cloud Run runtime SA → role **Service Account User** → Save.
4. Also grant the Cloud Run runtime SA the **Cloud Tasks Enqueuer** role:
   Console → **IAM & Admin → IAM → Grant access** → principal = runtime SA →
   role **Cloud Tasks Enqueuer** → Save.

## D. Set the env vars on the service
Console → **Cloud Run → glas-backend → Edit & deploy new revision → Variables & Secrets**
→ add each var from the table above (`TASKS_PROJECT`, `TASKS_LOCATION`, `TASKS_QUEUE`,
`TASKS_TARGET_URL`, `TASKS_INVOKER_SA_EMAIL`, `MINT_WORKER_SECRET`). Put
`MINT_WORKER_SECRET` in **Secret Manager** and reference it (or a plain var for now).
Deploy.

## E. Build on push (continuous deployment from GitHub)
Console → **Cloud Run → glas-backend → Edit & deploy new revision → top of the page,
"Continuously deploy from a repository" → Set up with Cloud Build**.
- **Repository provider:** GitHub → authenticate → pick `ilam0602/glas_backend`.
- **Branch:** `^main$`
- **Build type:** **Dockerfile** (path `/Dockerfile` — this repo has one).
- Save. From now on, **every push to `main` builds the image and deploys a new
  revision automatically.** (First-time it also enables the Cloud Build API and
  creates a trigger you can see under **Cloud Build → Triggers**.)

Note: the runtime service account, timeout, and env vars set above **persist across
these auto-deploys** (they're service config, not per-build) — you don't have to
re-enter them each push.

## F. Verify
1. `git push` → watch **Cloud Build → History** build, then **Cloud Run → Revisions**
   show a new revision serving traffic.
2. In the app, post a photo, then immediately lock the phone. After a bit, the post
   should appear. Check **Cloud Tasks → mint-queue** (tasks flowing) and the service
   logs for `/mint` (202) then `/mint/process` (mint completed).
3. If `/mint/process` returns 401/403, the OIDC wiring (step C) is off; if tasks
   never fire, check `TASKS_*` env vars and the Enqueuer role.

## Firestore rules (one addition)
The app watches its upload's job doc to clear the "Uploading…" card, so the owner
must be able to **read** `mintJobs/{id}` (the server writes them via Admin SDK, so
client writes stay denied):
```
match /mintJobs/{id} {
  allow read: if request.auth != null && resource.data.userId == request.auth.uid;
  allow write: if false;   // server-only
}
```
(`mintResults/{id}` stays server-only as before. The temp upload bytes live under
GCS `mint_uploads/…` and are deleted as soon as the job finishes.)

## Local dev note
Locally there's no Cloud Tasks; set `MINT_WORKER_SECRET` and (the code supports)
calling `/mint/process` directly with that secret header, or run the mint inline —
see the server's `enqueue_mint_job` fallback.
