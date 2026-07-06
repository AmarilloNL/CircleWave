#!/usr/bin/env python3
"""
Circlewave -- core (Qt-free) logic.

Pure data model, filtering/ranking, mirror networking, osu!stable collection.db
read/write, and osu! wiki / pack-page parsers. Imported by circlewave.py (the
PySide6 GUI) and exercised directly by the test suite, which runs without Qt.

Copyright (C) 2026 AmarilloNL.  GPLv3 -- see circlewave.py / LICENSE.
"""

from __future__ import annotations

import os
import re
import json
import time
import hashlib
import logging
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

try:                                    # urllib3 ships with requests; guard anyway
    from urllib3.util.retry import Retry
except Exception:                       # pragma: no cover
    Retry = None

log = logging.getLogger("circlewave")


# ----------------------------------------------------------------------------
# HTTP SESSION
# ----------------------------------------------------------------------------
# One shared session for the whole app: keep-alive connection pooling speeds up
# the many sequential search / pack / metadata requests, and an automatic
# retry/backoff handles the transient failures mirrors love to throw (429 rate
# limits, 502/503 while a node restarts). Use SESSION.get(...) everywhere instead
# of requests.get(...).
def _build_session() -> requests.Session:
    s = requests.Session()
    if Retry is not None:
        retry = Retry(
            total=3, connect=3, read=3,
            backoff_factor=0.5,                        # waits 0.5s, 1s, 2s
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    return s


SESSION = _build_session()

# Politeness throttle for the osu.ppy.sh website (pack listings, pack pages,
# most-played). Those are scraped from the public site rather than a mirror API,
# so we self-limit to avoid hammering it -- especially when bulk-loading the
# ~3,750 packs across categories. Mirror/API hosts are not throttled.
_OSU_MIN_INTERVAL = 0.5                  # seconds between osu.ppy.sh requests
_osu_lock = threading.Lock()
_osu_last = 0.0


def _osu_throttle():
    """Block just long enough to keep osu.ppy.sh requests >= _OSU_MIN_INTERVAL apart."""
    global _osu_last
    with _osu_lock:
        wait = _OSU_MIN_INTERVAL - (time.monotonic() - _osu_last)
        if wait > 0:
            time.sleep(wait)
        _osu_last = time.monotonic()


# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
# Branding. APP_TITLE is the one place to change the product name -- it drives
# the window title and the header wordmark.
APP_TITLE = "Circlewave"
APP_VERSION = "1.2.1"
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
HINA_STATUS = {"all": [1, 3, 4, 0, -2],          # distinct buckets only (2==1, -1==0)
               "ranked": [1], "qualified": [3], "loved": [4],
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
    checksum: str = ""      # per-diff .osu md5 (for update detection); "" if unknown


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
                checksum=str(b.get("checksum", "") or ""),
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
                checksum=str(b.get("FileMD5", "") or ""),
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
    r = SESSION.get(NERINYAN_SEARCH, params=params,
                     headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("beatmapsets", [])


def fetch_card_meta(set_id: int) -> tuple:
    """Fetch full osu!-web metadata for one set from osu.direct (BPM, play and
    favourite counts, accurate per-diff data) to enrich a hinamizawa card.
    Returns (set_id, Beatmapset)."""
    r = SESSION.get(OSU_DIRECT_SET.format(id=set_id),
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
    except Exception as e:
        log.info("hinamizawa search failed (%s); falling back to nerinyan", e)
        if token is None:                  # fall back only on a fresh search; a
            return _search_nerinyan(filters, None)   # paging token isn't portable
        return [], None


def _hina_get(params: dict, status_code=None) -> list:
    """Fetch one page and parse to Beatmapsets. If a status_code is given, it's
    sent as the `status` filter AND used to tag each result (the response's
    RankedStatus field is unreliable, so the queried code is authoritative)."""
    if status_code is not None:
        params = dict(params, status=status_code)
    r = SESSION.get(HINA_SEARCH, params=params,
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
    """Sort a merged/field-scoped result set locally. Title/artist sort on those
    fields; ranked/updated use the set id as an age proxy (lower id = older), since
    the search response has no date/play fields. Other sorts keep their order."""
    if sort_key == "title_asc":
        return sorted(sets, key=lambda s: s.title.lower())
    if sort_key == "title_desc":
        return sorted(sets, key=lambda s: s.title.lower(), reverse=True)
    if sort_key == "artist_asc":
        return sorted(sets, key=lambda s: s.artist.lower())
    if sort_key == "ranked_asc":
        return sorted(sets, key=lambda s: s.id)
    if sort_key in ("ranked_desc", "updated_desc"):
        return sorted(sets, key=lambda s: s.id, reverse=True)
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
    multi = len(codes) > 1                                  # only "Any" now

    if field_scope or multi:
        # One batch per status code, merged into a single (unpaginated) result.
        # Field scope fetches by relevance so the artist's catalogue clusters in;
        # otherwise we server-sort each code. Either way we re-sort the *merged*
        # list locally so statuses interleave instead of appending in blocks
        # (which made "oldest" show old maps then a wall of fresh qualified ones).
        sort = HINA_SORT.get(filters.get("sort"))
        if sort and not field_scope:
            base["sort"] = sort
        sets, have = [], set()
        for c in codes:
            for s in _hina_get(dict(base), c):
                if s.id not in have:
                    have.add(s.id)
                    sets.append(s)
        if field_scope:
            ranked = [(rk, s) for s in sets
                      for rk in (_field_rank(getter(s), q),) if rk >= 0]
            ranked.sort(key=lambda t: t[0])
            sets = [s for _, s in ranked]
        sets = _client_sort(sets, filters.get("sort"))
        next_token = None
    else:
        # Single status: page server-side via offset, server-sorted.
        sort = HINA_SORT.get(filters.get("sort"))
        if sort:
            base["sort"] = sort
        raw = _hina_get(dict(base, offset=offset), codes[0])
        sets = list(raw)
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
    except FileNotFoundError:
        return set()
    except Exception as e:  # noqa: BLE001 - corrupt file shouldn't crash the app
        log.warning("could not read download history %s: %s", path, e)
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
    hashes = []
    try:
        with zipfile.ZipFile(osz_path) as z:
            for name in z.namelist():
                if name.lower().endswith(".osu"):
                    hashes.append(hashlib.md5(z.read(name)).hexdigest())
    except (zipfile.BadZipFile, OSError):
        pass
    return hashes


def _dedup(hashes) -> list:
    """Drop empties and duplicates, preserving first-seen order."""
    seen, out = set(), []
    for h in hashes:
        if h and h not in seen:
            seen.add(h); out.append(h)
    return out


def _mutate_collections(db_path: Path, fn) -> None:
    """Read collection.db, back it up, apply fn(collections)->collections, write back.

    Every mutation goes through here so they all get the same .bak safety net.
    Designed to run with osu! closed.
    """
    db_path = Path(db_path)
    version, collections = read_collection_db(db_path)
    if db_path.exists():
        backup = db_path.with_suffix(db_path.suffix + ".bak")
        try:
            backup.write_bytes(db_path.read_bytes())
        except OSError:
            pass
    write_collection_db(db_path, version or DEFAULT_DB_VERSION, fn(collections))


def list_collections(db_path: Path) -> list:
    """Return [(name, map_count), ...] for the collections in collection.db."""
    _, collections = read_collection_db(db_path)
    return [(n, len(h)) for (n, h) in collections]


def upsert_collection(db_path: Path, name: str, hashes: list) -> str:
    """Create/replace a named collection in collection.db (backing up first).

    Returns a short status string.
    """
    uniq = _dedup(hashes)

    def _fn(collections):
        collections = [(n, h) for (n, h) in collections if n != name]
        collections.append((name, uniq))
        return collections

    _mutate_collections(db_path, _fn)
    return f"{len(uniq)} maps in collection \u201c{name}\u201d"


def delete_collection(db_path: Path, name: str) -> bool:
    """Remove a collection. Returns True if it existed."""
    existed = [False]

    def _fn(collections):
        out = [(n, h) for (n, h) in collections if n != name]
        existed[0] = len(out) != len(collections)
        return out

    _mutate_collections(db_path, _fn)
    return existed[0]


def rename_collection(db_path: Path, old: str, new: str) -> bool:
    """Rename a collection. If `new` already exists, the two are merged (deduped)
    into `new`. Returns True if `old` existed."""
    if old == new:
        return False
    existed = [False]

    def _fn(collections):
        by_name = dict(collections)
        if old not in by_name:
            return collections
        existed[0] = True
        merged = _dedup(list(by_name.get(new, [])) + list(by_name[old]))
        out = []
        placed = False
        for n, h in collections:
            if n == old:
                if not placed:              # put the merged result at old's slot
                    out.append((new, merged)); placed = True
            elif n == new:
                continue                    # folded into merged
            else:
                out.append((n, h))
        return out

    _mutate_collections(db_path, _fn)
    return existed[0]


def merge_collections(db_path: Path, names: list, into: str) -> int:
    """Combine every collection in `names` into one called `into` (deduped),
    removing the sources. Returns the resulting map count."""
    names = set(names)
    result = [0]

    def _fn(collections):
        by_name = dict(collections)
        combined = []
        for n in list(names) + ([into] if into not in names else []):
            combined += list(by_name.get(n, []))
        combined = _dedup(combined)
        result[0] = len(combined)
        out = [(n, h) for (n, h) in collections if n not in names and n != into]
        out.append((into, combined))
        return out

    _mutate_collections(db_path, _fn)
    return result[0]


def preview_collection_merge(db_path: Path, name: str, hashes: list) -> dict:
    """Describe what upsert_collection() *would* do, without touching disk.

    Lets the GUI show a confirmation before modifying a user's collection.db.
    Returns a dict with:
      db_exists   : bool  -- is there an existing collection.db to merge into
      replacing   : bool  -- a collection with this name already exists
      new_maps    : int   -- unique, non-empty hashes that will be in the collection
      old_maps    : int   -- map count of the same-named collection being replaced
      kept        : [(name, count), ...]  -- other collections left untouched
    """
    db_path = Path(db_path)
    _, collections = read_collection_db(db_path)
    seen, uniq = set(), []
    for h in hashes:
        if h and h not in seen:
            seen.add(h); uniq.append(h)
    existing = {n: h for (n, h) in collections}
    return {
        "db_exists": db_path.exists(),
        "replacing": name in existing,
        "new_maps": len(uniq),
        "old_maps": len(existing.get(name, [])),
        "kept": [(n, len(h)) for (n, h) in collections if n != name],
    }


def default_collection_db_path(songs_dir: str) -> Path:
    """collection.db lives in the osu! root, i.e. the parent of the Songs folder."""
    p = Path(songs_dir) if songs_dir else Path.home()
    return p.parent / "collection.db"


# ----------------------------------------------------------------------------
# LIBRARY / UPDATE / FORMAT HELPERS
# ----------------------------------------------------------------------------
def set_current_checksums(s: "Beatmapset") -> set:
    """The set of per-diff .osu md5s the mirror reports for this set (may be empty
    if the source didn't provide checksums)."""
    return {d.checksum for d in s.diffs if d.checksum}


def local_osz_is_outdated(osz_path, s: "Beatmapset"):
    """True if the local .osz is missing any of the set's current diff checksums
    (i.e. a newer version exists). False if up to date. None if we can't tell
    (no checksums known, or the file can't be read)."""
    current = set_current_checksums(s)
    if not current:
        return None
    local = set(md5s_from_osz(osz_path))
    if not local:
        return None
    return not current.issubset(local)


def library_stats(songs_dir: str) -> dict:
    """Cheap, top-level library summary: how many beatmapsets are present and the
    total size of the .osz files sitting in the download folder. Extracted map
    folders are counted but not deep-walked (kept fast for huge libraries)."""
    ids = scan_downloaded_ids(songs_dir)
    osz_bytes = osz_files = 0
    p = Path(songs_dir) if songs_dir else None
    if p and p.is_dir():
        try:
            for entry in p.iterdir():
                try:
                    if entry.is_file() and entry.suffix.lower() == ".osz":
                        osz_bytes += entry.stat().st_size
                        osz_files += 1
                except OSError:
                    continue
        except OSError:
            pass
    return {"count": len(ids), "osz_files": osz_files, "osz_bytes": osz_bytes}


def fmt_size(nbytes: int) -> str:
    """Human-readable byte size (e.g. '4.2 GB')."""
    n = float(nbytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_speed(bytes_per_sec: float) -> str:
    """Download speed as '3.4 MB/s'."""
    if not bytes_per_sec or bytes_per_sec < 0:
        return ""
    return f"{fmt_size(bytes_per_sec)}/s"


def fmt_eta(seconds) -> str:
    """Remaining time as 'm:ss' (or 'h:mm:ss'). '' when unknown."""
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ----------------------------------------------------------------------------
# DOWNLOAD QUEUE PERSISTENCE
# ----------------------------------------------------------------------------
# A queued Beatmapset is stored as a tiny dict -- just enough to rebuild the row
# and download it after a restart. Full metadata (diffs etc.) isn't needed to
# download, so we keep it minimal.
def queue_item_to_dict(s: "Beatmapset") -> dict:
    return {"id": s.id, "title": s.title, "artist": s.artist,
            "creator": s.creator, "status": s.status, "cover_url": s.cover_url}


def queue_item_from_dict(d: dict) -> "Beatmapset":
    variant = "card" + "@2x.jpg"
    sid = int(d.get("id", 0) or 0)
    return Beatmapset(
        id=sid, title=d.get("title", "(unknown)"), artist=d.get("artist", ""),
        creator=d.get("creator", ""), status=d.get("status", ""),
        bpm=0, play_count=0, favourite_count=0,
        cover_url=d.get("cover_url") or f"https://assets.ppy.sh/beatmaps/{sid}/covers/{variant}",
        diffs=[], minimal=True,
    )


def save_queue(path: Path, sets: list) -> None:
    """Persist the pending download queue (list of Beatmapset) to JSON."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps([queue_item_to_dict(s) for s in sets]))
    except OSError as e:
        log.warning("could not save download queue %s: %s", path, e)


def load_queue(path: Path) -> list:
    """Restore a persisted download queue. [] if missing/corrupt."""
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("could not read download queue %s: %s", path, e)
        return []
    return [queue_item_from_dict(d) for d in data if isinstance(d, dict) and d.get("id")]


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
    _osu_throttle()
    r = SESSION.get(url, headers={"User-Agent": DOWNLOAD_UA}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return parse_pack_list(r.text)


def fetch_most_played(username: str, limit: int) -> tuple:
    """Resolve a username/ID and return (username, [Beatmapset, ...]) for their
    most-played maps, deduped to beatmapsets and ordered by total play count.
    Uses the public profile JSON route -- no login or API key."""
    _osu_throttle()
    prof = SESSION.get(USER_PROFILE_URL.format(user=username),
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
        _osu_throttle()
        rr = SESSION.get(MOST_PLAYED_URL.format(id=uid, limit=n, offset=offset),
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
    r = SESSION.get(MEDAL_WIKI_URL, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
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
        _osu_throttle()
        r = SESSION.get(url, headers={"User-Agent": DOWNLOAD_UA}, timeout=HTTP_TIMEOUT)
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
