#!/usr/bin/env python3
"""
Bulk-export all MTT tournament hands for a user into a single
PokerStars-format TXT file (suitable for PT4 import).
"""
import json, os, sys, re

SA_KEY_PATH = os.path.join(os.path.dirname(__file__), 'serviceAccountKey.json')
BUCKET_NAME = 'pppoker-analyser.firebasestorage.app'
UID = 'NTjeyaLEYWeQVXAih2xdE1vlJyJ3'

import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore, storage as admin_storage

cred = credentials.Certificate(SA_KEY_PATH)
firebase_admin.initialize_app(cred)
db = admin_firestore.client()
bucket = admin_storage.bucket(name=BUCKET_NAME)

print('Listing tournaments …')
docs = db.collection('users').document(UID).collection('tournaments').get()

from hand_parser import classify_game, CATEGORY_TOURNAMENT

# Real-money MTTs only — cash tables, sit-and-gos and play-money games (which
# include play-money MTTs) would pollute a PT4 database with unreal stakes.
mtt_docs = []
cash_docs = []
for doc in docs:
    d = doc.to_dict()
    cls = classify_game(d.get('room_name') or '', d.get('is_mtt', False))
    if cls['category'] == CATEGORY_TOURNAMENT:
        mtt_docs.append((doc.id, d))
    else:
        cash_docs.append((doc.id, d))

print(f'  Total tournament docs      : {len(mtt_docs) + len(cash_docs)}')
print(f'  Real-money MTT (exporting) : {len(mtt_docs)}')
print(f'  Cash / play money (skip)   : {len(cash_docs)}')

all_records = []
failed = []
for i, (tid, d) in enumerate(mtt_docs, 1):
    storage_path = d.get('storage_path', f'tournaments/{UID}/{tid}.json')
    room = d.get('room_name', '?')
    hands = d.get('hands', 0)
    print(f'  [{i}/{len(mtt_docs)}] {room} ({hands}h) … ', end='', flush=True)
    blob = bucket.blob(storage_path)
    if not blob.exists():
        print('MISSING')
        failed.append(tid)
        continue
    records = json.loads(blob.download_as_bytes())
    all_records.extend(records)
    print(f'OK ({len(records)} records)')

print(f'\nTotal records collected: {len(all_records)}')
if failed:
    print(f'Missing blobs ({len(failed)}): {failed}')

if not all_records:
    print('Nothing to export.')
    sys.exit(1)

from hand_exporter import export_pokerstars

sys.path.insert(0, os.path.dirname(__file__))
from app import _blind_levels_by_room, _resolve_tournament_cfg, _norm_room_name

blind_map = _blind_levels_by_room(all_records)

filepath, stats = export_pokerstars(all_records, blind_levels_by_room=blind_map)
print(f'\nExported to: {filepath}')
print(f'  Attempted : {stats["attempted"]}')
print(f'  Converted : {stats["converted"]}')
print(f'  Warned    : {stats["warned"]}')
print(f'  Skipped   : {stats["skipped"]}')
