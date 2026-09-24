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
                        'label': f'Knocked out {name}', 'villain': name,
                        'villain_start': start})
    return out


PATTERN_DETECTORS = (
    _detect_lucky_river,
    _detect_triple_barrel,
    _detect_knock_off,
)


# ── Opponent extraction ──────────────────────────────────────────────────────

def _main_opponent(hand, villain=None):
    """The opponent a highlight is "against": {name, stack, cards}.
    `villain` (a knock-off's knocked-out player) wins outright; otherwise the
    opponent who went to showdown, else the last opponent to fold. `cards` is
    '' when they were never shown (mucked / folded) — the art draws card backs.
    None when there is no identifiable opponent."""
    hero = hand.get('hero')
    shows = hand.get('shows') or {}
    name = villain
    if not name:
        name = next((n for n in shows if n != hero), None)
    if not name:
        for street in reversed(_STREETS):
            folders = [a['name'] for a in hand['streets'][street]
                       if a['verb'] == 'fold' and a['name'] != hero]
            if folders:
                name = folders[-1]
                break
    if not name:
        return None
    return {'name': name,
            'stack': (hand.get('stacks') or {}).get(name),
            'cards': shows.get(name) or ''}


# ── Knock-off cap ────────────────────────────────────────────────────────────

MAX_KNOCK_OFFS = 3


def select_knock_offs(candidates, cap=MAX_KNOCK_OFFS):
    """candidates: dicts with 'bb_size' (villain's starting stack / big blind),
    'chips' (villain's starting stack) and 'order' (position in the window,
    later = larger), in any order. Returns at most `cap` of them, in window
    order: the biggest in BBs, then the biggest in chips, then the latest. If
    one candidate wins more than one of those picks, the freed slot goes to
    the next-latest not yet chosen."""
    if len(candidates) <= cap:
        return sorted(candidates, key=lambda c: c['order'])
    chosen = []
    for key in (lambda c: (c['bb_size'], c['order']), lambda c: (c['chips'], c['order'])):
        top = max(candidates, key=key)
        if top not in chosen:      # already picked -> slot is freed for "latest"
            chosen.append(top)
    for c in sorted(candidates, key=lambda c: c['order'], reverse=True):
        if len(chosen) >= cap:
            break
        if c not in chosen:
            chosen.append(c)
    return sorted(chosen, key=lambda c: c['order'])


# ── SVG art ──────────────────────────────────────────────────────────────────
# A self-contained "mini table" card sized for the 600px email column — no
# external fonts/assets. Colours are the site's dark theme tokens
# (static/style.css) and the 4-colour deck, so the email matches the website.

_BG, _PANEL, _PANEL_ALT, _BORDER = '#0d1117', '#111a11', '#162016', '#1e2d1e'
_TEXT, _MUTED, _GREEN, _RED = '#d0ddd0', '#6b8c6b', '#00e676', '#ff5252'
_SUIT_GLYPH = {'s': '♠', 'h': '♥', 'd': '♦', 'c': '♣'}
_SUIT_COLOR = {'h': '#ff5252', 'd': '#40c4ff', 'c': '#00e676', 's': '#d0ddd0'}
_FONT = 'Helvetica,Arial,sans-serif'
_W, _H = 600, 330


