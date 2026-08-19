---
name: deploy-to-prod
description: Ship outstanding ppptracker work to production and verify it's live. Trigger on "deploy to prod", and when draining Notion Tasks in the Reviewed status.
---

# Deploy to prod

The full check/merge/PR/deploy/verify routine for shipping ppptracker to
production. Runs unprompted end-to-end — the person invoking it should be able
to say only "deploy to prod" and get the same result every time.

Railway auto-deploys `main`, so "deploy" in practice means: get everything
outstanding merged to `main` with CI green, then confirm prod is actually
serving the new build (a merge is not proof of a deploy).

## When this fires

- Caio says "deploy to prod" (or equivalent) in a ppptracker session.
- Draining the Notion Tasks board's **Reviewed** bucket (see below) — that
  status is this skill's trigger, not just a coding-session request.

## Routine

1. **Survey for undeployed work.** Check local branches and open PRs
   (`gh pr list`) for anything not yet merged to `main`. Don't assume the
   current branch is the only outstanding work.
2. **Run CI locally first.** Compile check + test suite, matching
   `.github/workflows/ci.yml`:
   ```bash
   python -m py_compile app.py gamification.py hand_parser.py hand_exporter.py leak_engine.py leak_validation.py tournament_analyzer.py equity.py
   python test_hand_exporter.py
   python test_leak_cache.py
   python test_leak_targets.py
   python test_leaks_api.py
   python test_game_category.py
   python test_admin_users.py
   python test_pricing_refs.py
   python test_gamification.py
   python test_tiering.py
   ```
   Fix failures before opening/merging a PR — don't rely on CI alone to
   catch them.
3. **Commit and push** any outstanding work. Don't sweep in uncommitted
   changes sitting in other worktrees that weren't asked for — surface them
   and let Caio decide instead of silently bundling them into the deploy.
4. **Open a PR**, wait for CI to go green, then **squash-merge to `main`**.
5. **Firestore rules auto-deploy.** A GitHub Actions workflow
   (`.github/workflows/deploy-rules.yml`) fires on push to `main` when
   `firestore.rules`, `firebase.json`, or `.firebaserc` change, and runs
   `firebase-tools deploy --only firestore:rules` using the
   `FIREBASE_SERVICE_ACCOUNT_JSON` repo secret. If the PR touched any of
   those files, confirm it actually ran:
   ```bash
   gh run list --workflow=deploy-rules.yml --limit 1
   ```
   and check it shows `success`. No separate manual rules-deploy step is
   needed — the PR route ships code and rules together.
6. **Verify prod is serving the new build.** Fetch
   `https://ppptracker.up.railway.app` and confirm the bumped
   `app.js?v=N` / `style.css?v=N` query strings appear, and that the changed
   code is actually present in the served asset (a browser service worker
   caches these, so the version bump is what proves a real refresh — always
   bump those query strings in any template when touching `static/`).

## Draining the Notion Reviewed bucket

On the ppptracker Notion Tasks board, Caio moves a Task to **Reviewed** once
he's reviewed and approved its PR. That bucket is Claude Code's
responsibility to drain:

1. Pick each Task in `Reviewed`.
2. Run the routine above for its PR.
3. Move the Task to `Done` on success, or back to `In Review` (with a comment
   explaining what broke) on any deploy or verification failure.
4. Repeat until `Reviewed` is empty. Draining the whole queue in one run is
   the intended behavior — it's one bounded deploy job, not a second "primary
   task" pickup in the Operating Manual's guard-rail sense.

**Multiple Reviewed tasks from the same decomposed Feature.** When several
Reviewed tasks extend the same area (e.g. sibling sub-Tasks of one Feature),
each was reviewed as an independent PR but merging the first can invalidate
`mergeable` on the rest. For each subsequent PR:

```bash
gh api repos/handtrackerpppoker/ppptracker/pulls/N/update-branch -X PUT
gh pr view N --json mergeStateStatus   # poll until CLEAN
```

If `update-branch` 422s with a merge-conflict error, the PRs touch
overlapping lines and need a manual resolve:

```bash
git worktree list | grep <branch>   # find the branch's existing worktree
git merge origin/main
# resolve conflicts by keeping both branches' additions side-by-side —
# these are usually two independent additions to the same dict/table,
# not real logical conflicts
```

Re-run the test suite, push, wait for CI green, then squash-merge.

## Disposable backfill scripts

Some Tasks ship a one-shot backfill script (e.g. a Stripe or Firestore
backfill) that needs prod secrets (`STRIPE_SECRET_KEY`,
`FIREBASE_SERVICE_ACCOUNT_JSON`) this environment doesn't have. Running a
prod data-mutating script isn't in scope for "deploy code, verify it's live"
anyway. Correct handling:

- Merge and deploy the code as normal (the webhook/write path going forward
  works fine without the backfill).
- Leave the script in place, undeleted.
- Flag in the Notion completion comment that Caio needs to run it manually
  with his prod credentials, then delete it per the project's
  one-shot/disposable-script convention.
- Still mark the Task `Done` — the shipped code path is complete; only
  historical backfill for pre-existing data is pending, and that's a
  follow-up, not a reason to withhold Done.
