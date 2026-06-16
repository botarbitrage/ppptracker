import os
import threading
import time

# Load .env file when running locally (no-op if file absent or python-dotenv not installed)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
except ImportError:
    pass
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

import requests
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory, Response

from hand_parser import process_hands
from hand_exporter import validate_hands, export_pokerstars

# In-memory store for the most recently imported hand records (used by export endpoints)
_session_records = None

app = Flask(__name__)

@app.after_request
def _set_coop_header(response):
    # unsafe-none lets Firebase's signInWithPopup check popup.closed without COOP violations
    if response.content_type and response.content_type.startswith('text/html'):
        response.headers['Cross-Origin-Opener-Policy'] = 'unsafe-none'
    return response

REQUEST_TIMEOUT = 30
MAX_WORKERS = 10
REQUEST_DELAY = 0.1

_thread_local = threading.local()


def _session():
    if not hasattr(_thread_local, 'session'):
        _thread_local.session = requests.Session()
    return _thread_local.session


def _extract(url, key):
    qs = parse_qs(urlparse(url).query)
    vals = qs.get(key)
    return vals[0] if vals else None


def _rand():
    return f"1858_{time.time()}"


def _headers(referer):
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
        "Origin": "https://replay.pppoker.net",
        "Accept": "application/json, text/plain, */*",
    }


def _find_share_key(data):
    if isinstance(data, dict):
        for k in ("share_key", "shareKey", "sharekey", "L"):
            v = data.get(k)
            if isinstance(v, str) and len(v) > 20:
                return v
        for v in data.values():
            found = _find_share_key(v)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_share_key(item)
            if found:
                return found
    return None


