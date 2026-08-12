# Launch Readiness Review — pppokerht

Scope: full-repo review ahead of relaunch under a new GitHub/Railway/Firebase owner
account. Findings are grouped by severity; each entry cites `file:line`, what's
wrong, the fix, and the blast radius if left alone. Three items marked **[FIXED
IN THIS PR]** were patched directly on this branch per an explicit go-ahead;
everything else is report-only.

No secrets, keys, or credential values appear anywhere in this document.

---

## 1. LAUNCH BLOCKERS

### 1.1 — MKO tournaments render as plain MTT and are excluded from PKO stats — **[FIXED IN THIS PR]**
- **Where:** `templates/tournaments.html:527-534` (`badgeClass()`), `static/app.js:1244` (`pkoCount`)
- **What was wrong:** Badge/type classification was a raw `type.toUpperCase().includes('PKO')` substring test with no MKO case and no brown badge CSS class. `"MKO".includes("PKO")` is `false`, so MKO tournaments (a PKO/bounty variant, brown badge in the source app) fell through to the generic blue MTT badge and were undercounted in the "PKO" summary pill. The `is_pko` field on the tournament doc was never consulted by the badge renderer at all — an admin could correctly set `is_pko: true` and it would have zero visual effect.
- **Fix applied:** Added an `MKO` branch to `badgeClass()` (checked before the `PKO` branch), a new `.tourney-badge-mko` CSS class (brown, matching the visual language of the other three badges), and widened the `pkoCount` filter in `static/app.js:1244` to match `mko` as well as `pko`.
- **Blast radius if left alone:** Every MKO tournament in the product would be mis-badged and mis-counted for the life of the app — a correctness bug visible to every user on the tournaments page, not an edge case.
- **Not fixed / out of scope:** `is_pko` is still not read by the badge renderer (it drives other logic elsewhere, e.g. exports); left as-is since the string-based badge now correctly covers MKO. If a tournament's `type` field is ever something other than a literal `"MKO"`/`"PKO"`/`"SAT"` substring, it will still fall through to MTT — that's a pre-existing design (free-text `type` field), not something introduced here.

