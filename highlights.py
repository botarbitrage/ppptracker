"""
highlights.py — PokerPulse highlight & pattern detection (Notion task F1-2).

Given a subscriber's raw PPPoker hand records for a session window, produces
a list of "highlight" hands worth surfacing in the PokerPulse report: the
session's biggest win/loss, plus named situational patterns (lucky river,
winning triple barrel, knock-off). Each highlight carries a shareable replay
link (PPPoker's own hosted replay viewer — see hand_parser._replay_url) and a
small self-contained SVG "hand recap" card, so the caller (F1-5's Analyse Now
/ the email template, see render_pokerpulse_email in app.py) can drop it
straight in with no image-rendering pipeline of its own. There is no image
library anywhere in this repo, and adding one (Pillow, a headless-browser
screenshot, etc.) for one MVP feature felt like the wrong trade — a
hand-drawn SVG needs nothing but text.

Pattern detection reuses the exact same battle-tested action model the Leak
Finder / F1-1 session engine already rely on
(hand_exporter.records_to_ps_blocks_with_stacks -> leak_engine.parse_ps_text)
rather than re-deriving raw PPPoker action-code parsing a second time — see
docs/pppoker-action-model.md. Each raw record is converted to its own PS-text
block (not one joined multi-hand blob, unlike session_engine.py's stats path)
specifically so a block, its parsed IR hand, and its stack-chained end stacks
all stay in lock-step, one-to-one — a highlight has to point back at exactly
the hand it came from.

Pure functions, no I/O, no cache, nothing persisted — same contract as
session_engine.compute_session_stats. The caller owns fetching records and
(per the MVP spec) does not keep run history.

New patterns register in PATTERN_DETECTORS (a plain tuple of functions) with
no change to the scan loop in detect_session_highlights — this task's own
Acceptance Criteria ("pattern list implemented so new patterns can be added
later without restructuring").
"""

import base64

from equity import hero_net, _parse_cards
from hand_exporter import records_to_ps_blocks_with_stacks
from hand_parser import _replay_url
from leak_engine import parse_ps_text
from session_engine import _in_window

try:
    import eval7
except ImportError:                                    # pragma: no cover
    eval7 = None

_STREETS = ('preflop', 'flop', 'turn', 'river')
_POSTFLOP_STREETS = ('flop', 'turn', 'river')


def _hand_result(hand, name):
    """Chips `name` collected from this hand's pot(s) — 0 if they won nothing.
    This is "did they take down (part of) the pot", not net profit; use
    equity.hero_net() for the session-level biggest-win/loss ranking, which
    is genuinely about net chip swing."""
    return sum(c['amount'] for c in hand['collected'] if c['name'] == name)


def _hero_bet_or_raised(hand, street, hero):
    return any(a['name'] == hero and a['verb'] in ('bet', 'raise')
               for a in hand['streets'][street])


def _folded_names(hand):
    return {a['name'] for street in _STREETS for a in hand['streets'][street]
            if a['verb'] == 'fold'}


# ── Pattern detectors ────────────────────────────────────────────────────────
# Each detector takes (hand, ctx) — the IR hand (leak_engine.parse_ps_text
# shape) and a per-hand context dict ({'end_stacks': {...}} today, room to
# grow) — and returns a list of zero or more match dicts:
# {pattern, label, ...pattern-specific fields}. A detector that can't apply
# to this hand (missing hero, hand didn't reach the needed street, eval7
# unavailable) just returns [] — detectors never raise.

def _detect_triple_barrel(hand, ctx):
    hero = hand.get('hero')
    if not hero:
        return []
    if not all(_hero_bet_or_raised(hand, s, hero) for s in _POSTFLOP_STREETS):
        return []
    if _hand_result(hand, hero) <= 0:
        return []
    if hand.get('shows'):
        return [{'pattern': 'triple_barrel_showdown',
                 'label': 'Winning triple barrel (showdown)'}]
    return [{'pattern': 'triple_barrel_no_showdown',
             'label': 'Winning triple barrel (villain folded)'}]


def _detect_lucky_river(hand, ctx):
    hero = hand.get('hero')
    if not hero or eval7 is None:
        return []
    board = hand.get('board') or []
    if len(board) != 5:
        return []                                       # must reach the river
    if _hand_result(hand, hero) <= 0:
        return []                                        # must actually win
    hero_cards = _parse_cards(hand.get('hero_cards'))
    if not hero_cards or len(hero_cards) != 2:
        return []

    folded = _folded_names(hand)
    villains = []
    for nm in hand['seats'].values():
        if nm == hero or nm in folded:
            continue
        cards = _parse_cards((hand.get('shows') or {}).get(nm))
        if cards and len(cards) == 2:
            villains.append(cards)
    if not villains:
        return []                                    # nobody's cards known

    turn_board = _parse_cards(' '.join(board[:4]))
    river_board = _parse_cards(' '.join(board[:5]))
    if not turn_board or not river_board:
        return []

    hero_turn = eval7.evaluate(hero_cards + turn_board)
    hero_river = eval7.evaluate(hero_cards + river_board)
    best_villain_turn = max(eval7.evaluate(v + turn_board) for v in villains)
    best_villain_river = max(eval7.evaluate(v + river_board) for v in villains)

    was_behind_on_turn = best_villain_turn > hero_turn
    wins_after_river = hero_river > best_villain_river
    if was_behind_on_turn and wins_after_river:
        return [{'pattern': 'lucky_river', 'label': 'Lucky river'}]
    return []


def _detect_knock_off(hand, ctx):
    hero = hand.get('hero')
    end_stacks = ctx.get('end_stacks') or {}
    starts = hand.get('stacks') or {}
    out = []
    for name, start in starts.items():
        if name == hero or not start:
            continue
        if end_stacks.get(name) == 0:
            out.append({'pattern': 'knock_off',
                        'label': f'Knocked out {name}', 'villain': name})
    return out


