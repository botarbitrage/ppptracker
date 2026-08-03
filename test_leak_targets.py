"""
test_leak_targets.py — unit checks for the effective-stack depth machinery and
the target-grid invariants. Standalone (no pytest):

    python test_leak_targets.py
"""

import sys

from leak_engine import effective_bb, stack_band, STACK_BAND_KEYS
import leak_validation as V


def _hand(hero='Hero', bb=100, stacks=None):
    return {'hero': hero, 'bb_amt': bb, 'stacks': stacks or {}}


def main():
    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    # ── effective_bb ─────────────────────────────────────────────────────────
    # min(hero, deepest opponent) / bb: min(3000, max(1200, 5000)) = 3000 -> 30
    check('eff_bb takes min(hero, deepest opp)',
          effective_bb(_hand(bb=100, stacks={'Hero': 3000, 'A': 1200, 'B': 5000})) == 30.0)
    # hero is the short stack -> hero/bb
    check('eff_bb bounded by hero stack',
          effective_bb(_hand(bb=100, stacks={'Hero': 800, 'A': 5000})) == 8.0)
    # no opponent with a stack -> None
    check('eff_bb None without opponents',
          effective_bb(_hand(bb=100, stacks={'Hero': 3000})) is None)
    # no hero / no bb -> None
    check('eff_bb None without hero',
          effective_bb(_hand(hero=None, bb=100, stacks={'A': 3000})) is None)
    check('eff_bb None without bb',
          effective_bb(_hand(bb=0, stacks={'Hero': 3000, 'A': 3000})) is None)
    # hero absent from stacks -> None (can't know hero's depth)
    check('eff_bb None when hero has no stack',
          effective_bb(_hand(bb=100, stacks={'A': 3000})) is None)

    # ── stack_band boundaries (half-open [lo, hi)) ───────────────────────────
    check('band <15', stack_band(14.9) == 'lt15')
    check('band 15 is 15_25 (closed low edge)', stack_band(15.0) == '15_25')
    check('band 24.9 is 15_25', stack_band(24.9) == '15_25')
    check('band 25 is 25_40', stack_band(25.0) == '25_40')
    check('band 40 is gte40', stack_band(40.0) == 'gte40')
    check('band 250 is gte40', stack_band(250.0) == 'gte40')
    check('band None passes through', stack_band(None) is None)
    check('every band key covered',
          set(stack_band(x) for x in (5, 20, 30, 100)) == set(STACK_BAND_KEYS))

    # ── target-grid invariants ───────────────────────────────────────────────
    inv = V.run_target_invariants()
    for c in inv['checks']:
        check('invariant: ' + c['name'], c['ok'], c['detail'])
    check('target invariants all_ok', inv['all_ok'])

    for p in problems:
        print('  FAIL', p)
    print('leak targets: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
