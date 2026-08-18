#!/usr/bin/env python3
"""
backfill_subscription_status.py — one-shot, disposable.

Sets subscription_status on every users/{uid} doc that has a
stripe_customer_id, by asking Stripe for that customer's current
subscription. Existing users predate the subscription_status field (added by
Sub-mgmt #4), so this is what makes admin.html's Subscription column
non-blank for them going forward — new writes come from the
customer.subscription.updated/.deleted webhook handlers in app.py.

Users with no stripe_customer_id (free, or manually granted pro) are left
alone — no fabricated status.

Run once against prod, then delete this file — see docs/firestore-schema.md
for the field this populates.

    FIREBASE_SERVICE_ACCOUNT_JSON='...' STRIPE_SECRET_KEY='...' \
        python backfill_subscription_status.py
"""

import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'unused-by-this-script')

import stripe  # noqa: E402

import app as A  # noqa: E402


def _current_status(customer_id):
    """Most recently created subscription for a customer, any status."""
    try:
        subs = stripe.Subscription.list(customer=customer_id, status='all', limit=1)
    except stripe.StripeError as exc:
        print(f'  [skip] {customer_id}: Stripe lookup failed: {exc}')
        return None
    if not subs.data:
        return None
    return subs.data[0].status


def main():
    db = A._get_admin_db()

    updated, skipped = 0, 0
    for doc in db.collection('users').stream():
        d = doc.to_dict() or {}
        customer_id = d.get('stripe_customer_id')
        if not customer_id:
            continue
        if d.get('subscription_status'):
            continue  # already backfilled or already kept current by the webhook

        status = _current_status(customer_id)
        if not status:
            skipped += 1
            continue

        db.collection('users').document(doc.id).set(
            {'subscription_status': status}, merge=True
        )
        print(f'  [set] {doc.id} ({customer_id}) -> {status}')
        updated += 1

    print(f'\nDone. {updated} updated, {skipped} had a stripe_customer_id but no Stripe subscription found.')


if __name__ == '__main__':
    if not os.getenv('STRIPE_SECRET_KEY'):
        sys.exit('STRIPE_SECRET_KEY is required')
    main()
