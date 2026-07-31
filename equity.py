"""
equity.py — all-in equity adjustment for the Leak Finder's headline winrate.

PokerTracker's "All-In Adj BB/100" replaces the realised result of an all-in
pot with the equity the player held at the moment the money went in, which
strips the variance that dominates a small tournament sample. The `.pt4rpt`
states the rule precisely:

    "…instead of attributing real results, this statistic attributes to the
     player the equity they had in the pot at the time of the all in. Pots
     where players call the all-in bet or raise but later fold (hence their
     cards are not known) are not adjusted."

So a hand is adjusted only when it went all-in AND reached showdown with every
contesting player's cards known; otherwise the realised result stands.

Enumeration is exact (never Monte Carlo) — the validation gate compares against
PT4 to two decimals, which sampling error cannot hold. Cost is dominated by
preflop all-ins (C(48,5) = 1.7M runouts, ~2s); flop/turn/river all-ins are
cheap. Results are memoised on the canonical card layout, and `hero_net` is
pure arithmetic, so hands that never went all-in cost nothing.
"""

import itertools
import json
import os
import time

try:
    import eval7
    _HAVE_EVAL7 = True
except ImportError:                                    # pragma: no cover
    _HAVE_EVAL7 = False

_STREETS = ('preflop', 'flop', 'turn', 'river')
# Board cards face-up once each street's betting is done.
_BOARD_AT = {'preflop': 0, 'flop': 3, 'turn': 4, 'river': 5}

_equity_cache = {}
_cache_dirty = set()

# Enumerating a preflop all-in costs ~2s, so an un-warmed cache could otherwise
# stall a web request past its timeout. A budget caps how long one report may
# spend computing NEW equities; cached entries are always free. Hands left
# uncomputed simply fall back to their realised result and are counted, so the
# caller can disclose partial coverage rather than hang or silently mislead.
_deadline = None
_skipped = 0


def set_budget(seconds):
    """Allow `seconds` of new equity computation from now (None = unlimited)."""
    global _deadline, _skipped
    _deadline = None if seconds is None else time.monotonic() + seconds
    _skipped = 0


def skipped_count():
    """Hands that fell back to realised because the budget ran out."""
    return _skipped


def _budget_left():
    return _deadline is None or time.monotonic() < _deadline

_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'data', 'equity_cache.json')


def load_cache(path=_CACHE_PATH):
    """Warm the in-memory cache from disk. Exact runout enumeration is costly
    (a preflop all-in is C(48,5) = 1.7M boards, ~2s), so results are persisted:
    they depend only on the card layout and can never change."""
    try:
        with open(path, encoding='utf-8') as fh:
            for k, v in json.load(fh).items():
                _equity_cache.setdefault(k, [{n: f for n, f in d.items()} for d in v])
    except (OSError, ValueError):
        pass


def save_cache(path=_CACHE_PATH):
    """Persist newly computed entries. Returns the number of entries written."""
    if not _cache_dirty:
        return 0
    merged = {}
    try:
        with open(path, encoding='utf-8') as fh:
            merged = json.load(fh)
    except (OSError, ValueError):
        pass
    for k in _cache_dirty:
        merged[k] = _equity_cache[k]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(merged, fh, separators=(',', ':'), sort_keys=True)
    n = len(_cache_dirty)
    _cache_dirty.clear()
    return n


# ── Money ───────────────────────────────────────────────────────────────────

def contributions(hand):
    """{name: chips left in the pot} — everything wagered less uncalled returns."""
    total = {}

    def add(nm, amt):
        total[nm] = total.get(nm, 0) + amt

    for p in hand['posts']:
        if p['kind'] == 'ante':
            add(p['name'], p['amount'])
    for street in _STREETS:
        commit = {}
        if street == 'preflop':
            for p in hand['posts']:
                if p['kind'] in ('sb', 'bb'):
                    commit[p['name']] = commit.get(p['name'], 0) + p['amount']
        for a in hand['streets'][street]:
            nm, verb, amt = a['name'], a['verb'], a['amount']
            if verb in ('call', 'bet'):
                commit[nm] = commit.get(nm, 0) + amt
            elif verb == 'raise':
                commit[nm] = amt          # raise amounts are raise-TO totals
        for nm, amt in commit.items():
            add(nm, amt)
    for u in hand['uncalled']:
        add(u['name'], -u['amount'])
    return total


def hero_net(hand):
    """Hero's realised net chips for the hand (negative when they lost)."""
    hero = hand.get('hero')
    if not hero:
        return 0
    won = sum(c['amount'] for c in hand['collected'] if c['name'] == hero)
    return won - contributions(hand).get(hero, 0)


# ── Equity ──────────────────────────────────────────────────────────────────

def _parse_cards(text):
    """'Ks 4s' → [eval7.Card, …] (None if any card is unknown)."""
    if not text:
        return None
    out = []
    for tok in text.split():
        if len(tok) != 2 or '?' in tok:
            return None
        try:
            out.append(eval7.Card(tok))
        except Exception:
            return None
    return out or None


def _cache_key(hands_by_player, board, subsets):
    """Stable string key — card layout only, never chip amounts, so entries
    are reusable across hands and safe to persist."""
    names = sorted(hands_by_player)
    idx = {n: i for i, n in enumerate(names)}
    hands = '|'.join(''.join(sorted(str(c) for c in hands_by_player[n]))
                     for n in names)
    subs = '|'.join(','.join(str(idx[n]) for n in sorted(s)) for s in subsets)
    return f"{hands}/{''.join(sorted(str(c) for c in board))}/{subs}"


