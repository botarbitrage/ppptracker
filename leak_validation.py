"""
leak_validation.py — Leak Finder validation harness (Phase 0).

Compares our leak_engine output against PokerTracker 4's exported report CSV
(the ground truth) for the same hands. Used by the /leaks/validate dev page
and runnable as a CLI:

    python leak_validation.py data/validation/*.txt \
        --csv data/validation/ReportExport_deepfreeze.csv

The PT4 CSV layout: one row per position (+ a totals row with an empty
Position), columns = Hands, All-In Adj BB/100, ~39 percentage stats, then a
"<stat> Count" column per stat. Values use '-' for no-sample cells.
"""

import csv
import glob
import io
import os

from leak_engine import parse_ps_text, aggregate_positions, POSITION_BUCKETS

VALIDATION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'data', 'validation')


# ── PT4 CSV ground truth ────────────────────────────────────────────────────

def _num(cell):
    """PT4 CSV cell → float or None ('-' and '' are no-sample)."""
    cell = (cell or '').strip().replace(',', '')
    if cell in ('', '-'):
        return None
    try:
        return float(cell)
    except ValueError:
        return None


def parse_pt4_csv(path):
    """
    {position: {stat: value}} from a PT4 report export. The totals row (empty
    Position cell) is keyed 'total'. Stats keep their CSV header names;
    percentage stats and 'Hands' / count columns are all included.
    """
    with open(path, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return {}
    header = rows[0]
    out = {}
    for row in rows[1:]:
        if not row or all(not c.strip() for c in row):
            continue
        pos = row[0].strip() or 'total'
        out[pos] = {header[i]: _num(row[i]) for i in range(1, min(len(header), len(row)))}
    return out


# ── Diff ────────────────────────────────────────────────────────────────────

def _cells_hands(ours, pt4):
    """Phase 0 cells: per-position hand counts, ours vs PT4. Includes each
    side's share of its own total so partial fixture coverage (fewer txt files
    than the CSV was built from) still gives a meaningful comparison."""
    our_total = ours.get('total') or 0
    pt4_total = (pt4.get('total') or {}).get('Hands') or 0
    cells = []
    for pos in list(POSITION_BUCKETS) + ['total']:
        pt4_val = (pt4.get(pos) or {}).get('Hands')
        our_val = ours.get(pos)
        cells.append({
            'stat': 'Hands',
            'position': pos,
            'ours': our_val,
            'pt4': pt4_val,
            'ours_share': round(our_val / our_total * 100, 1) if our_total else None,
            'pt4_share': (round(pt4_val / pt4_total * 100, 1)
                          if (pt4_val is not None and pt4_total) else None),
            'match': (pt4_val is not None and our_val == int(pt4_val)),
        })
    return cells


def run_validation(txt_paths=None, csv_path=None):
    """
    Parse the fixture txt file(s), aggregate, and diff against the PT4 CSV.
    Defaults to everything under data/validation/. Returns a JSON-ready dict.
    """
    if txt_paths is None:
        txt_paths = sorted(glob.glob(os.path.join(VALIDATION_DIR, '*.txt')))
    if csv_path is None:
        candidates = sorted(glob.glob(os.path.join(VALIDATION_DIR, '*.csv')))
        # Prefer the deepfreeze-scoped export — it matches the fixture txts.
        csv_path = next((c for c in candidates if 'deepfreeze' in c.lower()),
                        candidates[0] if candidates else None)

    hands = []
    for p in txt_paths:
        with io.open(p, encoding='utf-8') as fh:
            hands.extend(parse_ps_text(fh.read()))

    ours = aggregate_positions(hands)
    pt4 = parse_pt4_csv(csv_path) if csv_path else {}
    cells = _cells_hands(ours, pt4)

    pt4_total = (pt4.get('total') or {}).get('Hands')
    coverage_note = None
    if pt4_total is not None and ours['total'] != int(pt4_total):
        missing = int(pt4_total) - ours['total']
        coverage_note = (
            f"Fixture txt files cover {ours['total']} hands but the PT4 CSV was "
            f"built from {int(pt4_total)} — {missing} hands' worth of exports "
            f"are missing from data/validation/. Add the remaining tournament "
            f"export(s) for a full-match gate."
        )

    return {
        'txt_files': [os.path.basename(p) for p in txt_paths],
        'csv_file': os.path.basename(csv_path) if csv_path else None,
        'hands_parsed': len(hands),
        'hero': next((h['hero'] for h in hands if h.get('hero')), None),
        'unmapped': ours.get('unmapped', 0),
        'coverage_note': coverage_note,
        'cells': cells,
        'all_match': all(c['match'] for c in cells),
    }


# ── Raw-record action-type audit (see docs/pppoker-action-model.md) ────────

def audit_action_types(records):
    """
    Tally PPPoker action-type codes per street from raw JSON records (as
    downloaded via the app's JSON export endpoints). Used to settle action-code
    semantics empirically — in particular the type 12/13 question.
    For each (street, type): occurrences, how many carried chips, and how many
    were by players whose hand ended before showdown.
    """
    tally = {}
    for rec in records or []:
        flow = (rec.get('full_hand') or {}).get('flow') or {}
        for street in ('pre_flop', 'flop', 'turn', 'river'):
            for a in (flow.get(street) or {}).get('actions', []):
                t = a.get('type')
                key = (street, t)
                d = tally.setdefault(key, {'n': 0, 'with_chips': 0, 'zero_chips': 0})
                d['n'] += 1
                if a.get('chips'):
                    d['with_chips'] += 1
                else:
                    d['zero_chips'] += 1
    return {f'{s}/type{t}': v for (s, t), v in sorted(tally.items(),
            key=lambda kv: (kv[0][0], kv[0][1] if kv[0][1] is not None else -1))}


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description='Leak Finder validation harness')
    ap.add_argument('txts', nargs='*', help='hand-history txt fixtures')
    ap.add_argument('--csv', help='PT4 report CSV (ground truth)')
    ap.add_argument('--audit-actions', metavar='RECORDS_JSON',
                    help='tally action-type codes in a raw records JSON export')
    args = ap.parse_args()

    if args.audit_actions:
        with open(args.audit_actions, encoding='utf-8') as fh:
            print(_json.dumps(audit_action_types(_json.load(fh)), indent=2))
        raise SystemExit(0)

    result = run_validation(args.txts or None, args.csv)
    print(f"txt: {', '.join(result['txt_files'])}")
    print(f"csv: {result['csv_file']}   hero: {result['hero']}   "
          f"hands parsed: {result['hands_parsed']}   unmapped: {result['unmapped']}")
    if result['coverage_note']:
        print(f"NOTE: {result['coverage_note']}")
    print(f"{'stat':<8}{'pos':<7}{'ours':>6}{'pt4':>6}   match")
    for c in result['cells']:
        pt4v = '-' if c['pt4'] is None else int(c['pt4'])
        print(f"{c['stat']:<8}{c['position']:<7}{c['ours']:>6}{pt4v:>6}   "
              f"{'OK' if c['match'] else 'X'}")
    raise SystemExit(0 if result['all_match'] else 1)
