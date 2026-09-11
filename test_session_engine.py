"""
Unit tests for session_engine.compute_session_stats against a fixed set of
synthetic raw PPPoker hand records with hand-computed expected stats.

Covers: total_hands/total_games/hands_per_street, VPIP/PFR/3-bet/steal/
check-raise/fold-to-bet/c-bet/fold-to-c-bet percentages, and the hand-level
window-split requirement (a tournament whose hands straddle a window
boundary contributes only its in-window hands to each side, and can appear
in both).

Run with:  python3 test_session_engine.py   (exits non-zero on any failure)
"""
import sys

from session_engine import compute_session_stats

FAILURES = []


def check(label, actual, expected):
    ok = actual == expected
    if not ok:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {actual!r}")


# ── Fixture builder ──────────────────────────────────────────────────────────

def make_hand(gameid, ts, *, dealer, players, pre, flop=None, winner_seatid,
              sb=100):
    """
    players: list of (seatid, name, isSelf) — every player gets a large,
    uniform stack (raw chips) so no all-in/capping edge cases trigger.
    pre / flop: list of raw PPPoker action dicts {seatid, type, chips}.
    """
    flow = {'pre_flop': {'actions': pre}}
    if flop is not None:
        flow['flop'] = {'actions': flop, 'cards': [258, 259, 260]}
    flow['showdown_result'] = []
    flow['winning_info'] = [{'seatid': winner_seatid, 'chips': 1, 'poolid': 0}]
    return {
        'summary': {'D': gameid, 'C': ts, 'G': sb, 'A': 0, 'H': 0},
        'share_key': 'x',
        'full_hand': {
            'info': {
                'cards': [258, 259],
                'room': {'small_blind': sb, 'ante': 0, 'dealer_seatid': dealer,
                         'mtt': {'table_num': '1'}, 'room_name': 'TEST ROOM'},
                'players': [
                    {'seatid': s, 'user_name': n, 'uid': f'u{s}',
                     'hand_chips': 1000000, 'isSelf': is_self}
                    for s, n, is_self in players
                ],
            },
            'flow': flow,
        },
    }


PLAYERS = [(0, 'Hero', True), (1, 'Villain1', False), (2, 'Villain2', False)]

# Tournament 1001, hand 1 @ ts=1000: hero on BTN (seat0), raise-first (steal
# opp), heads-up flop vs BB, hero c-bets and wins uncontested.
HAND1 = make_hand(
    '1-1001-1', 1000, dealer=0, players=PLAYERS, winner_seatid=0,
    pre=[
        {'seatid': 1, 'type': 8, 'chips': 100},    # SB posts
        {'seatid': 2, 'type': 9, 'chips': 200},    # BB posts
        {'seatid': 0, 'type': 4, 'chips': 600},    # Hero (BTN) raises to 600
        {'seatid': 1, 'type': 1, 'chips': 0},      # SB folds
        {'seatid': 2, 'type': 3, 'chips': 400},    # BB calls (400 more)
    ],
    flop=[
        {'seatid': 2, 'type': 2, 'chips': 0},      # BB checks
        {'seatid': 0, 'type': 7, 'chips': 300},    # Hero bets (c-bet)
        {'seatid': 2, 'type': 1, 'chips': 0},      # BB folds
    ],
)

# Tournament 1001, hand 2 @ ts=2000: hero on BB (seat0), faces a BTN open,
# 3-bets, gets called, then check-raises the flop.
HAND2 = make_hand(
    '1-1001-2', 2000, dealer=1, players=PLAYERS, winner_seatid=0,
    pre=[
        {'seatid': 2, 'type': 8, 'chips': 100},    # SB posts
        {'seatid': 0, 'type': 9, 'chips': 200},    # Hero (BB) posts
        {'seatid': 1, 'type': 4, 'chips': 500},    # BTN opens to 500
        {'seatid': 2, 'type': 1, 'chips': 0},      # SB folds
        {'seatid': 0, 'type': 4, 'chips': 1400},   # Hero 3-bets to 1400
        {'seatid': 1, 'type': 3, 'chips': 900},    # BTN calls (900 more)
    ],
    flop=[
        {'seatid': 0, 'type': 2, 'chips': 0},      # Hero checks
        {'seatid': 1, 'type': 7, 'chips': 800},    # BTN bets
        {'seatid': 0, 'type': 4, 'chips': 2400},   # Hero check-raises to 2400
        {'seatid': 1, 'type': 1, 'chips': 0},      # BTN folds
    ],
)

