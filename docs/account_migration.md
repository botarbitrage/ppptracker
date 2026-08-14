# Account Migration Runbook — pppokerht → new GitHub/Railway/Firebase

> **Rename note (2026-08-14):** The GitHub repo and the Railway service were both
> renamed from `pppokerht` to **`ppptracker`**. The `origin` remote is now
> `https://github.com/handtrackerpppoker/ppptracker.git`. The historical commands
> and references below still say `pppokerht` (repo *and* `--service pppokerht`) —
> they are left as-run for the record. GitHub redirects old repo URLs and
> `gh api repos/handtrackerpppoker/pppokerht/...` calls to the new name
> indefinitely (as long as no new repo reuses `pppokerht`), and the Railway
> commands should now target `--service ppptracker`. The live domain
> `ppptracker.up.railway.app`, the Firebase project `pppoker-analyser`, and Stripe
> are unaffected by either rename.

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

### 2.1 CLI install — done
```bash
npm install -g @railway/cli
```
Node/npm were already present in this environment (`node v24.14.0`,
`npm 11.9.0`) — no separate Node install needed. Installed `railway v5.37.7`.

### 2.2 Login — done (you)
```bash
railway login
```
This is a browser OAuth flow that has to be run by a human — done by you,
confirmed via `railway whoami` → `Logged in as PPPoker Hand Tracker
(handtrackerpppoker@gmail.com)`.

### 2.3 Create the project — done
```bash
railway init --name pppokerht
```
Created project **`pppokerht`** under workspace "PPPoker Hand Tracker's
Projects" (i.e. `handtrackerpppoker@gmail.com`).
Dashboard: https://railway.com/project/75fb9e1e-38a3-4baf-94c5-864f8306a201

### 2.4 Connect to GitHub, `main` branch — done
```bash
railway add --service pppokerht --repo handtrackerpppoker/pppokerht --branch main
```
Service `pppokerht` is connected to `handtrackerpppoker/pppokerht` on
`main`, auto-deploy on push (Railway's default once a repo is connected).

**⚠️ Important — this step immediately triggered a real build+deploy.**
Connecting a GitHub repo to a Railway service isn't just a config change —
Railway kicks off a build right away. That first deploy built and started
running (no env vars set, so `firebase_admin.initialize_app()` would only
have failed once something actually tried to touch Firestore — Flask itself
booted fine). It's been stopped and removed (`railway down`) so nothing is
running right now — confirmed via `railway status` showing
`activeDeployments: []`.

**This means the "don't deploy until I approve env vars" instruction has one
real consequence going forward: merging PR #1 (or any future push to
`main`) will trigger another automatic deploy**, the same way this one
fired. There's no CLI toggle to disable that while keeping the GitHub
connection — the practical mitigation is: **don't merge PR #1 until the env
vars in §2.6 are set on the Railway service first.** If you want an extra
safety margin, Railway's dashboard has a per-service "Automatic deploys"
toggle under **Service → Settings → Source** you can switch off until
you're ready, then back on.

### 2.5 Public domain — done
Target `ppptracker.up.railway.app` was available and is now live:
```bash
railway domain --service pppokerht                                    # generated pppokerht-production.up.railway.app
railway domain update pppokerht-production.up.railway.app \
  --domain ppptracker --service pppokerht                             # renamed to ppptracker.up.railway.app
```
Confirmed `syncStatus: ACTIVE` via `railway domain list --service pppokerht --json`.
No fallback name was needed.

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

### 2.7 First deploy + smoke test — done
Deployed and tested at your explicit request, ahead of merging the PR (via
`railway up`, deploying this branch's snapshot directly rather than
merging to `main` first — the PR is still open and unmerged as of this
writing).

