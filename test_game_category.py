"""
test_game_category.py — the real-money-MTT vs cash-&-play-money split.

Play-money games regressed into the Tournaments section because the only
signal in use was PPPoker's room.mtt flag, which is set for a play-money MTT
exactly as it is for a club MTT. classify_game adds the second signal (a real
club room name) and everything that decides a section, a leak-report room list
or an export must go through it. These tests pin that rule at every layer:
the pure classifier, process_hands (import path), and /api/tournaments
(the read path, which re-derives so pre-split docs are fixed without a
migration).

    python test_game_category.py
"""

import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')

from hand_parser import (classify_game, is_play_money_room, norm_room_name,
                         process_hands, CATEGORY_TOURNAMENT, CATEGORY_CASH_PLAY)

problems = []


def check(label, ok, extra=''):
    if not ok:
        problems.append(label + (' — ' + extra if extra else ''))


# ── 1. The classifier ────────────────────────────────────────────────────────

def test_classifier():
    # Real-money club MTT — the only thing that belongs in Tournaments.
    for name in ('DEEP FREEZE', '🌐 LUCKY DAY', 'Texas', '40-100BB JP SAT'):
        c = classify_game(name, True)
        check('real-money MTT is a tournament: %r' % name,
              c == {'is_play_money': False, 'category': CATEGORY_TOURNAMENT}, str(c))

    # The regression: room.mtt set, but no club room name → play money.
    for name in ('', '   ', 'Unknown', 'unknown', 'UNKNOWN', '(unknown)',
                 'N/A', 'none', '???', '—', 'Play Money'):
        c = classify_game(name, True)
        check('play-money MTT is cash_play: %r' % name,
              c == {'is_play_money': True, 'category': CATEGORY_CASH_PLAY}, str(c))

    # Single-table games are cash_play whichever side of the money they are on.
    check('named cash table is cash_play',
          classify_game('40-100BB JP', False)
          == {'is_play_money': False, 'category': CATEGORY_CASH_PLAY})
    check('play-money sit-and-go is cash_play',
          classify_game('Unknown', False)
          == {'is_play_money': True, 'category': CATEGORY_CASH_PLAY})

    # Truthiness, not identity — room.mtt arrives as a dict of table info.
    check('room.mtt dict counts as MTT',
          classify_game('DEEP FREEZE', {'table_num': '3'})['category']
          == CATEGORY_TOURNAMENT)
    check('room.mtt None counts as not-MTT',
          classify_game('DEEP FREEZE', None)['category'] == CATEGORY_CASH_PLAY)

    check('norm_room_name strips emoji and punctuation',
          norm_room_name('🌐 Lucky Day!') == 'LUCKY DAY',
          norm_room_name('🌐 Lucky Day!'))
    check('is_play_money_room agrees with classify_game',
          is_play_money_room('Unknown') and not is_play_money_room('Texas'))


# ── 2. The import path (process_hands) ───────────────────────────────────────

def _record(tid, room_name, mtt, seq=1):
    room = {'small_blind': 100, 'dealer_seatid': 0, 'room_name': room_name}
    if mtt:
        room['mtt'] = {'table_num': '1'}
    return {
        'share_key': 'k%s' % seq,
        'summary': {'D': '%s-%d' % (tid, seq), 'C': 1780272000 + seq,
                    'H': 0, 'G': 100, 'B': []},
        'full_hand': {
            'info': {'room': room,
                     'players': [{'seatid': 0, 'uid': 'u1', 'isSelf': True,
                                  'user_name': 'Hero', 'hand_chips': 10000}]},
            'flow': {'pre_flop': {'actions': []}},
        },
    }


