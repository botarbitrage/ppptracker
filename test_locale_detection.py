"""
test_locale_detection.py — pt-BR #2: Accept-Language header must decide the
locale for first-time visitors, without ever writing the `lang` cookie itself.

Only `app.get_locale()` is exercised (via `test_request_context`), so this
never touches Firestore/Stripe — those are lazy-initialised and irrelevant
to locale selection.

    python test_locale_detection.py
"""

import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')

import app as appmod

problems = []


def check(label, cond, detail=''):
    if not cond:
        problems.append('%s %s' % (label, ('— ' + detail) if detail else ''))


def locale_for(accept_language=None, lang_cookie=None):
    headers = {'Accept-Language': accept_language} if accept_language else {}
    environ_overrides = {'HTTP_COOKIE': 'lang=' + lang_cookie} if lang_cookie else {}
    with appmod.app.test_request_context(headers=headers, environ_overrides=environ_overrides):
        return appmod.get_locale()


def test_pt_variants_default_to_pt_br():
    for hdr in ('pt-BR', 'pt_BR', 'pt', 'pt-BR,pt;q=0.9,en;q=0.8'):
        check('%r -> pt_BR' % hdr, locale_for(accept_language=hdr) == 'pt_BR', locale_for(accept_language=hdr))


def test_non_pt_and_missing_header_default_to_en():
    for hdr in ('en-US,en;q=0.9', 'es', 'fr-FR', 'pt-PT'):
        check('%r -> en' % hdr, locale_for(accept_language=hdr) == 'en', locale_for(accept_language=hdr))
    check('no header -> en', locale_for() == 'en', locale_for())


def test_cookie_always_wins_over_header():
    check('lang=en cookie beats pt-BR header',
          locale_for(accept_language='pt-BR', lang_cookie='en') == 'en')
    check('lang=pt_BR cookie beats en header',
          locale_for(accept_language='en', lang_cookie='pt_BR') == 'pt_BR')


def test_detection_never_sets_the_cookie():
    with appmod.app.test_request_context(headers={'Accept-Language': 'pt-BR'}):
        appmod.get_locale()
        from flask import request
        check('no lang cookie present on the incoming request',
              'lang' not in request.cookies)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                problems.append('%s raised %s: %s' % (name, type(exc).__name__, exc))

    for p in problems:
        print('  FAIL', p)
    print('locale_detection: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
