#!/usr/bin/env python3
"""
backfill_pokerpulse_subscription.py — one-shot, disposable.

Writes users/{uid}.pokerpulse_subscribed = False for every users/{uid} doc
that doesn't already have the field set.

Not strictly required for correctness — every read site treats a missing
field as falsy (see admin_list_users() and docs/firestore-schema.md), so an
existing user is already effectively unsubscribed. This script exists only to
make the state visible in Firestore immediately (for anyone inspecting the
console or writing a report) rather than appearing lazily on first read.
Users created after this script runs never need it: their doc is created
without the field, which already reads as unsubscribed.

Run once against prod, then delete this file — see docs/firestore-schema.md
for the field this populates.

    FIREBASE_SERVICE_ACCOUNT_JSON='...' python backfill_pokerpulse_subscription.py
"""

import os

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'unused-by-this-script')

import app as A  # noqa: E402


def main():
    db = A._get_admin_db()

    updated, skipped = 0, 0
    for doc in db.collection('users').stream():
        d = doc.to_dict() or {}
        if 'pokerpulse_subscribed' in d:
            skipped += 1
            continue

        doc.reference.update({'pokerpulse_subscribed': False})
        print(f'  [set] {doc.id} -> pokerpulse_subscribed: False')
        updated += 1

    print(f'\nDone. {updated} users backfilled, {skipped} already had the field.')


if __name__ == '__main__':
    main()
