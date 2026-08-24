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

## `users/{uid}/gate_events/{completion_id}` — server-only

One document per gate completion, across every gate mechanism (this doc
currently covers only the "watch to unlock" stub — see below). Written with
`create()`, keyed by a client-generated `completion_id`, so a double-clicked
OK button or a retried request is recognised as a duplicate and does not
double-grant.

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | string | `import` or `hand_export` — which gate the user hit. |
| `gated` | bool | Always `true` for a stub completion — present so this shape matches the multi-provider event history a parallel task is standardising (`_record_gate_event`). |
| `gate_provider` | string | `stub` for this modal. Reserved values for the real ad-network integration (`ayet`, `wannads`) belong to a follow-up Feature, not this one. |
| `at` | int (epoch secs) | When the completion was recorded. |
| `gate_completion_id` | string | Mirrors the document id. |

**Known gap (documented, not fixed here):** this endpoint (`POST
/api/gate/stub-completion`) does not verify that the client's 30s countdown
actually elapsed — it trusts the browser. A technical user who calls the
endpoint directly, or edits `_GATE_STUB_SECONDS` in devtools, still gets a
completion recorded. Acceptable for an MVP stub with no ad revenue on the
line; will need revisiting if/when this record starts being consumed to
actually grant something (see "Task 6", not yet built).

**Reconciliation note:** this collection and its four written fields
(`kind`, `gated`, `gate_provider`, `at`, `gate_completion_id`) are being
defined in parallel by another in-flight change that adds a shared
`_record_gate_event(uid, kind, gated, provider, completion_id)` helper on a
different branch. The write in `app.py`'s `gate_stub_completion()` was kept
hand-rolled, matching this exact shape, specifically so it becomes a drop-in
call to that helper once the branches merge — expect a small merge
reconciliation, not a schema change.

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
2. **`ad_jtis`, `survey_completions` and `gate_events` are excluded from the
   blanket subcollection grant**, not merely re-matched with a stricter rule.
   Rule matches are OR'd, so a permissive parent rule would outvote a strict
   child one, and the account that benefits from deleting a spent-unlock
   record is exactly the account that must not be able to.

All three subcollections stay owner-**readable**, so a player can audit their
own unlocks, payouts and gate completions.