PATTERN_DETECTORS = (
    _detect_lucky_river,
    _detect_triple_barrel,
    _detect_knock_off,
)


# ── SVG art ──────────────────────────────────────────────────────────────────
# A small, self-contained "hand recap" card — no external fonts/assets, safe
# to embed inline or as a data: URI (see svg_data_uri below) anywhere,
# including the email template.

_SUIT_GLYPH = {'s': '♠', 'h': '♥', 'd': '♦', 'c': '♣'}
_SUIT_COLOR = {'s': '#1b2b24', 'c': '#1b2b24', 'h': '#e0455f', 'd': '#e0455f'}


def _esc(s):
    return (str(s or '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _card_svg(x, y, token):
    """One playing-card glyph (rank + suit) as an SVG group. token like 'Ks'."""
    if not token or len(token) != 2:
        return ''
    rank, suit = token[0], token[1].lower()
    glyph = _SUIT_GLYPH.get(suit, '?')
    color = _SUIT_COLOR.get(suit, '#1b2b24')
    return (
        f'<g transform="translate({x},{y})">'
        f'<rect width="34" height="46" rx="5" fill="#f4f1ea" stroke="#0f2a1f" stroke-width="1.5"/>'
        f'<text x="6" y="18" font-family="Georgia,serif" font-size="15" font-weight="bold" '
        f'fill="{color}">{_esc(rank)}</text>'
        f'<text x="17" y="36" font-family="Georgia,serif" font-size="16" fill="{color}" '
        f'text-anchor="middle">{glyph}</text>'
        f'</g>'
    )


def render_highlight_svg(label, hero_cards, board, footer=''):
    """
    label: pattern title, e.g. "Lucky river".
    hero_cards: "Ks 4s" (hand['hero_cards']) or ''/None.
    board: ['Ks','4s','2h',...] (hand['board']) or [].
    footer: one short line, e.g. "Net: +12,400 chips" or "Knocked out villain1".
    Returns raw <svg>...</svg> markup — wrap with svg_data_uri() to embed.
    """
    hero_tokens = (hero_cards or '').split()
    board_tokens = list(board or [])

    hero_svg = ''.join(_card_svg(16 + i * 40, 40, t) for i, t in enumerate(hero_tokens))
    board_svg = ''.join(_card_svg(16 + i * 40, 96, t) for i, t in enumerate(board_tokens))

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="170" viewBox="0 0 320 170">'
        '<rect width="320" height="170" rx="10" fill="#101b17"/>'
        '<rect x="1" y="1" width="318" height="168" rx="9" fill="none" stroke="#1d3a2c"/>'
        f'<text x="16" y="24" font-family="Helvetica,Arial,sans-serif" font-size="14" '
        f'font-weight="bold" fill="#00e676">{_esc(label)}</text>'
        f'{hero_svg}{board_svg}'
        f'<text x="16" y="156" font-family="Helvetica,Arial,sans-serif" font-size="11" '
        f'fill="#9fb8ac">{_esc(footer)}</text>'
        '</svg>'
    )


def svg_data_uri(svg):
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode('utf-8')).decode('ascii')


# ── Main entry point ─────────────────────────────────────────────────────────

def _highlight_from(pattern, label, hand, record, footer):
    return {
        'pattern':   pattern,
        'label':     label,
        'hand_id':   hand.get('hand_id') or '',
        'hand_url':  _replay_url(record.get('share_key', '')),
        'art_uri':   svg_data_uri(render_highlight_svg(
            label, hand.get('hero_cards'), hand.get('board'), footer)),
    }


def detect_session_highlights(records, start_ts, end_ts):
    """
    Given the full candidate record set (any number of tournaments, any
    order — same contract as session_engine.compute_session_stats) and a
    half-open [start_ts, end_ts) window, returns a list of highlight dicts:
    {pattern, label, hand_id, hand_url, art_uri, net_result (win/loss only)}.
    Biggest win/loss (if any in-window hand converts cleanly) come first,
    followed by pattern matches in hand order. Nothing is written or cached.
    """
    windowed = [r for r in records if _in_window(r, start_ts, end_ts)]
    if not windowed:
        return []

    sorted_records, blocks, end_stacks_list, _stats = records_to_ps_blocks_with_stacks(windowed)

    pattern_highlights = []
    best_win = None    # (net, hand, record)
    best_loss = None   # (net, hand, record)

    for record, block, end_stacks in zip(sorted_records, blocks, end_stacks_list):
        if block.startswith('# SKIPPED'):
            continue
        parsed = parse_ps_text(block + '\n')
        if not parsed:
            continue
        hand = parsed[0]
        hero = hand.get('hero')
        if not hero:
            continue

        net = hero_net(hand)
        if best_win is None or net > best_win[0]:
            best_win = (net, hand, record)
        if best_loss is None or net < best_loss[0]:
            best_loss = (net, hand, record)

        ctx = {'end_stacks': end_stacks}
        for detector in PATTERN_DETECTORS:
            for match in detector(hand, ctx):
                pattern_highlights.append(
                    _highlight_from(match['pattern'], match['label'], hand, record, footer=''))

    result = []
    if best_win is not None:
        net, hand, record = best_win
        result.append(_highlight_from('biggest_win', 'Biggest win', hand, record,
                                      footer=f'Net: {net:+,} chips'))
    if best_loss is not None and best_loss[1] is not (best_win[1] if best_win else None):
        net, hand, record = best_loss
        result.append(_highlight_from('biggest_loss', 'Biggest loss', hand, record,
                                      footer=f'Net: {net:+,} chips'))

    result.extend(pattern_highlights)
    return result