1. **Trigger deploy** — `railway up --service pppokerht --environment
   production`. Note: changing Railway variables also auto-triggers a
   redeploy, but that redeploy rebuilds from whatever the connected GitHub
   source (`main`) currently is — since `main` doesn't have this PR's fixes
   yet, a var-change-triggered redeploy will silently serve the **old,
   unfixed** code until the PR is merged. Hit this exact thing once during
   testing (`/health` 404'd because a var change redeployed `main`) and had
   to re-run `railway up` to restore the fixed snapshot. **Merge the PR
   before relying on auto-deploy-on-var-change or auto-deploy-on-push.**
   One build attempt also hit a transient Railway build-infra error (`mise`
   HTTP/2 "refused stream" downloading the Python runtime) — unrelated to
   this app, resolved by simply retrying `railway up`.
2. **Tail logs**: `railway logs --service pppokerht --deployment` —
   confirmed clean gunicorn boot, no import-time crash.
3. **Smoke test checklist:**
   - [x] `GET https://ppptracker.up.railway.app/` → `200`
   - [x] `GET https://ppptracker.up.railway.app/health` → `200`,
     `{"status": "ok"}`
   - [x] Signed in through the UI, tournaments list loads real data from
     the correct Firestore project (`pppoker-analyser`, confirmed via
     `/api/firebase-config` and a direct Firestore REST probe), leaks page
     opens. Legacy multi-time `starting_time` rows (e.g. `23:30, 05:30,
     07:30, 11:30`) render correctly, no crash.
   - [ ] Hit an `auth/unauthorized-domain` error and then a broken OAuth
     popup along the way — both were config issues (domain not yet
     authorized, then `FIREBASE_AUTH_DOMAIN` set to the storage-bucket
     domain instead of the auth domain), not code bugs. See §3 for the
     full story. Both fixed now.
   - [x] Stripe flow — **tested and confirmed** (2026-08-14): webhook endpoint
     registered against `ppptracker.up.railway.app`, end-to-end upgrade-to-Pro
     checkout run against a real checkout and working. See §5.
   - [ ] `serviceAccountKey.json` absent from the running container —
     attempted via `railway ssh` but hit host-key verification issues in
     this environment and didn't chase it further, since it's already
     guaranteed by code (`_get_admin_db()` only ever reads
     `FIREBASE_SERVICE_ACCOUNT_JSON` from env, `ApplicationDefault()` as
     the only fallback, never a file path) and by the build logs (`copy
     /app` mirrors the repo, which has never contained that file). If you
     want to double-check directly: `railway ssh --service pppokerht`,
     register your SSH key first if prompted (`railway ssh keys add`).

---

## 3. Firebase

**Status: done, verified working end-to-end.** Reused the **existing**
Firebase project `pppoker-analyser` (the one with real tournament/config/
user data already in it) as the single Firebase backend for the new site,
rather than standing up the brand-new empty `ppptracker` project created
earlier in this process. That avoided a data-migration step entirely — same
project, same data, just a second Owner and a repointed deployment.
Confirmed live: signed in successfully, tournaments list loads real data
(including the six legacy `starting_time` docs displaying correctly, e.g.
multi-time rows like `23:30, 05:30, 07:30, 11:30`), leaks page opens.

**Gotcha hit during setup, for next time:** `FIREBASE_AUTH_DOMAIN` was
initially set to `pppoker-analyser.firebasestorage.app` (the storage
bucket's domain) instead of the actual auth domain
`pppoker-analyser.firebaseapp.com`. Sign-in failed with a browser-level
`ERR_NAME_NOT_RESOLVED` on the OAuth popup because that storage-bucket host
doesn't serve the Firebase Auth handler. Both values live right next to
each other in the Console's SDK config object (`storageBucket` vs
`authDomain`) and are easy to swap by mistake — worth double-checking
against the Console directly rather than pattern-guessing the value if this
ever needs to be re-entered.

No local credentials exist in this environment to do any of this
programmatically — there's no `serviceAccountKey.json`, no `.env`, and
neither `gcloud` nor the `firebase` CLI is installed here. All of the
following is manual, on your end, in the Firebase Console:

1. Sign in to [Firebase Console](https://console.firebase.google.com/) as
   whichever account currently owns the **existing** project (the one the
   old Railway deployment's `FIREBASE_PROJECT_ID` points at).
2. Open that project → **Project Settings (gear icon) → Users and
   permissions** → **Add member** → `handtrackerpppoker@gmail.com` → role
   **Owner** → Add.
3. **Authentication → Settings → Authorized domains → Add domain** →
   `ppptracker.up.railway.app` (this has to be added to *this* project
   specifically — the earlier attempt to sign in failed with
   `auth/unauthorized-domain` because the domain was only ever a candidate
   for the new, now-unused `ppptracker` project, never for this one).
4. Once accepted, sign in as `handtrackerpppoker@gmail.com` and generate a
   fresh key: **Project Settings → Service Accounts → Generate new private
   key**.
5. Grab this project's public Web SDK config values from **Project
   Settings → General → Your apps → SDK setup and configuration** (the
   `firebaseConfig` object) — these map directly to the
   `FIREBASE_API_KEY` / `FIREBASE_AUTH_DOMAIN` / `FIREBASE_PROJECT_ID` /
   `FIREBASE_MESSAGING_SENDER_ID` / `FIREBASE_APP_ID` /
   `FIREBASE_MEASUREMENT_ID` / `FIREBASE_STORAGE_BUCKET` Railway variables.
6. **Update the Railway service's variables** (`pppokerht` service,
   `production` environment) to the existing project's values from steps 4
   and 5 — this **overwrites** what's currently set (those currently point
   at the new, now-unused `ppptracker` project). Same rule as before: enter
   these yourself in the Railway dashboard or via `railway variables --set`
   run from your own terminal — not something to paste here for me to
   enter.
7. The task notes the **old key was already revoked** — so until step 4's
   fresh key lands in `FIREBASE_SERVICE_ACCOUNT_JSON`, the deployment can't
   reach Firestore. Expected until you do step 6, not before.
8. **Confirm the existing project's `firestore.rules` matches (or is at
   least compatible with) [`firestore.rules`](../firestore.rules) in this
   repo** — if that project's deployed rules differ from what's in the repo
   (e.g. an older ruleset from before some of the fields/collections this
   app now uses), reads/writes could still fail even with everything else
   correct. Re-deploy the repo's rules to it if unsure
   (`firebase deploy --only firestore:rules`, or paste into the Console's
   Rules tab and publish).
