"""
Regression tests for hand_exporter pot / ante accounting.

Poker Tracker 4 rejects a hand ("Invalid pot size") when the pot it re-derives
from the action lines disagrees with the "Total pot" / "collected" amounts the
hand history declares.  Antes are part of that sum, so a pot that drifts by an
ante multiple reads as "the ante was dropped from the pot".

These tests export synthetic PPPoker records and assert, for every hand:

  A. declared "Total pot"  ==  pot independently re-derived from the emitted
     action lines (the exact check PT4 performs);
  B. the "collected from pot / side pot" amounts sum to the declared pot;
  C. (clean inputs only) the declared pot equals the pot implied by the input
     model — catching any wager the exporter drops or double-counts.

Run with:  python3 test_hand_exporter.py   (exits non-zero on any failure)
"""
import random
import re
import sys

from hand_exporter import hand_to_ps_block, export_pokerstars


# ── Independent PT4-style pot re-derivation (does NOT call the exporter's own
#    helper — it re-parses the emitted text from scratch) ─────────────────────

def pt4_pot(block):
    """Sum wagers exactly as a PokerStars-format parser (PT4) does."""
    total = 0
    street_bet = {}
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith('*** SHOW DOWN') or line.startswith('*** SUMMARY'):
            break
        if re.match(r'^\*\*\* (FLOP|TURN|RIVER) \*\*\*', line):
            street_bet = {}
            continue
        m = re.match(r'^(.+?): posts the ante (\d+)$', line)
        if m:
            total += int(m.group(2)); continue
        m = re.match(r'^(.+?): posts (?:small|big) blind (\d+)$', line)
        if m:
            p, a = m.group(1), int(m.group(2))
            street_bet[p] = street_bet.get(p, 0) + a; total += a; continue
        m = re.match(r'^(.+?): (?:bets|calls) (\d+)', line)
        if m:
            p, a = m.group(1), int(m.group(2))
            street_bet[p] = street_bet.get(p, 0) + a; total += a; continue
        m = re.match(r'^(.+?): raises \d+ to (\d+)', line)
        if m:
            p, to = m.group(1), int(m.group(2))
            total += to - street_bet.get(p, 0); street_bet[p] = to; continue
        m = re.match(r'^Uncalled bet \((\d+)\) returned to ', line)
        if m:
            total -= int(m.group(1)); continue
    return total


def declared_pot(block):
    return int(re.search(r'^Total pot (\d+)', block, re.M).group(1))


def collected_sum(block):
    """Sum of the 'collected N from ...' distribution lines."""
    return sum(int(x) for x in re.findall(r'collected (\d+) from ', block))


# ── Record builder ───────────────────────────────────────────────────────────

def make_record(players, pre_actions, *, sb, ante, gameid='G-777-1',
                winners=None, chips_back=None, dealer=0, room_name='TEST'):
    """players: list of (seatid, name, hand_chips_raw)."""
    flow = {'pre_flop': {'actions': pre_actions}}
    if chips_back:
        flow['pre_flop']['chips_back'] = chips_back
    flow['winning_info'] = winners or []
    return {
        'summary': {'D': gameid, 'C': 1719800000, 'G': sb, 'A': ante, 'H': 0},
        'share_key': 'x',
        'full_hand': {
            'info': {
                'room': {'small_blind': sb, 'ante': ante, 'dealer_seatid': dealer,
                         'mtt': {'table_num': '1'}, 'room_name': room_name},
                'players': [{'seatid': s, 'user_name': n, 'hand_chips': hc,
                             'uid': f'u{s}', 'isSelf': s == 0}
                            for s, n, hc in players],
                'cards': [258, 259],
            },
            'flow': flow,
        },
    }


FAILURES = []


