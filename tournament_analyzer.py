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


def _detect_scale(hands, bb_map, starting_chips):
    """
    PPPoker exports a whole tournament at one consistent chip scale — raw, or
    multiplied by 100. Because both blinds AND stacks are scaled together, the
    per-hand BB count is scale-invariant and cannot reveal the scale (the same
    observed big blind maps to two ladder levels 100x apart, e.g. 80000 → the
    "raw" level 22 or, ÷100, the real level 6).

    The one unscaled anchor is `starting_chips`: a hero entering a tournament
    holds ~1 starting stack, so dividing their entry stack by the true scale
    lands near `starting_chips` (~1x), while the wrong scale lands ~100x off.
    We pick the scale (100 preferred) whose divided-down big blinds all match a
    real ladder level and whose entry stack isn't an impossible pile of stacks.
    """
    bbs = [h['big_blind'] for h in hands if h['big_blind']]
    if not bbs:
        return 1
    entry_chips = next((h['chips'] for h in hands if h['chips']), 0)
    for scale in (100, 1):
        if not all(round(bb / scale) in bb_map for bb in bbs):
            continue
        if starting_chips and entry_chips:
            # You can't hold ~100 starting stacks on your first recorded hand;
            # that only happens when the export scale is 100x too large.
            if (entry_chips / scale) / starting_chips > 20:
                continue
        return scale
    return 1


def _infer_level(big_blind, scale, bb_map):
    """Map an observed big blind to a level, correcting for the export scale."""
    if not big_blind:
        return None
    return bb_map.get(round(big_blind / scale))


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

    start_ts = min((h['ts'] for h in hands if h['ts']), default=0)

    # Resolve the export's chip scale once (raw vs x100) so both level lookup
    # and add-on sizing work against the real (unscaled) ladder / starting stack.
    scale = _detect_scale(hands, bb_map, starting_chips)
    # An add-on injects roughly a starting stack; require at least half of one
    # (scale-aware) so ordinary hand-to-hand stack changes aren't misread.
    addon_min_chips = 0.5 * starting_chips * scale if starting_chips else None

    hand_levels    = {}
    level_timeline = []
    rebuys, addons, spots = [], [], []

    prev_post = None   # hero stack after the previous hand (chips + profit)
    prev_bb   = None
    for h in hands:
        lvl = _infer_level(h['big_blind'], scale, bb_map)
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