### 1.2 — Cross-user hand-data leak via shared in-memory session state — **[FIXED IN THIS PR]**
- **Where:** `app.py` — was a single process-global `_session_records = None` (previously line 22-23), written unconditionally by `POST /api/analyze` with no auth check, and read by six export endpoints (`/api/export/hand`, `/api/export/tournament`, `/api/export/pokerstars`, `/api/export/json/all`, `/api/export/json/tournament`, `/api/export/json/hand`) that also had no auth or ownership check.
- **What was wrong:** `/api/analyze` is intentionally anonymous (paste-a-replay-link, no login required — that's a real free-tier feature, not an oversight). But because the imported hand data was stored in one shared global with no per-caller scoping, whichever user's import ran most recently was the data every subsequent export call — from any browser, any user — would receive. Two people using the app around the same time (not even concurrently — just sequentially within the same worker's lifetime) would leak one user's imported PPPoker hand history to the other via the export endpoints. Requiring Firebase auth was **not** the right fix here since it would break the intentional anonymous flow.
- **Fix applied:** Reused the app's existing per-browser `session_id` (`static/app.js` `getSessionId()`, already used to key anonymous `guests/{session_id}` usage docs in Firestore — same trust model, nothing new invented). `_session_records` is now a dict keyed by that `session_id`, sent by the client on `/api/analyze` and all seven export calls; the backend rejects analyze calls missing a `session_id` and looks up `_session_records.get(session_id)` (falling back to the same "please import first" error) instead of a bare global. A simple bound (`_SESSION_RECORDS_MAX = 200`, oldest-evicted) prevents unbounded memory growth from abandoned sessions.
- **Blast radius if left alone:** Real, low-effort cross-user data exposure of imported PPPoker hand histories — a privacy issue on a poker app where hand histories can reveal identity, stakes, and play patterns. Would have gotten worse, not better, if the Railway deployment is ever scaled to more than one gunicorn worker (`Procfile` currently pins `--workers 1`).
- **Verification note:** No live Firestore/browser environment available in this pass — verified via static review, `python -m py_compile app.py`, and grep confirming every one of the 7 frontend call sites and 7 backend read/write sites was updated consistently. **Please smoke-test the import → export flow in a real browser before considering this closed** (open two tabs/sessions, import different links in each, confirm exports don't cross over).

### 1.3 — Stripe checkout redirect silently falls back to the OLD prod URL — **[FIXED IN THIS PR]**
- **Where:** `app.py:598` (now inside `create_checkout_session`, originally `origin = request.headers.get('Origin', os.getenv('APP_URL', 'https://pppokerha.up.railway.app'))`)
- **What was wrong:** If a browser request to `/api/create-checkout-session` had no `Origin` header (plausible for some redirect flows / non-browser callers) and the `APP_URL` env var wasn't set on the new deployment, Stripe's `success_url`/`cancel_url` would be built against the **old** production URL (`pppokerha.up.railway.app`) — i.e., a customer paying on the new site could get redirected back to the old one after checkout.
- **Fix applied:** Replaced the hardcoded old-prod fallback with `request.host_url` (the current request's own host), so the redirect target is always correct for whichever deployment is actually serving the request, regardless of whether `APP_URL` is set. `APP_URL` remains a valid override if you want it, but it's no longer required to avoid leaking the old domain.
- **Blast radius if left alone:** Customers completing a real Stripe payment on the new site could land back on the old, soon-to-be-decommissioned site after paying — confusing at best, and a real support/trust problem for a paid product.

### 1.4 — `requirements.txt` has zero pinned versions and no lock file
- **Where:** `requirements.txt` (all 7 lines use `>=`)
- **What's wrong:** Every dependency (`flask`, `requests`, `tzdata`, `python-dotenv`, `gunicorn`, `stripe`, `firebase-admin`, `eval7`) is a minimum-version constraint, not an exact pin, and there's no lock file. A Railway build today resolves `flask==3.1.3`, `gunicorn==26.0.0`, `stripe==15.2.0`, `python-dotenv==1.2.2`, `requests==2.34.2`, `eval7==0.1.11`, `tzdata==2026.2` (confirmed via a clean `pip install -r requirements.txt` in this review) — a build next month could silently resolve different versions, including breaking major-version bumps (Stripe and Flask both ship breaking major versions periodically).
- **Fix (not applied — recommend a deliberate follow-up):** Pin exact versions, e.g.:
  ```
  flask==3.1.3
  requests==2.34.2
  tzdata==2026.2
  python-dotenv==1.2.2
  gunicorn==26.0.0
  stripe==15.2.0
  firebase-admin>=6.5   # keep range-checked or pin after confirming the exact resolved version separately
  eval7==0.1.11
  ```
  (firebase-admin's exact resolved version wasn't captured in this pass — pin it the same way before shipping.) Re-test the full suite after pinning.
- **Blast radius if left alone:** A future `git push` (even one unrelated to dependencies) can trigger a Railway rebuild that pulls new dependency versions and breaks the app in prod with no code change to point at — the classic "worked yesterday" incident.
- **requirements.txt does install clean** — confirmed via `pip install -r requirements.txt` in this environment, no resolution conflicts.

### 1.5 — No `/health` route existed — **[FIXED IN THIS PR]**
- **Where:** `app.py` (new route, added next to `/`)
- **What was wrong:** No health-check endpoint existed anywhere, and Railway deployments benefit from one for readiness checks and the post-deploy smoke test.
- **Fix applied:** Added `GET /health` returning `{"status": "ok"}`.

---

## 2. HIGH

### 2.1 — `POST /api/create-checkout-session` trusts a client-supplied `uid`
- **Where:** `app.py:609-628` (`create_checkout_session`)
- **What's wrong:** The route never calls `_verify_bearer` — `uid` and `email` come straight from the JSON body and are placed into Stripe `metadata`. The webhook handler (`app.py:647-683`) later reads `metadata.uid` and grants `is_pro: true` to that Firestore user document without re-validating it against the authenticated caller. A malicious client could submit someone else's Firebase `uid` in the checkout request, complete a real payment themselves, and grant `is_pro` to the other account instead of their own.
- **Fix:** Require `_verify_bearer(request)` in `create_checkout_session` and derive `uid` from the verified token server-side rather than trusting `data.get('uid')`.
- **Blast radius:** Low-likelihood (requires deliberately crafting the request) but high-impact if exploited — free Pro access granted to an attacker-chosen account, or griefing another user's account state.

### 2.2 — `_verify_bearer` / `_is_admin` silently swallow all exceptions, including infra errors
- **Where:** `app.py:686-698` (`_verify_bearer`), `app.py:699-707` (`_is_admin`)
- **What's wrong:** Both catch bare `Exception` and return `None`/`False` with no logging. A genuine Firestore/network outage during token verification or the admin-uid lookup is indistinguishable in logs from "the caller just wasn't authenticated/authorized" — every user would appear logged out and every admin action would silently 403 during an outage, with nothing in the logs pointing at the real cause.
- **Fix:** Log the exception (`app.logger.warning` or `traceback.print_exc()`) before returning the safe default, so an outage is visible instead of masquerading as an auth failure.
- **Blast radius:** Debugging a real outage would be significantly harder — support would see "everyone is logged out" reports with no server-side signal explaining why.

### 2.3 — Leak-target overlay doc: non-transactional read-then-`.set()`
- **Where:** `app.py:1367` (`leak_targets_set`), `app.py:1386` and `app.py:1394` (`leak_targets_reset`)
- **What's wrong:** All three read the existing `cells` array, modify it in Python, then `.set()` the whole doc back — without a Firestore transaction. Two concurrent admin edits (e.g. two admins editing different cells at the same time) can race: both read the same starting state, and whichever `.set()` lands second silently overwrites the first admin's change.
- **Fix:** Wrap the read-modify-write in a Firestore `@firestore.transactional` function, the same pattern already used correctly in `_merge_tournament` (`app.py:475-539`).
- **Blast radius:** Admin-only, low-frequency writes — a lost edit is annoying, not catastrophic, but worth fixing since the transactional pattern already exists elsewhere in the codebase and this is a straightforward application of it.

### 2.4 — Firestore reads/writes inside per-request loops (N+1-shaped)
- **Where:** `app.py` `_try_save_tournaments` (~line 599) → `_merge_tournament` (`app.py:486`) called once per tournament in an import batch — each call does a transactional `doc_ref.get()` + a Cloud Storage blob download + a `transaction.set()`. Also `_build_or_load_leak_cache` (leak-report cold-cache path) writes one cache doc per stale tournament inside a loop.
- **What's wrong:** A user importing hands spanning many tournaments in one paste triggers N sequential transactional read+write+blob round-trips inside a single request handler. Not incorrect, but a real latency/timeout risk as import batches grow — `Procfile` sets `--timeout 120`, so a large enough import could hit the gunicorn worker timeout.
- **Fix:** Not urgent enough to block launch, but worth benchmarking with a realistic worst-case import size before/soon after launch, and considering batched Firestore writes if it's slow.
- **Blast radius:** Slow or timed-out `/api/analyze` calls for power users with large import batches; not a correctness bug.

### 2.5 — `static/app.js` hardcodes two full blind-ladder tables + tournament timing constants, duplicating Firestore config
- **Where:** `static/app.js:1358-1366` (`_TG_CFGS`), `static/app.js:1368-1388` (`_TG_BB_LVL`), `static/app.js:1390-1404` (`_TG_LUCKY_BB_LVL`), consumed by `_tgGetCfg`/`_tgInferLevel` (`static/app.js:1406-1431`)
- **What's wrong:** The backend correctly composes blind ladders from a single source of truth (`/config/blind_structure_base` + per-tournament `blind_structure_extra`/`blind_structure_override`, resolved once in `_resolve_tournament_cfg`, `app.py:864-927` — **no backend duplication found, this part is correctly designed**). But the tournament-progress-graph feature in the frontend hand-copies two full derived ladders (levels 1-68) and per-tournament timing constants (`itm_h`, `end_h`, `late_reg_level`, level durations) that already exist in Firestore, and selects between them via string-matching on room name. If an admin edits `/config/blind_structure_base` or a tournament's blind override via the admin UI, the backend picks it up automatically — the progress graph silently keeps using the stale hardcoded values until someone manually edits the JS.
- **Fix:** Serve the resolved blind ladder + timing config from the backend (there's already `_resolve_tournament_cfg` for this) and have the progress-graph feature fetch/consume it instead of maintaining a parallel hardcoded copy.
- **Blast radius:** Not a crash — a silent, growing-over-time correctness drift in one specific chart feature. Low urgency but should be tracked; will get more painful the more the blind structure is edited via the admin UI post-launch.

### 2.6 — Input validation gaps on Firestore-writing routes
- **Where:** `app.py` `_coerce_tourney_payload` (~line 745-773, used by `admin_create_tournament`/`admin_update_tournament`) coerces types but doesn't range-check — e.g. a negative `buy_in_total` or `starting_chips` passes through as a "valid" float/int. `POST /api/analyze` trusts the shape of PPPoker's own API response (`hands = (summary_data.get("I") or [])[:200]`) with no schema validation before it flows into `process_hands`/Firestore writes.
- **Fix:** Add range checks to `_coerce_tourney_payload` for numeric tournament fields (buy-in, chips, blind values ≥ 0); consider a minimal shape check on the PPPoker API response before trusting it.
- **Blast radius:** Admin-only for the tournament config path (low risk, trusted operator); the PPPoker-response path is a third-party API contract — a malformed/changed PPPoker response could propagate unexpected types into Firestore stats. Not observed to be happening, just unguarded.

---

## 3. MEDIUM

### 3.1 — Silent `except: pass` blocks with no logging (non-auth paths)
- **Where:** `app.py` `_try_save_tournaments` per-tournament save loop (~line 599, `except Exception` around `_merge_tournament` calls), `_uid_for_customer` (`app.py:633-645`, `except Exception: pass`), `_seed_grid`/`_load_target_overlay` and the leak-cache read/save fallbacks (~lines 1200-1250, 1460-1480).
- **What's wrong:** Failures here (e.g. a Firestore blip mid-import, or a Stripe customer lookup failure) are swallowed with no `app.logger`/`traceback.print_exc()` call, so they're invisible in Railway logs — you'd only notice as "data seems to be missing" reports from users, with nothing to correlate.
- **Fix:** Add logging at each of these catch sites — doesn't need to change behavior (the fallbacks are often intentionally best-effort), just needs to be observable.

### 3.2 — Route handlers with unguarded Firestore calls
- **Where:** `GET /api/tournaments` (~line 741), `GET /api/tournaments/<id>/hands`, `POST /api/tournaments/<id>/export/json` — Firestore calls not wrapped in a route-local `try/except`.
- **What's wrong:** Not a crash (the global error handler at `app.py:34-59` correctly converts any unhandled exception on `/api/` paths into a JSON 500), but there's no route-specific error message or logging context, so debugging relies entirely on the raw exception string reaching the client.
- **Fix:** Optional — add route-local try/except with a clearer error message where it'd meaningfully help debugging; not required since the global handler already prevents a raw crash.

### 3.3 — `.set()` audit — all reviewed, one pattern flagged (2.3 above), rest justified
Every `.set(` call in the codebase (Firestore writes only; plain Python `dict.set`/`set.update()` calls excluded) was individually reviewed:

| Location | Verdict |
|---|---|
| `app.py:536` `_merge_tournament` transactional `.set()` | **Justified** — transactional recompute-and-replace of a derived stats doc; `first_seen` explicitly preserved by reading it first. |
| `app.py:662,673,675,681` Stripe webhook `.set(..., merge=True)` | **Justified** — all use `merge=True`, safe partial writes. |
| `app.py:796` `admin_create_tournament` `.set(data)` | **Justified** — brand-new doc creation, existence-checked first, no merge needed. |
| `app.py:1367,1394` leak-target overlay `.set()` | **Flagged** — see 2.3, non-transactional read-then-write race. |
| `app.py:1386` leak-target "clear all" `.set()` | **Justified** — intentional full-doc reset. |
| `app.py:1473` leak-cache `.set()` | **Justified** — derived cache doc, always fully regenerated when stale, write wrapped in try/except as explicitly best-effort. |

Every genuinely partial update elsewhere in the codebase correctly uses `.update()` (e.g. `app.py:801-818` `admin_update_tournament`, explicitly commented "merge — preserves blind_structure_extra / override, etc."; `app.py:819-834` `admin_stamp_new_tournaments` backfill). **No accidental full-document overwrites found.**

### 3.4 — `debug=True` in the local dev entrypoint
- **Where:** `app.py:1729-1730` (`if __name__ == "__main__": app.run(debug=True, port=5000)`)
- **What's wrong:** This block never runs under the actual deployment (`Procfile` uses `gunicorn app:app`), so it's not a live production risk — but if anyone ever runs `python app.py` on a machine reachable from the network, `debug=True` enables the Werkzeug debugger, which is a known RCE vector.
- **Fix:** Drop `debug=True` (or gate it behind an explicit env var), since gunicorn is the only supported way to run this in any shared environment.

### 3.5 — `.well-known/assetlinks.json` ships obvious placeholder values
- **Where:** `app.py:1697-1721` (`asset_links()`)
- **What's wrong:** `package_name: "com.yourname.pppokerha"` and `sha256_cert_fingerprints: ["REPLACE_WITH_SHA256_FINGERPRINT_FROM_PLAY_CONSOLE"]` are unfinished placeholders (correctly commented as TODO in the code). Harmless as long as no Android TWA build references this domain, but worth cleaning up or removing before launch so it doesn't look broken to anyone who checks it.
- **Fix:** Either fill in real values if a TWA build is planned, or remove the route/file until it's needed.

### 3.6 — `static/sw.js` cache name still says `pppokerha`
- **Where:** `static/sw.js:3` (`const CACHE_NAME = 'pppokerha-v5';`)
- **What's wrong:** Cosmetic only — a stale brand name in a cache-bucket string. No functional impact (it's just a cache key), but worth a rename for consistency once the new brand/domain is finalized.

---

## 4. LOW / nice-to-have

- **`app.py:1729`** — dev-only `app.run()` block could also bind to `127.0.0.1` explicitly instead of the Werkzeug default, belt-and-suspenders alongside 3.4.
- **`.gitignore`** — covers `.env`, `*.env`, `serviceAccountKey.json`, and `*serviceAccount*.json` correctly, but has no generic credential-file glob (e.g. `*.pem`, `*.key`, `credentials.json`). Not a live gap today (the app reads `FIREBASE_SERVICE_ACCOUNT_JSON` from an env var in production, not a file), but cheap insurance for local-dev hygiene going forward.
- **`export_all_mtt.py:9-10`** — standalone offline utility script hardcodes a Firebase Storage bucket name and a specific user's Firebase UID for a personal export. Not part of the deployed app, not a secret, but should probably not ship in the same repo as the product code if it's a one-off personal tool — consider moving to a private scratch location.
- Two other tournament-type helper areas (`_TG_CFGS` room-name string matching in `static/app.js`) rely on substring matching similar in spirit to the MKO badge bug (2.5 covers the data-duplication angle; this is a maintainability note, not a new bug) — worth a broader look when 2.5 is addressed, since fixing the data source will likely simplify the matching logic too.

---

## 5. Specifically-requested checks — results

- **`.gitignore` excludes `serviceAccountKey.json`** — confirmed (`.gitignore` lines for `serviceAccountKey.json` and `*serviceAccount*.json`).
- **Git history has never contained it** — confirmed: `git log --all --full-history -- serviceAccountKey.json` returns no output (empty), across all branches/refs. `git ls-files | grep -i service` and `git ls-files | grep -i '.json$' | grep -i key` also return nothing — no credential-like JSON is tracked anywhere.
- **Every Firestore write uses `.update()` where it should** — see §3.3 full audit table. One pattern flagged (leak-target overlay, §2.3), everything else justified.
- **Blind level structures read only from `/config/blind_structure_base`, not duplicated per tournament (backend)** — confirmed correct; per-tournament docs store only `blind_structure_extra`/`blind_structure_override` deltas, composed at read time in `_resolve_tournament_cfg` (`app.py:864-927`). The duplication that does exist is frontend-only (§2.5), not backend.
- **MKO correctly treated as PKO (`is_pko: true`)** — was **not** correctly handled before this PR (§1.1); badge/stat display now fixed. `is_pko` itself remains an admin-set boolean not derived from `type` anywhere in the codebase — that's a pre-existing manual-entry design, unchanged here.
- **Six legacy `starting_time`-as-string docs (`crazy_2`, `deep_freeze`, `east_pko_sat`, `lucky_day`, `mini`, `texas`) — is the `.match()` crash still present?** **No — already fixed**, in commit `9641649` ("fix: handle starting_time stored as a list of daily start times"), well before this review. Current code (`templates/tournaments.html:445,453,480-483,488,495`) casts every value through `String()` before calling `.match()` and normalizes both string and array shapes via `startTimesArr()`, so none of the six legacy docs can trigger `timeStr.match is not a function` today. **Residual, non-crashing risk**: if any of the six stores multiple times as one comma-joined string (e.g. `"18:30, 23:30"`) rather than a real array, the display silently falls back to showing the raw un-converted string instead of crashing — cosmetic, not a blocker, but can't be fully ruled out without checking the live Firestore values for those six docs.
- **All secrets read from env vars, no hardcoded keys/URLs/tokens** — confirmed via targeted grep (`sk_live|sk_test|AIza|pk_live|pk_test|whsec_|mongodb+srv|postgres://|BEGIN PRIVATE KEY`) across the whole repo: no matches. One non-secret hardcoded URL was found and fixed (§1.3, the old-prod `APP_URL` fallback — not a credential, but an environment-identity leak).
- **`requirements.txt` pinned and installable clean** — installable clean: confirmed (`pip install -r requirements.txt` succeeds with no conflicts). **Not pinned** — see §1.4 for the recommended pin list and resolved versions captured during this review.

---

## Summary

- **Launch blockers:** 5 found, **3 fixed in this PR** (MKO badge, cross-user session leak, old-prod URL fallback), 2 flagged for a deliberate follow-up (dependency pinning, and the `/health` route was also added in this PR so effectively that one's closed too — 4 of 5 closed, 1 open: dependency pinning).
- **High:** 6 findings, all report-only (auth-trust gap on checkout, silent exception swallowing in auth helpers, one non-transactional write pattern, N+1-shaped import path, frontend blind-ladder duplication, coercion-without-range-checks).
- **Medium:** 6 findings, all report-only (logging gaps, unguarded routes documented as safe-by-default-handler, full `.set()` audit table, dev-server debug flag, placeholder TWA config, stale cache-name branding).
- **Low:** 4 notes, no action required before launch.
