"""
test_pokerpulse_admin.py — end-to-end shape test for the F1-5 PokerPulse
admin endpoints (/api/admin/pokerpulse/analyse, /preview/<uid>, /send)
against a fake Firestore and a fake Firebase Auth.

session_engine's actual hand-parsing is already covered by
test_session_engine.py, so this test stubs compute_uid_session_stats()
directly and focuses on what F1-5 actually adds: picking subscribed users,
storing/reading users/{uid}/pokerpulse/last_analysis per the documented
schema (docs/firestore-schema.md), the no-hands/stale-window skip rules for
Send Now, the last_report_sent_at marker, and the admin auth gate.
_send_pokerpulse_email() (F1-8's real Brevo API transport) is stubbed too —
this file only covers Send Now's own selection/marker logic, not the transport itself.

    python test_pokerpulse_admin.py
"""

import json
import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')

ADMIN_UID = 'uid-admin'
SUB_HANDS_UID = 'uid-sub-hands'     # subscribed, has hands in the window
SUB_EMPTY_UID = 'uid-sub-empty'     # subscribed, zero hands in the window
PLAIN_UID = 'uid-plain'             # not subscribed — must never appear


# ── Fake Firestore ───────────────────────────────────────────────────────────

class _Snap:
    def __init__(self, doc_id, data):
        self.id, self._data, self.exists = doc_id, data, data is not None

    def to_dict(self):
        return dict(self._data or {})


class _Doc:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def get(self):
        return _Snap(self._path[-1], self._store.get(self._path))

    def set(self, data, merge=False):
        cur = dict(self._store.get(self._path) or {}) if merge else {}
        cur.update(data)
        self._store.put(self._path, cur)

    def update(self, data):
        cur = dict(self._store.get(self._path) or {})
        cur.update(data)
        self._store.put(self._path, cur)

    def collection(self, name):
        return _Col(self._store, self._path + (name,))


class _Query:
    """Supports exactly the one filter shape the app uses: .where(field, '==', value)."""

    def __init__(self, store, path, field, op, value):
        assert op == '==', 'fake only supports =='
        self._store, self._path, self._field, self._value = store, path, field, value

    def stream(self):
        depth = len(self._path) + 1
        return [_Snap(p[-1], data) for p, data in self._store.items(self._path, depth)
                if (data or {}).get(self._field) == self._value]


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))

    def where(self, field, op, value):
        return _Query(self._store, self._path, field, op, value)

    def stream(self):
        depth = len(self._path) + 1
        return [_Snap(p[-1], data) for p, data in self._store.items(self._path, depth)]

    def get(self):
        return self.stream()


class FakeDB:
    """Nested dict keyed by path tuple: ('users', uid, 'pokerpulse', 'last_analysis')."""

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


# ── Fake Firebase Auth ───────────────────────────────────────────────────────

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
    _User(SUB_HANDS_UID, 'hands@example.com'),
    _User(SUB_EMPTY_UID, 'empty@example.com'),
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
        self.store = {}   # path -> (bytes, content_type)

    def blob(self, path):
        return _FakeBlob(self, path)


FAKE_STATS = {
    SUB_HANDS_UID: {
        'total_hands': 42, 'total_games': 3, 'hands_parsed': 42,
        'hands_per_street': {'Preflop': 10, 'Flop': 20, 'Turn': 8, 'River': 4, 'Showdown': 0},
        'vpip_pct': 25.0, 'pfr_pct': 18.0, 'steal_pct': 30.0, 'check_raise_pct': 5.0,
        'three_bet_pct': 8.0, 'fold_to_bet_pct': 40.0, 'cbet_pct': 60.0, 'fold_to_cbet_pct': 35.0,
    },
    SUB_EMPTY_UID: {
        'total_hands': 0, 'total_games': 0, 'hands_parsed': 0,
        'hands_per_street': {'Preflop': 0, 'Flop': 0, 'Turn': 0, 'River': 0, 'Showdown': 0},
        'vpip_pct': 0.0, 'pfr_pct': 0.0, 'steal_pct': 0.0, 'check_raise_pct': 0.0,
        'three_bet_pct': 0.0, 'fold_to_bet_pct': 0.0, 'cbet_pct': 0.0, 'fold_to_cbet_pct': 0.0,
    },
}

from highlights import render_highlight_svg, svg_data_uri  # noqa: E402

