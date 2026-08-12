# Account Migration Runbook — pppokerht → new GitHub/Railway/Firebase

Target: relaunch under `handtrackerpppoker@gmail.com` (GitHub + Railway), new
domain `ppptracker.up.railway.app`, without touching the old prod
(`pppokerha.up.railway.app`, repo `botarbitrage/pppokerHA`) until the new one
is verified healthy. **No secret values appear in this document** — only
variable names, commands, and click-paths.

---

## 1. GitHub

### 1.1 Remote status — verified, no action needed
Both the primary clone and this worktree already have `origin` pointing at
the new repo:
```
origin  https://github.com/handtrackerpppoker/pppokerht.git
```
`gh auth status` confirms we're authenticated as `handtrackerpppoker`
(active account). No `git remote set-url` was needed.

### 1.2 `old` remote — added
```bash
git remote add old https://github.com/botarbitrage/pppokerHA
```
Run once per local clone/worktree that needs it (this worktree has it now;
your other local checkout of the repo may still need this command run
manually if you want the same reference there).

### 1.3 Secret history check — verified clean
```bash
git log --all --full-history -- serviceAccountKey.json
```
Returns no output on any branch/ref — the file has never been committed.
`git ls-files | grep -i service` and `git ls-files | grep -i '\.json$' | grep -i key`
also return nothing. Re-run this exact command before every future push as a
standing habit, not just this one time.

### 1.4 Secret scanning + push protection — already on, verified
```bash
gh api repos/handtrackerpppoker/pppokerht --jq '.security_and_analysis'
```
Returned `secret_scanning: enabled` and `secret_scanning_push_protection:
enabled` — these are on by default for public repos and required no action.
(`dependabot_security_updates` is currently `disabled` — optional, not part
of the original ask, worth turning on separately if you want it: Settings →
Code security → Dependabot → Enable.)

### 1.5 CI workflow — added
`.github/workflows/ci.yml` runs on every push/PR to `main`: installs
`requirements.txt`, compile-checks every backend module, and runs the four
standalone test scripts (`test_hand_exporter.py`, `test_leak_cache.py`,
`test_leak_targets.py`, `test_leaks_api.py`). This didn't exist before — it
was added specifically so "require status checks to pass" (§1.6) has a real
check to require, not an empty rule.

### 1.6 Branch protection on `main` — applied via `gh api`
Applied programmatically:
```bash
gh api -X PUT repos/handtrackerpppoker/pppokerht/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks[strict]=true \
  -f 'required_status_checks[contexts][]=test' \
  -F enforce_admins=false \
  -f required_pull_request_reviews[required_approving_review_count]=1 \
  -F required_pull_request_reviews[dismiss_stale_reviews]=true \
  -F required_conversation_resolution=true \
  -F allow_force_pushes=false \
  -F allow_deletions=false \
  -F restrictions=null
```
(`test` here is the CI job's id from `.github/workflows/ci.yml` — GitHub uses
the job id as the check's display name since the job has no explicit `name:`.
This rule can only be satisfied once the `test` check has actually run at
least once — i.e. after this PR's first CI run — same caveat as the manual
path below.)
This requires 1 PR approval, requires the `test` CI job to pass, blocks
force-pushes, blocks branch deletion, and blocks direct pushes to `main`
(everyone, including admins on the push-restriction side — `enforce_admins`
is left `false` so you personally don't get locked out of emergency fixes,
but the PR/status-check/force-push rules still apply to everyone).