def test_process_hands():
    records = [
        _record('11', 'DEEP FREEZE', True, 1),
        _record('22', 'Unknown', True, 2),     # play-money MTT
        _record('33', '', False, 3),           # play-money sit-and-go
        _record('44', '40-100BB JP', False, 4),  # real cash table
    ]
    _, _, _, tourneys = process_hands(records)
    by_room = {t['room_name']: t for t in tourneys}
    check('all four sessions parsed', len(tourneys) == 4, str(len(tourneys)))

    check('club MTT categorised as tournament',
          by_room['DEEP FREEZE']['category'] == CATEGORY_TOURNAMENT)
    check('club MTT not flagged play money',
          by_room['DEEP FREEZE']['is_play_money'] is False)

    play_mtt = by_room['Unknown']
    check('play-money MTT categorised as cash_play',
          play_mtt['category'] == CATEGORY_CASH_PLAY, str(play_mtt['category']))
    check('play-money MTT flagged play money', play_mtt['is_play_money'] is True)
    # is_mtt stays raw so hand exports still emit a tournament header for it.
    check('play-money MTT keeps raw is_mtt for exports',
          bool(play_mtt['is_mtt']) is True)

    check('play-money sit-and-go categorised as cash_play',
          by_room['']['category'] == CATEGORY_CASH_PLAY)
    check('named cash table categorised as cash_play, not play money',
          by_room['40-100BB JP']['category'] == CATEGORY_CASH_PLAY
          and by_room['40-100BB JP']['is_play_money'] is False)


# ── 3. The read path (/api/tournaments re-derives) ───────────────────────────

def test_api_reclassifies_stored_docs():
    import app as A

    class _Snap:
        def __init__(self, data): self._d = data
        def to_dict(self): return dict(self._d)

    class _Col:
        def __init__(self, docs): self._docs = docs
        def get(self): return [_Snap(d) for d in self._docs]

    class _Doc:
        def __init__(self, docs): self._docs = docs
        def collection(self, _n): return _Col(self._docs)

    class _Root:
        def __init__(self, docs): self._docs = docs
        def collection(self, _n): return self
        def document(self, _i): return _Doc(self._docs)

    # Docs as they were written before the split: a play-money MTT stored with
    # is_mtt=True and no category field at all. The read must still demote it.
    docs = [
        {'tourney_id': '11', 'room_name': 'DEEP FREEZE', 'is_mtt': True,
         'storage_path': 'secret/path.json'},
        {'tourney_id': '22', 'room_name': 'Unknown', 'is_mtt': True},
        {'tourney_id': '33', 'room_name': '', 'is_mtt': False},
        # A stale wrong category written by an older deploy must be overridden.
        {'tourney_id': '44', 'room_name': 'Unknown', 'is_mtt': True,
         'category': CATEGORY_TOURNAMENT, 'is_play_money': False},
    ]

    orig_db, orig_verify = A._get_admin_db, A._verify_bearer
    A._get_admin_db = lambda: _Root(docs)
    A._verify_bearer = lambda req: 'test-uid'
    try:
        client = A.app.test_client()
        res = client.get('/api/tournaments')
        body = res.get_json()
    finally:
        A._get_admin_db, A._verify_bearer = orig_db, orig_verify

    check('tournaments listed', res.status_code == 200 and body, str(res.status_code))
    by_id = {t['tourney_id']: t for t in (body or {}).get('tournaments', [])}
    check('club MTT stays a tournament',
          by_id['11']['category'] == CATEGORY_TOURNAMENT)
    check('storage_path still stripped from the response',
          'storage_path' not in by_id['11'])
    check('legacy play-money MTT demoted on read',
          by_id['22']['category'] == CATEGORY_CASH_PLAY
          and by_id['22']['is_play_money'] is True, str(by_id['22']))
    check('unnamed single-table game stays cash_play',
          by_id['33']['category'] == CATEGORY_CASH_PLAY)
    check('stale stored category overridden on read',
          by_id['44']['category'] == CATEGORY_CASH_PLAY
          and by_id['44']['is_play_money'] is True, str(by_id['44']))


def main():
    test_classifier()
    test_process_hands()
    test_api_reclassifies_stored_docs()
    for p in problems:
        print('  FAIL', p)
    print('game category: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
