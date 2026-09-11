"""
Unit + integration tests for highlights.py (Notion task F1-2).

Unit tests exercise each pattern detector directly against hand-crafted IR
hand dicts (the leak_engine.parse_ps_text shape) — precise and fast, and
independent of the raw-record/PS-text round trip, which hand_exporter.py's
own test suite already covers.

The integration test exercises the full pipeline (raw record ->
records_to_ps_blocks_with_stacks -> parse_ps_text -> detectors) against one
hand-crafted raw record for "winning triple barrel, no showdown" (chosen
because — unlike lucky river or a showdown triple barrel — it needs no
card-id encoding or showdown reveals to construct), plus the existing
test_session_engine.py fixtures for a lightweight biggest-win/biggest-loss +
"nothing crashes across a mixed multi-hand window" check.

Run with:  python3 test_highlights.py   (exits non-zero on any failure)
"""
import sys

import highlights
from highlights import (detect_session_highlights, _detect_lucky_river,
                        _detect_triple_barrel, _detect_knock_off,
                        render_highlight_svg, svg_data_uri)
from test_session_engine import ALL_RECORDS, HAND1, HAND3, make_hand, PLAYERS

FAILURES = []


def _short(v):
    r = repr(v)
    return r if len(r) <= 120 else r[:117] + '...'


def check(label, actual, expected=None, cond=None):
    ok = (actual == expected) if cond is None else cond
    if not ok:
        FAILURES.append(f"{label}: got {actual!r}" + (f", expected {expected!r}" if cond is None else ''))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {_short(actual)}")


# ── IR-hand builder for unit tests ───────────────────────────────────────────

def _ir_hand(*, hero='Hero', seats=None, hero_cards='', board=None, shows=None,
            streets=None, collected=None, stacks=None):
    return {
        'hand_id': 'h1', 'tourney_id': 't1', 'table_size': len(seats or {}),
        'btn_seat': 0, 'sb_amt': 100, 'bb_amt': 200, 'ante': 0,
        'seats': seats or {0: hero, 1: 'Villain'},
        'stacks': stacks or {},
        'hero': hero, 'hero_cards': hero_cards,
        'sb_seat': 0, 'bb_seat': 1, 'posts': [],
        'streets': streets or {'preflop': [], 'flop': [], 'turn': [], 'river': []},
        'board': board or [], 'shows': shows or {}, 'collected': collected or [],
        'uncalled': [], 'total_pot': None,
    }


