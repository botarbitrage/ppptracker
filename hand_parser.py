"""PPPoker hand parser — card decoding, position calc, stats."""

# Card encoding: suit_idx = code // 256, rank = code % 256
_RANK_MAP = {2:'2',3:'3',4:'4',5:'5',6:'6',7:'7',8:'8',9:'9',
             10:'T',11:'J',12:'Q',13:'K',14:'A'}
_SUIT_MAP  = {1:'♦', 2:'♣', 3:'♥', 4:'♠'}
_SUIT_CLASS = {1:'suit-diamonds', 2:'suit-clubs', 3:'suit-hearts', 4:'suit-spades'}


def decode_card(code):
    if not isinstance(code, int) or code < 256:
        return {'rank':'?', 'suit':'?', 'suit_class':'suit-unknown', 'display':'??'}
    suit_idx = code // 256
    rank = code % 256
    rank_str = _RANK_MAP.get(rank, str(rank))
    suit_str = _SUIT_MAP.get(suit_idx, '?')
    return {
        'rank': rank_str,
        'suit': suit_str,
        'suit_class': _SUIT_CLASS.get(suit_idx, 'suit-unknown'),
        'display': rank_str + suit_str,
    }


def decode_cards(codes):
    return [decode_card(c) for c in (codes or [])]


def _hero_player(players):
    for p in players:
        if p.get('isSelf'):
            return p
    return None


def calc_position(dealer_seatid, hero_seatid, active_seatids):
    if dealer_seatid is None or hero_seatid is None or not active_seatids:
        return '?'
    seats = sorted(active_seatids)
    if dealer_seatid not in seats or hero_seatid not in seats:
        return '?'
    n = len(seats)
    dealer_idx = seats.index(dealer_seatid)
    ordered = [seats[(dealer_idx + i) % n] for i in range(n)]
    # ordered[0]=BTN, [1]=SB, [2]=BB, [3]=UTG, ...
    pos_names = {0:'BTN', 1:'SB', 2:'BB'}
    late = ['UTG','UTG+1','UTG+2','LJ','HJ','CO']
    for i in range(n - 3):
        pos_names[3 + i] = late[i] if i < len(late) else f'P{3+i}'
    idx = ordered.index(hero_seatid)
    return pos_names.get(idx, f'P{idx}')


def last_street_reached(flow, hero_seatid, sd_res, hero_uid):
    """Return the last street the hero actively participated in.

    Preflop folds are split into:
      'Pre'      — no voluntary chips in (non-VPIP, pure fold)
      'Pre VPIP' — hero put chips in voluntarily preflop before hand ended
      'VPIP' — hero put chips in voluntarily preflop (called/raised) before hand ended
    """
    if _hero_at_showdown(sd_res, hero_uid):
        return 'SD'
    for street, label in [('river','River'), ('turn','Turn'), ('flop','Flop')]:
        if any(a.get('seatid') == hero_seatid
               for a in flow.get(street, {}).get('actions', [])):
            return label
    # Hand ended preflop — distinguish VPIP from pure fold
    pre_actions = flow.get('pre_flop', {}).get('actions', [])
    return 'Pre VPIP' if _is_vpip(pre_actions, hero_seatid) else 'Pre'


def summarize_actions(flow, hero_seatid):
    parts = []
    for street_name, label in [
        ('pre_flop','Pre'), ('flop','Flop'), ('turn','Turn'), ('river','River')
    ]:
        actions = flow.get(street_name, {}).get('actions', [])
        hero_acts = [a for a in actions if a.get('seatid') == hero_seatid]
        if street_name == 'pre_flop':
            hero_acts = [a for a in hero_acts if a.get('type') not in (8, 9, 10)]
        labels = []
        for a in hero_acts:
            t = a.get('type')
            if t == 1:   labels.append('fold')
            elif t == 2: labels.append('check')
            elif t == 3: labels.append('call')
            elif t in (4, 5): labels.append('raise')
        if labels:
            parts.append(f"{label}: {', '.join(labels)}")
    return ' · '.join(parts)


def _is_vpip(pre_actions, hero_seatid):
    return any(
        a.get('seatid') == hero_seatid and a.get('type') in (3, 4)
        for a in pre_actions
    )


def _is_pfr(pre_actions, hero_seatid):
    return any(
        a.get('seatid') == hero_seatid and a.get('type') == 4
        for a in pre_actions
    )


def _postflop_af(flow, hero_seatid):
    bets = calls = 0
    for street in ('flop', 'turn', 'river'):
        for a in flow.get(street, {}).get('actions', []):
            if a.get('seatid') != hero_seatid:
                continue
            t = a.get('type')
            if t in (4, 5): bets += 1
            elif t == 3:    calls += 1
    return bets, calls


def _hero_saw_flop(flow, hero_seatid):
    if not flow.get('flop', {}).get('cards'):
        return False
    for a in flow.get('pre_flop', {}).get('actions', []):
        if a.get('seatid') == hero_seatid and a.get('type') == 1:
            return False
    return True