def assert_consistent(label, rec, expected_pot=None):
    block, _, _ = hand_to_ps_block(rec)
    if block is None:
        FAILURES.append(f"{label}: hand unexpectedly skipped")
        return
    dec, pt4, coll = declared_pot(block), pt4_pot(block), collected_sum(block)
    if dec != pt4:
        FAILURES.append(f"{label}: declared {dec} != PT4 line-sum {pt4}\n{block}")
    if coll != dec:
        FAILURES.append(f"{label}: collected {coll} != declared {dec}\n{block}")
    if expected_pot is not None and dec != expected_pot:
        FAILURES.append(f"{label}: declared {dec} != true pot {expected_pot}\n{block}")
    ok = (dec == pt4 and coll == dec and
          (expected_pot is None or dec == expected_pot))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: pot={dec}")


# ── Named scenarios ──────────────────────────────────────────────────────────

def scenario_all_ante_walk():
    """6 players each ante; SB/BB post; folds to BB (walk, uncalled SB→BB diff)."""
    sb, bb, ante = 25000, 50000, 10000
    players = [(i, f'P{i}', 4000000) for i in range(6)]
    pre = [{'seatid': i, 'type': 10, 'chips': ante} for i in range(6)]
    pre += [{'seatid': 1, 'type': 8, 'chips': sb}, {'seatid': 2, 'type': 9, 'chips': bb}]
    pre += [{'seatid': i, 'type': 1, 'chips': 0} for i in (3, 4, 5, 0, 1)]
    # antes 6*100=600, SB 250, BB 500, uncalled 250 -> pot 1100
    rec = make_record(players, pre, sb=sb, ante=ante,
                      winners=[{'seatid': 2, 'chips': 100000, 'poolid': 0}])
    assert_consistent('all-ante walk-to-BB', rec, expected_pot=1100)


def scenario_bb_ante_raise_call():
    """Single big-blind-style ante; raise then call."""
    sb, bb, ante = 25000, 50000, 50000
    players = [(i, f'P{i}', 4000000) for i in range(6)]
    pre = [{'seatid': 2, 'type': 10, 'chips': ante}]        # one ante (= 1 BB)
    pre += [{'seatid': 1, 'type': 8, 'chips': sb}, {'seatid': 2, 'type': 9, 'chips': bb}]
    pre += [{'seatid': 3, 'type': 4, 'chips': 150000}]      # raise to 1500
    pre += [{'seatid': i, 'type': 1, 'chips': 0} for i in (4, 5, 0, 1)]
    pre += [{'seatid': 2, 'type': 3, 'chips': 100000}]      # BB calls 1000 more
    # ante 500 + SB 250(folded, stays) + raiser 1500 + BB(500 blind + 1000 call)=1500
    # = 500 + 250 + 1500 + 1500 = 3750
    rec = make_record(players, pre, sb=sb, ante=ante,
                      winners=[{'seatid': 3, 'chips': 100000, 'poolid': 0}])
    assert_consistent('bb-ante raise+call', rec, expected_pot=3750)


def scenario_shove_uncalled(with_chips_back):
    """Antes; UTG shoves 40BB; everyone folds; large uncalled remainder."""
    sb, bb, ante = 25000, 50000, 10000
    players = [(i, f'P{i}', 4000000) for i in range(6)]
    pre = [{'seatid': i, 'type': 10, 'chips': ante} for i in range(6)]
    pre += [{'seatid': 1, 'type': 8, 'chips': sb}, {'seatid': 2, 'type': 9, 'chips': bb}]
    pre += [{'seatid': 3, 'type': 4, 'chips': 4000000}]     # shove to 40000
    pre += [{'seatid': i, 'type': 1, 'chips': 0} for i in (4, 5, 0, 1, 2)]
    cb = [{'seatid': 3, 'chips': 3950000}] if with_chips_back else None
    # antes 600 + SB 250 + BB 500 + shove 40000 - uncalled(40000-500=39500) = 1850
    rec = make_record(players, pre, sb=sb, ante=ante, chips_back=cb,
                      winners=[{'seatid': 3, 'chips': 100000, 'poolid': 0}])
    tag = 'data chips_back' if with_chips_back else 'synthesized'
    assert_consistent(f'antes shove+uncalled ({tag})', rec, expected_pot=1850)