FAKE_HIGHLIGHTS = {
    SUB_HANDS_UID: [{
        'pattern': 'biggest_win', 'label': 'Biggest win', 'hand_id': 'h1',
        'hand_url': 'https://example.com/h1',
        'art_uri': svg_data_uri(render_highlight_svg(
            'Biggest win', 'As Kd', ['2h', '3c', '4d'], 'Net: +12,400')),
    }],
    SUB_EMPTY_UID: [],
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _json(res):
    body = res.get_data(as_text=True)
    try:
        return res.status_code, json.loads(body)
    except ValueError:
        raise AssertionError('non-JSON response (%s): %s' % (res.status_code, body[:300]))


def main():
    import app as A

    db = FakeDB()
    db.put(('users', SUB_HANDS_UID), {'pokerpulse_subscribed': True})
    db.put(('users', SUB_EMPTY_UID), {'pokerpulse_subscribed': True})
    db.put(('users', PLAIN_UID), {'pokerpulse_subscribed': False})

    bucket = FakeBucket()
    A._get_admin_db = lambda: db
    A._get_admin_bucket = lambda: bucket
    A.admin_auth = FakeAuth(USERS)
    A.compute_uid_session_stats = lambda uid, start, end: FAKE_STATS[uid]
    A.compute_uid_session_highlights = lambda uid, start, end: [dict(h) for h in FAKE_HIGHLIGHTS[uid]]

    sent_emails = []
    send_result = {'ok': True}
    A._send_pokerpulse_email = lambda to_email, html: (
        sent_emails.append(to_email) or send_result['ok'])

    caller = {'uid': ADMIN_UID}
    A._verify_bearer = lambda req: caller['uid']
    A._is_admin = lambda uid: uid == ADMIN_UID

    client = A.app.test_client()
    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    WIN = {'start': 1_800_000_000, 'end': 1_800_050_000}

    def analyse(win=WIN):
        return _json(client.post('/api/admin/pokerpulse/analyse',
                                 data=json.dumps(win), content_type='application/json'))

    def send(win=WIN):
        return _json(client.post('/api/admin/pokerpulse/send',
                                 data=json.dumps(win), content_type='application/json'))

    def preview(uid):
        return client.get('/api/admin/pokerpulse/preview/%s' % uid)

    def last_analysis(uid):
        return db.get(('users', uid, 'pokerpulse', 'last_analysis'))

    # ── 1. Auth gate ─────────────────────────────────────────────────────────
    for who, label in ((PLAIN_UID, 'non-admin'), (None, 'signed-out')):
        caller['uid'] = who
        status, _ = analyse()
        check('analyse 403 for %s' % label, status == 403, str(status))
        status, _ = send()
        check('send 403 for %s' % label, status == 403, str(status))
        status = preview(SUB_HANDS_UID).status_code
        check('preview 403 for %s' % label, status == 403, str(status))
    caller['uid'] = ADMIN_UID

    # ── 2. Analyse Now ───────────────────────────────────────────────────────
    status, body = analyse()
    check('analyse 200', status == 200, str(status))
    by_uid = {r['uid']: r for r in body.get('results', [])}
    check('only subscribed users analysed', set(by_uid) == {SUB_HANDS_UID, SUB_EMPTY_UID},
          str(set(by_uid)))
    check('non-subscribed user excluded', PLAIN_UID not in by_uid)
    check('hands subscriber stats surfaced', by_uid[SUB_HANDS_UID]['total_hands'] == 42,
          str(by_uid[SUB_HANDS_UID]))
    check('empty subscriber stats surfaced', by_uid[SUB_EMPTY_UID]['total_hands'] == 0)
    check('results sorted by email', [r['email'] for r in body['results']] ==
          sorted(r['email'] for r in body['results']))

    stored = last_analysis(SUB_HANDS_UID)
    check('last_analysis stored (full overwrite)', stored is not None)
    check('stored window matches request',
          stored['window_start'] == WIN['start'] and stored['window_end'] == WIN['end'])
    check('stored stats match compute_uid_session_stats output',
          stored['stats'] == FAKE_STATS[SUB_HANDS_UID], str(stored))
    check('no last_report_sent_at until Send Now runs',
          'last_report_sent_at' not in stored, str(stored))

    # ── 2b. Highlight art persisted to Storage, art_uri rewritten ───────────
    empty_stored = last_analysis(SUB_EMPTY_UID)
    check('subscriber with no highlights stores an empty list',
          empty_stored['highlights'] == [], str(empty_stored))

    hl = stored['highlights']
    check('one highlight stored for the hands subscriber', len(hl) == 1, str(hl))
    art_uri = hl[0]['art_uri'] if hl else ''
    check('art_uri rewritten to a hosted URL, not left as a data URI',
          art_uri.startswith('http') and '/api/pokerpulse/highlight-art/' in art_uri,
          art_uri[:80])
    check('original SVG bytes actually landed in the fake bucket',
          any(path.startswith(f'pokerpulse_highlights/{SUB_HANDS_UID}/') for path in bucket.store),
          str(list(bucket.store)))

    art_path = art_uri.split('http://localhost', 1)[-1] if art_uri.startswith('http://localhost') else None
    if art_path:
        art_res = client.get(art_path)
        check('hosted highlight art route serves the SVG', art_res.status_code == 200,
              str(art_res.status_code))
        check('hosted highlight art has the right content type',
              art_res.content_type.startswith('image/svg+xml'), art_res.content_type)
    else:
        check('hosted highlight art route serves the SVG', False, 'unexpected art_uri host: ' + art_uri)

    # ── 3. Preview ───────────────────────────────────────────────────────────
    res = preview(SUB_HANDS_UID)
    check('preview 200 after analyse', res.status_code == 200, str(res.status_code))
    html = res.get_data(as_text=True)
    check('preview is real HTML', '<html' in html.lower())
    check('preview embeds the real stat, not a placeholder', '42' in html, html[:200])
    check('preview response is html, not json', 'text/html' in res.content_type, res.content_type)

    res = preview('uid-never-analysed')
    check('preview 404 before analyse', res.status_code == 404, str(res.status_code))

    # ── 4. Send Now — skip rules ─────────────────────────────────────────────
    status, body = send()
    check('send 200', status == 200, str(status))
    sent_uids = {s['uid'] for s in body['sent']}
    skipped = {s['uid']: s['reason'] for s in body['skipped']}
    check('only the subscriber with hands was sent to', sent_uids == {SUB_HANDS_UID},
          str(sent_uids))
    check('zero-hand subscriber skipped as no_hands', skipped.get(SUB_EMPTY_UID) == 'no_hands',
          str(skipped))

    check('last_report_sent_at stamped for the sent user',
          last_analysis(SUB_HANDS_UID).get('last_report_sent_at') is not None)
    check('sending did not clobber the stored stats',
          last_analysis(SUB_HANDS_UID)['stats'] == FAKE_STATS[SUB_HANDS_UID])
    check('last_report_sent_at NOT stamped for the skipped user',
          'last_report_sent_at' not in last_analysis(SUB_EMPTY_UID))
    check('transport was actually invoked for the sent user',
          sent_emails == ['hands@example.com'], str(sent_emails))

    # ── 4b. Send Now — transport failure is not counted as sent ─────────────
    stamp_before_failure = last_analysis(SUB_HANDS_UID).get('last_report_sent_at')
    sent_emails.clear()
    send_result['ok'] = False
    status, body = send()
    check('send 200 even when transport fails', status == 200, str(status))
    check('failed transport skips rather than sends',
          body['sent'] == [] and
          {s['uid']: s['reason'] for s in body['skipped']}.get(SUB_HANDS_UID) == 'send_failed',
          str(body))
    check('last_report_sent_at left untouched by a failed send',
          last_analysis(SUB_HANDS_UID).get('last_report_sent_at') == stamp_before_failure)
    send_result['ok'] = True

    # ── 5. Send Now — stale/missing analysis ─────────────────────────────────
    db.put(('users', 'uid-sub-new'), {'pokerpulse_subscribed': True})
    FAKE_STATS['uid-sub-new'] = dict(FAKE_STATS[SUB_HANDS_UID])
    USERS.append(_User('uid-sub-new', 'new@example.com'))
    A.admin_auth = FakeAuth(USERS)
    status, body = send()
    check('newly-subscribed, never-analysed user skipped as not_analysed',
          {s['uid']: s['reason'] for s in body['skipped']}.get('uid-sub-new') == 'not_analysed',
          str(body['skipped']))

    other_win = {'start': WIN['start'] + 999999, 'end': WIN['end'] + 999999}
    status, body = send(other_win)
    check('mismatched window treated as stale for everyone analysed under WIN',
          all(s['reason'] == 'stale_analysis' for s in body['skipped']
              if s['uid'] in (SUB_HANDS_UID, SUB_EMPTY_UID)),
          str(body['skipped']))
    check('stale window sends to nobody', body['sent'] == [], str(body['sent']))

    # ── 6. Malformed window ──────────────────────────────────────────────────
    for bad in ({'start': 'x', 'end': 2}, {'start': 5, 'end': 5}, {}, {'start': 10, 'end': 5}):
        res = client.post('/api/admin/pokerpulse/analyse',
                          data=json.dumps(bad), content_type='application/json')
        check('bad window (%r) 400 on analyse' % bad, res.status_code == 400, str(res.status_code))
        res = client.post('/api/admin/pokerpulse/send',
                          data=json.dumps(bad), content_type='application/json')
        check('bad window (%r) 400 on send' % bad, res.status_code == 400, str(res.status_code))

    for p in problems:
        print('  FAIL', p)
    print('pokerpulse admin API: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
