# Changelog

All notable changes to CircleWave are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] - 2026-06-23

### Changed
- **Beatmap packs load automatically as you scroll** instead of needing the "Load
  more" button each time.

### Fixed
- **Sort by oldest under "Any" status** no longer shows old maps followed by a block
  of freshly-qualified ones — all statuses are now ordered together.
- **Most-played cards now show their star rating** (the most-played data has no
  difficulty info, so it's pulled from osu.direct like BPM).

> Note: some very old graveyard maps (pre-2009, and pre-2012 maps with video) aren't
> searchable or downloadable from osu!'s mirrors at all, so they can't appear here —
> an osu!-side limitation, not a CircleWave bug.

## [1.2.0] - 2026-06-20

### Added
- **Most played** — a new 🔥 *Most played* button loads any player's most-played
  beatmaps by username (or user ID), ordered by play count. It reads the public osu!
  profile, so no login or API key is needed. The maps drop into the same grid as the
  packs, so you can mass-download them and build a collection named after the player.

## [1.1.0] - 2026-06-20

### Added
- **Beatmap packs browser** — a 📦 *Beatmap packs* button browses all ~3,750 official
  osu! packs across the seven categories (Standard, Featured Artist, Tournament,
  Project Loved, Spotlights, Theme, Artist/Album), with a game-mode filter, name
  search, and paging. Picking a pack downloads every map and builds a collection named
  after it — the same flow as the medal packs.
- **Genre and Language filters** for searches.
- **BPM and play/favourite counts on cards**, enriched on demand from osu.direct
  (the search backend doesn't include them).
- **Sort directions** — Title A–Z/Z–A, Ranked newest/oldest, etc. now sort the way
  the labels say.

### Changed
- **Search backend migrated to the Hinamizawa mirror** for a complete index and
  clean, relevance-ranked results (it finds maps the previous backend was missing and
  returns an artist's full catalogue). Nerinyan is now the automatic fallback, and
  downloads still cascade across multiple mirrors.
- **Default view is now Ranked · osu! · newest** instead of an empty-query grab-bag.
- The **"Ranked" filter now includes approved maps**, matching the osu! website's
  counts.
- **Sayobot moved to the end of the download cascade** — it's China-hosted and slow
  from outside CN, so it's now only a last resort.

### Fixed
- **"Any" status returned nothing** for text searches; it now spans every status.
- **Status badges were wrong** (graveyard maps showed as "pending"); each card is now
  tagged with the status it was actually queried under.
- **Field-scoped searches** (Artist / Title / Mapper) returned incomplete results;
  they now fetch by relevance so the whole catalogue surfaces, then sort for display.
- **"In library" button styling** was inconsistent between preloaded-owned and
  freshly-downloaded maps; both now show the same green outline.
- **"No-video" toggle** only applied after a re-search; it's now read live at
  download time.

## [1.0.0] - Initial release

### Added
- Synthwave-themed PySide6 desktop app to browse and batch-download osu! beatmaps.
- Filters: mode, status, sort, BPM range, length range, star range, and a
  "Search in" field scope (Artist / Title / Mapper / Tags).
- Lazy-loading grid with cover art, audio preview, and a bottom download dock with a
  multi-mirror download cascade.
- **Medal packs** — browse Beatmap Pack medals from the osu! wiki, download a whole
  pack, and auto-build an osu!stable collection named after the medal.
- "Already in library" detection and hide-owned, driven by your osu! Songs folder.
- GPL-3.0 licensed; Windows `.exe` built via GitHub Actions.

[1.2.1]: https://github.com/AmarilloNL/CircleWave/releases/tag/v1.2.1
[1.2.0]: https://github.com/AmarilloNL/CircleWave/releases/tag/v1.2.0
[1.1.0]: https://github.com/AmarilloNL/CircleWave/releases/tag/v1.1.0
[1.0.0]: https://github.com/AmarilloNL/CircleWave/releases/tag/v1.0.0
