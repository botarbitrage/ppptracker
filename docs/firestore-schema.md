# Firestore schema

Firestore is the source of truth for tiering. Every counter that decides what a
player may do lives here rather than in process memory, because gunicorn runs
several workers and a player's requests land on whichever one is free.

This document covers the documents the **tiered access** feature reads and
writes. Collections that predate it (`tournaments`, `config`, `gamification`,
`leaderboards`) are described only where tiering touches them.

Writers are named per field. Anything marked **server-only** is written through
the Admin SDK in `app.py` and is blocked for clients in `firestore.rules` — see
[Security rules](#security-rules).

---

## `users/{uid}`

One document per signed-in account.

| Field | Type | Written by | Notes |
| --- | --- | --- | --- |
| `uid` | string | client | Mirrors the document id. |
| `email` | string | client | Captured on sign-in; also what makes exports a login-gated action. |
| `first_seen` | timestamp | client | Server timestamp, set once on creation. |
| `last_seen` | timestamp | client | Server timestamp, refreshed on each sign-in. |
| `is_pro` | bool | **server-only** (Stripe webhook) | The whole Free/Pro split. `true` removes every quota, the history window and the survey gate. |
| `stripe_customer_id` | string | **server-only** (Stripe webhook) | Fallback lookup key when a subscription event carries no uid metadata. |
| `subscription_status` | string | **server-only** (Stripe webhook) | Stripe's raw subscription status (`active`, `past_due`, `canceled`, `unpaid`, …), written by `customer.subscription.updated`/`.deleted`. Observability only — `is_pro` is still the field that gates access; this just makes the expiration-demotion logic visible instead of trusted blindly. Unset for users who never had a Stripe subscription. |
| `last_payment_at` | int (epoch secs) | **server-only** (Stripe webhook) | Stamped on `checkout.session.completed` and `invoice.payment_succeeded` — the two events that represent an actual payment. Left untouched by `customer.subscription.updated` (status sync, not necessarily a new payment). Unset for users who have never paid (`app.py:stripe_webhook`). |
| `quota` | map | **server-only** | Today's usage counters. See below. |
| `credits` | map | **server-only** | Unspent survey unlocks. See below. |

### `quota`

Lazily created on the first import or export of the day. The whole map is
rewritten by `_bump_quota()` inside a transaction, which rolls the day over
first, so a stale `day` reads as zero without needing a nightly job.

| Key | Type | Notes |
| --- | --- | --- |
| `day` | string | UTC date, `YYYY-MM-DD`. A different value means every counter below is stale and reads as 0. |
| `imports` | int | Successful imports today. Free cap: **3/day** (`FREE_IMPORTS_PER_DAY`). Claiming a signed-out import counts as one. |
| `hand_exports` | int | Single-hand exports today. Free cap: **5/day** (`FREE_HAND_EXPORTS_PER_DAY`); the first **2** (`FREE_HAND_EXPORTS_UNGATED`) need no survey. |
| `tourney_exports` | int | Per-tournament exports today. Free cap: **1/day** (`FREE_TOURNEY_EXPORTS_DAY`), and every one of them needs a survey unlock. |

Pro accounts are never counted — no quota key is written for them at all.

### `credits`

Single-use export unlocks earned by completing a survey. Deliberately **not**
reset daily; capped instead, so a week of surveys can't be stockpiled and dumped.

| Key | Type | Cap | Notes |
| --- | --- | --- | --- |
| `survey_credit_hand` | int | 3 | Unlocks one gated single-hand export. |
| `survey_credit_tourney` | int | 1 | Unlocks one per-tournament export. |

Granted by `_grant_credit()` from a provider callback; spent by
`_consume_credit()` when the export succeeds, or when traded for an ad token via
`POST /api/ad-token`. A reversal (CPX `status=2`) takes the credit back only
while it is still unspent.

---

## `users/{uid}/quota/tourney_export` — server-only

A single document, keyed by a fixed id (`tourney_export`), holding the
tourney-export gating state. This is **separate from** the `quota` map field
on `users/{uid}` above — that map is today's daily hard/soft quota shape
(`imports`, `hand_exports`, `tourney_exports`) and it does not fit tourney
export's new rule, which is **lifetime, not daily**: 1 free export ever, then
1 per ISO week thereafter. Rather than force a lifetime+weekly rule into a
daily-reset shape, it gets its own doc.

| Field | Type | Notes |
| --- | --- | --- |
| `lifetime_free_used` | bool | Whether the one free-forever tourney export has been spent. Missing/absent reads as `False` — see backfill note below. |
| `lifetime_free_used_at` | timestamp \| null | When the lifetime freebie was spent. `null`/absent until then. |
| `current_week_iso` | string | ISO 8601 week of the last write, `'YYYY-Www'` (e.g. `'2026-W34'`), from Python's `datetime.isocalendar()` — **always resolved server-side**, never trusted from the client. |
| `current_week_used` | int | Exports counted against `current_week_iso`. A stored week that isn't the current one reads as `0` on the next read, the same lazy-rollover pattern `quota.day` uses — no nightly job needed. |
| `last_reset_at` | timestamp | When the counter was last written (bump or rollover). |

Read by `_tourney_export_state(uid)` (lazy, matches stored data against the
server-computed current week, never writes). Written by
`_bump_tourney_export_usage(uid)`, which spends the lifetime freebie first
and only starts incrementing `current_week_used` once `lifetime_free_used` is
`True`. Both are plain helpers as of this writing — no route calls
`_bump_tourney_export_usage` yet; the tourney-export endpoint still enforces
the old daily `quota.tourney_exports` limit until a later task rewires it to
this doc.

**Backfill:** existing users have no `quota/tourney_export` doc at all.
`_tourney_export_state` treats a missing doc as `lifetime_free_used = False`
— i.e. every existing user gets one fresh free lifetime export the first time
the new model reads their state, rather than trying to infer "have they
already benefited from a free tourney export" from the old daily-counter
history (which can't actually answer that question — the old model gated
*every* tourney export behind a survey, so "have they exported before" says
nothing about whether they should get today's specific *lifetime freebie*).
`backfill_tourney_export_state.py` (one-shot, disposable — see the file
header) makes this explicit by writing `lifetime_free_used: False` onto every
user doc that doesn't already have the subdocument, so the state is visible
in Firestore immediately rather than only appearing lazily on first read.

---

## `users/{uid}/gate_events` — server-only

Append-only history of every gate check across the three actions that can
require an unlock: tourney export, hand export, and import. One document per
event, auto-generated id, never updated or deleted after being written — this
is what makes later reporting/audit possible without replaying quota state.
Shared shape by design: hand-export and import gates get history for free by
writing into the same subcollection instead of each inventing their own log.

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | string | `'tourney_export'` \| `'hand_export'` \| `'import'`. |
| `gated` | bool | `True` when the action actually required an unlock to proceed; `False` when it went through free (inside a free allowance). |
| `gate_provider` | string \| null | Free-form, **not a fixed enum** — who granted the unlock. Values in use as of this writing: `'stub'` (the watch-to-unlock modal) and `'cpx'` (CPX Research survey). A future rewarded-video SDK adds `'ayet'` and/or `'wannads'` without any schema change here. `null` when `gated` is `False`, or when no fresh provider event applies (e.g. a previously-banked credit). |
| `at` | timestamp | When the event was recorded. |
| `gate_completion_id` | string \| null | The provider's own transaction/response id when it has one (e.g. CPX's `trans_id`), else `null`. |

Written by the single shared helper `_record_gate_event(uid, kind, gated,
provider, completion_id)` — best-effort, failures are logged and swallowed
rather than blocking the export/import they describe. Not called from any
route yet; wiring it into the tourney-export, hand-export and import gate
paths is a later task. Owner-readable like `ad_jtis` and
`survey_completions`, for the same audit reason.

---

## `users/{uid}/tournaments/{tourney_id}`

One document per tournament the player has imported; the hands themselves live
in Cloud Storage at `storage_path`. Written by `_merge_tournament()`.

Tiering reads one field from it:

| Field | Type | Notes |
| --- | --- | --- |
| `earliest_ts` | int (epoch secs) | The history window compares against this. Free accounts see only `earliest_ts >= now - 7 days` (`FREE_HISTORY_DAYS`); a document with no `earliest_ts` is always shown, because "undated" is not evidence of "old". |

Nothing is ever deleted for being outside the window — the filter is applied on
read, so upgrading restores the full history immediately.

---

## `users/{uid}/ad_jtis/{jti}` — server-only

One document per **spent** export unlock. Written with `create()`, which is
atomic: a replayed `X-Ad-Token` loses the race and is refused.

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | string | `hand` or `tourney` — the endpoint class the token was scoped to. |
| `exp` | int (epoch secs) | The token's own expiry, 5 minutes after issue. |
| `used_at` | int (epoch secs) | When it was redeemed. |

The document id is the token's `jti` (a uuid4 hex). Its existence *is* the
"already spent" record, which is why clients cannot delete it.

---

## `users/{uid}/survey_completions/{completion_id}` — server-only

One document per survey payout, keyed by the provider's own transaction id
(`trans_id` for CPX, `responseId` for Tally). Written with `create()`, so a
redelivered webhook is recognised as a duplicate and pays once.

| Field | Type | Notes |
| --- | --- | --- |
| `source` | string | `cpx` or `tally`. |
| `kind` | string | `hand` or `tourney` — which credit was granted. |
| `status` | string | CPX status as delivered: `1` complete, `2` reversal. Tally submissions are recorded as `1`. |
| `at` | int (epoch secs) | When we processed it. |
| `credit_granted` | bool | False when the grant was refused because the credit was already at its cap. |
| `credit_reversed` | bool | Set by a CPX reversal that successfully clawed the credit back. |
| `reversed_at` | int (epoch secs) | When that happened. |
| `trans_id` / `response_id` | string | Provider id, mirroring the document id. |
| `amount_local`, `amount_usd`, `offer_id`, `subid_1` | string | CPX payload, kept as delivered for revenue reconciliation. |
| `form_id` | string | Tally form the submission came from. |

---

## Storage: `anon_sessions/{token}.json`

Not Firestore, but part of the same flow. An import made while signed out is
analysed and parked here for **1 hour** instead of being persisted to any
account. The browser holds only an HMAC-signed token; possession of that token is
the entire authorisation to claim it.

```json
{ "player_uid": "<pppoker uid>", "records": [ /* raw hand records */ ] }
```

Blob metadata carries `created_at` (epoch secs). Expired objects are swept
best-effort on each new anonymous import (up to 100 per pass), and the blob is
deleted outright once claimed.

---

## Security rules

`firestore.rules` enforces the "server-only" column above. Two properties matter:

1. **`users/{uid}` update** may not touch `is_pro`, `stripe_customer_id`,
   `subscription_status`, `quota` or `credits`; **create** may not seed them
   either, so a delete-and-recreate cannot wash away a spent allowance.
2. **`ad_jtis`, `survey_completions`, `quota` and `gate_events` are excluded
   from the blanket subcollection grant**, not merely re-matched with a
   stricter rule. Rule matches are OR'd, so a permissive parent rule would
   outvote a strict child one, and the account that benefits from deleting a
   spent-unlock record (or its own gate-event history) is exactly the account
   that must not be able to.

All four subcollections stay owner-**readable**, so a player can audit their
own unlocks, payouts, and tourney-export/gate history.
