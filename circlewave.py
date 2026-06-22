#!/usr/bin/env python3
"""
Circlewave
==========

A PySide6 desktop browser + batch downloader for osu! beatmaps, with a
synthwave/neon look. Search the catalogue, preview audio, queue downloads with
mirror fallback, and auto-build osu!stable collections from Beatmap Pack medals.

Copyright (C) 2026 AmarilloNL

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, version 3.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.  See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program.  If not, see <https://www.gnu.org/licenses/>.

Data source : Nerinyan (https://api.nerinyan.moe) osu!-web-compatible mirror API.
              No auth required. The actual .osz download falls back across
              nerinyan / sayobot / catboy / beatconnect.

Features
--------
* A-Z catalog        : empty query + "Title (A-Z)" sort + infinite scroll.
* Search             : free-text over title / artist / mapper / tags.
* Filters            : game mode, status (ranked/loved/graveyard/...), search
                       field (mapper/title/...), BPM range, star-rating range.
                       BPM/star ranges are applied client-side, so the grid is
                       guaranteed to respect them whatever the server returns.
* Sort               : relevance, title, artist, difficulty, ranked date, rating,
                       plays, favourites.
* Audio preview      : streams b.ppy.sh/preview/{id}.mp3 (needs QtMultimedia codecs).
* Cover art grid     : async-loaded, disk-cached thumbnails in a responsive flow.
* Library awareness  : auto-detects your osu! Songs folder (manual override too),
                       greys out / can hide sets you already have.
* Batch downloader   : concurrent queue, per-item progress, mirror fallback,
                       optional no-video, optional auto-open in osu! after download.

NOTE ON ENDPOINTS
-----------------
Mirror APIs change occasionally. All base URLs + params live in the CONFIG block
below so they're trivial to tweak. Quick sanity check from a shell:

    curl "https://catboy.best/api/v2/search?q=freedom%20dive&sort=title_asc" | head
    curl -L -o test.osz "https://catboy.best/d/41823"

Run:  python osu_beatmap_downloader.py      (requires: PySide6, requests)
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import math
import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from PySide6.QtCore import (
    Qt, QObject, QRunnable, QThreadPool, Signal, Slot, QSize, QUrl, QRect,
    QPoint, QTimer, QSettings,
)
from PySide6.QtGui import QPixmap, QDesktopServices, QFont, QFontMetrics, QIcon, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit,
    QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox, QHBoxLayout, QVBoxLayout,
    QGridLayout, QFormLayout, QScrollArea, QFrame, QSizePolicy, QLayout,
    QToolButton, QProgressBar, QDialog, QDialogButtonBox, QFileDialog,
    QMessageBox, QSplitter, QStatusBar, QStyle, QSlider, QGraphicsDropShadowEffect,
    QListWidget, QListWidgetItem,
)

# QtMultimedia is optional (preview audio). Degrade gracefully if codecs missing.
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    HAS_MULTIMEDIA = True
except Exception:  # pragma: no cover
    HAS_MULTIMEDIA = False


# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
# Branding. APP_TITLE is the one place to change the product name -- it drives
# the window title and the header wordmark.
APP_TITLE = "Circlewave"
APP_VERSION = "1.2.0"
APP_TAGLINE = "osu! beatmap browser & downloader"
ORG_NAME = "AmarilloNL"
APP_NAME = "Circlewave"

NERINYAN_SEARCH = "https://api.nerinyan.moe/search"   # POST JSON body; returns osu!-web array
# Hinamizawa mirror search: complete index (incl. maps Nerinyan lacks), clean
# relevance, server-side genre/language/status/bpm/star filters. CheeseGull-style
# response. No working pagination and no BPM/play-count fields -- so it's used for
# field-scoped searches and genre/language filters, with Nerinyan for plain browse.
HINA_SEARCH = "https://mirror.hinamizawa.ai/api/v1/hinai/search"
HINA_AMOUNT = 100                # page size; paginates via the `offset` param
# hinamizawa's search has no BPM / play counts; osu.direct returns full osu!-web
# data for any set, so visible hinamizawa cards are enriched from it on demand.
OSU_DIRECT_SET = "https://osu.direct/api/v2/s/{id}"
PREVIEW_URL = "https://b.ppy.sh/preview/{id}.mp3"
WEB_SET_URL = "https://osu.ppy.sh/beatmapsets/{id}"

# Beatmap-pack medal data. The medal->pack mapping lives in the osu! wiki (mirrored
# on GitHub raw, which isn't bot-gated); pack contents come from the public pack page.
MEDAL_WIKI_URL = ("https://raw.githubusercontent.com/ppy/osu-wiki/master/"
                  "wiki/Medals/Unlock_requirements/Beatmap_packs/en.md")
PACK_PAGE_URL = "https://osu.ppy.sh/beatmaps/packs/{tag}"
# Pack listing (newest first, 100 per page). Categories use osu!'s `type` values.
PACK_LIST_URL = "https://osu.ppy.sh/beatmaps/packs?type={type}&page={page}"
PACK_TYPES = [
    ("Standard", "standard"), ("Featured Artist", "featured"),
    ("Tournament", "tournament"), ("Project Loved", "loved"),
    ("Spotlights", "chart"), ("Theme", "theme"), ("Artist/Album", "artist"),
]
PACK_PAGE_COUNT = 100          # packs per listing page
# A user's most-played beatmaps (public; no auth). The profile URL redirects to
# /users/{id}, and the website's own JSON route serves the most-played list.
USER_PROFILE_URL = "https://osu.ppy.sh/users/{user}"
MOST_PLAYED_URL = "https://osu.ppy.sh/users/{id}/beatmapsets/most_played?limit={limit}&offset={offset}"
MOST_PLAYED_PAGE = 51          # the route caps a single request at 51

PAGE_SIZE = 50
# When a search is narrowed client-side (field scope, BPM / star / length range),
# we pull a much bigger page so 1-2 requests gather enough matches instead of ~20
# sequential 50-result pages. The mirror caps ps at 1000; only matched cards are
# rendered, so the rest is just discarded JSON.
FILTER_PAGE_SIZE = 250
# Field-scoped searches (Search in -> Artist/Title/...) can't be filtered by the
# server, so we fetch the whole bounded `q` result set in max-size pages and
# filter locally. ps maxes at 1000 on the mirror; a few pages covers any artist.
FIELD_SEARCH_PS = 1000
FIELD_SEARCH_MAX_PAGES = 5
HTTP_TIMEOUT = 30
USER_AGENT = "osu-beatmap-downloader/1.0 (+personal use)"
# Some mirrors (catboy, beatconnect) gate non-browser clients; use a browser UA
# for the actual file downloads so those fallbacks work for e.g. graveyard maps.
DOWNLOAD_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0")

# Download mirrors, tried top-to-bottom. Each entry: name, full url, no-video url.
# {id} is substituted with the beatmapset id.
MIRRORS = [
    {"name": "nerinyan",   "full": "https://api.nerinyan.moe/d/{id}",                      "novideo": "https://api.nerinyan.moe/d/{id}?noVideo=true"},
    {"name": "catboy",     "full": "https://catboy.best/d/{id}",                           "novideo": "https://catboy.best/d/{id}?n=1"},
    {"name": "beatconnect","full": "https://beatconnect.io/b/{id}",                        "novideo": None},
    # Sayobot is China-hosted and slow/throttled from outside CN, so it's the last resort.
    {"name": "sayobot",    "full": "https://dl.sayobot.cn/beatmaps/download/full/{id}",    "novideo": "https://dl.sayobot.cn/beatmaps/download/novideo/{id}"},
]

MODES = [("Any", None), ("osu!", 0), ("taiko", 1), ("catch", 2), ("mania", 3)]
MODE_NAME = {"osu": "osu!", "taiko": "taiko", "fruits": "catch", "mania": "mania"}

STATUSES = [
    ("Any", "all"), ("Ranked", "ranked"), ("Qualified", "qualified"),
    ("Loved", "loved"), ("Pending", "pending"), ("WIP", "wip"),
    ("Graveyard", "graveyard"),
]

SORTS = [
    ("Ranked (newest)", "ranked_desc"),
    ("Ranked (oldest)", "ranked_asc"),
    ("Title (A-Z)", "title_asc"),
    ("Title (Z-A)", "title_desc"),
    ("Artist (A-Z)", "artist_asc"),
    ("Most played", "plays_desc"),
    ("Most favourited", "favourites_desc"),
    ("Recently updated", "updated_desc"),
]

# Which field(s) the text query matches. Maps to Nerinyan's `option` param;
# "" = all fields (relevance-less, so a bare mapper name pulls in tag matches),
# "creator" = mapper only -> the reliable way to find a mapper's maps.
SEARCH_FIELDS = [
    ("Everything", ""),
    ("Mapper", "creator"),
    ("Title", "title"),
    ("Artist", "artist"),
    ("Tags", "tag"),
]

# osu! genre / language ids (used by the hinamizawa mirror's genre=/language= params).
GENRES = [
    ("Any genre", 0), ("Video Game", 2), ("Anime", 3), ("Rock", 4), ("Pop", 5),
    ("Other", 6), ("Novelty", 7), ("Hip Hop", 9), ("Electronic", 10),
    ("Metal", 11), ("Classical", 12), ("Folk", 13), ("Jazz", 14),
    ("Unspecified", 1),
]
LANGUAGES = [
    ("Any language", 0), ("English", 2), ("Japanese", 3), ("Chinese", 4),
    ("Instrumental", 5), ("Korean", 6), ("French", 7), ("German", 8),
    ("Swedish", 9), ("Spanish", 10), ("Italian", 11), ("Russian", 12),
    ("Polish", 13), ("Other", 14), ("Unspecified", 1),
]
# Map our status strings <-> hinamizawa's numeric RankedStatus.
# Map our status strings to hinamizawa numeric RankedStatus codes. "Ranked"
# bundles ranked(1)+approved(2), matching how osu! and the mirror's own UI treat
# it (sent as a comma list); "Any" omits the param.
HINA_STATUS = {"all": [1, 2, 3, 4, 0, -1, -2],   # no "all" sentinel exists, so fan out
               "ranked": [1, 2], "qualified": [3], "loved": [4],
               "pending": [0], "wip": [-1], "graveyard": [-2]}
# The mirror's response RankedStatus is coarse/unreliable (only 0/1), so we tag
# each result with the status code we *queried* instead -- that's authoritative.
HINA_CODE_STATUS = {1: "ranked", 2: "approved", 3: "qualified", 4: "loved",
                    0: "pending", -1: "wip", -2: "graveyard"}
HINA_STATUS_REV = {1: "ranked", 2: "approved", 3: "qualified", 4: "loved",
                   0: "pending", -1: "wip", -2: "graveyard"}
# Our sort keys -> the mirror's `sort` values (it has no asc/desc variants).
HINA_SORT = {k: k for k in (        # the mirror takes "{field}_{asc|desc}" directly,
    "ranked_desc", "ranked_asc",    # which is exactly our SORTS key format, so each
    "title_asc", "title_desc",      # key passes through unchanged (real A-Z/Z-A and
    "artist_asc",                   # newest/oldest). Unknown keys are dropped.
    "plays_desc", "favourites_desc", "updated_desc")}
HINA_MODE = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}

# Preset ranges for the BPM / Stars dropdowns. Each value is (min, max); 0 = open.
BPM_RANGES = [
    ("Any BPM", (0, 0)),
    ("Under 120", (0, 120)),
    ("120 \u2013 150", (120, 150)),
    ("150 \u2013 180", (150, 180)),
    ("180 \u2013 200", (180, 200)),
    ("200 \u2013 240", (200, 240)),
    ("240+", (240, 0)),
]
LENGTH_RANGES = [
    ("Any length", (0, 0)),
    ("Under 1 min", (0, 60)),
    ("1 \u2013 2 min", (60, 120)),
    ("2 \u2013 3 min", (120, 180)),
    ("3 \u2013 5 min", (180, 300)),
    ("5 \u2013 7 min", (300, 420)),
    ("Over 7 min", (420, 0)),
]
STAR_RANGES = [
    ("Any difficulty", (0, 0)),
    ("Easy  \u00b7  0\u20132\u2605", (0, 2)),
    ("Normal  \u00b7  2\u20132.7\u2605", (2, 2.7)),
    ("Hard  \u00b7  2.7\u20134\u2605", (2.7, 4)),
    ("Insane  \u00b7  4\u20135.3\u2605", (4, 5.3)),
    ("Expert  \u00b7  5.3\u20136.5\u2605", (5.3, 6.5)),
    ("Expert+  \u00b7  6.5\u2605+", (6.5, 0)),
    ("7\u2605 and up", (7, 0)),
    ("8\u2605 and up", (8, 0)),
    ("9\u2605 and up", (9, 0)),
    ("10\u2605 and up", (10, 0)),
]

STATUS_COLORS = {
    "ranked": "#7ac74f", "approved": "#7ac74f", "loved": "#ff66aa",
    "qualified": "#3a7bd5", "pending": "#e0a23a", "wip": "#e0a23a",
    "graveyard": "#8a8a8a", "pack": "#ff66ab",
}


# ----------------------------------------------------------------------------
# DATA MODEL
# ----------------------------------------------------------------------------
@dataclass
class Diff:
    mode: str
    sr: float
    bpm: float
    length: int
    version: str


@dataclass
class Beatmapset:
    id: int
    title: str
    artist: str
    creator: str
    status: str
    bpm: float
    play_count: int
    favourite_count: int
    cover_url: str
    diffs: list = field(default_factory=list)
    minimal: bool = False   # built from a pack page (id + name only)
    tags: str = ""

    @property
    def sr_range(self) -> tuple:
        if not self.diffs:
            return (0.0, 0.0)
        srs = [d.sr for d in self.diffs]
        return (min(srs), max(srs))

    @property
    def length(self) -> int:
        return max((d.length for d in self.diffs), default=0)

    @property
    def modes(self) -> list:
        seen, out = set(), []
        for d in self.diffs:
            if d.mode not in seen:
                seen.add(d.mode)
                out.append(d.mode)
        return out

    @classmethod
    def from_json(cls, js: dict) -> "Beatmapset":
        sid = int(js.get("id", 0) or 0)
        covers = js.get("covers") or {}
        cover = (covers.get("card@2x") or covers.get("card")
                 or covers.get("cover") or covers.get("slimcover") or "")
        # Nerinyan often omits the covers object; osu!'s CDN serves cover art at a
        # predictable path keyed by set id, so build it ourselves as a fallback.
        if not cover and sid:
            variant = "card" + "@2x.jpg"   # = card@2x.jpg (split to avoid mangling)
            cover = f"https://assets.ppy.sh/beatmaps/{sid}/covers/{variant}"
        diffs = []
        for b in js.get("beatmaps", []) or []:
            diffs.append(Diff(
                mode=b.get("mode", "osu"),
                sr=float(b.get("difficulty_rating", 0) or 0),
                bpm=float(b.get("bpm", 0) or 0),
                length=int(b.get("total_length", 0) or 0),
                version=b.get("version", ""),
            ))
        diffs.sort(key=lambda d: d.sr)
        return cls(
            id=sid,
            title=js.get("title", "(unknown)"),
            artist=js.get("artist", ""),
            creator=js.get("creator", ""),
            status=str(js.get("status", "")).lower(),
            bpm=float(js.get("bpm", 0) or 0),
            play_count=int(js.get("play_count", 0) or 0),
            favourite_count=int(js.get("favourite_count", 0) or 0),
            cover_url=cover,
            diffs=diffs,
            tags=str(js.get("tags", "") or ""),
        )

    @classmethod
    def from_hinai(cls, js: dict) -> "Beatmapset":
        """Parse the hinamizawa mirror's CheeseGull-style set object. It carries
        no BPM or play/favourite counts, so those stay 0 (cards adapt)."""
        sid = int(js.get("SetID", 0) or 0)
        diffs = []
        for b in js.get("ChildrenBeatmaps", []) or []:
            diffs.append(Diff(
                mode=HINA_MODE.get(int(b.get("Mode", 0) or 0), "osu"),
                sr=float(b.get("DifficultyRating", 0) or 0),
                bpm=0.0,
                length=int(b.get("TotalLength", 0) or 0),
                version=b.get("DiffName", ""),
            ))
        diffs.sort(key=lambda d: d.sr)
        variant = "card" + "@2x.jpg"
        return cls(
            id=sid,
            title=js.get("Title", "(unknown)"),
            artist=js.get("Artist", ""),
            creator=js.get("Creator", ""),
            status=HINA_STATUS_REV.get(int(js.get("RankedStatus", 0) or 0), ""),
            bpm=0.0, play_count=0, favourite_count=0,
            cover_url=f"https://assets.ppy.sh/beatmaps/{sid}/covers/{variant}",
            diffs=diffs, tags="",
        )

    @classmethod
    def from_pack(cls, sid: int, name: str) -> "Beatmapset":
        """Lightweight set built from a pack page (only id + 'Artist - Title')."""
        artist, _, title = name.partition(" - ")
        if not title:
            artist, title = "", name
        variant = "card" + "@2x.jpg"
        return cls(
            id=sid, title=title.strip(), artist=artist.strip(), creator="",
            status="pack", bpm=0, play_count=0, favourite_count=0,
            cover_url=f"https://assets.ppy.sh/beatmaps/{sid}/covers/{variant}",
            diffs=[], minimal=True,
        )


# ----------------------------------------------------------------------------
# API CLIENT
# ----------------------------------------------------------------------------
def _field_rank(value: str, query: str) -> int:
    """Match quality of `query` against a field `value`, lower = closer:
      0  exact            (artist == "xi")
      1  field starts with the whole query   ("xi feat. ...")
      2  query tokens are whole words in the field
      3  loose: each token only prefixes some word ("xi" -> "xiao")
     -1  no match
    Used to order field-scoped results so the exact artist leads and incidental
    matches (a word merely starting with the query) sink to the end."""
    v = (value or "").lower().strip()
    q = query.lower().strip()
    if not q or v == q:
        return 0
    if v.startswith(q):
        return 1
    vwords = re.findall(r"[0-9a-z]+", v)
    qtoks = re.findall(r"[0-9a-z]+", q)
    if not qtoks:
        return 0
    if all(t in vwords for t in qtoks):
        return 2
    if all(any(w.startswith(t) for w in vwords) for t in qtoks):
        return 3
    return -1


def _field_match(value: str, query: str) -> bool:
    """True if `query` matches `value` at all (any quality tier)."""
    return _field_rank(value, query) >= 0


_FIELD_GETTERS = {
    "title": lambda s: s.title,
    "artist": lambda s: s.artist,
    "creator": lambda s: s.creator,
    "tag": lambda s: s.tags,
}


def passes_range(s: "Beatmapset", f: dict) -> bool:
    """Client-side BPM / star-rating / length range filter (Nerinyan's GET API
    has no query param for these). A set matches if ANY of its difficulties falls
    in range -- same semantics as the site's server-side filter."""
    if f["bpm_min"] or f["bpm_max"]:
        bpms = [d.bpm for d in s.diffs if d.bpm] or ([s.bpm] if s.bpm else [])
        if bpms:
            if f["bpm_min"] and max(bpms) < f["bpm_min"]:
                return False
            if f["bpm_max"] and min(bpms) > f["bpm_max"]:
                return False
    if f["sr_min"] or f["sr_max"]:
        diffs = s.diffs
        if f["mode"] is not None:
            mode_str = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}[f["mode"]]
            diffs = [d for d in diffs if d.mode == mode_str] or s.diffs
        srs = [d.sr for d in diffs] or [0.0]
        if f["sr_min"] and max(srs) < f["sr_min"]:
            return False
        if f["sr_max"] and min(srs) > f["sr_max"]:
            return False
    if f.get("len_min") or f.get("len_max"):
        lengths = [d.length for d in s.diffs if d.length]
        if lengths:                      # keep sets with no length data
            if f.get("len_min") and max(lengths) < f["len_min"]:
                return False
            if f.get("len_max") and min(lengths) > f["len_max"]:
                return False
    return True


