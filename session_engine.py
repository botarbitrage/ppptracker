"""
session_engine.py — PokerPulse session-stats engine (Notion task F1-1).

Computes a subscriber's stats for a hand-level time window directly from raw
PPPoker hand records — never from the precomputed per-tournament Firestore
fields (vpip_pct/pfr_pct/af/wtsd_pct on users/{uid}/tournaments/{tid}), so the
same engine works standalone today and as a future scheduled recurring
routine over all data in a period (see the "F1 — PokerPulse MVP" Feature
spec).

Pure functions, no I/O (mirrors hand_exporter.py / leak_engine.py /
tournament_analyzer.py's design): records go in, stats come out. The caller
owns fetching a uid's tournament docs + hand JSON (Cloud Storage) and does
not persist any run history — this module has no write path and no cache.

Windowing happens at hand level, not tournament level: pass the full set of
candidate records (spanning any number of tournaments, any order) plus a
half-open [start_ts, end_ts) window; a tournament whose hands straddle the
window boundary naturally has only its in-window hands counted, so the same
tournament can contribute to two adjacent windows with no special-casing.

VPIP/PFR/3-bet/steal/check-raise/c-bet/fold-to-* percentages are computed
over the in-window hands that convert cleanly into the validated
PokerStars-dialect action model (hand_exporter.records_to_ps_text →
leak_engine.parse_ps_text) — the same battle-tested action normalization
(zero-chip call suppression, incremental-raise correction, all-in capping,
and the canonical fold/check semantics for action types 12/13 documented in
docs/pppoker-action-model.md) the Leak Finder and PT4 exports already rely
on, rather than re-deriving raw action-code parsing a second time.
total_hands/total_games/hands_per_street are computed straight off the raw
records instead, since those need no action semantics at all and should
count every in-window hand regardless of whether it converts cleanly.

Stat definitions follow the standard, simple convention used by mainstream
trackers' basic stat panel (PT4/HM3), not the Leak Finder's granular
HU/3-bet-pot-split report columns:
  - check-raise% / fold-to-bet% / c-bet% / fold-to-c-bet% are all FLOP-only
    (the conventional default when a tracker shows one unqualified number
    for these, rather than separate flop/turn/river variants), and each is
    a single made/opp decision per hand — hero's first bet-facing decision
    on the flop, not every subsequent one.
  - steal% is raise-first-in from BTN/CO/SB specifically.
  - 3-bet% is the standard preflop re-raise-facing-one-raise definition.
"""

from hand_exporter import records_to_ps_text
from hand_parser import extract_tourney_id
from leak_engine import parse_ps_text, preflop_stat_flags, pt4_positions, _street_walk, _folded_by

# PT4 numeric position scheme (see leak_engine.position_bucket): 0=BTN,
# 1=CO, 9=SB — the three seats a steal attempt is made from.
_STEAL_POSITIONS = (0, 1, 9)

_STAT_KEYS = (
    'vpip', 'pfr', 'three_bet', 'steal', 'check_raise', 'fold_to_bet',
    'cbet', 'fold_to_cbet',
)


def _in_window(record, start_ts, end_ts):
    ts = (record.get('summary') or {}).get('C', 0)
    return start_ts <= ts < end_ts


def _hands_per_street_bucket(record):
    """
    The last street hero was active on for one raw record: 'Preflop' | 'Flop'
    | 'Turn' | 'River' | 'Showdown', or None when no hero seat is identified.
    Computed directly off the raw record — independent of the PS-text/IR
    pipeline used for the percentage stats below.
    """
    fh = record.get('full_hand', {})
    flow = fh.get('flow', {})
    players = fh.get('info', {}).get('players', [])
    hero = next((p for p in players if p.get('isSelf')), None)
    if not hero:
        return None
    hero_seatid = hero.get('seatid')
    hero_uid = hero.get('uid')
    sd_res = flow.get('showdown_result', [])
    if any(sr.get('uid') == hero_uid and sr.get('is_showdown') for sr in sd_res):
        return 'Showdown'
    for street, label in (('river', 'River'), ('turn', 'Turn'), ('flop', 'Flop')):
        if any(a.get('seatid') == hero_seatid
               for a in flow.get(street, {}).get('actions', [])):
            return label
    return 'Preflop'


