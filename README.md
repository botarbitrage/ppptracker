# PPPokerHA

PPPoker Hand Tracker — imports a PPPoker replay link, analyses the session, and
exports hands for PokerTracker / DriveHUD / GTO Wizard.

## Running locally

```bash
pip install -r requirements.txt
```

Put the environment variables below in a `.env` at the repo root (loaded
automatically by `python-dotenv` when present), then:

```bash
python app.py
```

Tests are standalone scripts, run the same way CI does:

```bash
python test_tiering.py
```

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
| `CPX_SECURE_HASH` | yes | CPX app secret. Verifies `POST /api/cpx/postback` (`md5(trans_id + secret)`) and derives the per-user `secure_hash`. Never sent to the browser. |
| `TALLY_SIGNING_SECRET` | no | Verifies `POST /api/tally/callback` (base64 HMAC-SHA256 of the raw body). Unset means no Tally submission is ever accepted. |
| `TALLY_FORM_URL` | no | The Tally form to embed when CPX has no eligible survey. Unset simply means no fallback is offered. |

Generate the two HMAC secrets with anything that produces 32+ random bytes:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Other

| Variable | Required | Purpose |
| --- | --- | --- |
| `APP_URL` | no | Origin used for Stripe success/cancel URLs when the request carries no `Origin`. |
| `PERMANENT_ADMIN_EMAILS` | no | Comma-separated emails that are always admin, so the admin page can't lock everyone out. Defaults to the project owner. |

## Provider setup

**CPX Research** — set the postback URL to `https://<host>/api/cpx/postback`. It
must carry `user_id`, `trans_id`, `hash`, `status` and `subid_1`; `subid_1` is
the unlock kind (`hand` or `tourney`) and is echoed back from the widget URL.

**Tally** — the form needs hidden fields named `uid` and `kind` (Tally populates
hidden fields from URL query params of the same name), and a webhook pointed at
`https://<host>/api/tally/callback` with signing enabled using
`TALLY_SIGNING_SECRET`.
