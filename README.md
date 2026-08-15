# PPPokerHA

PPPoker Hand Tracker — imports a PPPoker replay link, analyses the session, and
exports hands for PokerTracker / DriveHUD / GTO Wizard.

## Deploying

There is one path to production: **merge a PR into `main`.**

Pushing to `main` triggers both halves of a deploy:

- **Railway** builds and serves the app.
- **`.github/workflows/deploy-rules.yml`** publishes `firestore.rules`, but only
  when the rules (or the Firebase project config) actually changed.

Nothing else needs running by hand. `_push_and_deploy.bat` used to be a second,
parallel route that pushed straight to `main` and deployed the rules itself; it
is gone, because "which way did I ship it?" decided whether the security rules
went out, and that is not a question a deploy should ask.

Two things to remember when shipping:

- **Bump the asset cache busters** (`?v=N` on `style.css` / `app.js`) in every
  template whenever you touch `static/` — a service worker caches those files.
- **Verify prod actually serves the new build** rather than assuming the merge
  was enough: load the site and confirm the bumped `?v=N` appears.

To re-publish the rules without changing them — say someone edited them in the
Firebase console and you want the repo's version back — run the **Deploy
Firestore rules** workflow from the Actions tab.

If GitHub Actions itself is unavailable, the rules deploy is one command from a
checkout that has them (mind which branch you're on — the repo root usually sits
on `main`):

```bash
firebase deploy --only firestore:rules --project pppoker-analyser
```

### CI secrets

| Secret | Purpose |
| --- | --- |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Service account JSON used to publish `firestore.rules`. Needs the **Firebase Rules Admin** role on `pppoker-analyser`. |

## Running locally

```bash
pip install -r requirements.txt
python app.py
```

Environment variables go in a `.env` at the repo root, loaded automatically by
`python-dotenv` when present.

Tests are standalone scripts, run the same way CI does:

```bash
python test_gamification.py
```
