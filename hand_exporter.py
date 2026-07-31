"""
hand_exporter.py — PokerStars-style TXT export for PPPoker hand records.

Card encoding, player/flow structure are the same as hand_parser.py.
This module operates on already-parsed records (no file I/O of raw JSON).
"""
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tournament_analyzer import _bb_level_map

_ADELAIDE_TZ = ZoneInfo('Australia/Adelaide')

# ── Tournament level label ──────────────────────────────────────────────────
# PokerStars hand histories label the blind level as a Roman numeral
# ("Level VI"). The real level number is looked up from the tournament's
# resolved blind ladder (matched on the already-rescaled big blind); callers
# without a ladder (cash games, or exports where the ladder couldn't be
# resolved) fall back to the literal "Level I" PokerStars uses for cash-game
# blind announcements.
_ROMAN_TABLE = [
    (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
    (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
    (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
]

def _to_roman(n):
    if not n or n < 1:
        return 'I'
    out = []
    for value, sym in _ROMAN_TABLE:
        count, n = divmod(n, value)
        out.append(sym * count)
    return ''.join(out)

def _level_label(big_blind, ante, blind_levels):
    """'Level <roman>' using the real level matched from the ladder, else 'Level I'."""
    bb_map = _bb_level_map(blind_levels) if blind_levels else {}
    lvl = bb_map.get(big_blind)
    return f'Level {_to_roman(lvl)}' if lvl else 'Level I'

# ── Chip denomination ───────────────────────────────────────────────────────
# PPPoker's API returns chip values 100× the in-game display amount.
# Divide everything by this constant before exporting.
_CHIP_DIV = 100

def _rc(x):
    """Scale a raw PPPoker chip value to the displayed denomination."""
    return round((x or 0) / _CHIP_DIV)

def _scale_flow(flow):
    """Return flow with every 'chips' field in actions/chips_back/winning_info ÷ _CHIP_DIV."""
    result = {}
    for st in ('pre_flop', 'flop', 'turn', 'river'):
        if st not in flow:
            continue
        sd = {}
        if 'actions' in flow[st]:
            sd['actions'] = [
                {k: (_rc(v) if k == 'chips' else v) for k, v in a.items()}
                for a in flow[st]['actions']
            ]
        if 'chips_back' in flow[st]:
            sd['chips_back'] = [
                {k: (_rc(v) if k == 'chips' else v) for k, v in cb.items()}
                for cb in flow[st]['chips_back']
            ]
        for k, v in flow[st].items():
            if k not in sd:
                sd[k] = v
        result[st] = sd
    if 'winning_info' in flow:
        result['winning_info'] = [
            {k: (_rc(v) if k == 'chips' else v) for k, v in w.items()}
            for w in flow['winning_info']
        ]
    for k, v in flow.items():
        if k not in result:
            result[k] = v
    return result

# ── Card helpers ────────────────────────────────────────────────────────────

_PS_RANK = {2:'2',3:'3',4:'4',5:'5',6:'6',7:'7',8:'8',9:'9',
            10:'T',11:'J',12:'Q',13:'K',14:'A'}
_PS_SUIT = {1:'d', 2:'c', 3:'h', 4:'s'}   # diamonds clubs hearts spades


def _card_ps(code):
    """PPPoker card code → PokerStars notation (e.g. 777 → '9s')."""
    if not isinstance(code, int) or code < 256:
        return '??'
    return _PS_RANK.get(code % 256, '?') + _PS_SUIT.get(code // 256, '?')


def _cards_ps(codes):
    return ' '.join(_card_ps(c) for c in (codes or []))


# ── Action helpers ──────────────────────────────────────────────────────────

# Action types we fully understand
_FOLD_TYPES  = {1, 12}    # 12 = fold-and-muck variant
_CHECK_TYPES = {2, 13}    # 13 = first-to-act check variant
_CALL_TYPES  = {3}
_RAISE_TYPES = {4}
_BET_TYPES   = {7}        # postflop bet (type 5 unused in observed data)
_BLIND_TYPES = {8, 9, 10} # sb, bb, ante
_SKIP_TYPES  = {100}      # system event — no chips, skip silently
_KNOWN_TYPES = _FOLD_TYPES | _CHECK_TYPES | _CALL_TYPES | _RAISE_TYPES | \
               _BET_TYPES | _BLIND_TYPES | _SKIP_TYPES


def _pname(seat_map, seatid):
    p = seat_map.get(seatid, {})
    return (p.get('user_name') or f'Seat{seatid + 1}').strip()


def _format_action(action, seat_map, current_bet):
    """
    Convert one PPPoker action to a PokerStars action line.
    Returns (line_or_None, updated_current_bet).
    """
    seatid = action.get('seatid', 0)
    name   = _pname(seat_map, seatid)
    chips  = action.get('chips') or 0
    allin  = (action.get('hand_chips', -1) == 0 and chips > 0)
    sfx    = ' and is all-in' if allin else ''
    t      = action.get('type')

    if t in _SKIP_TYPES or t is None:
        return None, current_bet
    if t in _FOLD_TYPES:
        return f"{name}: folds", current_bet
    if t in _CHECK_TYPES:
        return f"{name}: checks", current_bet
    if t in _CALL_TYPES:
        if chips == 0:
            return None, current_bet          # zero-chip call — PPPoker no-op, suppress
        # If chips > current_bet the player is making an all-in re-raise
        if current_bet > 0 and chips > current_bet:
            line = f"{name}: raises {chips - current_bet} to {chips}{sfx}"
            return line, chips
        return f"{name}: calls {chips}{sfx}", current_bet
    if t in _RAISE_TYPES:
        if chips <= 0:
            return f"{name}: checks", current_bet   # degenerate zero raise → check
        raise_by = max(0, chips - current_bet)
        line = f"{name}: raises {raise_by} to {chips}{sfx}"
        return line, chips
    if t in _BET_TYPES:
        if chips <= 0:
            return f"{name}: checks", current_bet   # degenerate zero bet → check
        return f"{name}: bets {chips}{sfx}", chips
    # Truly unknown type
    return f"# UNKNOWN action type={t} seatid={seatid} chips={chips}", current_bet


def _street_lines(flow, street_name, seat_map, initial_bet=0,
                  start_chips=None, invested_prior=None):
    """Return (lines, street_invested) for one postflop street.

    start_chips     — {seatid: chips} effective starting stack for the hand
    invested_prior  — {seatid: chips} already committed before this street
    street_invested — {seatid: chips} put in during this street (returned)
    """
    lines = []
    current_bet  = initial_bet
    player_bets  = {}   # cumulative bet this street
    street_inv   = {}   # chips committed this street (returned to caller)

    for a in flow.get(street_name, {}).get('actions', []):
        t     = a.get('type')
        sid   = a.get('seatid')
        chips = a.get('chips') or 0

        if t in _CALL_TYPES and current_bet > 0:
            actual = min(chips, max(0, current_bet - player_bets.get(sid, 0)))
            # Cap to remaining chips if start_chips provided
            if start_chips is not None:
                _prior    = (invested_prior or {}).get(sid, 0)
                _this_str = street_inv.get(sid, 0)
                _remain   = max(0, start_chips.get(sid, 0) - _prior - _this_str)
                actual    = min(actual, _remain)
            if actual != chips:
                a     = dict(a, chips=actual)
                chips = actual

        elif t in _RAISE_TYPES | _BET_TYPES and start_chips is not None:
            _prior    = (invested_prior or {}).get(sid, 0)
            _this_str = street_inv.get(sid, 0)
            _remain   = max(0, start_chips.get(sid, 0) - _prior - _this_str)
            if chips > _remain:
                a     = dict(a, chips=_remain)
                chips = _remain

        line, current_bet = _format_action(a, seat_map, current_bet)
        if t in _RAISE_TYPES:
            player_bets[sid] = current_bet
            street_inv[sid]  = current_bet
        elif t in _BET_TYPES:
            player_bets[sid] = player_bets.get(sid, 0) + chips
            street_inv[sid]  = street_inv.get(sid, 0) + chips
        elif t in _CALL_TYPES:
            # Record the TRUE cumulative commitment, not current_bet: an
            # all-in call capped below the bet must not look like a full match,
            # or the balanced-pot heuristic suppresses a real uncalled return
            # (PT4 then rejects the hand as unbalanced).
            player_bets[sid] = player_bets.get(sid, 0) + chips
            street_inv[sid]  = street_inv.get(sid, 0) + chips
        if line:
            lines.append(line)

    for cb in flow.get(street_name, {}).get('chips_back', []):
        name   = _pname(seat_map, cb.get('seatid', 0))
        amount = cb.get('chips', 0)
        if amount:
            lines.append(f"Uncalled bet ({amount}) returned to {name}")
    return lines, street_inv


def _fold_street(flow, seatid):
    for street in ('pre_flop', 'flop', 'turn', 'river'):
        for a in flow.get(street, {}).get('actions', []):
            if a.get('seatid') == seatid and a.get('type') in _FOLD_TYPES:
                return street
    return None


# ── Pot reconstruction from the emitted lines ────────────────────────────────
# Poker Tracker 4 (and every PokerStars-format parser) validates a hand by
# re-deriving the pot from the action lines it reads — antes + blinds + bets +
# calls + raises, minus any "Uncalled bet returned" — and rejects the hand with
# "Invalid pot size" when that total disagrees with the "Total pot" / "collected"
# amounts we declare.  Historically the exporter computed the declared pot in a
# separate pre-pass that had to stay perfectly in sync with the code that emits
# the action lines; the two drifted apart in edge cases (all-in caps, type-4→call
# conversions, incremental-raise fixes, chips_back suppression, stack overrides),
# and the gap frequently equalled a multiple of the ante — which read as "the
# ante was dropped from the pot".  We now compute the declared pot from the very
# lines we emit, so the two are equal by construction for every hand.

_ANTE_RE   = re.compile(r'^(?P<name>.+?): posts the ante (?P<amt>\d+)$')
_BLIND_RE  = re.compile(r'^(?P<name>.+?): posts (?:small|big) blind (?P<amt>\d+)$')
_BET_RE    = re.compile(r'^(?P<name>.+?): bets (?P<amt>\d+)')
_CALL_RE   = re.compile(r'^(?P<name>.+?): calls (?P<amt>\d+)')
_RAISE_RE  = re.compile(r'^(?P<name>.+?): raises \d+ to (?P<to>\d+)')
_UNCALL_RE = re.compile(r'^Uncalled bet \((?P<amt>\d+)\) returned to (?P<name>.+)$')
_STREET_RE = re.compile(r'^\*\*\* (?:FLOP|TURN|RIVER) \*\*\*')


def _pot_from_ps_lines(lines):
    """Re-derive the pot exactly as a PokerStars-format parser does.

    Returns (total_pot, per_name_net) where per_name_net[name] is that player's
    net chips left in the pot (gross wagered minus any uncalled bet returned).
    Antes are dead money — added to the pot but not to the per-street bet used
    to size raises.  Parsing stops at showdown/summary so post-hand
    "collected"/"Seat N:" lines can never be miscounted as wagers.  Bet/call/
    raise patterns intentionally don't anchor the end of the line so the
    " and is all-in" suffix is tolerated.
    """
    total = 0
    net = {}            # name -> net chips contributed to the pot
    street_bet = {}     # name -> chips committed on the current street
    for raw in lines:
        line = raw.strip()
        if line.startswith('*** SHOW DOWN') or line.startswith('*** SUMMARY'):
            break
        if _STREET_RE.match(line):
            street_bet = {}
            continue
        m = _ANTE_RE.match(line)
        if m:
            amt = int(m.group('amt'))
            total += amt
            net[m.group('name')] = net.get(m.group('name'), 0) + amt
            continue
        m = _BLIND_RE.match(line) or _BET_RE.match(line) or _CALL_RE.match(line)
        if m:
            name, amt = m.group('name'), int(m.group('amt'))
            total += amt
            net[name] = net.get(name, 0) + amt
            street_bet[name] = street_bet.get(name, 0) + amt
            continue
        m = _RAISE_RE.match(line)
        if m:
            name, to = m.group('name'), int(m.group('to'))
            delta = to - street_bet.get(name, 0)
            total += delta
            net[name] = net.get(name, 0) + delta
            street_bet[name] = to
            continue
        m = _UNCALL_RE.match(line)
        if m:
            name, amt = m.group('name'), int(m.group('amt'))
            total -= amt
            net[name] = net.get(name, 0) - amt
            continue
    return total, net


def _distribute(total, weights):
    """Split `total` into integer parts ~proportional to `weights`, summing
    EXACTLY to `total`.  Leftover chips (from integer rounding) go to the
    largest weights first.  Guarantees payouts reconcile to the pot, avoiding
    the ±1 "Invalid pot size" errors that per-winner rounding produced."""
    n = len(weights)
    if n == 0:
        return []
    s = sum(weights)
    if s <= 0:                       # no weights — split evenly
        base = total // n
        out = [base] * n
        for i in range(total - base * n):
            out[i % n] += 1
        return out
    out = [total * w // s for w in weights]
    leftover = total - sum(out)
    order = sorted(range(n), key=lambda i: weights[i], reverse=True)
    i = 0
    while leftover > 0:
        out[order[i % n]] += 1
        leftover -= 1
        i += 1
    return out


# ── Single-hand converter ───────────────────────────────────────────────────

def hand_to_ps_block(record, tz=None, stack_overrides=None, blind_levels=None):
    """
    Convert one hand record to a PokerStars-style block.
    Returns (block_str, warnings_list, player_end_stacks).
    Returns (None, [reason], {}) if the hand is unrecoverable.
    player_end_stacks: {user_name: end_stack} computed from action data.
    stack_overrides:   {user_name: chips} — replaces hand_chips in seat listing.
    blind_levels:      resolved tournament ladder ([{level, bb, ...}]) used to
                        label the real level, e.g. "Level VI" instead of "Level I".
    """
    if tz is None:
        tz = _ADELAIDE_TZ

    warnings = []

    summary   = record.get('summary', {})
    fh        = record.get('full_hand', {})
    share_key = record.get('share_key', '')
    info      = fh.get('info', {})
    room      = info.get('room', {})
    players   = info.get('players', [])
    flow      = _scale_flow(fh.get('flow', {}))   # scale all chip values ÷100

    gameid = summary.get('D', '')
    if not gameid:
        return None, ['Missing game ID — hand cannot be exported'], {}

    timestamp   = summary.get('C', 0)
    small_blind = _rc(summary.get('G') or room.get('small_blind', 0))
    big_blind   = small_blind * 2
    ante        = _rc(room.get('ante', summary.get('A', 0)))
    dealer_sid  = room.get('dealer_seatid')
    tourney_id  = gameid.split('-')[1] if '-' in gameid else gameid
    table_num   = str((room.get('mtt') or {}).get('table_num', '1'))
    is_mtt      = bool(room.get('mtt'))
    seat_map    = {p.get('seatid', -1): p for p in players}
    # PokerStars hand IDs must be plain integers — strip PPPoker dashes
    hand_id     = gameid.replace('-', '')

    if not players:
        warnings.append('No player data — seat/action info will be incomplete')

    # Flag any truly unknown action types
    for street in ('pre_flop', 'flop', 'turn', 'river'):
        for a in flow.get(street, {}).get('actions', []):
            t = a.get('type')
            if t is not None and t not in _KNOWN_TYPES:
                warnings.append(f"Unknown action type {t} (rendered as comment)")

    # Timestamps
    dt_local  = datetime.fromtimestamp(timestamp, tz=tz) if timestamp else datetime.now(tz=tz)
    dt_utc    = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else datetime.now(tz=timezone.utc)
    tz_abbr   = dt_local.strftime('%Z')
    local_str = dt_local.strftime('%Y/%m/%d %H:%M:%S')
    utc_str   = dt_utc.strftime('%Y/%m/%d %H:%M:%S')

    lines = []

    # ── Header ──────────────────────────────────────────────────────────────
    # Blinds in the Level string must be bare integers (no commas) — parsers
    # use strict regexes and will reject "400,000/800,000".
    level = f"{_level_label(big_blind, ante, blind_levels)} ({small_blind}/{big_blind}"
    if ante:
        level += f" ante {ante}"
    level += ")"

    if is_mtt:
        lines.append(
            f"PokerStars Hand #{hand_id}: Tournament #{tourney_id}, "
            f"No Limit Hold'em - {level} - "
            f"{local_str} {tz_abbr} [{utc_str} UTC]"
        )
    else:
        lines.append(
            f"PokerStars Hand #{hand_id}: No Limit Hold'em - "
            f"{level} - {local_str} {tz_abbr} [{utc_str} UTC]"
        )

    # ── Table ────────────────────────────────────────────────────────────────
    n_seats   = len(players) if players else 9
    btn_seat  = (dealer_sid + 1) if dealer_sid is not None else '?'
    lines.append(f"Table '{tourney_id} {table_num}' {n_seats}-max Seat #{btn_seat} is the button")

    # ── Seat list ────────────────────────────────────────────────────────────
    # Collect all seatids that appear in any action (active participants).
    _active_sids = set()
    for _st in ('pre_flop', 'flop', 'turn', 'river'):
        for _a in flow.get(_st, {}).get('actions', []):
            _sid = _a.get('seatid')
            if _sid is not None:
                _active_sids.add(_sid)

    # PPPoker's hand_chips is the stack AFTER antes and blinds are deducted.
    # To list the correct pre-hand stack in PokerStars format (seat lines come
    # before antes/blinds actions), we add back each player's prehand payments.
    _prehand_paid = {}  # seatid -> chips paid in antes + blinds this hand
    for _a in flow.get('pre_flop', {}).get('actions', []):
        if _a.get('type') in (8, 9, 10):   # SB, BB, ante
            _s = _a.get('seatid')
            if _s is not None:
                _prehand_paid[_s] = _prehand_paid.get(_s, 0) + (_a.get('chips') or 0)

    for sid in sorted(s for s in seat_map if s >= 0):
        p    = seat_map[sid]
        name = (p.get('user_name') or f'Player{sid+1}').strip()
        chips = (stack_overrides.get(name) if stack_overrides else None)
        if chips is None:
            # hand_chips is post-ante/blind; restore to pre-hand amount
            chips = _rc(p.get('hand_chips', 0) or 0) + _prehand_paid.get(sid, 0)
        # Skip seats with 0 chips that never acted — ghost/bust-out entries that
        # would cause PT4 "Invalid stack" errors.
        if chips == 0 and sid not in _active_sids:
            continue
        lines.append(f"Seat {sid + 1}: {name} ({chips} in chips)")

    # ── Antes then blinds ────────────────────────────────────────────────────
    pre_actions = flow.get('pre_flop', {}).get('actions', [])

    for a in pre_actions:
        if a.get('type') == 10:
            lines.append(f"{_pname(seat_map, a['seatid'])}: posts the ante {a.get('chips',0)}")
    for a in pre_actions:
        if a.get('type') == 8:
            lines.append(f"{_pname(seat_map, a['seatid'])}: posts small blind {a.get('chips',0)}")
    for a in pre_actions:
        if a.get('type') == 9:
            lines.append(f"{_pname(seat_map, a['seatid'])}: posts big blind {a.get('chips',0)}")

    # ── Pre-compute pot and synthetic uncalled bets ───────────────────────────
    # PPPoker's chips_back field is sometimes missing (e.g. when a preflop
    # all-in gets no callers, or a walk to BB occurs with no SB). When absent,
    # compute the uncalled amount per street as:
    #   uncalled = max(player_bets_this_street) - second_max(player_bets_this_street)
    # This matches PT4's own pot computation from the action lines we emit.
    # Effective starting chips per seatid — used to cap all-in amounts so that
    # action lines remain consistent when a stack_override is applied.
    _start_chips = {}
    for _sid_sc, _p_sc in seat_map.items():
        if _sid_sc < 0:
            continue
        _nm_sc = (_p_sc.get('user_name') or '').strip()
        _v_sc  = (stack_overrides.get(_nm_sc) if stack_overrides else None)
        if _v_sc is None:
            # hand_chips is post-ante/blind; restore to pre-hand amount
            _v_sc = _rc(_p_sc.get('hand_chips', 0) or 0) + _prehand_paid.get(_sid_sc, 0)
        _start_chips[_sid_sc] = _v_sc

    _pb             = {}   # seatid → cumulative bet this street
    _pot_in         = 0
    _pot_unc        = 0
    _synth_uncalled = {}   # street → (amount, seatid)
    _player_total   = {}   # seatid → gross chips put in across all streets
    _player_uncalled = {}  # seatid → chips returned as uncalled

    for _st in ('pre_flop', 'flop', 'turn', 'river'):
        if _st != 'pre_flop':
            _pb = {}
        _last_atype = {}   # seatid → last action type that contributed to _pb
        for _a in flow.get(_st, {}).get('actions', []):
            _t, _c, _sid = _a.get('type'), _a.get('chips', 0) or 0, _a.get('seatid')
            if _t == 10:
                _pot_in += _c
                _player_total[_sid] = _player_total.get(_sid, 0) + _c
            elif _t in (8, 9):
                _pot_in += _c; _pb[_sid] = _pb.get(_sid, 0) + _c; _last_atype[_sid] = _t
                _player_total[_sid] = _player_total.get(_sid, 0) + _c
            elif _t == 3:
                _cur_max = max(_pb.values()) if _pb else 0
                _actual  = min(_c, max(0, _cur_max - _pb.get(_sid, 0)))
                # Cap to remaining chips so pot stays consistent with listed stack.
                _remaining = max(0, _start_chips.get(_sid, 0) - _player_total.get(_sid, 0))
                _actual = min(_actual, _remaining)
                _pot_in += _actual; _pb[_sid] = _pb.get(_sid, 0) + _actual; _last_atype[_sid] = 3
                _player_total[_sid] = _player_total.get(_sid, 0) + _actual
            elif _t == 4:
                _pv = _pb.get(_sid, 0)
                # PPPoker sometimes encodes type-4 chips as INCREMENTAL (additional
                # beyond what the player already committed as a blind) rather than
                # cumulative.  Detect: raw increment is < 1 BB (illegal raise alone)
                # but blind + increment >= 1 BB (legal with correction).
                _cur_max_pb = max(_pb.values()) if _pb else 0
                _raw_inc    = _c - _cur_max_pb
                if _pv > 0 and 0 < _raw_inc < big_blind and (_raw_inc + _pv) >= big_blind:
                    _c_adj = _c + _pv   # convert incremental → cumulative
                else:
                    _c_adj = _c
                _delta = max(0, _c_adj - _pv)
                # Cap raise delta to remaining chips.
                _remaining = max(0, _start_chips.get(_sid, 0) - _player_total.get(_sid, 0))
                _delta = min(_delta, _remaining)
                _pot_in += _delta; _pb[_sid] = _pv + _delta; _last_atype[_sid] = 4
                _player_total[_sid] = _player_total.get(_sid, 0) + _delta
            elif _t == 7:
                _pot_in += _c; _pb[_sid] = _pb.get(_sid, 0) + _c; _last_atype[_sid] = 7
                _player_total[_sid] = _player_total.get(_sid, 0) + _c

        _cb_sum = sum(cb.get('chips', 0) for cb in flow.get(_st, {}).get('chips_back', []))
        _sv      = sorted(_pb.values(), reverse=True)
        _balanced = len(_sv) >= 2 and _sv[0] == _sv[1]
        if _cb_sum > 0 and not _balanced:
            # Trust the data-provided chips_back (unless players are balanced —
            # means PPPoker reported a spurious chips_back when no uncalled bet exists).
            _pot_unc += _cb_sum
            for _cb in flow.get(_st, {}).get('chips_back', []):
                _sid_cb = _cb.get('seatid')
                _player_uncalled[_sid_cb] = _player_uncalled.get(_sid_cb, 0) + (_cb.get('chips') or 0)
        elif _pb and not _cb_sum:
            # No chips_back in data — compute synthetic uncalled from _player_bet.
            _max_sid = max(_pb, key=_pb.get)
            _max_lat = _last_atype.get(_max_sid)
            _second  = _sv[1] if len(_sv) > 1 else 0
            _unc     = max(0, _sv[0] - _second)
            # Eligible when the top player raised/bet aggressively, or only blinds
            # were posted and no one called (lone-BB or walk-to-BB scenarios).
            _all_blind_only = bool(_last_atype) and all(v in (8, 9) for v in _last_atype.values())
            _eligible = (
                _max_lat in (4, 7)
                or (_max_lat in (8, 9) and _second == 0)
                or (_max_lat == 9 and _all_blind_only and _unc > 0)  # walk-to-BB
            )
            if _unc > 0 and _eligible:
                _synth_uncalled[_st] = (_unc, _max_sid)
                _pot_unc += _unc
                _player_uncalled[_max_sid] = _player_uncalled.get(_max_sid, 0) + _unc

    # NOTE: total_pot and the per-player end-stack reconstruction are computed
    # AFTER all the action lines are emitted (see below), by re-deriving the pot
    # from those very lines with _pot_from_ps_lines().  That guarantees the
    # declared "Total pot"/"collected" amounts equal what PokerTracker re-computes
    # from the hand history, instead of relying on a parallel pre-pass that could
    # drift.  The pre-pass above is kept only for _start_chips (all-in caps) and
    # _synth_uncalled (synthetic uncalled-bet lines emitted per street).

    # ── Hole cards ───────────────────────────────────────────────────────────
    hero = next((p for p in players if p.get('isSelf')), None)
    hero_name  = (hero.get('user_name') or 'Hero').strip() if hero else 'Hero'
    hero_cards = info.get('cards') or summary.get('B', [])

    lines.append("*** HOLE CARDS ***")
    if hero_cards:
        lines.append(f"Dealt to {hero_name} [{_cards_ps(hero_cards)}]")
    else:
        lines.append(f"Dealt to {hero_name} [?? ??]")
        warnings.append("Hero hole cards unavailable — shown as ?? ??")

    # ── Preflop actions ──────────────────────────────────────────────────────
    # _disp_pb tracks per-player cumulative bet for call correction.
    # _disp_antes tracks per-player antes (not in _disp_pb) for stack-cap checks.
    _disp_antes = {}
    for _a in pre_actions:
        if _a.get('type') == 10:
            _sid_b = _a.get('seatid')
            _disp_antes[_sid_b] = _disp_antes.get(_sid_b, 0) + (_a.get('chips') or 0)
    _disp_pb = {}
    for _a in pre_actions:
        if _a.get('type') in (8, 9):
            _sid_b = _a.get('seatid')
            _disp_pb[_sid_b] = _disp_pb.get(_sid_b, 0) + (_a.get('chips') or 0)

    current_bet = big_blind
    for a in pre_actions:
        if a.get('type') in _BLIND_TYPES:
            continue
        t    = a.get('type')
        sid  = a.get('seatid')
        chips = a.get('chips') or 0
        if t in _CALL_TYPES:
            _cur_max = max(_disp_pb.values()) if _disp_pb else 0
            actual   = min(chips, max(0, _cur_max - _disp_pb.get(sid, 0)))
            # Cap to remaining chips so the action line is consistent with the
            # overridden seat stack (avoids PT4 "Invalid stack" on all-in calls).
            _invested_pf = _disp_antes.get(sid, 0) + _disp_pb.get(sid, 0)
            _p_start_pf  = _start_chips.get(sid, _rc(seat_map.get(sid, {}).get('hand_chips', 0) or 0) + _prehand_paid.get(sid, 0))
            actual = min(actual, max(0, _p_start_pf - _invested_pf))
            a     = dict(a, chips=actual)
            chips = actual
        elif t in _RAISE_TYPES and chips <= current_bet and chips > 0:
            # PPPoker encodes some calls as type-4 with no raise increment.
            # Convert to a corrected type-3 call using the player's known investment.
            actual_call = max(0, chips - _disp_pb.get(sid, 0))
            a     = dict(a, type=3, chips=actual_call)
            t     = 3
            chips = actual_call
        elif t in _RAISE_TYPES:
            # PPPoker sometimes sends type-4 chips as INCREMENTAL (additional
            # beyond the blind already posted) instead of cumulative.  Detect by
            # checking if the raw raise increment is below 1 BB (illegal alone)
            # but becomes legal when the player's blind commitment is added back.
            _blind_committed = _disp_pb.get(sid, 0)
            _raw_inc = chips - current_bet
            if _blind_committed > 0 and 0 < _raw_inc < big_blind and (_raw_inc + _blind_committed) >= big_blind:
                chips = chips + _blind_committed
                a     = dict(a, chips=chips)
            # Cap raise-to amount to the player's available chips (antes already paid).
            _p_start_r = _start_chips.get(sid, _rc(seat_map.get(sid, {}).get('hand_chips', 0) or 0) + _prehand_paid.get(sid, 0))
            _max_raise  = _p_start_r - _disp_antes.get(sid, 0)
            if chips > _max_raise:
                a     = dict(a, chips=_max_raise)
                chips = _max_raise
        line, current_bet = _format_action(a, seat_map, current_bet)
        if t in _RAISE_TYPES:
            _disp_pb[sid] = current_bet
        elif t in _CALL_TYPES:
            # True cumulative commitment (chips is already corrected/capped) —
            # a capped all-in call must not register as matching current_bet,
            # or _pf_balanced wrongly suppresses the uncalled-bet return line
            # and PT4 rejects the hand as unbalanced.
            _disp_pb[sid] = _disp_pb.get(sid, 0) + chips
        elif t in _BET_TYPES:
            _disp_pb[sid] = _disp_pb.get(sid, 0) + chips
        if line:
            lines.append(line)
    # Suppress chips_back from raw data when all active players are balanced
    # (PPPoker sometimes reports a spurious chips_back in that situation).
    _pf_sv = sorted(_disp_pb.values(), reverse=True)
    _pf_balanced = len(_pf_sv) >= 2 and _pf_sv[0] == _pf_sv[1]
    if not _pf_balanced:
        for cb in flow.get('pre_flop', {}).get('chips_back', []):
            name   = _pname(seat_map, cb.get('seatid', 0))
            amount = cb.get('chips', 0)
            if amount:
                lines.append(f"Uncalled bet ({amount}) returned to {name}")
    if 'pre_flop' in _synth_uncalled:
        _sa, _ss = _synth_uncalled['pre_flop']
        lines.append(f"Uncalled bet ({_sa}) returned to {_pname(seat_map, _ss)}")

    # Cumulative investment per seatid entering each postflop street.
    # Preflop: antes + blind bets (from display loop trackers).
    _inv_entering = {
        sid: _disp_antes.get(sid, 0) + _disp_pb.get(sid, 0)
        for sid in set(list(_disp_antes) + list(_disp_pb))
    }

    # ── Flop ─────────────────────────────────────────────────────────────────
    flop_cards = flow.get('flop', {}).get('cards', [])
    if flop_cards:
        lines.append(f"*** FLOP *** [{_cards_ps(flop_cards)}]")
        _flop_lines, _flop_inv = _street_lines(flow, 'flop', seat_map,
                                               start_chips=_start_chips,
                                               invested_prior=_inv_entering)
        lines.extend(_flop_lines)
        if 'flop' in _synth_uncalled:
            _sa, _ss = _synth_uncalled['flop']
            lines.append(f"Uncalled bet ({_sa}) returned to {_pname(seat_map, _ss)}")
        # Accumulate into entering dict for next street
        for _s, _v in _flop_inv.items():
            _inv_entering[_s] = _inv_entering.get(_s, 0) + _v

    # ── Turn ─────────────────────────────────────────────────────────────────
    turn_cards = flow.get('turn', {}).get('cards', [])
    if turn_cards:
        board = _cards_ps(flop_cards)
        lines.append(f"*** TURN *** [{board}] [{_cards_ps(turn_cards)}]")
        _turn_lines, _turn_inv = _street_lines(flow, 'turn', seat_map,
                                               start_chips=_start_chips,
                                               invested_prior=_inv_entering)
        lines.extend(_turn_lines)
        if 'turn' in _synth_uncalled:
            _sa, _ss = _synth_uncalled['turn']
            lines.append(f"Uncalled bet ({_sa}) returned to {_pname(seat_map, _ss)}")
        for _s, _v in _turn_inv.items():
            _inv_entering[_s] = _inv_entering.get(_s, 0) + _v

    # ── River ────────────────────────────────────────────────────────────────
    river_cards = flow.get('river', {}).get('cards', [])
    if river_cards:
        board = _cards_ps(flop_cards + turn_cards)
        lines.append(f"*** RIVER *** [{board}] [{_cards_ps(river_cards)}]")
        _river_lines, _ = _street_lines(flow, 'river', seat_map,
                                        start_chips=_start_chips,
                                        invested_prior=_inv_entering)
        lines.extend(_river_lines)
        if 'river' in _synth_uncalled:
            _sa, _ss = _synth_uncalled['river']
            lines.append(f"Uncalled bet ({_sa}) returned to {_pname(seat_map, _ss)}")

    # ── Show down ────────────────────────────────────────────────────────────
    show_hands = flow.get('show_hands', [])
    if show_hands:
        lines.append("*** SHOW DOWN ***")
        for sh in show_hands:
            name = _pname(seat_map, sh.get('seatid', 0))
            cards = sh.get('code', [])
            if cards:
                lines.append(f"{name}: shows [{_cards_ps(cards)}]")
            else:
                lines.append(f"{name}: shows [?? ??]")
                warnings.append(f"{name} shown at showdown but cards missing")

    # ── Summary ──────────────────────────────────────────────────────────────
    winning_info = flow.get('winning_info', [])
    winner_sids  = {w.get('seatid') for w in winning_info}
    all_board    = flop_cards + turn_cards + river_cards

    sb_sid = next((a.get('seatid') for a in pre_actions if a.get('type') == 8), None)
    bb_sid = next((a.get('seatid') for a in pre_actions if a.get('type') == 9), None)

    # Derive the pot (and each player's net contribution) from the action lines
    # we just emitted, so the declared "Total pot"/"collected" amounts equal what
    # PokerTracker re-computes from this hand history.  This is the single source
    # of truth for the pot — no parallel pre-pass to drift out of sync.
    total_pot, _emit_net = _pot_from_ps_lines(lines)

    # Split the pot across winners with integer amounts that sum EXACTLY to
    # total_pot (largest pools absorb the rounding remainder), so payouts always
    # reconcile to the pot.
    _win_amts   = _distribute(total_pot, [w.get('chips', 0) for w in winning_info])
    _won_by_sid = {}
    for w, amt in zip(winning_info, _win_amts):
        _won_by_sid[w.get('seatid')] = _won_by_sid.get(w.get('seatid'), 0) + amt

    # ── Per-player end-stack reconstruction ──────────────────────────────────
    # Compute each player's stack after this hand so the next hand's seat listing
    # can be corrected if PPPoker's hand_chips drifts.  Net invested comes from
    # the same emitted lines (keyed by the emitted name) so stacks stay consistent
    # with the pot.
    player_end_stacks = {}
    for _sid_es, _p_es in seat_map.items():
        if _sid_es < 0:
            continue
        _name_es = (_p_es.get('user_name') or f'Player{_sid_es+1}').strip()
        # Effective starting chips: override if supplied, else hand_chips.
        # hand_chips is post-ante/blind, so add back prehand payments.
        _eff = (stack_overrides.get(_name_es) if stack_overrides else None)
        if _eff is None:
            _eff = _rc(_p_es.get('hand_chips', 0) or 0) + _prehand_paid.get(_sid_es, 0)
        _net_invested = _emit_net.get(_pname(seat_map, _sid_es), 0)
        _end = _eff - _net_invested + _won_by_sid.get(_sid_es, 0)
        if _end >= 0:
            player_end_stacks[_name_es] = _end

    # "collected from pot" lines — PT4 uses these to validate pot distribution.
    # Amounts come from the exact-sum split so they reconcile to total_pot.
    if len(winning_info) == 1:
        w = winning_info[0]
        lines.append(f"{_pname(seat_map, w.get('seatid', 0))} collected {total_pot} from pot")
    else:
        for i, (w, amt) in enumerate(zip(winning_info, _win_amts)):
            pid   = w.get('poolid', w.get('pool_id', i))
            label = 'main pot' if pid == 0 else f'side pot-{pid}'
            lines.append(f"{_pname(seat_map, w.get('seatid', 0))} collected {amt} from {label}")

    lines.append("*** SUMMARY ***")
    lines.append(f"Total pot {total_pot} | Rake 0")
    if all_board:
        lines.append(f"Board [{_cards_ps(all_board)}]")

    for sid in sorted(s for s in seat_map if s >= 0):
        p    = seat_map[sid]
        name = (p.get('user_name') or f'Player{sid+1}').strip()
        # Skip ghost seats (0 chips, never acted) — consistent with seat list above
        if int(p.get('hand_chips', 0) or 0) == 0 and sid not in _active_sids:
            continue

        roles = []
        if sid == dealer_sid:
            roles.append('button')
        if sid == sb_sid:
            roles.append('small blind')
        if sid == bb_sid:
            roles.append('big blind')
        role_str = f" ({', '.join(roles)})" if roles else ''

        fold_st = _fold_street(flow, sid)

        if sid in winner_sids:
            # Reuse the exact-sum split computed above so these summary amounts
            # match the "collected" lines and the "Total pot" total exactly.
            won = _won_by_sid.get(sid, 0)
            shown_cards = next((sh.get('code', []) for sh in show_hands if sh.get('seatid') == sid), None)
            if shown_cards:
                lines.append(
                    f"Seat {sid+1}: {name}{role_str} showed [{_cards_ps(shown_cards)}] and won ({won})"
                )
            else:
                lines.append(f"Seat {sid+1}: {name}{role_str} collected ({won})")
        elif fold_st == 'pre_flop':
            # "(didn't bet)" applies to players who folded without posting SB or BB.
            # Antes don't count — even ante-posters get "(didn't bet)" if they weren't a blind.
            posted_blind = any(
                a.get('seatid') == sid and a.get('type') in {8, 9}
                for a in pre_actions
            )
            suffix = '' if posted_blind else " (didn't bet)"
            lines.append(f"Seat {sid+1}: {name}{role_str} folded before Flop{suffix}")
        elif fold_st == 'flop':
            lines.append(f"Seat {sid+1}: {name}{role_str} folded on the Flop")
        elif fold_st == 'turn':
            lines.append(f"Seat {sid+1}: {name}{role_str} folded on the Turn")
        elif fold_st == 'river':
            lines.append(f"Seat {sid+1}: {name}{role_str} folded on the River")
        else:
            shown_at_sd = next(
                (sh.get('code', []) for sh in show_hands if sh.get('seatid') == sid), None
            )
            if shown_at_sd:
                lines.append(f"Seat {sid+1}: {name}{role_str} showed [{_cards_ps(shown_at_sd)}] and lost")
            else:
                lines.append(f"Seat {sid+1}: {name}{role_str} mucked")

    if warnings:
        for w in warnings:
            lines.append(f"# WARNING: {w}")

    return '\n'.join(lines), warnings, player_end_stacks


# ── Validation summary ──────────────────────────────────────────────────────

def validate_hands(records):
    """Return a validation summary dict from a list of records."""
    if not records:
        return dict(hands_imported=0, full_hands_loaded=0, hands_with_hero_cards=0,
                    hands_with_board_cards=0, hands_with_shown_cards=0,
                    unknown_action_types=[], export_ready=0, skipped=0)

    full_loaded = with_hero = with_board = with_turn = with_shown = export_ready = skipped = won = 0
    unknown_types: set = set()

    for rec in records:
        fh      = rec.get('full_hand', {})
        info    = fh.get('info', {})
        players = info.get('players', [])
        flow    = fh.get('flow', {})
        summary = rec.get('summary', {})

        if players:
            full_loaded += 1
        if info.get('cards') or summary.get('B'):
            with_hero += 1
        if flow.get('flop', {}).get('cards'):
            with_board += 1
        if flow.get('turn', {}).get('cards'):
            with_turn += 1
        if flow.get('show_hands'):
            with_shown += 1
        if (summary.get('H') or 0) > 0:
            won += 1

        for street in ('pre_flop', 'flop', 'turn', 'river'):
            for a in flow.get(street, {}).get('actions', []):
                t = a.get('type')
                if t is not None and t not in _KNOWN_TYPES:
                    unknown_types.add(t)

        if summary.get('D'):
            export_ready += 1
        else:
            skipped += 1

    return dict(
        hands_imported=len(records),
        full_hands_loaded=full_loaded,
        hands_with_hero_cards=with_hero,
        hands_with_board_cards=with_board,
        hands_with_turn=with_turn,
        hands_with_shown_cards=with_shown,
        unknown_action_types=sorted(unknown_types),
        export_ready=export_ready,
        skipped=skipped,
        hands_won=won,
    )



def _records_to_blocks(records, tz, blind_levels_by_room):
    """
    Convert records to PokerStars text blocks (oldest-first), carrying the
    per-player computed-stack overrides from hand to hand. This is the core
    of export_pokerstars, shared with records_to_ps_text so the leak engine
    consumes the exact same text a file export would produce.
    Returns (blocks, {attempted, converted, warned, skipped}).
    """
    import re as _re3
    blind_levels_by_room = blind_levels_by_room or {}

    def _norm_room2(name):
        return _re3.sub(r'[^A-Z0-9 ]', '', (name or '').upper()).strip()

    records = sorted(records, key=lambda r: r.get('summary', {}).get('C', 0))

    attempted = converted = warned = skipped = 0
    blocks = []
    computed_stacks = {}   # {player_name → end stack computed from previous hand}

    for i, rec in enumerate(records):
        # Build stack overrides from computed_stacks with safeguards.
        # Threshold: discard override when |diff| > 2×BB (likely rebuy or cascade error).
        stack_overrides = {}
        _fh_r   = rec.get('full_hand', {})
        _info_r = _fh_r.get('info', {})
        _sum_r  = rec.get('summary', {})
        _sb_r   = _rc(_sum_r.get('G') or _info_r.get('room', {}).get('small_blind', 0))
        _bb_r   = (_sb_r * 2) if _sb_r else 0
        # Compute prehand antes+blinds per player name so _listed is on the same
        # pre-hand basis as computed_stacks (which are post-previous-hand stacks).
        _flow_r  = _scale_flow(_fh_r.get('flow', {}))
        _sid2nm_r = {p.get('seatid', -1): (p.get('user_name') or '').strip()
                     for p in _info_r.get('players', [])}
        _prehand_paid_r = {}  # player_name -> antes+blinds paid this hand
        for _a_r in _flow_r.get('pre_flop', {}).get('actions', []):
            if _a_r.get('type') in (8, 9, 10):
                _n_r = _sid2nm_r.get(_a_r.get('seatid'), '')
                if _n_r:
                    _prehand_paid_r[_n_r] = _prehand_paid_r.get(_n_r, 0) + (_a_r.get('chips') or 0)
        for _p_r in _info_r.get('players', []):
            _nm_r = (_p_r.get('user_name') or '').strip()
            if not _nm_r or _nm_r not in computed_stacks:
                continue
            # Restore to pre-hand basis (hand_chips is post-ante/blind)
            _listed   = _rc(_p_r.get('hand_chips', 0) or 0) + _prehand_paid_r.get(_nm_r, 0)
            _computed = computed_stacks[_nm_r]
            if _computed < 0:
                continue                             # safeguard: computation went negative
            if _bb_r and abs(_listed - _computed) > 2 * _bb_r:
                continue                             # large diff: likely rebuy or cascade error
            stack_overrides[_nm_r] = _computed

        _room_r = _info_r.get('room', {}).get('room_name', '')
        _levels_r = blind_levels_by_room.get(_norm_room2(_room_r))

        try:
            block, w, end_stacks = hand_to_ps_block(rec, tz, stack_overrides or None, _levels_r)
            computed_stacks.update(end_stacks)
            if block is None:
                skipped += 1
                gid = rec.get('summary', {}).get('D', f'index-{i+1}')
                blocks.append(f"# SKIPPED hand {gid}: {'; '.join(w)}")
            elif w:
                warned += 1
                blocks.append(block)
            else:
                converted += 1
                blocks.append(block)
        except Exception as exc:
            skipped += 1
            gid = rec.get('summary', {}).get('D', f'index-{i+1}')
            blocks.append(f"# SKIPPED hand {gid}: unexpected error — {exc}")
        attempted += 1

    return blocks, dict(attempted=attempted, converted=converted,
                        warned=warned, skipped=skipped)


def records_to_ps_text(records, tz=None, blind_levels_by_room=None):
    """
    Records → PokerStars-dialect text (no file I/O). Same conversion path as
    export_pokerstars, so downstream consumers (the leak engine) see exactly
    the text a user export would contain.
    """
    if tz is None:
        tz = _ADELAIDE_TZ
    blocks, stats = _records_to_blocks(records, tz, blind_levels_by_room)
    return '\n\n'.join(blocks) + '\n', stats


# ── Full export ─────────────────────────────────────────────────────────────

def export_pokerstars(records, tz=None, platform=None, blind_levels_by_room=None):
    """
    Write all records to a PokerStars-style TXT file.
    Returns (filepath, log_dict).
    blind_levels_by_room: optional {normalized_room_name: blind_levels list} so
                           each hand's "Level" header reflects its own
                           tournament's real ladder (a session export can span
                           several different tournaments/room types).
    """
    if tz is None:
        tz = _ADELAIDE_TZ
    blind_levels_by_room = blind_levels_by_room or {}

    def _norm_room(name):
        import re as _re2
        return _re2.sub(r'[^A-Z0-9 ]', '', (name or '').upper()).strip()

    os.makedirs(os.path.join('exports', 'pokerstars'), exist_ok=True)

    # Build a meaningful filename: pppoker_<room>_<date>_<time>.txt
    # Extract room name from the first record with one; keep only alphanumerics.
    import re as _re
    _room_raw = ''
    for _r in records:
        _room_raw = (_r.get('full_hand', {}).get('info', {})
                      .get('room', {}).get('room_name', '') or '')
        if _room_raw:
            break
    _room_slug = _re.sub(r'[^A-Za-z0-9]', '', _room_raw)[:24]  # cap length
    # Filename convention:
    #   tournament export → pppoker_<RoomName>_<timestamp>.txt
    #   full export (all hands) → pppoker_full_export_<timestamp>.txt
    _is_single_tourney = len({
        (_re.sub(r'[^A-Za-z0-9]', '', r.get('full_hand', {}).get('info', {})
                  .get('room', {}).get('room_name', '') or ''))
        for r in records if r.get('full_hand', {}).get('info', {}).get('room', {}).get('room_name')
    }) == 1
    _platform_slug = _re.sub(r'[^A-Za-z0-9]', '', platform or '')[:20]
    _ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    if _room_slug and _is_single_tourney:
        if _platform_slug:
            filename = f"pppoker_{_room_slug}_{_platform_slug}_{_ts}.txt"
        else:
            filename = f"pppoker_{_room_slug}_{_ts}.txt"
    else:
        if _platform_slug:
            filename = f"pppoker_full_export_{_platform_slug}_{_ts}.txt"
        else:
            filename = f"pppoker_full_export_{_ts}.txt"
    filepath = os.path.join('exports', 'pokerstars', filename)

    # Sort oldest→newest so PT4's stack-continuity checks pass
    blocks, stats = _records_to_blocks(records, tz, blind_levels_by_room)
    attempted, converted = stats['attempted'], stats['converted']
    warned, skipped = stats['warned'], stats['skipped']
    log_lines = [
        '=' * 72,
        'CONVERSION LOG',
        f'Total hands attempted : {attempted}',
        f'Fully converted       : {converted}',
        f'Exported with warnings: {warned}',
        f'Hands skipped         : {skipped}',
        '=' * 72,
    ]

    # Write hand history file (clean — no non-PS content)
    with open(filepath, 'w', encoding='utf-8') as fh:
        fh.write('\n\n'.join(blocks))
        fh.write('\n')

    # Write conversion log to a separate file so it never contaminates the HH
    log_path = filepath.replace('.txt', '.log')
    with open(log_path, 'w', encoding='utf-8') as lf:
        lf.write('\n'.join(log_lines))
        lf.write('\n')

    return filepath, dict(attempted=attempted, converted=converted, warned=warned, skipped=skipped)
