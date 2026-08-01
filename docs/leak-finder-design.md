# Leak Finder — Design & Delivery Plan

_Status: **Phase 4 complete, plus a UX review pass** (§13): every stat always lists for every
position (39 rows each, not just ones with samples); the All pill now counts low-sample stats;
sample-count notation redesigned away from BBZ's subscript style; Result/Rec. Action columns are
sortable by distance-from-target with a new colour-graded Δ column; the report position scheme
was tuned further (§12/§13). Next: Phase 5 (filters + per-tournament caching)._
_Owner: Caio · Consulting engineer: Claude_

## 1. Goal

Collapse the current three-tool pipeline

```
PPPokerHA (export .txt) → PokerTracker 4 (compute report → .csv) → BBZ Leak Finder (visual report)
```

into a single feature **inside PPPokerHA**. The hands are **already in the app** (persisted
per-tournament in Firestore/Storage by the existing import flow) — the Leak Finder reads them
directly. **No file import, no export step, no dependency on the exporting page/process.** The
existing PokerStars-export functionality stays on the main page untouched; exports remain useful
for users who want PT4, but the Leak Finder never touches them at runtime (the `.txt`/CSV files
appear only in the offline validation harness, §5).

Flow: open **Leak Finder** page → filter saved tournaments (by tournament, buy-in value, dates)
→ the engine aggregates the matching persisted hands → by-position / by-action stat report with
good/bad verdicts, equivalent to the BBZ screenshots. Initial phases ship with a single report
over **all** saved tournaments; the filters arrive in a later phase (§9).

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

- **`/config/leak_ranges`** — the BBZ target table, seeded from the harvested
  `data/bbz_leak_ranges.json` (§7). Per-position: each position carries its own list of
  evaluated stats (the membership mask) with `target: [min, max]` bands. The engine is agnostic
  to the numbers; updating ranges is a data edit, not a code change.
- **Per-tournament cache (optimization, Phase 5):** store the aggregated count-vector
  (`{position: {stat: {made, opp}}}` + all-in sums) on each `users/{uid}/tournaments/{tid}` doc
  at import time. The cross-tournament report then sums vectors (~480 numbers/tournament) instead
  of re-reading every blob. v1 computes on-read (acceptable, consistent with how
  `analyze_tournament` already runs on page load).

### Report scope

The BBZ report is a **cross-tournament aggregate** (screenshot: 3,454 hands / 84 tournaments).
So `/api/leaks` aggregates across the hero's persisted tournaments. The page gets a filter bar
with three filters — **tournament, buy-in value, and date range** — that select which saved
tournaments feed the report (buy-in resolves from the tournament-config docs via the existing
room-name matching, `_norm_room_name`/`_resolve_tournament_cfg`). Initial phases ship a single
report over **all** saved tournaments; the filter bar lands in Phase 5. Pro-gated, exactly like
the existing persisted-tournament features.

## 4. UI plan (reuse existing components)

New page **`/leaks`** (`templates/leaks.html`), modelled on `tournaments.html`: same auth bar,
same `info-tile` nav, same `section-card` + `table-dark` styling, same dark theme. Reachable from
a new **"Leak Report"** `info-tile` on both `/` and `/tournaments`. No redesign of existing pages.

Layout mirrors the BBZ screenshot, built from components we already have:

- **By Position / By Action** tabs (same tab pattern as `tournaments.html`'s list/bankroll/schedule).
  Confirmed from the BBZ UI: **By Action is a pure transposition of the same cells** — stat-major
  grouping (one expandable card per stat, position rows inside) over identical values, targets and
  verdicts. Both tabs render from the same `aggregate()` output; no extra engine work.
- One expandable row per position (BTN/CO/MP/EP/BB/SB), each showing **Winrate (all-in adj bb)**,
  **Hands**, and **All / Good / Bad** pills — the same pill component as the `hstat-pill` row.
- Expanding a row reveals that position's stat list with the same columns as BBZ's detail view
  (confirmed from the expanded screenshots): **Name** (with opportunity count as subscript),
  **Hero** (his %), **Result** (`LOW` / `GOOD` / `HIGH` badge), **Target** (`min% – max%`),
  **Rec. action** ("Raise more/less", "Fold more/less", …, or "Good job").

Verdict colours reuse the existing `--green` / danger palette; "insufficient data" renders greyed
(our honesty improvement over the source — see §8).

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

**Phase-0 outcome — gate PASSED** against a per-tournament ground-truth pair
(`data/validation/10002806/`): BTN 14 · CO 14 · MP 38 · EP 31 · BB 15 · SB 15 · total 127, all
cell-exact. Three findings along the way:

1. **PT4's EP/MP bucketing is table-size dependent** — EP = the two earliest non-blind seats
   (positions ≥ N−4), not a fixed {5,6,7}. See Appendix B.
2. **PT4 silently drops hands that fail pot arithmetic.** The fixture txt has 128 hands; PT4
   imported 127. The harness mirrors this: `validate_pot()` re-derives each pot from the actions
   and excludes failing hands from suite aggregation (they're listed in the output).
3. **Real exporter bug found & fixed** (`hand_exporter.py`): a capped all-in call was recorded
   as matching the full bet, so the balanced-pot heuristic suppressed the "Uncalled bet
   returned" line → unbalanced hand → PT4 rejected it. The corpus tournaments show 7 more such
   hands from old exports. Fixed for future exports; historical fixtures keep the old text
   (that's what PT4 actually saw).

Fixture layout: `data/validation/<suite>/` (txt + the PT4 CSV generated from exactly those
txts), `corpus/` (txt-only parse-robustness set), `reference/` (unpaired CSVs). Adding more
per-tournament pairs (e.g. a final-table one to pin short-handed bucketing) = drop two files in
a new folder.

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

## 7. Verdict ranges — HARVESTED ✅ (`data/bbz_leak_ranges.json`)

We use **BBZ's ranges**, and they have now been harvested from screenshots of the expanded BBZ
report (all six positions, 2026-07-30) into **`data/bbz_leak_ranges.json`**, which seeds
`/config/leak_ranges`.

What the harvest established:

- **Verdict model:** `LOW` if hero% < target-min · `GOOD` if within band · `HIGH` if above.
  "Bad" = LOW or HIGH. **Rec. action** is a per-stat verb pair chosen by direction
  ("Raise more/less", "Fold more/less", "Bet more/less", …; `GOOD` → "Good job").
- **Per-position targets confirmed** — the same stat carries different bands per position
  (e.g. Fold to F Cbet (HU): BTN 27–33, CO 28–35, EP/MP/SB 33–38). Globally-computed stats
  (e.g. 3Bet NAI <35, identical value on every row) are still scored against per-position bands,
  producing different verdicts per position.
- **Membership mask captured:** which stats BBZ evaluates under each position —
  BTN 21 · CO 27 · MP 29 · EP 26 · BB 29 · SB 31 = **163 cells** (matches 14 good + 149 bad).
  BBZ advertises **175**; the ~12-cell gap is stats with zero opportunities in this dataset,
  which the UI simply omits. Back-fill those rows' targets if/when they appear in a future report.
- **BBZ is a pure renderer:** every hero value and opportunity count in the BBZ UI matches the
  PT4 CSV cell-for-cell (verified across all six positions). BBZ = PT4 report values + this
  target table + the verdict model above. Full parity is therefore: our engine (matching the CSV,
  §5) + this harvested table.
- **Cross-validated against the By Action tab** (full text capture, 2026-07-30): the tab's
  per-stat groups transpose to exactly the same 163 cells (sums check out: 163 All / 14 Good /
  149 Bad), which corrected five screenshot-read cells and cleared all `verify` flags. The
  harvest is now considered authoritative.

## 8. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| **Action-code ambiguity** — `hand_parser.py` treats 12=all-in call / 13=all-in raise, `hand_exporter.py` treats 12=fold-muck / 13=first-to-act check. Flags depend on this. | **Phase 0 prerequisite:** script over real hands to nail the canonical action model; document it once; both modules reconcile to it. |
| **Definitional edge cases** (dead blinds, walks, all-in-preflop killing postflop opps, multiway "position") | Found automatically by the CSV diff (§5); reconcile until green. |
| **`.txt` round-trip is lossy** — PT4 computes off our exporter's corrected text, so "match PT4" and "be correct" can mildly diverge. | Validate through the `.txt` path first to _prove parity_; optionally compute direct-from-JSON afterwards for accuracy, accepting small principled diffs. |
| **Thin samples** — true per-position × ~40 stats → single-digit denominators. | Sample-size gate in `classify()`: below a threshold render **insufficient/grey**, never a red/green verdict. More honest than the source (BBZ verdicts a 1-sample stat). |
| **Range-harvest residuals** (§7) | 3 cells flagged `verify:true`; ~12 zero-sample cells of the 175 grid lack targets until observed. Neither blocks any phase. |

## 9. Phased delivery — each phase ends with a visible, validated UI increment

| Phase | Build | **Visible in UI** | Validation gate |
|------|-------|-------------------|-----------------|
| **0 — Foundations ✅** | Canonical action model; `position_bucket`; CSV → ground-truth parser; diff endpoint | **Validation grid** (dev page): our counts vs PT4, per cell | Per-position **Hands** counts match the CSV — **PASSED** |
| **1 — Preflop ✅** | All preflop flags (RFI, limp family, 3-bet/opp, fold-to-steal, call-2bet, 4-bet, squeeze, BB-v-SB); `aggregate_stats()`; `records_to_ps_text()` runtime adapter | **`/leaks` page**: by-position preflop report with BBZ targets + provisional verdicts | Preflop columns match PT4 CSV — **PASSED** (2 accepted-deviation cells, Appendix C) |
| **2 — Postflop ✅** | Flop/turn/river flags (c-bet/float/probe/donk/check-raise/fold-to; HU & 3-bet variants); `report` position scheme | **`/leaks` grouped by street**, postflop rows added | Postflop columns match PT4 CSV — **PASSED** |
| **3 — Headline winrate ⚠️** | `equity.py` — exact enumeration, side-pot layers, persistent cache | **"Winrate: X bb"** on each position card + overall | Reported, **not gated** — PT4's column not reproducible cell-exact (§11) |
| **4 — Verdicts ✅** | `classify()` in `leak_engine.py`; sample-size gating (`MIN_SAMPLE=5`); local-JSON target loader (no Firestore move — see §12) | **All / Good / Bad / Low-sample pills**, `INSUFFICIENT` badge in the stat table | Classifier boundary logic matches **163/163** harvested BBZ verdicts — **PASSED** |
| **5 — Cut the cord** | Filter bar (tournament / buy-in value / date range); count-vector caching; remove "export→PT4→BBZ" guidance; nav entry | **Filters + polished standalone Leak Finder**; single-click flow | End-to-end: saved tournaments → report with no external tools |

Estimate: **~2 weeks to a solid v1** (Phases 0–4) that matches PT4 numbers on the validation set;
Phase 5 + exact-parity edge cases follow.

## 10. Case study: the "phantom missing hands" investigation (2026-07-30/31)

After the Phase 0 gate first passed (§5, single tournament), deploying to prod surfaced a second,
much stranger problem while re-validating on more tournaments: PT4 would report **"128 hands
imported, 0 errors, 0 duplicates"** and then show only **72** in the position-based report — for
data our own `validate_pot()` accepted as 100% internally consistent. Worth recording the
investigation because the eventual cause was not in our code at all, and the false leads are
useful context for future debugging:

1. **DeepFreeze #10002806, single-hand fix.** A real bug: a capped all-in call was recorded as
   matching the full bet, suppressing the "Uncalled bet returned" line → unbalanced hand → PT4's
   own explicit rejection ("Invalid pot size"). Fixed in `hand_exporter.py` (§Phase 0 above).
   Confirmed correct — but re-importing surfaced a **new**, much larger discrepancy (128 accepted,
   only 72 in the report) that this fix could not explain.
2. **False lead: table-transition / stack-continuity theories.** Byte-diffing old vs new exports
   showed only the one corrected hand differed; every downstream hand's stack reconciled exactly.
   Investigated whether PT4's session/sitting bookkeeping was confused by a table redraw
   immediately after the corrected hand — plausible-sounding, never confirmed.
3. **Texas tournaments (305 hands, 4 tournaments): same symptom, zero data explanation.** All 305
   new-export hands passed `validate_pot()` with zero rejections, yet PT4's report totalled 240 —
   a 65-hand gap with no textual difference to blame. Ruled out our exporter as the cause a second
   time.
4. **CRAZY2 (36 hands): the decisive test.** Old and new export files proved **byte-for-byte
   identical** — same hands, same bytes — yet one import reported 36 and another reported 27 for
   the *same file*. This is conclusive: with identical input producing different output, the
   cause cannot be in the data or our code. It has to be PT4-side (stale report cache, a database
   that wasn't actually clean, a report filter carried over between runs).
5. **Resolution.** Caio found and fixed a PT4-side import/report setting (not an app change).
   Re-exporting DeepFreeze (4 tournaments, 349 hands, 5–9 player tables) and re-importing gave
   **100% import, and PT4's report total (349) now matches our own parse count exactly** — see
   the `deepfreeze_all4` validation suite (§5). The "phantom missing hands" saga is closed.
6. **Bonus finding from the same dataset.** With a bigger, more varied validation set (down to
   5-handed tables), a real EP/MP position-bucketing gap appeared (EP off by +4, MP by −4,
   symmetric) and was fixed in the same sitting — see Appendix B.

Takeaway for future debugging of "PT4 shows fewer hands than we exported": check for a
byte-identical-input, different-output case *first* — it's the cheapest way to rule our code in
or out before chasing structural theories in the data.

## 11. Phase 3 finding: the all-in-adjusted winrate does not reconcile with PT4

**What we built.** `equity.py` computes each hand's all-in-adjusted result: it detects the street
where the money went in, enumerates *every* remaining runout exactly (never Monte Carlo — the gate
compares to two decimals, which sampling error cannot hold), settles main and side pots layer by
layer, and splits ties. The realised-money side is provably right: hero's net plus every
opponent's net sums to **exactly zero on all 476 validation hands**.

**What matches.** On clean heads-up all-ins our figure tracks PT4 almost exactly — EP in both
suites lands within **0.02–0.03 bb/100** of PT4 after adjustment (e.g. 29.08 vs 29.06 across 78
hands), which is what you would expect from correct equity and correct money.

**What doesn't.** Two positions in each suite disagree materially (BB and MP), and every position
carries a small residual (~0.1–0.25 bb/100) even where no hand is adjusted at all — implying PT4's
*realised* baseline also differs slightly from ours. Attempts to infer PT4's inclusion rule failed
because **structurally identical hands land on both sides of it**: in `#…8466` the villain shoves
and hero calls covering, and PT4 adjusts; in `#…68510` the villain shoves and hero calls covering,
and PT4 does not. No rule keyed on street, all-in ordering, who covered whom, side pots, or pot
size separates the two. Without PT4's internal `amt_expected_won` per hand we cannot close this.

**Decision.** Ship our number and label it honestly, consistent with decision Q4 (prefer
correctness over reproducing a source quirk). `/leaks` shows it as the headline "Winrate"; the
validation grid lists it with `gated: false`, rendered `≈` in blue, so the delta stays visible and
never silently passes. Should PT4's per-hand expected values become available, this is
re-openable.

**Cost and caching.** A preflop all-in enumerates C(48,5) = 1,712,304 runouts (~2s); flop/turn/
river all-ins are trivial. Across the live 4,403-hand history ~290 hands qualify, so a cold
computation is far too slow for a request. Equity depends only on the card layout and can never
change, so results are cached as pot-size-independent share *fractions* in
`data/equity_cache.json`, warmed offline and committed. The per-tournament aggregate cache in §3
(Phase 5) remains the durable fix for the rest of the report's cost.

## 12. Phase 4: verdicts and why the gate changed shape

**The BBZ-screenshot gate as originally written couldn't survive.** It compared our engine's
Good/Bad *counts* against the specific 163-cell snapshot harvested from BBZ's UI on 2026-07-30.
But `/leaks` now aggregates a live, growing dataset (4,403 hands vs. the snapshot's 3,454) — the
counts were never going to match again after the first new hand landed, through no fault of the
engine. The real, durable thing worth gating is the **classifier's boundary logic**, not a
snapshot's arithmetic. `run_classifier_check()` does exactly that: it re-applies `classify()` to
each of the 163 harvested (hero%, target, result) triples with sample-size gating switched off
(`opp=None`) — pure boundary math, decoupled from any dataset — and requires all 163 to reproduce
BBZ's rendered verdict. This is the new Phase 4 gate, run from `/leaks/validate` and the CLI.

**One real, useful finding from the check.** The single exact-boundary case in the harvest —
"F to T Pr (HU)" at hero%=30 against target `[30, 40]` (a global stat, so it repeats identically
across all 5 positions carrying it, not five independent trials) — is verdicted `LOW` by BBZ, not
`GOOD`. That means the boundary is **closed on both sides**: `pct <= min → LOW`, `pct >= max →
HIGH`, strictly-between → `GOOD`. `classify()` was corrected accordingly. Only the low end was
observed at an exact value; the high end is assumed symmetric, flagged as such in the docstring.

**Sample-size gating.** BBZ itself renders a confident verdict on samples as thin as n=1 — 43 of
its own 163 harvested cells (26%) sit under 5 opportunities. We gate at **`MIN_SAMPLE = 5`**:
below that, `classify()` returns `INSUFFICIENT` rather than a color, and the UI shows a grey
"low n" badge with the raw pct/count still visible (never hidden, just not overclaimed). Chosen
as a conservative floor — enough to suppress a coin-flip-sized sample without hiding most of
BBZ's own coverage. On the live report this reclassifies 60 of 152 evaluated cells (39%).

**No Firestore move for `/config/leak_ranges`.** The original plan (§3) proposed seeding a
Firestore doc so "updating ranges is a data edit, not a code change." Loading directly from
`data/bbz_leak_ranges.json` already satisfies that — editing the file and redeploying is a data
edit, no code change, and Caio is the only user, so there is no live-multi-tenant editing need
that would justify the added moving part. Revisit only if that changes.

## 13. Post-Phase-4 UX review (2026-08-01)

A review pass before starting Phase 5 raised six points; each is addressed below.

**1. Where targets come from, and how to fill the gaps.** Every target is a value Caio read
directly off the BBZ UI on 2026-07-30 into `data/bbz_leak_ranges.json` (§7) — there is no formula
generating them, so a stat with no target has simply never been harvested. Of the full 234-cell
grid (39 stats × 6 positions), **163 are harvested and 71 are not**. Cross-referencing the 71
against the live report found **31 that already have real samples** and would show a verdict the
moment a target exists — topped by **Limp Open** (EP 816 hands, CO 379, MP 341, BTN 248
opportunities) and the **Limp/Raise-Call-Fold family** at EP/MP/CO/BTN. Direction: open BBZ's
report again (it should reflect more hands now than the original 3,454-hand snapshot, since the
underlying PT4 database keeps growing) and screenshot the "+" detail for those rows the same way
as the original harvest (§7) — the highest-value targets to fetch first are exactly the 31 listed
above, ranked by sample size. Send screenshots and they merge into the same JSON file, no code
change.

**2/3. All 39 stats always shown; the "All" pill recount.** `/leaks` no longer hides stats with
zero opportunities — every position lists all 39, so row counts stop varying by position (they
previously ranged 27–34, purely an artifact of which stats happened to have both a target *and* a
sample). The **All** pill now counts Good + Bad + Low-sample (previously Good + Bad only); a
stat with zero samples or no target isn't counted in any pill since there's nothing to say about
it yet, but it still renders in the table.

**4. Sample-count notation.** The BBZ-style subscript glued to the stat name (`Raise First␣122`)
is gone; opportunity count is now its own **N** column, and Hero % is separate from it — visually
distinct from the source rather than mirroring it.

**6. Position balance, revisited.** The 5-handed table's single "extra" seat (previously routed
to EP) now goes to MP instead — see the code comment on `position_bucket`'s `'report'` branch for
the exact reasoning. This is a binary, per-hand choice (one seat, no way to split it), so it
cannot zero out the imbalance, only pick the better of two options: **total deviation from the
734-hand average drops from 399 to 227** (BTN 721, CO 707, MP 783, EP 660, BB 775, SB 757). The
residual imbalance flips direction (EP now slightly under, was slightly over) rather than
vanishing — flagged in code as the practical ceiling of a seat-counting approach; eliminating it
requires abandoning strict seat-based bucketing, which is a larger change than this review scope.

**7. Sorting and colour grading.** `leak_engine.delta_from_target()` gives every stat a signed
distance from its target band, normalized by the band's own width (so a 5–8% target and a
55–65% one are comparable on the same scale) — 0 inside the band, negative below the floor,
positive above the ceiling. **Result** and **Rec. Action** column headers are both clickable and
share one sort key: ascending ranks by smallest `|delta|` first (closest to target = best),
descending ranks largest first (furthest from target = most improvement needed); stats with no
delta (zero-sample, low-sample, or no target) always sort to the bottom in both directions, per
spec. A new **Δ** column renders the signed delta as a colour-graded chip (green → amber → red)
so severity is visible without reading the number.

## Appendix A — Stat → formula map (from the `.pt4rpt`)

All stats are `numerator / denominator × 100`. Grouped by street. Label alignments previously
marked uncertain are now **confirmed** by the BBZ UI screenshots (row labels + "Fold more/less"
rec-actions match the fold-frequency interpretation).

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
| 2Bet PF & Fold (fold to 3bet after open — confirmed) | `cnt_p_3bet_def_action_fold_when_open_raised` | `cnt_p_3bet_def_opp_when_open_raised` |
| Raise & 4Bet+ PF | `cnt_p_4bet_after_raising` | `cnt_p_4bet_opp_when_open_raised` |
| 3Bet PF & Fold (fold to 4bet after 3bet — confirmed) | `cnt_p_4bet_def_action_fold_after_3b` | `cnt_p_4bet_def_opp_after_3b` |
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

## Appendix B — Position schemes

`lookup_positions` collapses seats into six buckets: **SB, BB, EP, MP, CO, BTN**. Numeric
scheme (validated cell-exact): position 0 = BTN counting away from the button through non-blind
seats (CO = 1, …), BB = 8, SB = 9. Numeric position equals *players still to act behind you,
minus the two blinds*, independent of table size.

The engine carries **two bucketings**, selected by `position_bucket(..., scheme=)`:

**`pt4`** — reproduces PokerTracker exactly; the validation gate depends on it and it must never
drift. EP = the two earliest non-blind seats (positions ≥ N−4) at 7–9 handed; **tables of ≤6
players get no EP bucket at all**, those seats fall into MP.

**`report`** — what `/leaks` displays. Identical to `pt4` at 7–9 handed (EP = earliest
⌈pool/2⌉ seats, capped at 2, which reproduces PT4's mapping exactly); it only fills the gap PT4
leaves below 7-handed, so 6-max and 5-max UTG are EP rather than MP.

| seats | pool (2..K) | `pt4` EP | `report` EP |
|---|---|---|---|
| 9-handed | 2–6 | {5,6} | {5,6} |
| 8-handed | 2–5 | {4,5} | {4,5} |
| 7-handed | 2–4 | {3,4} | {3,4} |
| 6-handed | 2–3 | — (both MP) | {3} |
| 5-handed | 2 | — (MP) | {2} |

**Why the report needed its own scheme.** On a field that is ~60% short-handed (measured:
5-handed 22%, 6-handed 38% of 4,403 hands), PT4's no-EP-below-7 rule put **772 of 1,062 MP
hands** there purely because the table was short — burying every early-position hand inside MP
and starving EP. Distribution before → after: MP 1,062 → 574 and EP 381 → 869, with the other
four buckets unchanged (BTN 721, CO 707, BB 775, SB 757), i.e. from a 381–1,062 spread to a
574–869 one.

**Known trade-off.** Because numeric position encodes "players behind", any relative scheme
mixes slightly different strategic depths inside one row (6-max UTG and 9-max UTG both land in
EP though they face different numbers of opponents). Filtering by table size — a natural
companion to the Phase 5 filter bar — is the principled fix; the scheme choice only decides
labelling.

## Appendix C — Open items before/at build

- [x] Harvest BBZ's numeric ranges (§7) — **done 2026-07-30** → `data/bbz_leak_ranges.json`, all 6 positions.
- [x] Confirm column-label alignments (Appendix A) — confirmed by the BBZ UI screenshots.
- [x] Verify low-confidence target cells — resolved via the By Action tab capture (5 cells corrected, all flags cleared).
- [x] Confirm the By Action tab — pure transposition of the same 163 cells (§4); no extra data needed.
- [ ] Back-fill targets for the ~12 zero-sample cells of the 175 grid if/when they appear in a future BBZ report (nice-to-have; many can be deduced from neighbouring positions' bands).
- [x] Canonical action model documented — `docs/pppoker-action-model.md` (exporter semantics adopted; `hand_parser` 12/13 flagged as probable bug).
- [ ] Empirically confirm 12/13 via `python leak_validation.py --audit-actions <records.json>` on a raw JSON export (§8, action-model doc).
- [x] Phase-0 gate — **PASSED** with the #10002806 per-tournament pair (127/127, all positions cell-exact).
- [x] ≤6-player EP/MP bucketing — **PASSED**, confirmed via the 349-hand `deepfreeze_all4` suite (§10, Appendix B).
- [x] "Phantom missing hands" (PT4 reporting far fewer hands than imported) — **RESOLVED**, root cause was PT4-side, not our data; see §10 case study.
- [x] Phase-1 gate — **PASSED**: all 17 preflop stats cell-exact on both suites. Four stats
      confirmed *position-blind* in the BBZ report ("Limp Open", "Raise SB Open Limp",
      "3Bet NAI <35", "Fold to PF 4Bet After 3Bet <30" — their custom formulas lost the position
      grouping); gated population-wide, while `/leaks` shows true per-position splits (decision Q4).
- [x] Known accepted deviation (±1): PT4 grants a 4bet opportunity in hand #…68510 (BB shoves
      all-in, everyone else folded — a 4bet is impossible) while denying it in the structurally
      identical #…8466 in the same report. We follow the rules of poker; the two cells are
      pinned exactly in `leak_validation.ACCEPTED_DEVIATIONS` so any drift re-fails the gate.
- [x] Phase-2 gate — **PASSED**: all 22 postflop stats cell-exact on both suites. Confirmed rule:
      **every stat carrying a custom "(HU)" restriction is position-blind** in the BBZ report
      (their heads-up filter keyed off the hand-level `cnt_players_f`, collapsing the position
      grouping), while non-HU stats keep per-position grouping. Definitions that the diff pinned
      down: facing a c-bet is hero's first decision *with a bet to call* (not their first action
      — the OOP line checks first); c-bets form a chain (flop c-bettor → turn → river), so a flop
      check-raiser is not "continuing"; donk/probe require acting before the previous street's
      aggressor; turn float requires position; "3bet+ pot" does not require hero to have been the
      original raiser.
- [x] Report position bucketing corrected for short-handed tables (Appendix B).
- [x] Equity library — **eval7** (C evaluator). Note its `py_hand_vs_range_exact` is broken in the
      current build (always returns 0.0) and its Monte Carlo drifts in the 3rd decimal, so we
      enumerate runouts ourselves with `eval7.evaluate`.
- [x] Phase-3 winrate — shipped, **reported but not gated**; PT4's column not reproducible (§11).
- [ ] Re-open the winrate reconciliation if PT4 per-hand `amt_expected_won` values become available.
- [x] Phase-4 gate — **PASSED**: `classify()` matches all 163 harvested BBZ verdicts exactly.
- [x] Boundary inclusivity resolved: closed on both ends (`<=`/`>=`), confirmed at one exact
      hit (low end); high end assumed symmetric pending a confirming data point.
- [x] Sample-size gating shipped at `MIN_SAMPLE = 5` (`leak_engine.classify`); 60/152 live cells
      reclassified from a confident verdict to `INSUFFICIENT`.
- [ ] Phase 5: per-tournament aggregate cache (makes the whole report instant, supersedes the
      equity-only cache file); filter bar (tournament / buy-in / date range).
