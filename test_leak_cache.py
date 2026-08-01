"""
test_leak_cache.py — the per-tournament leak cache must be equivalent to
computing the report from scratch.

The cache stores one count-vector per tournament and sums the selected ones
at request time. That is only safe because every quantity in the vector is
additive: stats are (made, opportunity) integer pairs and the winrate is a
big-blind total, so summing vectors equals re-counting hands. This test
proves that on real fixture data, through the same JSON round-trip the
Firestore cache performs.

Counts must match EXACTLY (they are integers). Big-blind totals are compared
with a tolerance: float addition is not associative, so summing per-tournament
subtotals differs from one flat sum in the last bit or two (~1e-14 relative).
That is inherent to IEEE-754, not a bug, and sits ~12 orders of magnitude
below the two-decimal bb/100 the report displays.

    python test_leak_cache.py
"""

import glob
import io
import json
import sys

from leak_engine import (parse_ps_text, validate_pot, aggregate_stats,
                         hands_to_vector, merge_vectors, POSITION_BUCKETS,
                         ALL_STATS)

BB_TOLERANCE = 1e-9


def _hands(path):
    with io.open(path, encoding='utf-8') as fh:
        return [h for h in parse_ps_text(fh.read()) if not validate_pot(h)]


def main():
    files = sorted(glob.glob('data/validation/*/*.txt'))
    if not files:
        print('no fixtures found'); return 1

    per_tourney, all_hands = [], []
    for f in files:
        hs = _hands(f)
        if not hs:
            continue
        all_hands += hs
        vec = hands_to_vector(hs, scheme='report')
        # Exactly what the cache stores and reloads.
        per_tourney.append(json.loads(json.dumps(vec, separators=(',', ':'))))

    merged = merge_vectors(per_tourney)['positions']
    direct = aggregate_stats(all_hands, scheme='report', winrate=True)['positions']

    problems = []
    for bucket in POSITION_BUCKETS:
        m, d = merged[bucket], direct[bucket]
        if m['hands'] != d['hands']:
            problems.append(f'{bucket} hands {m["hands"]} != {d["hands"]}')
        for field in ('bb', 'bb_adj'):
            if abs(m[field] - d[field]) > BB_TOLERANCE:
                problems.append(f'{bucket} {field} {m[field]} != {d[field]}')
        for key, _label, _g in ALL_STATS:
            if m['stats'][key] != d['stats'][key]:
                problems.append(
                    f'{bucket} {key} {m["stats"][key]} != {d["stats"][key]}')

    size = len(json.dumps(per_tourney[0], separators=(',', ':')))
    print(f'{len(per_tourney)} tournament vectors · {len(all_hands)} hands · '
          f'{size} bytes/vector')
    for p in problems:
        print('  MISMATCH', p)
    print('cache equivalence: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
