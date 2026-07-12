# Changelog

All notable changes to CircleWave are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **osu.direct & hinamizawa download mirrors** — added both to the fallback
  chain. osu.direct uses its `/d/{id}` endpoint (the `/api/d/` path rate-limits);
  hinamizawa uses `/api/v1/hinai/d/{id}` — the same complete-index infra already
  used for search, which is handy for graveyard maps. More redundancy overall.

### Changed
- **Default mirror order**: hinamizawa, nerinyan, catboy, sayobot, beatconnect,
  osu.direct. beatconnect and osu.direct are last as keyless fallbacks (they
  benefit from a manual API key). The queue still reorders live by measured
  per-session speed/reliability on top of this default.
  Note this is only a *seed*: at download time the queue already reorders mirrors
  by the speed and reliability it measures per session, since real mirror speed
  swings a lot per map (CDN cache hit vs cold generate) and rate-limit state.

## [2.4.0] - 2026-07-12

### Fixed
- **Downloads crashed on start** — the mirror-health reordering added in 2.2.0
  indexed a mirror *name* (a string) as if it were a dict, throwing
  `TypeError: string indices must be integers` the moment any download began
  (single, batch, or pack). The ordering is now a tested pure helper
  (`order_mirrors`) and downloads work again. This affected 2.2.0 and 2.3.0.

### Added
- **"For You" recommendations** — command palette → ✨ For You surfaces ranked
  maps from the mappers you own the most sets from, filtered to ones you don't
  already have.
- **Similar maps** — a "Similar maps" button in the beatmap detail panel browses
  ranked maps around that set's difficulty in the same mode.
- **Shortcuts & tips (F1)** — a cheat-sheet dialog of every keyboard shortcut and
  where to find the newer features.
- **Library length** — the dashboard now shows the approximate total mapped-audio
  length of your library (e.g. "~412h 9m of mapped audio").

## [2.3.0] - 2026-07-11

### Added
- **Practice-set generator** — pick a mode + star band + count and CircleWave
  builds a collection of that many *ranked maps you don't already own* in range
  (owned = download history + `osu!.db`), for deliberate skill progression.
  (Command palette → "Generate practice set".)
- **Smart collections** — save named dynamic rules (a rule is a search filter);
  **Apply** one to browse, or **Build collection** to materialise a fresh
  `collection.db` collection from everything it matches right now. Rules persist
  in `smart_rules.json`.
- **Library dashboard** — a read-only overview from `osu!.db` (beatmapset /
  difficulty counts, breakdowns by mode and status, `.osz` size on disk) plus a
  **duplicate finder** for beatmapset folders and leftover `.osz` versions.
- **Follows** — watch a mapper or your current search; on each launch CircleWave
  checks for newly-ranked maps and notifies you (baselines silently on first add,
  so no back-catalogue spam). Stored in `follows.json`.
- **Mappool / bulk-link importer** — paste any text full of osu! beatmap links or
  ids (a tournament mappool post, a spreadsheet, a plain list); CircleWave
  extracts every reference, resolves them, and builds a collection. Command
  palette → "Import mappool / links".
- **Collection tracklist export** — save a collection as a readable `.txt`
  tracklist (`Artist - Title [diff]`), resolved via `osu!.db`.
- **Duplicate cleanup** — the Library dashboard can now *remove* the duplicate
  set folders / `.osz` it finds, moving them into a `_CircleWave_trash` folder
  (recoverable, never hard-deleted) and keeping one copy of each.
- **Download missing maps from a collection** — one click in the collection
  manager resolves every map in a collection you *don't* own (via its checksums)
  and queues them. Perfect after an osu!Collector or mappool import.
- **Top mappers** — the Library dashboard now ranks the creators you have the
  most beatmapsets from.
- **Queue: move to top** — an ⭱ button on a pending download bumps it to the
  front so it downloads next.

### Fixed
- **osu!.db unreadable on current osu! builds** — recent osu!stable (db version
  20260711) stores per-map star ratings as a Single/float (type tag `0x0c`, 4
  bytes) instead of the older Double (`0x0d`, 8 bytes). The parser assumed a
  Double and desynced on the very first beatmap, so *every* `osu!.db` feature
  silently failed and fell back (exact "✓ In library", collection **Stats** /
  **Cleanup**, Library dashboard, practice-set owned-exclusion). It now reads the
  value tag and decodes float or double accordingly — verified against a real
  109,454-map database.
