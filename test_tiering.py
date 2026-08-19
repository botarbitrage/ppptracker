"""
test_tiering.py — the anon/free/pro rules, end to end, against a fake Firestore
and a fake Storage bucket so the handlers run without credentials or network.

Everything here is a rule someone can lose money on: an export that should have
cost a survey and didn't, a survey postback that paid out twice, a free account
reading history it no longer has, an ad token spent more than once. The counters
live in Firestore precisely because a process-global can't be trusted across
gunicorn workers, so the tests drive the real handlers through the real
transactions rather than asserting on helper return values alone.

    python test_tiering.py
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')
# Read at import time by app.py, so they must be set before the import below.
os.environ['AD_TOKEN_SECRET']      = 'ad-secret'
os.environ['ANON_SESSION_SECRET']  = 'anon-secret'
os.environ['CPX_SECURE_HASH']      = 'cpx-secret'
os.environ['CPX_APP_ID']           = 'cpx-app'
os.environ['TALLY_SIGNING_SECRET'] = 'tally-secret'

FREE_UID = 'uid-free'
PRO_UID  = 'uid-pro'

_FAILURES = []


def check(label, cond, extra=''):
    if cond:
        print(f'  [PASS] {label}')
    else:
        _FAILURES.append(label)
        print(f'  [FAIL] {label} {extra}')


# ── Fake Firestore ───────────────────────────────────────────────────────────
# Paths are tuples: ('users', uid, 'tournaments', tid). Enough of the surface to
# run the handlers: get/set/update/create, subcollections, and transactions.

class _Snap:
    def __init__(self, doc_id, data, ref=None):
        self.id, self._data, self.exists = doc_id, data, data is not None
        self.reference = ref

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def collection(self, name):
        return _Col(self._store, self._path + (name,))

    def get(self, transaction=None):
        return _Snap(self._path[-1], self._store.get(self._path), self)

    def set(self, data, merge=False):
        cur = dict(self._store.get(self._path) or {}) if merge else {}
        cur.update(data)
        self._store.put(self._path, cur)

    def update(self, data):
        cur = self._store.get(self._path)
        if cur is None:
            raise RuntimeError(f'update() on missing doc {self._path}')
        cur = dict(cur)
        cur.update(data)
        self._store.put(self._path, cur)

    def create(self, data):
        from google.api_core import exceptions as gexc
        if self._store.get(self._path) is not None:
            raise gexc.AlreadyExists(f'{self._path} exists')
        self._store.put(self._path, dict(data))


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))

    def get(self):
        out = []
        for path, data in self._store.items():
            if path[:-1] == self._path and len(path) == len(self._path) + 1:
                out.append(_Snap(path[-1], data, _Doc(self._store, path)))
        return out


class _Txn:
    """Applies straight through — the fake is single-threaded, so the only thing
    worth exercising is that reads and writes go to the right paths."""

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, data):
        ref.update(data)


class FakeDB:
    def __init__(self):
        self._d = {}

    def collection(self, name):
        return _Col(self, (name,))

    def transaction(self):
        return _Txn()

    def get(self, path):
        return self._d.get(path)

    def put(self, path, data):
        self._d[path] = data

    def items(self):
        return list(self._d.items())


# ── Fake Storage ─────────────────────────────────────────────────────────────

class _Blob:
    def __init__(self, bucket, name):
        self._bucket, self.name = bucket, name
        self.metadata = None

    def upload_from_string(self, data, content_type=None):
        self._bucket.objects[self.name] = (data, dict(self.metadata or {}))

    def exists(self):
        return self.name in self._bucket.objects

    def download_as_bytes(self):
        data = self._bucket.objects[self.name][0]
        return data.encode() if isinstance(data, str) else data

    def delete(self):
        self._bucket.objects.pop(self.name, None)


class FakeBucket:
    def __init__(self):
        self.objects = {}

    def blob(self, name):
        b = _Blob(self, name)
        if name in self.objects:
            b.metadata = dict(self.objects[name][1])
        return b

    def list_blobs(self, prefix='', max_results=None):
        out = []
        for name in list(self.objects):
            if name.startswith(prefix):
                out.append(self.blob(name))
        return out[:max_results] if max_results else out


# ── Harness ──────────────────────────────────────────────────────────────────

import google.cloud.firestore as _gcf
_gcf.transactional = lambda fn: fn        # the fake transaction needs no retry loop

import app as A                                                    # noqa: E402

DB     = FakeDB()
BUCKET = FakeBucket()
_signed_in = {'uid': None}

A._get_admin_db      = lambda: DB
A._get_admin_bucket  = lambda: BUCKET
A._verify_bearer     = lambda req: _signed_in['uid']
A._verify_bearer_claims = lambda req: ({'uid': _signed_in['uid'], 'email': 'x@y.z'}
                                       if _signed_in['uid'] else None)
A.export_pokerstars  = lambda records, platform='', blind_levels_by_room=None: (
    _write_tmp(records), [])
A._blind_levels_by_room = lambda records: {}
A._score_import      = lambda claims, new_hands: None


def _write_tmp(records):
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.txt')
    with os.fdopen(fd, 'w') as fh:
        fh.write(f'{len(records)} hands')
    return path


def as_user(uid):
    _signed_in['uid'] = uid


CLIENT = A.app.test_client()


def _reset_user(uid, is_pro=False):
    DB.put(('users', uid), {'is_pro': is_pro})
    for path in [p for p, _ in DB.items()
                 if len(p) > 2 and p[0] == 'users' and p[1] == uid
                 and p[2] in ('ad_jtis', 'survey_completions')]:
        DB._d.pop(path, None)


def _put_tournament(uid, tid, earliest_ts, hands=3):
    DB.put(('users', uid, 'tournaments', tid),
           {'tourney_id': tid, 'room_name': 'DEEP FREEZE', 'is_mtt': True,
            'earliest_ts': earliest_ts, 'hands': hands,
            'storage_path': f'tournaments/{uid}/{tid}.json'})


def _records(tid, n=3):
    # gameid is prefix-tourneyid-seq (see hand_parser.extract_tourney_id), so the
    # tournament id must be the middle segment for the routes to match on it.
    return [{'summary': {'D': f'1858-{tid}-{i}', 'C': 1700000000 + i},
             'full_hand': {'info': {'room': {'room_name': 'DEEP FREEZE'}}}}
            for i in range(n)]


def _stub_tournament_records(mapping):
    A._fetch_tournament_records = lambda uid, tid: (
        (mapping.get(tid), DB.get(('users', uid, 'tournaments', tid)))
        if tid in mapping else (None, None))


NOW = int(time.time())
OLD_TS    = NOW - 30 * 86400
RECENT_TS = NOW - 2 * 86400


# ── 1. Quota bookkeeping ─────────────────────────────────────────────────────

def test_quota():
    print('quota')
    _reset_user(FREE_UID)
    with A.app.test_request_context('/'):
        check('lazily zero before any write',
              A._quota_state(FREE_UID)['imports'] == 0)

    # A stale day must read as zero without anyone rewriting it first.
    DB.put(('users', FREE_UID), {'is_pro': False,
                                 'quota': {'day': '2000-01-01', 'imports': 3,
                                           'hand_exports': 5, 'tourney_exports': 1}})
    with A.app.test_request_context('/'):
        state = A._quota_state(FREE_UID)
    check('previous UTC day rolls over on read',
          state['imports'] == 0 and state['hand_exports'] == 0, str(state))

    with A.app.test_request_context('/'):
        A._bump_quota(FREE_UID, 'imports')
        A._bump_quota(FREE_UID, 'imports')
        state = A._quota_state(FREE_UID)
    check('bump increments today only',
          state['imports'] == 2 and state['day'] == A._utc_day(), str(state))
    check('rollover rewrote the stored day',
          DB.get(('users', FREE_UID))['quota']['day'] == A._utc_day())

    # The user doc is created lazily — a user who has never been written must
    # not 500 the first time they export.
    DB._d.pop(('users', 'uid-fresh'), None)
    with A.app.test_request_context('/'):
        A._bump_quota('uid-fresh', 'hand_exports')
    check('quota creates the user doc when absent',
          DB.get(('users', 'uid-fresh'))['quota']['hand_exports'] == 1)


# ── 2. Survey credits ────────────────────────────────────────────────────────

def test_credits():
    print('credits')
    _reset_user(FREE_UID)
    with A.app.test_request_context('/'):
        for _ in range(5):
            A._grant_credit(FREE_UID, 'hand')
        for _ in range(3):
            A._grant_credit(FREE_UID, 'tourney')
        credits = A._credits(FREE_UID)
    check('hand credits cap at 3', credits['hand'] == 3, str(credits))
    check('tourney credits cap at 1', credits['tourney'] == 1, str(credits))

    with A.app.test_request_context('/'):
        spent = A._consume_credit(FREE_UID, 'tourney')
        again = A._consume_credit(FREE_UID, 'tourney')
        left  = A._credits(FREE_UID)
    check('consume spends one credit', spent is True and left['tourney'] == 0)
    check('consume at zero is a no-op', again is False)


# ── 3. Ad tokens ─────────────────────────────────────────────────────────────

def test_ad_tokens():
    print('ad tokens')
    _reset_user(FREE_UID)
    with A.app.test_request_context('/'):
        token, exp = A._issue_ad_token(FREE_UID, 'hand')
        check('token carries a future expiry', exp > time.time())
        check('valid token verifies', A._verify_ad_token(token, FREE_UID, 'hand'))
        check('single use — a replay is refused',
              A._verify_ad_token(token, FREE_UID, 'hand') is False)

        other, _ = A._issue_ad_token(FREE_UID, 'hand')
        check('kind is scoped to the endpoint',
              A._verify_ad_token(other, FREE_UID, 'tourney') is False)
        check('another user cannot spend it',
              A._verify_ad_token(other, PRO_UID, 'hand') is False)

        encoded = other.split('.')[0]
        check('a forged signature is refused',
              A._verify_ad_token(f'{encoded}.deadbeef', FREE_UID, 'hand') is False)

        stale = base64.urlsafe_b64encode(
            f'{FREE_UID}|hand|{int(time.time()) - 1}|jti'.encode()).decode().rstrip('=')
        check('an expired token is refused',
              A._verify_ad_token(f'{stale}.{A._sign(A._AD_TOKEN_SECRET, stale)}',
                                 FREE_UID, 'hand') is False)


# ── 4. Export gates ──────────────────────────────────────────────────────────

def _export_hand(tid='trecent', hand='1858-trecent-0', headers=None):
    return CLIENT.post(f'/api/tournaments/{tid}/export/hand',
                       json={'hand_id': hand, 'platform': 'PokerTracker'},
                       headers=headers or {})


def _export_tourney(tid='trecent'):
    return CLIENT.post(f'/api/tournaments/{tid}/export', json={'platform': 'PokerTracker'})


def test_export_gates():
    print('export gates')
    _reset_user(FREE_UID)
    _reset_user(PRO_UID, is_pro=True)
    _put_tournament(FREE_UID, 'trecent', RECENT_TS)
    _put_tournament(PRO_UID, 'trecent', RECENT_TS)
    _stub_tournament_records({'trecent': _records('trecent')})

    as_user(None)
    res = _export_hand()
    check('anonymous export is refused with login_required',
          res.status_code == 401 and res.get_json().get('error') == 'login_required',
          str(res.status_code))
    res = CLIENT.post('/api/export/pokerstars', json={'tourney_ids': ['trecent']})
    check('anonymous session export is refused too', res.status_code == 401)

    as_user(FREE_UID)
    codes = [_export_hand().status_code for _ in range(2)]
    check('free hand exports 1 and 2 need no survey', codes == [200, 200], str(codes))

    res = _export_hand()
    body = res.get_json()
    check('hand export 3 asks for a survey',
          res.status_code == 402 and body.get('error') == 'survey_required'
          and body.get('kind') == 'hand', f'{res.status_code} {body}')

    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'hand')
    res = _export_hand()
    check('a survey credit unlocks hand export 3', res.status_code == 200,
          str(res.status_code))
    with A.app.test_request_context('/'):
        after = A._credits(FREE_UID)
    check('the credit was spent', after['hand'] == 0, str(after))

    # 4 and 5 via ad tokens, which is the other half of the same contract.
    for n in (4, 5):
        with A.app.test_request_context('/'):
            token, _ = A._issue_ad_token(FREE_UID, 'hand')
        res = _export_hand(headers={'X-Ad-Token': token})
        check(f'ad token unlocks hand export {n}', res.status_code == 200,
              str(res.status_code))

    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'hand')
    res = _export_hand()
    body = res.get_json()
    check('hand export 6 is over the daily cap, credit or not',
          res.status_code == 402 and body.get('error') == 'quota_exceeded'
          and body.get('upgrade') is True, f'{res.status_code} {body}')

    # Per-tournament exports: always survey-gated, one a day.
    res = _export_tourney()
    check('tournament export asks for a survey first',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          str(res.status_code))
    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'tourney')
    check('tournament export succeeds on a credit', _export_tourney().status_code == 200)
    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'tourney')
    res = _export_tourney()
    check('second tournament export is capped for the day',
          res.status_code == 402 and res.get_json().get('error') == 'quota_exceeded',
          str(res.status_code))

    res = CLIENT.post('/api/export/pokerstars', json={'tourney_ids': ['trecent']})
    body = res.get_json()
    check('free whole-session export is upgrade-only, with no survey path',
          res.status_code == 403 and body.get('error') == 'upgrade_required'
          and body.get('feature') == 'full_session_export', f'{res.status_code} {body}')
    res = CLIENT.post('/api/export/json/all', json={'tourney_ids': ['trecent']})
    check('free whole-session JSON export is upgrade-only too',
          res.status_code == 403 and res.get_json().get('error') == 'upgrade_required')

    as_user(PRO_UID)
    codes = [_export_hand().status_code for _ in range(8)]
    check('pro hand exports are uncapped', codes == [200] * 8, str(codes))
    codes = [_export_tourney().status_code for _ in range(3)]
    check('pro tournament exports are uncapped', codes == [200] * 3, str(codes))
    check('pro whole-session export works',
          CLIENT.post('/api/export/pokerstars',
                      json={'tourney_ids': ['trecent']}).status_code == 200)
    with A.app.test_request_context('/'):
        check('pro exports are not counted against any quota',
              A._quota_state(PRO_UID)['hand_exports'] == 0)


# ── 4b. Export ads config (admin-controlled hard/soft survey limits) ────────

def test_export_ads_config():
    print('export ads config')
    _reset_user(FREE_UID)
    _put_tournament(FREE_UID, 'trecent', RECENT_TS)
    _stub_tournament_records({'trecent': _records('trecent')})
    DB._d.pop(('config', 'export_ads'), None)
    DB.put(('config', 'admins'), {'uids': ['uid-admin']})

    DEFAULT_CFG = {
        'hand_hard_limit':    A.FREE_HAND_EXPORTS_PER_DAY,
        'hand_soft_limit':    A.FREE_HAND_EXPORTS_PER_DAY - A.FREE_HAND_EXPORTS_UNGATED,
        'tourney_hard_limit': A.FREE_TOURNEY_EXPORTS_DAY,
        'tourney_soft_limit': A.FREE_TOURNEY_EXPORTS_DAY,
    }

    as_user(None)
    res = CLIENT.get('/api/export-ads-config')
    check('the public config endpoint needs no account', res.status_code == 200,
          str(res.status_code))
    check('and reproduces the hardcoded constants by default',
          res.get_json() == DEFAULT_CFG, str(res.get_json()))
    check('the admin config endpoint needs an account',
          CLIENT.get('/api/admin/export-ads-config').status_code == 403)

    as_user(FREE_UID)  # not an admin
    check('non-admin cannot read the admin config',
          CLIENT.get('/api/admin/export-ads-config').status_code == 403)
    check('non-admin cannot write the config',
          CLIENT.post('/api/admin/export-ads-config',
                      json={'hand_hard_limit': 10}).status_code == 403)

    as_user('uid-admin')
    res = CLIENT.get('/api/admin/export-ads-config')
    check('admin can read the config', res.status_code == 200, str(res.status_code))
    check('default config matches the public endpoint',
          res.get_json() == DEFAULT_CFG, str(res.get_json()))

    for bad in ({'hand_hard_limit': 'five'}, {'hand_soft_limit': -1},
                {'tourney_hard_limit': True}, {'tourney_soft_limit': 'x'}, {}):
        res = CLIENT.post('/api/admin/export-ads-config', json=bad)
        check(f'rejects malformed body {bad}', res.status_code == 400, str(res.status_code))

    # Bullet 1: hard limit 0 blocks the kind outright — quota_exceeded, no
    # survey ever offered.
    res = CLIENT.post('/api/admin/export-ads-config', json={'hand_hard_limit': 0})
    check('setting hand hard limit to 0 200', res.status_code == 200, str(res.status_code))
    check('other fields untouched', res.get_json()['hand_soft_limit'] == DEFAULT_CFG['hand_soft_limit'])

    as_user(FREE_UID)
    res = _export_hand()
    check('hard limit 0 blocks with quota_exceeded, not survey_required',
          res.status_code == 402 and res.get_json().get('error') == 'quota_exceeded',
          f'{res.status_code} {res.get_json()}')

    # Bullet 3: soft limit 0 (with a positive hard limit) means every slot up
    # to the hard cap is free — no survey ever, but the cap still bites.
    _reset_user(FREE_UID)
    as_user('uid-admin')
    res = CLIENT.post('/api/admin/export-ads-config',
                      json={'hand_hard_limit': 5, 'hand_soft_limit': 0})
    check('soft limit 0 200', res.status_code == 200, str(res.status_code))

    as_user(FREE_UID)
    codes = [_export_hand().status_code for _ in range(5)]
    check('with soft limit 0, all 5 free hand exports need no survey',
          codes == [200] * 5, str(codes))
    res = _export_hand()
    check('the hard cap still applies once soft limit is 0',
          res.status_code == 402 and res.get_json().get('error') == 'quota_exceeded',
          f'{res.status_code} {res.get_json()}')

    # Bullet 4: soft limit > 0 sets how many of the hard-limit slots are
    # survey-gated, counted off the end (free_count = hard - soft).
    _reset_user(FREE_UID)
    as_user('uid-admin')
    res = CLIENT.post('/api/admin/export-ads-config',
                      json={'hand_hard_limit': 5, 'hand_soft_limit': 1,
                            'tourney_hard_limit': 2, 'tourney_soft_limit': 1})
    check('reconfiguring hard/soft limits 200', res.status_code == 200, str(res.status_code))

    as_user(FREE_UID)
    codes = [_export_hand().status_code for _ in range(4)]
    check('hand free_count=5-1=4: first 4 exports need no survey',
          codes == [200] * 4, str(codes))
    res = _export_hand()
    check('hand export 5 needs a survey (the last soft_limit=1 slot)',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          f'{res.status_code} {res.get_json()}')

    res = _export_tourney()
    check('tourney free_count=2-1=1: export 1 needs no survey',
          res.status_code == 200, str(res.status_code))
    res = _export_tourney()
    check('tourney export 2 needs a survey (the last soft_limit=1 slot)',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          f'{res.status_code} {res.get_json()}')

    # Tourney's default (hard=1, soft=1 -> free_count=0) must still reproduce
    # today's "always gated" behaviour with no config doc at all.
    DB._d.pop(('config', 'export_ads'), None)
    _reset_user(FREE_UID)
    res = _export_tourney()
    check('default tourney export still needs a survey immediately',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          f'{res.status_code} {res.get_json()}')


# ── 5. The 7-day history window ──────────────────────────────────────────────

def test_history_window():
    print('history window')
    for uid, is_pro in ((FREE_UID, False), (PRO_UID, True)):
        _reset_user(uid, is_pro=is_pro)
        _put_tournament(uid, 'trecent', RECENT_TS)
        _put_tournament(uid, 'told', OLD_TS)
        _put_tournament(uid, 'tundated', None)
    _stub_tournament_records({'trecent': _records('trecent'),
                              'told': _records('told'),
                              'tundated': _records('tundated')})

    as_user(FREE_UID)
    body = CLIENT.get('/api/tournaments').get_json()
    ids = {t['tourney_id'] for t in body['tournaments']}
    check('free list hides tournaments older than 7 days', 'told' not in ids, str(ids))
    check('free list keeps recent tournaments', 'trecent' in ids)
    check('a tournament with no timestamp is not assumed old', 'tundated' in ids)
    check('the response says how many were hidden', body['hidden_by_history_cap'] == 1,
          str(body.get('hidden_by_history_cap')))

    res = CLIENT.get('/api/tournaments/told/hands')
    check('free detail view of an expired tournament 404s as history_expired',
          res.status_code == 404 and res.get_json().get('error') == 'history_expired',
          str(res.status_code))
    check('free detail view of a recent tournament still works',
          CLIENT.get('/api/tournaments/trecent/hands').status_code == 200)

    res = _export_tourney('told')
    check('expired tournaments cannot be exported either',
          res.status_code == 404 and res.get_json().get('error') == 'history_expired',
          str(res.status_code))

    as_user(PRO_UID)
    body = CLIENT.get('/api/tournaments').get_json()
    ids = {t['tourney_id'] for t in body['tournaments']}
    check('pro sees the whole history', {'told', 'trecent', 'tundated'} <= ids, str(ids))
    check('pro detail view of an old tournament works',
          CLIENT.get('/api/tournaments/told/hands').status_code == 200)


# ── 6. Imports: quota, window, anonymous sessions, claiming ──────────────────

def _stub_pppoker(records):
    A.fetch_summaries = lambda uid, rdkey, referer: {
        'code': 0, 'I': [r['summary'] for r in records]}
    by_id = {r['summary']['D']: r for r in records}
    A._fetch_record = lambda uid, rdkey, summary, referer: by_id.get(summary.get('D'))


IMPORT_URL = 'https://replay.pppoker.net/?uid=999&rdkey=abc'


def _import():
    return CLIENT.post('/api/analyze', json={'url': IMPORT_URL})


def test_import_quota_and_window():
    print('imports')
    _reset_user(FREE_UID)
    saved = {}
    A._save_tournaments = lambda claims, records, tournaments: (
        saved.update({'tournaments': tournaments}) or (True, {'g1'}))
    _stub_pppoker(_records('trecent'))

    as_user(FREE_UID)
    codes = [_import().status_code for _ in range(3)]
    check('three imports a day are allowed', codes == [200, 200, 200], str(codes))
    res = _import()
    body = res.get_json()
    check('the fourth import is refused with an upgrade CTA',
          res.status_code == 402 and body.get('error') == 'quota_exceeded'
          and body.get('kind') == 'import' and body.get('upgrade') is True,
          f'{res.status_code} {body}')

    as_user(PRO_UID)
    _reset_user(PRO_UID, is_pro=True)
    codes = [_import().status_code for _ in range(5)]
    check('pro imports are uncapped', codes == [200] * 5, str(codes))
    with A.app.test_request_context('/'):
        check('pro imports are not counted', A._quota_state(PRO_UID)['imports'] == 0)


def test_free_import_prunes_old_tournaments():
    print('import history window')
    _reset_user(FREE_UID)
    kept = {}
    A._save_tournaments = lambda claims, records, tournaments: (
        kept.update({'ids': [t['tourney_id'] for t in tournaments]}) or (True, set()))

    old, new = _records('told', 2), _records('tfresh', 2)
    _stub_pppoker(old + new)
    from hand_parser import extract_tourney_id
    real_process = A.process_hands
    A.process_hands = lambda records: (
        [], [],
        {},
        [{'tourney_id': tid, 'earliest_ts': ts, 'room_name': 'DEEP FREEZE'}
         for tid, ts in (('told', OLD_TS), ('tfresh', RECENT_TS))
         if any(extract_tourney_id(r['summary']['D']) == tid for r in records)])
    A.validate_hands = lambda records: {}
    try:
        as_user(FREE_UID)
        body = _import().get_json()
    finally:
        A.process_hands = real_process

    check('the out-of-window tournament is not persisted',
          kept.get('ids') == ['tfresh'], str(kept))
    check('and it is pruned from the response',
          [t['tourney_id'] for t in body['tournaments']] == ['tfresh'],
          str(body['tournaments']))
    check('the response reports the prune', body['history_expired_tournaments'] == 1,
          str(body.get('history_expired_tournaments')))


def test_anon_import_and_claim():
    print('anonymous import and claim')
    _reset_user(FREE_UID)
    BUCKET.objects.clear()
    # Mirrors the real thing: an import with no verified token persists nothing.
    A._save_tournaments = lambda claims, records, tournaments: (
        (True, {'g1'}) if claims else (False, set()))
    A._tournament_graphs = lambda records, tournaments: [{'tourney_id': 'stub'}]
    _stub_pppoker(_records('trecent'))

    as_user(None)
    body = _import().get_json()
    token = body.get('session_token')
    check('an anonymous import returns a signed session token', bool(token))
    check('and per-tournament graph data, so the graphs render without an account',
          body.get('tournament_graphs') == [{'tourney_id': 'stub'}])
    check('the anonymous import is parked in Storage, not the history',
          any(k.startswith('anon_sessions/') for k in BUCKET.objects), str(BUCKET.objects))
    check('anonymous imports are reported as unsaved', body.get('saved') is False)

    check('a forged session token loads nothing',
          A._load_anon_session(token.split('.')[0] + '.forged') is None)

    as_user(FREE_UID)
    res = CLIENT.post('/api/analyze/claim', json={'session_token': token})
    body = res.get_json()
    check('signing in claims the pending import',
          res.status_code == 200 and body.get('claimed') is True, f'{res.status_code} {body}')
    with A.app.test_request_context('/'):
        check('the claim counts as the day\'s import',
              A._quota_state(FREE_UID)['imports'] == 1)
    check('the claimed blob is cleaned up', not BUCKET.objects, str(BUCKET.objects))

    res = CLIENT.post('/api/analyze/claim', json={'session_token': token})
    check('a claimed token cannot be replayed', res.status_code == 404, str(res.status_code))

    # At the import cap the blob must survive so it can be claimed tomorrow.
    BUCKET.objects.clear()
    as_user(None)
    token = _import().get_json()['session_token']
    DB.put(('users', FREE_UID), {'is_pro': False,
                                 'quota': {'day': A._utc_day(), 'imports': 3,
                                           'hand_exports': 0, 'tourney_exports': 0}})
    as_user(FREE_UID)
    res = CLIENT.post('/api/analyze/claim', json={'session_token': token})
    check('claiming over the import cap is refused',
          res.status_code == 402 and res.get_json().get('error') == 'quota_exceeded',
          str(res.status_code))
    check('and the pending import is kept for a later attempt',
          any(k.startswith('anon_sessions/') for k in BUCKET.objects))

    as_user(None)
    res = CLIENT.post('/api/analyze/claim', json={'session_token': token})
    check('claiming needs an account', res.status_code == 401, str(res.status_code))


# ── 7. Survey callbacks ──────────────────────────────────────────────────────

def test_cpx_postback():
    print('cpx postback')
    _reset_user(FREE_UID)
    trans = 'trans-1'
    good  = hashlib.md5(f'{trans}-cpx-secret'.encode()).hexdigest()

    res = CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans}'
                     f'&subid_1=hand&status=1&hash=nope')
    check('a bad hash is rejected', res.status_code == 403, str(res.status_code))
    with A.app.test_request_context('/'):
        check('and grants nothing', A._credits(FREE_UID)['hand'] == 0)

    unhyphenated = hashlib.md5(f'{trans}cpx-secret'.encode()).hexdigest()
    res = CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans}'
                     f'&subid_1=hand&status=1&hash={unhyphenated}')
    check('a hash missing the hyphen separator is rejected',
          res.status_code == 403, str(res.status_code))
    with A.app.test_request_context('/'):
        check('and grants nothing', A._credits(FREE_UID)['hand'] == 0)

    url = (f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans}&subid_1=hand'
           f'&status=1&amount_usd=0.35&offer_id=42&hash={good}')
    res = CLIENT.get(url)
    check('a valid postback answers a literal 1',
          res.status_code == 200 and res.get_data(as_text=True) == '1',
          res.get_data(as_text=True))
    with A.app.test_request_context('/'):
        check('and grants exactly one credit', A._credits(FREE_UID)['hand'] == 1)
    check('the completion is recorded with the provider payload',
          (DB.get(('users', FREE_UID, 'survey_completions', trans)) or {}).get('offer_id') == '42',
          str(DB.get(('users', FREE_UID, 'survey_completions', trans))))

    CLIENT.get(url)
    CLIENT.get(url)
    with A.app.test_request_context('/'):
        check('redelivery of the same trans_id pays once',
              A._credits(FREE_UID)['hand'] == 1)

    rev = (f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans}&subid_1=hand'
           f'&status=2&hash={good}')
    CLIENT.get(rev)
    with A.app.test_request_context('/'):
        check('a reversal claws back the unspent credit',
              A._credits(FREE_UID)['hand'] == 0)
    CLIENT.get(rev)
    with A.app.test_request_context('/'):
        check('a repeated reversal does not go negative',
              A._credits(FREE_UID)['hand'] == 0)

    # A credit that has already been spent has nothing left to reverse.
    trans2 = 'trans-2'
    hash2  = hashlib.md5(f'{trans2}-cpx-secret'.encode()).hexdigest()
    CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans2}'
               f'&subid_1=tourney&status=1&hash={hash2}')
    with A.app.test_request_context('/'):
        A._consume_credit(FREE_UID, 'tourney')
    CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans2}'
               f'&subid_1=tourney&status=2&hash={hash2}')
    with A.app.test_request_context('/'):
        check('reversing a spent credit leaves the balance alone',
              A._credits(FREE_UID)['tourney'] == 0)

    # Non-complete event types (screen-outs, bonuses) still answer '1' so CPX
    # doesn't retry, but must not grant a credit.
    for ev_type in ('out', 'bonus'):
        trans_ev = f'trans-{ev_type}'
        hash_ev  = hashlib.md5(f'{trans_ev}-cpx-secret'.encode()).hexdigest()
        res = CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans_ev}'
                         f'&subid_1=hand&status=1&type={ev_type}&hash={hash_ev}')
        check(f'type={ev_type} is accepted',
              res.status_code == 200 and res.get_data(as_text=True) == '1',
              res.get_data(as_text=True))
        with A.app.test_request_context('/'):
            check(f'but type={ev_type} grants zero credits',
                  A._credits(FREE_UID)['hand'] == 0)


def test_survey_config_hash():
    print('survey config hash')
    as_user(FREE_UID)
    res = CLIENT.get('/api/survey-config')
    check('survey-config responds ok', res.status_code == 200, str(res.status_code))
    expected = hashlib.md5(f'{FREE_UID}-cpx-secret'.encode()).hexdigest()
    check('secure_hash is md5(uid + "-" + secret)',
          res.get_json()['cpx']['secure_hash'] == expected,
          res.get_json()['cpx'])


def test_tally_callback():
    print('tally callback')
    _reset_user(FREE_UID)
    body = json.dumps({'eventType': 'FORM_RESPONSE', 'data': {
        'responseId': 'resp-1', 'formId': 'form-1',
        'fields': [{'key': 'uid', 'label': 'uid', 'value': FREE_UID},
                   {'key': 'kind', 'label': 'kind', 'value': 'tourney'}]}})
    sig = base64.b64encode(hmac.new(b'tally-secret', body.encode(),
                                    hashlib.sha256).digest()).decode()

    res = CLIENT.post('/api/tally/callback', data=body,
                      headers={'Content-Type': 'application/json',
                               'Tally-Signature': 'wrong'})
    check('an unsigned submission is rejected', res.status_code == 403, str(res.status_code))

    res = CLIENT.post('/api/tally/callback', data=body,
                      headers={'Content-Type': 'application/json',
                               'Tally-Signature': sig})
    check('a signed submission is accepted', res.status_code == 200, str(res.status_code))
    with A.app.test_request_context('/'):
        check('and grants the credit named by the hidden field',
              A._credits(FREE_UID)['tourney'] == 1)

    res = CLIENT.post('/api/tally/callback', data=body,
                      headers={'Content-Type': 'application/json',
                               'Tally-Signature': sig})
    check('a redelivered responseId pays once',
          res.get_json().get('duplicate') is True, str(res.get_json()))
    with A.app.test_request_context('/'):
        check('so the balance is unchanged', A._credits(FREE_UID)['tourney'] == 1)


def test_credit_endpoints():
    print('credit endpoints')
    _reset_user(FREE_UID)
    as_user(None)
    check('credits need an account', CLIENT.get('/api/credits').status_code == 401)
    check('ad tokens need an account',
          CLIENT.post('/api/ad-token', json={'kind': 'hand'}).status_code == 401)

    as_user(FREE_UID)
    check('credits start empty', CLIENT.get('/api/credits').get_json() == {'hand': 0, 'tourney': 0})
    res = CLIENT.post('/api/ad-token', json={'kind': 'hand'})
    check('no credit means no token',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          str(res.status_code))

    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'hand')
    check('the client can poll for the granted credit',
          CLIENT.get('/api/credits').get_json()['hand'] == 1)
    res = CLIENT.post('/api/ad-token', json={'kind': 'hand'})
    check('a credit buys a token', res.status_code == 200 and res.get_json().get('token'))
    check('and is spent doing so', CLIENT.get('/api/credits').get_json()['hand'] == 0)
    check('an unknown kind is rejected',
          CLIENT.post('/api/ad-token', json={'kind': 'wat'}).status_code == 400)


def main():
    for test in (test_quota, test_credits, test_ad_tokens, test_export_gates,
                 test_export_ads_config,
                 test_history_window, test_import_quota_and_window,
                 test_free_import_prunes_old_tournaments, test_anon_import_and_claim,
                 test_cpx_postback, test_survey_config_hash, test_tally_callback,
                 test_credit_endpoints):
        test()
    print()
    if _FAILURES:
        print(f'tiering: FAIL ({len(_FAILURES)}) — ' + '; '.join(_FAILURES))
        sys.exit(1)
    print('tiering: PASS')


if __name__ == '__main__':
    main()
