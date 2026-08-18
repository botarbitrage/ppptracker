"""
test_admin_users.py — end-to-end shape test for the admin console APIs
(/api/admin/users and /api/admin/users/<uid>/admin) against a fake Firestore
and a fake Firebase Auth, so the handler bodies run without credentials or
network.

The interesting logic is not the listing itself but the guardrails around it:
the permanent admin must stay admin even when /config/admins.uids is empty or
someone tries to untick them, and an unknown uid must never reach the
allowlist. Those are the cases that would lock everyone out of the page.

    python test_admin_users.py
"""

import json
import os
import sys
from datetime import datetime, timezone

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')
# Set before importing app: _PERMANENT_ADMIN_EMAILS is built at import time.
os.environ['PERMANENT_ADMIN_EMAILS'] = 'perm@example.com'

PERM_UID  = 'uid-perm'
ADMIN_UID = 'uid-admin'
PLAIN_UID = 'uid-plain'


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
        from google.cloud import firestore as gcf
        cur = dict(self._store.get(self._path) or {}) if merge else {}
        for k, v in data.items():
            if isinstance(v, gcf.ArrayUnion):
                arr = list(cur.get(k) or [])
                arr += [x for x in v.values if x not in arr]
                cur[k] = arr
            elif isinstance(v, gcf.ArrayRemove):
                cur[k] = [x for x in (cur.get(k) or []) if x not in v.values]
            else:
                cur[k] = v
        self._store.put(self._path, cur)

    def update(self, data):
        cur = dict(self._store.get(self._path) or {})
        cur.update(data)
        self._store.put(self._path, cur)


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))

    def stream(self):
        return self._store.stream(self._path)


class FakeDB:
    """Nested dict keyed by path tuple: ('config', 'admins')."""

    def __init__(self):
        self._d = {}

    def collection(self, name):
        return _Col(self, (name,))

    def get(self, path):
        return self._d.get(path)

    def put(self, path, data):
        self._d[path] = data

    def stream(self, path):
        """Every doc directly under path — mirrors collection.stream()."""
        depth = len(path) + 1
        return [_Snap(p[-1], data) for p, data in self._d.items()
                if len(p) == depth and p[:len(path)] == path]


# ── Fake Firebase Auth ───────────────────────────────────────────────────────

class _UserNotFound(Exception):
    pass


class _Meta:
    def __init__(self, created_ms, last_ms):
        self.creation_timestamp = created_ms
        self.last_sign_in_timestamp = last_ms


class _User:
    def __init__(self, uid, email, created_ms=None, last_ms=None, disabled=False):
        self.uid, self.email, self.disabled = uid, email, disabled
        self.user_metadata = _Meta(created_ms, last_ms)


class _Page:
    """Two pages, so the get_next_page() loop is actually exercised."""

    def __init__(self, users, rest):
        self.users, self._rest = users, rest

    def get_next_page(self):
        return _Page(self._rest, []) if self._rest else None


class FakeAuth:
    UserNotFoundError = _UserNotFound

    def __init__(self, users):
        self._users = users

    def list_users(self):
        return _Page(self._users[:1], self._users[1:])

    def get_user(self, uid):
        for u in self._users:
            if u.uid == uid:
                return u
        raise _UserNotFound(uid)

    def get_user_by_email(self, email):
        for u in self._users:
            if (u.email or '').lower() == email.lower():
                return u
        raise _UserNotFound(email)


