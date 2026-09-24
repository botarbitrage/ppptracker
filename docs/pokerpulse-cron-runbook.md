# PokerPulse Scheduled Sends — Cron Runbook (F2-1)

Sets up the Railway cron service that drives `POST /api/cron/pokerpulse`
every 15 minutes, plus the Cloud Storage lifecycle rule that expires
highlight-art SVGs after 30 days. Both are one-off setup steps — the app
code (`app.py`'s `cron_pokerpulse()`, `pokerpulse_scheduler.py`) is already
deployed once this PR merges; nothing here needs a redeploy to change later.

**No secret values appear in this document** — only variable names, commands,
and click-paths, same convention as `docs/account_migration.md`.

---

## 1. Generate and set `POKERPULSE_CRON_SECRET`

The cron endpoint authenticates via a shared secret in the `X-Cron-Secret`
header (`hmac.compare_digest`-checked against this env var — see
`cron_pokerpulse()` in `app.py`). It returns `503` if the env var is unset
and `403` on a mismatch, so the service is safe to create before the secret
exists, but sends will never fire until it's set on **both** the main app
service and the cron service below.

```bash
# Any sufficiently random value — 32+ bytes of urlsafe base64 is plenty.
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set it on the main `ppptracker` app service (Railway dashboard → Variables,
or `railway variables --set` in an authenticated shell) as
`POKERPULSE_CRON_SECRET`. It must match exactly on the cron service created
in step 2.

## 2. Create the Railway cron service

Railway cron services are billed only while they run; 96 very short `curl`
invocations a day (one per 15-minute tick) fits well inside the Hobby
plan's included usage.

In the Railway project dashboard:

1. **New → Empty Service**, name it `pokerpulse-cron`.
2. **Settings → Cron Schedule**: `*/15 * * * *` (every 15 minutes, UTC —
   the endpoint itself does all the Adelaide-time logic; the cron schedule
   only controls how often it's *checked*, not when it fires).
3. **Settings → Deploy → Custom Start Command**, pointed at the main app's
   public URL:
   ```bash
   curl -sf -X POST "$POKERPULSE_APP_URL/api/cron/pokerpulse" \
     -H "X-Cron-Secret: $POKERPULSE_CRON_SECRET"
   ```
   Use the app's actual public Railway domain for `POKERPULSE_APP_URL`
   (e.g. `https://ppptracker.up.railway.app`) — `_persist_highlight_art()`
   builds highlight-art URLs from `request.url_root`, so the cron tick must
   hit the public URL, not an internal/private one, or the art links it
   writes into `last_analysis` would be unreachable from a recipient's
   inbox.
4. Set `POKERPULSE_CRON_SECRET` (same value as step 1) and
   `POKERPULSE_APP_URL` as variables on this cron service. No other env
   vars are needed here — this service does nothing but make one HTTP call.
5. A minimal image is enough to run `curl`; Railway's default Docker image
   already has it, so no Dockerfile is required unless the project's
   default builder doesn't include `curl` (check the deploy logs on first
   run).

## 3. Keep it DISABLED until verified

Per the F2-1 Acceptance Criteria: the service (and/or `config/pokerpulse`'s
`enabled` flag — see Admin → Session Reports → Scheduled sends) stays
**disabled** until:

1. **One dry run** — Admin → Session Reports → Scheduled sends → **Dry
   run**. Confirms classification (daily/weekly) and windows look right for
   the current subscriber list, without writing or sending anything.
2. **One real run to Caio's own account** — flip `enabled` on with
   `send_time` set a few minutes in the future (Adelaide time), confirm the
   cron service's next tick actually sends and the email/highlight art
   render correctly, then decide whether to leave it enabled for every
   subscriber.

Toggling `enabled` is instant (no redeploy) — it's `config/pokerpulse` in
Firestore, read fresh on every cron tick via `_pokerpulse_settings()`.

## 4. Cloud Storage lifecycle rule — 30-day highlight-art expiry

Highlight-art SVGs are now keyed per report window
(`pokerpulse_highlights/{uid}/{window_end}/{i}.svg` — see F2-1's Context on
the Task page), so they accumulate rather than being overwritten. This rule
caps storage at roughly `subscribers × highlights × 30 days` of tiny SVGs,
negligible in cost. It's a one-off console/`gsutil` step — not something
`app.py` can apply itself.

**Console:** Cloud Storage → the `FIREBASE_STORAGE_BUCKET` bucket →
Lifecycle → Add rule:
- **Action:** Delete
- **Conditions:** Age = 30 days, **Object name prefix** =
  `pokerpulse_highlights/`

**Or via `gsutil`:**
```bash
cat > /tmp/pokerpulse-lifecycle.json <<'EOF'
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 30, "matchesPrefix": ["pokerpulse_highlights/"]}
    }
  ]
}
EOF
gsutil lifecycle set /tmp/pokerpulse-lifecycle.json gs://<FIREBASE_STORAGE_BUCKET>
```

Verify with `gsutil lifecycle get gs://<FIREBASE_STORAGE_BUCKET>` — it
should print the rule back. This only ever deletes objects under the
`pokerpulse_highlights/` prefix; it doesn't touch `ad_media/` or any other
bucket content.

## 5. Sanity checks after go-live

- Admin → Session Reports → Scheduled sends → **Run log** shows the last 7
  cutoff dates with each subscriber's bucket, hand count, window, and
  outcome (`sent` / `skipped: <reason>` / `error: <reason>`) — confirms
  ticks are actually landing and idempotency is working (no duplicate
  `sent` rows for the same date+uid).
- A missed tick (deploy, transient Firestore error) is picked up by the
  next one automatically — no manual intervention needed unless the **Run
  log** shows the same subscriber stuck at `claimed` with no `done_at`
  across several ticks, which would mean a tick crashed mid-processing;
  check the app's server logs for `[cron_pokerpulse]` lines.
