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


def _seed(db, vectors):
    """Two rooms, two dates. Tournament ids are the fixture file names."""
    db.put(('users', UID), {'is_pro': True})
    names = sorted(vectors)
    for i, tid in enumerate(names):
        db.put(('users', UID, 'tournaments', tid), {
            'room_name': 'Deep Freeze' if i % 2 == 0 else 'Texas',
            'earliest_ts': TS_A if i % 2 == 0 else TS_B,
            'updated_at': 1000 + i,
            'hands': vectors[tid][1],
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

    room_keys = {r['key'] for r in data['filters']['rooms']}
    check('both rooms offered', room_keys == {'DEEP FREEZE', 'TEXAS'}, str(room_keys))
    check('room hand counts sum to total',
          sum(r['hands'] for r in data['filters']['rooms']) == total)

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
    check('room filter still offers every room',
          {r['key'] for r in texas['filters']['rooms']} == room_keys)

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

    # 7. Non-pro and unauthenticated are refused in JSON.
    db.put(('users', UID), {'is_pro': False})
    status, denied = _get(client)
    check('non-pro gets 403', status == 403, str(status))
    check('non-pro gets a JSON error', 'error' in denied)
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
