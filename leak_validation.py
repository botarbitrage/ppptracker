"""
leak_validation.py — Leak Finder validation harness (Phase 0).

Compares our leak_engine output against PokerTracker 4's exported report CSV
(the ground truth) for the same hands. Used by the /leaks/validate dev page
and runnable as a CLI:

    python leak_validation.py                 # run all fixture suites
    python leak_validation.py a.txt b.txt --csv report.csv   # ad-hoc suite

Fixture layout (data/validation/):
    <suite>/   *.txt + *.csv  → a validation suite: the txt(s) are exactly the
               hand histories the CSV report was generated from in PT4.
    corpus/    *.txt only     → parse-robustness corpus (no ground truth):
               every hand must parse, pot-validation stats are reported.
    reference/ *.csv only     → kept for reference, not currently pairable.

PT4 mirror: hands that fail pot/stack arithmetic (validate_pot) are excluded
from suite aggregation, because PT4 silently drops exactly those hands at
import — the ground-truth CSV never saw them. They are listed in the output.
"""

import csv
import glob
import io
import os

from leak_engine import (parse_ps_text, aggregate_positions, aggregate_stats,
                         validate_pot, POSITION_BUCKETS, PREFLOP_STATS)

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
    # Headers are stripped: some PT4 columns carry trailing spaces
    # ("Raise & 4Bet+ PF ", "Fold to PF 4Bet After 3Bet <30 ").
    header = [h.strip() for h in rows[0]]
    out = {}
    for row in rows[1:]:
        if not row or all(not c.strip() for c in row):
            continue
        pos = row[0].strip() or 'total'
        out[pos] = {header[i]: _num(row[i]) for i in range(1, min(len(header), len(row)))}
    return out


# ── Suite / corpus discovery ────────────────────────────────────────────────

def _discover():
    """(suites, corpus): suite dirs have both txt+csv; corpus dirs txt only."""
    suites, corpus = [], []
    if not os.path.isdir(VALIDATION_DIR):
        return suites, corpus
    for name in sorted(os.listdir(VALIDATION_DIR)):
        d = os.path.join(VALIDATION_DIR, name)
        if not os.path.isdir(d):
            continue
        txts = sorted(glob.glob(os.path.join(d, '*.txt')))
        csvs = sorted(glob.glob(os.path.join(d, '*.csv')))
        if txts and csvs:
            suites.append({'name': name, 'txts': txts, 'csv': csvs[0]})
        elif txts:
            corpus.append({'name': name, 'txts': txts})
    return suites, corpus


def _parse_files(txt_paths):
    hands = []
    for p in txt_paths:
        with io.open(p, encoding='utf-8') as fh:
            hands.extend(parse_ps_text(fh.read()))
    return hands


# ── Accepted deviations ─────────────────────────────────────────────────────
# Cells where PT4's output is internally inconsistent and our engine's answer
# is the poker-correct one. Keyed exactly (suite, stat, position, ours_n,
# pt4_n) so ANY change in either side re-triggers a real failure.
#
# Raise & 4Bet+ PF, deepfreeze_all4 MP: hand #…68510 — hero opened, the BB
# shoved all-in, everyone else already folded. A 4bet is impossible (call or
# fold only), yet PT4 grants a 4bet opportunity here while denying it in the
# structurally identical EP hand #…8466 in the same report. We follow the
# rules of poker; PT4's inconsistency is documented in the design doc.
ACCEPTED_DEVIATIONS = {
    ('deepfreeze_all4', 'Raise & 4Bet+ PF', 'MP', 5, 6),
    ('deepfreeze_all4', 'Raise & 4Bet+ PF', 'total', 7, 8),
}


# ── Diff ────────────────────────────────────────────────────────────────────

def _cells_hands(ours, pt4):
    """Per-position hand counts, ours vs PT4 (the Phase 0 gate)."""
    cells = []
    for pos in list(POSITION_BUCKETS) + ['total']:
        pt4_val = (pt4.get(pos) or {}).get('Hands')
        our_val = ours.get(pos)
        cells.append({
            'stat': 'Hands',
            'position': pos,
            'ours_pct': None, 'pt4_pct': None,
            'ours_n': our_val,
            'pt4_n': int(pt4_val) if pt4_val is not None else None,
            'match': (pt4_val is not None and our_val == int(pt4_val)),
        })
    return cells