USERS = [
    _User(PLAIN_UID, 'zoe@example.com', 1_700_000_000_000, 1_800_000_000_000),
    _User(ADMIN_UID, 'ann@example.com', 1_600_000_000_000, None),
    _User(PERM_UID,  'perm@example.com', 1_500_000_000_000, 1_810_000_000_000,
          disabled=True),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _json(res):
    body = res.get_data(as_text=True)
    try:
        return res.status_code, json.loads(body)
    except ValueError:
        raise AssertionError('non-JSON response (%s): %s'
                             % (res.status_code, body[:300]))


def main():
    import app as A

    db = FakeDB()
    db.put(('config', 'admins'), {'uids': [ADMIN_UID]})

    today = A._utc_day()
    # PLAIN_UID: Pro, active today. ADMIN_UID: free, quota from a stale day (reads
    # as 0 today). PERM_UID: no Firestore doc at all — never loaded the home page,
    # so admin_list_users() must fall back to defaults rather than 500 or KeyError.
    db.put(('users', PLAIN_UID), {
        'is_pro': True,
        'first_seen': datetime(2024, 1, 1, tzinfo=timezone.utc),
        'last_seen': datetime(2024, 6, 1, tzinfo=timezone.utc),
        'quota': {'day': today, 'hand_exports': 2, 'tourney_exports': 1},
    })
    db.put(('users', ADMIN_UID), {
        'is_pro': False,
        'first_seen': datetime(2023, 5, 1, tzinfo=timezone.utc),
        'quota': {'day': '2000-01-01', 'hand_exports': 9, 'tourney_exports': 9},
    })

    A._get_admin_db = lambda: db
    A.admin_auth = FakeAuth(USERS)
    A._PERM_ADMIN_UID_CACHE = None

    caller = {'uid': ADMIN_UID}
    A._verify_bearer = lambda req: caller['uid']

    client = A.app.test_client()
    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    def admins_now():
        return list((db.get(('config', 'admins')) or {}).get('uids') or [])

    def set_admin(uid, value):
        return _json(client.post('/api/admin/users/%s/admin' % uid,
                                 data=json.dumps({'is_admin': value}),
                                 content_type='application/json'))

    def set_pro(uid, value):
        return _json(client.patch('/api/admin/users/%s/pro' % uid,
                                  data=json.dumps({'is_pro': value}),
                                  content_type='application/json'))

    def is_pro_now(uid):
        return bool((db.get(('users', uid)) or {}).get('is_pro'))

    # ── 1. Permanent admin is admin without being in the allowlist ───────────
    check('permanent admin is admin', A._is_admin(PERM_UID) is True)
    check('allowlisted uid is admin', A._is_admin(ADMIN_UID) is True)
    check('plain uid is not admin', A._is_admin(PLAIN_UID) is False)
    check('missing uid is not admin', A._is_admin(None) is False)
    check('permanent admin not written to allowlist', admins_now() == [ADMIN_UID],
          str(admins_now()))

    # ── 2. Gate ──────────────────────────────────────────────────────────────
    for who, label in ((PLAIN_UID, 'non-admin'), (None, 'signed-out')):
        caller['uid'] = who
        status, _ = _json(client.get('/api/admin/users'))
        check('GET users 403 for %s' % label, status == 403, str(status))
        status, _ = set_admin(PLAIN_UID, True)
        check('POST admin 403 for %s' % label, status == 403, str(status))
    check('403s left the allowlist alone', admins_now() == [ADMIN_UID], str(admins_now()))
    caller['uid'] = ADMIN_UID

    # ── 3. Listing ───────────────────────────────────────────────────────────
    status, data = _json(client.get('/api/admin/users'))
    check('GET users 200', status == 200, str(status))
    rows = {u['uid']: u for u in data.get('users', [])}
    check('every auth account listed (both pages)', len(rows) == len(USERS),
          '%d of %d' % (len(rows), len(USERS)))
    check('permanent flagged', rows[PERM_UID]['is_permanent'] is True)
    check('permanent is admin', rows[PERM_UID]['is_admin'] is True)
    check('allowlisted is admin', rows[ADMIN_UID]['is_admin'] is True)
    check('allowlisted not permanent', rows[ADMIN_UID]['is_permanent'] is False)
    check('plain user is not admin', rows[PLAIN_UID]['is_admin'] is False)
    check('disabled flag surfaced', rows[PERM_UID]['disabled'] is True)
    check('timestamps in seconds', rows[PLAIN_UID]['created_at'] == 1_700_000_000,
          str(rows[PLAIN_UID]['created_at']))
    check('missing last sign-in is null', rows[ADMIN_UID]['last_sign_in'] is None)

    # ── 3b. Subscription/activity fields from users/{uid} ────────────────────
    check('pro user flagged', rows[PLAIN_UID]['is_pro'] is True)
    check('free user not flagged', rows[ADMIN_UID]['is_pro'] is False)
    check('user with no Firestore doc defaults to not pro',
          rows[PERM_UID]['is_pro'] is False)
    check('first_seen converted to epoch secs',
          rows[PLAIN_UID]['first_seen'] == 1_704_067_200, str(rows[PLAIN_UID]['first_seen']))
    check('missing first_seen is null', rows[PERM_UID]['first_seen'] is None)
    check("today's exports summed", rows[PLAIN_UID]['exports_today'] == 3,
          str(rows[PLAIN_UID]['exports_today']))
    check('stale-day quota reads as zero exports today',
          rows[ADMIN_UID]['exports_today'] == 0, str(rows[ADMIN_UID]['exports_today']))
    check('user with no Firestore doc has zero exports today',
          rows[PERM_UID]['exports_today'] == 0, str(rows[PERM_UID]['exports_today']))

    order = [u['uid'] for u in data['users']]
    check('admins first, then email A-Z', order == [ADMIN_UID, PERM_UID, PLAIN_UID],
          str(order))

    # ── 4. Promote / demote ──────────────────────────────────────────────────
    status, body = set_admin(PLAIN_UID, True)
    check('promote 200', status == 200, str(status))
    check('promote echoes state', body.get('is_admin') is True, str(body))
    check('promote adds uid', PLAIN_UID in admins_now(), str(admins_now()))

    status, _ = set_admin(PLAIN_UID, True)
    check('promote is idempotent', admins_now().count(PLAIN_UID) == 1, str(admins_now()))

    status, _ = _json(client.get('/api/admin/users'))
    check('promoted uid now admin server-side', A._is_admin(PLAIN_UID) is True)

    status, _ = set_admin(PLAIN_UID, False)
    check('demote 200', status == 200, str(status))
    check('demote removes uid', PLAIN_UID not in admins_now(), str(admins_now()))
    check('demote leaves others alone', ADMIN_UID in admins_now(), str(admins_now()))

    # ── 4b. Manual Pro toggle ─────────────────────────────────────────────────
    check('admin starts free', is_pro_now(ADMIN_UID) is False)
    status, body = set_pro(ADMIN_UID, True)
    check('grant pro 200', status == 200, str(status))
    check('grant pro echoes state', body.get('is_pro') is True, str(body))
    check('grant pro writes doc', is_pro_now(ADMIN_UID) is True)
    # Existing sibling fields on the doc must survive an .update() (no clobber).
    check('grant pro leaves other fields alone',
          (db.get(('users', ADMIN_UID)) or {}).get('first_seen') is not None)

    status, body = set_pro(ADMIN_UID, False)
    check('revoke pro 200', status == 200, str(status))
    check('revoke pro writes doc', is_pro_now(ADMIN_UID) is False)

    # PERM_UID has no users/{uid} doc at all (never loaded the home page) — the
    # endpoint must create one via a merge-set rather than 500 on a missing doc.
    check('perm admin has no doc yet', db.get(('users', PERM_UID)) is None)
    status, body = set_pro(PERM_UID, True)
    check('grant pro for docless user 200', status == 200, str(status))
    check('grant pro for docless user creates doc', is_pro_now(PERM_UID) is True)

    for who, label in ((PLAIN_UID, 'non-admin'), (None, 'signed-out')):
        caller['uid'] = who
        status, _ = set_pro(ADMIN_UID, True)
        check('PATCH pro 403 for %s' % label, status == 403, str(status))
    caller['uid'] = ADMIN_UID

    status, _ = set_pro('uid-does-not-exist', True)
    check('unknown uid pro 404', status == 404, str(status))

    for bad in ('yes', 1, None):
        res = client.patch('/api/admin/users/%s/pro' % PLAIN_UID,
                           data=json.dumps({'is_pro': bad}),
                           content_type='application/json')
        check('non-bool is_pro (%r) 400' % bad, res.status_code == 400,
              str(res.status_code))
    res = client.patch('/api/admin/users/%s/pro' % PLAIN_UID,
                       data='not json', content_type='application/json')
    check('malformed pro body 400', res.status_code == 400, str(res.status_code))

    # ── 5. Guardrails ────────────────────────────────────────────────────────
    before = admins_now()
    status, body = set_admin(PERM_UID, False)
    check('demoting permanent admin 400', status == 400, str(status))
    check('permanent admin error names them', 'perm@example.com' in body.get('error', ''),
          str(body))
    check('permanent admin still admin', A._is_admin(PERM_UID) is True)
    check('failed demote changed nothing', admins_now() == before, str(admins_now()))

    status, _ = set_admin('uid-does-not-exist', True)
    check('unknown uid 404', status == 404, str(status))
    check('unknown uid not added', 'uid-does-not-exist' not in admins_now(),
          str(admins_now()))

    for bad in ('yes', 1, None):
        res = client.post('/api/admin/users/%s/admin' % PLAIN_UID,
                          data=json.dumps({'is_admin': bad}),
                          content_type='application/json')
        check('non-bool is_admin (%r) 400' % bad, res.status_code == 400,
              str(res.status_code))
    res = client.post('/api/admin/users/%s/admin' % PLAIN_UID,
                      data='not json', content_type='application/json')
    check('malformed body 400', res.status_code == 400, str(res.status_code))

    # ── 6. Empty allowlist still leaves the permanent admin in ───────────────
    db.put(('config', 'admins'), {})
    check('permanent admin survives an empty allowlist',
          A._is_admin(PERM_UID) is True)
    check('nobody else is admin then', A._is_admin(ADMIN_UID) is False)
    status, _ = set_admin(ADMIN_UID, True)
    check('emptied allowlist locks the previous admin out', status == 403, str(status))
    # ...but the permanent admin can always let them back in.
    caller['uid'] = PERM_UID
    status, _ = set_admin(ADMIN_UID, True)
    check('permanent admin can restore an admin', status == 200, str(status))
    check('allowlist doc rebuilt from empty', admins_now() == [ADMIN_UID],
          str(admins_now()))

    # ── 7. A permanent admin who hasn't signed up yet isn't cached away ──────
    A._PERM_ADMIN_UID_CACHE = None
    A.admin_auth = FakeAuth([u for u in USERS if u.uid != PERM_UID])
    check('unregistered permanent admin is not admin', A._is_admin(PERM_UID) is False)
    check('unresolved lookup is not cached', A._PERM_ADMIN_UID_CACHE is None,
          str(A._PERM_ADMIN_UID_CACHE))
    A.admin_auth = FakeAuth(USERS)  # they sign up — no restart needed
    check('permanent admin works once they register', A._is_admin(PERM_UID) is True)
    check('resolved lookup is cached', A._PERM_ADMIN_UID_CACHE == {PERM_UID},
          str(A._PERM_ADMIN_UID_CACHE))

    # ── 8. Pricing plan ──────────────────────────────────────────────────────
    caller['uid'] = ADMIN_UID
    db.put(('config', 'admins'), {'uids': [ADMIN_UID]})
    os.environ['STRIPE_PRICE_ID'] = 'price_early'
    os.environ.pop('STRIPE_PRO_PRICE_ID', None)

    def active_plan():
        return (db.get(('config', 'pricing')) or {}).get('active_plan')

    status, data = _json(client.get('/api/pricing'))
    check('public pricing 200', status == 200, str(status))
    check('defaults to early access', data.get('plan') == 'early_access', str(data))
    check('public pricing carries a label', data.get('label') == 'Early Access', str(data))

    status, data = _json(client.get('/api/admin/pricing'))
    check('admin pricing 200', status == 200, str(status))
    by_key = {p['key']: p for p in data.get('plans', [])}
    check('early access is stripe-configured', by_key['early_access']['stripe_configured'] is True)
    check('pro is not stripe-configured', by_key['pro']['stripe_configured'] is False)

    # Switching to a plan with no Stripe price would 503 every checkout.
    status, body = _json(client.post('/api/admin/pricing',
                                     data=json.dumps({'plan': 'pro'}),
                                     content_type='application/json'))
    check('unconfigured plan rejected', status == 400, str(status))
    check('rejection names the env var', 'STRIPE_PRO_PRICE_ID' in body.get('error', ''), str(body))
    check('rejected switch did not write', active_plan() is None, str(active_plan()))

    os.environ['STRIPE_PRO_PRICE_ID'] = 'price_pro'
    status, _ = _json(client.post('/api/admin/pricing',
                                  data=json.dumps({'plan': 'pro'}),
                                  content_type='application/json'))
    check('configured plan accepted', status == 200, str(status))
    check('active plan persisted', active_plan() == 'pro', str(active_plan()))
    status, data = _json(client.get('/api/pricing'))
    check('public pricing follows the switch', data.get('plan') == 'pro', str(data))

    status, _ = _json(client.post('/api/admin/pricing',
                                  data=json.dumps({'plan': 'nope'}),
                                  content_type='application/json'))
    check('unknown plan rejected', status == 400, str(status))

    caller['uid'] = PLAIN_UID
    status, _ = _json(client.get('/api/admin/pricing'))
    check('non-admin cannot read pricing config', status == 403, str(status))
    status, _ = _json(client.post('/api/admin/pricing',
                                  data=json.dumps({'plan': 'early_access'}),
                                  content_type='application/json'))
    check('non-admin cannot switch plan', status == 403, str(status))
    check('non-admin switch did not write', active_plan() == 'pro', str(active_plan()))
    caller['uid'] = ADMIN_UID

    for p in problems:
        print('  FAIL', p)
    print('admin users API: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
