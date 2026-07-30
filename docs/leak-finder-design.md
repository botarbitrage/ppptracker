# Leak Finder — Design & Delivery Plan

_Status: proposed. No code beyond this document yet._
_Owner: Caio · Consulting engineer: Claude_

## 1. Goal

Collapse the current three-tool pipeline

```
PPPokerHA (export .txt) → PokerTracker 4 (compute report → .csv) → BBZ Leak Finder (visual report)
```

into a single feature **inside PPPokerHA**: import hands → click **Leak Report** → see a
by-position / by-action stat table with good/bad verdicts, equivalent to the BBZ screenshots.

We already own the raw material for every column. This document specifies the engine, how it
slots into the existing app, and a phased plan where **every phase ships something visible in the
UI** so we can validate and feel progress.

### Decisions locked (2026-07)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Verdict ranges | **Use BBZ's ranges/targets** (populate a config table from BBZ — see §7) |
| 2 | All-in adjusted BB/100 | **Include in v1** — it's the headline "Winrate" number and matters most at small samples |
| 3 | Scope | **Hero-only** — no villain/HUD database |
| 4 | Postflop breakdown | **True per-position splits** (better than the source report, which repeats a global value across all six rows) |
| 5 | UI | **Reuse the existing app** (dark theme, `section-card`, `table-dark`, Chart.js). New surface only where required. |

## 2. Why this is a bounded, verifiable task (not a research gamble)

Three facts de-risk the whole build:

1. **PT4 had no input we don't have.** PT4's report is a pure function of the `.txt` hand
   histories, and those `.txt` files are produced by our own `hand_exporter.py` from the `flow`
   JSON already in Firestore. Every column PT4 computed is therefore derivable from data we
   already hold — there is no hidden signal.
2. **We have the formula.** The `.pt4rpt` report definition gives the exact ratio for all ~40
   stats plus the underlying opportunity/action flags (see Appendix A). These are standard,
   publicly-documented poker-stat definitions.
3. **We have the answer key.** The two PT4 CSV exports are ground-truth output _for a known hand
   set_. We diff our engine's output against them cell-by-cell until they match. Correctness is
   **measurable**, not hoped for.

