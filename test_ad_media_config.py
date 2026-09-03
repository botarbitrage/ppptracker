"""
test_ad_media_config.py — shape and guardrail test for the ad media library
APIs (/api/admin/ad-media-config, .../upload, .../active, DELETE .../<id>,
and the public /api/ad-media/<type>/<id> stream) against a fake Firestore +
fake Storage bucket, so the handler bodies run without credentials or network.

Video duration checking (_video_duration_seconds, backed by mutagen) is
monkeypatched rather than fed a real MP4 fixture — the interesting logic
under test is the route's response to a given duration (accept/reject against
target +/- tolerance), not mutagen's MP4 parsing itself.

    python test_ad_media_config.py
"""

import io
import json
import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')
os.environ['PERMANENT_ADMIN_EMAILS'] = ''

ADMIN_UID = 'uid-admin'
PLAIN_UID = 'uid-plain'


# ── Fake Firestore (same shape as test_banners_config.py's) ───────────────────

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
    def __init__(self):
        self._d = {}

    def collection(self, name):
        return _Col(self, (name,))

    def get(self, path):
        return self._d.get(path)

    def put(self, path, data):
        self._d[path] = data


class BoomDB:
    def collection(self, name):
        raise RuntimeError('firestore is down')


# ── Fake Cloud Storage bucket ──────────────────────────────────────────────────

class _FakeBlob:
    def __init__(self, bucket, path):
        self._bucket, self.path = bucket, path

    def upload_from_string(self, data, content_type=None):
        self._bucket.store[self.path] = (data, content_type)

    def download_as_bytes(self):
        if self.path not in self._bucket.store:
            raise RuntimeError('no such blob: ' + self.path)
        return self._bucket.store[self.path][0]

    def delete(self):
        self._bucket.store.pop(self.path, None)


class FakeBucket:
    def __init__(self):
        self.store = {}   # path -> (bytes, content_type)

    def blob(self, path):
        return _FakeBlob(self, path)


def _json(res):
    body = res.get_data(as_text=True)
    try:
        return res.status_code, json.loads(body)
    except ValueError:
        raise AssertionError('non-JSON response (%s): %s' % (res.status_code, body[:300]))


