"""
test_banners_config.py — shape and guardrail test for the promo banner config
APIs (/api/banners-config and /api/admin/banners-config) against a fake
Firestore, so the handler bodies run without credentials or network.

The images in both slots are no longer stored here as URLs: they come from the
ad media library ('banner_horizontal' and 'banner_vertical' in
_AD_MEDIA_TYPES), and _banners_config() resolves the current selection into the
{mid_image, side_images} shape the main page has always consumed. So what this
covers is the resolution: shipped defaults render out of the box, an uploaded
file resolves to its stream URL, and a deselected slot goes empty.

Zero images is still a supported state (the banners' ACs call it "gracefully
degrades when zero images are configured"), reachable by an admin deselecting
every slide, so it gets its own coverage.

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

    HORZ = A._ad_media_defaults(A._AD_MEDIA_TYPES['banner_horizontal'])
    VERT = A._ad_media_defaults(A._AD_MEDIA_TYPES['banner_vertical'])
    default_urls = [d['path'] for d in HORZ + VERT]

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

    def select(media_type, payload):
        return _json(client.post(f'/api/admin/ad-media/{media_type}/active',
                                 data=json.dumps(payload),
                                 content_type='application/json'))

    def stored():
        return dict(db.get(('config', 'banners')) or {})

    # ── 1. Shipped defaults populate both slots out of the box ──────────────
    cfg = A._banners_config()
    check('default rotates 3 side images', len(cfg['side_images']) == 3, str(cfg['side_images']))
    check('default mid_image set', cfg['mid_image'] != '', repr(cfg['mid_image']))
    check('default interval is 6s', cfg['side_interval_ms'] == 6000, str(cfg['side_interval_ms']))
    check('side_images resolve to the shipped slide art',
          cfg['side_images'] == [d['path'] for d in VERT], str(cfg['side_images']))
    check('mid_image resolves to the shipped art',
          cfg['mid_image'] == HORZ[0]['path'], repr(cfg['mid_image']))

    # …and that art must actually exist on disk. This is the check that catches
    # art renamed or deleted without _AD_MEDIA_TYPES being updated, which would
    # otherwise ship broken <img> tags to every visitor.
    here = os.path.dirname(os.path.abspath(__file__))
    for u in default_urls:
        check('default art exists: ' + u, os.path.isfile(os.path.join(here, u.lstrip('/'))), u)

    # A failed read must land on those same defaults, not raise.
    A._get_admin_db = lambda: BoomDB()
    cfg = A._banners_config()
    check('read failure falls back to defaults',
          cfg['side_images'] == [d['path'] for d in VERT]
          and cfg['mid_image'] == HORZ[0]['path']
          and cfg['side_interval_ms'] == 6000, str(cfg))
    A._get_admin_db = lambda: db

    # ── 2. Public GET is unauthenticated and mirrors the config ─────────────
    caller['uid'] = None
    status, body = _json(client.get('/api/banners-config'))
    check('public GET is open', status == 200, str(status))
    check('public GET serves the shipped defaults',
          body['side_images'] == [d['path'] for d in VERT]
          and body['mid_image'] == HORZ[0]['path'], str(body))
    caller['uid'] = ADMIN_UID

    # ── 3. Admin routes are gated ───────────────────────────────────────────
    caller['uid'] = PLAIN_UID
    status, _ = _json(client.get('/api/admin/banners-config'))
    check('non-admin cannot read banners config', status == 403, str(status))
    status, _ = post({'side_interval_ms': 9000})
    check('non-admin cannot write banners config', status == 403, str(status))
    check('non-admin write did not persist', stored() == {}, str(stored()))
    caller['uid'] = ADMIN_UID

    # ── 4. The images follow the media-library selection ────────────────────
    # An uploaded file resolves to its stream URL rather than a stored path.
    db.put(('config', 'ad_media'), {
        'banner_horizontal': {
            'files': [{'id': 'up1', 'path': 'ad_media/banner_horizontal/up1.png',
                       'filename': 'wide.png', 'content_type': 'image/png',
                       'size': 10, 'duration': None, 'uploaded_at': 0, 'uploaded_by': ADMIN_UID}],
            'active': 'up1',
        },
    })
    cfg = A._banners_config()
    check('mid_image resolves an uploaded file to its stream URL',
          cfg['mid_image'] == '/api/ad-media/banner_horizontal/up1', repr(cfg['mid_image']))

    status, body = select('banner_horizontal', {'file_id': 'default'})
    check('horizontal can go back to the default', status == 200, str(body))
    check('mid_image back to the shipped art',
          A._banners_config()['mid_image'] == HORZ[0]['path'],
          repr(A._banners_config()['mid_image']))

    # Each slide is selected on its own, and the rotation follows suit.
    slide_ids = [d['id'] for d in VERT]
    status, body = select('banner_vertical', {'file_ids': [slide_ids[0], slide_ids[2]]})
    check('slide subset accepted', status == 200, str(body))
    check('side_images follow the slide selection',
          A._banners_config()['side_images'] == [VERT[0]['path'], VERT[2]['path']],
          str(A._banners_config()['side_images']))

    # ── 4b. An admin can still turn both slots off ──────────────────────────
    # This is the "gracefully degrades with zero images" AC. It used to be the
    # shipped default; now that the defaults point at real placeholder art it
    # is only reachable by an admin deselecting everything, so it needs its own
    # coverage: an empty selection must WIN over the non-empty default rather
    # than falling back to it.
    select('banner_vertical', {'file_ids': []})
    status, body = select('banner_horizontal', {'file_id': 'none'})
    check('horizontal slot can be switched off', status == 200, str(body))
    cfg = A._banners_config()
    check('side slot can be emptied', cfg['side_images'] == [], str(cfg))
    check('mid slot can be hidden', cfg['mid_image'] == '', repr(cfg['mid_image']))
    status, body = _json(client.get('/api/banners-config'))
    check('public GET serves both slots off',
          body['side_images'] == [] and body['mid_image'] == '', str(body))

    # 'none' is only offered to the page slots — a gate banner always shows.
    status, body = select('banner_a', {'file_id': 'none'})
    check('gate banner cannot be switched off', status == 400, str(body))

    select('banner_vertical', {'file_ids': slide_ids})   # restore for the rest
    select('banner_horizontal', {'file_id': 'default'})

    # ── 5. Rotation speed is the only thing this route still writes ─────────
    status, body = post({'side_interval_ms': 8000})
    check('interval save accepted', status == 200, str(body))
    check('interval round-trip', body['side_interval_ms'] == 8000, str(body))
    check('write is attributed', 'updated_by' in stored() and 'updated_at' in stored(),
          str(stored()))

    status, body = _json(client.get('/api/banners-config'))
    check('public GET reflects the saved interval', body['side_interval_ms'] == 8000, str(body))

    # ── 6. Validation ───────────────────────────────────────────────────────
    for label, payload in [
        ('interval too small',        {'side_interval_ms': 10}),
        ('interval too large',        {'side_interval_ms': 999999}),
        ('interval not an int',       {'side_interval_ms': '8000'}),
        ('interval bool',             {'side_interval_ms': True}),
        ('no recognised fields',      {'nonsense': 1}),
        # The image fields moved to the media library — this route must not
        # quietly accept and store them any more.
        ('side_images no longer accepted', {'side_images': ['/static/a.png']}),
        ('mid_image no longer accepted',   {'mid_image': 'https://x.com/wide.png'}),
    ]:
        status, body = post(payload)
        check('rejected: ' + label, status == 400, '%s %s' % (status, body))

    status, _ = _json(client.post('/api/admin/banners-config',
                                  data='not json', content_type='application/json'))
    check('rejected: malformed body', status == 400, str(status))

    # A rejected save must not have touched the stored config.
    check('rejected saves did not persist', stored()['side_interval_ms'] == 8000, str(stored()))

    # ── 7. Sanitising a corrupt stored doc on read ──────────────────────────
    # Nothing in the routes above can produce this, but a hand-edited doc can,
    # and the public GET feeds an <img src> for every visitor.
    db.put(('config', 'banners'), {'side_interval_ms': 'soon'})
    check('bad interval falls back to default',
          A._banners_config()['side_interval_ms'] == 6000,
          str(A._banners_config()['side_interval_ms']))

    db.put(('config', 'banners'), {'side_interval_ms': 999999})
    check('out-of-range interval falls back to default',
          A._banners_config()['side_interval_ms'] == 6000,
          str(A._banners_config()['side_interval_ms']))

    # A hand-written selection naming files that no longer exist resolves to
    # nothing rather than a broken <img src>.
    db.put(('config', 'ad_media'), {
        'banner_vertical': {'files': [], 'active': ['no-such-slide']},
        'banner_horizontal': {'files': [], 'active': 'no-such-file'},
    })
    cfg = A._banners_config()
    check('unknown slide ids drop out of the rotation', cfg['side_images'] == [],
          str(cfg['side_images']))
    # A dangling single-select id falls back to the shipped art rather than
    # blanking the slot — hiding it is an explicit 'none', not a typo.
    check('unknown horizontal id falls back to the default',
          cfg['mid_image'] == HORZ[0]['path'], repr(cfg['mid_image']))

    db.put(('config', 'ad_media'), {'banner_vertical': {'active': 'not-a-list'}})
    check('non-list slide selection reads as the shipped rotation',
          A._banners_config()['side_images'] == [d['path'] for d in VERT],
          str(A._banners_config()['side_images']))

    for p in problems:
        print('  FAIL', p)
    print('banners config API: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