def _has_client_filter(f: dict) -> bool:
    """Whether this search is narrowed in the client (BPM / star / length range,
    or a 'Search in' field scope) and therefore benefits from the larger page."""
    return bool(f.get("bpm_min") or f.get("bpm_max") or f.get("sr_min")
                or f.get("sr_max") or f.get("len_min") or f.get("len_max")
                or (f.get("option") and (f.get("q") or "").strip()))


def _fetch_search_page(base: dict, page: int, ps: int) -> list:
    params = dict(base, p=page, ps=ps)
    r = requests.get(NERINYAN_SEARCH, params=params,
                     headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("beatmapsets", [])


def fetch_card_meta(set_id: int) -> tuple:
    """Fetch full osu!-web metadata for one set from osu.direct (BPM, play and
    favourite counts, accurate per-diff data) to enrich a hinamizawa card.
    Returns (set_id, Beatmapset)."""
    r = requests.get(OSU_DIRECT_SET.format(id=set_id),
                     headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError(f"no metadata for set {set_id}")
    return set_id, Beatmapset.from_json(data)


def search_beatmapsets(filters: dict, token) -> tuple:
    """Search via the hinamizawa mirror (complete index, clean relevance, genre/
    language/sort/status filters), falling back to Nerinyan only if it errors so
    the app keeps working."""
    try:
        return _search_hinamizawa(filters, token)
    except Exception:
        if token is None:                  # fall back only on a fresh search; a
            return _search_nerinyan(filters, None)   # paging token isn't portable
        return [], None


def _hina_get(params: dict, status_code=None) -> list:
    """Fetch one page and parse to Beatmapsets. If a status_code is given, it's
    sent as the `status` filter AND used to tag each result (the response's
    RankedStatus field is unreliable, so the queried code is authoritative)."""
    if status_code is not None:
        params = dict(params, status=status_code)
    r = requests.get(HINA_SEARCH, params=params,
                     headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    raw = data if isinstance(data, list) else []
    sets = [Beatmapset.from_hinai(s) for s in raw]
    if status_code is not None:
        label = HINA_CODE_STATUS.get(status_code, "")
        for s in sets:
            s.status = label
    return sets


def _client_sort(sets: list, sort_key: str) -> list:
    """Sort a field-scoped result set locally. Only title/artist are reliably
    sortable here (play counts/dates aren't in the response), so other sorts keep
    the relevance/exactness order they already have."""
    if sort_key == "title_asc":
        return sorted(sets, key=lambda s: s.title.lower())
    if sort_key == "title_desc":
        return sorted(sets, key=lambda s: s.title.lower(), reverse=True)
    if sort_key == "artist_asc":
        return sorted(sets, key=lambda s: s.artist.lower())
    return sets


def _search_hinamizawa(filters: dict, token) -> tuple:
    """Query the hinamizawa mirror and return (list[Beatmapset], next_token|None).

    The mirror has no "all statuses" option and returns nothing for a text search
    with no status, so "Any" fans out across every status code and merges. Each
    result is tagged with the status we *queried* (its RankedStatus field only
    reports 0/1). Field-scoped searches (Artist/Title/Mapper) are fetched by
    relevance so the whole catalogue surfaces, then sorted locally for display;
    browses page server-side via `offset`. BPM/play counts aren't in the response
    (cards enrich those from osu.direct), so BPM ranges filter server-side and
    stars/length filter locally.
    """
    offset = 0 if token is None else int(token)
    q = filters["q"].strip()
    base = {"query": q, "amount": HINA_AMOUNT}
    if filters.get("mode") is not None:
        base["mode"] = filters["mode"]
    if filters.get("genre"):
        base["genre"] = filters["genre"]
    if filters.get("language"):
        base["language"] = filters["language"]
    if filters.get("bpm_min"):                       # bpm not in the response, so
        base["min_bpm"] = filters["bpm_min"]         # ranges filter server-side
    if filters.get("bpm_max"):
        base["max_bpm"] = filters["bpm_max"]

    codes = HINA_STATUS.get(filters.get("status")) or [1]   # default to ranked-tier
    getter = _FIELD_GETTERS.get(filters.get("option") or "")
    field_scope = bool(q and getter and filters.get("option") != "tag")

    if field_scope:
        # Fetch every requested status by relevance (no sort, no paging) so the
        # artist's full catalogue clusters in; merge, keep matches, sort locally.
        sets, have = [], set()
        for c in codes:
            for s in _hina_get(dict(base), c):
                if s.id not in have:
                    have.add(s.id)
                    sets.append(s)
        ranked = [(rk, s) for s in sets
                  for rk in (_field_rank(getter(s), q),) if rk >= 0]
        ranked.sort(key=lambda t: t[0])
        sets = _client_sort([s for _, s in ranked], filters.get("sort"))
        next_token = None
    else:
        # Browse: the primary status code paginates via offset; any extra codes
        # (e.g. approved under "Ranked", or all of them under "Any") merge on page 1.
        sort = HINA_SORT.get(filters.get("sort"))
        if sort:
            base["sort"] = sort
        raw = _hina_get(dict(base, offset=offset), codes[0])
        sets, have = list(raw), {s.id for s in raw}
        if len(codes) > 1 and offset == 0:
            for c in codes[1:]:
                for s in _hina_get(dict(base), c):
                    if s.id not in have:
                        have.add(s.id)
                        sets.append(s)
        next_token = offset + HINA_AMOUNT if len(raw) >= HINA_AMOUNT else None

    # stars + length client-side (bpm is filtered server-side; bpm fields are 0)
    if any(filters.get(k) for k in ("sr_min", "sr_max", "len_min", "len_max")):
        sets = [s for s in sets if passes_range(s, filters)]
    return sets, next_token


def _search_nerinyan(filters: dict, token) -> tuple:
    """Fallback backend (Nerinyan, GET) -> (list[Beatmapset], next_token|None).

    Used only when the hinamizawa mirror is unreachable. The deployed mirror
    ignores the per-field `option` param (it silently disables text filtering),
    so a "Search in" field scope is done client-side. An artist's maps are
    scattered through a noisy `q` result by title, so for a field-scoped search we
    pull the *whole* (bounded) `q` result set in max-size pages and filter + rank
    locally. BPM/star/length ranges have no query param either and are applied here.
    """
    base = {
        "q": filters["q"].strip(),
        "s": "all" if filters["status"] in (None, "", "any") else filters["status"],
        "sort": filters["sort"],
    }
    if filters["mode"] is not None:
        base["m"] = filters["mode"]

    q = filters["q"].strip()
    getter = _FIELD_GETTERS.get(filters.get("option") or "")
    range_filter = any(filters.get(k) for k in ("bpm_min", "bpm_max", "sr_min",
                                                "sr_max", "len_min", "len_max"))

    if q and getter:
        # Pull the full result set for this query (capped), tolerating the server
        # clamping `ps` to its own max -- we detect the real page size and stop at
        # the last (short) page rather than assuming a fixed size.
        raw, page_size = [], None
        for p in range(FIELD_SEARCH_MAX_PAGES):
            page = _fetch_search_page(base, p, FIELD_SEARCH_PS)
            if not page:
                break
            raw.extend(page)
            if page_size is None:
                page_size = len(page)
            if len(page) < page_size:      # last, partial page
                break
        sets = [Beatmapset.from_json(s) for s in raw]
        ranked = [(r, s) for s in sets for r in (_field_rank(getter(s), q),) if r >= 0]
        ranked.sort(key=lambda t: t[0])    # stable: keeps the chosen sort within a tier
        sets = [s for _, s in ranked]
        if range_filter:
            sets = [s for s in sets if passes_range(s, filters)]
        return sets, None                  # complete set; no further paging

    # Non-field search: one page at a time (range filters use a bigger page so a
    # rare match isn't stranded), with normal scroll pagination.
    page = 0 if token is None else int(token)
    ps = FILTER_PAGE_SIZE if range_filter else PAGE_SIZE
    raw = _fetch_search_page(base, page, ps)
    sets = [Beatmapset.from_json(s) for s in raw]
    if range_filter:
        sets = [s for s in sets if passes_range(s, filters)]
    next_token = page + 1 if len(raw) >= ps else None
    return sets, next_token


# ----------------------------------------------------------------------------
# osu! LIBRARY DETECTION
# ----------------------------------------------------------------------------
def candidate_songs_dirs() -> list:
    home = Path.home()
    cands = [
        home / ".local/share/osu-wine/osu!/Songs",
        home / ".local/share/osu-wine/OSU/Songs",
        home / ".local/share/osu/Songs",
        home / "Games/osu/drive_c/users" ,  # lutris-ish, scanned shallowly below
        Path(os.environ.get("LOCALAPPDATA", "")) / "osu!/Songs",
        home / "AppData/Local/osu!/Songs",
        home / "Library/Application Support/osu/Songs",
    ]
    found = []
    for c in cands:
        try:
            if c.is_dir():
                found.append(c)
        except OSError:
            pass
    return found


def scan_downloaded_ids(songs_dir: str) -> set:
    """Beatmap folders / .osz files are named '<setid> Artist - Title'."""
    ids = set()
    if not songs_dir:
        return ids
    p = Path(songs_dir)
    if not p.is_dir():
        return ids
    try:
        for entry in p.iterdir():
            m = re.match(r"^(\d+)\b", entry.name)
            if m:
                ids.add(int(m.group(1)))
    except OSError:
        pass
    return ids


def load_history(path: Path) -> set:
    """Load the persistent set of set-ids downloaded through this app."""
    try:
        return {int(x) for x in json.loads(Path(path).read_text())}
    except Exception:  # noqa: BLE001 - missing/corrupt file is fine
        return set()


def save_history(path: Path, ids: set):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(sorted(ids)))
    except OSError:
        pass


# ----------------------------------------------------------------------------
# MEDAL PACKS  +  osu!stable collection.db
# ----------------------------------------------------------------------------
# collection.db layout (osu!stable):
#   int32 version
#   int32 collection_count
#   per collection:  osu-string name,  int32 map_count,  map_count x osu-string md5
# osu-string: 0x00 (null) OR 0x0b + ULEB128 length + UTF-8 bytes.
DEFAULT_DB_VERSION = 20231101


def _read_uleb128(buf: bytes, pos: int):
    val = shift = 0
    while True:
        b = buf[pos]; pos += 1
        val |= (b & 0x7f) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7


def _write_uleb128(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_osu_string(buf: bytes, pos: int):
    kind = buf[pos]; pos += 1
    if kind == 0x00:
        return "", pos
    if kind != 0x0b:
        raise ValueError(f"bad osu string marker {kind:#x} at {pos - 1}")
    length, pos = _read_uleb128(buf, pos)
    s = buf[pos:pos + length].decode("utf-8", "replace")
    return s, pos + length


def _write_osu_string(s: str) -> bytes:
    if s is None:
        return b"\x00"
    data = s.encode("utf-8")
    return b"\x0b" + _write_uleb128(len(data)) + data


def read_collection_db(path: Path):
    """Return (version, [(name, [md5, ...]), ...]). ([] if file missing/unreadable)."""
    try:
        buf = Path(path).read_bytes()
    except OSError:
        return DEFAULT_DB_VERSION, []
    pos = 0
    version = int.from_bytes(buf[pos:pos + 4], "little"); pos += 4
    count = int.from_bytes(buf[pos:pos + 4], "little"); pos += 4
    collections = []
    for _ in range(count):
        name, pos = _read_osu_string(buf, pos)
        n = int.from_bytes(buf[pos:pos + 4], "little"); pos += 4
        hashes = []
        for _ in range(n):
            h, pos = _read_osu_string(buf, pos)
            hashes.append(h)
        collections.append((name, hashes))
    return version, collections


def write_collection_db(path: Path, version: int, collections):
    out = bytearray()
    out += int(version).to_bytes(4, "little")
    out += len(collections).to_bytes(4, "little")
    for name, hashes in collections:
        out += _write_osu_string(name)
        out += len(hashes).to_bytes(4, "little")
        for h in hashes:
            out += _write_osu_string(h)
    Path(path).write_bytes(bytes(out))


def md5s_from_osz(osz_path) -> list:
    """MD5 (hex) of every .osu difficulty inside an .osz - matches osu!'s map hashes."""
    import zipfile
    hashes = []
    try:
        with zipfile.ZipFile(osz_path) as z:
            for name in z.namelist():
                if name.lower().endswith(".osu"):
                    hashes.append(hashlib.md5(z.read(name)).hexdigest())
    except (zipfile.BadZipFile, OSError):
        pass
    return hashes


def upsert_collection(db_path: Path, name: str, hashes: list) -> str:
    """Create/replace a named collection in collection.db (backing up first).

    Returns a short status string. Designed to run with osu! closed.
    """
    db_path = Path(db_path)
    version, collections = read_collection_db(db_path)
    if db_path.exists():
        backup = db_path.with_suffix(db_path.suffix + ".bak")
        try:
            backup.write_bytes(db_path.read_bytes())
        except OSError:
            pass
    # de-dupe while preserving order
    seen, uniq = set(), []
    for h in hashes:
        if h and h not in seen:
            seen.add(h); uniq.append(h)
    collections = [(n, h) for (n, h) in collections if n != name]
    collections.append((name, uniq))
    write_collection_db(db_path, version or DEFAULT_DB_VERSION, collections)
    return f"{len(uniq)} maps in collection \u201c{name}\u201d"


def default_collection_db_path(songs_dir: str) -> Path:
    """collection.db lives in the osu! root, i.e. the parent of the Songs folder."""
    p = Path(songs_dir) if songs_dir else Path.home()
    return p.parent / "collection.db"


_MEDAL_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")
_PACK_TAG_RE = re.compile(r"/beatmaps/packs/([A-Za-z0-9]+)")


def parse_pack_medals(markdown: str) -> list:
    """Parse the wiki table into [{'medal': str, 'tags': [str, ...]}]."""
    medals = []
    for line in markdown.splitlines():
        m = _MEDAL_ROW_RE.match(line)
        if not m:
            continue
        medal, req = m.group(1), m.group(2)
        if medal.lower().startswith("medal name") or set(medal) <= {"-", ":", " "}:
            continue
        tags = _PACK_TAG_RE.findall(req)
        if tags:
            medals.append({"medal": medal.replace("\\", ""), "tags": tags})
    return medals


# Each pack entry looks like:
#   <a href="...beatmapsets/123" class="beatmap-pack-items__link">
#     <span class="beatmap-pack-items__artist">Artist</span>
#     <span class="beatmap-pack-items__title"> - Title</span></a>
_PACK_SET_RE = re.compile(
    r'/beatmapsets/(\d+)"[^>]*class="beatmap-pack-items__link"[^>]*>'
    r'\s*<span class="beatmap-pack-items__artist">([^<]*)</span>'
    r'\s*<span class="beatmap-pack-items__title">([^<]*)</span>', re.S)
_PACK_ID_RE = re.compile(r'/beatmapsets/(\d+)')


def parse_pack_page(html_text: str) -> list:
    """Parse a pack page into [(set_id, 'Artist - Title'), ...] (deduped, ordered).
    The title span already carries the ' - ' separator, so artist+title rebuilds
    the familiar 'Artist - Title' string. Falls back to ids-only if the markup
    ever changes (covers still load; names just stay blank)."""
    import html as _html
    out, seen = [], set()
    for sid, artist, title in _PACK_SET_RE.findall(html_text):
        sid = int(sid)
        if sid in seen:
            continue
        seen.add(sid)
        out.append((sid, _html.unescape((artist + title).strip())))
    if out:
        return out
    for raw in _PACK_ID_RE.findall(html_text):     # fallback: ids only
        sid = int(raw)
        if sid not in seen:
            seen.add(sid)
            out.append((sid, ""))
    return out


_PACK_LIST_RE = re.compile(
    r'data-pack-tag="([^"]+)"\s*>\s*<a[^>]*class="beatmap-pack__header[^"]*"[^>]*>\s*'
    r'<div class="beatmap-pack__name">([^<]*)</div>\s*'
    r'<div class="beatmap-pack__details">\s*'
    r'<span class="beatmap-pack__date">([^<]*)</span>', re.S)
_PACK_LIST_FALLBACK_RE = re.compile(
    r'data-pack-tag="([^"]+)".*?beatmap-pack__name">([^<]*)<', re.S)


def _pack_mode(name: str) -> str:
    """Best-effort game mode from a pack name (the Standard category mixes modes,
    e.g. 'osu!taiko Beatmap Pack #410'). '' when the name gives no hint."""
    n = name.lower()
    if "osu!taiko" in n:
        return "taiko"
    if "osu!catch" in n or "osu!fruits" in n:
        return "fruits"
    if "osu!mania" in n:
        return "mania"
    if "osu!" in n:
        return "osu"
    return ""


def parse_pack_list(html_text: str) -> list:
    """Parse a pack listing page into [{'tag','name','date','mode'}, ...]."""
    import html as _html
    out = []
    for tag, name, date in _PACK_LIST_RE.findall(html_text):
        nm = _html.unescape(name.strip())
        out.append({"tag": tag, "name": nm, "date": date.strip(), "mode": _pack_mode(nm)})
    if not out:                                   # markup changed: tag + name only
        for tag, name in _PACK_LIST_FALLBACK_RE.findall(html_text):
            nm = _html.unescape(name.strip())
            out.append({"tag": tag, "name": nm, "date": "", "mode": _pack_mode(nm)})
    return out


def fetch_pack_list(pack_type: str, page: int) -> list:
    """Fetch one listing page (100 packs, newest first). Empty list = past the end."""
    url = PACK_LIST_URL.format(type=pack_type, page=page)
    r = requests.get(url, headers={"User-Agent": DOWNLOAD_UA}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return parse_pack_list(r.text)


def fetch_most_played(username: str, limit: int) -> tuple:
    """Resolve a username/ID and return (username, [Beatmapset, ...]) for their
    most-played maps, deduped to beatmapsets and ordered by total play count.
    Uses the public profile JSON route -- no login or API key."""
    prof = requests.get(USER_PROFILE_URL.format(user=username),
                        headers={"User-Agent": DOWNLOAD_UA}, timeout=HTTP_TIMEOUT)
    prof.raise_for_status()
    m = re.search(r"/users/(\d+)", prof.url)
    if not m:
        raise RuntimeError(f"couldn't find user '{username}'")
    uid = m.group(1)
    hdr = {"User-Agent": DOWNLOAD_UA, "Accept": "application/json",
           "X-Requested-With": "XMLHttpRequest"}
    sets, order, scanned, offset = {}, [], 0, 0
    while scanned < limit:
        n = min(MOST_PLAYED_PAGE, limit - scanned)
        rr = requests.get(MOST_PLAYED_URL.format(id=uid, limit=n, offset=offset),
                          headers=hdr, timeout=HTTP_TIMEOUT)
        rr.raise_for_status()
        batch = rr.json()
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            bs = item.get("beatmapset")
            if not isinstance(bs, dict) or not bs.get("id"):
                continue
            sid = bs["id"]
            cnt = int(item.get("count", 0) or 0)
            if sid in sets:                      # same set, another difficulty
                sets[sid]["count"] += cnt
            else:
                sets[sid] = {"set": Beatmapset.from_json(bs), "count": cnt}
                order.append(sid)
        scanned += len(batch)
        offset += len(batch)
        if len(batch) < n:                       # reached the end of their plays
            break
    order.sort(key=lambda sid: -sets[sid]["count"])
    return username, [sets[sid]["set"] for sid in order]


def fetch_pack_medals() -> list:
    """Download the wiki table and return the medal -> pack-tag list."""
    r = requests.get(MEDAL_WIKI_URL, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    medals = parse_pack_medals(r.text)
    if not medals:
        raise RuntimeError("could not parse the medal list")
    return medals


def fetch_pack_contents(tags: list) -> list:
    """Return the combined, deduped [(set_id, name)] across one or more pack tags."""
    out, seen = [], set()
    for tag in tags:
        url = PACK_PAGE_URL.format(tag=tag)
        # browser UA: the pack pages are public but reject obvious bots
        r = requests.get(url, headers={"User-Agent": DOWNLOAD_UA}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        for sid, name in parse_pack_page(r.text):
            if sid not in seen:
                seen.add(sid)
                out.append((sid, name))
    if not out:
        raise RuntimeError("no beatmaps found for this pack")
    return out


def fetch_set_meta(set_id: int, name: str) -> tuple:
    """Best-effort full metadata for one pack map. The mirror has no per-set
    endpoint, so we run the normal text search for the map's name and match the
    exact set id in the results. Returns (set_id, Beatmapset); raises if the set
    doesn't surface (the card then keeps its artist/title from the pack page)."""
    if not name:
        raise RuntimeError("no name to search")
    f = {"q": name, "status": "all", "sort": "title_asc", "mode": None,
         "option": "", "bpm_min": 0, "bpm_max": 0, "sr_min": 0, "sr_max": 0,
         "hide_owned": False, "no_video": False}
    sets, _ = search_beatmapsets(f, None)
    for s in sets:
        if s.id == set_id:
            return set_id, s
    raise RuntimeError(f"set {set_id} not found via search")


# ----------------------------------------------------------------------------
# WORKERS
# ----------------------------------------------------------------------------
class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    """Run any callable off the GUI thread."""
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"{type(e).__name__}: {e}")
        else:
            self.signals.result.emit(res)


class ImageSignals(QObject):
    done = Signal(int, bytes)  # setid, image bytes


class ImageWorker(QRunnable):
    def __init__(self, setid: int, url: str, cache_dir: Path):
        super().__init__()
        self.setid, self.url, self.cache_dir = setid, url, cache_dir
        self.signals = ImageSignals()

    @Slot()
    def run(self):
        if not self.url:
            return
        cache_file = self.cache_dir / f"{self.setid}.img"
        try:
            if cache_file.exists():
                self.signals.done.emit(self.setid, cache_file.read_bytes())
                return
            r = requests.get(self.url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            if r.status_code == 200 and r.content:
                try:
                    cache_file.write_bytes(r.content)
                except OSError:
                    pass
                self.signals.done.emit(self.setid, r.content)
        except Exception:  # noqa: BLE001
            pass


class DownloadSignals(QObject):
    progress = Signal(int, int, str)   # setid, percent (-1 = unknown), status text
    done = Signal(int, str)            # setid, filepath
    failed = Signal(int, str)          # setid, error


class DownloadWorker(QRunnable):
    def __init__(self, s: Beatmapset, dest_dir: str, no_video: bool, mirrors: list):
        super().__init__()
        self.s = s
        self.dest_dir = dest_dir
        self.no_video = no_video
        self.mirrors = mirrors
        self.signals = DownloadSignals()
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    @Slot()
    def run(self):
        sid = self.s.id
        fname = sanitize_filename(f"{sid} {self.s.artist} - {self.s.title}.osz")
        dest_dir = Path(self.dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / fname
        # Stage the partial file *inside* the destination dir so the final move
        # is a same-filesystem atomic rename (avoids cross-device errors when the
        # system temp dir is a tmpfs and the download folder is on disk).
        tmp = dest_dir / (fname + ".part")
        last_err = "no mirror succeeded"

        for mirror in self.mirrors:
            if self.cancelled:
                self.signals.failed.emit(sid, "cancelled")
                return
            url = mirror["novideo"] if (self.no_video and mirror["novideo"]) else mirror["full"]
            url = url.format(id=sid)
            self.signals.progress.emit(sid, -1, f"trying {mirror['name']}\u2026")
            try:
                with requests.get(url, headers={"User-Agent": DOWNLOAD_UA},
                                  stream=True, timeout=HTTP_TIMEOUT, allow_redirects=True) as r:
                    ctype = r.headers.get("Content-Type", "").lower()
                    if r.status_code != 200 or "text/html" in ctype or "json" in ctype:
                        last_err = f"{mirror['name']} HTTP {r.status_code}"
                        continue
                    total = int(r.headers.get("Content-Length", 0) or 0)
                    got = 0
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(chunk_size=65536):
                            if self.cancelled:
                                fh.close()
                                tmp.unlink(missing_ok=True)
                                self.signals.failed.emit(sid, "cancelled")
                                return
                            if chunk:
                                fh.write(chunk)
                                got += len(chunk)
                                pct = int(got * 100 / total) if total else -1
                                self.signals.progress.emit(sid, pct, mirror["name"])
                    if got < 1024:  # almost certainly an error page
                        last_err = f"{mirror['name']} returned {got} bytes"
                        tmp.unlink(missing_ok=True)
                        continue
                    tmp.replace(dest)
                    self.signals.done.emit(sid, str(dest))
                    return
            except Exception as e:  # noqa: BLE001
                last_err = f"{mirror['name']}: {e}"
                continue

        self.signals.failed.emit(sid, last_err)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name[:200]


def fmt_len(seconds: int) -> str:
    if not seconds:
        return "?:??"
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ----------------------------------------------------------------------------
# FLOW LAYOUT (responsive wrapping grid)  -- adapted from the Qt examples
# ----------------------------------------------------------------------------
class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=8, spacing=10):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        left = rect.x() + m.left()
        avail = rect.width() - m.left() - m.right()
        sp = self._spacing
        if avail <= 0 or not self._items:
            return m.top() + m.bottom()

        # Uniform responsive grid: pick a column count from the cards' min width,
        # then give every card the SAME width that fills a full row. A partial
        # last row keeps that width and left-aligns (no stretching to fill).
        base_w = max(it.sizeHint().width() for it in self._items)
        cols = max(1, int((avail + sp) // (base_w + sp)))
        card_w = (avail - (cols - 1) * sp) / cols

        y = rect.y() + m.top()
        x = float(left)
        col = 0
        row_h = 0
        for it in self._items:
            if col == cols:                      # wrap to next row
                x = float(left)
                y += row_h + sp
                col, row_h = 0, 0
            h = it.sizeHint().height()
            if not test_only:
                it.setGeometry(QRect(int(round(x)), int(round(y)),
                                     int(round(card_w)), int(h)))
            x += card_w + sp
            row_h = max(row_h, h)
            col += 1
        y += row_h
        return int(y - rect.y() + m.bottom())


# ----------------------------------------------------------------------------
# BEATMAP CARD
# ----------------------------------------------------------------------------
class BeatmapCard(QFrame):
    CARD_W = 320
    COVER_H = 128
    CARD_H = 296

    previewRequested = Signal(int)
    downloadRequested = Signal(object)

    def __init__(self, s: Beatmapset, downloaded: bool):
        super().__init__()
        self.s = s
        self.setObjectName("card")
        self.setFixedHeight(self.CARD_H)
        self.setMinimumWidth(self.CARD_W)        # min width; grid stretches wider to fill rows
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._cover_pix = None                   # original loaded pixmap (for rescaling)
        self._raw_title = s.title
        self._raw_artist = s.artist

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 10)
        root.setSpacing(7)

        # --- cover (hero) with overlaid badges ---
        self.cover = QLabel()
        self.cover.setFixedHeight(self.COVER_H)  # width follows the (flexible) card width
        self.cover.setObjectName("cover")
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setText("\u266a")
        root.addWidget(self.cover)

        color = STATUS_COLORS.get(s.status, "#8a8a8a")
        self.status_badge = QLabel(s.status or "?", self.cover)
        self.status_badge.setStyleSheet(
            f"background:{color}; color:#15151a; border-radius:5px;"
            "padding:2px 8px; font-size:11px; font-weight:700;")
        self.status_badge.adjustSize()
        self.status_badge.move(8, 8)
        self.status_badge.raise_()

        lo, hi = s.sr_range
        sr_text = f"\u2605 {lo:.1f}" if abs(hi - lo) < 0.05 else f"\u2605 {lo:.1f}\u2013{hi:.1f}"
        self.sr_badge = QLabel(sr_text, self.cover)
        self.sr_badge.setStyleSheet(
            "background:rgba(18,18,24,0.82); color:#ffd45e; border-radius:5px;"
            "padding:2px 8px; font-size:11px; font-weight:700;")
        self.sr_badge.adjustSize()
        self.sr_badge.move(self.CARD_W - self.sr_badge.width() - 8, 8)
        self.sr_badge.raise_()
        if s.minimal:
            self.sr_badge.hide()

        # --- info block (labels always present; filled from whatever we have) ---
        body = QVBoxLayout()
        body.setContentsMargins(13, 1, 13, 0)
        body.setSpacing(4)

        self.title_lbl = QLabel()
        self.title_lbl.setObjectName("title")
        body.addWidget(self.title_lbl)

        self.artist_lbl = QLabel()
        self.artist_lbl.setObjectName("meta")
        body.addWidget(self.artist_lbl)

        self.stats_lbl = QLabel()
        self.stats_lbl.setObjectName("stats")
        body.addWidget(self.stats_lbl)

        self.sub_lbl = QLabel()
        self.sub_lbl.setObjectName("sub")
        body.addWidget(self.sub_lbl)
        root.addLayout(body)

        self._in_pack = s.minimal     # this card belongs to a medal pack
        self._fill_text(s)

        root.addStretch(1)

        # --- actions ---
        btns = QHBoxLayout()
        btns.setContentsMargins(11, 0, 11, 0)
        btns.setSpacing(6)

        self.preview_btn = QToolButton()
        self.preview_btn.setText("\u25b6")
        self.preview_btn.setToolTip("Preview audio")
        self.preview_btn.setObjectName("circbtn")
        self.preview_btn.clicked.connect(lambda: self.previewRequested.emit(s.id))
        btns.addWidget(self.preview_btn)

        self.dl_btn = QPushButton("Download")
        self.dl_btn.setObjectName("dlbtn")
        self.dl_btn.clicked.connect(lambda: self.downloadRequested.emit(self.s))
        btns.addWidget(self.dl_btn, 1)

        web_btn = QToolButton()
        web_btn.setText("\u2197")
        web_btn.setToolTip("Open on osu! website")
        web_btn.setObjectName("circbtn")
        web_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(WEB_SET_URL.format(id=s.id))))
        btns.addWidget(web_btn)

        root.addLayout(btns)
        self.set_downloaded(downloaded)

    def _fill_text(self, s: Beatmapset):
        """Populate title / artist / stats / sub from a set. For a pack card with
        no metadata yet, stats is blank and sub shows the pack hint."""
        self._raw_title = s.title
        self._raw_artist = s.artist
        avail = max(self.width(), self.CARD_W) - 28
        self.title_lbl.setText(elide(s.title, avail, bold=True, px=17))
        self.title_lbl.setToolTip(f"{s.artist} - {s.title}" if s.artist else s.title)
        self.artist_lbl.setText(elide(s.artist, avail, px=14))
        if s.minimal:
            self.stats_lbl.setText("")
            self.sub_lbl.setText("part of this pack")
            return
        modes = " ".join(MODE_NAME.get(m, m) for m in s.modes)
        stats = []
        if s.bpm:                              # hinamizawa results carry no BPM
            stats.append(f"\u266a {int(s.bpm)} BPM")
        if s.length:
            stats.append(f"\u23f1 {fmt_len(s.length)}")
        if modes:
            stats.append(modes)
        self.stats_lbl.setText("   \u00b7   ".join(stats))
        sub = f"mapped by {elide(s.creator, 116, px=13)}"
        if s.play_count or s.favourite_count:  # absent on hinamizawa
            sub += (f"   \u00b7   \u25b6 {fmt_count(s.play_count)}"
                    f"   \u2665 {fmt_count(s.favourite_count)}")
        if self._in_pack:
            sub += "   \u00b7   in pack"
        self.sub_lbl.setText(sub)

    def apply_full(self, full: Beatmapset):
        """Upgrade a minimal pack card in place once full metadata arrives:
        real status colour, star badge, mapper and play/favourite stats."""
        self.s = full
        color = STATUS_COLORS.get(full.status, "#8a8a8a")
        self.status_badge.setText(full.status or "?")
        self.status_badge.setStyleSheet(
            f"background:{color}; color:#15151a; border-radius:5px;"
            "padding:2px 8px; font-size:11px; font-weight:700;")
        self.status_badge.adjustSize()
        self.status_badge.move(8, 8)

        lo, hi = full.sr_range
        self.sr_badge.setText(f"\u2605 {lo:.1f}" if abs(hi - lo) < 0.05
                              else f"\u2605 {lo:.1f}\u2013{hi:.1f}")
        self.sr_badge.adjustSize()
        self.sr_badge.move(max(self.width(), self.CARD_W) - self.sr_badge.width() - 8, 8)
        self.sr_badge.show()
        self.sr_badge.raise_()
        self.status_badge.raise_()

        self._in_pack = True
        self._fill_text(full)

    def set_downloaded(self, yes: bool):
        self.dl_btn.setEnabled(True)
        self.dl_btn.setText("\u2713 In library" if yes else "Download")
        self.setProperty("owned", yes)
        # Re-polish the button too: the green "In library" style comes from the
        # `#card[owned="true"] #dlbtn` descendant rule, and re-polishing only the
        # card doesn't refresh the child, so a freshly downloaded map would keep
        # its pink button. Polishing both keeps every in-library button identical.
        for w in (self, self.dl_btn):
            w.style().unpolish(w)
            w.style().polish(w)

    def mark_queued(self):
        self.dl_btn.setEnabled(False)
        self.dl_btn.setText("Queued\u2026")

    def mark_downloading(self):
        self.dl_btn.setEnabled(False)
        self.dl_btn.setText("Downloading\u2026")

    def mark_failed(self):
        self.dl_btn.setEnabled(True)
        self.dl_btn.setText("Retry")

    def set_preview_playing(self, playing: bool):
        self.preview_btn.setText("\u275a\u275a" if playing else "\u25b6")

    def set_cover(self, data: bytes):
        pix = QPixmap()
        if pix.loadFromData(data):
            self._cover_pix = pix
            self.cover.setText("")
            self._rescale_cover()
            self.status_badge.raise_()
            self.sr_badge.raise_()

    def _rescale_cover(self):
        if self._cover_pix is not None:
            w = max(self.width(), self.CARD_W)
            self.cover.setPixmap(self._cover_pix.scaled(
                w, self.COVER_H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        # keep the star badge pinned to the right edge as the card widens
        self.sr_badge.move(w - self.sr_badge.width() - 8, 8)
        self._rescale_cover()
        # re-elide title/artist so wider cards show more text
        avail = w - 28
        self.title_lbl.setText(elide(self._raw_title, avail, bold=True, px=17))
        self.artist_lbl.setText(elide(self._raw_artist, avail, px=14))

    def sizeHint(self):
        return QSize(self.CARD_W, self.CARD_H)

    def minimumSizeHint(self):
        return QSize(self.CARD_W, self.CARD_H)


def elide(text: str, width: int, bold: bool = False, px: int = 0) -> str:
    f = QFont()
    f.setBold(bold)
    if px:
        f.setPixelSize(px)
    fm = QFontMetrics(f)
    return fm.elidedText(text, Qt.ElideRight, width)


def apply_glow(widget, hexcolor="#ff66ab", radius=22, alpha=150):
    """Soft neon glow around a widget (Qt has no CSS box-shadow)."""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(radius)
    col = QColor(hexcolor)
    col.setAlpha(alpha)
    eff.setColor(col)
    eff.setOffset(0, 0)
    widget.setGraphicsEffect(eff)


# ----------------------------------------------------------------------------
# FILTER BAR
# ----------------------------------------------------------------------------
class FilterBar(QWidget):
    searchRequested = Signal()
    medalPacksRequested = Signal()
    beatmapPacksRequested = Signal()
    mostPlayedRequested = Signal()

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 11, 14, 8)
        outer.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(9)

        logo = QLabel("\u25ce")          # hitcircle motif
        logo.setObjectName("logo")
        row1.addWidget(logo)
        # Two-tone wordmark: last ~4 letters get the cyan accent. Falls back to a
        # single colour for very short names. Driven entirely by APP_TITLE.
        _t = APP_TITLE
        if len(_t) > 4:
            _head, _tail = _t[:-4], _t[-4:]
            _markup = f"{_head}<span style='color:#36e0ff'>{_tail}</span>"
        else:
            _markup = _t
        wordmark = QLabel(_markup)
        wordmark.setObjectName("wordmark")
        wordmark.setTextFormat(Qt.RichText)
        wordmark.setToolTip(APP_TAGLINE)
        row1.addWidget(wordmark)
        row1.addSpacing(6)

        self.query = QLineEdit()
        self.query.setObjectName("search")
        self.query.setPlaceholderText("Search title, artist, mapper, tags\u2026   (leave empty for the A\u2013Z catalog)")
        self.query.returnPressed.connect(self.searchRequested.emit)
        apply_glow(self.query, "#ff66ab", radius=16, alpha=70)
        row1.addWidget(self.query, 1)

        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("primary")
        self.search_btn.clicked.connect(self.searchRequested.emit)
        apply_glow(self.search_btn, "#ff66ab", radius=20, alpha=130)
        row1.addWidget(self.search_btn)

        self.medals_btn = QPushButton("\U0001F3C5  Medal packs")
        self.medals_btn.setObjectName("medalbtn")
        self.medals_btn.setToolTip("Browse Beatmap Pack medals and grab a whole pack")
        self.medals_btn.clicked.connect(self.medalPacksRequested.emit)
        row1.addWidget(self.medals_btn)

        self.packs_btn = QPushButton("\U0001F4E6  Beatmap packs")
        self.packs_btn.setObjectName("medalbtn")
        self.packs_btn.setToolTip("Browse all osu! beatmap packs by category and mode")
        self.packs_btn.clicked.connect(self.beatmapPacksRequested.emit)
        row1.addWidget(self.packs_btn)

        self.mostplayed_btn = QPushButton("\U0001F525  Most played")
        self.mostplayed_btn.setObjectName("medalbtn")
        self.mostplayed_btn.setToolTip("Load a player's most-played beatmaps and grab them all")
        self.mostplayed_btn.clicked.connect(self.mostPlayedRequested.emit)
        row1.addWidget(self.mostplayed_btn)
        outer.addLayout(row1)

        # filters live in a distinct surface panel so they read as real controls
        panel = QFrame()
        panel.setObjectName("filterpanel")
        panel_lay = QVBoxLayout(panel)
        panel_lay.setContentsMargins(16, 13, 16, 14)
        panel_lay.setSpacing(13)

        row2 = QHBoxLayout()
        row2.setSpacing(14)

        self.search_in = self._combo(SEARCH_FIELDS)
        self.mode = self._combo(MODES)
        self.status = self._combo(STATUSES)
        self.sort = self._combo(SORTS)

        for lbl, w in [("Search in", self.search_in), ("Mode", self.mode),
                       ("Status", self.status), ("Sort", self.sort)]:
            row2.addWidget(self._labeled(lbl, w))

        row2.addStretch(1)
        panel_lay.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(14)
        self.bpm = self._combo(BPM_RANGES)
        self.length = self._combo(LENGTH_RANGES)
        self.stars = self._combo(STAR_RANGES)
        self.stars.setMinimumWidth(178)
        self.genre = self._combo(GENRES)
        self.language = self._combo(LANGUAGES)
        row3.addWidget(self._labeled("BPM", self.bpm))
        row3.addWidget(self._labeled("Length", self.length))
        row3.addWidget(self._labeled("Stars", self.stars))
        row3.addWidget(self._labeled("Genre", self.genre))
        row3.addWidget(self._labeled("Language", self.language))

        toggles = QWidget()
        toggles.setObjectName("togglebox")
        tlay = QVBoxLayout(toggles)
        tlay.setContentsMargins(0, 0, 0, 0)
        tlay.setSpacing(2)
        tlay.addWidget(self._eyebrow("Options"))
        trow = QHBoxLayout()
        trow.setSpacing(16)
        self.hide_owned = QCheckBox("Hide maps I already have")
        trow.addWidget(self.hide_owned)
        self.no_video = QCheckBox("No-video downloads")
        trow.addWidget(self.no_video)
        tlay.addLayout(trow)
        row3.addSpacing(6)
        row3.addWidget(toggles)
        row3.addStretch(1)
        panel_lay.addLayout(row3)
        outer.addWidget(panel)

        # auto-search when dropdowns change (swallow the int arg they emit)
        for c in (self.mode, self.status, self.sort, self.bpm, self.length,
                  self.stars, self.genre, self.language, self.search_in):
            c.currentIndexChanged.connect(lambda *_: self.searchRequested.emit())
        self.hide_owned.stateChanged.connect(lambda *_: self.searchRequested.emit())

        # sensible defaults: ranked osu! standard maps, newest first
        self.mode.setCurrentIndex(max(0, self.mode.findData(0)))         # osu!
        self.status.setCurrentIndex(max(0, self.status.findData("ranked")))
        self.sort.setCurrentIndex(max(0, self.sort.findData("ranked_desc")))  # newest

    # -- builders -----------------------------------------------------------
    def _eyebrow(self, text):
        lbl = QLabel(text.upper())
        lbl.setObjectName("fieldlabel")
        f = lbl.font()
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.4)
        lbl.setFont(f)
        return lbl

    def _combo(self, items):
        c = QComboBox()
        for label, val in items:
            c.addItem(label, val)
        c.setMinimumWidth(132)
        c.setMinimumHeight(34)
        return c

    def _labeled(self, text, w):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self._eyebrow(text))
        lay.addWidget(w)
        return box

    def filters(self) -> dict:
        bpm_lo, bpm_hi = self.bpm.currentData()
        sr_lo, sr_hi = self.stars.currentData()
        len_lo, len_hi = self.length.currentData()
        return {
            "q": self.query.text(),
            "mode": self.mode.currentData(),
            "status": self.status.currentData(),
            "sort": self.sort.currentData(),
            "bpm_min": bpm_lo,
            "bpm_max": bpm_hi,
            "sr_min": sr_lo,
            "sr_max": sr_hi,
            "len_min": len_lo,
            "len_max": len_hi,
            "genre": self.genre.currentData() or 0,
            "language": self.language.currentData() or 0,
            "option": self.search_in.currentData(),
            "hide_owned": self.hide_owned.isChecked(),
            "no_video": self.no_video.isChecked(),
        }


# ----------------------------------------------------------------------------
# DOWNLOAD QUEUE
# ----------------------------------------------------------------------------
class DownloadRow(QFrame):
    cancelRequested = Signal(int)

    def __init__(self, s: Beatmapset):
        super().__init__()
        self.setid = s.id
        self.setObjectName("dlrow")
        self.setFixedSize(262, 62)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 8, 11, 9)
        lay.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = QLabel(elide(f"{s.artist} - {s.title}", 196, px=12))
        self.name.setObjectName("dlname")
        top.addWidget(self.name, 1)
        self.cancel_btn = QToolButton()
        self.cancel_btn.setText("\u2715")
        self.cancel_btn.setToolTip("Cancel")
        self.cancel_btn.setObjectName("xbtn")
        self.cancel_btn.clicked.connect(lambda: self.cancelRequested.emit(self.setid))
        top.addWidget(self.cancel_btn)
        lay.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        bottom.addWidget(self.bar, 1)
        self.status = QLabel("queued")
        self.status.setObjectName("dlstatus")
        bottom.addWidget(self.status)
        lay.addLayout(bottom)

    def _hide_cancel(self):
        self.cancel_btn.hide()

    def set_progress(self, pct: int, status: str):
        if pct < 0:
            self.bar.setRange(0, 0)  # busy indicator
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(pct)
        self.status.setText(status)

    def set_done(self):
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.status.setText("\u2713 done")
        self.status.setStyleSheet("color:#7ac74f;")
        self._hide_cancel()

    def set_failed(self, err: str):
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status.setText("failed")
        self.status.setStyleSheet("color:#ff6b6b;")
        self.setToolTip(err)
        self._hide_cancel()

    def set_cancelled(self):
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status.setText("cancelled")
        self.status.setStyleSheet("color:#9b9ba6;")
        self._hide_cancel()


# ----------------------------------------------------------------------------
# MEDAL PACKS DIALOG
# ----------------------------------------------------------------------------
class MedalPacksDialog(QDialog):
    def __init__(self, pool: QThreadPool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Beatmap Pack medals")
        self.resize(470, 580)
        self.pool = pool
        self.medals = []
        self.chosen = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        head = QLabel("Pick a medal: its whole beatmap pack downloads, and a collection "
                      "named after the medal is built in osu! automatically.")
        head.setObjectName("hint")
        head.setWordWrap(True)
        lay.addWidget(head)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter medals\u2026")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda *_: self._accept())
        self.list.itemSelectionChanged.connect(
            lambda: self.load_btn.setEnabled(self.list.currentItem() is not None))
        lay.addWidget(self.list, 1)

        self.status = QLabel("Loading medal list\u2026")
        self.status.setObjectName("hint")
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("smallbtn")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.load_btn = QPushButton("Load pack")
        self.load_btn.setObjectName("primary")
        self.load_btn.setEnabled(False)
        self.load_btn.clicked.connect(self._accept)
        btns.addWidget(self.load_btn)
        lay.addLayout(btns)

        w = Worker(fetch_pack_medals)
        w.signals.result.connect(self._loaded)
        w.signals.error.connect(self._failed)
        self.pool.start(w)

    def _loaded(self, medals):
        self.medals = medals
        self.status.setText(f"{len(medals)} Beatmap Pack medals")
        self._filter(self.search.text())

    def _failed(self, err):
        self.status.setText(f"Couldn't load the medal list: {err}")

    def _filter(self, text):
        text = (text or "").lower()
        self.list.clear()
        for med in self.medals:
            if text in med["medal"].lower():
                it = QListWidgetItem(med["medal"])
                it.setData(Qt.UserRole, med)
                self.list.addItem(it)

    def _accept(self):
        it = self.list.currentItem()
        if it is not None:
            self.chosen = it.data(Qt.UserRole)
            self.accept()


class BeatmapPacksDialog(QDialog):
    """Browse all osu! beatmap packs by category/mode and pick one to load."""
    def __init__(self, pool: QThreadPool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Beatmap packs")
        self.resize(560, 660)
        self.pool = pool
        self.chosen = None
        self.packs = []            # everything loaded for the current category
        self.page = 0
        self.loading = False
        self.has_more = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        head = QLabel("Browse osu! beatmap packs. Pick one to load its maps \u2014 then "
                      "download them all and build a collection named after the pack.")
        head.setObjectName("hint")
        head.setWordWrap(True)
        lay.addWidget(head)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.cat = QComboBox()
        for label, val in PACK_TYPES:
            self.cat.addItem(label, val)
        self.cat.currentIndexChanged.connect(self._reload)
        self.mode = QComboBox()
        for label, val in [("All modes", ""), ("osu!", "osu"), ("osu!taiko", "taiko"),
                           ("osu!catch", "fruits"), ("osu!mania", "mania")]:
            self.mode.addItem(label, val)
        self.mode.currentIndexChanged.connect(lambda *_: self._filter())
        row.addWidget(self.cat, 1)
        row.addWidget(self.mode, 1)
        lay.addLayout(row)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter loaded packs by name\u2026")
        self.search.textChanged.connect(lambda *_: self._filter())
        lay.addWidget(self.search)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda *_: self._accept())
        self.list.itemSelectionChanged.connect(
            lambda: self.load_btn.setEnabled(self.list.currentItem() is not None))
        lay.addWidget(self.list, 1)

        self.status = QLabel("Loading packs\u2026")
        self.status.setObjectName("hint")
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        self.more_btn = QPushButton("Load more")
        self.more_btn.setObjectName("smallbtn")
        self.more_btn.setEnabled(False)
        self.more_btn.clicked.connect(self._load_next)
        btns.addWidget(self.more_btn)
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("smallbtn")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.load_btn = QPushButton("Load pack")
        self.load_btn.setObjectName("primary")
        self.load_btn.setEnabled(False)
        self.load_btn.clicked.connect(self._accept)
        btns.addWidget(self.load_btn)
        lay.addLayout(btns)

        self._reload()

    def _reload(self, *_):
        self.packs = []
        self.page = 0
        self.has_more = True
        self.list.clear()
        self._load_next()

    def _load_next(self):
        if self.loading or not self.has_more:
            return
        self.loading = True
        self.more_btn.setEnabled(False)
        self.status.setText("Loading packs\u2026")
        self.page += 1
        w = Worker(fetch_pack_list, self.cat.currentData(), self.page)
        w.signals.result.connect(self._loaded)
        w.signals.error.connect(self._failed)
        self.pool.start(w)

    def _loaded(self, packs):
        self.loading = False
        if len(packs) < PACK_PAGE_COUNT:
            self.has_more = False
        self.packs.extend(packs)
        self._filter()
        self.more_btn.setEnabled(self.has_more)

    def _failed(self, err):
        self.loading = False
        self.status.setText(f"Couldn't load packs: {err}")

    def _filter(self, *_):
        text = self.search.text().lower().strip()
        mode = self.mode.currentData()
        self.list.clear()
        shown = 0
        for p in self.packs:
            if text and text not in p["name"].lower():
                continue
            if mode and p["mode"] != mode:
                continue
            label = p["name"] + (f"     \u00b7  {p['date']}" if p["date"] else "")
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, p)
            self.list.addItem(it)
            shown += 1
        tail = "" if self.has_more else "  \u00b7 end"
        self.status.setText(f"{shown} shown \u00b7 {len(self.packs)} loaded{tail}")

    def _accept(self):
        it = self.list.currentItem()
        if it is not None and it.data(Qt.UserRole) is not None:
            self.chosen = it.data(Qt.UserRole)
            self.accept()


class MostPlayedDialog(QDialog):
    """Ask for an osu! username/ID and how many most-played maps to load."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Most played")
        self.resize(440, 210)
        self.username = None
        self.limit = 100

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        head = QLabel("Load a player's most-played beatmaps. Enter their osu! username "
                      "(or user ID) \u2014 then mass-download the maps and build a "
                      "collection named after them.")
        head.setObjectName("hint")
        head.setWordWrap(True)
        lay.addWidget(head)

        self.user_in = QLineEdit()
        self.user_in.setPlaceholderText("osu! username or ID\u2026")
        self.user_in.returnPressed.connect(self._accept)
        lay.addWidget(self.user_in)

        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("How many:")
        lbl.setObjectName("hint")
        row.addWidget(lbl)
        self.count = QComboBox()
        for label, val in [("Top 50", 50), ("Top 100", 100), ("Top 200", 200), ("Top 500", 500)]:
            self.count.addItem(label, val)
        self.count.setCurrentIndex(1)
        row.addWidget(self.count, 1)
        lay.addLayout(row)

        lay.addStretch(1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("smallbtn")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        ok = QPushButton("Load")
        ok.setObjectName("primary")
        ok.clicked.connect(self._accept)
        btns.addWidget(ok)
        lay.addLayout(btns)
        self.user_in.setFocus()

    def _accept(self):
        u = self.user_in.text().strip()
        if not u:
            return
        self.username = u
        self.limit = self.count.currentData()
        self.accept()


# ----------------------------------------------------------------------------
# SETTINGS DIALOG
# ----------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumSize(680, 500)
        self.resize(700, 520)
        form = QFormLayout(self)
        form.setContentsMargins(22, 20, 22, 20)
        form.setVerticalSpacing(16)
        form.setHorizontalSpacing(16)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)

        # single folder: downloads land here AND it's scanned for "already have".
        # Defaults to the auto-detected osu! Songs folder.
        self.songs_dir = QLineEdit(settings.value("songs_dir", ""))
        self.songs_dir.setPlaceholderText("auto-detected, or pick your own folder")
        songs_row = QHBoxLayout()
        songs_row.addWidget(self.songs_dir, 1)
        bdet = QPushButton("Auto-detect")
        bdet.clicked.connect(self._autodetect)
        songs_row.addWidget(bdet)
        b2 = QPushButton("Browse\u2026")
        b2.clicked.connect(lambda: self._browse(self.songs_dir))
        songs_row.addWidget(b2)
        form.addRow("osu! Songs folder", self._wrap(songs_row))

        # collection.db (osu!stable) - where Medal-pack collections get written.
        self.collection_db = QLineEdit(settings.value("collection_db", ""))
        self.collection_db.setPlaceholderText("pick your osu!stable collection.db (optional)")
        cdb_row = QHBoxLayout()
        cdb_row.addWidget(self.collection_db, 1)
        cdet = QPushButton("Auto-detect")
        cdet.clicked.connect(self._autodetect_cdb)
        cdb_row.addWidget(cdet)
        cb = QPushButton("Browse\u2026")
        cb.clicked.connect(self._browse_cdb)
        cdb_row.addWidget(cb)
        form.addRow("osu! collection.db", self._wrap(cdb_row))

        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 8)
        self.concurrency.setValue(int(settings.value("concurrency", 3)))
        form.addRow("Concurrent downloads", self.concurrency)

        self.auto_open = QCheckBox("Open .osz in osu! after each download (triggers import)")
        self.auto_open.setChecked(settings.value("auto_open", "false") == "true")
        form.addRow("", self.auto_open)

        hint = QLabel("Maps download into the Songs folder, which is also scanned to mark what "
                      "you already have (auto-detects osu-wine / lazer / Windows / macOS).\n\n"
                      "collection.db is your osu!stable collection file (usually in the osu! root, "
                      "next to Songs) \u2014 where the Medal-pack feature writes collections. "
                      "Leave blank if you don't use it.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        form.addRow(hint)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _wrap(self, layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def _browse(self, line: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", line.text() or str(Path.home()))
        if d:
            line.setText(d)

    def _browse_cdb(self):
        start = self.collection_db.text() or self.songs_dir.text() or str(Path.home())
        f, _ = QFileDialog.getOpenFileName(
            self, "Select collection.db", start, "osu! collection (collection.db);;All files (*)")
        if f:
            self.collection_db.setText(f)

    def _autodetect_cdb(self):
        guess = default_collection_db_path(self.songs_dir.text())
        if guess.exists():
            self.collection_db.setText(str(guess))
        else:
            QMessageBox.information(self, "Auto-detect",
                f"No collection.db found at the expected location:\n\n{guess}\n\n"
                "Pick it manually with Browse, or leave blank.")

    def _autodetect(self):
        found = candidate_songs_dirs()
        if found:
            self.songs_dir.setText(str(found[0]))
            if len(found) > 1:
                QMessageBox.information(self, "Auto-detect",
                    "Found several candidates; using the first:\n\n" +
                    "\n".join(str(f) for f in found))
        else:
            QMessageBox.warning(self, "Auto-detect",
                "No osu! Songs folder found in common locations. Pick it manually.")

    def _save(self):
        self.settings.setValue("songs_dir", self.songs_dir.text().strip())
        self.settings.setValue("collection_db", self.collection_db.text().strip())
        self.settings.setValue("concurrency", self.concurrency.value())
        self.settings.setValue("auto_open", "true" if self.auto_open.isChecked() else "false")
        self.accept()


# ----------------------------------------------------------------------------
# MAIN WINDOW
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} {APP_VERSION}")
        self.resize(1180, 820)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        if not self.settings.value("songs_dir"):
            cands = candidate_songs_dirs()
            self.settings.setValue(
                "songs_dir",
                str(cands[0]) if cands else str(Path.home() / "osu-beatmaps"))

        self.cache_dir = Path(tempfile.gettempdir()) / "osu_dl_covers"
        self.cache_dir.mkdir(exist_ok=True)

        self.pool = QThreadPool()              # search
        self.img_pool = QThreadPool()          # thumbnails
        self.img_pool.setMaxThreadCount(8)
        self.meta_pool = QThreadPool()         # pack-card metadata enrichment
        self.meta_pool.setMaxThreadCount(4)
        self.dl_pool = QThreadPool()           # downloads
        self.dl_pool.setMaxThreadCount(int(self.settings.value("concurrency", 3)))

        self.cursor_token = None
        self.more = True
        self.loading = False
        self.cur_filters = None
        self.cards = {}            # setid -> BeatmapCard
        self._cover_requested = set()   # setids whose cover load has started (lazy)
        self._bpm_requested = set()     # hinamizawa setids whose BPM enrich started
        self.dl_rows = {}          # setid -> DownloadRow
        self.dl_workers = {}       # setid -> active DownloadWorker
        self.dl_pending = []       # list[Beatmapset] waiting for a free slot
        self.dl_paused = False
        self.dl_concurrency = int(self.settings.value("concurrency", 3))
        self.downloaded_ids = set()
        self.auto_pages = 0        # guard against runaway auto-fetch on tight filters
        self.pack = None           # active medal-pack session, or None

        # persistent download history (also marks lazer imports / other machines)
        self.history_path = Path(self.settings.fileName()).parent / "download_history.json"
        self.history_ids = load_history(self.history_path)

        self._build_ui()
        self._setup_audio()
        self._refresh_downloaded()
        QTimer.singleShot(0, self.new_search)  # initial A-Z-ish load

    # -- UI -----------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.filter_bar = FilterBar()
        self.filter_bar.searchRequested.connect(self.new_search)
        self.filter_bar.medalPacksRequested.connect(self._open_medal_packs)
        self.filter_bar.beatmapPacksRequested.connect(self._open_beatmap_packs)
        self.filter_bar.mostPlayedRequested.connect(self._open_most_played)
        root.addWidget(self.filter_bar)

        rule = QFrame()
        rule.setObjectName("neonrule")
        rule.setFixedHeight(2)
        root.addWidget(rule)

        # medal-pack banner (hidden unless a pack is loaded)
        self.pack_banner = QFrame()
        self.pack_banner.setObjectName("packbanner")
        pb = QHBoxLayout(self.pack_banner)
        pb.setContentsMargins(16, 9, 16, 9)
        pb.setSpacing(12)
        self.pack_label = QLabel("")
        self.pack_label.setObjectName("packlabel")
        pb.addWidget(self.pack_label, 1)
        self.pack_dl_btn = QPushButton("Download all & create collection")
        self.pack_dl_btn.setObjectName("primary")
        self.pack_dl_btn.clicked.connect(self._download_pack_and_collect)
        pb.addWidget(self.pack_dl_btn)
        pack_exit = QPushButton("Exit pack")
        pack_exit.setObjectName("smallbtn")
        pack_exit.clicked.connect(self._exit_pack_mode)
        pb.addWidget(pack_exit)
        self.pack_banner.hide()
        root.addWidget(self.pack_banner)

        split = QSplitter(Qt.Vertical)
        root.addWidget(split, 1)

        # results grid (top)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("results")
        self.grid_host = QWidget()
        self.flow = FlowLayout(self.grid_host, margin=14, spacing=14)
        self.scroll.setWidget(self.grid_host)
        self.scroll.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        split.addWidget(self.scroll)

        # downloads dock (bottom, horizontal)
        dock = QWidget()
        dock.setObjectName("dock")
        dlay = QVBoxLayout(dock)
        dlay.setContentsMargins(14, 10, 14, 10)
        dlay.setSpacing(9)

        head = QHBoxLayout()
        head.setSpacing(8)
        h = QLabel("Downloads")
        h.setObjectName("panelhead")
        head.addWidget(h)
        head.addSpacing(8)
        self.dl_all_btn = QPushButton("Download all shown")
        self.dl_all_btn.setObjectName("dockbtn")
        self.dl_all_btn.clicked.connect(self.download_all_shown)
        head.addWidget(self.dl_all_btn)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("dockbtn")
        self.pause_btn.clicked.connect(self.toggle_pause)
        head.addWidget(self.pause_btn)
        self.cancel_all_btn = QPushButton("Cancel all")
        self.cancel_all_btn.setObjectName("dockbtn")
        self.cancel_all_btn.clicked.connect(self.cancel_all)
        head.addWidget(self.cancel_all_btn)
        head.addStretch(1)
        clear = QPushButton("Clear finished")
        clear.setObjectName("dockbtn")
        clear.clicked.connect(self._clear_finished)
        head.addWidget(clear)
        dlay.addLayout(head)

        # horizontal strip of download chips
        self.dl_scroll = QScrollArea()
        self.dl_scroll.setWidgetResizable(True)
        self.dl_scroll.setObjectName("dockscroll")
        self.dl_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.dl_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.dl_host = QWidget()
        self.dl_layout = QHBoxLayout(self.dl_host)
        self.dl_layout.setContentsMargins(0, 0, 0, 0)
        self.dl_layout.setSpacing(9)
        self.dl_empty = QLabel("No downloads yet \u2014 hit Download on a map, or \u201cDownload all shown\u201d.")
        self.dl_empty.setObjectName("dockempty")
        self.dl_layout.addWidget(self.dl_empty)
        self.dl_layout.addStretch(1)
        self.dl_scroll.setWidget(self.dl_host)
        dlay.addWidget(self.dl_scroll, 1)

        split.addWidget(dock)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([640, 168])
        dock.setMinimumHeight(120)
        dock.setMaximumHeight(280)

        # status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_label = QLabel("Ready")
        self.status.addWidget(self.status_label, 1)
        if HAS_MULTIMEDIA:
            self.stop_btn = QToolButton()
            self.stop_btn.setText("\u25a0")
            self.stop_btn.setObjectName("pillbtn")
            self.stop_btn.setToolTip("Stop preview")
            self.stop_btn.clicked.connect(self.stop_preview)
            self.status.addPermanentWidget(self.stop_btn)
            vol = QLabel("\U0001F509")
            self.status.addPermanentWidget(vol)
            self.vol_slider = QSlider(Qt.Horizontal)
            self.vol_slider.setFixedWidth(96)
            self.vol_slider.setRange(0, 100)
            self.vol_slider.setValue(int(self.settings.value("volume", 40)))
            self.vol_slider.setToolTip("Preview volume")
            self.vol_slider.valueChanged.connect(self._set_volume)
            self.status.addPermanentWidget(self.vol_slider)
        settings_btn = QPushButton("\u2699 Settings")
        settings_btn.setObjectName("smallbtn")
        settings_btn.clicked.connect(self._open_settings)
        self.status.addPermanentWidget(settings_btn)

        self.setStyleSheet(STYLE)

    def _setup_audio(self):
        self.player = None
        self.now_playing = None
        if HAS_MULTIMEDIA:
            self.player = QMediaPlayer()
            self.audio_out = QAudioOutput()
            self.audio_out.setVolume(int(self.settings.value("volume", 40)) / 100)
            self.player.setAudioOutput(self.audio_out)
            self.player.playbackStateChanged.connect(self._on_playback_change)

    def _set_volume(self, v):
        self.settings.setValue("volume", v)
        if getattr(self, "audio_out", None):
            self.audio_out.setVolume(v / 100)

    # -- searching ----------------------------------------------------------
    def _clear_grid(self):
        while self.flow.count():
            it = self.flow.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self.cards.clear()
        self._cover_requested.clear()
        self._bpm_requested.clear()

    def _add_card(self, s, owned):
        card = BeatmapCard(s, owned)
        card.previewRequested.connect(self.preview)
        card.downloadRequested.connect(self.enqueue_download)
        self.flow.addWidget(card)
        self.cards[s.id] = card
        # Covers are loaded lazily for cards near the viewport (see
        # _load_visible_covers); a big artist result no longer fires dozens of
        # thumbnail downloads at once. Schedule a scan once layout settles.
        QTimer.singleShot(0, self._load_visible_covers)
        return card

    def new_search(self):
        if self.pack:
            self._exit_pack_mode(refresh=False)
        self.cur_filters = self.filter_bar.filters()
        self.cursor_token = None
        self.more = True
        self.auto_pages = 0
        self._clear_grid()
        self._refresh_downloaded()
        self._fetch_page()

    def _fetch_page(self):
        if self.loading or not self.more:
            return
        self.loading = True
        self.status_label.setText("Loading\u2026")
        f = dict(self.cur_filters)
        w = Worker(search_beatmapsets, f, self.cursor_token)
        w.signals.result.connect(self._on_results)
        w.signals.error.connect(self._on_error)
        self.pool.start(w)

    @Slot(object)
    def _on_results(self, payload):
        sets, next_token = payload
        self.loading = False
        self.cursor_token = next_token
        self.more = next_token is not None
        f = self.cur_filters

        added = 0
        for s in sets:
            if s.id in self.cards:
                continue
            owned = s.id in self.downloaded_ids
            if f["hide_owned"] and owned:
                continue
            self._add_card(s, owned)
            added += 1

        total = len(self.cards)
        # client-side-only filters (BPM / stars / length / field scope) aren't
        # applied by the mirror, so a rare pick can be sparse on the first page.
        # We now pull big pages (FILTER_PAGE_SIZE), so a handful of pages is plenty.
        client_filter = _has_client_filter(f)
        target = 24 if client_filter else 1
        cap = 5 if client_filter else 6

        if self.more and total < target and self.auto_pages < cap:
            self.auto_pages += 1
            self.status_label.setText(f"Filtering\u2026 {total} match" + ("" if total == 1 else "es"))
            self._fetch_page()
            return

        self.status_label.setText(
            f"{total} maps shown"
            + ("  \u2022  end of results" if not self.more else "")
            + ("  \u2022  no matches \u2014 try widening the filters" if not total else ""))

    @Slot(str)
    def _on_error(self, msg):
        self.loading = False
        self.status_label.setText(f"Error: {msg}")

    # -- medal packs --------------------------------------------------------
    def _open_medal_packs(self):
        dlg = MedalPacksDialog(self.pool, self)
        if dlg.exec() and dlg.chosen:
            self._enter_pack_mode(dlg.chosen)

    def _open_beatmap_packs(self):
        dlg = BeatmapPacksDialog(self.pool, self)
        if dlg.exec() and dlg.chosen:
            p = dlg.chosen
            # reuse pack mode: one tag, collection named after the pack
            self._enter_pack_mode({"medal": p["name"], "tags": [p["tag"]]})

    def _open_most_played(self):
        dlg = MostPlayedDialog(self)
        if not (dlg.exec() and dlg.username):
            return
        if self.pack:
            self._exit_pack_mode(refresh=False)
        self.pack = None
        self.more = False
        self.loading = False
        self._clear_grid()
        self.pack_banner.show()
        self.pack_dl_btn.setEnabled(False)
        self.pack_label.setText(f"\U0001F525  Most played by {dlg.username}   \u2014   loading\u2026")
        self.status_label.setText("Fetching most-played maps\u2026")
        w = Worker(fetch_most_played, dlg.username, dlg.limit)
        w.signals.result.connect(self._on_most_played)
        w.signals.error.connect(self._on_pack_error)
        self.pool.start(w)

    @Slot(object)
    def _on_most_played(self, payload):
        user, sets = payload
        if not sets:
            self.pack_label.setText(f"\U0001F525  No most-played maps found for {user}")
            self.status_label.setText("Nothing found")
            return
        self._enter_setlist_mode(f"{user}'s most played", sets, icon="\U0001F525")

    def _enter_setlist_mode(self, name: str, sets: list, icon: str = "\U0001F3C5"):
        """Pack mode fed a pre-built set list (most-played, etc.) instead of a pack
        page. Reuses the download + collection flow via self.pack."""
        self.pack = {"medal": name, "tags": [], "ids": [s.id for s in sets],
                     "hashes": {}, "pending": set(), "collecting": False}
        self.more = False
        self.loading = False
        self._clear_grid()
        self._refresh_downloaded()
        self.pack_banner.show()
        for s in sets:
            self._add_card(s, s.id in self.downloaded_ids)
        n = len(sets)
        have = sum(1 for s in sets if s.id in self.downloaded_ids)
        self.pack_label.setText(f"{icon}  {name}   \u2014   {n} maps"
                                + (f"  ({have} already in library)" if have else ""))
        self.pack_dl_btn.setEnabled(True)
        self.status_label.setText(f"{n} maps")

    def _enter_pack_mode(self, medal: dict):
        self.pack = {"medal": medal["medal"], "tags": medal["tags"],
                     "ids": [], "hashes": {}, "pending": set(), "collecting": False}
        self.more = False            # no infinite-scroll in pack mode
        self.loading = False
        self._clear_grid()
        self._refresh_downloaded()
        self.pack_banner.show()
        self.pack_dl_btn.setEnabled(False)
        self.pack_label.setText(f"\U0001F3C5  {medal['medal']}   \u2014   loading pack\u2026")
        self.status_label.setText("Loading pack contents\u2026")
        w = Worker(fetch_pack_contents, medal["tags"])
        w.signals.result.connect(self._on_pack_contents)
        w.signals.error.connect(self._on_pack_error)
        self.pool.start(w)

    @Slot(object)
    def _on_pack_contents(self, sets):
        if not self.pack:
            return
        self.pack["ids"] = [sid for sid, _ in sets]
        for sid, name in sets:
            s = Beatmapset.from_pack(sid, name)
            owned = sid in self.downloaded_ids
            self._add_card(s, owned)
            # best-effort enrich with full metadata (mapper, bpm, stars...) via search
            if name:
                w = Worker(fetch_set_meta, sid, name)
                w.signals.result.connect(self._on_set_meta)
                w.signals.error.connect(self._on_meta_error)
                self.meta_pool.start(w)
        n = len(sets)
        have = sum(1 for sid, _ in sets if sid in self.downloaded_ids)
        self.pack_label.setText(
            f"\U0001F3C5  {self.pack['medal']}   \u2014   {n} maps"
            + (f"  ({have} already in library)" if have else ""))
        self.pack_dl_btn.setEnabled(True)
        self.status_label.setText(f"{n} maps in pack")

    @Slot(object)
    def _on_set_meta(self, payload):
        """Apply fetched full metadata to the matching pack card (if still shown)."""
        sid, full = payload
        card = self.cards.get(sid)
        if card is not None:
            card.apply_full(full)

    @Slot(str)
    def _on_meta_error(self, msg):
        # Best-effort enrichment: a map that doesn't surface in search just keeps
        # its artist/title from the pack page. Nothing to surface to the user.
        pass

    @Slot(str)
    def _on_pack_error(self, msg):
        self.pack_label.setText(f"\U0001F3C5  Couldn't load pack: {msg}")
        self.status_label.setText(f"Error: {msg}")

    def _collection_db_path(self, prompt=False):
        """The configured collection.db. If unset and prompt=True, ask the user to
        pick it (and remember it). Returns a Path, or None if they cancel."""
        cfg = (self.settings.value("collection_db") or "").strip()
        if cfg:
            return Path(cfg)
        guess = default_collection_db_path(self.settings.value("songs_dir", ""))
        if not prompt:
            return guess
        start = str(guess if guess.exists() else guess.parent)
        f, _ = QFileDialog.getOpenFileName(
            self, "Select your osu! collection.db", start,
            "osu! collection (collection.db);;All files (*)")
        if not f:
            return None
        self.settings.setValue("collection_db", f)
        return Path(f)

    def _download_pack_and_collect(self):
        if not self.pack or not self.pack["ids"]:
            return
        db_path = self._collection_db_path(prompt=True)
        if db_path is None:
            self.status_label.setText("Set your collection.db to build a collection (Settings).")
            return
        resp = QMessageBox.question(
            self, "Download pack & build collection",
            f"This will download {len(self.pack['ids'])} maps and then add a collection "
            f"named \u201c{self.pack['medal']}\u201d to:\n\n{db_path}\n\n"
            "osu! must be CLOSED when the download finishes (the collection file is "
            "rewritten, with a .bak backup kept). Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if resp != QMessageBox.Yes:
            return
        self.pack["db"] = str(db_path)
        self.pack["collecting"] = True
        self.pack["pending"] = set(self.pack["ids"])
        self.pack["hashes"] = {}
        self.pack_dl_btn.setEnabled(False)
        self.pack_dl_btn.setText("Downloading pack\u2026")
        for sid in self.pack["ids"]:
            card = self.cards.get(sid)
            if card is not None:
                self.enqueue_download(card.s)

    def _pack_map_finished(self, setid, path):
        """Called from _on_dl_done while a pack collection is being assembled."""
        pk = self.pack
        if not pk or not pk.get("collecting") or setid not in pk["pending"]:
            return
        if path:
            pk["hashes"][setid] = md5s_from_osz(path)
        pk["pending"].discard(setid)
        done = len(pk["ids"]) - len(pk["pending"])
        self.pack_dl_btn.setText(f"Downloading pack\u2026 {done}/{len(pk['ids'])}")
        if not pk["pending"]:
            self._finalize_collection()

    def _finalize_collection(self):
        pk = self.pack
        pk["collecting"] = False
        hashes = [h for sid in pk["ids"] for h in pk["hashes"].get(sid, [])]
        db_path = Path(pk.get("db") or self._collection_db_path())
        self.pack_dl_btn.setText("Download all & create collection")
        self.pack_dl_btn.setEnabled(True)
        if not hashes:
            QMessageBox.warning(self, "No collection written",
                                "No beatmap files were found to hash, so no collection "
                                "was created. Check that the downloads succeeded.")
            return
        try:
            status = upsert_collection(db_path, pk["medal"], hashes)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Collection error",
                                 f"Couldn't write the collection:\n{type(e).__name__}: {e}")
            return
        QMessageBox.information(
            self, "Collection created",
            f"{status}.\n\nWritten to {db_path}\nA backup (.bak) was kept.\n\n"
            "Reopen osu! and the collection will appear in song select (press F5 to "
            "reprocess if any maps don't show yet).")

    def _exit_pack_mode(self, refresh=True):
        self.pack = None
        self.pack_banner.hide()
        self.pack_dl_btn.setText("Download all & create collection")
        if refresh:
            self.new_search()

    def _maybe_load_more(self, value):
        bar = self.scroll.verticalScrollBar()
        if value >= bar.maximum() - 400 and self.more and not self.loading:
            self.auto_pages = 0
            self._fetch_page()
        self._load_visible_covers()

    # -- covers -------------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._load_visible_covers)   # newly-visible cards

    def _load_visible_covers(self):
        """Start cover downloads only for cards in (or near) the viewport, so a
        large result set doesn't queue dozens of thumbnails up front."""
        bar = self.scroll.verticalScrollBar()
        top = bar.value()
        bottom = top + self.scroll.viewport().height()
        margin = 700                       # preload a screenful above/below
        for sid, card in self.cards.items():
            if sid in self._cover_requested:
                continue
            if not card.s.cover_url:
                self._cover_requested.add(sid)
                continue
            y = card.y()
            if y + card.height() >= top - margin and y <= bottom + margin:
                self._cover_requested.add(sid)
                self._load_cover(card.s)
                # hinamizawa cards have no BPM/play counts -> enrich visible ones
                # from osu.direct. (Pack cards enrich via their own path.)
                if (sid not in self._bpm_requested and not card._in_pack
                        and not card.s.minimal and not card.s.bpm):
                    self._bpm_requested.add(sid)
                    w = Worker(fetch_card_meta, sid)
                    w.signals.result.connect(self._on_card_meta)
                    w.signals.error.connect(self._on_meta_error)
                    self.meta_pool.start(w)

    @Slot(object)
    def _on_card_meta(self, payload):
        """Apply BPM + play/favourite counts fetched from osu.direct to a card."""
        sid, full = payload
        card = self.cards.get(sid)
        if card is None:
            return
        card.s.bpm = full.bpm
        card.s.play_count = full.play_count
        card.s.favourite_count = full.favourite_count
        if full.diffs:                 # more accurate per-diff data (incl. BPM)
            card.s.diffs = full.diffs
        card._fill_text(card.s)

    def _load_cover(self, s: Beatmapset):
        if not s.cover_url:
            return
        w = ImageWorker(s.id, s.cover_url, self.cache_dir)
        w.signals.done.connect(self._on_cover)
        self.img_pool.start(w)

    @Slot(int, bytes)
    def _on_cover(self, setid, data):
        card = self.cards.get(setid)
        if card:
            card.set_cover(data)

    # -- preview ------------------------------------------------------------
    def preview(self, setid: int):
        if not self.player:
            self.status_label.setText("Audio preview unavailable (QtMultimedia codecs missing).")
            return
        if self.now_playing == setid and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            return
        if self.now_playing is not None and self.now_playing in self.cards:
            self.cards[self.now_playing].set_preview_playing(False)
        self.now_playing = setid
        self.player.setSource(QUrl(PREVIEW_URL.format(id=setid)))
        self.player.play()

    def _on_playback_change(self, state):
        playing = state == QMediaPlayer.PlayingState
        if self.now_playing in self.cards:
            self.cards[self.now_playing].set_preview_playing(playing)
        if state == QMediaPlayer.StoppedState:
            # clip finished or stopped -> reset the play icon
            if self.now_playing in self.cards:
                self.cards[self.now_playing].set_preview_playing(False)

    def stop_preview(self):
        if self.player:
            self.player.stop()
        if self.now_playing in self.cards:
            self.cards[self.now_playing].set_preview_playing(False)
        self.now_playing = None

    # -- downloads (queue manager) ------------------------------------------
    def enqueue_download(self, s: Beatmapset):
        if s.id in self.dl_workers or any(p.id == s.id for p in self.dl_pending):
            return
        # allow re-queue of a previously finished/failed/cancelled row
        old = self.dl_rows.pop(s.id, None)
        if old is not None:
            old.deleteLater()

        row = DownloadRow(s)
        row.cancelRequested.connect(self.cancel_download)
        self.dl_layout.insertWidget(self.dl_layout.count() - 1, row)
        self.dl_rows[s.id] = row
        self.dl_pending.append(s)
        if s.id in self.cards:
            self.cards[s.id].mark_queued()
        self._update_dock_empty()
        self._pump()

    def _update_dock_empty(self):
        self.dl_empty.setVisible(not self.dl_rows)

    def download_all_shown(self):
        for sid, card in list(self.cards.items()):
            if sid in self.downloaded_ids or sid in self.dl_workers:
                continue
            if any(p.id == sid for p in self.dl_pending):
                continue
            self.enqueue_download(card.s)

    def _pump(self):
        if self.dl_paused:
            return
        while len(self.dl_workers) < self.dl_concurrency and self.dl_pending:
            s = self.dl_pending.pop(0)
            self._start_download(s)

    def _start_download(self, s: Beatmapset):
        dest = self.settings.value("songs_dir") or str(Path.home() / "osu-beatmaps")
        no_video = self.filter_bar.no_video.isChecked()   # live, not search-time
        worker = DownloadWorker(s, dest, no_video, MIRRORS)
        worker.signals.progress.connect(self._on_dl_progress)
        worker.signals.done.connect(self._on_dl_done)
        worker.signals.failed.connect(self._on_dl_failed)
        self.dl_workers[s.id] = worker
        if s.id in self.cards:
            self.cards[s.id].mark_downloading()
        self.dl_pool.start(worker)

    def toggle_pause(self):
        self.dl_paused = not self.dl_paused
        self.pause_btn.setText("Resume" if self.dl_paused else "Pause")
        if not self.dl_paused:
            self._pump()

    def cancel_download(self, setid: int):
        self.dl_pending = [p for p in self.dl_pending if p.id != setid]
        worker = self.dl_workers.get(setid)
        if worker:                       # in-flight: ask it to stop (emits failed "cancelled")
            worker.cancel()
        else:                            # was only queued
            row = self.dl_rows.get(setid)
            if row:
                row.set_cancelled()
            if setid in self.cards:
                self.cards[setid].set_downloaded(setid in self.downloaded_ids)
            self._pump()

    def cancel_all(self):
        pending = self.dl_pending
        self.dl_pending = []
        for s in pending:
            row = self.dl_rows.get(s.id)
            if row:
                row.set_cancelled()
            if s.id in self.cards:
                self.cards[s.id].set_downloaded(s.id in self.downloaded_ids)
        for worker in list(self.dl_workers.values()):
            worker.cancel()

    @Slot(int, int, str)
    def _on_dl_progress(self, setid, pct, status):
        row = self.dl_rows.get(setid)
        if row:
            txt = status if pct < 0 else f"{pct}%  \u2022  {status}"
            row.set_progress(pct, txt)

    @Slot(int, str)
    def _on_dl_done(self, setid, path):
        row = self.dl_rows.get(setid)
        if row:
            row.set_done()
        self.dl_workers.pop(setid, None)
        self.downloaded_ids.add(setid)
        self.history_ids.add(setid)
        save_history(self.history_path, self.history_ids)
        if setid in self.cards:
            self.cards[setid].set_downloaded(True)
        if self.settings.value("auto_open", "false") == "true":
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self._pack_map_finished(setid, path)
        self._pump()

    @Slot(int, str)
    def _on_dl_failed(self, setid, err):
        row = self.dl_rows.get(setid)
        if err == "cancelled":
            if row:
                row.set_cancelled()
            if setid in self.cards:
                self.cards[setid].set_downloaded(setid in self.downloaded_ids)
        else:
            if row:
                row.set_failed(err)
            if setid in self.cards:
                self.cards[setid].mark_failed()
        self.dl_workers.pop(setid, None)
        # a pack map that won't arrive shouldn't stall the collection build
        if err == "cancelled" and self.pack and self.pack.get("collecting"):
            # cancelling the queue aborts the whole collection build
            self.pack["collecting"] = False
            self.pack_dl_btn.setText("Download all & create collection")
            self.pack_dl_btn.setEnabled(True)
        else:
            self._pack_map_finished(setid, None)
        self._pump()

    def _clear_finished(self):
        for setid in list(self.dl_rows.keys()):
            if setid not in self.dl_workers and not any(p.id == setid for p in self.dl_pending):
                row = self.dl_rows.pop(setid)
                row.deleteLater()
        self._update_dock_empty()

    # -- misc ---------------------------------------------------------------
    def _refresh_downloaded(self):
        songs = self.settings.value("songs_dir", "")
        self.downloaded_ids = scan_downloaded_ids(songs) | self.history_ids

    def _open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.dl_concurrency = int(self.settings.value("concurrency", 3))
            self.dl_pool.setMaxThreadCount(self.dl_concurrency)
            self._refresh_downloaded()
            self.new_search()
            self._pump()


# ----------------------------------------------------------------------------
# STYLE  (synthwave / osu! neon theme  -- pink x cyan on deep indigo)
# ----------------------------------------------------------------------------
STYLE = """
* { font-family: "Inter", "Segoe UI", "Noto Sans", sans-serif; font-size: 12px; color: #f1ecfb; }

QMainWindow, QDialog { background: #100c1a; }
QWidget { background: transparent; }
QMainWindow > QWidget, QDialog > QWidget {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #16101f, stop:1 #0e0a16);
}

QScrollArea#results { background: transparent; border: none; }
QScrollArea { border: none; }

/* ---- cards ---- */
#card {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #221a35, stop:1 #1b1530);
    border: 1px solid #342a4d; border-radius: 14px;
}
#card:hover { border: 1px solid #ff66ab; }
#card[owned="true"] {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1c2530, stop:1 #16202a);
    border: 1px solid #2f4a48;
}
#cover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2a2142, stop:0.5 #3a2350, stop:1 #2a2142);
    border-top-left-radius: 14px; border-top-right-radius: 14px;
    color: #5b5078; font-size: 30px;
}
#title { font-size: 17px; font-weight: 700; color: #ffffff; }
#meta { color: #cabfe8; font-size: 14px; }
#stats { color: #3fe3ff; font-size: 13px; font-weight: 600; }
#sub { color: #9b90bd; font-size: 13px; }

/* ---- inputs ---- */
#fieldlabel { color: #9a8fc0; font-size: 10px; font-weight: 800; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #241c39; border: 1px solid #3d3059; border-radius: 9px;
    padding: 7px 11px; font-size: 13px; selection-background-color: #ff66ab; color: #f1ecfb;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #ff66ab; }
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border: 1px solid #5a4785; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { width: 11px; height: 11px; }
QComboBox QAbstractItemView {
    background: #1d1730; border: 1px solid #382c52; selection-background-color: #ff66ab;
    selection-color: #14101e; outline: none; padding: 4px;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 20px;
    background: #2e2450; border-left: 1px solid #3d3059; border-top-right-radius: 9px;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right; width: 20px;
    background: #2e2450; border-left: 1px solid #3d3059; border-bottom-right-radius: 9px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover { background: #ff66ab; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { width: 10px; height: 10px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { width: 10px; height: 10px; }
#filterpanel {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1b1530, stop:1 #161025);
    border: 1px solid #2f2548; border-radius: 13px;
}
#search {
    font-size: 15px; padding: 10px 15px; border-radius: 11px;
    background: #1d1730; border: 1px solid #3a2c58;
}
#search:focus { border: 1px solid #ff66ab; }

/* ---- buttons ---- */
QPushButton {
    background: #2a2140; border: 1px solid #3d3059; border-radius: 8px;
    padding: 6px 13px; color: #e7e0f7;
}
QPushButton:hover { background: #342752; border-color: #5a4785; }
QPushButton:disabled { color: #6b6388; background: #221a33; border-color: #2e2545; }
QPushButton#primary, QPushButton#dlbtn {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff5fa6, stop:1 #ff86c0);
    border: none; color: #19101c; font-weight: 700;
}
QPushButton#dlbtn { min-height: 36px; font-size: 14px; }
QPushButton#primary:hover, QPushButton#dlbtn:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff79b6, stop:1 #ff9bce);
}
QPushButton#dlbtn:disabled {
    background: #2a2140; color: #8a80aa; border: 1px solid #3d3059;
}
#card[owned="true"] QPushButton#dlbtn {
    background: transparent; color: #58e0b0; border: 1px solid #2f5a4e;
}
QPushButton#smallbtn { padding: 5px 11px; font-size: 11px; }
QPushButton#medalbtn {
    background: #241c39; border: 1px solid #36e0ff; border-radius: 11px;
    padding: 9px 15px; font-size: 13px; font-weight: 700; color: #6fe9ff;
}
QPushButton#medalbtn:hover { background: #36e0ff; color: #10131a; }
#packbanner {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2a1733, stop:1 #1a1330);
    border-bottom: 1px solid #ff66ab;
}
#packlabel { font-size: 15px; font-weight: 800; color: #ffd45e; }

/* circular preview / web buttons -- the osu! hitcircle motif */
QToolButton#circbtn {
    background: #241c39; border: 1px solid #ff66ab; border-radius: 18px;
    min-width: 36px; min-height: 36px; max-height: 38px; color: #ff8cc4; font-size: 14px;
}
QToolButton#circbtn:hover { background: #ff66ab; color: #19101c; }
QToolButton#pillbtn {
    background: #241c39; border: 1px solid #3d3059; border-radius: 8px;
    padding: 6px 9px; color: #c3b9e0;
}
QToolButton#pillbtn:hover { border-color: #36e0ff; color: #36e0ff; }
QToolButton#xbtn { background: transparent; border: none; color: #8b81ab; padding: 0 4px; font-size: 13px; }
QToolButton#xbtn:hover { color: #ff5f8f; }

/* ---- identity ---- */
#logo { color: #ff66ab; font-size: 22px; }
#wordmark { color: #f1ecfb; font-size: 15px; font-weight: 800; }
#neonrule {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(255,102,171,0), stop:0.5 #ff66ab, stop:1 rgba(54,224,255,0.0));
    max-height: 2px; min-height: 2px; border: none;
}

/* ---- downloads dock (bottom) ---- */
#dock { background: #0e0a18; border-top: 1px solid #2c2344; }
QScrollArea#dockscroll { background: transparent; }
#panelhead { font-size: 15px; font-weight: 800; color: #ffffff; }
#dockempty { color: #6f6790; font-size: 13px; }
QPushButton#dockbtn {
    background: #2a2140; border: 1px solid #3d3059; border-radius: 9px;
    padding: 9px 17px; font-size: 13px; font-weight: 600; color: #e7e0f7;
}
QPushButton#dockbtn:hover { background: #36285a; border-color: #5a4785; }
#dlrow {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #221a35, stop:1 #1b1530);
    border: 1px solid #342a4d; border-radius: 11px;
}
#dlname { font-size: 12px; color: #e7e0f7; }
#dlstatus { color: #8b81ab; font-size: 11px; }
QProgressBar { background: #2a2140; border: none; border-radius: 3px; }
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff66ab, stop:1 #36e0ff);
    border-radius: 3px;
}

/* ---- chrome ---- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ff66ab, stop:1 #b14bd0);
    border-radius: 5px; min-height: 36px;
}
QScrollBar::handle:vertical:hover { background: #ff85c0; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QStatusBar { background: #0c0813; border-top: 1px solid #271f3c; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: #b3a9d0; }
#hint { color: #8b81ab; font-size: 11px; }
QSplitter::handle { background: #271f3c; width: 1px; }
QCheckBox { color: #d6cdf0; spacing: 8px; font-size: 12px; }
QCheckBox::indicator {
    width: 18px; height: 18px; border-radius: 5px;
    border: 1px solid #3d3059; background: #241c39;
}
QCheckBox::indicator:hover { border: 1px solid #ff66ab; }
QCheckBox::indicator:checked { background: #ff66ab; border-color: #ff66ab; }
QToolTip { background: #1d1730; color: #f1ecfb; border: 1px solid #ff66ab; padding: 4px 7px; }
QSlider::groove:horizontal { height: 4px; background: #2a2140; border-radius: 2px; }
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff66ab, stop:1 #36e0ff); border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #ffffff; width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
}
"""


def resource_path(name: str) -> str:
    """Path to a bundled resource, working both from source and from a
    PyInstaller one-file build (which unpacks data into sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def main():
    # Make Windows show our own taskbar icon instead of grouping under python.exe.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{ORG_NAME}.{APP_NAME}")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)

    icon_file = resource_path("icon.ico")
    if os.path.exists(icon_file):
        app.setWindowIcon(QIcon(icon_file))

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
