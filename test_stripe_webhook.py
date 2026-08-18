"""
test_stripe_webhook.py — /api/stripe-webhook handler, against a fake Firestore
and a stubbed stripe.Webhook.construct_event (signature verification is
Stripe's own tested code, not ours).

    python test_stripe_webhook.py
"""

import json
import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')

UID = 'uid-sub'
CUSTOMER = 'cus_123'


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


class _Query:
    def __init__(self, store, path, field, value):
        self._store, self._path, self._field, self._value = store, path, field, value

    def limit(self, n):
        return self

    def get(self):
        depth = len(self._path) + 1
        return [_Snap(p[-1], data) for p, data in self._store._d.items()
                if len(p) == depth and p[:len(self._path)] == self._path
                and data.get(self._field) == self._value]


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))

    def where(self, field, op, value):
        return _Query(self._store, self._path, field, value)


class FakeDB:
    def __init__(self):
        self._d = {}

    def collection(self, name):
        return _Col(self, (name,))

    def get(self, path):
        return self._d.get(path)

    def put(self, path, data):
        self._d[path] = data


def _event(event_type, obj):
    return {'type': event_type, 'data': {'object': obj}}


def main():
    import app as A

    db = FakeDB()
    A._get_admin_db = lambda: db
    db.put(('users', UID), {'stripe_customer_id': CUSTOMER})

    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    client = A.app.test_client()

    def send(event_type, obj):
        A.stripe.Webhook.construct_event = staticmethod(
            lambda payload, sig, secret: _event(event_type, obj)
        )
        return client.post('/api/stripe-webhook', data=json.dumps({}),
                            content_type='application/json',
                            headers={'Stripe-Signature': 'unused'})

    # ── active: is_pro True, raw status recorded ─────────────────────────────
    res = send('customer.subscription.updated',
                {'customer': CUSTOMER, 'status': 'active', 'metadata': {}})
    check('active 200', res.status_code == 200, str(res.status_code))
    doc = db.get(('users', UID))
    check('active sets is_pro True', doc.get('is_pro') is True, str(doc))
    check('active sets raw status', doc.get('subscription_status') == 'active', str(doc))

    # ── past_due: is_pro untouched (stays True), status still recorded ──────
    res = send('customer.subscription.updated',
                {'customer': CUSTOMER, 'status': 'past_due', 'metadata': {}})
    doc = db.get(('users', UID))
    check('past_due does not flip is_pro', doc.get('is_pro') is True, str(doc))
    check('past_due still records raw status',
          doc.get('subscription_status') == 'past_due', str(doc))

    # ── canceled: is_pro False, raw status recorded ──────────────────────────
    res = send('customer.subscription.updated',
                {'customer': CUSTOMER, 'status': 'canceled', 'metadata': {}})
    doc = db.get(('users', UID))
    check('canceled sets is_pro False', doc.get('is_pro') is False, str(doc))
    check('canceled sets raw status', doc.get('subscription_status') == 'canceled', str(doc))

    # ── subscription.deleted with an empty status still demotes + records ───
    res = send('customer.subscription.deleted',
                {'customer': CUSTOMER, 'status': '', 'metadata': {}})
    doc = db.get(('users', UID))
    check('deleted sets is_pro False', doc.get('is_pro') is False, str(doc))
    check('deleted records the (empty) raw status',
          doc.get('subscription_status') == '', str(doc))

    for p in problems:
        print('  FAIL', p)
    print('stripe webhook: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