- **Collection manager osu!.db path** — Stats/Cleanup now honour the osu!.db path
  set in Settings (they previously only looked next to the Songs folder), and the
  "couldn't read" message now names the actual path and reason instead of a
  misleading "set it in Settings".

## [2.2.0] - 2026-07-11

### Added
- **Inline search operators** — type `star>6`, `bpm<200`, `length>120`,
  `mode=mania`, `status=loved`, or field scopes `artist=` / `title=` / `mapper=`
  right in the search box; they override the equivalent dropdowns for that search
  (`parse_query`).
- **Fuzzy / typo-tolerant matching** helpers (`fuzzy_score`, `fuzzy_rank_sets`)
  so near-miss queries can still surface the right set.
- **Download verification** — a finished `.osz` is checked against the set's
  authoritative per-diff md5s; a mismatch flags the row (⚠) instead of silently
  passing (`verify_osz`).
- **Size / disk-space guard** — queueing a large "Download all shown" batch first
  shows an estimated total and the destination's free space (`estimate_download_size`).
- **Mirror health** — per-mirror speed and reliability are tracked this session and
  the download fallback chain prefers the fastest, most reliable mirror
  (`MirrorStats`).
- **Collection tools** in the manager — **Stats** (installed/missing + mode
  breakdown vs `osu!.db`), **Diff** (compare two collections), and **Cleanup**
  (find empty, redundant/subset, and orphaned-hash collections).
- **Command palette** (Ctrl+K) — fuzzy-filterable list of every action; type and
  press Enter to run.
- **osu!Collector import** — paste an osu!Collector URL or id in the collection
  manager to import that collection straight into your `collection.db`.
- **Offline search cache** — the first page of each search is cached; the same
  search reopens instantly and still shows results if you're offline (up to a day
  old).
- **Smart-collection rule engine** (`set_matches_rule` / `filter_sets_by_rule`) —
  materialise a collection from a saved filter rule.
- **Collection from results** — a "Build collection from shown results" action
  (command palette) turns whatever the current search matched into a
  `collection.db` collection in one step.
- **Keyboard grid navigation** — arrow keys move a focus ring between result
  cards; Enter opens details, **P** previews, **D** downloads, **X** selects. Keys
  are scoped to the results area, so they never interfere with typing.
- **Auto-play previews** — an optional radio mode (command palette / `autoplay`
  setting) advances to the next card's preview when a clip finishes.
- **List / compact view** — a ▤ toolbar toggle (or **Ctrl+L**) switches results
  between the hero-cover grid and a dense one-line list (status, ★, artist,
  length, BPM, modes, mapper + the same actions), for fast bulk scanning. The
  choice is remembered.
- **App self-update check** — on startup CircleWave quietly checks GitHub for a
  newer release and, if one exists, shows a note; the command palette has a
  "Get the latest release" action. Toggle via the `check_app_update` setting.
- **macOS CI build** — a `build-macos.yml` workflow produces an (unsigned) arm64
  binary attached to tagged releases, alongside the Windows and Linux artifacts.

### Changed
- **Settings dialog breathing room** — taller, uniform field heights, even row
  spacing, and aligned inline buttons (the file-picker rows no longer sit tighter
  than the plain fields); the window sizes to fit so nothing reads as squished.
- **No more boxed labels** — the dialog backdrop gradient was leaking onto form
  labels and checkboxes (both direct children of the dialog), drawing an ugly box
  behind each one; those now render as plain text on the backdrop.

### Fixed
- **Mode order on cards** — a set's modes now list in canonical game order
  (osu! → taiko → catch → mania) instead of star-rating order, so a hybrid set
  like *The Big Black* (an osu! marathon with one easier guest taiko diff) reads
  "osu! taiko" rather than the confusing "taiko osu!".
