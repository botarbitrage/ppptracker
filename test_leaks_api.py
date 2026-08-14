"""
test_leaks_api.py — end-to-end shape test for /api/leaks against a fake
Firestore, so the endpoint body runs without credentials or network.

This exists because the engine tests (test_leak_cache.py, leak_validation.py)
all stop at the count-vector: they never execute the request handler, so a
bug in the response assembly — filter parsing, date formatting, jsonify —
shipped to production undetected and surfaced as `Unexpected token '<'` in
the browser (the HTML 500 page a fetch() tried to parse as JSON).

The fake stands in for exactly the four Firestore calls the handler makes,
and the vectors come from the real fixture hands, so the assertions cover
the numbers as well as the plumbing.

    python test_leaks_api.py
"""

import glob
import io
import json
import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')

UID = 'test-uid'


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

    def set(self, data):
        self._store.put(self._path, data)

    def collection(self, name):
        return _Col(self._store, self._path + (name,))


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))

    def get(self):
        return [_Snap(k, v) for k, v in self._store.children(self._path)]


class FakeDB:
    """Nested dict keyed by path tuple: ('users', uid, 'tournaments', tid)."""

    def __init__(self):
        self._d = {}
        self.writes = 0

    def collection(self, name):
        return _Col(self, (name,))

    def get(self, path):
        return self._d.get(path)

    def put(self, path, data):
        self._d[path] = data
        self.writes += 1

    def children(self, path):
        n = len(path)
        return sorted((k[n], v) for k, v in self._d.items()
                      if len(k) == n + 1 and k[:n] == path)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _fixture_vectors():
    """One count-vector per validation fixture file, built from real hands."""
    from leak_engine import parse_ps_text, validate_pot, hands_to_vector
    out = {}
    for path in sorted(glob.glob('data/validation/*/*.txt')):
        with io.open(path, encoding='utf-8') as fh:
            hands = [h for h in parse_ps_text(fh.read()) if not validate_pot(h)]
        if hands:
            out[os.path.basename(path)] = (hands_to_vector(hands, scheme='report'),
                                           len(hands))
    return out


# 2026-06-01 and 2026-07-15 UTC — two distinct days, for the date filter.
TS_A, TS_B = 1780272000, 1784073600

# A cash-game tournament doc, deliberately absent from `vectors`: if the
# handler ever tried to build its leak vector, _build_leak_vector's stub
# (below) would KeyError, so its absence from every failure is itself proof
# the exclusion works, not just that the counts happen to match.
CASH_TID = 'CASH_TABLE'
CASH_HANDS = 42

# A play-money MTT: PPPoker reports room.mtt for it exactly like a real one, so
# the only thing keeping it out of the report is the missing club room name.
# Same trick as CASH_TID — absent from `vectors`, so selecting it would KeyError.
PLAY_MTT_TID = 'PLAY_MONEY_MTT'
PLAY_MTT_HANDS = 17


def _seed(db, vectors):
    """Two rooms, two dates, plus one cash-game tournament and one play-money
    MTT that must never be selectable. Tournament ids are the fixture file names."""
    db.put(('users', UID), {'is_pro': True})
    names = sorted(vectors)
    for i, tid in enumerate(names):
        db.put(('users', UID, 'tournaments', tid), {
            'room_name': 'Deep Freeze' if i % 2 == 0 else 'Texas',
            'is_mtt': True,
            'earliest_ts': TS_A if i % 2 == 0 else TS_B,
            'updated_at': 1000 + i,
            'hands': vectors[tid][1],
        })
    db.put(('users', UID, 'tournaments', CASH_TID), {
        'room_name': '40-100BB JP',
        'is_mtt': False,
        'earliest_ts': TS_A,
        'updated_at': 999,
        'hands': CASH_HANDS,
    })
    db.put(('users', UID, 'tournaments', PLAY_MTT_TID), {
        'room_name': 'Unknown',
        'is_mtt': True,
        'earliest_ts': TS_A,
        'updated_at': 998,
        'hands': PLAY_MTT_HANDS,
    })


# ── Checks ───────────────────────────────────────────────────────────────────

def _get(client, query=''):
    res = client.get('/api/leaks' + query)
    body = res.get_data(as_text=True)
    try:
        return res.status_code, json.loads(body)
    except ValueError:
        raise AssertionError('non-JSON response (%s): %s'
                             % (res.status_code, body[:300]))