def main():
    # ── Unit: lucky river ────────────────────────────────────────────────────
    # Hero 7h7d vs Villain TcTd — villain ahead through the turn (pair of tens
    # beats pair of sevens), hero rivers a third seven and wins at showdown.
    behind_then_wins = _ir_hand(
        hero_cards='7h 7d', board=['2h', '3c', '9s', '4d', '7c'],
        shows={'Villain': 'Tc Td'},
        collected=[{'name': 'Hero', 'amount': 1000, 'pot': 0}],
    )
    m = _detect_lucky_river(behind_then_wins, {})
    check('lucky river fires when hero was behind on the turn and rivers a win',
          len(m), 1)
    check('lucky river match is labelled', m[0]['pattern'] if m else None, 'lucky_river')

    # Same cards, but hero was already ahead on the turn (pair of sevens beats
    # villain's pair of fours before the river) — not "lucky", just won.
    already_ahead = _ir_hand(
        hero_cards='7h 7d', board=['2h', '3c', '9s', '4d', 'Kc'],
        shows={'Villain': '4c 4s'},
        collected=[{'name': 'Hero', 'amount': 1000, 'pot': 0}],
    )
    check('lucky river does not fire when hero was already ahead on the turn',
          _detect_lucky_river(already_ahead, {}), [])

    # Hero wins but no villain showed cards — can't tell if it was "lucky".
    no_shows = _ir_hand(
        hero_cards='7h 7d', board=['2h', '3c', '9s', '4d', '7c'], shows={},
        collected=[{'name': 'Hero', 'amount': 1000, 'pot': 0}],
    )
    check('lucky river does not fire with no villain showdown cards known',
          _detect_lucky_river(no_shows, {}), [])

    # Hero lost the hand — never a "lucky river" regardless of card strength.
    lost = _ir_hand(
        hero_cards='7h 7d', board=['2h', '3c', '9s', '4d', '7c'],
        shows={'Villain': 'Tc Td'}, collected=[{'name': 'Villain', 'amount': 1000, 'pot': 0}],
    )
    check('lucky river does not fire when hero lost the hand', _detect_lucky_river(lost, {}), [])

    # Board hasn't reached the river yet.
    turn_only = _ir_hand(
        hero_cards='7h 7d', board=['2h', '3c', '9s', '4d'],
        shows={'Villain': 'Tc Td'}, collected=[{'name': 'Hero', 'amount': 1000, 'pot': 0}],
    )
    check('lucky river does not fire before the river', _detect_lucky_river(turn_only, {}), [])

    # ── Unit: winning triple barrel ──────────────────────────────────────────
    def barreled(showdown):
        return _ir_hand(
            streets={
                'preflop': [],
                'flop': [{'name': 'Hero', 'verb': 'bet', 'amount': 300, 'allin': False}],
                'turn': [{'name': 'Hero', 'verb': 'bet', 'amount': 500, 'allin': False}],
                'river': [{'name': 'Hero', 'verb': 'bet', 'amount': 800, 'allin': False}],
            },
            shows={'Hero': 'As Ks', 'Villain': 'Qh Qd'} if showdown else {},
            collected=[{'name': 'Hero', 'amount': 3000, 'pot': 0}],
        )

    m = _detect_triple_barrel(barreled(showdown=True), {})
    check('triple barrel (showdown) detected', [x['pattern'] for x in m], ['triple_barrel_showdown'])
    m = _detect_triple_barrel(barreled(showdown=False), {})
    check('triple barrel (no showdown) detected', [x['pattern'] for x in m],
          ['triple_barrel_no_showdown'])

    two_streets = _ir_hand(
        streets={
            'preflop': [],
            'flop': [{'name': 'Hero', 'verb': 'bet', 'amount': 300, 'allin': False}],
            'turn': [{'name': 'Hero', 'verb': 'check', 'amount': 0, 'allin': False}],
            'river': [{'name': 'Hero', 'verb': 'bet', 'amount': 800, 'allin': False}],
        },
        collected=[{'name': 'Hero', 'amount': 3000, 'pot': 0}],
    )
    check('triple barrel needs all three streets (checked turn disqualifies)',
          _detect_triple_barrel(two_streets, {}), [])

    barreled_but_lost = _ir_hand(
        streets=barreled(showdown=True)['streets'],
        collected=[{'name': 'Villain', 'amount': 3000, 'pot': 0}],
    )
    check('triple barrel does not fire on a losing hand',
          _detect_triple_barrel(barreled_but_lost, {}), [])

    # ── Unit: knock-off ──────────────────────────────────────────────────────
    knockout_hand = _ir_hand(stacks={'Hero': 5000, 'Villain': 2000, 'Villain2': 9000})
    m = _detect_knock_off(knockout_hand, {'end_stacks': {'Villain': 0, 'Villain2': 6000, 'Hero': 10000}})
    check('knock-off fires for the villain whose end stack is 0', [x['villain'] for x in m], ['Villain'])

    check('knock-off does not fire on hero even if hero somehow hit 0',
          _detect_knock_off(_ir_hand(hero='Hero', stacks={'Hero': 100}),
                            {'end_stacks': {'Hero': 0}}), [])
    check('knock-off does not fire when nobody hit 0',
          _detect_knock_off(knockout_hand, {'end_stacks': {'Villain': 500, 'Villain2': 6000}}), [])
    check('knock-off ignores a villain who started the hand already at 0 (not busted THIS hand)',
          _detect_knock_off(_ir_hand(stacks={'Hero': 5000, 'Villain': 0}),
                            {'end_stacks': {'Villain': 0}}), [])

    # ── Unit: SVG art ────────────────────────────────────────────────────────
    svg = render_highlight_svg('Lucky river', '7h 7d', ['2h', '3c', '9s', '4d', '7c'], 'Net: +1,000')
    check('svg is well-formed', svg.strip().startswith('<svg') and svg.strip().endswith('</svg>'), True)
    check('svg embeds the label', 'Lucky river' in svg, True)
    check('svg has no unescaped hostile characters from a crafted label',
          '<script>' not in render_highlight_svg('<script>x</script>', '', [], ''), True)
    uri = svg_data_uri(svg)
    check('data uri has the right prefix', uri.startswith('data:image/svg+xml;base64,'), True)

    # ── Integration: raw record -> full pipeline (triple barrel, no showdown) ─
    barrel_raw = make_hand(
        '1-2001-1', 1000, dealer=0, players=PLAYERS[:2], winner_seatid=0,
        pre=[
            {'seatid': 0, 'type': 8, 'chips': 100},     # Hero (SB/BTN) posts
            {'seatid': 1, 'type': 9, 'chips': 200},     # Villain (BB) posts
            {'seatid': 0, 'type': 4, 'chips': 600},     # Hero raises to 600
            {'seatid': 1, 'type': 3, 'chips': 400},     # Villain calls
        ],
        flop=[
            {'seatid': 0, 'type': 7, 'chips': 300},     # Hero bets
            {'seatid': 1, 'type': 3, 'chips': 300},     # Villain calls
        ],
    )
    # test_session_engine.make_hand only builds pre/flop — add turn/river by hand.
    barrel_raw['full_hand']['flow']['turn'] = {
        'actions': [
            {'seatid': 0, 'type': 7, 'chips': 500},
            {'seatid': 1, 'type': 3, 'chips': 500},
        ],
        'cards': [261],   # 5d
    }
    barrel_raw['full_hand']['flow']['river'] = {
        'actions': [
            {'seatid': 0, 'type': 7, 'chips': 800},
            {'seatid': 1, 'type': 1, 'chips': 0},       # Villain folds the river bet
        ],
        'cards': [262],   # 6d
    }

    result = detect_session_highlights([barrel_raw], 0, 2000)
    patterns = [h['pattern'] for h in result]
    check('integration: triple_barrel_no_showdown found', 'triple_barrel_no_showdown' in patterns,
          True, cond='triple_barrel_no_showdown' in patterns)
    check('integration: biggest_win found (only hand in window)', 'biggest_win' in patterns,
          True, cond='biggest_win' in patterns)
    check('integration: no biggest_loss (same hand can\'t be both)', 'biggest_loss' in patterns,
          False, cond='biggest_loss' not in patterns)
    barrel_hl = next(h for h in result if h['pattern'] == 'triple_barrel_no_showdown')
    check('integration: hand_url built from share_key', barrel_hl['hand_url'],
          cond=barrel_hl['hand_url'] != '#' and 'sharekey=x' in barrel_hl['hand_url'])
    check('integration: art_uri is a data URI', barrel_hl['art_uri'],
          cond=barrel_hl['art_uri'].startswith('data:image/svg+xml;base64,'))
    import base64 as _b64
    win_hl = next(h for h in result if h['pattern'] == 'biggest_win')
    win_svg = _b64.b64decode(win_hl['art_uri'].split(',', 1)[1]).decode('utf-8')
    check('integration: biggest_win art footer shows a positive net (villain folded, hero collects)',
          'Net: +' in win_svg, cond='Net: +' in win_svg)

    # ── Integration: mixed multi-hand window doesn't crash and finds a win/loss
    mixed = detect_session_highlights(ALL_RECORDS, 0, 10000)
    mixed_patterns = {h['pattern'] for h in mixed}
    check('integration: mixed window still finds a biggest_win', 'biggest_win' in mixed_patterns, True,
          cond='biggest_win' in mixed_patterns)
    check('integration: mixed window still finds a biggest_loss', 'biggest_loss' in mixed_patterns, True,
          cond='biggest_loss' in mixed_patterns)
    check('integration: empty window returns no highlights',
          detect_session_highlights(ALL_RECORDS, 20000, 30000), [])

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nAll highlights tests passed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
