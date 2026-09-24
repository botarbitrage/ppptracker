"""
test_pokerpulse_cron.py — end-to-end shape test for F2-1's scheduled-send
surface: /api/cron/pokerpulse, /api/admin/pokerpulse/settings,
/api/admin/pokerpulse/dry-run and /api/admin/pokerpulse/run-log, against a
fake Firestore (supporting the atomic .create() the cron endpoint's
idempotency relies on) and a fake Firebase Auth/Storage.

Covers the F2-1 Task's remaining test bullets not already covered by
test_pokerpulse_scheduler.py's pure date math: idempotency (a second tick
for the same cutoff sends nothing), secret auth (missing/wrong/right
X-Cron-Secret), and that the highlight-art path/URL includes window_end so
two runs for the same user don't overwrite each other. Also covers the
weekly_day gate, the enabled/send_time no-ops, and the admin settings/
dry-run/run-log endpoints' own auth gates and shapes.

    python test_pokerpulse_cron.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')

ADMIN_UID = 'uid-admin'
DAILY_UID = 'uid-daily'     # >= threshold hands in the lookback window
WEEKLY_UID = 'uid-weekly'   # < threshold hands in the lookback window
PLAIN_UID = 'uid-plain'     # not subscribed — must never appear

CRON_SECRET = 'test-cron-secret-value'


# ── Fake Firestore (adds atomic .create(), order_by/limit over the earlier
#    test_pokerpulse_admin.py fake, which this file deliberately doesn't
#    import — keeping this test self-contained) ────────────────────────────

class _AlreadyExists(Exception):
    pass


class _Snap:
    def __init__(self, doc_id, data, ref=None):
        self.id, self._data, self.exists, self.reference = doc_id, data, data is not None, ref

    def to_dict(self):
        return dict(self._data or {})


class _Doc:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def get(self):
        return _Snap(self._path[-1], self._store.get(self._path), ref=self)

    def set(self, data, merge=False):
        cur = dict(self._store.get(self._path) or {}) if merge else {}
        cur.update(data)
        self._store.put(self._path, cur)

    def update(self, data):
        cur = dict(self._store.get(self._path) or {})
        cur.update(data)
        self._store.put(self._path, cur)

    def create(self, data):
        if self._store.get(self._path) is not None:
            raise _AlreadyExists(str(self._path))
        self._store.put(self._path, dict(data))

    def collection(self, name):
        return _Col(self._store, self._path + (name,))


class _Query:
    def __init__(self, store, path, field, op, value):
        assert op == '==', 'fake only supports =='
        self._store, self._path, self._field, self._value = store, path, field, value

    def stream(self):
        depth = len(self._path) + 1
        return [_Snap(p[-1], data) for p, data in self._store.items(self._path, depth)
                if (data or {}).get(self._field) == self._value]


class _Col:
    def __init__(self, store, path, order_field=None, order_desc=False, limit_n=None):
        self._store, self._path = store, path
        self._order_field, self._order_desc, self._limit_n = order_field, order_desc, limit_n

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))

    def where(self, field, op, value):
        return _Query(self._store, self._path, field, op, value)

    def order_by(self, field, direction=None):
        return _Col(self._store, self._path, order_field=field,
                    order_desc=(direction == 'DESCENDING' or direction == _DESC_MARKER),
                    limit_n=self._limit_n)

    def limit(self, n):
        return _Col(self._store, self._path, order_field=self._order_field,
                    order_desc=self._order_desc, limit_n=n)

    def stream(self):
        depth = len(self._path) + 1
        items = list(self._store.items(self._path, depth))
        if self._order_field == '__name__':
            items.sort(key=lambda pair: pair[0][-1], reverse=self._order_desc)
        if self._limit_n is not None:
            items = items[:self._limit_n]
        return [_Snap(p[-1], data, ref=_Doc(self._store, p)) for p, data in items]

    def get(self):
        return self.stream()


class FakeDB:
    def __init__(self):
        self._d = {}

    def collection(self, name):
        return _Col(self, (name,))

    def get(self, path):
        return self._d.get(path)

    def put(self, path, data):
        self._d[path] = data

    def items(self, prefix, depth):
        return [(p, data) for p, data in self._d.items()
                if len(p) == depth and p[:len(prefix)] == prefix]


_DESC_MARKER = object()


class _FakeQueryEnum:
    DESCENDING = _DESC_MARKER


# ── Fake Firebase Auth / Storage ────────────────────────────────────────────

class _UserNotFound(Exception):
    pass


class _User:
    def __init__(self, uid, email):
        self.uid, self.email = uid, email


class FakeAuth:
    UserNotFoundError = _UserNotFound

    def __init__(self, users):
        self._users = {u.uid: u for u in users}

    def get_user(self, uid):
        if uid not in self._users:
            raise _UserNotFound(uid)
        return self._users[uid]


USERS = [
    _User(ADMIN_UID, 'admin@example.com'),
    _User(DAILY_UID, 'daily@example.com'),
    _User(WEEKLY_UID, 'weekly@example.com'),
    _User(PLAIN_UID, 'plain@example.com'),
]


class _FakeBlob:
    def __init__(self, bucket, path):
        self._bucket, self.path = bucket, path

    def upload_from_string(self, data, content_type=None):
        self._bucket.store[self.path] = (data, content_type)

    def download_as_bytes(self):
        if self.path not in self._bucket.store:
            raise RuntimeError('no such blob: ' + self.path)
        return self._bucket.store[self.path][0]


class FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, path):
        return _FakeBlob(self, path)


from highlights import render_highlight_svg, svg_data_uri  # noqa: E402

STATS_WITH_HANDS = {
    'total_hands': 150, 'total_games': 4, 'hands_parsed': 150,
    'hands_per_street': {'Preflop': 10, 'Flop': 20, 'Turn': 8, 'River': 4, 'Showdown': 0},
    'vpip_pct': 25.0, 'pfr_pct': 18.0, 'steal_pct': 30.0, 'check_raise_pct': 5.0,
    'three_bet_pct': 8.0, 'fold_to_bet_pct': 40.0, 'cbet_pct': 60.0, 'fold_to_cbet_pct': 35.0,
}
STATS_FEW_HANDS = dict(STATS_WITH_HANDS, total_hands=30)
STATS_EMPTY = dict(STATS_WITH_HANDS, total_hands=0)


def make_highlight():
    return [{
        'pattern': 'biggest_win', 'label': 'Biggest win', 'hand_id': 'h1',
        'hand_url': 'https://example.com/h1',
        'art_uri': svg_data_uri(render_highlight_svg(
            'Biggest win', 'As Kd', ['2h', '3c', '4d'], 'Net: +12,400')),
    }]


def _json(res):
    body = res.get_data(as_text=True)
    try:
        return res.status_code, json.loads(body)
    except ValueError:
        raise AssertionError('non-JSON response (%s): %s' % (res.status_code, body[:300]))


def main():
    import app as A
    import pokerpulse_scheduler as S

    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    db = FakeDB()
    db.put(('users', DAILY_UID), {'pokerpulse_subscribed': True})
    db.put(('users', WEEKLY_UID), {'pokerpulse_subscribed': True})
    db.put(('users', PLAIN_UID), {'pokerpulse_subscribed': False})
    db.put(('config', 'pokerpulse'), {
        'enabled': True, 'send_time': '05:15',
        'daily_threshold_hands': 100, 'lookback_days': 3, 'weekly_day': 'Monday',
    })

    bucket = FakeBucket()
    A._get_admin_db = lambda: db
    A._get_admin_bucket = lambda: bucket
    A.admin_auth = FakeAuth(USERS)
    A.admin_firestore.Query = _FakeQueryEnum  # order_by(direction=...) target

    def fake_stats(uid, start, end):
        if uid == DAILY_UID:
            return STATS_WITH_HANDS
        if uid == WEEKLY_UID:
            return STATS_FEW_HANDS
        return STATS_EMPTY
    A.compute_uid_session_stats = fake_stats
    A.compute_uid_session_highlights = lambda uid, start, end: [dict(h) for h in make_highlight()]

    sent_emails = []
    A._send_pokerpulse_email = lambda to_email, html, subject=None: (sent_emails.append(to_email) or True)

    caller = {'uid': ADMIN_UID}
    A._verify_bearer = lambda req: caller['uid']
    A._is_admin = lambda uid: uid == ADMIN_UID

    os.environ['POKERPULSE_CRON_SECRET'] = CRON_SECRET

    # 2026-06-15 is a Monday — DAILY_UID's threshold classifies daily every
    # tick; WEEKLY_UID (30 hands, under 100) only sends on Mondays.
    NOW = datetime(2026, 6, 15, 6, 0, 0, tzinfo=S.ADELAIDE_TZ)
    A._pokerpulse_now = lambda: NOW

    client = A.app.test_client()

    def cron(secret=CRON_SECRET):
        headers = {'X-Cron-Secret': secret} if secret is not None else {}
        return _json(client.post('/api/cron/pokerpulse', headers=headers))

    # ── 1. Secret auth ───────────────────────────────────────────────────
    old_secret = os.environ.pop('POKERPULSE_CRON_SECRET')
    status, _ = cron()
    check('missing env secret -> 503', status == 503, str(status))
    os.environ['POKERPULSE_CRON_SECRET'] = old_secret

    status, _ = cron(secret='wrong-value')
    check('wrong header secret -> 403', status == 403, str(status))

    status, _ = cron(secret=None)
    check('no header at all -> 403', status == 403, str(status))
    check('no email sent by an unauthenticated tick', sent_emails == [], str(sent_emails))

    # ── 2. Disabled kill switch is a no-op ──────────────────────────────
    db.put(('config', 'pokerpulse'), dict(db.get(('config', 'pokerpulse')), enabled=False))
    status, body = cron()
    check('disabled -> 200 no-op', status == 200 and body.get('skipped_reason') == 'disabled', str(body))
    check('disabled tick sends nothing', sent_emails == [], str(sent_emails))
    db.put(('config', 'pokerpulse'), dict(db.get(('config', 'pokerpulse')), enabled=True))

    # ── 3. Before send_time is a no-op ──────────────────────────────────
    A._pokerpulse_now = lambda: NOW.replace(hour=5, minute=0, second=0)
    status, body = cron()
    check('before send_time -> 200 no-op', status == 200 and body.get('skipped_reason') == 'too_early', str(body))
    check('too-early tick sends nothing', sent_emails == [], str(sent_emails))
    A._pokerpulse_now = lambda: NOW

    # ── 4. First real tick: DAILY sends, WEEKLY sends (it's Monday) ────
    status, body = cron()
    check('first tick 200', status == 200, str(status))
    check('cutoff_date is 2026-06-15', body.get('cutoff_date') == '2026-06-15', str(body))
    sent_uids = {s['uid'] for s in body.get('sent', [])}
    check('DAILY subscriber sent on first tick', DAILY_UID in sent_uids, str(body))
    check('WEEKLY subscriber sent on first tick (it is Monday)', WEEKLY_UID in sent_uids, str(body))
    check('unsubscribed PLAIN_UID never touched', PLAIN_UID not in sent_uids, str(body))
    check('two emails actually sent', sorted(sent_emails) == ['daily@example.com', 'weekly@example.com'],
          str(sent_emails))
    daily_outcome = next(s for s in body['sent'] if s['uid'] == DAILY_UID)
    check('DAILY subscriber classified daily', daily_outcome['cadence'] == 'daily', str(daily_outcome))
    weekly_outcome = next(s for s in body['sent'] if s['uid'] == WEEKLY_UID)
    check('WEEKLY subscriber classified weekly', weekly_outcome['cadence'] == 'weekly', str(weekly_outcome))

    # last_analysis doc gets the cadence field and last_report_sent_at.
    daily_analysis = db.get(('users', DAILY_UID, 'pokerpulse', 'last_analysis'))
    check('last_analysis records cadence', daily_analysis.get('cadence') == 'daily', str(daily_analysis))
    check('last_analysis stamped with last_report_sent_at',
          bool(daily_analysis.get('last_report_sent_at')), str(daily_analysis))

    # ── 5. Idempotency: a second tick for the same cutoff sends nothing ─
    sent_emails.clear()
    status, body = cron()
    check('second tick 200', status == 200, str(status))
    check('second tick sends nothing (already claimed sent)', body.get('sent') == [], str(body))
    check('no new emails on the second tick', sent_emails == [], str(sent_emails))

    # ── 6. Highlight art path/URL includes window_end ───────────────────
    art_uri = daily_analysis['highlights'][0]['art_uri']
    window_end = daily_outcome['window_end']
    check('art URL includes the window_end segment', f'/{window_end}/' in art_uri, art_uri)
    expected_blob_path = f'pokerpulse_highlights/{DAILY_UID}/{window_end}/0.svg'
    check('SVG uploaded under the per-window blob path', expected_blob_path in bucket.store,
          str(list(bucket.store.keys())))

    # A later run for the same uid with a *different* window must not
    # overwrite the earlier window's art — advance to Tuesday (still
    # DAILY, new cutoff date) and confirm both blobs coexist.
    NOW2 = NOW + timedelta(days=1)
    A._pokerpulse_now = lambda: NOW2
    status, body2 = cron()
    check('next day tick 200', status == 200, str(status))
    daily_outcome2 = next((s for s in body2.get('sent', []) if s['uid'] == DAILY_UID), None)
    check('DAILY subscriber sent again the next day', daily_outcome2 is not None, str(body2))
    if daily_outcome2:
        window_end2 = daily_outcome2['window_end']
        check('the new window_end differs from the first run\'s', window_end2 != window_end,
              f'{window_end2} == {window_end}')
        check('old blob still present (not overwritten)', expected_blob_path in bucket.store)
        check('new blob written under its own window_end',
              f'pokerpulse_highlights/{DAILY_UID}/{window_end2}/0.svg' in bucket.store,
              str(list(bucket.store.keys())))
    A._pokerpulse_now = lambda: NOW

    # ── 7. weekly_day gate: WEEKLY subscriber skipped on a non-Monday ──
    NOW_TUE = datetime(2026, 6, 16, 6, 0, 0, tzinfo=S.ADELAIDE_TZ)  # Tuesday
    A._pokerpulse_now = lambda: NOW_TUE
    sent_emails.clear()
    status, body = cron()
    weekly_touched = any(s['uid'] == WEEKLY_UID for s in
                          body.get('sent', []) + body.get('skipped', []) + body.get('errors', []))
    check('WEEKLY subscriber gets no run record at all on a non-weekly_day',
          not weekly_touched, str(body))
    check('WEEKLY subscriber not emailed on a non-weekly_day', 'weekly@example.com' not in sent_emails,
          str(sent_emails))
    A._pokerpulse_now = lambda: NOW

    # ── 8. Run log ────────────────────────────────────────────────────
    status, body = _json(client.get('/api/admin/pokerpulse/run-log'))
    check('run-log 200', status == 200, str(status))
    dates = [r['date'] for r in body.get('runs', [])]
    check('run-log includes 2026-06-15', '2026-06-15' in dates, str(dates))
    check('run-log is newest-first', dates == sorted(dates, reverse=True), str(dates))
    caller['uid'] = PLAIN_UID
    status, _ = _json(client.get('/api/admin/pokerpulse/run-log'))
    check('run-log 403 for non-admin', status == 403, str(status))
    caller['uid'] = ADMIN_UID

    # ── 9. Dry run: read-only, no side effects ──────────────────────────
    sent_before = list(sent_emails)
    db_snapshot_before = dict(db._d)
    status, body = _json(client.post('/api/admin/pokerpulse/dry-run'))
    check('dry-run 200', status == 200, str(status))
    rows = {r['uid']: r for r in body.get('results', [])}
    check('dry-run includes DAILY subscriber', DAILY_UID in rows, str(rows))
    check('dry-run reports the daily cadence', rows.get(DAILY_UID, {}).get('cadence') == 'daily', str(rows))
    check('dry-run does not send email', sent_emails == sent_before, str(sent_emails))
    check('dry-run writes nothing to Firestore', dict(db._d) == db_snapshot_before, 'store mutated')
    caller['uid'] = PLAIN_UID
    status, _ = _json(client.post('/api/admin/pokerpulse/dry-run'))
    check('dry-run 403 for non-admin', status == 403, str(status))
    caller['uid'] = ADMIN_UID

    # ── 10. Settings endpoint: auth gate + validation + round-trip ─────
    caller['uid'] = PLAIN_UID
    status, _ = _json(client.get('/api/admin/pokerpulse/settings'))
    check('settings GET 403 for non-admin', status == 403, str(status))
    status, _ = _json(client.patch('/api/admin/pokerpulse/settings',
                                   data=json.dumps({'enabled': True}), content_type='application/json'))
    check('settings PATCH 403 for non-admin', status == 403, str(status))
    caller['uid'] = ADMIN_UID

    status, body = _json(client.get('/api/admin/pokerpulse/settings'))
    check('settings GET 200', status == 200, str(status))
    check('settings GET reflects live doc', body.get('enabled') is True, str(body))

    for bad_body, why in (
        ({'send_time': '25:00'}, 'out-of-range hour'),
        ({'send_time': 'bad'}, 'not HH:MM'),
        ({'daily_threshold_hands': 0}, 'non-positive threshold'),
        ({'daily_threshold_hands': 'ten'}, 'non-int threshold'),
        ({'lookback_days': -1}, 'negative lookback'),
        ({'weekly_day': 'Someday'}, 'unknown weekday'),
        ({'enabled': 'yes'}, 'non-bool enabled'),
        ({}, 'empty body'),
    ):
        status, _ = _json(client.patch('/api/admin/pokerpulse/settings',
                                       data=json.dumps(bad_body), content_type='application/json'))
        check(f'settings PATCH rejects {why}', status == 400, f'{bad_body} -> {status}')

    status, body = _json(client.patch('/api/admin/pokerpulse/settings',
                                      data=json.dumps({'daily_threshold_hands': 200, 'weekly_day': 'Friday'}),
                                      content_type='application/json'))
    check('valid partial PATCH 200', status == 200, str(status))
    check('partial PATCH updates only named fields',
          body.get('daily_threshold_hands') == 200 and body.get('weekly_day') == 'Friday'
          and body.get('send_time') == '05:15',
          str(body))

    for p in problems:
        print('  FAIL', p)
    print('pokerpulse cron/scheduled-send API: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