def scenario_side_pot():
    """Antes; short all-in for less, two bigger stacks continue -> side pot."""
    sb, bb, ante = 25000, 50000, 10000
    players = [(0, 'P0', 4000000), (1, 'P1', 4000000),
               (2, 'P2', 800000), (3, 'P3', 4000000)]       # P2 short (8000)
    pre = [{'seatid': i, 'type': 10, 'chips': ante} for i in range(4)]
    pre += [{'seatid': 1, 'type': 8, 'chips': sb}, {'seatid': 2, 'type': 9, 'chips': bb}]
    pre += [{'seatid': 3, 'type': 4, 'chips': 2000000}]     # P3 raises to 20000
    pre += [{'seatid': 0, 'type': 3, 'chips': 2000000}]     # P0 calls 20000
    pre += [{'seatid': 1, 'type': 1, 'chips': 0}]           # SB folds
    pre += [{'seatid': 2, 'type': 3, 'chips': 750000}]      # BB all-in for 7500 more (8000 total)
    rec = make_record(players, pre, sb=sb, ante=ante,
                      winners=[{'seatid': 3, 'chips': 240000, 'poolid': 0},
                               {'seatid': 3, 'chips': 250000, 'poolid': 1}])
    assert_consistent('antes side-pot all-in-for-less', rec)  # A+B only


def scenario_split_pot_rounding():
    """Two winners split an odd pot -> exercises exact-sum distribution."""
    sb, bb, ante = 25000, 50000, 10000
    players = [(i, f'P{i}', 4000000) for i in range(3)]
    pre = [{'seatid': i, 'type': 10, 'chips': ante} for i in range(3)]
    pre += [{'seatid': 1, 'type': 8, 'chips': sb}, {'seatid': 2, 'type': 9, 'chips': bb}]
    pre += [{'seatid': 0, 'type': 3, 'chips': 50000}]       # P0 calls
    pre += [{'seatid': 1, 'type': 3, 'chips': 25000}]       # SB completes
    pre += [{'seatid': 2, 'type': 2, 'chips': 0}]           # BB checks
    # pot = antes 300 + three players 500 each = 1800 (even here); make it odd
    # by an extra single-chip ante on one player:
    pre.append({'seatid': 0, 'type': 10, 'chips': 100})     # +1 disp -> pot 1801
    rec = make_record(players, pre, sb=sb, ante=ante,
                      winners=[{'seatid': 0, 'chips': 100, 'poolid': 0},
                               {'seatid': 1, 'chips': 100, 'poolid': 0}])
    assert_consistent('split-pot odd rounding', rec)  # A+B; distribution must sum


def scenario_no_ante():
    sb, bb = 25000, 50000
    players = [(i, f'P{i}', 4000000) for i in range(3)]
    pre = [{'seatid': 1, 'type': 8, 'chips': sb}, {'seatid': 2, 'type': 9, 'chips': bb}]
    pre += [{'seatid': 0, 'type': 3, 'chips': 50000}, {'seatid': 1, 'type': 3, 'chips': 25000},
            {'seatid': 2, 'type': 2, 'chips': 0}]
    # 3 players * 500 = 1500
    rec = make_record(players, pre, sb=sb, ante=0,
                      winners=[{'seatid': 0, 'chips': 100, 'poolid': 0}])
    assert_consistent('no-ante limped pot', rec, expected_pot=1500)


# ── Clean-input fuzz: model the true pot and assert the export matches ────────

