# Testing tiered access locally

`test_tiering.py` covers the rules against a fake Firestore. This walks the same
path against a real one, which is the only way to see the transactions, the
security rules and the CPX postback actually behave.

## Findings from 2026-08-15 prod test — all fixed in this PR

1. **Anon banner + Hand pill zeroed out.** `showImportSuccess` / `renderHandStats`
   now read `data.stats` (current-import scope), and the banner has three copy
   variants for anon / signed-in-new / signed-in-re-import.
2. **Export All wall.** Signed-out users no longer see the "Pro only" upsell —
   they get a "Sign in to export" wall on both the top section and the player
   badge. Signed-in free still gets the Pro upsell.
3. **Hand-card blocked the Stage line.** Added a 50px vertical dead-zone: folds
   only surface the card when the cursor is near the curve; snapped played hands
   always win.
4. **Hand-card layout.** Smaller footprint, hole cards centred, `#hand · L#`
   moved to a top-right pill.
5. **Tournament header.** Hands count + `K-max` render as pills; the duplicated
   tournament name is gone. `_tournament_detail` meta now surfaces `max_players`
   for the anon path.
6. **Tier-compare card.** Moved from `FREE_ONLY_ELS` to a new `ANON_ONLY_ELS`
   list so signed-in free users don't see the sign-in pitch again.

## Setup

```bash
python -c "import secrets; print('AD_TOKEN_SECRET=' + secrets.token_urlsafe(32)); print('ANON_SESSION_SECRET=' + secrets.token_urlsafe(32)); print('CPX_SECURE_HASH=' + secrets.token_urlsafe(24))"
```

Put those in `.env` alongside the Firebase and Stripe values (see the README),
add `CPX_APP_ID=test-app`, then start the server:

```bash
python app.py
```

Two things you need by hand:

- **A fresh replay link.** Open PPPoker → Hand History → copy the share link.
  They expire quickly, so grab it just before you start.
- **An ID token** for the signed-in steps. Sign in at http://localhost:5000, open
  the browser console and run `await firebase.auth().currentUser.getIdToken()`.
  It is good for an hour.

```bash
export BASE=http://localhost:5000
export URL='<paste the PPPoker replay link>'
export TOKEN='<paste the Firebase ID token>'
export CPX_SECURE_HASH='<the value from your .env>'
export UID=$(python -c "import base64,json,os,sys; p=os.environ['TOKEN'].split('.')[1]; print(json.loads(base64.urlsafe_b64decode(p + '='*(-len(p)%4)))['user_id'])")
```

## 1. Signed-out import returns a claim ticket and graph data

```bash
curl -s -X POST $BASE/api/analyze -H 'Content-Type: application/json' \
  -d "{\"url\":\"$URL\"}" \
  | python -c "import json,sys; d=json.load(sys.stdin); print('saved:', d['saved']); print('tournaments:', len(d['tournaments'])); print('graphs:', len(d.get('tournament_graphs', []))); print('token:', d['session_token'][:24], '...'); open('/tmp/session_token','w').write(d['session_token'])"
```

Expect `saved: False`, one graph per tournament, and a token. Nothing has been
written to any account yet.

## 2. Every export refuses a signed-out caller

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/api/export/pokerstars \
  -H 'Content-Type: application/json' -d '{"tourney_ids":["anything"]}'
```

Expect `401`.

## 3. Signing in claims the pending import

```bash
curl -s -X POST $BASE/api/analyze/claim \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"session_token\":\"$(cat /tmp/session_token)\"}" \
  | python -c "import json,sys; d=json.load(sys.stdin); print('claimed:', d.get('claimed')); print('saved:', d['saved']); print('expired by 7d cap:', d['history_expired_tournaments'])"
```

Expect `claimed: True`. Check `users/{uid}.quota.imports` in the Firebase console
— it should now be `1`. Replaying the same call answers `404 session_expired`.

## 4. The import cap

The claim used one of three. Two more imports work; the fourth does not:

```bash
for i in 2 3 4; do
  printf '%s: ' "import $i"
  curl -s -o /tmp/out -w '%{http_code} ' -X POST $BASE/api/analyze \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"url\":\"$URL\"}"
  python -c "import json; d=json.load(open('/tmp/out')); print(d.get('error',''), d.get('kind',''))"
