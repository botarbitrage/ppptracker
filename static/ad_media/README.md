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

## No default for video_30 / video_60

The two video types (`video_30`, `video_60`) ship with **no** bundled default
file — encoding a placeholder 30s/60s MP4 needs tooling this codebase doesn't
have. Their `default_path` is `None`, and picking `active: "default"` for a
video type is rejected by the admin API until a real file has been uploaded
and marked active. The gate-modal wiring Task (next in the "Build and
implement ad services" Feature) already requires falling back to the
existing stub behavior when a type has no active video configured — this is
exactly that state, reachable from day one.

## The files

- `banner_a_default.svg`, `banner_b_default.svg` — 320×180 placeholder crops,
  same palette/typography as `static/banners/`. Swap for real creative or
  point `_AD_MEDIA_TYPES[...]['default_path']` at a different asset.