def main():
    import app as A
    from leak_engine import POSITION_BUCKETS, ALL_STATS

    vectors = _fixture_vectors()
    if not vectors:
        print('no fixtures found'); return 1

    db = FakeDB()
    _seed(db, vectors)

    A._verify_bearer = lambda req: UID
    # /api/leaks is admin-gated now that the Leak Finder lives under /admin.
    # This suite is about the report body, so stand the gate down.
    A._is_admin = lambda uid: True
    A._get_admin_db = lambda: db
    A._build_leak_vector = lambda uid, tid: (vectors[tid][0], vectors[tid][1], 0)
    client = A.app.test_client()

    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    # 1. Unfiltered report over every fixture tournament.
    status, data = _get(client)
    check('unfiltered status 200', status == 200, str(status))
    total = sum(v[1] for v in vectors.values())
    check('unfiltered hand total', data['meta']['hands'] == total,
          '%s != %s' % (data['meta']['hands'], total))
    check('all tournaments loaded',
          data['meta']['tournaments'] == len(vectors))
    check('nothing pending', data['meta']['tournaments_pending'] == 0)

    # The bug that shipped: `timezone` passed where `timezone.utc` was meant,
    # so any user with dated tournaments got a 500 instead of a report.
    check('date_min formatted', data['filters']['date_min'] == '2026-06-01',
          repr(data['filters']['date_min']))
    check('date_max formatted', data['filters']['date_max'] == '2026-07-15',
          repr(data['filters']['date_max']))

    check('every position present',
          [p['position'] for p in data['positions']] == list(POSITION_BUCKETS))
    check('every stat present in every position',
          all(len(p['stats']) == len(ALL_STATS) for p in data['positions']))

    # The sample-size gate moved to the client (it follows the reader's
    # Confidence level), so the payload must carry the level map and must NOT
    # pre-collapse thin samples to INSUFFICIENT — doing so would pin the page
    # to one threshold again.
    from leak_engine import CONFIDENCE_LEVELS
    check('confidence levels published',
          data['meta']['confidence_levels'] == CONFIDENCE_LEVELS,
          repr(data['meta'].get('confidence_levels')))
    check('min_sample still published as the default',
          data['meta']['min_sample'] == CONFIDENCE_LEVELS['med'])
    all_rows = [s for p in data['positions'] for s in p['stats']]
    check('no pre-gated INSUFFICIENT verdicts',
          not any(s['result'] == 'INSUFFICIENT' for s in all_rows))
    # Thin rows must still carry a real verdict for the client to grade.
    thin = [s for s in all_rows
            if 0 < s['opp'] < CONFIDENCE_LEVELS['med'] and s['target']]
    check('thin rows keep a real verdict',
          bool(thin) and all(s['result'] in ('LOW', 'GOOD', 'HIGH') for s in thin),
          '%d thin rows, results=%s' % (len(thin), {s['result'] for s in thin}))
    check('zero-opportunity rows stay unverdicted',
          all(s['result'] is None for s in all_rows if s['opp'] == 0))

    # The cash-game tournament must never be counted, and never rebuilt —
    # if the handler tried, _build_leak_vector's stub would KeyError on
    # CASH_TID since it's deliberately absent from `vectors`.
    check('cash-game hands excluded from the total',
          data['meta']['hands'] == total, str(data['meta']['hands']))
    check('cash-game tournament excluded from the count',
          data['meta']['tournaments'] == len(vectors))
    excluded_rooms = {r['key']: r for r in data['filters']['rooms'] if r['is_mtt'] is False}
    cash_room = excluded_rooms.get('40100BB JP')
    check('cash room offered in filters, flagged is_mtt=False', cash_room is not None)
    if cash_room:
        check('cash room hand count shown for transparency',
              cash_room['hands'] == CASH_HANDS, str(cash_room))

    # A play-money MTT carries room.mtt just like a real one; only the missing
    # room name demotes it. It must be offered as a disabled row, never selected.
    play_room = excluded_rooms.get('UNKNOWN')
    check('play-money MTT flagged is_mtt=False despite room.mtt',
          play_room is not None, str(sorted(excluded_rooms)))
    if play_room:
        check('play-money MTT hand count shown for transparency',
              play_room['hands'] == PLAY_MTT_HANDS, str(play_room))
    check('every real-money tournament room flagged is_mtt=True',
          all(r['is_mtt'] for r in data['filters']['rooms']
              if r['key'] not in excluded_rooms))

    # Explicitly asking for the cash / play-money rooms must still yield nothing
    # selected — the exclusion is server-side, not just a disabled checkbox.
    status, cash_only = _get(client, '?rooms=40100BB%20JP')
    check('explicit cash-room filter still excludes it', status == 200 and
          cash_only['meta']['hands'] == 0 and cash_only['meta']['tournaments'] == 0,
          str(cash_only['meta']))
    status, play_only = _get(client, '?rooms=UNKNOWN')
    check('explicit play-money filter still excludes it', status == 200 and
          play_only['meta']['hands'] == 0 and play_only['meta']['tournaments'] == 0,
          str(play_only['meta']))

    mtt_rooms = [r for r in data['filters']['rooms'] if r['is_mtt']]
    room_keys = {r['key'] for r in mtt_rooms}
    check('both tournament rooms offered', room_keys == {'DEEP FREEZE', 'TEXAS'}, str(room_keys))
    check('tournament room hand counts sum to total',
          sum(r['hands'] for r in mtt_rooms) == total,
          '%s != %s' % (sum(r['hands'] for r in mtt_rooms), total))
    check('all rooms (incl. cash + play money) sum to total + their hands',
          sum(r['hands'] for r in data['filters']['rooms'])
          == total + CASH_HANDS + PLAY_MTT_HANDS)

    # 2. Cache: the second call must serve from Firestore, not rebuild.
    writes_after_cold = db.writes
    check('cold run populated the cache', writes_after_cold > len(vectors) - 1)
    A._build_leak_vector = lambda uid, tid: (_ for _ in ()).throw(
        AssertionError('rebuilt a tournament that was already cached'))
    status, cached = _get(client)
    check('cached status 200', status == 200, str(status))
    check('cached report identical to cold report',
          cached['positions'] == data['positions'])
    check('cache served without new writes', db.writes == writes_after_cold)

    # 3. Room filter selects whole tournaments.
    _, texas = _get(client, '?rooms=TEXAS')
    want = sum(v[1] for i, (k, v) in enumerate(sorted(vectors.items())) if i % 2)
    check('room filter hand total', texas['meta']['hands'] == want,
          '%s != %s' % (texas['meta']['hands'], want))
    check('room filter echoed back',
          texas['filters']['applied']['rooms'] == ['TEXAS'])
    check('room filter still offers every tournament room',
          {r['key'] for r in texas['filters']['rooms'] if r['is_mtt']} == room_keys)

    # 4. Date filter, inclusive of the whole 'to' day.
    _, early = _get(client, '?from=2026-06-01&to=2026-06-01')
    check('date filter keeps the boundary day',
          early['meta']['hands'] == total - want,
          '%s != %s' % (early['meta']['hands'], total - want))

    # 5. Empty selection must be a valid empty report, not a crash.
    status, empty = _get(client, '?rooms=NOPE')
    check('empty selection status 200', status == 200, str(status))
    check('empty selection has no hands', empty['meta']['hands'] == 0)
    check('empty selection winrate is null',
          empty['meta']['winrate_bb100'] is None)
    check('empty selection still lists positions',
          len(empty['positions']) == len(POSITION_BUCKETS))

    # 6. A malformed date is ignored, not fatal.
    status, junk = _get(client, '?from=not-a-date')
    check('bad date ignored', status == 200 and junk['meta']['hands'] == total)

    # 7. Non-admin and unauthenticated are refused in JSON. The Leak Finder moved
    # under /admin, so the gate is admin membership now, not is_pro — a non-pro
    # admin is allowed through and a Pro non-admin is not.
    A._is_admin = lambda uid: False
    status, denied = _get(client)
    check('non-admin gets 403', status == 403, str(status))
    check('non-admin gets a JSON error', 'error' in denied)
    A._is_admin = lambda uid: True

    db.put(('users', UID), {'is_pro': False})
    status, _ = _get(client)
    check('admin without pro is allowed', status == 200, str(status))
    db.put(('users', UID), {'is_pro': True})

    A._verify_bearer = lambda req: None
    status, anon = _get(client)
    check('anonymous gets 401', status == 401, str(status))

    # 8. An unhandled error under /api/ must still answer JSON, so the browser
    #    reports the cause instead of choking on Flask's HTML 500 page.
    A._verify_bearer = lambda req: UID
    A._get_admin_db = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
    print('--- expected traceback below: /api/ error handler under test ---')
    status, crash = _get(client)
    print('--- end expected traceback ---')
    check('crash answers JSON 500', status == 500, str(status))
    check('crash reports the cause', 'boom' in crash.get('error', ''),
          repr(crash))

    print('%d tournaments · %d hands · %d cache writes'
          % (len(vectors), total, writes_after_cold))
    for p in problems:
        print('  FAIL', p)
    print('leaks api: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