def _hero_at_showdown(showdown_result, hero_uid):
    for sr in showdown_result:
        if sr.get('uid') == hero_uid:
            return sr.get('is_showdown', False)
    return False


def _hero_won_showdown(winning_info, showdown_result, hero_seatid, hero_uid):
    if not _hero_at_showdown(showdown_result, hero_uid):
        return False
    return any(
        w.get('seatid') == hero_seatid and w.get('chips', 0) > 0
        for w in winning_info
    )


def extract_tourney_id(gameid):
    parts = (gameid or '').split('-')
    return parts[1] if len(parts) >= 2 else (gameid or 'unknown')


def _seq_num(gameid):
    """Per-tournament strictly-monotonic hand sequence number (3rd segment of gameid), or 0 if unparseable."""
    parts = (gameid or '').split('-')
    try:
        return int(parts[2]) if len(parts) >= 3 else 0
    except ValueError:
        return 0


def _fmt_duration(seconds):
    """Format a duration in seconds as 'Xh YYm' or 'Ym'."""
    if seconds is None or seconds < 0:
        return 'N/A'
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _empty_stats():
    return dict(
        total_hands=0, vpip_pct=0.0, pfr_pct=0.0, af=0.0,
        wtsd_pct=0.0, wsd_pct=0.0, bb_100=0.0,
        net_profit=0, biggest_win=0, biggest_loss=0,
    )


