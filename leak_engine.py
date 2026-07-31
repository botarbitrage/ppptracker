"""
leak_engine.py — Leak Finder core.

Pure functions, no I/O (mirrors tournament_analyzer.py's design): parse hands
into a normalized intermediate representation (IR), map seats to PokerTracker's
six position buckets, and aggregate per-hand stat flags by position.

Input is the PokerStars-dialect text our own hand_exporter.py emits — the same
text PT4 ingested, so the validation harness compares like-for-like, and the
live report reaches it via hand_exporter.records_to_ps_text() without touching
the filesystem.

Stats are (made, opportunity) pairs per hand: 17 preflop and 22 postflop,
matching the report columns decoded from the BBZ .pt4rpt (design doc
Appendix A) and validated cell-exact against PT4's own CSV exports.

The IR is a plain dict per hand:
  hand_id, tourney_id, table_size, btn_seat,
  sb_amt, bb_amt, ante,
  seats        {seat_no: name}          (1-based seat numbers, as in the text)
  stacks       {name: starting chips}
  hero         name or None             (from "Dealt to ...")
  hero_cards   'Ks 4s' or ''
  sb_seat / bb_seat                     seat numbers or None (dead-SB hands)
  streets      {street: [action, ...]}  street in preflop/flop/turn/river
      action = {name, verb, amount, allin}
      verb in fold/check/call/bet/raise; amount = call amount, bet amount or
      raise-TO total; allin bool
  board        [card, ...]
  shows        {name: cards}
  collected    [{name, amount, pot}]
"""

import re

# ── PT4 position scheme ─────────────────────────────────────────────────────
# PokerTracker's numeric position: 0 = BTN, counting away from the button
# through the non-blind seats (CO = 1, HJ = 2 …), with the blinds fixed at
# BB = 8 and SB = 9 (the 8/9 encoding is visible in the BBZ .pt4rpt formulas).
#
# Bucketing is TABLE-SIZE DEPENDENT: the two earliest-to-act non-blind seats
# are EP, everything between them and the CO is MP. With N active players the
# non-blind positions run 0..N-3, so EP = positions ≥ N-4:
#     9-handed → EP {5,6} · 8-handed → EP {4,5} · 7-handed → EP {3,4}
# Validated cell-exact against PT4 report CSVs across 349 hands spanning 5-9
# player tables (four DeepFreeze tournaments, 2026-07-31).
POSITION_BUCKETS = ('BTN', 'CO', 'MP', 'EP', 'BB', 'SB')

# Two schemes:
#   'pt4'    — reproduces PokerTracker exactly (the validation gate depends on
#              this and must never drift). PT4 gives EP the two earliest
#              non-blind seats at 7-9 handed and has NO EP bucket at all for
#              tables of 6 or fewer, dumping those seats into MP.
#   'report' — what the Leak Finder shows. Identical to 'pt4' at 7-9 handed;
#              it only fills PT4's short-table gap so EP always means "the
#              earliest seat(s) at your table". Without this, 6-max and 5-max
#              UTG are labelled MP, which (on a field that is ~60% short-
#              handed) buries early-position hands inside MP and starves EP.
#
# Caveat worth knowing when reading the report: numeric position equals
# "players still to act behind you, minus the two blinds" regardless of table
# size, so a relative scheme necessarily mixes slightly different strategic
# depths inside one row. Filtering by table size (Phase 5) is the principled
# way to remove that mixing; the schemes here only decide the labelling.
SCHEMES = ('pt4', 'report')