The stat _numbers_ are objective (right/wrong vs PT4). The _verdict ranges_ are judgemental
opinions (BBZ's included) — treated as a config input, not as something to be computed (§7).

## 3. Architecture fit (reuse-first)

Mirror the existing pattern set by `tournament_analyzer.py`: **pure functions of
`(records, cfg)`, no I/O**, so they run on-demand now and can be cached/backgrounded later.

### New Python modules

```
leak_engine.py      # the core — hero-perspective per-hand flags + aggregation
  position_bucket(dealer_seatid, hero_seatid, active_seatids, n_players)
        -> 'SB'|'BB'|'EP'|'MP'|'CO'|'BTN'          # PT4 lookup_positions scheme
  action_string(flow, street, hero_seatid)          # compact 'X','CR','CC','R'… like PT4 lookup_actions
  hand_flags(record) -> { '<flag>': 0|1, ... }      # ~40 opportunity/action pairs, hero only
  aggregate(records) -> { position: { stat: {made, opp} }, ... , '_allin': {...} }

equity.py           # all-in-adjusted BB/100 (§6)
  hero_allin_equity(record) -> float | None         # bb the hero is "expected" to win, or None
                                                     # None when villain cards unknown (real result used)

leak_ranges.py      # thin loader/typing over the /config/leak_ranges Firestore doc (§7)
  classify(stat, position, pct, sample) -> 'good'|'bad'|'insufficient'|'neutral'
```

`leak_engine.py` reuses the flow-parsing conventions already proven in `hand_exporter.py`
(street actions, action-type codes, blind/ante handling). No parsing is invented from scratch.

### New Flask endpoints (thin — all logic in the modules)

```
GET  /leaks                              -> render templates/leaks.html   (Pro-gated page)
GET  /api/leaks?from=&to=&tourney=       -> aggregated leak report JSON for the signed-in hero
                                            (loads persisted tournament blobs, runs the engine)
GET  /api/leaks/validate  (dev/admin)    -> our counts vs the imported PT4 CSV, per cell (§5)
```

### Data model additions (Firestore)

- **`/config/leak_ranges`** — the BBZ target table. Shape:
  `{ stats: [ { key, label, street, min, max, per_position?: { SB:{min,max}, ... } } ] }`.
  The engine is agnostic to the numbers; swapping in BBZ's real values is a data edit, not a
  code change (§7).
- **Per-tournament cache (optimization, Phase 5):** store the aggregated count-vector
  (`{position: {stat: {made, opp}}}` + all-in sums) on each `users/{uid}/tournaments/{tid}` doc
  at import time. The cross-tournament report then sums vectors (~480 numbers/tournament) instead
  of re-reading every blob. v1 computes on-read (acceptable, consistent with how
  `analyze_tournament` already runs on page load).

### Report scope

The BBZ report is a **cross-tournament aggregate over a date range** (screenshot: 3,454 hands /
84 tournaments). So `/api/leaks` aggregates across the hero's persisted tournaments, with optional
`from`/`to`/`tourney` filters (filters land in Phase 5). Pro-gated, exactly like the existing
persisted-tournament features.

## 4. UI plan (reuse existing components)

New page **`/leaks`** (`templates/leaks.html`), modelled on `tournaments.html`: same auth bar,
same `info-tile` nav, same `section-card` + `table-dark` styling, same dark theme. Reachable from
a new **"Leak Report"** `info-tile` on both `/` and `/tournaments`. No redesign of existing pages.

Layout mirrors the BBZ screenshot, built from components we already have:

- **By Position / By Action** tabs (same tab pattern as `tournaments.html`'s list/bankroll/schedule).
- One expandable row per position (BTN/CO/MP/EP/BB/SB), each showing **Winrate (all-in adj bb)**,
  **Hands**, and **All / Good / Bad** pills — the same pill component as the `hstat-pill` row.
- Expanding a row reveals that position's stat list, each stat coloured **good/bad/insufficient**
  and showing `pct` + `made/opp` sample.

Verdict colours reuse the existing `--green` / danger palette; "insufficient data" renders greyed
(our honesty improvement over the screenshot — see §8).

## 5. Validation harness (built first, used every phase)

`tools/validate_leaks.py` (offline) + `/api/leaks/validate` (in-app, dev/admin only):

1. Parse the two PT4 CSVs (`ReportExport*.csv`) into a ground-truth table keyed by
   `(position, stat)` → `(pct, count)`.
2. Run our engine on the **same** hands (the DeepFreeze `.txt` corresponds to
   `ReportExport_deepfreeze.csv`).
3. Render a diff grid: our value vs PT4 value per cell, green when equal within tolerance, red on
   mismatch. **This is Phase 0's visible deliverable** and the acceptance gate for every later phase.

First gate is the cheapest and highest-signal: **per-position `Hands` counts must match**
(DeepFreeze: SB 34 / BB 30 / EP 58 / MP 70 / CO 32 / BTN 33 / total 257). If the position buckets
match, the mapper is right; then we layer stats on top.

## 6. All-in adjusted BB/100 (the headline "Winrate")

The screenshot's "Winrate: 68bb" **is** the CSV's `All-In Adj BB/100` column (BTN 68.65,
CO −14.58 — exact match). Per the `.pt4rpt` definition:

> _"…instead of attributing real results, this statistic attributes to the player the equity they
> had in the pot at the time of the all in. Pots where players call the all-in but later fold
> (cards unknown) are **not** adjusted."_

Rule, hero-only:

- Detect the hero's all-in point; if the pot reached showdown with **known** villain cards,
  substitute the hero's pot equity (enumerate remaining runouts) for the realised result.
- Otherwise use the realised result unchanged.
- Wrap a standard evaluator (`eval7` / `treys` / `pokerkit`) — solved problem, ~1 day, trivial
  compute at 3.2k hands. New dependency added to `requirements.txt`.

Non-all-in and unknown-card pots fall straight through to the realised bb result the app already
computes (`process_hands` → `bb_100`).

## 7. Verdict ranges — sourcing BBZ's targets (honest dependency)

We decided to **use BBZ's ranges** rather than invent our own. Important: **those exact numeric
ranges are not in any file provided** — the CSVs contain values, the screenshots show good/bad
_counts_, but not the thresholds themselves.

- The engine reads ranges from `/config/leak_ranges`; it works the moment that doc is populated.
- **Recommended way to obtain BBZ's numbers:** Caio has access to the BBZ tool — the per-stat
  detail (the "+" expanders) exposes each stat's acceptable band. Harvest those into the config
  doc (one-time data-entry task). Reverse-inferring thresholds from good/bad counts alone is
  under-determined and not recommended.
- Until harvested, ship a **placeholder** range set (solver-sane defaults) so the UI is fully
  functional; swapping in BBZ's real values is a config edit with zero code change.

This keeps the build unblocked while being upfront that exact BBZ-verdict parity depends on a data
input we still need to collect.

## 8. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| **Action-code ambiguity** — `hand_parser.py` treats 12=all-in call / 13=all-in raise, `hand_exporter.py` treats 12=fold-muck / 13=first-to-act check. Flags depend on this. | **Phase 0 prerequisite:** script over real hands to nail the canonical action model; document it once; both modules reconcile to it. |
| **Definitional edge cases** (dead blinds, walks, all-in-preflop killing postflop opps, multiway "position") | Found automatically by the CSV diff (§5); reconcile until green. |
| **`.txt` round-trip is lossy** — PT4 computes off our exporter's corrected text, so "match PT4" and "be correct" can mildly diverge. | Validate through the `.txt` path first to _prove parity_; optionally compute direct-from-JSON afterwards for accuracy, accepting small principled diffs. |
| **Thin samples** — true per-position × ~40 stats → single-digit denominators. | Sample-size gate in `classify()`: below a threshold render **insufficient/grey**, never a red/green verdict. More honest than the source. |
| **BBZ range data missing** (§7) | Placeholder ranges + config-driven swap-in. |

## 9. Phased delivery — each phase ends with a visible, validated UI increment

| Phase | Build | **Visible in UI** | Validation gate |
|------|-------|-------------------|-----------------|
| **0 — Foundations** | Canonical action model; `position_bucket`; CSV → ground-truth parser; diff endpoint | **Validation grid** (dev page): our counts vs PT4, per cell | Per-position **Hands** counts match the CSV |
| **1 — Preflop** | All preflop flags (RFI, limp family, 3-bet/opp, fold-to-steal, call-2bet, 4-bet, squeeze, BB-v-SB); `aggregate()` | **`/leaks` page**: by-position table, preflop columns, coloured cells | Preflop columns match PT4 CSV |
| **2 — Postflop** | Flop/turn/river flags (c-bet/float/probe/donk/check-raise/fold-to; HU & 3-bet variants) | **By Position + By Action tabs**, postflop rows added | Postflop columns match (true per-position) |
| **3 — Headline winrate** | `equity.py`; all-in-adj bb/100 per position | **"Winrate: X bb"** on each position row + total | All-In Adj BB/100 matches CSV |
| **4 — Verdicts** | Load BBZ ranges; `classify()`; sample-size gating | **All / Good / Bad pills** + expandable per-stat detail with sample counts | Good/Bad counts reproduce BBZ (given harvested ranges) |
| **5 — Cut the cord** | Date/tourney filters; count-vector caching; remove "export→PT4→BBZ" guidance; nav entry | **Filters + polished standalone Leak Finder**; single-click flow | End-to-end: import → report with no external tools |

Estimate: **~2 weeks to a solid v1** (Phases 0–4) that matches PT4 numbers on the validation set;
Phase 5 + exact-parity edge cases follow.

## Appendix A — Stat → formula map (from the `.pt4rpt`)

All stats are `numerator / denominator × 100`. Grouped by street. (A few CSV column labels vs
flag names to be confirmed against the CSV during Phase 0 — marked ⚠.)

### Preflop
| Report column | Numerator | Denominator |
|---|---|---|
| Raise First In | `cnt_p_raise_first_in` | `cnt_p_open_opp` |
| Limp Open | `cnt_p_limp_open` | `cnt_p_limp_open_opp` |
| Limp/Raise | `cnt_p_limp_raise` | `cnt_p_limp_faceraise` |
| Limp/Call | `cnt_p_limp_call` | `cnt_p_limp_faceraise` |
| Limp/Fold | `cnt_p_limp_fold` | `cnt_p_limp_faceraise` |
| Raise SB Open Limp (raise in BB, only SB limped) | `cnt_p_raise_sb_limp_in_bb` | `cnt_p_face_sb_limp_in_bb` |
| Fold BB v SB | `cnt_p_bb_v_sb_fold` | `fold + call + 3bet` |
| Fold to Steal | `cnt_steal_def_action_fold` | `cnt_steal_def_opp` |
| Call PF 2Bet | `cnt_p_2bet_def_action_call` | `cnt_p_2bet_def_opp` |
| 3Bet PF | `cnt_p_3bet` | `cnt_p_3bet_opp` |
| 3Bet Steal | `cnt_steal_def_action_raise` | `cnt_steal_def_3bet_opp` |
| 3Bet NAI <35 | `cnt_p_3bet_NAI_u35` | `cnt_p_3bet_opp_u35` |
| 2Bet PF & Fold ⚠ (fold to 3bet after open) | `cnt_p_3bet_def_action_fold_when_open_raised` | `cnt_p_3bet_def_opp_when_open_raised` |
| Raise & 4Bet+ PF | `cnt_p_4bet_after_raising` | `cnt_p_4bet_opp_when_open_raised` |
| 3Bet PF & Fold ⚠ (fold to 4bet after 3bet) | `cnt_p_4bet_def_action_fold_after_3b` | `cnt_p_4bet_def_opp_after_3b` |
| Fold to PF 4Bet After 3Bet <30 | `cnt_p_4bet_def_action_fold_after_3b_30` | `cnt_p_4bet_def_opp_after_3b_30` |
| PF Squeeze | `cnt_p_squeeze` | `cnt_p_squeeze_opp` |

### Flop
| Report column | Numerator | Denominator |
|---|---|---|
| CBet F OOP (HU) | `cnt_f_cbet_oop_hu` | `cnt_f_cbet_opp_oop_HU` |
| CBet F IP (HU) | `cnt_f_cbet_ip_hu` | `cnt_f_cbet_opp_ip_hu` |
| Float F HU | `cnt_f_float_hu` | `cnt_f_float_opp_hu` |
| Fold to F Cbet (HU) | `cnt_f_fold_cbet_hu` | `cnt_f_cbet_def_opp_hu` |
| Fold to F CBet (3B) | `cnt_p_3bet_f_cbet_def_action_fold` | `cnt_p_3bet_f_cbet_def_opp` |
| Fold to F Float HU | `cnt_f_float_def_opp_action_fold_hu` | `cnt_f_float_def_opp_hu` |
| CBet F & Fold (HU) (fold to raise after cbet) | `cnt_f_cbet_fold_to_raise_hu` | `cnt_f_cbet_face_raise_hu` |
| Raise F CBet (HU) | `cnt_f_cbet_def_action_raise_hu` | `cnt_f_cbet_def_opp_hu` |
| XR Flop HU | `cnt_f_check_raise_hu` | `cnt_f_check_raise_opp_hu` |
| Raise F CBet (3B) | `cnt_p_3bet_f_cbet_def_action_raise` | `cnt_p_3bet_f_cbet_def_opp` |

### Turn
| Report column | Numerator | Denominator |
|---|---|---|
| Donk T (HU) | `cnt_t_donk_hu` | `cnt_t_donk_opp_hu` |
| CBet T (HU) | `cnt_t_cbet_hu` | `cnt_t_cbet_opp_hu` |
| Float T | `cnt_t_float` | `cnt_t_float_opp` |
| Probe T (HU) | `cnt_t_probe_hu` | `cnt_t_probe_opp_hu` |
| Probe T HU & Bet R | `cnt_t_probe_hu_r_bet` | `cnt_t_probe_hu_r_open_opp` |
| Fold to T CBet | `cnt_t_cbet_def_action_fold` | `cnt_t_cbet_def_opp` |
| F to T Pr (HU) | `cnt_t_probe_def_action_fold_hu` | `cnt_t_probe_def_opp_hu` |
| Raise T CBet | `cnt_t_cbet_def_action_raise` | `cnt_t_cbet_def_opp` |
| Raise T Probe (HU) | `cnt_t_probe_def_action_raise` | `cnt_t_probe_def_opp_hu` |

### River
| Report column | Numerator | Denominator |
|---|---|---|
| Donk R | `cnt_r_donk` | `cnt_r_donk_opp` |
| CBet R | `cnt_r_cbet` | `cnt_r_cbet_opp` |
| Fold to R CBet | `cnt_r_cbet_def_action_fold` | `cnt_r_cbet_def_opp` |

### Headline
| Report column | Definition |
|---|---|
| Hands | count of hands played at that position |
| All-In Adj BB/100 | `amt_expected_bb_won / (hands / 100)` — see §6 |

## Appendix B — PT4 position scheme

`lookup_positions` collapses seats into six buckets: **SB, BB, EP, MP, CO, BTN**. Numeric hints
from the `.pt4rpt`: `position = 8` is BB, `position = 9` is SB (from the BB-v-SB steal formula,
`val_p_raise_aggressor_pos = 9`). EP/MP/CO bucketing is table-size dependent and must replicate
PT4's mapping; the Phase-0 gate (per-position Hands counts vs the CSV) confirms it.

## Appendix C — Open items before/at build

- [ ] Harvest BBZ's numeric ranges into `/config/leak_ranges` (§7) — data task, unblocks Phase 4 verdict parity.
- [ ] Confirm action-code semantics 12/13 against real hands (§8) — Phase 0 prerequisite.
- [ ] Confirm the three ⚠ column-label alignments against the CSV (Appendix A) — Phase 0/1.
- [ ] Pick equity library (`eval7` vs `treys` vs `pokerkit`) — Phase 3.
