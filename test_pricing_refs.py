"""
test_pricing_refs.py — the admin console's Active Plan Price switch must move
*every* price the site shows, not just the ones someone remembered to wire up.

Two halves:

  1. Propagation — flipping the plan through /api/admin/pricing changes what
     /api/pricing reports: active label, active price, and the is_discounted
     flag that decides whether the card strikes the full price through.

  2. No stranded literals — every "A$..." in the templates and in app.js sits
     behind a data-pricing-* hook (so _applyPricingCopy overwrites it) or is on
     the short exemption list below. This is the half that actually protects the
     feature: the propagation test still passes if someone hardcodes a fresh
     price into a new banner, but this one fails.

    python test_pricing_refs.py
"""

import io
import json
import os
import re
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')
os.environ['PERMANENT_ADMIN_EMAILS'] = 'perm@example.com'

ADMIN_UID = 'uid-admin'

# Price literals that must NOT track the plan switch, with the reason. Anything
# else carrying a price has to be hook-driven.
EXEMPT = {
    # The 'protest' tier is a fixed test subscription, deliberately outside the
    # plan switch (app.py keeps it on _STRIPE_PROTEST_PRICE_ID).
    'A$0.50',
    # app.js's _PRICING fallback: the values the page renders with before
    # /api/pricing answers, and if it never does.
    'FALLBACK',
}

PRICE_RE = re.compile(r'A\$[0-9]+\.[0-9]+')
HOOK_RE  = re.compile(r'data-pricing-(plan|price|regular|cta|discount-only)\b')


# ── Fakes (mirrors test_admin_users.py) ──────────────────────────────────────

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


class _UserNotFound(Exception):
    pass


class FakeAuth:
    UserNotFoundError = _UserNotFound

    def get_user_by_email(self, email):
        raise _UserNotFound(email)


# ── Half 1: the switch propagates ────────────────────────────────────────────

def check_propagation(check):
    import app as A

    db = FakeDB()
    db.put(('config', 'admins'), {'uids': [ADMIN_UID]})
    A._get_admin_db = lambda: db
    A.admin_auth = FakeAuth()
    A._PERM_ADMIN_UID_CACHE = None
    A._verify_bearer = lambda req: ADMIN_UID

    os.environ['STRIPE_PRICE_ID']     = 'price_early'
    os.environ['STRIPE_PRO_PRICE_ID'] = 'price_pro'
    os.environ.pop('STRIPE_EARLY_ACCESS_LABEL', None)
    os.environ.pop('STRIPE_PRO_LABEL', None)

    client = A.app.test_client()

    def pricing():
        return json.loads(client.get('/api/pricing').get_data(as_text=True))

    def switch(plan):
        res = client.post('/api/admin/pricing', data=json.dumps({'plan': plan}),
                          content_type='application/json')
        return res.status_code

    # Default: Early Access on sale, Pro's price shown struck through.
    p = pricing()
    check('defaults to early access', p['plan'] == 'early_access', str(p))
    check('active price is the discount', p['price_label'] == 'A$7.99/mo', str(p))
    check('regular price is the full price', p['regular_price_label'] == 'A$13.99/mo', str(p))
    check('discount flag set', p['is_discounted'] is True, str(p))

    # Switching to the full price drops the discount framing entirely.
    check('switch to pro accepted', switch('pro') == 200)
    p = pricing()
    check('label follows the switch', p['label'] == 'Pro', str(p))
    check('price follows the switch', p['price_label'] == 'A$13.99/mo', str(p))
    check('discount flag cleared on full price', p['is_discounted'] is False, str(p))

    # And back.
    check('switch back accepted', switch('early_access') == 200)
    p = pricing()
    check('switching back restores the discount', p['is_discounted'] is True, str(p))
    check('switching back restores the price', p['price_label'] == 'A$7.99/mo', str(p))

    # Labels are env-driven, so a price change is a variable edit, not a deploy.
    os.environ['STRIPE_PRO_LABEL'] = 'A$19.99/mo'
    p = pricing()
    check('regular price tracks STRIPE_PRO_LABEL',
          p['regular_price_label'] == 'A$19.99/mo', str(p))
    os.environ.pop('STRIPE_PRO_LABEL', None)

    # Checkout must bill the plan that's actually on sale.
    plans = A._pricing_plans()
    switch('pro')
    check('checkout uses the active plan price',
          plans[A._active_plan()]['price_id'] == 'price_pro', A._active_plan())
    switch('early_access')
    check('checkout follows back',
          plans[A._active_plan()]['price_id'] == 'price_early', A._active_plan())


# ── Half 2: no price literal escapes the switch ──────────────────────────────

def _hooked(lines, i):
    """True if the price on line i sits inside an element carrying a hook.

    The attribute can be a couple of lines above the text — the upgrade button
    puts its label on its own line — so look back a little as well.
    """
    return any(HOOK_RE.search(lines[j]) for j in range(max(0, i - 2), i + 1))


def check_no_stranded_literals(check):
    targets = ['static/app.js']
    targets += [os.path.join('templates', f) for f in sorted(os.listdir('templates'))
                if f.endswith('.html')]

    fallback_lines = set()
    stranded = []
    for path in targets:
        lines = io.open(path, encoding='utf-8').read().split('\n')
        in_fallback = False
        for i, line in enumerate(lines):
            # app.js's _PRICING fallback object is the one allowed home for
            # literal prices in JS.
            if path.endswith('app.js'):
                if re.search(r'^let _PRICING\s*=\s*\{', line):
                    in_fallback = True
                elif in_fallback and line.strip().startswith('}'):
                    in_fallback = False
            for m in PRICE_RE.finditer(line):
                price = m.group(0)
                if price in EXEMPT:
                    continue
                if in_fallback:
                    fallback_lines.add(price)
                    continue
                if line.lstrip().startswith(('*', '//', '#')) or '/**' in line:
                    continue          # doc comment, not rendered
                if path.endswith('.html') and _hooked(lines, i):
                    continue
                stranded.append('%s:%d %s' % (path, i + 1, line.strip()[:90]))

    check('every price literal is hook-driven or exempt', not stranded,
          '\n      ' + '\n      '.join(stranded) if stranded else '')
    # If the fallback disappears the page renders an empty price before
    # /api/pricing answers, so assert it is still there.
    check('app.js keeps a price fallback', bool(fallback_lines), str(fallback_lines))

    # The hooks are useless if nothing applies them.
    js = io.open('static/app.js', encoding='utf-8').read()
    for attr in ('data-pricing-plan', 'data-pricing-price', 'data-pricing-regular',
                 'data-pricing-cta', 'data-pricing-discount-only'):
        check('_applyPricingCopy handles %s' % attr, attr in js)
    check('pricing is fetched on boot', '_loadPricing()' in js)

    # Every hook used in a template must be one _applyPricingCopy knows about.
    for path in targets:
        if not path.endswith('.html'):
            continue
        for attr in set(re.findall(r'data-pricing-[a-z-]+', io.open(path, encoding='utf-8').read())):
            check('%s uses a known hook (%s)' % (os.path.basename(path), attr), attr in js, attr)


def main():
    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    check_propagation(check)
    check_no_stranded_literals(check)

    for p in problems:
        print('  FAIL', p)
    print('pricing references: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
