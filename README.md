<div align="center">

# ◎ Circlewave

**A synthwave-themed desktop browser & batch downloader for osu! beatmaps.**

Search the catalogue, preview audio, queue downloads with mirror fallback, and
auto-build osu!stable collections straight from Beatmap Pack medals — all from a
single-file PySide6 app with a neon pink-and-cyan UI.

</div>

---

## Install & run (CachyOS / Arch)
The system Python is externally managed (PEP 668), so use pacman rather than bare pip:
```bash
sudo pacman -S pyside6 python-requests
python circlewave.py
```
Isolated alternative (any distro):
```bash
python -m venv ~/.venvs/circlewave
~/.venvs/circlewave/bin/pip install -r requirements.txt
~/.venvs/circlewave/bin/python circlewave.py
```
Audio preview needs GStreamer/FFmpeg codecs (present on a typical CachyOS install).

## Windows (.exe)
Grab the latest **CircleWave.exe** from the [Releases](../../releases) page — no Python needed,
just download and run.

Building it yourself (two ways):
- **GitHub Actions (recommended):** push a version tag and the bundled workflow builds + publishes
  the `.exe` automatically:
  ```bash
  git tag v1.0.0 && git push origin v1.0.0
  ```
  You can also trigger it manually from the repo's **Actions → Build Windows EXE → Run workflow**,
  then download the artifact.
- **Locally on Windows:**
  ```bat
  pip install -r requirements.txt pyinstaller
  pyinstaller circlewave.spec
  ```
  The result is `dist\CircleWave.exe` (single windowed file). First launch may be a touch slow as a
  one-file build unpacks to a temp dir; Windows SmartScreen may warn about an unsigned app (More
  info → Run anyway).

## Features
- **Synthwave / osu! UI** — neon pink-and-cyan theme on deep indigo, glowing search,
  hitcircle-style controls, cover art with status + star badges.
- **A–Z catalogue** — empty search box + sort *Title (A–Z)*; infinite scroll loads more.
- **Search** — title / artist / mapper / tags, with a **Search in** selector. To pull up a
  specific mapper's maps, set *Search in → Mapper* (a plain text search otherwise mixes in maps
  merely *tagged* with that name).
- **Filters** — mode, status (ranked / loved / graveyard / ...), BPM range, star range
  (including 7★/8★/9★/10★-and-up bands). Mode/status/sort/search-in are sent to the API; BPM and
  star ranges are applied client-side, and the grid keeps auto-loading until it has enough matches.
- **Audio preview** — ▶ on any card streams the ~10s clip; volume slider + stop button in the status bar.
- **Medal packs (osu!stable)** — the 🏅 *Medal packs* button lists every Beatmap Pack medal
  (pulled live from the osu! wiki). Pick one and it loads the pack's maps, downloads them all
  through the queue, hashes the `.osu` files, and writes a collection named after the medal into a
  `collection.db` you choose. The path is set in Settings (Browse / Auto-detect) or picked once and
  remembered; a `.bak` backup is written and existing collections are merged, not overwritten. Close
  osu! before it finishes, then reopen. *Stable only — lazer keeps collections in a Realm DB that
  can't be safely written from outside.*
- **One folder** — a single location does double duty: maps download into it and it's scanned to
  mark what you already have (auto-detects osu-wine / lazer / Windows / macOS). Combined with a local
  history file, "✓ In library" / hide-owned works for lazer too and across machines.
- **Batch downloads** — queue manager with adjustable concurrency, per-item progress, mirror
  fallback, optional no-video, optional auto-open to import.
- **Bulk + queue control** — "Download all shown" queues the visible results; Pause/Resume the
  queue, Cancel all, or cancel an individual item (✕).

## Data source
- Search and metadata come from **Nerinyan** (`https://api.nerinyan.moe`), an open, no-auth mirror
  whose responses match the osu!-web beatmapset format. No account or API key needed.
- Downloads use a mirror fallback cascade: **nerinyan → sayobot → catboy → beatconnect**.
- Endpoints, mirror order, and the product name (`APP_TITLE`) all live in the `CONFIG` block at the
  top of `circlewave.py`.

## Notes / limits
- The folder scan reads osu!(stable)-style entries (`<setid> Artist - Title`); the local history
  file covers everything downloaded through the app, including lazer imports.
- Sort options are the ones the mirror supports (ranked date, title, artist, plays, favourites, updated).
- **No genre filter.** Genre metadata lives only behind osu!'s authenticated API (v2 OAuth / v1 key),
  and the no-auth mirrors don't expose it — so to keep Circlewave zero-setup, genre filtering isn't offered.

## License
MIT — see [LICENSE](LICENSE).

> Not affiliated with or endorsed by ppy Pty Ltd. "osu!" is a trademark of its respective owner.
> Beatmaps are downloaded from third-party mirrors; please support mappers and the official game.
