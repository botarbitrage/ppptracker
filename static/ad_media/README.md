# Ad media library placeholder art

Hard-saved defaults for the two image types (`banner_a`, `banner_b`) in the
self-hosted ad media library — see `_AD_MEDIA_TYPES` / `_ad_media_config()`
in `app.py`. Marked `PLACEHOLDER ART` in the bottom corner, same convention
as `static/banners/`.

Manage the library at **/admin → Ad Campaigns → Admin media library**: admins
can upload up to 4 additional files per type and mark one active, but these
two default files can't be deleted from the admin UI — they're the
always-available fallback each type resolves to when `active` is `"default"`
(the fresh-install state, before anyone has uploaded anything).

## Default videos for video_30 / video_60

Both video types ship a bundled default clip, wired via `default_path` in
`_AD_MEDIA_TYPES` and labelled in the admin library by `default_name`:

- `video_30_default.mp4` → **video_30** (Link Import gate), "PPPoker Hand
  Tracker short video ad #1".
- `video_60_default.mp4` → **video_60** (Hand Export gate), "long video ad #1".
  It is `video_30_default.mp4` played through twice (the same clip
  concatenated with itself), so the 60s slot is ~2× the 30s one.

Both are **portrait** (478×850) — the gate-stub modal sizes its player by
height (`.gate-stub-video` in `static/style.css`) so it isn't stretched to the
modal width. As with the banner defaults, these can't be deleted from the
admin UI; an admin uploads and selects a different clip to override. When a
default clip fails to load, `_showGateStubModal` in `static/app.js` still falls
back to its countdown-only stub.

## The files

- `banner_a_default.svg`, `banner_b_default.svg` — 320×180 placeholder crops,
  same palette/typography as `static/banners/`. Swap for real creative or
  point `_AD_MEDIA_TYPES[...]['default_path']` at a different asset.
- `video_30_default.mp4`, `video_60_default.mp4` — the portrait ad clips
  described above. Replace them to change the shipped default ad, or override
  per-slot from the admin library without touching the repo.