def main():
    import app as A

    db = FakeDB()
    bucket = FakeBucket()
    A._get_admin_db = lambda: db
    A._get_admin_bucket = lambda: bucket
    db.put(('config', 'admins'), {'uids': [ADMIN_UID]})

    caller = {'uid': ADMIN_UID}
    A._verify_bearer = lambda req: caller['uid']

    client = A.app.test_client()
    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    def get_cfg():
        return _json(client.get('/api/admin/ad-media-config'))

    def get_public_cfg():
        return _json(client.get('/api/ad-media-config'))

    def upload(media_type, filename, data, content_type):
        return _json(client.post(
            f'/api/admin/ad-media/{media_type}/upload',
            data={'file': (io.BytesIO(data), filename, content_type)},
            content_type='multipart/form-data'))

    def set_active(media_type, file_id):
        return _json(client.post(
            f'/api/admin/ad-media/{media_type}/active',
            data=json.dumps({'file_id': file_id}), content_type='application/json'))

    def set_slides(media_type, file_ids):
        return _json(client.post(
            f'/api/admin/ad-media/{media_type}/active',
            data=json.dumps({'file_ids': file_ids}), content_type='application/json'))

    def delete(media_type, file_id):
        return _json(client.delete(f'/api/admin/ad-media/{media_type}/{file_id}'))

    def stored(media_type):
        return dict((db.get(('config', 'ad_media')) or {}).get(media_type) or {})

    # ── 1. Shipped defaults exist on disk ───────────────────────────────────
    # Every default path any type declares, so art renamed or deleted without
    # _AD_MEDIA_TYPES being updated fails here rather than shipping a broken
    # <img> to every visitor (the two banner_* types feed the main page).
    here = os.path.dirname(os.path.abspath(__file__))
    for media_type, spec in A._AD_MEDIA_TYPES.items():
        for d in A._ad_media_defaults(spec):
            check('default art exists (%s): %s' % (media_type, d['path']),
                  os.path.isfile(os.path.join(here, d['path'].lstrip('/'))), d['path'])
    for media_type in ('banner_a', 'banner_b', 'banner_horizontal'):
        check('%s has one default' % media_type,
              len(A._ad_media_defaults(A._AD_MEDIA_TYPES[media_type])) == 1)
    check('banner_vertical ships 3 selectable slides',
          len(A._ad_media_defaults(A._AD_MEDIA_TYPES['banner_vertical'])) == 3)
    for media_type in ('video_30', 'video_60'):
        check('no bundled default for ' + media_type,
              A._ad_media_defaults(A._AD_MEDIA_TYPES[media_type]) == [])

    # ── 2. Fresh config: every type starts unselected-but-valid, no files ────
    # Single-select types sit on 'default'; the multi type starts with every
    # shipped slide selected, which is the rotation the side slot used to
    # hard-code as URLs.
    def _fresh_ok(entry, spec):
        if spec.get('multi'):
            return entry['active'] == [d['id'] for d in A._ad_media_defaults(spec)]
        return entry['active'] == 'default'

    cfg = A._ad_media_config()
    for media_type, spec in A._AD_MEDIA_TYPES.items():
        check('fresh %s starts empty' % media_type,
              cfg[media_type]['files'] == [] and _fresh_ok(cfg[media_type], spec),
              str(cfg[media_type]))

    # A failed read must land on that same empty shape, not raise.
    A._get_admin_db = lambda: BoomDB()
    cfg = A._ad_media_config()
    check('read failure falls back to empty shape',
          all(cfg[t]['files'] == [] and _fresh_ok(cfg[t], A._AD_MEDIA_TYPES[t])
              for t in A._AD_MEDIA_TYPES),
          str(cfg))
    A._get_admin_db = lambda: db

    # ── 2b. Multi-select: each slide is picked on its own ────────────────────
    slide_ids = [d['id'] for d in A._ad_media_defaults(A._AD_MEDIA_TYPES['banner_vertical'])]
    status, body = set_slides('banner_vertical', [slide_ids[0], slide_ids[2]])
    check('slide subset accepted', status == 200, str(body))
    check('only the picked slides stay selected',
          body['banner_vertical']['active'] == [slide_ids[0], slide_ids[2]],
          str(body.get('banner_vertical')))

    # Click order must not change rotation order — it follows the slot's own
    # defaults-then-uploads order so the carousel is deterministic.
    status, body = set_slides('banner_vertical', [slide_ids[2], slide_ids[0]])
    check('selection order is normalised',
          body['banner_vertical']['active'] == [slide_ids[0], slide_ids[2]],
          str(body.get('banner_vertical')))

    status, body = set_slides('banner_vertical', [])
    check('every slide can be deselected',
          status == 200 and body['banner_vertical']['active'] == [], str(body))
    check('an empty selection is stored, not treated as unset',
          A._ad_media_config()['banner_vertical']['active'] == [],
          str(A._ad_media_config()['banner_vertical']))

    status, body = set_slides('banner_vertical', ['no-such-id'])
    check('unknown slide id rejected', status == 400, str(body))
    status, body = set_slides('banner_vertical', 'not-a-list')
    check('non-list file_ids rejected', status == 400, str(body))
    status, body = set_active('banner_vertical', slide_ids[0])
    check('multi type rejects a single file_id', status == 400, str(body))

    # Defaults are not deletable, whichever id shape they use.
    status, body = delete('banner_vertical', slide_ids[1])
    check('a default slide cannot be deleted', status == 400, str(body))

    set_slides('banner_vertical', slide_ids)   # back to the shipped rotation

    # ── 3. Admin routes are gated; the public config mirror is not ───────────
    caller['uid'] = PLAIN_UID
    status, _ = get_cfg()
    check('non-admin cannot read config', status == 403, str(status))

    caller['uid'] = None   # unauthenticated — same audience as /api/banners-config
    status, public_cfg = get_public_cfg()
    check('public config readable while signed out', status == 200, str(status))
    admin_cfg = A._ad_media_config()
    check('public config matches the server-side shape', public_cfg == admin_cfg,
          str((public_cfg, admin_cfg)))
    caller['uid'] = PLAIN_UID
    status, _ = upload('banner_a', 'a.png', b'\x89PNG-fake-bytes', 'image/png')
    check('non-admin cannot upload', status == 403, str(status))
    status, _ = set_active('banner_a', 'default')
    check('non-admin cannot set active', status == 403, str(status))
    status, _ = delete('banner_a', 'default')
    check('non-admin cannot delete', status == 403, str(status))
    caller['uid'] = ADMIN_UID

    # ── 4. Unknown media type ─────────────────────────────────────────────────
    status, _ = upload('banner_z', 'a.png', b'x', 'image/png')
    check('unknown media type rejected on upload', status == 404, str(status))
    status, _ = set_active('banner_z', 'default')
    check('unknown media type rejected on set-active', status == 404, str(status))

    # ── 5. Image upload: happy path + validation ──────────────────────────────
    status, body = upload('banner_a', 'promo.png', b'0' * 1000, 'image/png')
    check('image upload accepted', status == 200, str(body))
    file_id = body['banner_a']['files'][0]['id'] if status == 200 else None
    check('uploaded file recorded', file_id and stored('banner_a').get('files'), str(stored('banner_a')))
    check('active unchanged after upload (still default)',
          body.get('banner_a', {}).get('active') == 'default', str(body))

    status, body = upload('banner_a', 'x.exe', b'0' * 10, 'application/octet-stream')
    check('rejected: bad content type', status == 400, str(body))

    oversize = A._AD_MEDIA_TYPES['banner_a']['max_bytes'] + 1
    status, body = upload('banner_a', 'big.png', b'0' * oversize, 'image/png')
    check('rejected: oversize image', status == 400, str(body))

    status, body = upload('banner_a', 'empty.png', b'', 'image/png')
    check('rejected: empty file', status == 400, str(body))

    # ── 6. Video upload: duration validated via monkeypatched reader ─────────
    orig_duration_fn = A._video_duration_seconds
    A._video_duration_seconds = lambda data: 30.5
    status, body = upload('video_30', 'ad.mp4', b'0' * 1000, 'video/mp4')
    check('video within tolerance accepted', status == 200, str(body))
    video_file_id = body['video_30']['files'][0]['id'] if status == 200 else None
    check('video duration stored',
          video_file_id and body['video_30']['files'][0]['duration'] == 30.5, str(body))

    A._video_duration_seconds = lambda data: 45.0
    status, body = upload('video_30', 'wrong-length.mp4', b'0' * 1000, 'video/mp4')
    check('video outside tolerance rejected', status == 400, str(body))

    A._video_duration_seconds = lambda data: None
    status, body = upload('video_30', 'not-really-mp4.mp4', b'0' * 1000, 'video/mp4')
    check('unreadable video duration rejected', status == 400, str(body))
    A._video_duration_seconds = orig_duration_fn

    # ── 7. Real (best-effort) _video_duration_seconds on garbage bytes ───────
    check('_video_duration_seconds(garbage) is None', A._video_duration_seconds(b'not an mp4') is None)

    # ── 8. Max files cap: 4 admin uploads OK, 5th rejected ────────────────────
    A._get_admin_db().collection('config').document('ad_media').set(
        {'banner_b': {'files': [], 'active': 'default'}}, merge=True)
    ids = []
    for i in range(A._AD_MEDIA_MAX_FILES):
        status, body = upload('banner_b', f'b{i}.png', b'0' * 100, 'image/png')
        check('slot %d upload accepted' % i, status == 200, str(body))
        if status == 200:
            ids.append(body['banner_b']['files'][-1]['id'])
    status, body = upload('banner_b', 'overflow.png', b'0' * 100, 'image/png')
    check('5th upload rejected (cap is %d)' % A._AD_MEDIA_MAX_FILES, status == 400, str(body))

    # ── 9. Set active ──────────────────────────────────────────────────────
    status, body = set_active('banner_b', ids[0])
    check('set active to a real file id', status == 200 and body['banner_b']['active'] == ids[0], str(body))
    status, body = set_active('banner_b', 'default')
    check('set active back to default', status == 200 and body['banner_b']['active'] == 'default', str(body))
    status, body = set_active('banner_b', 'no-such-id')
    check('set active rejects unknown id', status == 400, str(body))
    status, body = set_active('video_30', 'default')
    check("set active rejects 'default' for a type with no default file", status == 400, str(body))

    # ── 10. Delete ─────────────────────────────────────────────────────────
    status, _ = delete('banner_b', 'default')
    check('cannot delete the default entry', status == 400, str(status))
    status, body = set_active('banner_b', ids[1])
    check('setup: active set to ids[1]', status == 200, str(body))
    status, body = delete('banner_b', ids[1])
    check('delete an active file succeeds', status == 200, str(body))
    check('deleting the active file resets active to default',
          body['banner_b']['active'] == 'default', str(body))
    check('deleted file no longer listed',
          all(f['id'] != ids[1] for f in body['banner_b']['files']), str(body))
    status, _ = delete('banner_b', ids[1])
    check('deleting an already-gone id 404s', status == 404, str(status))

    # ── 11. Public streaming route ────────────────────────────────────────
    caller['uid'] = None   # unauthenticated
    res = client.get(f'/api/ad-media/banner_a/{file_id}')
    check('public stream serves the uploaded bytes', res.status_code == 200 and res.data == b'0' * 1000,
          str(res.status_code))
    check('public stream sets content type', res.mimetype == 'image/png', res.mimetype)
    status, _ = _json(client.get('/api/ad-media/banner_a/no-such-id'))
    check('public stream 404s on unknown id', status == 404, str(status))
    status, _ = _json(client.get('/api/ad-media/banner_z/whatever'))
    check('public stream 404s on unknown type', status == 404, str(status))
    caller['uid'] = ADMIN_UID

    for p in problems:
        print('  FAIL', p)
    print('ad media config API: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
