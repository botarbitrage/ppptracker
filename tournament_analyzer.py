"""
tournament_analyzer.py — Post-tournament analyser.

Reconciles a tournament's persisted hand records against its static blind
structure (resolved from Firebase by the caller) to produce the data the
Tournament Details graphs need:

  * the ACTUAL blind level each hand was played at — reconciled from the
    observed big-blind size, disambiguating the PPPoker "x100" export scaling
    via the hero's implied BB count; and
  * "spots": lose/rebuy events and add-on injections detected from stack jumps
    that the previous hand's profit/loss cannot explain.

`analyze_tournament` is a pure function of (records, cfg) and performs no I/O,
so it can run synchronously on page load now (it may be slow on very large
tournaments — acceptable for now) and be reused later by an asynchronous Cowork
skill triggered by time or on user request. The caller resolves `cfg` from the
Firebase config docs.
"""

from hand_parser import _seq_num, _hero_player

# Minimum unexplained top-up (in big blinds) to treat a stack jump as an add-on.
# Filters out small stack/profit reconciliation noise while still catching a
# starting-stack-sized injection near the add-on break.
_ADDON_MIN_BB = 5


def _hand_fields(rec):
    """Extract the per-hand fields the analyser needs from one raw record."""
    summary = rec.get('summary', {}) or {}
    fh      = rec.get('full_hand', {}) or {}
    info    = fh.get('info', {}) or {}
    room    = info.get('room', {}) or {}
    players = info.get('players', []) or []
    hero    = _hero_player(players)

    sb = summary.get('G') or room.get('small_blind', 0) or 0
    return {
        'gameid':    summary.get('D', ''),
        'ts':        summary.get('C', 0) or 0,
        'profit':    summary.get('H', 0) or 0,
        'big_blind': sb * 2 if sb else 0,
        'chips':     (hero.get('hand_chips', 0) if hero else 0) or 0,
    }


def _bb_level_map(levels):
    """{big_blind_value: level_number} from a blind-structure level list."""
    m = {}
    for lv in levels or []:
        bb = lv.get('bb')
        if bb:
            m[bb] = lv.get('level')
    return m


def _infer_level(big_blind, chips, bb_map):
    """
    Map an observed big blind to a level number using the tournament's ladder.
    The PPPoker export sometimes scales blinds/chips by 100, so the same bb
    value can match two levels 100x apart; disambiguate with the hero's implied
    BB count (a real count is almost always 1..300). This mirrors the previous
    client-side heuristic but reads the ladder from the DB instead of a
    hardcoded table.
    """
    if not big_blind:
        return None
    direct = bb_map.get(big_blind)
    scaled = bb_map.get(round(big_blind / 100)) if big_blind >= 100 else None
    if direct and scaled and chips:
        def plausible(v):
            return 1 <= v <= 300
        bb_direct = chips / big_blind
        bb_scaled = chips / (big_blind / 100)
        if plausible(bb_direct) and not plausible(bb_scaled):
            return direct
        if plausible(bb_scaled) and not plausible(bb_direct):
            return scaled
    return direct or scaled or None


def analyze_tournament(records, cfg):
    """
    records : persisted hand records for one tournament (any order).
    cfg     : resolved static config. Uses `blind_levels` (list of
              {level, sb, bb, ante}), `max_blinds`, `rebuy_period_end_level`.

    Returns:
      hand_levels    {gameid: level|None}
      level_timeline [{gameid, ts, elapsed, level, chips, bb}]  (chronological)
      rebuys         [{gameid, ts, level, chips_after}]
      addons         [{gameid, ts, level, chips_added}]
      spots          [{type, gameid, ts, level}]   (union, graph-friendly)
      resolved_cfg   (echo of cfg)
    """
    recs = list(records or [])
    # Chronological (oldest-first) by per-tournament sequence number.
    recs.sort(key=lambda r: _seq_num(r.get('summary', {}).get('D', '')))
    hands = [_hand_fields(r) for r in recs]

    bb_map          = _bb_level_map(cfg.get('blind_levels'))
    max_blinds      = cfg.get('max_blinds')
    rebuy_end_level = cfg.get('rebuy_period_end_level')
    starting_chips  = cfg.get('starting_chips')
    level_bb        = {lv.get('level'): lv.get('bb')
                       for lv in (cfg.get('blind_levels') or []) if lv.get('level')}

    start_ts = min((h['ts'] for h in hands if h['ts']), default=0)

    # PPPoker exports a whole tournament at a consistent scale (raw, or x100).
    # Detect it by comparing observed big blinds to the ladder so add-on sizing
    # can be judged against the (unscaled) starting stack.
    scale_votes = {}
    for h in hands:
        tb = level_bb.get(_infer_level(h['big_blind'], h['chips'], bb_map))
        if tb:
            s = round(h['big_blind'] / tb)
            if s in (1, 100):
                scale_votes[s] = scale_votes.get(s, 0) + 1
    scale = max(scale_votes, key=scale_votes.get) if scale_votes else 1
    # An add-on injects roughly a starting stack; require at least half of one
    # (scale-aware) so ordinary hand-to-hand stack changes aren't misread.
    addon_min_chips = 0.5 * starting_chips * scale if starting_chips else None

    hand_levels    = {}
    level_timeline = []
    rebuys, addons, spots = [], [], []

    prev_post = None   # hero stack after the previous hand (chips + profit)
    prev_bb   = None
    for h in hands:
        lvl = _infer_level(h['big_blind'], h['chips'], bb_map)
        if lvl and max_blinds:
            lvl = min(lvl, max_blinds)
        if h['gameid']:
            hand_levels[h['gameid']] = lvl
        level_timeline.append({
            'gameid':  h['gameid'],
            'ts':      h['ts'],
            'elapsed': (h['ts'] - start_ts) if h['ts'] else None,
            'level':   lvl,
            'chips':   h['chips'],
            'bb':      h['big_blind'],
        })

        # A chip injection is stack we can't explain by the prior hand's P/L.
        if prev_post is not None and h['chips'] and h['big_blind']:
            injected      = h['chips'] - prev_post
            injected_bb   = injected / h['big_blind']
            busted_before = prev_bb is not None and prev_post <= 0.5 * prev_bb
            in_window     = (rebuy_end_level is None
                             or (lvl is not None and lvl <= rebuy_end_level + 1))
            big_topup     = (injected >= addon_min_chips if addon_min_chips is not None
                             else injected_bb >= _ADDON_MIN_BB)
            if busted_before and h['chips'] > 0:
                rebuys.append({'gameid': h['gameid'], 'ts': h['ts'],
                               'level': lvl, 'chips_after': h['chips']})
                spots.append({'type': 'rebuy', 'gameid': h['gameid'],
                              'ts': h['ts'], 'level': lvl})
            elif in_window and injected > 0 and big_topup:
                # Substantial top-up not explained by the pot, inside the
                # rebuy/add-on window → best-effort add-on detection.
                addons.append({'gameid': h['gameid'], 'ts': h['ts'],
                               'level': lvl, 'chips_added': injected})
                spots.append({'type': 'addon', 'gameid': h['gameid'],
                              'ts': h['ts'], 'level': lvl})

        prev_post = h['chips'] + h['profit']
        prev_bb   = h['big_blind']

    return {
        'hand_levels':    hand_levels,
        'level_timeline': level_timeline,
        'rebuys':         rebuys,
        'addons':         addons,
        'spots':          spots,
        'resolved_cfg':   cfg,
    }
