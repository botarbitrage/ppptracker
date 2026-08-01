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

from hand_parser import process_hands, build_hand_rows
from hand_exporter import validate_hands, export_pokerstars
from tournament_analyzer import analyze_tournament

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

    saved, new_ids = _try_save_tournaments(id_token, records, tournaments)
    new_hands = len(new_ids)

    # Compute stats/validation for only the truly-new records so the UI can
    # show "X new hands loaded" with accurate breakdown counts.
    if new_ids:
        from hand_parser import extract_tourney_id as _extract_tid
        new_recs = [r for r in records if r.get('summary', {}).get('D') in new_ids]
        _, _, new_stats, new_tourneys = process_hands(new_recs)
        new_validation = validate_hands(new_recs)
        new_tourney_count = len({_extract_tid(r.get('summary', {}).get('D', '')) for r in new_recs})
        _new_ts = [t.get('earliest_ts') for t in new_tourneys if t.get('earliest_ts')]
        new_ts_min = min(_new_ts) if _new_ts else None
        new_ts_max = max(_new_ts) if _new_ts else None
    else:
        new_stats = None
        new_validation = None
        new_tourney_count = 0
        new_ts_min = None
        new_ts_max = None

    return jsonify({
        "player": {"name": player_name, "uid": uid},
        "total_fetched": len(records),
        "total_available": len(hands),
        "new_hands": new_hands,
        "new_tourney_count": new_tourney_count,
        "new_ts_min": new_ts_min,
        "new_ts_max": new_ts_max,
        "new_stats": new_stats,
        "new_validation": new_validation,
        "recent_hands": recent_hands,
        "recent_won_hands": recent_won,
        "stats": stats,
        "tournaments": tournaments,
        "validation": validation,
        "saved": saved,
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
        filepath, _ = export_pokerstars([match], platform=platform,
                                         blind_levels_by_room=_blind_levels_by_room([match]))
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
        filepath, _ = export_pokerstars(records, platform=platform,
                                         blind_levels_by_room=_blind_levels_by_room(records))
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
        filepath, log = export_pokerstars(records, platform=platform,
                                           blind_levels_by_room=_blind_levels_by_room(records))
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


def _merge_tournament(db, bucket, uid, tid, new_records):
    """
    Atomically merge new_records into the persisted tournament doc/blob for tid.
    Wrapped in a Firestore transaction so a concurrent import racing on the same
    tid is forced to retry from a fresh read rather than clobbering this write.
    """
    import json as _jj, time as _tt
    from google.cloud import firestore as gcf
    from hand_parser import _seq_num

    doc_ref      = db.collection('users').document(uid).collection('tournaments').document(tid)
    storage_path = f"tournaments/{uid}/{tid}.json"

    @gcf.transactional
    def _txn(transaction):
        snapshot = doc_ref.get(transaction=transaction)
        # Preserve the first-import timestamp across re-imports; new docs get "now".
        first_seen = (snapshot.to_dict() if snapshot.exists else {}).get('first_seen') \
            or int(_tt.time())
        merged_records = new_records
        # Default: all incoming IDs are new (no prior stored data for this tournament)
        new_ids = {r.get('summary', {}).get('D') for r in new_records}
        if snapshot.exists and bucket:
            old_path = snapshot.to_dict().get('storage_path', storage_path)
            blob = bucket.blob(old_path)
            if blob.exists():
                try:
                    old_records = _jj.loads(blob.download_as_bytes())
                    old_ids = {r.get('summary', {}).get('D') for r in old_records}
                    seen    = {r.get('summary', {}).get('D') for r in new_records}
                    new_ids = seen - old_ids
                    merged_records = new_records + [
                        r for r in old_records if r.get('summary', {}).get('D') not in seen
                    ]
                except Exception:
                    pass  # unreadable old blob — fall back to just the new hands

        # Sort by per-tournament sequence number (tie-free), not by concatenation order.
        merged_records.sort(
            key=lambda r: _seq_num(r.get('summary', {}).get('D', '')), reverse=True)

        _, _, _, recomputed = process_hands(merged_records)
        if not recomputed:
            return 0
        stat = recomputed[0]

        if bucket:
            bucket.blob(storage_path).upload_from_string(
                _jj.dumps(merged_records), content_type='application/json')

        transaction.set(doc_ref, {
            'tourney_id':    tid,
            'room_name':     stat.get('room_name', ''),
            'is_mtt':        stat.get('is_mtt', False),
            'hands':         stat.get('hands', 0),
            'net':           stat.get('net', 0),
            'first_chips':   stat.get('first_chips', 0),
            'last_chips':    stat.get('last_chips', 0),
            'finish_busted': stat.get('finish_busted', False),
            'duration_secs': stat.get('duration_secs'),
            'earliest_ts':   stat.get('earliest_ts'),
            'blind_min':     stat.get('blind_min', 0),
            'blind_max':     stat.get('blind_max', 0),
            'vpip_pct':      stat.get('vpip_pct', 0.0),
            'pfr_pct':       stat.get('pfr_pct', 0.0),
            'af':            stat.get('af', 0.0),
            'wtsd_pct':      stat.get('wtsd_pct', 0.0),
            'biggest_win':   stat.get('biggest_win', 0),
            'biggest_loss':  stat.get('biggest_loss', 0),
            'max_players':   stat.get('max_players', 0),
            'storage_path':  storage_path,
            'first_seen':    first_seen,
            'updated_at':    int(_tt.time()),
        })
        return new_ids

    return _txn(db.transaction())


def _try_save_tournaments(id_token, records, tournaments):
    """
    Merge each tournament from this import into per-tournament persisted storage.
    On a re-import of the same tourney_id, hands are merged (de-duped by gameid)
    with any previously stored hands for that tournament and stats recomputed
    from the merged set. Returns (saved, new_game_ids): saved is True when any
    tournament was written, new_game_ids is the set of game IDs not previously stored.
    """
    if not id_token or not tournaments:
        return False, set()
    try:
        from hand_parser import extract_tourney_id

        db = _get_admin_db()  # ensures firebase_admin.initialize_app() has run
        decoded = admin_auth.verify_id_token(id_token)
        uid = decoded['uid']

        user_snap = db.collection('users').document(uid).get()
        if not user_snap.exists or not user_snap.to_dict().get('is_pro'):
            return False, set()

        bucket = _get_admin_bucket()

        all_new_ids = set()
        for t in tournaments:
            tid = t.get('tourney_id')
            if not tid:
                continue
            new_records = [r for r in records
                            if extract_tourney_id(r.get('summary', {}).get('D', '')) == tid]
            if not new_records:
                continue
            ids = _merge_tournament(db, bucket, uid, tid, new_records)
            if ids:
                all_new_ids.update(ids)
        return True, all_new_ids
    except Exception as exc:
        import traceback
        print(f"[_try_save_tournaments] FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False, set()


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


def _verify_bearer(req):
    """Verify the Authorization: Bearer <token> header. Returns uid or None."""
    auth_hdr = req.headers.get('Authorization', '')
    if not auth_hdr.startswith('Bearer '):
        return None
    try:
        _get_admin_db()  # ensure Firebase Admin SDK is initialized before token verification
        decoded = admin_auth.verify_id_token(auth_hdr[7:])
        return decoded['uid']
    except Exception:
        return None


def _is_admin(uid):
    """True if uid is listed in /config/admins.uids (publicly readable doc)."""
    if not uid:
        return False
    try:
        snap = _get_admin_db().collection('config').document('admins').get()
        return snap.exists and uid in (snap.to_dict().get('uids') or [])
    except Exception:
        return False


@app.route('/api/tournaments', methods=['GET'])
def list_tournaments():
    db  = _get_admin_db()  # ensures firebase_admin.initialize_app() has run
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    docs = db.collection('users').document(uid).collection('tournaments').get()
    tournaments = []
    for doc in docs:
        d = doc.to_dict()
        d.pop('storage_path', None)  # internal detail, not needed by client
        tournaments.append(d)

    return jsonify({'tournaments': tournaments})


# ── Admin: tournament-config CRUD (writes via Admin SDK, gated to /config/admins) ─
_TOURNEY_FIELDS = {
    'name': str, 'type': str, 'is_pko': bool, 'is_mtt': bool, 'currency': str,
    'buy_in_total': float, 'buy_in_prize': float, 'buy_in_rake': float,
    'starting_chips': int, 'starting_time': 'strlist',
    'level_duration_min': int, 'level_duration_rebuy_min': int,
    'level_duration_ft_min': int, 'late_reg_level': int,
    'rebuy': bool, 'rebuy_type': str, 'rebuy_cost_multiplier': float,
    'rebuy_period_end_level': int, 'rebuy_bulk_options': list,
    'addon': bool, 'addon_cost_multiplier': float, 'addon_max_units': int,
    'addon_bulk_options': list,
    'player_min': int, 'player_max': int,
    'break_every_min': int, 'break_duration_min': int,
    'itm_h': float, 'end_h': float, 'ft_h': float, 'max_blinds': int,
    'active': bool,
}


def _coerce_tourney_payload(body):
    """Coerce a client JSON body to typed, whitelisted tournament-config fields.
    Empty string / None -> None; unparseable values fall back to None."""
    data = {}
    for key, typ in _TOURNEY_FIELDS.items():
        if key not in body:
            continue
        v = body[key]
        if v is None or (isinstance(v, str) and v.strip() == ''):
            data[key] = None
            continue
        try:
            if typ is bool:
                data[key] = v if isinstance(v, bool) else str(v).strip().lower() in ('1', 'true', 'yes', 'on')
            elif typ is int:
                data[key] = int(float(v))
            elif typ is float:
                data[key] = float(v)
            elif typ is list:
                seq = v if isinstance(v, list) else str(v).split(',')
                data[key] = [int(float(x)) for x in seq if str(x).strip() != '']
            elif typ == 'strlist':
                seq = v if isinstance(v, list) else str(v).split(',')
                data[key] = [s for s in (str(x).strip() for x in seq) if s] or None
            else:
                data[key] = str(v).strip()
        except (ValueError, TypeError):
            data[key] = None
    return data


@app.route('/api/admin/tournaments', methods=['POST'])
def admin_create_tournament():
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    body = request.get_json(silent=True) or {}
    data = _coerce_tourney_payload(body)
    if not data.get('name'):
        return jsonify({'error': 'Name is required'}), 400
    raw_id = (body.get('id') or data['name'] or '').strip().lower()
    tid = _re.sub(r'[^a-z0-9]+', '_', raw_id).strip('_')
    if not tid:
        return jsonify({'error': 'Could not derive a valid id from name/id'}), 400
    db = _get_admin_db()
    ref = db.collection('tournaments').document(tid)
    if ref.get().exists:
        return jsonify({'error': f'Tournament "{tid}" already exists'}), 409
    data.setdefault('active', True)
    data.setdefault('currency', 'AUD')
    data['created_at'] = int(time.time())
    ref.set(data)
    return jsonify({'ok': True, 'id': tid})


@app.route('/api/admin/tournaments/<tid>', methods=['PUT', 'PATCH'])
def admin_update_tournament(tid):
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    body = request.get_json(silent=True) or {}
    data = _coerce_tourney_payload(body)
    if not data:
        return jsonify({'error': 'No valid fields to update'}), 400
    db = _get_admin_db()
    ref = db.collection('tournaments').document(tid)
    if not ref.get().exists:
        return jsonify({'error': 'Tournament not found'}), 404
    data['updated_at'] = int(time.time())
    ref.update(data)  # merge — preserves blind_structure_extra / override, etc.
    return jsonify({'ok': True, 'id': tid})


@app.route('/api/admin/tournaments/stamp-new', methods=['POST'])
def admin_stamp_new_tournaments():
    """Stamp created_at (epoch secs) on any tournament config missing it.

    Tournaments added by the disposable seed script carry no created_at, so the
    tournaments-page "New" marker has nothing to key on. Calling this (admin-only,
    triggered when an admin loads the page) back-fills the timestamp the first time
    a doc is seen, which starts its review window. Docs that already have a
    created_at are left untouched. Returns the list of ids that were stamped."""
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    db = _get_admin_db()
    now = int(time.time())
    stamped = []
    for doc in db.collection('tournaments').get():
        if doc.to_dict().get('created_at') is None:
            doc.reference.update({'created_at': now})
            stamped.append(doc.id)
    return jsonify({'ok': True, 'stamped': stamped})


def _fetch_tournament_records(uid, tourney_id):
    """Returns (records, doc_dict) for a persisted tournament, or (None, None)."""
    db  = _get_admin_db()
    doc = db.collection('users').document(uid).collection('tournaments').document(tourney_id).get()
    if not doc.exists:
        return None, None
    d = doc.to_dict()
    storage_path = d.get('storage_path', '')
    bucket = _get_admin_bucket()
    if not bucket or not storage_path:
        return None, d
    blob = bucket.blob(storage_path)
    if not blob.exists():
        return None, d
    import json as _jj
    return _jj.loads(blob.download_as_bytes()), d


def _norm_room_name(s):
    """Strip platform emoji/punctuation so room names compare cleanly, e.g.
    "🌐 LUCKY DAY" (as stored on hand records) == "LUCKY DAY" (config doc name)."""
    return _re.sub(r'[^A-Z0-9 ]', '', (s or '').upper()).strip()


def _resolve_tournament_cfg(room_name):
    """
    Resolve the static config for a tournament instance, matched by room name.
    Per-tournament values win; missing scalars fall back to
    /config/tournament_defaults. The blind ladder is composed as base+extra
    (or the override ladder for LUCKY DAY / TEXAS), falling back to the canonical
    80-level ladder when no config doc matches. Returns a plain dict.
    """
    db = _get_admin_db()
    room = _norm_room_name(room_name)

    cfg_doc = {}
    if room:
        for snap in db.collection('tournaments').get():
            cd = snap.to_dict()
            if _norm_room_name(cd.get('name')) == room:
                cfg_doc = cd
                break

    def _config(name):
        snap = db.collection('config').document(name).get()
        return snap.to_dict() if snap.exists else {}

    defaults         = _config('tournament_defaults')
    base_levels      = _config('blind_structure_base').get('levels', [])
    canonical_levels = _config('blind_ladder_canonical').get('levels', [])

    if cfg_doc:
        extra = cfg_doc.get('blind_structure_extra') or []
        levels = (list(extra) if cfg_doc.get('blind_structure_override')
                  else list(base_levels) + list(extra))
    else:
        levels = canonical_levels

    graph_required = (
        'itm_h', 'end_h', 'ft_h', 'max_blinds',
        'late_reg_level', 'level_duration_min',
    )
    missing_graph_fields = [key for key in graph_required if cfg_doc.get(key) is None]
    if not levels:
        missing_graph_fields.append('blind_levels')
    graph_ready = bool(cfg_doc) and not missing_graph_fields and bool(levels)

    def pick(key):
        v = cfg_doc.get(key)
        return v if v is not None else defaults.get(key)

    return {
        'name':                     cfg_doc.get('name', room_name),
        'blind_levels':             levels,
        'itm_h':                    pick('itm_h'),
        'end_h':                    pick('end_h'),
        'ft_h':                     pick('ft_h'),
        'max_blinds':               pick('max_blinds'),
        'late_reg_level':           pick('late_reg_level'),
        'level_duration_min':       pick('level_duration_min'),
        'level_duration_rebuy_min': pick('level_duration_rebuy_min'),
        'level_duration_ft_min':    pick('level_duration_ft_min'),
        'starting_chips':           pick('starting_chips'),
        'rebuy_period_end_level':   pick('rebuy_period_end_level'),
        'graph_ready':              graph_ready,
        'graph_missing_fields':     missing_graph_fields,
        'graph_config_found':       bool(cfg_doc),
    }


def _blind_levels_by_room(records):
    """{normalized_room_name: blind_levels} for every distinct room among
    `records`, for hand_exporter's per-hand "Level" header — a session export
    (e.g. /api/export/pokerstars) can span several different tournaments."""
    rooms = {
        (r.get('full_hand', {}).get('info', {}).get('room', {}).get('room_name') or '')
        for r in records
    }
    rooms.discard('')
    return {
        _norm_room_name(room): _resolve_tournament_cfg(room).get('blind_levels')
        for room in rooms
    }


@app.route('/api/tournaments/<tourney_id>/hands', methods=['GET'])
def tournament_hands(tourney_id):
    """Per-hand display rows for one persisted tournament (Tournament Details)."""
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    records, doc = _fetch_tournament_records(uid, tourney_id)
    if records is None:
        return jsonify({'error': 'Tournament data not available'}), 404

    meta = {k: (doc or {}).get(k) for k in
            ['room_name', 'earliest_ts', 'last_chips', 'first_chips', 'finish_busted']}

    # Resolve the tournament's static config from Firebase (per-tournament values
    # with a canonical fallback) and run the post-tournament analyser so the
    # graphs get the ACTUAL level per hand plus rebuy/add-on spots. This may be
    # slow on large tournaments — acceptable for now; it is a pure function
    # designed to be reused later by an asynchronous Cowork skill.
    cfg = _resolve_tournament_cfg(meta.get('room_name') or '')
    analysis = analyze_tournament(records, cfg)

    for key in ('itm_h', 'end_h', 'ft_h', 'max_blinds', 'late_reg_level',
                'level_duration_min', 'level_duration_rebuy_min',
                'level_duration_ft_min', 'blind_levels', 'graph_ready',
                'graph_missing_fields', 'graph_config_found'):
        meta[key] = cfg.get(key)
    if not meta.get('graph_ready'):
        if not meta.get('graph_config_found'):
            meta['graph_warning'] = (
                'Graph cannot be displayed because this tournament has no matching '
                'configuration in the tournaments table.'
            )
        else:
            fields = ', '.join(meta.get('graph_missing_fields') or [])
            meta['graph_warning'] = (
                'Graph cannot be displayed because this tournament configuration '
                f'is missing: {fields}.'
            )
    meta['rebuys'] = analysis['rebuys']
    meta['addons'] = analysis['addons']
    meta['spots']  = analysis['spots']

    rows = build_hand_rows(records)
    hand_levels = analysis['hand_levels']
    for row in rows:
        row['level'] = hand_levels.get(row.get('hand_num'))

    return jsonify({'hands': rows, 'meta': meta})


@app.route('/api/tournaments/<tourney_id>/export', methods=['POST'])
def export_persisted_tournament(tourney_id):
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    records, _doc = _fetch_tournament_records(uid, tourney_id)
    if records is None:
        return jsonify({'error': 'Tournament data not available'}), 404

    body     = request.get_json(force=True, silent=True) or {}
    platform = (body.get('platform') or '').strip()
    try:
        filepath, _ = export_pokerstars(records, platform=platform,
                                         blind_levels_by_room=_blind_levels_by_room(records))
        return send_file(
            os.path.abspath(filepath),
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype='text/plain',
        )
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/tournaments/<tourney_id>/export/json', methods=['POST'])
def export_persisted_tournament_json(tourney_id):
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    records, doc = _fetch_tournament_records(uid, tourney_id)
    if records is None:
        return jsonify({'error': 'Tournament data not available'}), 404

    import json as _jj
    room     = _re.sub(r'[^A-Za-z0-9]', '', (doc or {}).get('room_name', ''))[:24]
    filename = f"pppoker_{room}_{tourney_id}.json" if room else f"pppoker_tourney{tourney_id}.json"
    data = _jj.dumps(records, indent=2)
    return Response(data, mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.route('/api/tournaments/<tourney_id>/export/hand', methods=['POST'])
def export_persisted_hand(tourney_id):
    _get_admin_db()  # ensure Firebase Admin SDK is initialized before token verification
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    body        = request.get_json(force=True, silent=True) or {}
    raw_hand_id = (body.get('hand_id') or '').strip()
    hand_id     = raw_hand_id.replace('-', '')
    platform    = (body.get('platform') or '').strip()
    if not hand_id:
        return jsonify({'error': 'hand_id required'}), 400

    records, _doc = _fetch_tournament_records(uid, tourney_id)
    if records is None:
        return jsonify({'error': 'Tournament data not available'}), 404

    match = next(
        (r for r in records if r.get('summary', {}).get('D', '').replace('-', '') == hand_id),
        None,
    )
    if not match:
        return jsonify({'error': f"Hand '{raw_hand_id}' not found."}), 404

    try:
        filepath, _ = export_pokerstars([match], platform=platform,
                                         blind_levels_by_room=_blind_levels_by_room([match]))
        return send_file(
            os.path.abspath(filepath),
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype='text/plain',
        )
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/tournaments/<tourney_id>/export/json/hand', methods=['POST'])
def export_persisted_hand_json(tourney_id):
    _get_admin_db()  # ensure Firebase Admin SDK is initialized before token verification
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    body        = request.get_json(force=True, silent=True) or {}
    raw_hand_id = (body.get('hand_id') or '').strip()
    hand_id     = raw_hand_id.replace('-', '')
    if not hand_id:
        return jsonify({'error': 'hand_id required'}), 400

    records, _doc = _fetch_tournament_records(uid, tourney_id)
    if records is None:
        return jsonify({'error': 'Tournament data not available'}), 404

    match = next(
        (r for r in records if r.get('summary', {}).get('D', '').replace('-', '') == hand_id),
        None,
    )
    if not match:
        return jsonify({'error': f"Hand '{raw_hand_id}' not found."}), 404

    import json as _jj
    filename = f"pppoker_hand_{raw_hand_id}.json"
    data = _jj.dumps(match, indent=2)
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


# ── Leak Finder ──────────────────────────────────────────────────────────────
# /leaks — the user-facing report (Phase 1: preflop stats, per position).
# /leaks/validate — dev page diffing leak_engine output against the PT4 report
# CSV ground truth in data/validation/ (aggregate fixture counts only, so it
# is intentionally unauthenticated).

_BBZ_RANGES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'data', 'bbz_leak_ranges.json')


def _load_bbz_targets():
    """{(position, stat_label): {'target': [lo, hi], 'rec': str}} from the
    harvested BBZ range table (design doc §7)."""
    import json as _jj
    try:
        with open(_BBZ_RANGES_PATH, encoding='utf-8') as fh:
            data = _jj.load(fh)
    except Exception:
        return {}
    out = {}
    for pos, pdata in (data.get('positions') or {}).items():
        for s in pdata.get('stats') or []:
            out[(pos, s['label'])] = {'target': s.get('target'), 'rec': s.get('rec')}
    return out


@app.route('/leaks')
def leaks_page():
    return render_template('leaks.html')


# ── Per-tournament leak cache ────────────────────────────────────────────────
# A tournament's hands compress to a count-vector (~490 numbers) that is
# additive: summing vectors equals re-counting hands. Caching those makes a
# filtered report one Firestore read plus arithmetic, instead of re-reading
# and re-parsing every hand blob (~10s) on every filter change.

def _leak_cache_ref(db, uid, tid):
    return db.collection('users').document(uid).collection('leak_cache').document(tid)


def _build_leak_vector(uid, tid):
    """Parse one tournament's hands and compress them to a count-vector.
    Returns (vector, hands_used, hands_skipped) or None when unreadable."""
    from hand_exporter import records_to_ps_text
    from leak_engine import parse_ps_text, validate_pot, hands_to_vector

    records, _doc = _fetch_tournament_records(uid, tid)
    if not records:
        return None
    # No blind-ladder resolution here: it only affects the cosmetic "Level"
    # header, which the leak engine ignores, and costs a Firestore scan.
    text, _stats = records_to_ps_text(records)
    hands, skipped = [], 0
    for h in parse_ps_text(text):
        if validate_pot(h):
            skipped += 1          # PT4 drops these too — see design doc §5
        else:
            hands.append(h)
    return hands_to_vector(hands, scheme='report'), len(hands), skipped


def _load_leak_vectors(db, uid, tourney_docs, budget_s=25):
    """
    {tid: {'vector', 'hands', 'skipped'}} for the given tournaments, served
    from cache and rebuilt where missing or stale. A stale entry is one whose
    engine version or source `updated_at` no longer matches, so re-imports and
    stat-definition changes both invalidate automatically.

    Rebuilds are bounded by `budget_s`: anything past the budget is left for a
    later request and reported as pending, so a cold cache degrades to a
    partial report instead of hanging.
    """
    import json as _jj
    import time as _tt
    from leak_engine import ENGINE_VERSION

    cached = {}
    for snap in db.collection('users').document(uid).collection('leak_cache').get():
        cached[snap.id] = snap.to_dict()

    out, pending = {}, 0
    deadline = _tt.monotonic() + budget_s
    for tid, meta in tourney_docs.items():
        entry = cached.get(tid)
        fresh = (entry
                 and entry.get('v') == ENGINE_VERSION
                 and entry.get('src_updated_at') == meta.get('updated_at'))
        if fresh:
            try:
                out[tid] = {'vector': _jj.loads(entry['data']),
                            'hands': entry.get('hands', 0),
                            'skipped': entry.get('skipped', 0)}
                continue
            except (ValueError, KeyError):
                pass              # corrupt entry — fall through and rebuild
        if _tt.monotonic() >= deadline:
            pending += 1
            continue
        built = _build_leak_vector(uid, tid)
        if not built:
            continue
        vector, n_hands, n_skipped = built
        out[tid] = {'vector': vector, 'hands': n_hands, 'skipped': n_skipped}
        try:
            _leak_cache_ref(db, uid, tid).set({
                'v': ENGINE_VERSION,
                'src_updated_at': meta.get('updated_at'),
                'data': _jj.dumps(vector, separators=(',', ':')),
                'hands': n_hands, 'skipped': n_skipped,
                'built_at': int(_tt.time()),
            })
        except Exception:
            pass                  # cache write is best-effort, never fatal
    return out, pending


@app.route('/api/leaks')
def leaks_api():
    from leak_engine import (POSITION_BUCKETS, ALL_STATS, STAT_STREET, classify,
                             delta_from_target, MIN_SAMPLE, merge_vectors)

    uid = _verify_bearer(request)   # inits the Admin SDK internally
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    db = _get_admin_db()
    user_snap = db.collection('users').document(uid).get()
    if not user_snap.exists or not user_snap.to_dict().get('is_pro'):
        return jsonify({'error': 'Pro subscription required'}), 403

    # ── Available tournaments (filter options) ──
    tourneys = {}
    for doc in db.collection('users').document(uid).collection('tournaments').get():
        d = doc.to_dict()
        room = d.get('room_name') or ''
        tourneys[doc.id] = {
            'room_key': _norm_room_name(room) or '(unnamed)',
            'room_label': room or '(unnamed)',
            'earliest_ts': d.get('earliest_ts'),
            'updated_at': d.get('updated_at'),
            'hands': d.get('hands', 0),
        }

    rooms = {}
    for meta in tourneys.values():
        r = rooms.setdefault(meta['room_key'],
                             {'key': meta['room_key'], 'label': meta['room_label'],
                              'tournaments': 0, 'hands': 0})
        r['tournaments'] += 1
        r['hands'] += meta['hands']
    all_ts = [m['earliest_ts'] for m in tourneys.values() if m['earliest_ts']]

    # ── Apply filters (they select whole tournaments, never partial ones) ──
    want_rooms = {r for r in (request.args.get('rooms') or '').split(',') if r}
    def _ts_arg(name):
        raw = (request.args.get(name) or '').strip()
        if not raw:
            return None
        try:
            from datetime import datetime as _d, timezone as _tz
            return int(_d.strptime(raw, '%Y-%m-%d')
                       .replace(tzinfo=_tz.utc).timestamp())
        except ValueError:
            return None
    ts_from, ts_to = _ts_arg('from'), _ts_arg('to')
    if ts_to is not None:
        ts_to += 86399            # 'to' is inclusive of the whole day

    selected = {}
    for tid, meta in tourneys.items():
        if want_rooms and meta['room_key'] not in want_rooms:
            continue
        ts = meta['earliest_ts']
        if ts_from is not None and (ts is None or ts < ts_from):
            continue
        if ts_to is not None and (ts is None or ts > ts_to):
            continue
        selected[tid] = meta

    # ── Cached vectors → one summed aggregate ──
    unadjusted = 0
    try:
        from equity import set_budget, skipped_count, save_cache
        set_budget(20)
    except Exception:
        set_budget = skipped_count = save_cache = None

    loaded, pending = _load_leak_vectors(db, uid, selected)

    if save_cache:
        try:
            save_cache()          # persist any newly enumerated all-in equities
            unadjusted = skipped_count()
        except Exception:
            pass
        set_budget(None)

    agg = merge_vectors(v['vector'] for v in loaded.values())
    total_skipped = sum(v['skipped'] for v in loaded.values())
    targets = _load_bbz_targets()

    positions = []
    for bucket in POSITION_BUCKETS:
        p = agg['positions'][bucket]
        rows = []
        for key, label, _is_global in ALL_STATS:
            st = p['stats'][key]
            made, opp = st['made'], st['opp']
            t = targets.get((bucket, label)) or {}
            pct = round(made / opp * 100, 2) if opp else None
            target = t.get('target')
            result = classify(pct, target, opp)
            delta = delta_from_target(pct, target)
            rows.append({'key': key, 'label': label, 'pct': pct,
                         'made': made, 'opp': opp, 'street': STAT_STREET[key],
                         'target': target, 'rec': t.get('rec'), 'result': result,
                         'delta': round(delta, 3) if delta is not None else None})
        n = p['hands']
        positions.append({
            'position': bucket, 'hands': n, 'stats': rows,
            'winrate_bb100': round(p['bb_adj'] / n * 100, 2) if n else None,
            'winrate_raw_bb100': round(p['bb'] / n * 100, 2) if n else None,
        })

    total_hands = sum(p['hands'] for p in positions)
    total_adj = sum(agg['positions'][b]['bb_adj'] for b in POSITION_BUCKETS)
    total_raw = sum(agg['positions'][b]['bb'] for b in POSITION_BUCKETS)
    from datetime import datetime as _dt2, timezone as _tz2
    def _day(ts):
        return _dt2.fromtimestamp(ts, tz=_tz2).strftime('%Y-%m-%d') if ts else None

    return jsonify({
        'meta': {
            'tournaments': len(loaded), 'hands': total_hands,
            'hands_skipped': total_skipped, 'phase': 5, 'min_sample': MIN_SAMPLE,
            'hands_unadjusted': unadjusted,
            'tournaments_pending': pending,
            'winrate_bb100': round(total_adj / total_hands * 100, 2) if total_hands else None,
            'winrate_raw_bb100': round(total_raw / total_hands * 100, 2) if total_hands else None,
        },
        'filters': {
            'rooms': sorted(rooms.values(), key=lambda r: -r['hands']),
            'date_min': _day(min(all_ts)) if all_ts else None,
            'date_max': _day(max(all_ts)) if all_ts else None,
            'applied': {'rooms': sorted(want_rooms),
                        'from': request.args.get('from') or None,
                        'to': request.args.get('to') or None},
        },
        'positions': positions,
    })


@app.route('/leaks/validate')
def leaks_validate_page():
    return render_template('leaks_validate.html')


@app.route('/api/leaks/validate')
def leaks_validate_api():
    from leak_validation import run_all
    try:
        return jsonify(run_all())
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── PWA / TWA support ─────────────────────────────────────────────────────────

@app.route('/tournaments')
def tournaments_page():
    return render_template('tournaments.html')

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