- **Approved maps were invisible** — beatmaps with the old *Approved* status (the
  original **FREEDOM DiVE** #39804, Big Black, most 2012-era marathons) never
  appeared under **Ranked** *or* **Any**. The "Ranked" filter only queried status
  `ranked`, "Any" omitted approved, and the mirror can't isolate approved maps
  (a `status=approved` query returns ordinary ranked maps) — they only come back
  on an unfiltered query. Ranked/Any searches now pull approved maps in via a
  supplementary no-status fetch and merge them, tagged with their real status.

### Internal
- New Qt-free helpers in `circlewave_core.py` with 54 added tests (128 total).

## [2.1.0] - 2026-07-07

### Added
- **Multi-select + collection builder** — tick maps across searches and use the
  selection bar to **download** them, build a **new collection**, or **add them
  to an existing collection**. Collections are built from per-diff checksums
  (fetched where needed), so no download is required first.
- **Paste a beatmap link or ID** into the search box to jump straight to that
  set's detail panel (beatmap/difficulty ids resolve to their set automatically).
- **Exact library via `osu!.db`** — point Settings at your osu!stable `osu!.db`
  (or auto-detect) for a precise installed-map list behind "✓ In library";
  falls back to the folder scan if it can't be read.
- **Export / import collections** — share collections as a portable JSON file
  from the collection manager; importing merges into your `collection.db`.

## [2.0.0] - 2026-07-06

### Added
- **Collection manager** (🗂) — view, rename, delete and merge the collections in
  your `collection.db`, with a library summary. Every edit backs up first.
- **Beatmap detail panel** (ⓘ on a card) — every difficulty at a glance, plus
  one-click **More by mapper** / **More by artist** scoped searches.
- **Persistent download queue** — a batch survives a restart: pending and
  in-flight items are saved and restored (paused), resuming via their `.part`
  files. New **Retry failed** button re-queues everything that errored.
- **Filter presets** (★) — save the current filter set under a name and re-apply
  it in one click.
- **Random / surprise me** (🎲) and **search-box history** (autocomplete of recent
  queries).
- **Check for updates** (⟳) — scans your downloaded `.osz` files against
  authoritative per-diff checksums and offers to re-download any with a newer
  version online.
- **16 full-UI themes** — Settings offers a real theme picker (Synthwave, Matrix,
  Ember, Dracula, Aurora, Carbon, Bubblegum…). Each recolours the *entire* UI —
  backgrounds, surfaces, borders, text and both accents — derived from seed
  colours, not just the two highlight colours.
- **System tray** icon + a notification when the queue finishes; **keyboard
  shortcuts** (Ctrl+F, F5, Ctrl+R, Ctrl+D, Ctrl+Shift+C, Ctrl+,, Esc);
  **live download speed + ETA**; and the app now **reopens with your last filters**.
- **Optional official osu! API** — set OAuth client credentials in Settings
  (client-credentials grant, no user login) for higher rate limits and
  authoritative data used by the update check.
- **Collection write preview** — before a `collection.db` is created or modified,
  a confirmation dialog shows how many maps go in, whether a same-named collection
  is being replaced, which others are kept, and where the file lands.
- **Resumable downloads** — an interrupted or cancelled `.osz` keeps its partial
  file and resumes via HTTP Range (from the same mirror), instead of restarting.
- **pip / pipx install** — `pyproject.toml` exposes a `circlewave` console script.
- **Linux release binary** built in CI (PyInstaller), attached to tagged releases.

### Changed
- **More reliable networking** — all requests share one pooled session with
  automatic retry/backoff on rate-limits and 5xx; the osu.ppy.sh scraping paths
  are politely throttled. Finished downloads are validated as real zips.
- **lazer collections** — clarified that CircleWave's stable `collection.db` can be
  imported into lazer via its Setup Wizard's Import step.

### Internal
- Split the Qt-free logic into `circlewave_core.py` with a `pytest` suite (74 tests);
  builds now run the tests before packaging. Swallowed errors are now logged
  (tunable via `CIRCLEWAVE_LOG`).

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

[2.1.0]: https://github.com/AmarilloNL/CircleWave/releases/tag/v2.1.0
[2.0.0]: https://github.com/AmarilloNL/CircleWave/releases/tag/v2.0.0
[1.2.1]: https://github.com/AmarilloNL/CircleWave/releases/tag/v1.2.1
[1.2.0]: https://github.com/AmarilloNL/CircleWave/releases/tag/v1.2.0
[1.1.0]: https://github.com/AmarilloNL/CircleWave/releases/tag/v1.1.0
[1.0.0]: https://github.com/AmarilloNL/CircleWave/releases/tag/v1.0.0
