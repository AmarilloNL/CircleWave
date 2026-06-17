# osu! Beatmap Downloader

A PySide6 desktop browser + batch downloader for osu! beatmaps.

## Install & run (CachyOS / Arch)
The system Python is externally managed (PEP 668), so use pacman, not bare pip:
```bash
sudo pacman -S pyside6 python-requests
python osu_beatmap_downloader.py
```
Isolated alternative: `python -m venv ~/.venvs/osu-dl && ~/.venvs/osu-dl/bin/pip install PySide6 requests`.
Audio preview needs GStreamer/FFmpeg codecs (present on a typical CachyOS install).

## Features
- **A-Z catalog** – empty search box + sort *Title (A-Z)*; scroll loads more.
- **Search** – title / artist / mapper / tags.
- **Filters** – mode, status (ranked/loved/graveyard/...), BPM range, star range.
  Mode/status/sort are sent to the API; BPM and star ranges are applied client-side
  (Nerinyan's search has no query param for them).
- **Audio preview** – ▶ on any card streams the 10s clip.
- **Library aware** – auto-detects your osu! Songs folder (override in Settings); owned maps
  show "✓ In library" and can be hidden.
- **Batch downloads** – concurrent queue, per-item progress, mirror fallback, optional no-video,
  optional auto-open to import.

## Data source
- Search and metadata come from **Nerinyan** (`GET https://api.nerinyan.moe/search`), an open,
  no-auth mirror whose responses match the osu!-web beatmapset format. No account or API key needed.
- Downloads use a mirror fallback cascade: nerinyan → sayobot → catboy → beatconnect.
- Endpoints + mirror order live in the `CONFIG` block at the top of the script.

## Notes / limits
- Library detection reads the **osu!(stable)** `Songs` folder (folders named `<setid> Artist - Title`).
  **lazer** stores maps by hash, so "already downloaded" won't catch lazer imports.
- Sort options are the ones Nerinyan supports (ranked date, title, artist, plays, favourites, updated).
# Osu-DL