done
```

Expect `200`, `200`, then `402 quota_exceeded import`.

## 5. A tournament export asks for a survey

```bash
export TID=$(curl -s $BASE/api/tournaments -H "Authorization: Bearer $TOKEN" \
  | python -c "import json,sys; print(json.load(sys.stdin)['tournaments'][0]['tourney_id'])")

curl -s -X POST $BASE/api/tournaments/$TID/export \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"platform":"PokerTracker"}'
```

Expect `{"error":"survey_required","kind":"tourney", ...}` with status 402.

## 6. Mock the CPX postback to grant the credit

```bash
export TRANS=test-$(date +%s)
export HASH=$(python -c "import hashlib,os; print(hashlib.md5((os.environ['TRANS']+os.environ['CPX_SECURE_HASH']).encode()).hexdigest())")

# A wrong hash must be refused outright:
curl -s -o /dev/null -w 'bad hash: %{http_code}\n' \
  "$BASE/api/cpx/postback?user_id=$UID&trans_id=$TRANS&subid_1=tourney&status=1&hash=nope"

# The real one answers a literal 1:
curl -s -w '\n' "$BASE/api/cpx/postback?user_id=$UID&trans_id=$TRANS&subid_1=tourney&status=1&hash=$HASH&amount_usd=0.35&offer_id=42"

# Redelivery must not pay twice:
curl -s -w '\n' "$BASE/api/cpx/postback?user_id=$UID&trans_id=$TRANS&subid_1=tourney&status=1&hash=$HASH"

curl -s $BASE/api/credits -H "Authorization: Bearer $TOKEN"
```

Expect `403`, then `1`, then `1`, and a credit balance of exactly
`{"hand": 0, "tourney": 1}`.

## 7. The export now succeeds, and the second one is capped

```bash
curl -s -o /tmp/export.txt -w 'export 1: %{http_code}\n' -X POST $BASE/api/tournaments/$TID/export \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"platform":"PokerTracker"}'
head -3 /tmp/export.txt

curl -s $BASE/api/credits -H "Authorization: Bearer $TOKEN"      # tourney back to 0

curl -s -X POST $BASE/api/tournaments/$TID/export \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"platform":"PokerTracker"}'
```

Expect `200` and a hand-history file, credits back to zero, then
`402 quota_exceeded` — and grant another credit via step 6 to confirm the cap
holds even with one in hand.

## 8. Hand exports: two free, then gated

```bash
export HAND=$(curl -s $BASE/api/tournaments/$TID/hands -H "Authorization: Bearer $TOKEN" \
  | python -c "import json,sys; print(json.load(sys.stdin)['hands'][0]['hand_num'])")

for i in 1 2 3; do
  printf '%s: ' "hand export $i"
  curl -s -o /tmp/h -w '%{http_code} ' -X POST $BASE/api/tournaments/$TID/export/hand \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"hand_id\":\"$HAND\",\"platform\":\"PokerTracker\"}"
  python -c "
import json
try: print(json.load(open('/tmp/h')).get('error',''))
except Exception: print('(file)')"
done
```

Expect `200 (file)`, `200 (file)`, then `402 survey_required`.

## 9. The history window

Set `earliest_ts` on one of the tournament documents in the Firebase console to
something older than 7 days, then:

```bash
curl -s $BASE/api/tournaments -H "Authorization: Bearer $TOKEN" \
  | python -c "import json,sys; d=json.load(sys.stdin); print('visible:', len(d['tournaments']), 'hidden:', d['hidden_by_history_cap'])"

curl -s $BASE/api/tournaments/$TID/hands -H "Authorization: Bearer $TOKEN"
```

Expect the count to drop by one, and the detail call to answer
`404 {"error":"history_expired","upgrade":true}`. Flip `users/{uid}.is_pro` to
`true` and both come back in full.