def _pct(made, opp):
    return round(made / opp * 100, 2) if opp else None


def _pct_match(ours, pt4, ours_n=None, pt4_n=None):
    if ours_n == 0 and pt4_n == 0:
        # Empty cell: PT4 renders '-' on position rows but '0.00' on the
        # totals row — both mean "no opportunities", same as our None.
        return True
    if ours is None and pt4 is None:
        return True
    if ours is None or pt4 is None:
        return False
    return abs(ours - pt4) <= 0.01


def _cells_preflop(agg, pt4):
    """
    Phase 1 cells: every preflop stat vs the PT4 CSV.
    Grouped stats compare per position (+ total); 'global' stats (whose BBZ
    formula lost the position grouping — the CSV repeats one population value
    per row) compare population-wide on a single ALL row.
    """
    cells = []
    for key, label, is_global in PREFLOP_STATS:
        count_col = label + ' Count'
        if is_global:
            g = agg['global'][key]
            row = pt4.get('BTN') or {}
            pt4_pct, pt4_n = row.get(label), row.get(count_col)
            ours_pct = _pct(g['made'], g['opp'])
            cells.append({
                'stat': label, 'position': 'ALL',
                'ours_pct': ours_pct, 'pt4_pct': pt4_pct,
                'ours_n': g['opp'],
                'pt4_n': int(pt4_n) if pt4_n is not None else None,
                'match': (pt4_n is not None and g['opp'] == int(pt4_n)
                          and _pct_match(ours_pct, pt4_pct, g['opp'], int(pt4_n))),
            })
            continue
        for pos in list(POSITION_BUCKETS) + ['total']:
            if pos == 'total':
                made = sum(agg['positions'][b]['stats'][key]['made']
                           for b in POSITION_BUCKETS)
                opp = sum(agg['positions'][b]['stats'][key]['opp']
                          for b in POSITION_BUCKETS)
            else:
                made = agg['positions'][pos]['stats'][key]['made']
                opp = agg['positions'][pos]['stats'][key]['opp']
            row = pt4.get(pos) or {}
            pt4_pct, pt4_n = row.get(label), row.get(count_col)
            ours_pct = _pct(made, opp)
            cells.append({
                'stat': label, 'position': pos,
                'ours_pct': ours_pct, 'pt4_pct': pt4_pct,
                'ours_n': opp,
                'pt4_n': int(pt4_n) if pt4_n is not None else None,
                'match': (pt4_n is not None and opp == int(pt4_n)
                          and _pct_match(ours_pct, pt4_pct, opp, int(pt4_n))),
            })
    return cells


def _run_suite(name, txt_paths, csv_path):
    hands = _parse_files(txt_paths)

    # Mirror PT4: exclude hands whose pot/stack arithmetic fails (PT4 drops
    # them on import, so the ground-truth CSV was computed without them).
    accepted, rejected = [], []
    for h in hands:
        problems = validate_pot(h)
        if problems:
            rejected.append({'hand_id': h['hand_id'], 'problems': problems})
        else:
            accepted.append(h)

    ours = aggregate_positions(accepted)
    pt4 = parse_pt4_csv(csv_path)
    agg = aggregate_stats(accepted)
    cells = _cells_hands(ours, pt4) + _cells_preflop(agg, pt4)

    for c in cells:
        if not c['match'] and (name, c['stat'], c['position'],
                               c['ours_n'], c['pt4_n']) in ACCEPTED_DEVIATIONS:
            c['match'] = True
            c['accepted_deviation'] = True

    return {
        'name': name,
        'txt_files': [os.path.basename(p) for p in txt_paths],
        'csv_file': os.path.basename(csv_path),
        'hands_parsed': len(hands),
        'hands_rejected': rejected,
        'hero': next((h['hero'] for h in hands if h.get('hero')), None),
        'unmapped': ours.get('unmapped', 0),
        'cells': cells,
        'all_match': all(c['match'] for c in cells),
    }


