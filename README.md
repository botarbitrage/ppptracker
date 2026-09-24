# PPPoker Hand Tracker

**Study your PPPoker hands in PokerTracker, DriveHUD and GTO Wizard.**

👉 **Try it: [ppptracker.up.railway.app](https://ppptracker.up.railway.app)**

PPPoker is a great place to play, but it won't let you study. There's no
hand-history export, so the serious training tools (PokerTracker 4, DriveHUD,
GTO Wizard) have nothing to work with. You're left replaying hands one at a
time in a small mobile viewer.

PPPoker Hand Tracker fixes that. Paste the replay link PPPoker already gives
you, and within seconds you get your stats, your tournament history and a
hand-history file your training software can read.

---

## What you can do

- **Import in one step.** Paste a PPPoker hand-review link. Nothing to install, no files to upload.
- **Export to your training tools.** Hands are converted to the standard
  PokerStars format that PokerTracker 4, DriveHUD and GTO Wizard all accept.
  Download everything, a single hand, or a whole tournament as `.txt` or `.json`.
- **See your tournaments at a glance.** The Tournament Summary shows games
  entered, buy-ins, finishing positions, ITM%, average hands, duration and
  hands per hour. You can filter by today, this week or this month.
- **Replay a tournament through its stack graph.** Open any event and follow
  your stack in chips or big blinds. Click any point on the curve to jump to
  that hand.
- **Keep cash games separate.** Cash tables and sit-and-gos (including play
  money) have their own section, apart from your real-money tournaments.
- **Earn points, streaks and badges.** Every import earns points, and there's
  a new leaderboard to climb each week.
- **Use it in your language.** Available in English and Português (Brasil).
- **Get session reports by email (coming soon).** *PokerPulse* sends you a
  recap of your session with its highlights. It's being rolled out to a
  first group of players now.

## How to use it

1. **Open PPPoker** on your phone or PC and go to your profile → **Hand History**.
2. **Open any hand's replay** and tap **Share / Copy Link**. The link looks
   like `http://replay.pppoker.net/...?uid=...&rdkey=...`
3. **Paste the link** into the box at the top of the site and press **IMPORT**.
   Up to 200 of your most recent hands load, usually in 10–30 seconds.
4. **Review your stats**, then press **PokerTracker**, **DriveHUD** or
   **GTO Wizard** to download a file you can import straight into that tool.

> ⚠️ Replay links expire after a short time. If you see a "link expired"
> error, copy a new link from PPPoker.

## Free vs Pro

The tracker is free to use, with some daily limits. Pro removes the limits.

| | Free | Pro |
| --- | --- | --- |
| Imports | 1/day (up to 3/day with a 30s wait) | Unlimited |
| History kept | 7 days | Forever |
| Session stats, tournament details & graphs | ✓ | ✓ |
| Points, streaks & badges | ✓ | ✓ |
| Single-hand exports | 2/day (up to 5 after short ads) | Unlimited |
| Tournament exports | 1 free + 1/week (after ads) | Unlimited |
| Export a whole session | — | ✓ |

Imports and exports need a free account. Pro is usually **A$13.99/month**.
**Early Access** pricing of **A$7.99/month** is available until the global
launch. You can cancel any time. The in-app **Free vs Pro** page always has the
current limits and prices.

## Your account is safe

- The tracker **never asks for your PPPoker username or password**. It only
  uses the public replay link PPPoker creates for you.
- The only data it processes is the hand history inside that link.
- Conversion is **95%+ accurate** on the hands tested so far. Rare edge cases,
  such as unusual all-ins, incomplete hands or PPPoker-specific actions, may
  not convert perfectly. Use exports for study, not as official records.

## What's new and what's next

**Latest (v0.5, Aug 2026):** tournament stack graph, points, streaks and a
weekly leaderboard, and Pro with Early Access pricing. PokerPulse email
session reports are being rolled out now.

**Coming up:**
- 🔍 **Leak Radar:** find your biggest leaks across every hand you bring in, from any poker room.
- 🤖 **AI Poker Coach:** personalised feedback based on your own hand histories.
- 🧠 **Solver & training tools:** a beginner-friendly way to train ranges and decisions using your own hands.

The full **Release Notes** and **Roadmap** are in the app menu.

---
---

## Technical documentation

*Everything below is for people working on the codebase.*

- [Architecture at a glance](#architecture-at-a-glance)
- [Repositories and deploying](#repositories-and-deploying)
- [Running locally](#running-locally)
- [Tests](#tests)
- [PokerPulse (session reports)](#pokerpulse-session-reports)
- [Environment variables](#environment-variables)
- [Provider setup](#provider-setup)
- [Internationalization (i18n)](#internationalization-i18n)
- [Further docs](#further-docs)

## Architecture at a glance

A single Flask app (`app.py`) served by gunicorn (see `Procfile`) on
**Railway**. It uses **Firebase** for Auth, Firestore and Storage, **Stripe**
for Pro subscriptions, and **Brevo** for transactional email.

| Path | What it does |
| --- | --- |
| `app.py` | Routes, API, auth, tiering/gates, Stripe webhook, admin API, PokerPulse delivery |
| `hand_parser.py` | Fetches and parses PPPoker replay data into hands |
| `hand_exporter.py` | Converts hands to PokerStars-format hand histories |
| `tournament_analyzer.py` | Tournament summaries and per-tournament breakdowns |
| `session_engine.py` | Session analysis from individual hands (PokerPulse) |
| `highlights.py` | Highlight and pattern detection for session reports |
| `leak_engine.py`, `leak_validation.py`, `equity.py` | Leak Finder engine, validation and equity maths |
| `gamification.py` | Points, streaks, badges, leaderboard |
| `templates/` | Jinja pages (`index`, `tournaments`, `leaks`, `admin`) and `emails/` |
| `static/` | `app.js`, `style.css`, the service worker `sw.js`, and ad/badge/banner media |
| `firestore.rules` | Firestore security rules (deployed by CI) |
| `translations/` | Flask-Babel catalogs |

Main pages: `/` (tracker), `/tournaments`, `/leaks`, `/admin`.

## Repositories and deploying

The project uses **two GitHub repos and one deploy path**. The full story is
in [CLAUDE.md](CLAUDE.md).

| Repo | Role |
| --- | --- |
| `botarbitrage/ppptracker` | **Development.** Open and merge all PRs here. |
| `handtrackerpppoker/ppptracker` | **Deploy.** Railway auto-deploys its `main`. |

`.github/workflows/mirror-to-upstream.yml` fast-forwards
`botarbitrage/main` → `handtrackerpppoker/main` on every push. **Never open a
PR against `handtrackerpppoker/ppptracker`.** Its `main` is protected, and
committing there directly breaks the mirror.

To ship, **merge a PR into `botarbitrage/main`**. After that:

- **Railway** builds and serves the app once the mirror has pushed.
- **`.github/workflows/deploy-rules.yml`** publishes `firestore.rules`, but
  only when the rules (or the Firebase project config) changed.

Two things to remember when shipping:

- **Bump the asset cache busters** (`?v=N` on `style.css` / `app.js`) in every
  template whenever you touch `static/`. The service worker caches those files.
- **Check that prod actually serves the new build.** Load the site and confirm
  the bumped `?v=N` appears, or hover the Admin pill while signed in as admin
  to see which versions are being served.

To re-publish the rules without changing them (for example, after someone
edited them in the Firebase console), run the **Deploy Firestore rules**
workflow from the Actions tab. If GitHub Actions is unavailable:

```bash
firebase deploy --only firestore:rules --project pppoker-analyser
```

### CI secrets

| Secret | Purpose |
| --- | --- |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Service account JSON used to publish `firestore.rules`. Needs the **Firebase Rules Admin** role on `pppoker-analyser`. |
| `UPSTREAM_TOKEN` | Used by the mirror workflow to push to `handtrackerpppoker/main`. |

## Running locally

```bash
pip install -r requirements.txt
```

Put the [environment variables](#environment-variables) in a `.env` at the
repo root (`python-dotenv` loads it automatically), then:

```bash
python app.py
```

## Tests

Tests are standalone scripts. CI (`.github/workflows/ci.yml`) byte-compiles
the core modules and then runs:

```bash
python test_hand_exporter.py
python test_leak_cache.py
python test_leak_targets.py
python test_leaks_api.py
python test_game_category.py
python test_admin_users.py
python test_pricing_refs.py
python test_gamification.py
python test_tiering.py
python test_banners_config.py
python test_session_engine.py
python test_pokerpulse_admin.py
python test_highlights.py
```

[docs/tiering-manual-test.md](docs/tiering-manual-test.md) walks through
testing tiered access against a real Firestore.

## PokerPulse (session reports)

PokerPulse emails a player a recap of their session with its highlights. It
is currently admin-driven (MVP):

1. **Subscribe.** An admin ticks the PokerPulse checkbox in **Admin → Users**
   (`users/{uid}.pokerpulse_subscribed`; a missing field means unsubscribed).
2. **Analyse.** **Admin → Session Reports → Analyse Now** runs
   `session_engine.py` and `highlights.py` over the player's hands and stores
   the result at `users/{uid}/pokerpulse/last_analysis`.
3. **Preview.** The report is rendered with
   `templates/emails/pokerpulse_report.html`. Highlight art is hosted in
   Firebase Storage rather than embedded in the email.
4. **Send Now.** The report is delivered through **Brevo's HTTP API**. It uses
   HTTP instead of SMTP because Railway's network couldn't reach SMTP reliably.
   The recipient is always the account's current Firebase Auth email.

The schema is in [docs/firestore-schema.md](docs/firestore-schema.md).
`backfill_pokerpulse_subscription.py` is a one-shot script that writes the
explicit `False` flag onto existing users.

## Environment variables

Set these in Railway for the deployed app, and in `.env` locally.

### Firebase

| Variable | Required | Purpose |
| --- | --- | --- |
| `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, `FIREBASE_STORAGE_BUCKET`, `FIREBASE_MESSAGING_SENDER_ID`, `FIREBASE_APP_ID`, `FIREBASE_MEASUREMENT_ID` | yes | Publishable client config, served to the browser by `/api/firebase-config`. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | yes in prod | Admin SDK credentials as one JSON blob. Falls back to application-default credentials when unset. |

### Stripe

| Variable | Required | Purpose |
| --- | --- | --- |
| `STRIPE_SECRET_KEY` | yes | Server-side Stripe key. |
| `STRIPE_PRICE_ID`, `STRIPE_PRO_PRICE_ID`, `STRIPE_PROTEST_PRICE_ID` | yes | Subscription prices per plan. |
| `STRIPE_WEBHOOK_SECRET` | yes | Verifies `/api/stripe-webhook`, which is what flips `users/{uid}.is_pro`. |
| `STRIPE_EARLY_ACCESS_PRICE_LABEL`, `STRIPE_PRO_PRICE_LABEL` | no | Display copy for the pricing CTAs. |

### Tiered access

Added by the anon/free/pro tiering work. See
[docs/firestore-schema.md](docs/firestore-schema.md) for what each one guards.

| Variable | Required | Purpose |
| --- | --- | --- |
| `AD_TOKEN_SECRET` | yes | HMAC key for the single-use export unlock in the `X-Ad-Token` header. Without it `POST /api/ad-token` answers 503 and no token ever verifies, so gated exports fall back to spending a credit directly. |
| `ANON_SESSION_SECRET` | yes | HMAC key for the claim ticket a signed-out import returns. Without it signed-out imports still analyse, but cannot be claimed after signing in. |
| `CPX_APP_ID` | yes | CPX Research app id, sent to the browser so the survey widget can load. |
| `CPX_SECURE_HASH` | yes | CPX app secret. Verifies `POST /api/cpx/postback` (`md5(trans_id + "-" + secret)`) and derives the per-user `secure_hash`. Never sent to the browser. |
| `TALLY_SIGNING_SECRET` | no | Verifies `POST /api/tally/callback` (base64 HMAC-SHA256 of the raw body). Unset means no Tally submission is ever accepted. |
| `TALLY_FORM_URL` | no | The Tally form to embed when CPX has no eligible survey. Unset simply means no fallback is offered. |
| `GATE_STUB_MODAL_ENABLED` | no | Self-hosted "watch to unlock" modal that stands in for a real rewarded-video ad while ad-network approvals are pending (see `_showGateStubModal` in `static/app.js` and `POST /api/gate/stub-completion`). Defaults **on**. Set to `0`/`false`/`no`/`off` to disable. |

Generate the two HMAC secrets with anything that produces 32+ random bytes:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### PokerPulse

| Variable | Required | Purpose |
| --- | --- | --- |
| `POKERPULSE_BREVO_API_KEY` | no | Brevo API key for sending PokerPulse reports. When unset, **Send Now** logs a skip and sends nothing. |

### Other

| Variable | Required | Purpose |
| --- | --- | --- |
| `APP_URL` | no | Origin used for Stripe success/cancel URLs when the request carries no `Origin`. |
| `PERMANENT_ADMIN_EMAILS` | no | Comma-separated emails that are always admin, so the admin page can't lock everyone out. Defaults to the project owner. |
| `FLASK_DEBUG` | no | Set to `1` to enable Flask debug mode when running `python app.py` locally. |

## Provider setup

**CPX Research.** Set the postback URL to `https://<host>/api/cpx/postback`. It
must carry `user_id`, `trans_id`, `hash`, `status` and `subid_1`. `subid_1` is
the unlock kind (`hand` or `tourney`) and is echoed back from the widget URL.

**Tally.** The form needs hidden fields named `uid` and `kind`, which Tally
fills from URL query params of the same name. Point a webhook at
`https://<host>/api/tally/callback` with signing enabled, using
`TALLY_SIGNING_SECRET`.

## Internationalization (i18n)

The app uses [Flask-Babel](https://python-babel.github.io/flask-babel/) for
translated strings. Supported locales are listed in `app.config['LANGUAGES']`
in `app.py` (currently `en`, `pt_BR`).

### Adding a new translated string

In Jinja templates, wrap the string in `_('...')`, or use `{% trans %}...{%
endtrans %}` for multi-line or block text:

```jinja
<span>{{ _('Points') }}</span>
```

In `app.py` (Python-side strings such as flash messages), use the same
`_('...')` call, which `flask_babel` provides. `babel.cfg` lists which files
get scanned (`app.py` and everything under `templates/`). A string outside
those files won't be picked up by extraction.

### How locale is chosen

`get_locale()` in `app.py` resolves the active locale in this order:

1. An explicit `lang` cookie, set by the language `<select>` in the header
   (see `static/app.js`). A user's manual choice always wins.
2. The browser's `Accept-Language` header, best-matched against
   `app.config['LANGUAGES']`.
3. `en` as the final fallback.

Detection is intentionally **Accept-Language only**. No GeoIP or other paid
geolocation service is used anywhere in the flow.

### Adding a new locale

1. Add the locale code to `app.config['LANGUAGES']` in `app.py`.
2. Add an `<option>` for it to the `#lang-select` dropdown in
   `templates/index.html` and any other page with the selector.
3. Generate a catalog for it:
   ```bash
   pybabel init -i translations/messages.pot -d translations -l <locale_code>
   ```
4. Fill in the `msgstr` entries in
   `translations/<locale_code>/LC_MESSAGES/messages.po`, then compile.

### Compiling translations

After editing translatable strings or `.po` files, re-extract and rebuild the
compiled catalog:

```bash
pybabel extract -F babel.cfg -o translations/messages.pot .
pybabel update -i translations/messages.pot -d translations
# fill in any new/blank msgstr entries in translations/<locale>/LC_MESSAGES/messages.po
pybabel compile -d translations
```

`pybabel compile` must run before deploying. The app reads the compiled `.mo`
file, not the `.po` source.

### Poker taxonomy glossary (pt_BR)

The following terms are intentionally left **untranslated** in `pt_BR`.
Brazilian players use these English terms natively, matching how PPPoker and
other training tools present them. The authoritative list is in the header
comment of `translations/pt_BR/LC_MESSAGES/messages.po`; keep the two in sync.

- Street names: Flop, Turn, River, Street
- Stack/format terms: Stack, BB / BBs, MTT, Satellite
- Hole-card jargon: hero, board, runout, all-in
- Stats: VPIP, PFR
- Tournament structure jargon: Showdown, Rebuy, Add-on

## Further docs

- [docs/firestore-schema.md](docs/firestore-schema.md): Firestore collections, tiering counters and PokerPulse fields.
- [docs/leak-finder-design.md](docs/leak-finder-design.md): Leak Finder design and delivery plan.
- [docs/pppoker-action-model.md](docs/pppoker-action-model.md): the canonical meaning of PPPoker action codes.
- [docs/tiering-manual-test.md](docs/tiering-manual-test.md): testing tiered access against a real Firestore.
- [docs/account_migration.md](docs/account_migration.md): runbook for the GitHub/Railway/Firebase account migration.
- [docs/launch_review.md](docs/launch_review.md): launch readiness review.