def fuzz(n=20000, seed=99):
    rng = random.Random(seed)
    fails = 0
    for _ in range(n):
        nseats = rng.randint(3, 7)
        sb_disp = rng.choice([100, 250, 500, 1000, 2500])
        sb, bb = sb_disp * 100, sb_disp * 200
        ante_disp = rng.choice([0, sb_disp // 5 or 1, sb_disp, bb // 100])
        ante = ante_disp * 100
        stack = 500 * bb                       # equal, deep -> no all-in caps
        players = [(i, f'P{i}', stack) for i in range(nseats)]
        pre, committed, betlevel = [], {i: 0 for i in range(nseats)}, {i: 0 for i in range(nseats)}
        if ante:
            posters = ([rng.randrange(nseats)] if rng.random() < 0.4 else list(range(nseats)))
            ante_amt = bb if len(posters) == 1 else ante   # single poster = BB-ante
            for i in posters:
                pre.append({'seatid': i, 'type': 10, 'chips': ante_amt})
                committed[i] += ante_amt
        sbs, bbs = 1 % nseats, 2 % nseats
        pre.append({'seatid': sbs, 'type': 8, 'chips': sb}); committed[sbs] += sb; betlevel[sbs] = sb
        pre.append({'seatid': bbs, 'type': 9, 'chips': bb}); committed[bbs] += bb; betlevel[bbs] = bb
        current = bb
        alive = set(range(nseats))
        for i in [x for x in range(nseats) if x not in (sbs, bbs)] + [sbs, bbs]:
            if i not in alive:
                continue
            r = rng.random()
            if r < 0.45:
                pre.append({'seatid': i, 'type': 1, 'chips': 0}); alive.discard(i)
            elif r < 0.75:
                need = current - betlevel[i]
                if need <= 0:
                    continue
                pre.append({'seatid': i, 'type': 3, 'chips': need})
                committed[i] += need; betlevel[i] = current
            else:
                to = current + rng.randint(1, 4) * bb       # cumulative raise-to (< stack)
                pre.append({'seatid': i, 'type': 4, 'chips': to})
                committed[i] += to - betlevel[i]; betlevel[i] = to; current = to
        # True pot: everything committed stays, minus the unique top better's
        # uncalled excess over the second-highest bet level.
        levels = sorted(betlevel.values(), reverse=True)
        top = levels[0]
        second = levels[1] if len(levels) > 1 else 0
        top_holders = sum(1 for v in betlevel.values() if v == top)
        uncalled = (top - second) if top_holders == 1 else 0
        # Exporter reports in display chips (raw / _CHIP_DIV=100); every wager
        # here is a multiple of 100, so the scaled pot is exact.
        true_pot = (sum(committed.values()) - uncalled) // 100
        winner = next(iter(alive)) if alive else 0
        rec = make_record(players, pre, sb=sb, ante=ante,
                          winners=[{'seatid': winner, 'chips': 100000, 'poolid': 0}])
        block, _, _ = hand_to_ps_block(rec)
        if block is None:
            fails += 1; continue
        dec, pt4, coll = declared_pot(block), pt4_pot(block), collected_sum(block)
        if not (dec == pt4 == true_pot and coll == dec):
            fails += 1
            if fails <= 3:
                print(f"  FUZZ FAIL dec={dec} pt4={pt4} true={true_pot} coll={coll}\n{block}\n")
    if fails:
        FAILURES.append(f"fuzz: {fails}/{n} hands inconsistent")
    print(f"  [{'PASS' if not fails else 'FAIL'}] clean-input fuzz: {n - fails}/{n} consistent")


def main():
    print("Named scenarios:")
    scenario_all_ante_walk()
    scenario_bb_ante_raise_call()
    scenario_shove_uncalled(with_chips_back=True)
    scenario_shove_uncalled(with_chips_back=False)
    scenario_side_pot()
    scenario_split_pot_rounding()
    scenario_no_ante()
    print("Fuzz:")
    fuzz()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print('-' * 70); print(f)
        sys.exit(1)
    print("\nAll pot/ante consistency checks passed.")


if __name__ == '__main__':
    main()