# Tournament 1001, hand 3 @ ts=5000 (on the other side of the 3000 test
# boundary — same tournament straddles it): hero on BB, calls a BTN open,
# faces the BTN's flop c-bet and folds.
HAND3 = make_hand(
    '1-1001-3', 5000, dealer=1, players=PLAYERS, winner_seatid=1,
    pre=[
        {'seatid': 2, 'type': 8, 'chips': 100},    # SB posts
        {'seatid': 0, 'type': 9, 'chips': 200},    # Hero (BB) posts
        {'seatid': 1, 'type': 4, 'chips': 500},    # BTN opens to 500
        {'seatid': 2, 'type': 1, 'chips': 0},      # SB folds
        {'seatid': 0, 'type': 3, 'chips': 300},    # Hero calls (300 more)
    ],
    flop=[
        {'seatid': 0, 'type': 2, 'chips': 0},      # Hero checks
        {'seatid': 1, 'type': 7, 'chips': 600},    # BTN bets (c-bet)
        {'seatid': 0, 'type': 1, 'chips': 0},      # Hero folds
    ],
)

# Tournament 1002, hand 4 @ ts=9000: separate tournament, hero pure-folds
# preflop with zero voluntary chips in.
HAND4 = make_hand(
    '1-1002-1', 9000, dealer=1, players=PLAYERS, winner_seatid=1,
    pre=[
        {'seatid': 0, 'type': 8, 'chips': 100},    # Hero (SB) posts
        {'seatid': 2, 'type': 9, 'chips': 200},    # BB posts
        {'seatid': 1, 'type': 4, 'chips': 500},    # BTN raises
        {'seatid': 0, 'type': 1, 'chips': 0},      # Hero (SB) folds
        {'seatid': 2, 'type': 1, 'chips': 0},      # BB folds
    ],
)

ALL_RECORDS = [HAND1, HAND2, HAND3, HAND4]


def main():
    print("Window [0, 3000) — hand1 + hand2 only:")
    a = compute_session_stats(ALL_RECORDS, 0, 3000)
    check('  total_hands', a['total_hands'], 2)
    check('  total_games', a['total_games'], 1)
    check('  hands_parsed', a['hands_parsed'], 2)
    check('  hands_per_street', a['hands_per_street'],
          {'Preflop': 0, 'Flop': 2, 'Turn': 0, 'River': 0, 'Showdown': 0})
    check('  vpip_pct', a['vpip_pct'], 100.0)
    check('  pfr_pct', a['pfr_pct'], 100.0)
    check('  three_bet_pct', a['three_bet_pct'], 100.0)
    check('  steal_pct', a['steal_pct'], 100.0)
    check('  check_raise_pct', a['check_raise_pct'], 100.0)
    check('  fold_to_bet_pct', a['fold_to_bet_pct'], 0.0)
    check('  cbet_pct', a['cbet_pct'], 50.0)
    check('  fold_to_cbet_pct', a['fold_to_cbet_pct'], 0.0)

    print("Window [3000, 6000) — hand3 only (same tournament as above):")
    b = compute_session_stats(ALL_RECORDS, 3000, 6000)
    check('  total_hands', b['total_hands'], 1)
    check('  total_games', b['total_games'], 1)
    check('  vpip_pct', b['vpip_pct'], 100.0)
    check('  pfr_pct', b['pfr_pct'], 0.0)
    check('  three_bet_pct', b['three_bet_pct'], 0.0)
    check('  steal_pct', b['steal_pct'], 0.0)
    check('  fold_to_bet_pct', b['fold_to_bet_pct'], 100.0)
    check('  cbet_pct', b['cbet_pct'], 0.0)
    check('  fold_to_cbet_pct', b['fold_to_cbet_pct'], 100.0)

    print("Tournament 1001 appears as a game in BOTH adjacent windows"
          " (hand-level split, not tournament-level):")
    check('  T1001 in window A', a['total_games'] >= 1, True)
    check('  T1001 in window B', b['total_games'] >= 1, True)

    print("Full window [0, 10000) — all 4 hands, 2 tournaments:")
    c = compute_session_stats(ALL_RECORDS, 0, 10000)
    check('  total_hands', c['total_hands'], 4)
    check('  total_games', c['total_games'], 2)
    check('  hands_parsed', c['hands_parsed'], 4)
    check('  hands_per_street', c['hands_per_street'],
          {'Preflop': 1, 'Flop': 3, 'Turn': 0, 'River': 0, 'Showdown': 0})
    check('  vpip_pct', c['vpip_pct'], 75.0)
    check('  pfr_pct', c['pfr_pct'], 50.0)
    # made=1 (hand2's 3-bet); opp=3 — hand2 and hand3 face a raise as the BB,
    # and hand4's hero (SB) also faces exactly one raise before folding, a
    # declined 3-bet opportunity in its own right.
    check('  three_bet_pct', c['three_bet_pct'], 33.3)
    check('  steal_pct', c['steal_pct'], 100.0)
    check('  check_raise_pct', c['check_raise_pct'], 50.0)
    check('  fold_to_bet_pct', c['fold_to_bet_pct'], 50.0)
    check('  cbet_pct', c['cbet_pct'], 50.0)
    check('  fold_to_cbet_pct', c['fold_to_cbet_pct'], 100.0)

    print("Empty input:")
    empty = compute_session_stats([], 0, 1000)
    check('  total_hands', empty['total_hands'], 0)
    check('  total_games', empty['total_games'], 0)
    check('  vpip_pct', empty['vpip_pct'], 0.0)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nAll session_engine tests passed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