def _esc(s):
    return (str(s or '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _card_svg(x, y, token):
    """One face-up playing card. token like 'Ks'; falsy/malformed -> card back."""
    if not token or len(token) != 2:
        return _card_back_svg(x, y)
    rank, suit = token[0], token[1].lower()
    glyph = _SUIT_GLYPH.get(suit, '?')
    color = _SUIT_COLOR.get(suit, _TEXT)
    return (
        f'<g transform="translate({x},{y})">'
        f'<rect width="38" height="52" rx="5" fill="{_PANEL_ALT}" stroke="{color}" stroke-width="1.5"/>'
        f'<text x="7" y="20" font-family="{_FONT}" font-size="17" font-weight="bold" '
        f'fill="{color}">{_esc(rank)}</text>'
        f'<text x="19" y="42" font-family="{_FONT}" font-size="18" fill="{color}" '
        f'text-anchor="middle">{glyph}</text>'
        f'</g>'
    )


def _card_back_svg(x, y):
    return (
        f'<g transform="translate({x},{y})">'
        f'<rect width="38" height="52" rx="5" fill="{_PANEL}" stroke="{_MUTED}" stroke-width="1.5"/>'
        f'<rect x="5" y="5" width="28" height="42" rx="3" fill="none" stroke="{_BORDER}" stroke-width="1.5"/>'
        f'</g>'
    )


def _cards_row(cx, y, tokens, count=None):
    """Cards centred on cx. `count` pads with card backs when tokens is short."""
    tokens = list(tokens)
    if count is not None and len(tokens) < count:
        tokens += [''] * (count - len(tokens))
    step = 44
    x0 = cx - (len(tokens) * step - 6) / 2
    return ''.join(_card_svg(round(x0 + i * step), y, t) for i, t in enumerate(tokens))


def _player_label(x, y, name, stack, anchor='middle'):
    stack_txt = f'{stack:,} chips' if isinstance(stack, (int, float)) else ''
    return (
        f'<text x="{x}" y="{y}" font-family="{_FONT}" font-size="13" font-weight="bold" '
        f'fill="{_TEXT}" text-anchor="{anchor}">{_esc(name)}</text>'
        f'<text x="{x}" y="{y + 16}" font-family="{_FONT}" font-size="12" fill="{_MUTED}" '
        f'text-anchor="{anchor}">{_esc(stack_txt)}</text>'
    )


def render_highlight_svg(label, hero_cards, board, footer='', *, hero_name='Hero',
                         hero_stack=None, opponent=None, pot=None, net=None):
    """
    label: pattern title(s), e.g. "Lucky river" or "Biggest win · Knocked out X".
    hero_cards: "Ks 4s" (hand['hero_cards']) or ''/None.
    board: ['Ks','4s','2h',...] (hand['board']) or [].
    footer: optional short caption, shown right-aligned in the top band.
    opponent: {name, stack, cards} from _main_opponent(), or None.
    net: hero's net chips; drawn as a green/red chip badge when given.
    Returns raw <svg>...</svg> markup — wrap with svg_data_uri() to embed.
    """
    hero_tokens = (hero_cards or '').split()
    board_tokens = list(board or [])
    cx = _W // 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" viewBox="0 0 {_W} {_H}">',
        f'<rect width="{_W}" height="{_H}" rx="10" fill="{_BG}"/>',
        f'<rect x="1" y="1" width="{_W - 2}" height="{_H - 2}" rx="9" fill="none" stroke="{_BORDER}"/>',
        # top band: label / comment
        f'<path d="M1 34V11a10 10 0 0 1 10-10h{_W - 22}a10 10 0 0 1 10 10v23z" fill="{_PANEL_ALT}"/>',
        f'<text x="16" y="23" font-family="{_FONT}" font-size="14" font-weight="bold" '
        f'fill="{_GREEN}">{_esc(label)}</text>',
    ]
    if footer:
        parts.append(f'<text x="{_W - 16}" y="23" font-family="{_FONT}" font-size="12" '
                     f'fill="{_MUTED}" text-anchor="end">{_esc(footer)}</text>')
    # felt
    parts.append(f'<rect x="16" y="44" width="{_W - 32}" height="{_H - 60}" rx="60" '
                 f'fill="{_PANEL}" stroke="{_BORDER}"/>')
    # opponent (top): cards, then name + stack beside them
    if opponent:
        parts.append(_cards_row(cx, 56, (opponent.get('cards') or '').split(), count=2))
        parts.append(_player_label(cx + 66, 76, opponent.get('name'), opponent.get('stack'),
                                   anchor='start'))
    # board + pot (middle)
    parts.append(_cards_row(cx, 132, board_tokens))
    if pot:
        parts.append(f'<text x="{cx}" y="204" font-family="{_FONT}" font-size="13" '
                     f'fill="{_TEXT}" text-anchor="middle">Pot {pot:,}</text>')
    # hero (bottom): cards, name + stack to their left, net chip badge to the right
    parts.append(_cards_row(cx, 236, hero_tokens, count=2))
    parts.append(_player_label(cx - 66, 256, hero_name, hero_stack, anchor='end'))
    if net is not None:
        colour = _GREEN if net >= 0 else _RED
        txt = f'{net:+,}'
        w = 26 + 9 * len(txt)
        parts.append(
            f'<rect x="{cx + 66}" y="248" width="{w}" height="30" rx="15" fill="{_BG}" '
            f'stroke="{colour}" stroke-width="2"/>'
            f'<text x="{cx + 66 + w / 2}" y="268" font-family="{_FONT}" font-size="14" '
            f'font-weight="bold" fill="{colour}" text-anchor="middle">{_esc(txt)}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def svg_data_uri(svg):
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode('utf-8')).decode('ascii')


# ── Main entry point ─────────────────────────────────────────────────────────

def _hand_pot(hand):
    return hand.get('total_pot') or sum(c['amount'] for c in hand['collected']) or None


def _build_highlight(matches, hand, record, net):
    """One highlight dict for a hand carrying one or more pattern matches — a
    hand is only ever shown once, with every label."""
    label = ' · '.join(dict.fromkeys(m['label'] for m in matches))
    villain = next((m.get('villain') for m in matches if m.get('villain')), None)
    hero = hand.get('hero')
    opponent = _main_opponent(hand, villain)
    board = list(hand.get('board') or [])
    art = render_highlight_svg(label, hand.get('hero_cards'), board, hero_name=hero,
                               hero_stack=(hand.get('stacks') or {}).get(hero),
                               opponent=opponent, pot=_hand_pot(hand), net=net)
    return {
        'pattern':    matches[0]['pattern'],
        'patterns':   [m['pattern'] for m in matches],
        'label':      label,
        'hand_id':    hand.get('hand_id') or '',
        'hand_url':   _replay_url(record.get('share_key', '')),
        'art_uri':    svg_data_uri(art),
        'net_result': net,
        'hero_cards': hand.get('hero_cards') or '',
        'board':      board,
        'opponent':   opponent,
    }


def detect_session_highlights(records, start_ts, end_ts):
    """
    Given the full candidate record set (any number of tournaments, any
    order — same contract as session_engine.compute_session_stats) and a
    half-open [start_ts, end_ts) window, returns a list of highlight dicts:
    {pattern, patterns, label, hand_id, hand_url, art_uri, net_result,
    hero_cards, board, opponent}. Biggest win/loss (if any in-window hand
    converts cleanly) come first, followed by pattern matches in hand order.
    At most MAX_KNOCK_OFFS knock-offs are kept (see select_knock_offs), and a
    hand appears once however many labels it earns. Nothing is written or
    cached.
    """
    windowed = [r for r in records if _in_window(r, start_ts, end_ts)]
    if not windowed:
        return []

    sorted_records, blocks, end_stacks_list, _stats = records_to_ps_blocks_with_stacks(windowed)

    pattern_matches = []   # (order, match, hand, record, net)
    knock_offs = []        # candidate dicts for select_knock_offs
    best_win = None    # (net, hand, record)
    best_loss = None   # (net, hand, record)

    for order, (record, block, end_stacks) in enumerate(
            zip(sorted_records, blocks, end_stacks_list)):
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
                if match['pattern'] == 'knock_off':
                    bb = hand.get('bb_amt') or 0
                    start = match.get('villain_start') or 0
                    knock_offs.append({'order': order, 'match': match, 'hand': hand,
                                       'record': record, 'net': net, 'chips': start,
                                       'bb_size': (start / bb) if bb else 0})
                else:
                    pattern_matches.append((order, match, hand, record, net))

    for c in select_knock_offs(knock_offs):
        pattern_matches.append((c['order'], c['match'], c['hand'], c['record'], c['net']))
    pattern_matches.sort(key=lambda t: t[0])

    # Group every label onto its hand (a hand shows once), first-appearance order.
    groups = {}   # id(hand) -> [matches, hand, record, net]

    def _add(match, hand, record, net):
        groups.setdefault(id(hand), [[], hand, record, net])[0].append(match)

    if best_win is not None:
        net, hand, record = best_win
        _add({'pattern': 'biggest_win', 'label': 'Biggest win'}, hand, record, net)
    if best_loss is not None and best_loss[1] is not (best_win[1] if best_win else None):
        net, hand, record = best_loss
        _add({'pattern': 'biggest_loss', 'label': 'Biggest loss'}, hand, record, net)
    for _order, match, hand, record, net in pattern_matches:
        _add(match, hand, record, net)

    return [_build_highlight(m, h, r, n) for m, h, r, n in groups.values()]
