#!/usr/bin/env python3
"""
backfill_last_payment_at.py — one-shot, disposable.

Sets last_payment_at on every users/{uid} doc that has a stripe_customer_id,
by asking Stripe for that customer's most recent successful invoice.
Existing users predate the last_payment_at field (added by Sub-mgmt #3), so
this is what makes admin.html's Last Payment column show real history for
them going forward — new writes come from the checkout.session.completed /
invoice.payment_succeeded webhook handlers in app.py.

Users with no stripe_customer_id (free, or manually granted pro) are left
alone — no fabricated data.

Run once against prod, then delete this file — see docs/firestore-schema.md
for the field this populates.

    FIREBASE_SERVICE_ACCOUNT_JSON='...' STRIPE_SECRET_KEY='...' \
        python backfill_last_payment_at.py
"""

import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'unused-by-this-script')

import stripe  # noqa: E402

import app as A  # noqa: E402


def _last_paid_at(customer_id):
    """created ts of the customer's most recent paid invoice, if any."""
    try:
        invoices = stripe.Invoice.list(customer=customer_id, status='paid', limit=1)
    except stripe.StripeError as exc:
        print(f'  [skip] {customer_id}: Stripe lookup failed: {exc}')
        return None
    if not invoices.data:
        return None
    return invoices.data[0].created


def main():
    db = A._get_admin_db()

    updated, skipped = 0, 0
    for doc in db.collection('users').stream():
        d = doc.to_dict() or {}
        customer_id = d.get('stripe_customer_id')
        if not customer_id:
            continue
        if d.get('last_payment_at'):
            continue  # already backfilled or already kept current by the webhook

        paid_at = _last_paid_at(customer_id)
        if not paid_at:
            skipped += 1
            continue

        db.collection('users').document(doc.id).set(
            {'last_payment_at': int(paid_at)}, merge=True
        )
        print(f'  [set] {doc.id} ({customer_id}) -> {paid_at}')
        updated += 1

    print(f'\nDone. {updated} updated, {skipped} had a stripe_customer_id but no paid invoice found.')


if __name__ == '__main__':
    if not os.getenv('STRIPE_SECRET_KEY'):
        sys.exit('STRIPE_SECRET_KEY is required')
    main()
