# Repository topology — read this before opening a PR

This project spans **two GitHub repos with one deploy path**. Get this wrong
and either your change never reaches production, or you silently break the
pipeline for everyone after you.

## The two repos

| Repo | Role |
|---|---|
| `botarbitrage/ppptracker` | **Development repo.** Open PRs and merge here. |
| `handtrackerpppoker/ppptracker` | **Deploy repo.** Railway auto-deploys its `main` (`docs/account_migration.md`). Kept under a separate account so the running app/system stays independent of `botarbitrage`. |

`.github/workflows/mirror-to-upstream.yml` fast-forward-pushes
`botarbitrage/main` → `handtrackerpppoker/main` on every push, using the
`UPSTREAM_TOKEN` secret. That is the **only** intended write path into the
deploy repo's `main`.

## The rule

**Always develop and open PRs against `botarbitrage/ppptracker`. Never open
a PR against `handtrackerpppoker/ppptracker`.**

`handtrackerpppoker/main` is protected (push restricted to the mirror's
token identity) specifically so a PR merged there directly is rejected
outright, rather than silently accepted. If you're looking at this file
because a push or merge to `handtrackerpppoker/main` was refused — that's
the protection working as intended. Redo the work as a PR against
`botarbitrage/ppptracker` instead.

## Why this matters

The mirror is fast-forward-only by design — a straightforward, auditable
sync. It breaks the moment a commit lands in `handtrackerpppoker/main` that
`botarbitrage/main` doesn't have as an ancestor: the push is rejected with
`! [rejected] HEAD -> main (fetch first)`, and **nothing merged into
`botarbitrage` can reach production** until someone manually reconciles the
two histories.

That already happened once (2026-09-01): five PRs (#36, #37, #40, #41, #42)
were merged directly into `handtrackerpppoker/main`, diverging it from
`botarbitrage/main` and breaking the mirror for days before anyone noticed —
during which an open PR fixing a real production bug (header misalignment)
sat unable to ship. Recovering required merging the two histories back
together by hand. The branch protection above exists to make that class of
mistake impossible rather than merely recoverable.

## If you need to check what's actually live

Don't guess from `botarbitrage/main` — the mirror can lag or (historically)
break. Compare `botarbitrage/main` against `handtrackerpppoker/main`
directly, or use the deploy-version hint on the Admin pill in the running
app (hover it while signed in as admin — added in #3, shows the `app.js` /
`style.css` cache-bust versions actually being served).