9. **The newly-created empty `ppptracker` Firebase project is now unused.**
   Nothing here deletes it — leaving it alone is harmless (an empty,
   inactive project costs nothing), or delete it yourself later if you'd
   rather not have a stray project around. Not something to delete
   automatically as part of this task.

Once steps 1-8 are done, tell me and I'll re-verify: the public
`/api/firebase-config` endpoint will show the new project ID, a direct
Firestore REST query (no credentials needed for this diagnostic, same
approach used to find this issue) should stop returning
`PERMISSION_DENIED` / `CONSUMER_INVALID`, and I'll re-test sign-in and the
tournaments page in the browser.

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

## 5. Current status / what's still open

Updated after live testing (deployed and verified working end-to-end at
your request, ahead of merging the PR):

**Done:**
- All 13 Railway env vars filled in, pointing at the existing
  `pppoker-analyser` Firebase project.
- `handtrackerpppoker@gmail.com` added as Owner on that Firebase project.
- `ppptracker.up.railway.app` authorized in Firebase Auth's domain
  allowlist.
- Deployed and smoke-tested: boots clean, `/` and `/health` both 200,
  sign-in works, tournaments page loads real data correctly (including the
  legacy multi-time `starting_time` rows).
- **PR #1 merged to `main`** (squash, commit `146a9e5`), and the second-pass
  fixes from `launch_review.md` are deployed and verified live in prod:
  unauthenticated checkout returns `401` and ignores a body-supplied uid,
  export endpoints require a `session_id`, the MKO badge class is served,
  the Pro-bypass dev buttons are gone from the served HTML/JS, and
  `/tournaments` + `/leaks` both return `200`.
- Branch protection on `main` adjusted: required approvals dropped from 1 to
  **0**, because GitHub does not allow approving your own PR and this is a
  single-owner repo — the rule was unsatisfiable and could only ever be
  cleared with an admin bypass. The protections that actually catch mistakes
  are unchanged: the `test` CI check is still required, and force-pushes and
  branch deletion are still blocked. If a second maintainer ever joins, raise
  this back to 1.

**✅ Railway auto-deploy from GitHub — working.** The Railway `pppokerht`
service is connected to `handtrackerpppoker/pppokerht` and deploys `main`
automatically on every push/merge — **no `railway up` or any other command is
required.** (An earlier transient `mise install` HTTP/2 failure on Railway's
builder, while it downloaded the `runtime.txt` CPython tarball, has since
cleared.)

**Done and verified (2026-08-14):**
- **✅ Firestore rules deployed** — the Firebase CLI is now installed and
  authenticated, `.firebaserc` pins the `pppoker-analyser` project, and the
  hardened `firestore.rules` were published with
  `firebase deploy --only firestore:rules`. The `is_pro` self-grant bypass is
  closed in production. Optional re-check, signed in as a **non-admin, non-Pro**
  account on the live site, in the browser console:

  ```js
  firebase.firestore().collection('users').doc(firebase.auth().currentUser.uid)
    .set({ is_pro: true }, { merge: true })
    .then(() => console.log('BAD — rules are NOT live, self-grant succeeded'))
    .catch(e => console.log('GOOD — blocked:', e.code));
  ```

  Expect `GOOD — blocked: permission-denied`. A brand-new account should still
  get its `users/{uid}` doc created (the `create` rule allows `is_pro: false`).

- **✅ Stripe fully configured and tested** — `STRIPE_PRO_PRICE_ID`,
  `STRIPE_PRO_LABEL`, `STRIPE_PRICE_ID`, `STRIPE_PROTEST_PRICE_ID`,
  `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` are all set on the Railway
  service, a "PPP Hand Tracker - Pro" price exists in the Stripe catalog, the
  webhook endpoint is registered against
  `https://ppptracker.up.railway.app/api/stripe-webhook` (subscribed to
  `checkout.session.completed`, signing secret matches `STRIPE_WEBHOOK_SECRET`),
  and the **end-to-end upgrade-to-Pro checkout flow has been run against a real
  checkout and confirmed working.**

**Still open:**
- **Old Railway project / old GitHub repo** — untouched throughout, as
  required. The old prod (`pppokerha.up.railway.app`) should stay up until
  you're ready to actually cut users over to the new domain.
- **The now-unused empty `ppptracker` Firebase project** — still exists,
  harmless, delete at your convenience or leave it.