def _hero_hand_flags(hand):
    """
    {key: (made, opp)} for one leak_engine IR hand, hero-perspective. Every
    stat has at most one opportunity per hand — including check_raise and
    fold_to_bet, which look only at hero's first bet-facing decision on the
    flop, matching the standard single-number-per-stat convention.
    """
    stats = {k: (0, 0) for k in _STAT_KEYS}
    hero = hand.get('hero')
    if not hero:
        return stats

    hero_pre = [a for a in hand['streets']['preflop'] if a['name'] == hero]
    stats['vpip'] = (1 if any(a['verb'] in ('call', 'raise') for a in hero_pre) else 0, 1)
    stats['pfr'] = (1 if any(a['verb'] == 'raise' for a in hero_pre) else 0, 1)

    pre_flags, pre_ctx = preflop_stat_flags(hand, with_context=True)
    stats['three_bet'] = pre_flags['threebet_pf']

    made_rfi, opp_rfi = pre_flags['raise_first']
    is_steal_pos = pt4_positions(hand).get(hero) in _STEAL_POSITIONS
    stats['steal'] = (made_rfi if is_steal_pos else 0, opp_rfi if is_steal_pos else 0)

    pfa = pre_ctx['pfa']
    board = hand.get('board') or []
    if len(board) < 3 or hero in _folded_by(hand, 'flop'):
        return stats

    ctxs, _info = _street_walk(hand, 'flop', hero)
    if not ctxs:
        return stats

    first = ctxs[0]
    facing = next((c for c in ctxs if c['facing'] > 0), None)

    if hero == pfa and first['n_bets'] == 0:
        stats['cbet'] = (1 if first['verb'] == 'bet' else 0, 1)

    if hero != pfa and facing is not None and facing['first_bettor'] == pfa:
        stats['fold_to_cbet'] = (1 if facing['verb'] == 'fold' else 0, 1)

    if first['verb'] == 'check' and facing is not None:
        stats['check_raise'] = (1 if facing['verb'] == 'raise' else 0, 1)

    if facing is not None:
        stats['fold_to_bet'] = (1 if facing['verb'] == 'fold' else 0, 1)

    return stats


def compute_session_stats(records, start_ts, end_ts):
    """
    records: the full/unfiltered list of raw PPPoker hand records (any number
    of tournaments, any order) that might overlap the window.
    start_ts/end_ts: epoch-second window, half-open [start_ts, end_ts).

    Returns a dict of session stats computed purely from the in-window hands.
    No I/O, no Firestore/Storage access; nothing is written or cached — the
    caller owns fetching `records` and (per the MVP spec) does NOT persist
    this result as run history.
    """
    windowed = [r for r in records if _in_window(r, start_ts, end_ts)]

    total_hands = len(windowed)
    tourney_ids = {extract_tourney_id((r.get('summary') or {}).get('D', ''))
                   for r in windowed}
    total_games = len(tourney_ids)

    hands_per_street = {'Preflop': 0, 'Flop': 0, 'Turn': 0, 'River': 0, 'Showdown': 0}
    for r in windowed:
        bucket = _hands_per_street_bucket(r)
        if bucket:
            hands_per_street[bucket] += 1

    text, _export_stats = records_to_ps_text(windowed)
    ir_hands = parse_ps_text(text)

    totals = {k: [0, 0] for k in _STAT_KEYS}
    for hand in ir_hands:
        for key, (made, opp) in _hero_hand_flags(hand).items():
            totals[key][0] += made
            totals[key][1] += opp

    def pct(key):
        made, opp = totals[key]
        return round(made / opp * 100, 1) if opp else 0.0

    return {
        'window_start': start_ts,
        'window_end': end_ts,
        'total_hands': total_hands,
        'total_games': total_games,
        'hands_parsed': len(ir_hands),
        'hands_per_street': hands_per_street,
        'vpip_pct': pct('vpip'),
        'pfr_pct': pct('pfr'),
        'steal_pct': pct('steal'),
        'check_raise_pct': pct('check_raise'),
        'three_bet_pct': pct('three_bet'),
        'fold_to_bet_pct': pct('fold_to_bet'),
        'cbet_pct': pct('cbet'),
        'fold_to_cbet_pct': pct('fold_to_cbet'),
    }