def position_bucket(numeric_pos, n_players=9, scheme='pt4'):
    """PT4 numeric position + table size → SB|BB|EP|MP|CO|BTN (None if unknown)."""
    if numeric_pos is None:
        return None
    if numeric_pos == 9:
        return 'SB'
    if numeric_pos == 8:
        return 'BB'
    if numeric_pos == 0:
        return 'BTN'
    if numeric_pos == 1:
        return 'CO'
    n = n_players or 9
    if scheme == 'pt4':
        if n <= 6:
            return 'MP'
        return 'EP' if numeric_pos >= max(2, n - 4) else 'MP'
    # 'report': EP takes the earliest ceil(pool/2) seats, capped at 2 — which
    # is exactly PT4's mapping for 7/8/9-handed and extends it below that.
    highest = n - 3                      # highest non-blind numeric seat
    pool = max(0, highest - 1)           # seats 2..highest are the EP/MP pool
    if pool <= 0:
        return 'MP'
    ep_seats = min(2, (pool + 1) // 2)
    return 'EP' if numeric_pos >= highest - ep_seats + 1 else 'MP'


def pt4_positions(hand):
    """
    {name: numeric_position} for every seated player in an IR hand.

    SB → 9, BB → 8 (by blind actually posted, so a dead-SB hand simply has no
    9). Remaining seats are ordered clockwise from the button and numbered
    from the button backwards: BTN 0, CO 1, … — exactly PT4's scheme. In
    heads-up the button posts SB and is therefore 9, matching PT4.
    """
    seats = sorted(hand['seats'])
    if not seats:
        return {}

    sb_seat, bb_seat = hand.get('sb_seat'), hand.get('bb_seat')
    btn = hand.get('btn_seat')

    pos = {}
    if sb_seat is not None:
        pos[hand['seats'][sb_seat]] = 9
    if bb_seat is not None:
        pos[hand['seats'][bb_seat]] = 8

    non_blind = [s for s in seats if s not in (sb_seat, bb_seat)]
    if not non_blind:
        return pos

    if btn in seats:
        anchor = btn
    else:
        # Dead button (unoccupied seat): anchor on the nearest occupied seat
        # counter-clockwise so the ordering stays button-relative.
        anchor = max((s for s in seats if s < (btn or 0)), default=seats[-1])

    # Clockwise from the seat after the anchor; the anchor lands last.
    order = sorted(seats, key=lambda s: ((s - anchor - 1) % 100, s))
    ordered_non_blind = [s for s in order if s in set(non_blind)]
    # Last in clockwise order is the button(-most) seat → numeric 0.
    n = len(ordered_non_blind)
    for i, s in enumerate(ordered_non_blind):
        pos[hand['seats'][s]] = n - 1 - i
    return pos


def hero_position(hand, scheme='pt4'):
    """The hero's position bucket for one IR hand, or None."""
    hero = hand.get('hero')
    if not hero:
        return None
    return position_bucket(pt4_positions(hand).get(hero),
                           len(hand['seats']), scheme)


# ── PokerStars-dialect parser ───────────────────────────────────────────────
# Parses the exact text hand_exporter.py writes (which is what PT4 imported),
# so the validation harness compares like-for-like.

_RE_HEADER = re.compile(
    r"^PokerStars Hand #(?P<id>\d+): (?:Tournament #(?P<tid>\d+), )?"
    r"No Limit Hold'em - Level [IVXLCDM]+ "
    r"\((?P<sb>\d+)/(?P<bb>\d+)(?: ante (?P<ante>\d+))?\)")
_RE_TABLE = re.compile(
    r"^Table '(?P<tname>[^']*)' (?P<size>\d+)-max Seat #(?P<btn>\d+|\?) is the button")
_RE_SEAT = re.compile(r"^Seat (?P<no>\d+): (?P<name>.*) \((?P<chips>\d+) in chips\)$")
_RE_DEALT = re.compile(r"^Dealt to (?P<name>.*) \[(?P<cards>[^\]]*)\]$")
_RE_UNCALLED = re.compile(r"^Uncalled bet \((?P<amt>\d+)\) returned to (?P<name>.*)$")
_RE_COLLECT = re.compile(r"^(?P<name>.*) collected (?P<amt>\d+) from (?P<pot>.+)$")

_STREET_MARKS = {
    '*** FLOP ***': 'flop',
    '*** TURN ***': 'turn',
    '*** RIVER ***': 'river',
}


def _match_actor(line, names_by_len):
    """Split 'Name: rest' using the hand's known player names (longest first),
    so names containing spaces or punctuation can't be mis-split."""
    for name in names_by_len:
        prefix = name + ': '
        if line.startswith(prefix):
            return name, line[len(prefix):]
    return None, None


def _parse_action(rest):
    """'raises 2640 to 3960 and is all-in' → action dict fields, or None."""
    allin = rest.endswith(' and is all-in')
    if allin:
        rest = rest[:-len(' and is all-in')]
    if rest == 'folds':
        return 'fold', 0, allin
    if rest == 'checks':
        return 'check', 0, allin
    m = re.match(r'^calls (\d+)$', rest)
    if m:
        return 'call', int(m.group(1)), allin
    m = re.match(r'^bets (\d+)$', rest)
    if m:
        return 'bet', int(m.group(1)), allin
    m = re.match(r'^raises \d+ to (\d+)$', rest)
    if m:
        return 'raise', int(m.group(1)), allin
    return None


def _new_hand():
    return {
        'hand_id': '', 'tourney_id': '', 'table_size': 0, 'btn_seat': None,
        'sb_amt': 0, 'bb_amt': 0, 'ante': 0,
        'seats': {}, 'stacks': {},
        'hero': None, 'hero_cards': '',
        'sb_seat': None, 'bb_seat': None,
        'posts': [],       # [{name, kind: ante|sb|bb, amount}]
        'streets': {'preflop': [], 'flop': [], 'turn': [], 'river': []},
        'board': [], 'shows': {}, 'collected': [],
        'uncalled': [],    # [{name, amount}]
        'total_pot': None,  # stated "Total pot N" from the summary
    }


def parse_ps_text(text):
    """
    Parse a PokerStars-dialect export (as written by hand_exporter.py) into a
    list of IR hands. Unknown/comment lines ('# …') are ignored. Summary
    sections are skipped (everything they say is derived from the actions).
    """
    hands = []
    hand = None
    street = 'preflop'
    in_summary = False
    names_by_len = []
    name_to_seat = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('# '):
            continue

        m = _RE_HEADER.match(line)
        if m:
            if hand is not None:
                hands.append(hand)
            hand = _new_hand()
            street = 'preflop'
            in_summary = False
            names_by_len = []
            name_to_seat = {}
            hand['hand_id'] = m.group('id')
            hand['tourney_id'] = m.group('tid') or ''
            hand['sb_amt'] = int(m.group('sb'))
            hand['bb_amt'] = int(m.group('bb'))
            hand['ante'] = int(m.group('ante') or 0)
            continue

        if hand is None:
            continue

        m = _RE_TABLE.match(line)
        if m:
            hand['table_size'] = int(m.group('size'))
            hand['btn_seat'] = None if m.group('btn') == '?' else int(m.group('btn'))
            continue

        if not in_summary:
            m = _RE_SEAT.match(line)
            if m:
                seat, name = int(m.group('no')), m.group('name').strip()
                hand['seats'][seat] = name
                hand['stacks'][name] = int(m.group('chips'))
                name_to_seat[name] = seat
                names_by_len = sorted(hand['seats'].values(), key=len, reverse=True)
                continue

        for mark, st in _STREET_MARKS.items():
            if line.startswith(mark):
                street = st
                cards = re.findall(r'\[([^\]]+)\]', line)
                if cards:
                    hand['board'].extend(cards[-1].split())
                break
        else:
            if line.startswith('*** HOLE CARDS ***'):
                continue
            if line.startswith('*** SHOW DOWN ***'):
                continue
            if line.startswith('*** SUMMARY ***'):
                in_summary = True
                continue
            if in_summary:
                m = re.match(r'^Total pot (\d+)', line)
                if m:
                    hand['total_pot'] = int(m.group(1))
                continue

            m = _RE_DEALT.match(line)
            if m:
                hand['hero'] = m.group('name').strip()
                hand['hero_cards'] = m.group('cards')
                continue
            m = _RE_UNCALLED.match(line)
            if m:
                hand['uncalled'].append({'name': m.group('name').strip(),
                                         'amount': int(m.group('amt'))})
                continue
            m = _RE_COLLECT.match(line)
            if m and ': ' not in m.group('name'):
                hand['collected'].append({
                    'name': m.group('name').strip(),
                    'amount': int(m.group('amt')),
                    'pot': m.group('pot'),
                })
                continue

            actor, rest = _match_actor(line, names_by_len)
            if actor is None:
                continue

            m = re.match(r'^posts the ante (\d+)$', rest)
            if m:
                hand['posts'].append({'name': actor, 'kind': 'ante',
                                      'amount': int(m.group(1))})
                continue
            m = re.match(r'^posts small blind (\d+)$', rest)
            if m:
                hand['sb_seat'] = name_to_seat.get(actor)
                hand['posts'].append({'name': actor, 'kind': 'sb',
                                      'amount': int(m.group(1))})
                continue
            m = re.match(r'^posts big blind (\d+)$', rest)
            if m:
                hand['bb_seat'] = name_to_seat.get(actor)
                hand['posts'].append({'name': actor, 'kind': 'bb',
                                      'amount': int(m.group(1))})
                continue
            m = re.match(r'^shows \[([^\]]*)\]$', rest)
            if m:
                hand['shows'][actor] = m.group(1)
                continue

            parsed = _parse_action(rest)
            if parsed:
                verb, amount, allin = parsed
                hand['streets'][street].append({
                    'name': actor, 'verb': verb, 'amount': amount, 'allin': allin,
                })
            continue

    if hand is not None:
        hands.append(hand)
    return hands


# ── PT4-style pot validation ────────────────────────────────────────────────

def validate_pot(hand):
    """
    Re-derive the pot from an IR hand's posts/actions and check it against the
    stated total pot, the collected amounts, and each player's stack — the
    same arithmetic PT4 validates on import (it silently drops hands that
    fail). Returns a list of problem strings (empty = importable).
    """
    problems = []
    pot = sum(p['amount'] for p in hand['posts'] if p['kind'] == 'ante')
    total_commit = {p['name']: p['amount'] for p in hand['posts']
                    if p['kind'] == 'ante'}

    for street in ('preflop', 'flop', 'turn', 'river'):
        commit = {}
        if street == 'preflop':
            for p in hand['posts']:
                if p['kind'] in ('sb', 'bb'):
                    commit[p['name']] = commit.get(p['name'], 0) + p['amount']
                    pot += p['amount']
                    total_commit[p['name']] = total_commit.get(p['name'], 0) + p['amount']
        for a in hand['streets'][street]:
            n, v = a['name'], a['amount']
            if a['verb'] in ('call', 'bet'):
                commit[n] = commit.get(n, 0) + v
                pot += v
                total_commit[n] = total_commit.get(n, 0) + v
            elif a['verb'] == 'raise':
                delta = v - commit.get(n, 0)
                commit[n] = v
                pot += delta
                total_commit[n] = total_commit.get(n, 0) + delta

    net = pot - sum(u['amount'] for u in hand['uncalled'])
    stated = hand.get('total_pot')
    if stated is not None and net != stated:
        problems.append(f'pot from actions {net} != stated total pot {stated}')
    if hand['collected'] and stated is not None and \
            sum(c['amount'] for c in hand['collected']) != stated:
        problems.append(f"collected {sum(c['amount'] for c in hand['collected'])}"
                        f' != stated total pot {stated}')
    for n, c in total_commit.items():
        stack = hand['stacks'].get(n, 0)
        if c > stack:
            problems.append(f'{n} committed {c} > stack {stack}')
    return problems


# ── Phase 1: preflop stats ──────────────────────────────────────────────────
# Each stat is a (made, opportunity) pair per hand, hero-perspective, matching
# the PT4/BBZ definitions decoded from the .pt4rpt (design doc Appendix A).
#
# 'global' stats are the ones whose BBZ report formula lost the position
# grouping (the CSV repeats one population-wide value on every position row).
# We still compute true per-position splits for them; the validation gate
# compares them population-wide, matching what PT4 actually emitted.

PREFLOP_STATS = [
    # key                        CSV/BBZ column label                global?
    ('raise_first',              'Raise First',                      False),
    ('limp_open',                'Limp Open',                        True),
    ('limp_raise',               'Limp/Raise',                       False),
    ('limp_call',                'Limp/Call',                        False),
    ('limp_fold',                'Limp/Fold',                        False),
    ('raise_sb_open_limp',       'Raise SB Open Limp',               True),
    ('fold_bb_v_sb',             'Fold BB v SB',                     False),
    ('fold_to_steal',            'Fold to Steal',                    False),
    ('call_pf_2bet',             'Call PF 2Bet',                     False),
    ('threebet_pf',              '3Bet PF',                          False),
    ('threebet_steal',           '3Bet Steal',                       False),
    ('threebet_nai_u35',         '3Bet NAI <35',                     True),
    ('twobet_pf_fold',           '2Bet PF & Fold',                   False),
    ('raise_4bet_pf',            'Raise & 4Bet+ PF',                 False),
    ('threebet_pf_fold',         '3Bet PF & Fold',                   False),
    ('fold_4bet_after_3bet_u30', 'Fold to PF 4Bet After 3Bet <30',   True),
    ('pf_squeeze',               'PF Squeeze',                       False),
]

_VERB_CHAR = {'raise': 'R', 'bet': 'R', 'call': 'C', 'fold': 'F', 'check': 'X'}


def _hero_preflop_walk(hand):
    """
    Replay the preflop action and capture the hero's decision contexts.

    Returns (contexts, state) where each context describes the situation at
    one hero action: verb, raises seen so far, limpers/callers before the
    action, whether a raise was legally available, etc. State carries
    hand-level facts (first raiser, limpers before the first raise, …).
    """
    hero = hand['hero']
    hero_stack = hand['stacks'].get(hero, 0)
    hero_ante = sum(p['amount'] for p in hand['posts']
                    if p['kind'] == 'ante' and p['name'] == hero)

    committed = {}                       # per-player chips committed preflop
    for p in hand['posts']:
        if p['kind'] in ('sb', 'bb'):
            committed[p['name']] = committed.get(p['name'], 0) + p['amount']
    current_bet = hand['bb_amt']

    n_raises = 0
    limpers = 0                          # callers before any raise
    limpers_at_first_raise = 0
    first_limper = None
    callers_since_raise = 0
    first_raiser = None
    aggressor = None
    aggressor_allin = False
    folded = set()
    hero_str = ''
    contexts = []

    for a in hand['streets']['preflop']:
        name, verb, amt = a['name'], a['verb'], a['amount']
        if name == hero:
            call_amt = max(0, current_bet - committed.get(hero, 0))
            remaining = hero_stack - hero_ante - committed.get(hero, 0)
            others_live = any(n != hero and n != aggressor and n not in folded
                              for n in hand['seats'].values())
            contexts.append({
                'verb': verb,
                'allin': a.get('allin', False),
                'n_raises': n_raises,
                'limpers': limpers,
                'callers_since_raise': callers_since_raise,
                'aggressor': aggressor,
                'aggressor_allin': aggressor_allin,
                'others_live': others_live,
                'first_action': hero_str == '',
                'call_amt': call_amt,
                'can_raise': call_amt < remaining,
            })
            hero_str += _VERB_CHAR.get(verb, '?')
        if verb in ('raise', 'bet'):
            if n_raises == 0:
                first_raiser = name
                limpers_at_first_raise = limpers
            n_raises += 1
            aggressor = name
            aggressor_allin = a.get('allin', False)
            callers_since_raise = 0
            committed[name] = amt        # raise amounts are raise-TO totals
            current_bet = max(current_bet, amt)
        elif verb == 'call':
            if n_raises == 0:
                limpers += 1
                if first_limper is None:
                    first_limper = name
            else:
                callers_since_raise += 1
            committed[name] = committed.get(name, 0) + amt
        elif verb == 'fold':
            folded.add(name)

    state = {
        'hero_str': hero_str,
        'first_raiser': first_raiser,
        'last_raiser': aggressor,          # the preflop aggressor
        'n_raises_total': n_raises,
        'limpers_at_first_raise': limpers_at_first_raise,
        'first_limper': first_limper,
    }
    return contexts, state


def preflop_stat_flags(hand, with_context=False):
    """
    {key: (made, opp)} for every PREFLOP_STATS entry, for one IR hand's hero.
    made/opp are 0/1 — the per-hand contribution to the aggregate counters.
    With with_context=True also returns the preflop facts the postflop stats
    depend on (aggressor, 3bet-pot flags, hero's preflop line).
    """
    flags = {key: (0, 0) for key, _label, _g in PREFLOP_STATS}
    empty_ctx = {'pfa': None, 'hero_line': '', 'n_raises': 0,
                 'hero_faced_raise': False, 'hero_called_raise': False,
                 'faced_3bet': False, 'faced_4bet': False, 'in_3bet_pot': False}
    hero = hand.get('hero')
    if not hero or hero not in hand['stacks']:
        return (flags, empty_ctx) if with_context else flags

    positions = pt4_positions(hand)
    hero_np = positions.get(hero)
    bb_amt = hand['bb_amt'] or 0
    stack_bb = (hand['stacks'].get(hero, 0) / bb_amt) if bb_amt else 0.0

    ctxs, st = _hero_preflop_walk(hand)
    hero_str = st['hero_str']

    def first_ctx(pred):
        return next((c for c in ctxs if pred(c)), None)

    def put(key, made, opp):
        flags[key] = (1 if (made and opp) else 0, 1 if opp else 0)

    # ── Open / RFI / open-limp ──
    c_open = first_ctx(lambda c: c['first_action'] and c['n_raises'] == 0
                       and c['limpers'] == 0)
    open_opp = c_open is not None
    put('raise_first', open_opp and c_open['verb'] == 'raise', open_opp)
    put('limp_open', open_opp and c_open['verb'] == 'call', open_opp)

    # ── Limp family ── (hero limped, then faced a raise; his next action
    # partitions the denominator via the PT4 action-string patterns)
    limped = any(c['verb'] == 'call' and c['n_raises'] == 0 for c in ctxs)
    limp_faced = limped and len(hero_str) >= 2
    put('limp_raise', hero_str.startswith('CR'), limp_faced)
    put('limp_call', hero_str.startswith('CC'), limp_faced)
    put('limp_fold', hero_str.startswith('CF'), limp_faced)

    # ── Facing exactly one raise (2bet defense / 3bet opportunity) ──
    c_2b = first_ctx(lambda c: c['n_raises'] == 1 and c['call_amt'] > 0)
    twobet_def_opp = c_2b is not None

    def raise_available(c):
        """PT4 grants a re-raise opportunity when the hero has chips beyond
        the call AND raising is legal/meaningful: facing an all-in with no
        other live player left is call-or-fold only (a raise the hero
        actually made proves the opportunity existed)."""
        if c['verb'] == 'raise':
            return True
        if not c['can_raise']:
            return False
        return not (c['aggressor_allin'] and not c['others_live'])

    put('call_pf_2bet', twobet_def_opp and c_2b['verb'] == 'call', twobet_def_opp)
    threebet_opp = twobet_def_opp and raise_available(c_2b)
    threebet = threebet_opp and c_2b['verb'] == 'raise'
    put('threebet_pf', threebet, threebet_opp)
    put('threebet_nai_u35',
        threebet and not c_2b['allin'] and stack_bb <= 35,
        threebet_opp and stack_bb <= 35)

    # ── Squeeze: facing one raise plus at least one caller of it ──
    sq_opp = twobet_def_opp and c_2b['callers_since_raise'] >= 1
    put('pf_squeeze', sq_opp and c_2b['verb'] == 'raise', sq_opp)

    # ── Steal defense (blinds facing a first-in open from CO/BTN/SB) ──
    fr = st['first_raiser']
    fr_np = positions.get(fr) if fr else None
    steal_attempt = (fr is not None and st['limpers_at_first_raise'] == 0
                     and fr_np in (0, 1, 9))
    # A cold-call between the steal and the blind kills the steal-defense
    # situation (it becomes a squeeze spot, not blind defense).
    blind_def_opp = False
    if steal_attempt and c_2b is not None and hero_np in (8, 9) \
            and c_2b['callers_since_raise'] == 0:
        blind_def_opp = (fr_np in (0, 1)) if hero_np == 9 else (fr_np in (0, 1, 9))
    put('fold_to_steal', blind_def_opp and hero_str == 'F', blind_def_opp)
    put('threebet_steal', blind_def_opp and hero_str.startswith('R'), blind_def_opp)

    # ── BB defending vs an SB open-raise ──
    bb_v_sb = blind_def_opp and hero_np == 8 and fr_np == 9
    put('fold_bb_v_sb', bb_v_sb and hero_str == 'F', bb_v_sb)

    # ── BB facing exactly one limper who is the SB ──
    rslib_opp = (hero_np == 8
                 and first_ctx(lambda c: c['first_action'] and c['n_raises'] == 0
                               and c['limpers'] == 1) is not None
                 and st['first_limper'] is not None
                 and positions.get(st['first_limper']) == 9)
    put('raise_sb_open_limp', rslib_opp and hero_str.startswith('R'), rslib_opp)

    # ── Open-raiser facing a 3bet+ ──
    made_first_raise = any(c['verb'] == 'raise' and c['n_raises'] == 0 for c in ctxs)
    c_f3b = first_ctx(lambda c: c['n_raises'] >= 2 and c['call_amt'] > 0) \
        if made_first_raise else None
    f3b_opp = c_f3b is not None
    put('twobet_pf_fold', f3b_opp and c_f3b['verb'] == 'fold', f3b_opp)
    # 4bet opportunity = facing exactly the 3bet (a later raise means the
    # 4th bet was already made by someone else), with a raise available.
    fourbet_opp = f3b_opp and c_f3b['n_raises'] == 2 and raise_available(c_f3b)
    put('raise_4bet_pf', fourbet_opp and c_f3b['verb'] == 'raise', fourbet_opp)

    # ── 3bettor facing a 4bet+ ──
    c_f4b = first_ctx(lambda c: c['n_raises'] >= 3 and c['call_amt'] > 0) \
        if threebet else None
    f4b_opp = c_f4b is not None
    put('threebet_pf_fold', f4b_opp and c_f4b['verb'] == 'fold', f4b_opp)
    put('fold_4bet_after_3bet_u30',
        f4b_opp and c_f4b['verb'] == 'fold' and stack_bb <= 30,
        f4b_opp and stack_bb <= 30)

    if not with_context:
        return flags

    context = {
        'pfa': st['last_raiser'],
        'hero_line': hero_str,
        'n_raises': st['n_raises_total'],
        'hero_faced_raise': any(c['call_amt'] > 0 and c['n_raises'] >= 1
                                for c in ctxs),
        'hero_called_raise': any(c['verb'] == 'call' and c['n_raises'] >= 1
                                 for c in ctxs),
        'faced_3bet': f3b_opp,
        'faced_4bet': f4b_opp,
        # Any spot where hero faced the 3rd bet or beyond — PT4's
        # flg_p_3bet_def_opp / flg_p_4bet_def_opp do not require hero to have
        # been the original raiser, so a cold-caller who then faces a 3bet
        # also puts the hand in a "3bet+ pot".
        'in_3bet_pot': any(c['n_raises'] >= 2 and c['call_amt'] > 0
                           for c in ctxs),
    }
    return flags, context


# ── Phase 2: postflop stats ─────────────────────────────────────────────────
# Every stat carrying a custom "(HU)" restriction is position-blind in the BBZ
# report: their heads-up filter keyed off a hand-level field (cnt_players_f),
# which collapsed the position grouping so one population-wide value is
# repeated on all six rows. Verified against every PT4 CSV we hold. Those are
# flagged global and gated population-wide; the rest keep per-position
# grouping. The UI always shows true per-position splits (decision Q4).

POSTFLOP_STATS = [
    # key                 report/CSV column label   global?
    ('f_cbet_oop_hu',     'CBet F OOP (HU)',        True),
    ('f_cbet_ip_hu',      'CBet F IP (HU)',         True),
    ('f_float_hu',        'Float F HU',             True),
    ('f_fold_cbet_hu',    'Fold to F Cbet (HU)',    True),
    ('f_fold_cbet_3b',    'Fold to F CBet (3B)',    False),
    ('f_fold_float_hu',   'Fold to F Float HU',     True),
    ('f_cbet_fold_hu',    'CBet F & Fold (HU)',     True),
    ('f_raise_cbet_hu',   'Raise F CBet (HU)',      True),
    ('f_xr_hu',           'XR Flop HU',             True),
    ('f_raise_cbet_3b',   'Raise F CBet (3B)',      False),
    ('t_donk_hu',         'Donk T (HU)',            True),
    ('t_cbet_hu',         'CBet T (HU)',            True),
    ('t_float',           'Float T',                False),
    ('t_probe_hu',        'Probe T (HU)',           True),
    ('t_probe_bet_r',     'Probe T HU & Bet R',     True),
    ('t_fold_cbet',       'Fold to T CBet',         False),
    ('t_fold_probe_hu',   'F to T Pr (HU)',         True),
    ('t_raise_cbet',      'Raise T CBet',           False),
    ('t_raise_probe_hu',  'Raise T Probe (HU)',     True),
    ('r_donk',            'Donk R',                 False),
    ('r_cbet',            'CBet R',                 False),
    ('r_fold_cbet',       'Fold to R CBet',         False),
]

ALL_STATS = PREFLOP_STATS + POSTFLOP_STATS

_PREFIX_STREET = {'f_': 'Flop', 't_': 'Turn', 'r_': 'River'}
STAT_STREET = {k: 'Preflop' for k, _l, _g in PREFLOP_STATS}
STAT_STREET.update({k: _PREFIX_STREET[k[:2]] for k, _l, _g in POSTFLOP_STATS})
STREETS_ORDER = ('Preflop', 'Flop', 'Turn', 'River')

_STREETS = ('preflop', 'flop', 'turn', 'river')


def _folded_by(hand, street):
    """Names that folded before `street` began."""
    out = set()
    for s in _STREETS[:_STREETS.index(street)]:
        for a in hand['streets'][s]:
            if a['verb'] == 'fold':
                out.add(a['name'])
    return out


def _postflop_order(hand, names):
    """Names in postflop action order (button acts last)."""
    btn = hand.get('btn_seat')
    if btn is None:
        btn = max(hand['seats'], default=0)
    seat_of = {v: k for k, v in hand['seats'].items()}
    return sorted(names, key=lambda nm: (seat_of.get(nm, 0) - btn - 1) % 100)


def _street_walk(hand, street, hero):
    """
    Hero's decision contexts on one postflop street, plus street facts.
    Each context: verb, facing (chips to call), n_bets seen so far, first_bettor.
    """
    ctxs = []
    current, committed = 0, {}
    n_bets, first_bettor, aggressor = 0, None, None
    acted = set()
    for a in hand['streets'][street]:
        nm, verb, amt = a['name'], a['verb'], a['amount']
        if nm == hero:
            ctxs.append({'verb': verb,
                         'facing': max(0, current - committed.get(hero, 0)),
                         'n_bets': n_bets, 'first_bettor': first_bettor,
                         'acted_before': set(acted)})
        acted.add(nm)
        if verb == 'bet':
            if n_bets == 0:
                first_bettor = nm
            n_bets += 1
            aggressor = nm
            committed[nm] = committed.get(nm, 0) + amt
            current = max(current, committed[nm])
        elif verb == 'raise':
            n_bets += 1
            aggressor = nm
            committed[nm] = amt
            current = max(current, amt)
        elif verb == 'call':
            committed[nm] = committed.get(nm, 0) + amt
    return ctxs, {'n_bets': n_bets, 'first_bettor': first_bettor,
                  'aggressor': aggressor}


def postflop_stat_flags(hand, pre):
    """
    {key: (made, opp)} for POSTFLOP_STATS. `pre` carries the preflop facts the
    postflop definitions depend on (aggressor, 3bet-pot flags, hero's line).
    """
    flags = {k: (0, 0) for k, _l, _g in POSTFLOP_STATS}
    hero = hand.get('hero')

    def put(key, made, opp):
        flags[key] = (1 if (made and opp) else 0, 1 if opp else 0)

    def facing_ctx(ctxs, start=0):
        """Hero's first decision at or after `start` where there is a bet to
        call — the out-of-position line (check, then face the bet) means this
        is usually NOT hero's first action on the street."""
        return next((c for c in ctxs[start:] if c['facing'] > 0), None)

    board = hand.get('board') or []
    if not hero or len(board) < 3:
        return flags

    live_f = [nm for nm in hand['seats'].values()
              if nm not in _folded_by(hand, 'flop')]
    if hero not in live_f:
        return flags
    hu_f = len(live_f) == 2                      # PT4's cnt_players_f = 2
    order_f = _postflop_order(hand, live_f)
    hero_ip_f = bool(order_f) and order_f[-1] == hero

    pfa = pre['pfa']
    f_ctx, f_info = _street_walk(hand, 'flop', hero)
    if not f_ctx:
        return flags
    first = f_ctx[0]

    # ── Flop ──
    # C-bet: hero was the preflop aggressor and the action reached them unbet.
    cbet_opp = (hero == pfa) and first['n_bets'] == 0
    cbet = cbet_opp and first['verb'] == 'bet'
    put('f_cbet_oop_hu', cbet, cbet_opp and hu_f and not hero_ip_f)
    put('f_cbet_ip_hu', cbet, cbet_opp and hu_f and hero_ip_f)

    # Facing the preflop aggressor's c-bet.
    f_def = facing_ctx(f_ctx)
    cbet_def_opp = (hero != pfa and f_def is not None
                    and f_def['first_bettor'] == pfa)
    def_verb = f_def['verb'] if f_def else None
    put('f_fold_cbet_hu', def_verb == 'fold', cbet_def_opp and hu_f)
    put('f_raise_cbet_hu', def_verb == 'raise', cbet_def_opp and hu_f)
    # 3bet+ pot variants are NOT heads-up restricted and keep position grouping.
    in_3bet_pot = pre['in_3bet_pot']
    put('f_fold_cbet_3b', def_verb == 'fold', cbet_def_opp and in_3bet_pot)
    put('f_raise_cbet_3b', def_verb == 'raise', cbet_def_opp and in_3bet_pot)

    # Check-raise: checked, then faced a bet.
    xr_face = facing_ctx(f_ctx, 1) if first['verb'] == 'check' else None
    xr_opp = xr_face is not None
    put('f_xr_hu', xr_opp and xr_face['verb'] == 'raise', xr_opp and hu_f)

    # C-bet then face a raise.
    raise_back = facing_ctx(f_ctx, 1) if cbet else None
    cbet_raised = raise_back is not None
    put('f_cbet_fold_hu', cbet_raised and raise_back['verb'] == 'fold',
        cbet_raised and hu_f)

    # Float: called exactly one preflop raise in position, and it checks to hero.
    float_opp = (hero != pfa and pre['hero_line'].strip('C') == ''
                 and pre['hero_called_raise'] and pre['n_raises'] == 1
                 and hero_ip_f and first['n_bets'] == 0)
    put('f_float_hu', first['verb'] == 'bet', float_opp and hu_f)

    # Facing a float: hero was the uncontested preflop raiser, checked OOP,
    # and got bet into.
    float_face = (facing_ctx(f_ctx, 1)
                  if (hero == pfa and not pre['hero_faced_raise']
                      and first['n_bets'] == 0 and first['verb'] == 'check')
                  else None)
    float_def_opp = float_face is not None
    put('f_fold_float_hu', float_def_opp and float_face['verb'] == 'fold',
        float_def_opp and hu_f)

    # ── Turn ──
    if len(board) < 4:
        return flags
    live_t = [nm for nm in hand['seats'].values()
              if nm not in _folded_by(hand, 'turn')]
    if hero not in live_t:
        return flags
    order_t = _postflop_order(hand, live_t)
    hero_ip_t = bool(order_t) and order_t[-1] == hero
    f_aggr = f_info['aggressor']
    t_ctx, t_info = _street_walk(hand, 'turn', hero)
    if not t_ctx:
        return flags
    t_first = t_ctx[0]

    # Donk: bet before the previous street's aggressor gets to act.
    donk_opp = (f_aggr is not None and f_aggr != hero
                and t_first['n_bets'] == 0
                and f_aggr not in t_first['acted_before'])
    put('t_donk_hu', t_first['verb'] == 'bet', donk_opp and hu_f)

    # Turn c-bet: hero c-bet the flop and the turn reaches them unbet
    # (a continuation chain, so a flop check-raise is not a "c-bet").
    t_cbet_opp = (cbet and t_first['n_bets'] == 0)
    t_cbet = t_cbet_opp and t_first['verb'] == 'bet'
    put('t_cbet_hu', t_cbet, t_cbet_opp and hu_f)

    # Probe: preflop caller, flop checked through, hero opens the turn before
    # the preflop aggressor acts.
    probe_opp = (hero != pfa and pre['hero_faced_raise']
                 and f_info['n_bets'] == 0 and t_first['n_bets'] == 0
                 and pfa not in t_first['acted_before'])
    probe = probe_opp and t_first['verb'] == 'bet'
    put('t_probe_hu', probe, probe_opp and hu_f)

    # Float turn: called a flop bet in position, then the flop bettor checks
    # the turn to hero.
    t_float_opp = (f_def is not None and f_def['verb'] == 'call'
                   and t_first['n_bets'] == 0 and hero_ip_t
                   and f_aggr in t_first['acted_before'])
    put('t_float', t_first['verb'] == 'bet', t_float_opp)

    # Facing a turn c-bet — only the player who c-bet the flop can be
    # "continuing", so this mirrors the chain rule used for hero's own c-bets.
    flop_cbettor = pfa if (pfa is not None
                           and f_info['first_bettor'] == pfa) else None
    t_def = facing_ctx(t_ctx)
    t_cbet_def_opp = (flop_cbettor is not None and flop_cbettor != hero
                      and t_def is not None
                      and t_def['first_bettor'] == flop_cbettor)
    t_def_verb = t_def['verb'] if t_def else None
    put('t_fold_cbet', t_def_verb == 'fold', t_cbet_def_opp)
    put('t_raise_cbet', t_def_verb == 'raise', t_cbet_def_opp)

    # Facing a turn probe: hero had the flop c-bet chance, checked, now faces a bet.
    t_probe_def_opp = (cbet_opp and first['verb'] == 'check'
                       and t_def is not None)
    put('t_fold_probe_hu', t_probe_def_opp and t_def_verb == 'fold',
        t_probe_def_opp and hu_f)
    put('t_raise_probe_hu', t_probe_def_opp and t_def_verb == 'raise',
        t_probe_def_opp and hu_f)

    # ── River ──
    if len(board) < 5:
        return flags
    if hero in _folded_by(hand, 'river'):
        return flags
    t_aggr = t_info['aggressor']
    r_ctx, _r_info = _street_walk(hand, 'river', hero)
    if not r_ctx:
        return flags
    r_first = r_ctx[0]

    r_donk_opp = (t_aggr is not None and t_aggr != hero
                  and r_first['n_bets'] == 0
                  and t_aggr not in r_first['acted_before'])
    put('r_donk', r_first['verb'] == 'bet', r_donk_opp)

    # River c-bet continues the turn c-bet (same chain rule as the turn).
    r_cbet_opp = (t_cbet and r_first['n_bets'] == 0)
    put('r_cbet', r_first['verb'] == 'bet', r_cbet_opp)

    turn_cbettor = (flop_cbettor if (flop_cbettor is not None
                                     and t_info['first_bettor'] == flop_cbettor)
                    else None)
    r_def = facing_ctx(r_ctx)
    r_cbet_def_opp = (turn_cbettor is not None and turn_cbettor != hero
                      and r_def is not None
                      and r_def['first_bettor'] == turn_cbettor)
    put('r_fold_cbet', r_def is not None and r_def['verb'] == 'fold',
        r_cbet_def_opp)

    # Probe the turn heads-up, then get the chance to open the river.
    put('t_probe_bet_r', r_first['verb'] == 'bet',
        probe and hu_f and r_first['n_bets'] == 0)

    return flags


def hand_stat_flags(hand):
    """{key: (made, opp)} for every stat (preflop + postflop) in one hand."""
    flags, pre = preflop_stat_flags(hand, with_context=True)
    flags.update(postflop_stat_flags(hand, pre))
    return flags


def aggregate_stats(hands, scheme='pt4', winrate=False):
    """
    Aggregate all stats over IR hands.
    Returns {'positions': {bucket: {'hands': n, 'stats': {key: {made, opp}},
                                    'bb': realised, 'bb_adj': all-in adjusted}},
             'global':   {key: {made, opp}}}   (population-wide sums)

    winrate=True also accumulates the big-blind results. That runs the equity
    engine, which is much slower than the flag counting, so it is opt-in.
    """
    keys = [k for k, _l, _g in ALL_STATS]
    positions = {b: {'hands': 0, 'bb': 0.0, 'bb_adj': 0.0,
                     'stats': {k: {'made': 0, 'opp': 0} for k in keys}}
                 for b in POSITION_BUCKETS}
    glob = {k: {'made': 0, 'opp': 0} for k in keys}

    result_bb = None
    if winrate:
        from equity import hero_result_bb as result_bb

    for h in hands:
        bucket = hero_position(h, scheme)
        flags = hand_stat_flags(h)
        if bucket in positions:
            p = positions[bucket]
            p['hands'] += 1
            for k, (made, opp) in flags.items():
                p['stats'][k]['made'] += made
                p['stats'][k]['opp'] += opp
            if result_bb is not None:
                realised, adjusted = result_bb(h)
                p['bb'] += realised
                p['bb_adj'] += adjusted
        for k, (made, opp) in flags.items():
            glob[k]['made'] += made
            glob[k]['opp'] += opp

    return {'positions': positions, 'global': glob}


# ── Aggregation (Phase 0: hands per position) ───────────────────────────────

def aggregate_positions(hands):
    """
    {bucket: hero hand count} over IR hands, plus 'total' and 'unmapped'
    (hands whose hero position could not be determined — should be 0).
    """
    counts = {b: 0 for b in POSITION_BUCKETS}
    unmapped = 0
    for h in hands:
        b = hero_position(h)
        if b in counts:
            counts[b] += 1
        else:
            unmapped += 1
    counts['total'] = sum(counts[b] for b in POSITION_BUCKETS)
    counts['unmapped'] = unmapped
    return counts
