# PPPoker Action Model (canonical)

_Phase 0 deliverable of the Leak Finder (see `docs/leak-finder-design.md` §8)._

The leak engine needs one authoritative meaning per PPPoker action-type code.
Today the two existing modules disagree on codes 12/13, so this file states the
canonical model, the evidence, and how the open question gets settled.

## Canonical table

| Type | Meaning | Chips field | Notes |
|-----:|---------|-------------|-------|
| 1  | **fold** | 0 | |
| 2  | **check** | 0 | |
| 3  | **call** | amount called | Sometimes 0 (PPPoker no-op — suppress); sometimes exceeds the current bet (encodes an all-in re-raise); may need capping to remaining stack |
| 4  | **raise** | raise-**to** total | Occasionally *incremental* (excludes the blind already posted) — detect & correct (see `hand_exporter.py`) |
| 5  | _(unused)_ | — | Reserved raise variant; never observed in data |
| 7  | **bet** | bet amount | Postflop opening bet |
| 8  | **post small blind** | SB amount | |
| 9  | **post big blind** | BB amount | |
| 10 | **post ante** | ante amount | |
| 12 | **fold** (variant) | 0 | Fold-and-muck; treated as fold |
| 13 | **check** (variant) | 0 | First-to-act check; treated as check |
| 100| **system event** | 0 | Skip silently |

**All-in is not an action code.** It is signalled per action by
`hand_chips == 0` with `chips > 0` on the same action object (the player's
stack after the action is zero).

## The 12/13 conflict and why the table above is canonical

- `hand_exporter.py` (`_FOLD_TYPES = {1, 12}`, `_CHECK_TYPES = {2, 13}`)
  treats 12 = fold variant, 13 = check variant.
- `hand_parser.py` (`_is_vpip` / `_is_pfr` / `_postflop_af`) treats
  12 = all-in call, 13 = all-in raise.

Both cannot be right. The exporter's semantics are adopted because they are
**battle-tested end-to-end**: its output has been imported into PokerTracker 4
at scale (3,454 hands), and PT4 validates pot arithmetic on import. If 12/13
really carried call/raise chips, rendering them as folds/checks would corrupt
pot totals and PT4 would have rejected those hands ("Invalid stack" /
pot-mismatch errors). It didn't. Additionally, all-in detection via
`hand_chips == 0` already covers the all-in case without special codes, and
codes 12/13 are chipless in the exporter's handling.

**Consequence to fix later:** if the audit below confirms the exporter's
semantics, `hand_parser.py`'s VPIP/PFR/AF slightly over-count whenever 12/13
occur (they'd count folds/checks as voluntary chips in). Fix alongside
Phase 1, when the leak engine's numbers exist to cross-check against.

## Empirical audit (closes the question)

Run the tally over a raw JSON export (Data → JSON export in the app, or the
`/api/export/json/*` endpoints):

```
python leak_validation.py --audit-actions pppoker_full_export_<ts>.json
```

It prints, per `(street, type)`: occurrences and how many carried chips.
Expected if the canonical table is right: types 12/13 appear with
`with_chips = 0` in all rows. If instead they carry chips, the table above is
wrong, `hand_parser.py` was right, and both this file and `hand_exporter.py`
need revisiting (and the historical PT4 imports were subtly off).

## Normalizations the engine must share with the exporter

PT4's ground-truth numbers were computed from the exporter's **corrected**
text. To match them (and to be right), any direct-from-JSON adapter must apply
the same normalizations the exporter applies:

1. Zero-chip type-3 calls → suppress (no-op).
2. Type-3 with chips > current bet → an all-in **raise** in call clothing.
3. Type-4 incremental encoding → convert to raise-to when
   `blind_committed > 0` and the raw increment alone would be an illegal raise.
4. Type-4 with chips ≤ current bet → actually a call.
5. Cap calls/raises to the player's remaining stack.
6. Chip values are 100× display scale (`_CHIP_DIV = 100`).
