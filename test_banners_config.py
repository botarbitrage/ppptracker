"""
test_banners_config.py — shape and guardrail test for the promo banner config
APIs (/api/banners-config and /api/admin/banners-config) against a fake
Firestore, so the handler bodies run without credentials or network.

The interesting logic here is not the read/write itself but what the two slots
do at the edges.

The shipped defaults point at the placeholder art in static/banners/, so this
checks those paths are both valid URLs and real files — art renamed without
_BANNERS_DEFAULTS being updated would otherwise ship broken <img> tags to
every visitor. Zero images is still a supported state (the banners' ACs call
it "gracefully degrades when zero images are configured"), but it is now only
reachable by an admin clearing the fields, so it gets its own coverage: a
stored empty value must win over the non-empty default.

The public GET is unauthenticated and feeds an <img src> on every visitor's
page, so a hand-edited or part-written doc has to be sanitised on read rather
than passed through.

    python test_banners_config.py
"""

import json
import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')
# No permanent admin here: admin-ness is granted through /config/admins below,
# and an empty list keeps _permanent_admin_uids() off the Firebase Auth path
# this harness has no app for. test_admin_users.py covers the permanent admin.
os.environ['PERMANENT_ADMIN_EMAILS'] = ''

ADMIN_UID = 'uid-admin'
PLAIN_UID = 'uid-plain'


# ── Fake Firestore (same shape as test_admin_users.py's, minus the bits the
#    banner routes never touch: no streaming, no ArrayUnion/ArrayRemove) ──────

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


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))


class FakeDB:
    """Nested dict keyed by path tuple: ('config', 'banners')."""

    def __init__(self):
        self._d = {}

    def collection(self, name):
        return _Col(self, (name,))

    def get(self, path):
        return self._d.get(path)

    def put(self, path, data):
        self._d[path] = data


class BoomDB:
    """Every read raises — _banners_config() must fall back to the defaults
    rather than propagate, same as _export_ads_config()."""

    def collection(self, name):
        raise RuntimeError('firestore is down')


def _json(res):
    body = res.get_data(as_text=True)
    try:
        return res.status_code, json.loads(body)
    except ValueError:
        raise AssertionError('non-JSON response (%s): %s'
                             % (res.status_code, body[:300]))