def fetch_summaries(uid, rdkey, referer):
    r = _session().get(
        "https://api.pppoker.club/poker/api/get_hand_collection.php",
        params={"uid": uid, "rdkey": rdkey, "type": 0, "start_time": 0, "rand": _rand()},
        headers=_headers(referer),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _fetch_share_key(uid, rdkey, gameid, referer):
    r = _session().get(
        "https://api.pppoker.club/poker/api/get_share_key.php",
        params={"uid": uid, "rdkey": rdkey, "gameid": gameid, "rand": _rand()},
        headers=_headers(referer),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return _find_share_key(r.json())


def _fetch_full_hand(share_key, referer):
    r = _session().get(
        f"https://alicdn.pppoker.club/review_hand/{share_key}.json",
        headers={"User-Agent": "Mozilla/5.0", "Referer": referer,
                 "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _fetch_record(uid, rdkey, summary, referer):
    gameid = summary.get('D')
    if not gameid:
        return None
    try:
        time.sleep(REQUEST_DELAY)
        sk = _fetch_share_key(uid, rdkey, gameid, referer)
        if not sk:
            return None
        time.sleep(REQUEST_DELAY)
        fh = _fetch_full_hand(sk, referer)
        return {"summary": summary, "share_key": sk, "full_hand": fh}
    except Exception as exc:
        app.logger.warning("Failed hand %s: %s", gameid, exc)
        return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    body     = request.get_json(force=True, silent=True) or {}
    url      = body.get("url", "").strip()
    auth_hdr = request.headers.get('Authorization', '')
    id_token = auth_hdr[7:] if auth_hdr.startswith('Bearer ') else None

    if not url:
        return jsonify({"error": "URL is required."}), 400

    uid   = _extract(url, "uid")
    rdkey = _extract(url, "rdkey")
    if not uid or not rdkey:
        return jsonify({"error": "Invalid URL – could not find uid and rdkey parameters."}), 400

    _EXPIRED_MSG = (
        "This link may have expired. Please re-open PPPoker, go to Hand History, "
        "and copy a fresh replay link."
    )

    try:
        summary_data = fetch_summaries(uid, rdkey, url)
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch hand list: {exc}"}), 502

    # PPPoker returns code=0 on success; any other value means auth/expired.
    api_code = summary_data.get("code")
    if api_code is not None and api_code != 0:
        return jsonify({"error": _EXPIRED_MSG}), 200

    hands = (summary_data.get("I") or [])[:200]
    if not hands:
        # Empty hand list with code=0 can mean the rdkey silently rejected.
        return jsonify({"error": _EXPIRED_MSG}), 200

    records = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_record, uid, rdkey, s, url): s
            for s in hands
        }
        for future in as_completed(futures):
            rec = future.result()
            if rec:
                records.append(rec)

    # Newest first (matching original list order)
    records.sort(key=lambda r: r["summary"].get("C", 0), reverse=True)

    player_name = "Hero"
    for rec in records:
        for p in rec.get("full_hand", {}).get("info", {}).get("players", []):
            if p.get("isSelf"):
                player_name = p.get("user_name", "Hero")
                break
        if player_name != "Hero":
            break

    global _session_records
    _session_records = records  # persist for export endpoints

    recent_hands, recent_won, stats, tournaments = process_hands(records)
    validation = validate_hands(records)

    session_id = _try_save_session(id_token, records, player_name, url, tournaments)

    return jsonify({
        "player": {"name": player_name, "uid": uid},
        "total_fetched": len(records),
        "total_available": len(hands),
        "recent_hands": recent_hands,
        "recent_won_hands": recent_won,
        "stats": stats,
        "tournaments": tournaments,
        "validation": validation,
        "session_id": session_id,
    })


@app.route("/api/export/hand", methods=["POST"])
def export_hand():
    if not _session_records:
        return jsonify({"error": "No hand data available. Please import first."}), 400
    body    = request.get_json(force=True, silent=True) or {}
    hand_id  = (body.get("hand_id") or "").strip().replace("-", "")
    platform = (body.get("platform") or "").strip()
    if not hand_id:
        return jsonify({"error": "Please provide a hand ID."}), 400

    # Match against the gameid stored in summary["D"], ignoring dashes
    match = next(
        (r for r in _session_records
         if r.get("summary", {}).get("D", "").replace("-", "") == hand_id),
        None,
    )
    if not match:
        return jsonify({"error": f"Hand '{hand_id}' not found in the imported data."}), 404

    try:
        filepath, _ = export_pokerstars([match], platform=platform)
        return send_file(
            os.path.abspath(filepath),
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype="text/plain",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/export/tournament", methods=["POST"])
def export_tournament():
    if not _session_records:
        return jsonify({"error": "No hand data available. Please import first."}), 400
    body = request.get_json(force=True, silent=True) or {}
    tid      = str(body.get("tourney_id", "")).strip()
    platform = (body.get("platform") or "").strip()
    if not tid:
        return jsonify({"error": "Please provide a tourney_id."}), 400

    from hand_parser import extract_tourney_id
    records = [r for r in _session_records
               if extract_tourney_id(r.get("summary", {}).get("D", "")) == tid]
    if not records:
        return jsonify({"error": f"No hands found for tournament '{tid}'."}), 404

    try:
        filepath, _ = export_pokerstars(records, platform=platform)
        return send_file(
            os.path.abspath(filepath),
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype="text/plain",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/export/pokerstars", methods=["POST"])
def export_ps():
    if not _session_records:
        return jsonify({"error": "No hand data available. Please import first."}), 400
    try:
        body    = request.get_json(force=True, silent=True) or {}
        limit    = body.get("limit")          # None = all hands
        platform = (body.get("platform") or "").strip()
        records  = _session_records[:limit] if limit else _session_records
        filepath, log = export_pokerstars(records, platform=platform)
        return send_file(
            os.path.abspath(filepath),
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype="text/plain",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── JSON export endpoints ─────────────────────────────────────────────────────

import re as _re
from datetime import datetime as _dt


def _room_slug(records):
    """Return alphanumeric room name slug from the first record that has one."""
    for r in (records or []):
        name = (r.get('full_hand', {}).get('info', {})
                 .get('room', {}).get('room_name', '') or '')
        slug = _re.sub(r'[^A-Za-z0-9]', '', name)[:24]
        if slug:
            return slug
    return ''


@app.route("/api/export/json/all", methods=["POST"])
def export_json_all():
    if not _session_records:
        return jsonify({"error": "No hand data available. Please import first."}), 400
    import json as _json
    ts    = _dt.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pppoker_full_export_{ts}.json"
    data = _json.dumps(_session_records, indent=2)
    return Response(data, mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/export/json/tournament", methods=["POST"])
def export_json_tournament():
    if not _session_records:
        return jsonify({"error": "No hand data available. Please import first."}), 400
    body = request.get_json(force=True, silent=True) or {}
    tid  = str(body.get("tourney_id", "")).strip()
    if not tid:
        return jsonify({"error": "Please provide a tourney_id."}), 400
    from hand_parser import extract_tourney_id
    import json as _json
    records = [r for r in _session_records
               if extract_tourney_id(r.get("summary", {}).get("D", "")) == tid]
    if not records:
        return jsonify({"error": f"No hands found for tournament '{tid}'."}), 404
    room  = _room_slug(records)
    ts    = _dt.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pppoker_{room}_{ts}.json" if room else f"pppoker_tourney{tid}_{ts}.json"
    data = _json.dumps(records, indent=2)
    return Response(data, mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/export/json/hand", methods=["POST"])
def export_json_hand():
    if not _session_records:
        return jsonify({"error": "No hand data available. Please import first."}), 400
    body         = request.get_json(force=True, silent=True) or {}
    raw_hand_id  = (body.get("hand_id") or "").strip()
    hand_id      = raw_hand_id.replace("-", "")
    if not hand_id:
        return jsonify({"error": "Please provide a hand ID."}), 400
    match = next(
        (r for r in _session_records
         if r.get("summary", {}).get("D", "").replace("-", "") == hand_id),
        None,
    )
    if not match:
        return jsonify({"error": f"Hand '{raw_hand_id}' not found."}), 404
    import json as _json
    filename = f"pppoker_hand_{raw_hand_id}.json"
    data = _json.dumps(match, indent=2)
    return Response(data, mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ── Firebase config endpoint ─────────────────────────────────────────────────
# NOTE: these are publishable client-side keys (not secret), but we still serve
# them via env vars so the values are never committed to source control.
# Firestore security rules should restrict writes to documents where
#   request.resource.data.session_id == the document ID.

# ── Stripe + Firebase Admin ───────────────────────────────────────────────────

import stripe
import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore, auth as admin_auth, storage as admin_storage

stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
_STRIPE_PRICE_ID         = os.getenv('STRIPE_PRICE_ID', '')
_STRIPE_PROTEST_PRICE_ID = os.getenv('STRIPE_PROTEST_PRICE_ID', '')
_STRIPE_WEBHOOK_SEC      = os.getenv('STRIPE_WEBHOOK_SECRET', '')


def _get_admin_db():
    """Lazy-init Firebase Admin SDK and return a Firestore client."""
    if not firebase_admin._apps:
        sa_json = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON', '')
        if sa_json:
            import json
            cred = credentials.Certificate(json.loads(sa_json))
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    return admin_firestore.client()


def _get_admin_bucket():
    """Return the Firebase Storage bucket, initialising Admin SDK if needed."""
    _get_admin_db()
    bucket_name = os.getenv('FIREBASE_STORAGE_BUCKET', '')
    if not bucket_name:
        return None
    return admin_storage.bucket(name=bucket_name)


def _try_save_session(id_token, records, player_name, url, tournaments):
    """Upload session to Firebase Storage + write Firestore metadata. Returns session_id or None."""
    if not id_token:
        return None
    try:
        import json as _jj, time as _tt
        decoded = admin_auth.verify_id_token(id_token)
        uid = decoded['uid']

        db = _get_admin_db()
        user_snap = db.collection('users').document(uid).get()
        if not user_snap.exists or not user_snap.to_dict().get('is_pro'):
            return None

        session_id   = f"{int(_tt.time() * 1000)}_{uid[:6]}"
        storage_path = f"sessions/{uid}/{session_id}.json"

        bucket = _get_admin_bucket()
        if bucket:
            blob = bucket.blob(storage_path)
            blob.upload_from_string(_jj.dumps(records), content_type='application/json')

        tourney_docs = [{
            'tourney_id':   t.get('tourney_id'),
            'room_name':    t.get('room_name', ''),
            'is_mtt':       t.get('is_mtt', False),
            'hands':        t.get('hands', 0),
            'net':          t.get('net', 0),
            'first_chips':  t.get('first_chips', 0),
            'last_chips':   t.get('last_chips', 0),
            'finish_busted':t.get('finish_busted', False),
            'duration_secs':t.get('duration_secs'),
            'earliest_ts':  t.get('earliest_ts'),
            'blind_min':    t.get('blind_min', 0),
            'blind_max':    t.get('blind_max', 0),
            'vpip_pct':     t.get('vpip_pct', 0.0),
            'pfr_pct':      t.get('pfr_pct', 0.0),
            'af':           t.get('af', 0.0),
            'wtsd_pct':     t.get('wtsd_pct', 0.0),
            'biggest_win':  t.get('biggest_win', 0),
            'biggest_loss': t.get('biggest_loss', 0),
        } for t in (tournaments or [])]

        db.collection('users').document(uid).collection('sessions').document(session_id).set({
            'created_at':       int(_tt.time()),
            'player_name':      player_name,
            'url':              url,
            'hand_count':       len(records),
            'tournament_count': len(tourney_docs),
            'storage_path':     storage_path,
            'tournaments':      tourney_docs,
        })
        return session_id
    except Exception:
        return None


@app.route('/api/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data  = request.get_json(silent=True) or {}
    tier  = data.get('tier', 'pro')
    price = _STRIPE_PROTEST_PRICE_ID if tier == 'protest' else _STRIPE_PRICE_ID
    if not stripe.api_key or not price:
        return jsonify({'error': 'Stripe not configured'}), 503
    uid       = data.get('uid', '')
    email     = data.get('email', '')
    origin    = request.headers.get('Origin', os.getenv('APP_URL', 'https://pppokerha.up.railway.app'))
    try:
        session = stripe.checkout.Session.create(
            mode               = 'subscription',
            line_items         = [{'price': price, 'quantity': 1}],
            success_url        = f'{origin}/?session_id={{CHECKOUT_SESSION_ID}}&upgraded=1',
            cancel_url         = f'{origin}/',
            customer_email     = email or None,
            metadata           = {'uid': uid},
            subscription_data  = {'metadata': {'uid': uid}},
        )
        return jsonify({'url': session.url})
    except stripe.StripeError as e:
        return jsonify({'error': str(e)}), 400


def _uid_for_customer(db, customer_id):
    """Look up Firestore uid by Stripe customer_id (fallback when metadata is absent)."""
    if not customer_id or not db:
        return None
    try:
        docs = db.collection('users').where('stripe_customer_id', '==', customer_id).limit(1).get()
        for doc in docs:
            return doc.id
    except Exception:
        pass
    return None


@app.route('/api/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig, _STRIPE_WEBHOOK_SEC)
    except (ValueError, stripe.SignatureVerificationError):
        return jsonify({'error': 'Invalid signature'}), 400

    db  = _get_admin_db()
    t   = event['type']
    obj = event['data']['object']

    if t == 'checkout.session.completed':
        uid = obj.get('metadata', {}).get('uid', '')
        if uid:
            db.collection('users').document(uid).set(
                {'is_pro': True, 'stripe_customer_id': obj.get('customer', '')},
                merge=True
            )

    elif t in ('customer.subscription.deleted', 'customer.subscription.updated'):
        uid = (obj.get('metadata', {}).get('uid', '')
               or _uid_for_customer(db, obj.get('customer', '')))
        if uid:
            status = obj.get('status', '')
            if t == 'customer.subscription.deleted' or status in ('canceled', 'unpaid'):
                db.collection('users').document(uid).set({'is_pro': False}, merge=True)
            elif status == 'active':
                db.collection('users').document(uid).set({'is_pro': True}, merge=True)

    elif t == 'invoice.payment_succeeded':
        if obj.get('subscription'):   # only subscription invoices, not one-off
            uid = _uid_for_customer(db, obj.get('customer', ''))
            if uid:
                db.collection('users').document(uid).set({'is_pro': True}, merge=True)

    return jsonify({'received': True})


@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    auth_hdr = request.headers.get('Authorization', '')
    if not auth_hdr.startswith('Bearer '):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        decoded = admin_auth.verify_id_token(auth_hdr[7:])
        uid = decoded['uid']
    except Exception:
        return jsonify({'error': 'Unauthorized'}), 401

    db   = _get_admin_db()
    docs = (db.collection('users').document(uid)
              .collection('sessions')
              .order_by('created_at', direction=admin_firestore.Query.DESCENDING)
              .get())

    sessions = []
    for doc in docs:
        d = doc.to_dict()
        d['session_id'] = doc.id
        # Remove storage_path from client response (internal detail)
        d.pop('storage_path', None)
        sessions.append(d)

    return jsonify({'sessions': sessions})


@app.route('/api/sessions/<session_id>/download', methods=['GET'])
def download_session(session_id):
    auth_hdr = request.headers.get('Authorization', '')
    if not auth_hdr.startswith('Bearer '):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        decoded = admin_auth.verify_id_token(auth_hdr[7:])
        uid = decoded['uid']
    except Exception:
        return jsonify({'error': 'Unauthorized'}), 401

    db  = _get_admin_db()
    doc = db.collection('users').document(uid).collection('sessions').document(session_id).get()
    if not doc.exists:
        return jsonify({'error': 'Session not found'}), 404

    storage_path = doc.to_dict().get('storage_path', '')
    bucket = _get_admin_bucket()
    if not bucket or not storage_path:
        return jsonify({'error': 'Storage not configured'}), 503

    blob = bucket.blob(storage_path)
    if not blob.exists():
        return jsonify({'archived': True, 'error': 'Session data no longer available'}), 410

    data        = blob.download_as_bytes()
    player_name = doc.to_dict().get('player_name', 'session')
    slug        = _re.sub(r'[^A-Za-z0-9]', '_', player_name)[:20]
    filename    = f"pppoker_session_{session_id}_{slug}.json"
    return Response(data, mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


_FIREBASE_ENV_KEYS = [
    'FIREBASE_API_KEY',
    'FIREBASE_AUTH_DOMAIN',
    'FIREBASE_PROJECT_ID',
    'FIREBASE_STORAGE_BUCKET',
    'FIREBASE_MESSAGING_SENDER_ID',
    'FIREBASE_APP_ID',
    'FIREBASE_MEASUREMENT_ID',
]

@app.route('/api/firebase-config')
def firebase_config():
    cfg = {k: os.getenv(k, '') for k in _FIREBASE_ENV_KEYS}
    if not cfg.get('FIREBASE_API_KEY'):
        return jsonify({'error': 'Firebase not configured'}), 503
    return jsonify(cfg)


# ── PWA / TWA support ─────────────────────────────────────────────────────────

@app.route('/offline')
def offline():
    """Minimal offline fallback page served by the service worker."""
    return render_template('offline.html')

@app.route('/static/sw.js')
def service_worker():
    """Serve the service worker from /static/ but with the right scope headers."""
    response = send_from_directory('static', 'sw.js',
                                   mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.route('/.well-known/assetlinks.json')
def asset_links():
    """
    Digital Asset Links — required for TWA (Trusted Web Activity) on Google Play.
    Replace the placeholder sha256_cert_fingerprints value with your actual
    signing key fingerprint from the Play Console / Bubblewrap output.
    """
    links = [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                # TODO: replace with your actual Android app package name and fingerprint
                "package_name": "com.yourname.pppokerha",
                "sha256_cert_fingerprints": [
                    "REPLACE_WITH_SHA256_FINGERPRINT_FROM_PLAY_CONSOLE"
                ]
            }
        }
    ]
    return Response(
        __import__('json').dumps(links, indent=2),
        mimetype='application/json',
        headers={'Cache-Control': 'no-cache'}
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