def process_hands(records):
    """
    records: list of {summary, share_key, full_hand} dicts (newest-first order).
    Returns (recent_hands, recent_played_hands, stats, tournaments).
    """
    if not records:
        return [], [], _empty_stats(), []

    total = len(records)
    vpip = pfr = af_bets = af_calls = 0
    wtsd_elig = wtsd = wsd = 0
    hero_saw_turn = hero_saw_river = 0
    net_profit = 0
    biggest_win = biggest_loss = 0
    bb_list = []

    tourney_map = {}
    recent_hands = []
    recent_won   = []

    for i, rec in enumerate(records):
        summary  = rec.get('summary', {})
        share_key = rec.get('share_key', '')
        fh       = rec.get('full_hand', {})

        info    = fh.get('info', {})
        room    = info.get('room', {})
        players = info.get('players', [])
        flow    = fh.get('flow', {})
        sd_res  = flow.get('showdown_result', [])
        win_info= flow.get('winning_info', [])

        gameid    = summary.get('D', '')
        timestamp = summary.get('C', 0)
        profit    = summary.get('H', 0)
        hole_codes= fh.get('info', {}).get('cards') or summary.get('B', [])
        small_blind = summary.get('G') or room.get('small_blind', 1)
        big_blind = small_blind * 2

        hero = _hero_player(players)
        hero_seatid = hero.get('seatid') if hero else None
        hero_uid    = hero.get('uid')    if hero else None
        hero_chips  = hero.get('hand_chips', 0) if hero else 0
        hero_name   = hero.get('user_name', 'Hero') if hero else 'Hero'

        dealer_seatid = room.get('dealer_seatid')
        active_seats  = [p.get('seatid') for p in players if p.get('seatid') is not None]
        position = calc_position(dealer_seatid, hero_seatid, active_seats)

        pre_actions = flow.get('pre_flop', {}).get('actions', [])

        # Accumulate stats (save intermediates for per-tournament reuse)
        _h_vpip     = _is_vpip(pre_actions, hero_seatid)
        _h_pfr      = _is_pfr(pre_actions, hero_seatid)
        _h_br, _h_c = _postflop_af(flow, hero_seatid)
        _h_saw_flop = _hero_saw_flop(flow, hero_seatid)
        _h_at_sd    = False

        if _h_vpip: vpip += 1
        if _h_pfr:  pfr  += 1
        af_bets += _h_br; af_calls += _h_c

        if _h_saw_flop:
            wtsd_elig += 1
            if any(a.get('seatid') == hero_seatid
                   for a in flow.get('turn', {}).get('actions', [])):
                hero_saw_turn += 1
            if any(a.get('seatid') == hero_seatid
                   for a in flow.get('river', {}).get('actions', [])):
                hero_saw_river += 1
            if _hero_at_showdown(sd_res, hero_uid):
                _h_at_sd = True
                wtsd += 1
                if _hero_won_showdown(win_info, sd_res, hero_seatid, hero_uid):
                    wsd += 1

        net_profit += profit
        if profit > biggest_win:  biggest_win  = profit
        if profit < biggest_loss: biggest_loss = profit
        if big_blind > 0:
            bb_list.append(profit / big_blind)

        # Tournament tracking
        tid = extract_tourney_id(gameid)
        is_mtt = bool(room.get('mtt'))
        if tid not in tourney_map:
            tourney_map[tid] = dict(
                tourney_id=tid,
                room_name=room.get('room_name', ''),
                is_mtt=is_mtt,
                hands=0, net=0,
                first_chips=hero_chips,
                last_chips=hero_chips,
                # records are newest-first: first encountered = latest hand
                latest_ts=timestamp,
                earliest_ts=timestamp,
                # Per-tournament stat counters
                vpip_count=0, pfr_count=0,
                t_af_bets=0, t_af_calls=0,
                t_wtsd_elig=0, t_wtsd=0,
                t_biggest_win=0, t_biggest_loss=0,
                blind_min=small_blind, blind_max=small_blind,
            )
        t = tourney_map[tid]
        t['hands'] += 1
        t['net']   += profit
        t['last_chips'] = hero_chips
        if timestamp and timestamp < (t['earliest_ts'] or timestamp + 1):
            t['earliest_ts'] = timestamp
        if _h_vpip:     t['vpip_count'] += 1
        if _h_pfr:      t['pfr_count']  += 1
        t['t_af_bets']  += _h_br
        t['t_af_calls'] += _h_c
        if _h_saw_flop: t['t_wtsd_elig'] += 1
        if _h_at_sd:    t['t_wtsd']      += 1
        if profit > t['t_biggest_win']:  t['t_biggest_win']  = profit
        if profit < t['t_biggest_loss']: t['t_biggest_loss'] = profit
        if small_blind and small_blind < t['blind_min']: t['blind_min'] = small_blind
        if small_blind and small_blind > t['blind_max']: t['blind_max'] = small_blind

        # Build hand entry for the recent-hand lists
        if len(recent_hands) < 3 or (len(recent_won) < 3 and profit > 0):
            result = 'Won' if profit > 0 else ('Lost' if profit < 0 else 'Break even')
            replay_url = (
                f"https://replay.pppoker.net/new_game_record_publish/Frame/"
                f"rls_20260526/index.html?lan=en&sharekey={share_key}"
            ) if share_key else '#'

            entry = dict(
                hand_num=gameid,
                ts=timestamp,
                hole_cards=decode_cards(hole_codes),
                position=position,
                last_street=last_street_reached(flow, hero_seatid, sd_res, hero_uid),
                result=result,
                profit=profit,
                big_blind=big_blind,
                replay_url=replay_url,
            )

            if len(recent_hands) < 3:
                recent_hands.append(entry)

            if len(recent_won) < 3 and profit > 0:
                recent_won.append(entry)

    bb_100 = (sum(bb_list) / len(bb_list) * 100) if bb_list else 0.0
    af_val = round(af_bets / af_calls, 2) if af_calls else float(af_bets)

    stats = dict(
        total_hands=total,
        vpip_pct=round(vpip / total * 100, 1),
        pfr_pct=round(pfr / total * 100, 1),
        af=af_val,
        wtsd_pct=round(wtsd / wtsd_elig * 100, 1) if wtsd_elig else 0.0,
        wsd_pct=round(wsd / wtsd * 100, 1) if wtsd else 0.0,
        bb_100=round(bb_100, 2),
        net_profit=net_profit,
        biggest_win=biggest_win,
        biggest_loss=biggest_loss,
        hands_at_showdown=wtsd,
        hands_hero_saw_flop=wtsd_elig,
        hands_hero_saw_turn=hero_saw_turn,
        hands_hero_saw_river=hero_saw_river,
    )

    tourney_list = []
    for t in tourney_map.values():
        earliest = t.get('earliest_ts')
        latest   = t.get('latest_ts')
        duration = (latest - earliest) if (latest and earliest) else None
        h_count  = t['hands']
        tourney_list.append(dict(
            tourney_id=t['tourney_id'],
            room_name=t['room_name'],
            is_mtt=t['is_mtt'],
            hands=h_count,
            net=t['net'],
            first_chips=t['first_chips'],
            last_chips=t['last_chips'],
            finish_busted=t['last_chips'] == 0,
            time_played=_fmt_duration(duration),
            duration_secs=int(duration) if duration is not None else None,
            earliest_ts=earliest,
            blind_min=t.get('blind_min', 0),
            blind_max=t.get('blind_max', 0),
            vpip_pct=round(t['vpip_count'] / h_count * 100, 1) if h_count else 0.0,
            pfr_pct=round(t['pfr_count']  / h_count * 100, 1) if h_count else 0.0,
            af=round(t['t_af_bets'] / t['t_af_calls'], 2) if t['t_af_calls'] else float(t['t_af_bets']),
            wtsd_pct=round(t['t_wtsd'] / t['t_wtsd_elig'] * 100, 1) if t['t_wtsd_elig'] else 0.0,
            biggest_win=t['t_biggest_win'],
            biggest_loss=t['t_biggest_loss'],
        ))

    tournaments = sorted(tourney_list, key=lambda x: x['tourney_id'])
    return recent_hands, recent_won, stats, tournaments
