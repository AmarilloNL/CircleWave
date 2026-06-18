<div align="center">

<img src="icon.png" width="96" alt="CircleWave icon">

# CircleWave

**A synthwave-themed desktop browser & batch downloader for osu! beatmaps.**

Search the catalogue, preview audio, queue downloads with mirror fallback, and
auto-build osu!stable collections straight from Beatmap Pack medals — all from a
single-file PySide6 app with a neon pink-and-cyan UI.

[![Release](https://img.shields.io/github/v/release/AmarilloNL/CircleWave?color=ff66ab)](https://github.com/AmarilloNL/CircleWave/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/AmarilloNL/CircleWave/total?color=36e0ff)](https://github.com/AmarilloNL/CircleWave/releases)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

</div>

---

## Download

**Windows:** grab the latest **`CircleWave.exe`** from the
[**Releases**](https://github.com/AmarilloNL/CircleWave/releases/latest) page — no Python, no
install, just download and run.

> It's an unsigned app, so Windows SmartScreen may warn *"Windows protected your PC"* on first
> launch → click **More info → Run anyway**. The first start is a little slow while the bundle
> unpacks.

**Linux / macOS:** run from source — see [below](#run-from-source). It's a single Python file with
two dependencies, so it's a couple of commands on any distro.

## Screenshots

<div align="center">
<!-- Drop a screenshot at docs/screenshot.png (e.g. the main grid or a medal pack) and it shows here. -->
<img src="docs/screenshot.png" width="900" alt="CircleWave screenshot">
</div>

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
  (pulled live from the osu! wiki). Pick one and it loads the pack's maps (with real artist / title /
  mapper / stars), downloads them all through the queue, hashes the `.osu` files, and writes a
  collection named after the medal into a `collection.db` you choose. The path is set in Settings
  (Browse / Auto-detect) or picked once and remembered; a `.bak` backup is written and existing
  collections are merged, not overwritten. Close osu! before it finishes, then reopen. *Stable only —
  lazer keeps collections in a Realm DB that can't be safely written from outside.*
- **One folder** — a single location does double duty: maps download into it and it's scanned to
  mark what you already have (auto-detects osu-wine / lazer / Windows / macOS). Combined with a local
  history file, "✓ In library" / hide-owned works for lazer too and across machines.
- **Batch downloads** — queue manager with adjustable concurrency, per-item progress, mirror
  fallback, optional no-video, optional auto-open to import.
- **Bulk + queue control** — "Download all shown" queues the visible results; Pause/Resume the
  queue, Cancel all, or cancel an individual item (✕).

## Run from source

CircleWave is one file (`circlewave.py`) and needs just **PySide6** and **requests** on
**Python 3.10+**. Pick the route for your system.

### Arch / CachyOS / Manjaro
The system Python is externally managed (PEP 668), so install from the repos:
```bash
sudo pacman -S pyside6 python-requests
python circlewave.py
```
If audio preview is silent, add the GStreamer codecs PySide6 uses on Linux:
```bash
sudo pacman -S gst-plugins-good gst-plugins-bad gst-libav
```

### Fedora
```bash
sudo dnf install python3-pyside6 python3-requests
python circlewave.py
# audio codecs (if preview is silent), from RPM Fusion:
sudo dnf install gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-libav
```

### Debian / Ubuntu / Linux Mint / Pop!_OS
The packaged PySide6 is often older than upstream, so a virtualenv with pip is the reliable path:
```bash
sudo apt install python3-venv gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav
python3 -m venv ~/.venvs/circlewave
~/.venvs/circlewave/bin/pip install -r requirements.txt
~/.venvs/circlewave/bin/python circlewave.py
```

### openSUSE
```bash
sudo zypper install python3-PySide6 python3-requests \
  gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugins-libav
python3 circlewave.py
```

### Any other distro (universal venv)
Works anywhere with Python 3.10+; pip ships a self-contained Qt, so the only extra you may need is
the system GStreamer plugins (above) for audio preview:
```bash
python3 -m venv ~/.venvs/circlewave
~/.venvs/circlewave/bin/pip install -r requirements.txt
~/.venvs/circlewave/bin/python circlewave.py
```

### macOS
```bash
brew install python              # if you don't already have Python 3.10+
python3 -m venv ~/.venvs/circlewave
~/.venvs/circlewave/bin/pip install -r requirements.txt
~/.venvs/circlewave/bin/python circlewave.py
```
Audio preview uses macOS's native media backend, so no extra codecs are needed. On Apple Silicon
the pip PySide6 wheels are arm64-native.

> **Note on audio preview:** on Linux, PySide6 plays through GStreamer, so the relevant plugin
> packages must be present (they usually are on a desktop install). Windows and macOS use their
> native backends and need nothing extra.

## Data source
- Search and metadata come from **Nerinyan** (`https://api.nerinyan.moe`), an open, no-auth mirror
  whose responses match the osu!-web beatmapset format. No account or API key needed.
- Medal-pack maps are read from the public osu! pack pages; per-map details (mapper, BPM, stars) are
  looked up through the same search.
- Downloads use a mirror fallback cascade: **nerinyan → sayobot → catboy → beatconnect**.
- Endpoints, mirror order, and the product name (`APP_TITLE`) all live near the top of `circlewave.py`.

## Notes / limits
- The folder scan reads osu!(stable)-style entries (`<setid> Artist - Title`); the local history
  file covers everything downloaded through the app, including lazer imports.
- Sort options are the ones the mirror supports (ranked date, title, artist, plays, favourites, updated).
- **No genre filter.** Genre metadata lives only behind osu!'s authenticated API, and the no-auth
  mirrors don't expose it — so to keep CircleWave zero-setup, genre filtering isn't offered.

## License
CircleWave is released under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
It bundles PySide6 (LGPL/Qt) and requests (Apache-2.0), both compatible with the GPL.

> Not affiliated with or endorsed by ppy Pty Ltd. "osu!" is a trademark of its respective owner.
> Beatmaps are downloaded from third-party mirrors; please support mappers and the official game.