**Equivalent manual click-path**, if you ever want to adjust these by hand:
1. `github.com/handtrackerpppoker/pppokerht` → **Settings** → **Branches**
2. Under "Branch protection rules" → **Add rule** (or edit the one this PR created)
3. Branch name pattern: `main`
4. Check **Require a pull request before merging** → set "Required approvals" to `1` → check **Dismiss stale pull request approvals when new commits are pushed**
5. Check **Require status checks to pass before merging** → check **Require branches to be up to date before merging** → search for and select the `test` check (from the new `ci.yml` workflow — it will only appear in the list after the workflow has run at least once, i.e. after this PR's first CI run)
6. Check **Require conversation resolution before merging**
7. Check **Do not allow bypassing the above settings** only if you want it to apply to yourself too (left unchecked by the `gh api` call above, via `enforce_admins=false`)
8. Under "Rules applied to everyone including administrators": check **Allow force pushes** = **off** (leave unchecked), **Allow deletions** = off (leave unchecked)
9. **Save changes**

### 1.7 PR
Branch `launch/code-review` → PR into `main`, opened via `gh pr create`. See
the PR description for the findings summary and env var checklist.

---

## 2. Railway

### 2.1 CLI install
```bash
npm install -g @railway/cli
```
Node/npm were already present in this environment (`node v24.14.0`,
`npm 11.9.0`) — no separate Node install needed.

### 2.2 Login — **requires you, interactively**
```bash
railway login
```
This opens a browser OAuth flow. It cannot be done non-interactively, and
per the ground rules for this task nothing here should be entering
credentials on your behalf — **run this yourself**, logged into
`handtrackerpppoker@gmail.com` in whichever browser session is signed into
that account.

### 2.3 Create the project (after you've logged in)
```bash
railway init
```
Run from the repo root. This creates a new Railway project under whichever
account `railway login` authenticated. Name it something recognizable (e.g.
`pppokerht`).

### 2.4 Connect to GitHub, `main` branch, auto-deploy on push
Railway's GitHub connection is an OAuth/App-installation step. In practice
this reliably works from the dashboard (Railway needs you to grant its
GitHub App access to the specific repo, which is a consent screen, not a
scriptable API call): **Project → Settings → Service → Source → Connect
Repo** → select `handtrackerpppoker/pppokerht` → branch `main` → the
"Deploy on push" toggle is on by default once connected.

If you'd rather try the CLI path first: `railway link` inside a repo
directory that already has `origin` pointed at the GitHub repo can pick it
up automatically in some Railway CLI versions — worth a try, but the
dashboard step above is the reliable fallback if it doesn't.

### 2.5 Public domain
Target: `ppptracker.up.railway.app`. Once the service exists (after §2.3),
generate a domain from **Project → Service → Settings → Networking →
Generate Domain**, then edit the subdomain to `ppptracker`. Railway
subdomains are first-come-first-served across all Railway users — **if
`ppptracker` is taken, this step will fail visibly in the dashboard; there's
no way to reserve it in advance from here.** Flag back to whoever's driving
this if that happens so you can agree on a fallback name.

### 2.6 Environment variables — checklist (names only)
Fill these in on the new Railway project from the old project's values —
**do not paste actual secret values into chat, commit them, or put them in
this file.** Set them directly in Railway's dashboard (**Service → Variables**)
or via `railway variables --set KEY=value` run by you locally.

- [ ] `STRIPE_SECRET_KEY`
- [ ] `STRIPE_PRICE_ID`
- [ ] `STRIPE_PROTEST_PRICE_ID`
- [ ] `STRIPE_WEBHOOK_SECRET` — **note:** this one specifically should probably be a *new* value, not copied from the old project, since Stripe webhook secrets are tied to a specific webhook endpoint URL and you'll likely register a new webhook endpoint pointing at `ppptracker.up.railway.app` in Stripe's dashboard, which generates its own secret.
- [ ] `FIREBASE_SERVICE_ACCOUNT_JSON`
- [ ] `FIREBASE_STORAGE_BUCKET`
- [ ] `APP_URL` — set this to `https://ppptracker.up.railway.app` explicitly (the code now falls back to the request's own host if this is unset, but setting it explicitly is still the clearest choice for a known-fixed prod domain)
- [ ] `FIREBASE_API_KEY`
- [ ] `FIREBASE_AUTH_DOMAIN`
- [ ] `FIREBASE_PROJECT_ID`
- [ ] `FIREBASE_MESSAGING_SENDER_ID`
- [ ] `FIREBASE_APP_ID`
- [ ] `FIREBASE_MEASUREMENT_ID`

Not needed — Railway injects `PORT` automatically; nothing to set for it.

### 2.7 Do NOT deploy yet
Per your acceptance criteria: the project is created and connected, but the
first deploy should wait for you to review and confirm the env var list
above is fully populated with correct values from the old project (and a
fresh `STRIPE_WEBHOOK_SECRET`, per the note in §2.6). Once you've confirmed
that, the following becomes the actual deploy step (documented here as a
procedure, not executed as part of this PR):

1. **Trigger deploy** — either push to `main` (now that CI + branch
   protection are live, this means merging this PR, or any subsequent PR)
   or `railway up` for a manual one-off deploy.
2. **Tail logs**: `railway logs` (or the dashboard's Deployments → Logs
   view) — watch for the gunicorn boot line and confirm no import-time
   crash (e.g. a missing env var would typically surface here first).
3. **Smoke test checklist** (run after a successful boot):
   - `GET https://ppptracker.up.railway.app/` → expect `200`
   - `GET https://ppptracker.up.railway.app/health` → expect `200`,
     `{"status": "ok"}` (route added in this PR, `app.py`)
   - Submit a real hand import through the UI, confirm the write lands in
     the **correct** Firestore project (check the project ID in the
     Firebase console against `FIREBASE_PROJECT_ID`, not just that a write
     happened somewhere)
   - Confirm `serviceAccountKey.json` is **not** present in the running
     container filesystem — it shouldn't be, since the app only ever reads
     credentials from the `FIREBASE_SERVICE_ACCOUNT_JSON` env var
     (`_get_admin_db()` in `app.py`) with `credentials.ApplicationDefault()`
     as the only fallback, never a hardcoded file path. You can confirm via
     `railway run ls` (or the Railway shell) if you want to double check —
     there should be no such file in the deploy image, since it was never
     committed and nothing in the build copies one in.

---

## 3. Firebase

No local credentials exist in this environment to check Firebase project
ownership programmatically — there's no `serviceAccountKey.json`, no `.env`,
and neither `gcloud` nor the `firebase` CLI is installed here. This has to
be a manual check on your end:

1. Go to [Firebase Console](https://console.firebase.google.com/), signed in
   as whichever account currently owns the project the app's
   `FIREBASE_SERVICE_ACCOUNT_JSON` / `FIREBASE_PROJECT_ID` point at (the old
   Railway project's env vars will tell you which project ID that is).
2. Open that project → **Project Settings (gear icon) → Users and
   permissions**.
3. Check whether `handtrackerpppoker@gmail.com` is already listed as an
   Owner.
   - **If yes** — nothing to do here, you can generate a fresh service
     account key from **Project Settings → Service Accounts → Generate new
     private key** while signed in as `handtrackerpppoker@gmail.com`, and
     use that for the new Railway project's `FIREBASE_SERVICE_ACCOUNT_JSON`.
   - **If no** — from an account that currently has Owner access: **Users
     and permissions → Add member** → enter `handtrackerpppoker@gmail.com`
     → role **Owner** → Add. Once that's accepted, sign in as
     `handtrackerpppoker@gmail.com` and generate the new service account key
     as above.
4. The task notes the **old key was already revoked** — so until a new key
   is generated and set as `FIREBASE_SERVICE_ACCOUNT_JSON` on the new
   Railway project, the new deployment won't be able to reach Firestore.
   This is expected to happen as part of you filling in §2.6, not before.

---

## 4. Cutover

- The old Railway project (`pppokerha.up.railway.app`) stays running,
  untouched, until the new one passes the smoke test in §2.7. Nothing in
  this task disabled or modified it.
- Once the new deployment is verified healthy end-to-end (boots, `/health`
  200s, a real hand import lands in the correct Firestore project, Stripe
  webhook registered against the new domain with its own webhook secret),
  that's the point to actually redirect users / update any external links
  to the new domain — not covered by this task, flagging it as the natural
  next step once you're ready.

---

## 5. What this task did NOT do (explicitly deferred)

- Did not fill in any Railway environment variable values.
- Did not trigger a Railway deploy.
- Did not touch the old Railway project or old GitHub repo in any way.
- Did not run `railway login` (requires your interactive browser session).
- Did not verify Firebase project ownership (no local credentials to do so
  programmatically — see §3).