def main():
    import app as A

    # Every URL the shipped defaults reference, flattened.
    global _BANNER_DEFAULT_URLS
    _BANNER_DEFAULT_URLS = (list(A._BANNERS_DEFAULTS['side_images'])
                            + [A._BANNERS_DEFAULTS['mid_image']])

    db = FakeDB()
    A._get_admin_db = lambda: db
    db.put(('config', 'admins'), {'uids': [ADMIN_UID]})

    caller = {'uid': ADMIN_UID}
    A._verify_bearer = lambda req: caller['uid']

    client = A.app.test_client()
    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    def post(payload):
        return _json(client.post('/api/admin/banners-config',
                                 data=json.dumps(payload),
                                 content_type='application/json'))

    def stored():
        return dict(db.get(('config', 'banners')) or {})

    # ── 1. _valid_banner_url ─────────────────────────────────────────────────
    for val, want in [
        ('https://x.com/a.png', True), ('http://x.com/a.png', True),
        ('/static/a.png', True), ('  https://x.com/a.png  ', True),
        ('', False), ('   ', False), (None, False), (123, False), (True, False),
        ('ftp://x/a.png', False), ('javascript:alert(1)', False),
        ('a.png', False), ('x' * 2001, False),
    ]:
        check('_valid_banner_url(%r)' % (val,), A._valid_banner_url(val) is want)

    # ── 2. Shipped default: both slots point at the placeholder art ──────────
    cfg = A._banners_config()
    check('default rotates 3 side images', len(cfg['side_images']) == 3, str(cfg['side_images']))
    check('default mid_image set', cfg['mid_image'] != '', repr(cfg['mid_image']))
    check('default interval is 6s', cfg['side_interval_ms'] == 6000, str(cfg['side_interval_ms']))

    # Every default must survive the sanitiser it is fed through on read —
    # a default that _valid_banner_url rejects would silently vanish.
    for u in _BANNER_DEFAULT_URLS:
        check('default URL is valid: ' + u, A._valid_banner_url(u))

    # …and must actually exist on disk. This is the check that catches art
    # renamed or deleted without _BANNERS_DEFAULTS being updated, which would
    # otherwise ship broken <img> tags to every visitor.
    here = os.path.dirname(os.path.abspath(__file__))
    for u in _BANNER_DEFAULT_URLS:
        path = os.path.join(here, u.lstrip('/'))
        check('default art exists: ' + u, os.path.isfile(path), path)

    # A failed read must land on those same defaults, not raise.
    A._get_admin_db = lambda: BoomDB()
    cfg = A._banners_config()
    check('read failure falls back to defaults',
          cfg['side_images'] == A._BANNERS_DEFAULTS['side_images']
          and cfg['mid_image'] == A._BANNERS_DEFAULTS['mid_image']
          and cfg['side_interval_ms'] == 6000, str(cfg))
    A._get_admin_db = lambda: db

    # ── 3. Public GET is unauthenticated and mirrors the config ──────────────
    caller['uid'] = None
    status, body = _json(client.get('/api/banners-config'))
    check('public GET is open', status == 200, str(status))
    check('public GET serves the shipped defaults',
          body['side_images'] == A._BANNERS_DEFAULTS['side_images']
          and body['mid_image'] == A._BANNERS_DEFAULTS['mid_image'], str(body))
    caller['uid'] = ADMIN_UID

    # ── 4. Admin routes are gated ────────────────────────────────────────────
    caller['uid'] = PLAIN_UID
    status, _ = _json(client.get('/api/admin/banners-config'))
    check('non-admin cannot read banners config', status == 403, str(status))
    status, _ = post({'mid_image': 'https://evil.example/x.png'})
    check('non-admin cannot write banners config', status == 403, str(status))
    check('non-admin write did not persist', stored() == {}, str(stored()))
    caller['uid'] = ADMIN_UID

    # ── 5. Happy path ────────────────────────────────────────────────────────
    status, body = post({
        'side_images': ['https://x.com/a.png', '/static/b.png'],
        'side_interval_ms': 8000,
        'mid_image': 'https://x.com/wide.png',
    })
    check('save accepted', status == 200, str(body))
    check('side_images round-trip',
          body['side_images'] == ['https://x.com/a.png', '/static/b.png'], str(body))
    check('interval round-trip', body['side_interval_ms'] == 8000, str(body))
    check('mid_image round-trip', body['mid_image'] == 'https://x.com/wide.png', str(body))
    check('write is attributed', 'updated_by' in stored() and 'updated_at' in stored(),
          str(stored()))

    # Public GET now serves what the admin saved.
    status, body = _json(client.get('/api/banners-config'))
    check('public GET reflects the save',
          body['mid_image'] == 'https://x.com/wide.png'
          and len(body['side_images']) == 2, str(body))

    # ── 6. PATCH-style merge: one field at a time ────────────────────────────
    status, body = post({'mid_image': ''})
    check('mid_image cleared', status == 200 and body['mid_image'] == '', str(body))
    check('clearing mid_image left side_images alone',
          body['side_images'] == ['https://x.com/a.png', '/static/b.png'], str(body))

    # ── 6b. An admin can still turn both slots off ───────────────────────────
    # This is the "gracefully degrades with zero images" AC. It used to be the
    # shipped default; now that the defaults point at real placeholder art it
    # is only reachable by an admin clearing the fields, so it needs its own
    # coverage: a stored empty value must WIN over the non-empty default
    # rather than falling back to it.
    status, body = post({'side_images': [], 'mid_image': ''})
    check('both slots can be cleared',
          status == 200 and body['side_images'] == [] and body['mid_image'] == '',
          str(body))
    cfg = A._banners_config()
    check('cleared state wins over the defaults on re-read',
          cfg['side_images'] == [] and cfg['mid_image'] == '', str(cfg))
    status, body = _json(client.get('/api/banners-config'))
    check('public GET serves the cleared state',
          body['side_images'] == [] and body['mid_image'] == '', str(body))

    # Put a real config back for the validation cases below.
    post({'side_images': ['https://x.com/a.png', '/static/b.png'],
          'mid_image': 'https://x.com/wide.png'})

    # ── 7. Validation ────────────────────────────────────────────────────────
    for label, payload in [
        ('non-list side_images',      {'side_images': 'https://x.com/a.png'}),
        ('bad URL scheme in list',    {'side_images': ['javascript:alert(1)']}),
        ('relative URL in list',      {'side_images': ['a.png']}),
        ('non-string in list',        {'side_images': [123]}),
        ('too many images',           {'side_images': ['/a.png'] * 11}),
        ('bad mid_image URL',         {'mid_image': 'ftp://x/a.png'}),
        ('non-string mid_image',      {'mid_image': 42}),
        ('interval too small',        {'side_interval_ms': 10}),
        ('interval too large',        {'side_interval_ms': 999999}),
        ('interval not an int',       {'side_interval_ms': '8000'}),
        ('interval bool',             {'side_interval_ms': True}),
        ('no recognised fields',      {'nonsense': 1}),
    ]:
        status, body = post(payload)
        check('rejected: ' + label, status == 400, '%s %s' % (status, body))

    status, _ = _json(client.post('/api/admin/banners-config',
                                  data='not json', content_type='application/json'))
    check('rejected: malformed body', status == 400, str(status))

    # A rejected save must not have touched the stored config.
    check('rejected saves did not persist',
          stored()['side_images'] == ['https://x.com/a.png', '/static/b.png'],
          str(stored()))

    # ── 8. Sanitising a corrupt stored doc on read ───────────────────────────
    # Nothing in the routes above can produce this, but a hand-edited doc can,
    # and the public GET feeds an <img src> for every visitor.
    db.put(('config', 'banners'), {
        'side_images': ['https://good.example/a.png', 'javascript:alert(1)',
                        '', 42, None, '/static/ok.png'],
        'side_interval_ms': 'soon',
        'mid_image': 'not-a-url',
    })
    cfg = A._banners_config()
    check('bad entries dropped from side_images',
          cfg['side_images'] == ['https://good.example/a.png', '/static/ok.png'],
          str(cfg['side_images']))
    check('bad interval falls back to default', cfg['side_interval_ms'] == 6000,
          str(cfg['side_interval_ms']))
    check('bad mid_image cleared', cfg['mid_image'] == '', repr(cfg['mid_image']))

    db.put(('config', 'banners'), {'side_images': 'not-a-list'})
    check('non-list side_images reads as empty', A._banners_config()['side_images'] == [],
          str(A._banners_config()['side_images']))

    db.put(('config', 'banners'), {'side_images': ['/static/%d.png' % i for i in range(50)]})
    check('over-long stored list is capped',
          len(A._banners_config()['side_images']) == A._BANNER_MAX_IMAGES,
          str(len(A._banners_config()['side_images'])))

    for p in problems:
        print('  FAIL', p)
    print('banners config API: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