def _layer_fractions(hands_by_player, board, subsets):
    """
    For each eligible-player subset, each player's expected FRACTION of a pot
    contested by that subset — one runout enumeration settles them all.
    Fractions (not chip amounts) keep cache entries independent of pot size.
    """
    global _skipped
    names = sorted(hands_by_player)
    key = _cache_key(hands_by_player, board, subsets)
    hit = _equity_cache.get(key)
    if hit is not None:
        return hit
    if not _budget_left():
        _skipped += 1
        return None

    ev = eval7.evaluate
    dead = {str(c) for c in board}
    for cards in hands_by_player.values():
        dead |= {str(c) for c in cards}
    avail = [eval7.Card(r + s) for r in '23456789TJQKA' for s in 'cdhs'
             if (r + s) not in dead]

    need = 5 - len(board)
    slots = [2 + len(board) + i for i in range(need)]
    bufs = {n: list(hands_by_player[n]) + list(board) + [None] * need
            for n in names}

    totals = [{n: 0.0 for n in names} for _ in subsets]
    boards = 0
    for combo in itertools.combinations(avail, need):
        for n in names:
            buf = bufs[n]
            for slot, card in zip(slots, combo):
                buf[slot] = card
        vals = {n: ev(bufs[n]) for n in names}
        for i, eligible in enumerate(subsets):
            if not eligible:
                continue
            best = max(vals[n] for n in eligible)
            winners = [n for n in eligible if vals[n] == best]
            share = 1.0 / len(winners)
            for n in winners:
                totals[i][n] += share
        boards += 1

    result = [{n: (t[n] / boards if boards else 0.0) for n in names}
              for t in totals]
    _equity_cache[key] = result
    _cache_dirty.add(key)
    return result


def _side_pot_layers(contrib, live):
    """
    Split the pot into main + side pots.
    Chips from players who folded are dead money in the lowest layer(s);
    a layer is contested only by live players who matched that level.
    """
    levels = sorted({contrib[n] for n in live if contrib.get(n)})
    layers, prev = [], 0
    for lvl in levels:
        amt = sum(min(c, lvl) - min(c, prev) for c in contrib.values())
        eligible = [n for n in live if contrib.get(n, 0) >= lvl]
        layers.append((amt, eligible))
        prev = lvl
    # Anything wagered above the highest live level cannot be contested; it
    # belongs to whoever put it in (an uncalled remainder).
    return layers


def _allin_street(hand):
    """The last street that saw betting action — where the money went in."""
    last = 'preflop'
    for street in _STREETS:
        if hand['streets'][street]:
            last = street
    return last


def hero_expected_net(hand):
    """
    Hero's all-in-adjusted net chips, or None when the hand does not qualify
    (no all-in, hero not at showdown, or a contesting player's cards unknown)
    and the realised result should be used instead.
    """
    hero = hand.get('hero')
    if not _HAVE_EVAL7 or not hero:
        return None

    # Somebody has to have been all-in.
    if not any(a.get('allin') for s in _STREETS for a in hand['streets'][s]):
        return None

    # Hero must still be contesting the pot at the end.
    folded = {a['name'] for s in _STREETS for a in hand['streets'][s]
              if a['verb'] == 'fold'}
    if hero in folded:
        return None
    live = [nm for nm in hand['seats'].values() if nm not in folded]
    if len(live) < 2:
        return None

    street = _allin_street(hand)
    board = (hand.get('board') or [])[:_BOARD_AT[street]]
    if len(board) == 5:
        return None                      # nothing left to run out

    hero_cards = _parse_cards(hand.get('hero_cards'))
    if not hero_cards or len(hero_cards) != 2:
        return None
    hands_by_player = {hero: hero_cards}
    for nm in live:
        if nm == hero:
            continue
        cards = _parse_cards(hand['shows'].get(nm))
        if not cards or len(cards) != 2:
            return None                  # unknown cards → PT4 leaves it alone
        hands_by_player[nm] = cards
    if len(hands_by_player) < 2:
        return None

    board_cards = _parse_cards(' '.join(board))
    if board_cards is None and board:
        return None

    contrib = contributions(hand)
    layers = _side_pot_layers(contrib, list(hands_by_player))
    fractions = _layer_fractions(hands_by_player, board_cards or [],
                                 [tuple(e) for _amt, e in layers])
    if fractions is None:
        return None                      # budget exhausted → realised result
    won = sum(amt * frac.get(hero, 0.0)
              for (amt, _e), frac in zip(layers, fractions))
    # Chips wagered beyond what any live opponent could match come straight
    # back to whoever put them in.
    contested = max((contrib.get(n, 0) for n in hands_by_player if n != hero),
                    default=0)
    won += max(0, contrib.get(hero, 0) - contested)
    return won - contrib.get(hero, 0)


def hero_result_bb(hand):
    """
    (realised_bb, adjusted_bb) for one hand — both in big blinds, so they can
    be summed across levels. adjusted_bb falls back to the realised result
    whenever the hand does not qualify for adjustment.
    """
    bb = hand.get('bb_amt') or 0
    if not bb:
        return 0.0, 0.0
    realised = hero_net(hand) / bb
    expected = hero_expected_net(hand)
    return realised, (realised if expected is None else expected / bb)


load_cache()