def _run_corpus(name, txt_paths):
    hands = _parse_files(txt_paths)
    rejected = [{'hand_id': h['hand_id'], 'problems': validate_pot(h)}
                for h in hands if validate_pot(h)]
    ours = aggregate_positions(hands)
    return {
        'name': name,
        'txt_files': [os.path.basename(p) for p in txt_paths],
        'hands_parsed': len(hands),
        'hands_rejected': rejected,
        'unmapped': ours.get('unmapped', 0),
        'positions': {b: ours[b] for b in POSITION_BUCKETS},
    }


def run_validation(txt_paths=None, csv_path=None):
    """
    With explicit paths: run a single ad-hoc suite. Otherwise: discover and
    run every fixture suite + corpus under data/validation/.
    Returns a JSON-ready dict: {'suites': [...], 'corpus': [...]}.
    """
    if txt_paths and csv_path:
        return {'suites': [_run_suite('ad-hoc', txt_paths, csv_path)], 'corpus': []}

    suite_defs, corpus_defs = _discover()
    suites = [_run_suite(s['name'], s['txts'], s['csv']) for s in suite_defs]
    return {
        'suites': suites,
        'corpus': [_run_corpus(c['name'], c['txts']) for c in corpus_defs],
        'all_match': bool(suites) and all(s['all_match'] for s in suites),
    }


def run_all():
    """Alias kept for the Flask endpoint."""
    return run_validation()


# ── Raw-record action-type audit (see docs/pppoker-action-model.md) ────────

def audit_action_types(records):
    """
    Tally PPPoker action-type codes per street from raw JSON records (as
    downloaded via the app's JSON export endpoints). Used to settle action-code
    semantics empirically — in particular the type 12/13 question.
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

def _print_suite(s, only_mismatches=False):
    print(f"== suite {s['name']}  (txt: {', '.join(s['txt_files'])} · csv: {s['csv_file']})")
    print(f"   hero: {s['hero']} · parsed: {s['hands_parsed']} · "
          f"rejected (PT4 mirror): {len(s['hands_rejected'])} · unmapped: {s['unmapped']}")
    for r in s['hands_rejected']:
        print(f"   REJECTED {r['hand_id']}: {'; '.join(r['problems'])}")
    shown = [c for c in s['cells'] if not (only_mismatches and c['match'])]
    print(f"   {'stat':<34}{'pos':<7}{'ours%':>8}{'pt4%':>8}{'ours_n':>7}{'pt4_n':>7}   match")
    for c in shown:
        f = lambda v: '-' if v is None else v
        print(f"   {c['stat']:<34}{c['position']:<7}{f(c['ours_pct']):>8}"
              f"{f(c['pt4_pct']):>8}{f(c['ours_n']):>7}{f(c['pt4_n']):>7}   "
              f"{'OK' if c['match'] else 'X'}")
    n_bad = sum(1 for c in s['cells'] if not c['match'])
    total = len(s['cells'])
    print(f"   → {'ALL MATCH' if s['all_match'] else f'{n_bad}/{total} cells MISMATCH'}")


if __name__ == '__main__':
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description='Leak Finder validation harness')
    ap.add_argument('txts', nargs='*', help='hand-history txt fixtures (ad-hoc suite)')
    ap.add_argument('--csv', help='PT4 report CSV ground truth (ad-hoc suite)')
    ap.add_argument('--audit-actions', metavar='RECORDS_JSON',
                    help='tally action-type codes in a raw records JSON export')
    args = ap.parse_args()

    if args.audit_actions:
        with open(args.audit_actions, encoding='utf-8') as fh:
            print(_json.dumps(audit_action_types(_json.load(fh)), indent=2))
        raise SystemExit(0)

    if args.txts and args.csv:
        result = run_validation(args.txts, args.csv)
        result['all_match'] = all(s['all_match'] for s in result['suites'])
    else:
        result = run_all()

    for s in result['suites']:
        _print_suite(s)
    for c in result['corpus']:
        print(f"== corpus {c['name']}  (txt: {', '.join(c['txt_files'])})")
        print(f"   parsed: {c['hands_parsed']} · pot-invalid: {len(c['hands_rejected'])} · "
              f"unmapped: {c['unmapped']} · positions: {c['positions']}")
        for r in c['hands_rejected']:
            print(f"   POT-INVALID {r['hand_id']}: {'; '.join(r['problems'])}")
    raise SystemExit(0 if result['all_match'] else 1)
