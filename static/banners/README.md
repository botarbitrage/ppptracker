# Promo banner placeholder art

Placeholder images for the two promotional banner slots on the main page.
They exist so the slots can be switched on and reviewed before real artwork
is commissioned — every one is marked `PLACEHOLDER ART` in the bottom corner.

## They are on by default

`_BANNERS_DEFAULTS` in `app.py` points at these files, so both slots are
populated on a fresh deploy with no admin action — `side-1/2/3.svg` rotating
every 6s in the side slot, `mid-1.svg` in the mid-page slot.

Change or turn them off at **/admin → Ad Campaigns → Banners**. Anything
saved there is stored in Firestore and wins over the defaults above:

| Field | Default | To turn the slot off |
| --- | --- | --- |
| Side (vertical) — one URL per line | `side-1.svg`, `side-2.svg`, `side-3.svg` | empty the textarea |
| Seconds per image | `6` | — |
| Mid-page (horizontal) | `mid-1.svg` (`mid-2.svg` is the alternate) | empty the field |

The side slot rotates when two or more images are set, shows a single image
statically when one is, and disappears entirely at zero.

Note that clearing a field only takes effect once saved: an admin save writes
every field, so the cleared value persists and the default never comes back
on its own. To restore the defaults, delete the `config/banners` document in
Firestore.

## The files

| File | Size | Message |
| --- | --- | --- |
| `side-1.svg` | 120×480 | Brand — PPPoker Hand Tracker, "Analyse · Study · Win" |
| `side-2.svg` | 120×480 | Leak Finder — "Spot the costly spots" |
| `side-3.svg` | 120×480 | Pro upsell — unlimited imports, no surveys |
| `mid-1.svg` | 1400×180 | Main pitch — "Paste any PPPoker replay link" |
| `mid-2.svg` | 1400×180 | Pro upsell — the Free vs Pro benefits, wide layout |

## Replacing them with real artwork

Match the dimensions above. The side slot is `object-fit: cover` at exactly
120×480, so anything off-ratio gets cropped. The mid slot renders at
`width: 100%` with `max-height: 180px` and scales down on narrower screens —
keep type large enough to survive roughly two-thirds scale.

Two things to know if you edit the SVGs rather than replace them:

- **Fonts.** These load through an `<img>` tag, which cannot fetch external
  resources, so the Google Fonts the rest of the site uses (Oxanium, Exo 2)
  are unavailable here. Every text element pins its width with `textLength`
  and `lengthAdjust="spacingAndGlyphs"` so the layout holds in whatever font
  the browser falls back to. If you change a string, update its `textLength`
  to match, or it will stretch or squash to the old width.
- **Prices.** Deliberately not baked into the art — the live figure comes
  from the pricing config and would go stale in an image.

Colours are the brand tokens from `static/style.css` (`--color-bg`,
`--color-brand-green`, `--green`, `--yellow`, `--blue`).
