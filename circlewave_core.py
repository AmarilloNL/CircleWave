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
import struct
import difflib
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
APP_VERSION = "2.4.0"
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

# Optional official osu! API v2 (client-credentials grant = app/"guest" token, no
# user login). Enables higher rate limits, authoritative per-diff checksums (used
# for map-update detection) and clean search. Off unless the user sets credentials.
OAUTH_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
OSU_API_BASE = "https://osu.ppy.sh/api/v2"

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
#
# The order below is a sensible *default* (fastest/most-reliable first), but real
# per-mirror speed varies a lot by map (CDN cache hit vs cold generate), time of
# day, and rate-limit state -- so at download time the queue reorders mirrors by
# the speed/reliability it actually measured this session (see MirrorStats /
# order_mirrors). The static order just seeds that.
#
# osu.direct's plain /d/{id} is the download endpoint; its /api/d/ path rate-limits
# aggressively (429), so use /d/. chimu.moe and kitsu.moe folded into osu.direct.
MIRRORS = [
    {"name": "catboy",     "full": "https://catboy.best/d/{id}",                           "novideo": "https://catboy.best/d/{id}?n=1"},
    {"name": "osu.direct", "full": "https://osu.direct/d/{id}",                            "novideo": "https://osu.direct/d/{id}?noVideo=1"},
    {"name": "beatconnect","full": "https://beatconnect.io/b/{id}",                        "novideo": None},
    {"name": "nerinyan",   "full": "https://api.nerinyan.moe/d/{id}",                      "novideo": "https://api.nerinyan.moe/d/{id}?noVideo=true"},
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
        # Distinct modes present, in canonical game order (osu!, taiko, catch,
        # mania) -- not the diff list's star-rating order, which would render a
        # hybrid set awkwardly as e.g. "taiko osu!" when the taiko diff is easier.
        seen = {d.mode for d in self.diffs}
        order = {"osu": 0, "taiko": 1, "fruits": 2, "mania": 3}
        return sorted(seen, key=lambda m: order.get(m, 99))

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

    # Approved maps (RankedStatus 2 -- e.g. the original FREEDOM DiVE, and most
    # 2012-era marathons) are "ranked" for leaderboard/pp purposes, but the mirror
    # can't isolate them: a status=2 query returns ordinary ranked maps, so they
    # only ever surface in a *no-status* query. When the user wants ranked/any,
    # pull them in with one unfiltered fetch (first page only) and merge the
    # approved hits, tagged by their real RankedStatus. See _search_hinamizawa.
    if token is None and q and filters.get("status") in ("ranked", "all"):
        approved_base = {k: v for k, v in base.items() if k not in ("sort", "offset")}
        try:
            extra = [s for s in _hina_get(approved_base) if s.status == "approved"]
        except Exception as e:  # noqa: BLE001
            log.info("approved-map merge failed (%s)", e)
            extra = []
        if extra:
            have = {s.id for s in sets}
            sets = sets + [s for s in extra if s.id not in have]
            sets = _client_sort(sets, filters.get("sort"))

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


def collection_hashes_for_sets(sets, fetch_fn=None) -> list:
    """All unique per-diff .osu checksums across `sets`, for building a collection
    from hand-picked maps. A set whose diffs carry no checksums (e.g. a mirror
    search result) is enriched via fetch_fn(set_id)->Beatmapset when provided."""
    out = []
    for s in sets:
        cs = [d.checksum for d in s.diffs if d.checksum]
        if not cs and fetch_fn:
            try:
                cs = [d.checksum for d in fetch_fn(s.id).diffs if d.checksum]
            except Exception as e:  # noqa: BLE001
                log.debug("checksum fetch failed for %s: %s", getattr(s, "id", "?"), e)
        out.extend(cs)
    return _dedup(out)


# ---- collection export / import (shareable, no server) ----------------------
def collections_to_json(db_path: Path, names=None) -> dict:
    """Serialise collections to a portable dict a friend can import. `names`
    limits the export; None exports all."""
    _, cols = read_collection_db(db_path)
    if names is not None:
        keep = set(names)
        cols = [(n, h) for (n, h) in cols if n in keep]
    return {"app": "circlewave", "type": "collections", "version": 1,
            "collections": [{"name": n, "hashes": h} for (n, h) in cols]}


def import_collections_into_db(db_path: Path, data) -> int:
    """Merge exported collections (dict from collections_to_json, or a bare list)
    into collection.db. Same-named collections are merged, not overwritten.
    Returns how many collections were imported."""
    if isinstance(data, dict):
        items = data.get("collections", [])
    elif isinstance(data, list):
        items = data
    else:
        items = []
    existing = dict(read_collection_db(db_path)[1])
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        hashes = [h for h in (it.get("hashes") or []) if isinstance(h, str)]
        if not name or not hashes:
            continue
        merged = _dedup(list(existing.get(name, [])) + hashes)
        upsert_collection(db_path, name, merged)
        existing[name] = merged
        n += 1
    return n


# ----------------------------------------------------------------------------
# osu!.db  (osu!stable's master beatmap database -- exact installed maps + hashes)
# ----------------------------------------------------------------------------
class _DbReader:
    """Little-endian cursor over an osu! binary DB (osu!.db / collection.db)."""
    def __init__(self, buf: bytes):
        self.b = buf
        self.p = 0

    def byte(self):
        v = self.b[self.p]; self.p += 1
        return v

    def boolean(self):
        return self.byte() != 0

    def short(self):
        v = int.from_bytes(self.b[self.p:self.p + 2], "little"); self.p += 2
        return v

    def integer(self):
        v = int.from_bytes(self.b[self.p:self.p + 4], "little"); self.p += 4
        return v

    def long(self):
        v = int.from_bytes(self.b[self.p:self.p + 8], "little"); self.p += 8
        return v

    def single(self):
        v = struct.unpack_from("<f", self.b, self.p)[0]; self.p += 4
        return v

    def double(self):
        v = struct.unpack_from("<d", self.b, self.p)[0]; self.p += 8
        return v

    def string(self):
        s, self.p = _read_osu_string(self.b, self.p)
        return s

    def int_double_pair(self):
        # A star-rating entry: an Int TLV then a floating-point TLV.
        #   0x08 <Int32 mod-combo>  then  <type><value>
        # The value type varies by osu!.db version: 0x0d = Double (8 bytes, older
        # builds), 0x0c = Single/float (4 bytes, current builds -- osu! shrank
        # these to save space). Read the tag and size the value accordingly, or
        # the whole record desyncs (every star pair would be off by 4 bytes).
        self.byte(); self.integer()
        tag = self.byte()
        if tag == 0x0c:
            self.single()
        elif tag == 0x0d:
            self.double()
        else:                       # unknown marker: assume legacy Double
            self.double()


def read_osu_db(path, max_beatmaps=None) -> dict:
    """Parse osu!stable's osu!.db into {version, player, beatmaps:[...]}, where each
    beatmap is {md5, beatmap_id, set_id, folder, status, mode, diff, artist,
    title, creator, total_time}.

    Follows the documented layout (osu! wiki / OsuParsers). Raises on malformed
    input -- callers should fall back to a folder scan on any exception. Only the
    modern layout (version >= 20140609) is supported; older DBs raise."""
    r = _DbReader(Path(path).read_bytes())
    version = r.integer()
    if version < 20140609:
        raise ValueError(f"unsupported osu!.db version {version}")
    folder_count = r.integer()
    r.boolean()              # account unlocked
    r.long()                 # unlock date
    player = r.string()
    count = r.integer()
    has_entry_size = version < 20191106       # removed in 20191106
    beatmaps = []
    for _ in range(count):
        if has_entry_size:
            r.integer()                        # entry size in bytes
        artist = r.string(); r.string()        # artist, artist unicode
        title = r.string(); r.string()         # title, title unicode
        creator = r.string()                   # creator
        diff = r.string()                      # difficulty (version) name
        r.string()                             # audio filename
        md5 = r.string()
        r.string()                             # .osu filename
        status = r.byte()
        r.short(); r.short(); r.short()        # hitcircles, sliders, spinners
        r.long()                               # last modified
        r.single(); r.single(); r.single(); r.single()   # AR, CS, HP, OD
        r.double()                             # slider velocity
        for _mode in range(4):                 # star-rating pairs per mode
            for _ in range(r.integer()):
                r.int_double_pair()
        r.integer()                            # drain time
        total_time = r.integer()               # total time (ms)
        r.integer()                            # preview time
        for _ in range(r.integer()):           # timing points
            r.double(); r.double(); r.boolean()
        beatmap_id = r.integer()
        set_id = r.integer()
        r.integer()                            # thread id
        r.byte(); r.byte(); r.byte(); r.byte() # grades (std/taiko/ctb/mania)
        r.short()                              # local offset
        r.single()                             # stack leniency
        mode = r.byte()
        r.string()                             # source
        r.string()                             # tags
        r.short()                              # online offset
        r.string()                             # title font
        r.boolean()                            # unplayed
        r.long()                               # last played
        r.boolean()                            # is osz2
        folder = r.string()
        r.long()                               # last checked against repo
        r.boolean(); r.boolean(); r.boolean(); r.boolean(); r.boolean()  # ignore/disable flags
        r.integer()                            # last modification time
        r.byte()                               # mania scroll speed
        beatmaps.append({"md5": md5, "beatmap_id": beatmap_id, "set_id": set_id,
                         "folder": folder, "status": status, "mode": mode, "diff": diff,
                         "artist": artist, "title": title, "creator": creator,
                         "total_time": total_time})
        if max_beatmaps and len(beatmaps) >= max_beatmaps:
            break
    return {"version": version, "player": player,
            "folder_count": folder_count, "beatmaps": beatmaps}


def osu_db_set_ids(path) -> set:
    """Set of beatmapset ids present in osu!.db (exact installed list). Empty set
    on any parse error, so callers can fall back to a folder scan."""
    try:
        db = read_osu_db(path)
    except Exception as e:  # noqa: BLE001
        log.info("osu!.db parse failed (%s); falling back to folder scan", e)
        return set()
    return {b["set_id"] for b in db["beatmaps"] if b.get("set_id", 0) > 0}


def default_osu_db_path(songs_dir: str) -> Path:
    """osu!.db sits in the osu! root, next to the Songs folder."""
    p = Path(songs_dir) if songs_dir else Path.home()
    return p.parent / "osu!.db"


def resolve_beatmap_to_set(beatmap_id: int) -> int:
    """Resolve a single beatmap (difficulty) id to its beatmapset id via osu.direct."""
    r = SESSION.get(f"https://osu.direct/api/v2/b/{beatmap_id}",
                    headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        data = data[0] if data else {}
    sid = data.get("beatmapset_id") or (data.get("beatmapset") or {}).get("id")
    if not sid:
        raise RuntimeError(f"no beatmapset for beatmap {beatmap_id}")
    return int(sid)


def parse_beatmap_ref(text: str):
    """Recognise a pasted osu! beatmap reference. Returns ('set', id),
    ('beatmap', id), or None. Beatmapset links win over the '#osu/<diff>' fragment;
    a bare number is treated as a set id."""
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(r"beatmapsets/(\d+)", t) or re.search(r"/s/(\d+)", t)
    if m:
        return ("set", int(m.group(1)))
    m = re.search(r"(?:beatmaps|/b)/(\d+)", t)
    if m:
        return ("beatmap", int(m.group(1)))
    if t.isdigit():
        return ("set", int(t))
    return None


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


def find_local_osz(songs_dir: str, set_id: int):
    """The .osz file for a set id in the download folder, or None. (Only files are
    checkable for updates; extracted map *folders* have no single archive to hash.)"""
    p = Path(songs_dir) if songs_dir else None
    if not p or not p.is_dir():
        return None
    try:
        for entry in p.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".osz":
                m = re.match(r"^(\d+)\b", entry.name)
                if m and int(m.group(1)) == set_id:
                    return entry
    except OSError:
        pass
    return None


def scan_local_updates(songs_dir: str, fetch_fn, cap=None, progress=None,
                       should_stop=None) -> list:
    """Find downloaded .osz files that have a newer version online.

    `fetch_fn(set_id) -> Beatmapset` supplies authoritative per-diff checksums
    (osu.direct or the official API). Returns the outdated sets as Beatmapsets, so
    the caller can re-queue them. `progress(done, total)` and `should_stop()` are
    optional hooks for a worker thread.
    """
    p = Path(songs_dir) if songs_dir else None
    if not p or not p.is_dir():
        return []
    files = []
    try:
        for entry in p.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".osz":
                m = re.match(r"^(\d+)\b", entry.name)
                if m:
                    files.append((int(m.group(1)), entry))
    except OSError:
        return []
    if cap:
        files = files[:cap]
    total, outdated = len(files), []
    for i, (sid, path) in enumerate(files):
        if should_stop and should_stop():
            break
        try:
            s = fetch_fn(sid)
            if local_osz_is_outdated(path, s):
                outdated.append(s)
        except Exception as e:  # noqa: BLE001
            log.debug("update check failed for %s: %s", sid, e)
        if progress:
            progress(i + 1, total)
    return outdated


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


# ----------------------------------------------------------------------------
# OPTIONAL OFFICIAL osu! API (client-credentials)
# ----------------------------------------------------------------------------
def fetch_oauth_token(client_id: str, client_secret: str) -> str:
    """Get an app access token via the client-credentials grant (public scope).
    Raises on failure (bad credentials, network). No user login involved."""
    if not client_id or not client_secret:
        raise ValueError("client id and secret are required")
    r = SESSION.post(
        OAUTH_TOKEN_URL,
        json={"client_id": int(client_id), "client_secret": client_secret,
              "grant_type": "client_credentials", "scope": "public"},
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError("no access_token in response")
    return tok


def osu_api_beatmapset(set_id: int, token: str) -> "Beatmapset":
    """Fetch a set from the official API v2 (carries per-diff checksums, so it's
    the authoritative source for update detection)."""
    r = SESSION.get(
        f"{OSU_API_BASE}/beatmapsets/{set_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return Beatmapset.from_json(r.json())


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


# ============================================================================
# BATCH 1 -- fuzzy search, query syntax, collection tools, verification,
# download estimation, and mirror health. Pure/Qt-free so the test suite
# exercises them directly; the GUI wires these in on top.
# ============================================================================

# ---------------------------------------------------------------------------
# Fuzzy / typo-tolerant search
# ---------------------------------------------------------------------------
def fuzzy_score(query: str, value: str) -> float:
    """Similarity of `query` to `value` in [0.0, 1.0] (1.0 = identical).

    A substring match scores high regardless of length; otherwise we fall back
    to difflib's ratio on the best-aligned window. Case- and space-insensitive.
    Used so 'freedom dvie' still surfaces 'FREEDOM DiVE'."""
    q = (query or "").lower().strip()
    v = (value or "").lower().strip()
    if not q or not v:
        return 0.0
    if q == v:
        return 1.0
    if q in v:
        # substring: strong but not perfect; shorter host = closer match
        return 0.92 if len(q) >= 3 else 0.75
    return difflib.SequenceMatcher(None, q, v).ratio()


def fuzzy_rank_sets(sets: list, query: str, cutoff: float = 0.6) -> list:
    """Order `sets` by fuzzy closeness of the query to 'artist title creator',
    dropping anything below `cutoff`. A precise substring hit always wins over a
    loose ratio. Returns a new list; input is untouched. Meant as a client-side
    fallback when a strict search returns little/nothing."""
    scored = []
    for s in sets:
        hay = " ".join(x for x in (s.artist, s.title, s.creator) if x)
        best = max(fuzzy_score(query, hay),
                   fuzzy_score(query, s.title or ""),
                   fuzzy_score(query, s.artist or ""),
                   fuzzy_score(query, s.creator or ""))
        if best >= cutoff:
            scored.append((best, s))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [s for _, s in scored]


# ---------------------------------------------------------------------------
# Advanced query syntax: 'camellia star>6 bpm<200 mode=mania'
# ---------------------------------------------------------------------------
_MODE_ALIASES = {
    "osu": 0, "std": 0, "standard": 0, "o": 0,
    "taiko": 1, "t": 1,
    "ctb": 2, "fruits": 2, "catch": 2, "f": 2,
    "mania": 3, "m": 3,
}
# Maps a query-syntax field key to the `option` code the search actually consumes
# (see SEARCH_FIELDS / _FIELD_GETTERS) -- NOT the display label.
_FIELD_ALIASES = {
    "artist": "artist", "title": "title",
    "mapper": "creator", "creator": "creator",
}
_QUERY_TOKEN = re.compile(
    r'(?P<key>artist|title|mapper|creator|star|stars|sr|bpm|length|len|mode|status)'
    r'(?P<op>[<>=:])'
    r'(?P<val>"[^"]*"|\S+)',
    re.IGNORECASE,
)


def parse_query(text: str) -> dict:
    """Parse a search string with inline operators into a partial filter dict.

    Recognised: star/sr/bpm/length with < > = (e.g. star>6, bpm<=200 via bpm<200),
    mode=<name>, status=<word>, and field scopes artist=/title=/mapper=. Anything
    unrecognised stays as free text under 'q'. Only keys that were actually
    specified appear in the result, so callers can merge it onto a base filter."""
    out: dict = {}
    leftover: list = []
    pos = 0
    t = text or ""
    for m in _QUERY_TOKEN.finditer(t):
        leftover.append(t[pos:m.start()])
        pos = m.end()
        key = m.group("key").lower()
        op = m.group("op")
        val = m.group("val").strip('"')
        if key in ("star", "stars", "sr", "bpm", "length", "len"):
            try:
                num = float(val)
            except ValueError:
                leftover.append(m.group(0))
                continue
            base = {"star": "sr", "stars": "sr", "sr": "sr",
                    "bpm": "bpm", "length": "len", "len": "len"}[key]
            if op == ">":
                out[base + "_min"] = num
            elif op == "<":
                out[base + "_max"] = num
            else:                         # '=' / ':' -> narrow band around value
                out[base + "_min"] = num
                out[base + "_max"] = num
        elif key == "mode":
            mv = _MODE_ALIASES.get(val.lower())
            if mv is not None:
                out["mode"] = mv
            else:
                leftover.append(m.group(0))
        elif key == "status":
            out["status"] = val.lower()
        else:                             # field scope
            out["option"] = _FIELD_ALIASES[key]
            out["q"] = val
    leftover.append(t[pos:])
    free = re.sub(r"\s+", " ", "".join(leftover)).strip()
    if free and "q" not in out:
        out["q"] = free
    elif free and out.get("option"):
        # field scope already captured the value; keep the scoped term
        pass
    return out


# ---------------------------------------------------------------------------
# Collection diff / cleanup / stats
# ---------------------------------------------------------------------------
def diff_collections(a_hashes, b_hashes) -> dict:
    """Compare two hash lists. Returns {'added', 'removed', 'common'} as sorted
    lists: 'added' are in b not a, 'removed' are in a not b."""
    a, b = set(a_hashes or []), set(b_hashes or [])
    return {"added": sorted(b - a),
            "removed": sorted(a - b),
            "common": sorted(a & b)}


def find_empty_collections(collections) -> list:
    """Names of collections with no maps."""
    return [n for (n, h) in collections if not h]


def find_orphan_hashes(collections, known_md5s) -> dict:
    """Per-collection md5s that aren't present in `known_md5s` (e.g. from osu!.db).
    Returns {name: [orphan_md5, ...]} for collections that have any. These point at
    maps referenced by a collection but no longer installed."""
    known = {h.lower() for h in known_md5s}
    out = {}
    for name, hashes in collections:
        orphans = [h for h in hashes if h.lower() not in known]
        if orphans:
            out[name] = orphans
    return out


def find_subset_collections(collections) -> list:
    """Find collections whose maps are fully contained in another (bigger) one.
    Returns [(subset_name, superset_name), ...]. Useful for spotting redundant
    collections. Empty collections are ignored."""
    sets = [(n, set(h)) for (n, h) in collections if h]
    out = []
    for i, (na, sa) in enumerate(sets):
        for j, (nb, sb) in enumerate(sets):
            if i == j:
                continue
            if sa < sb or (sa == sb and i > j):   # proper subset, or dupe (keep one)
                out.append((na, nb))
                break
    return out


def collection_stats(hashes, db_beatmaps) -> dict:
    """Summarise a collection against an osu!.db beatmap list (from read_osu_db).
    Returns installed/missing counts and mode/status breakdowns of the installed
    maps. `db_beatmaps` is the 'beatmaps' list; matching is by md5."""
    by_md5 = {b["md5"].lower(): b for b in db_beatmaps if b.get("md5")}
    total = len(hashes)
    installed, by_mode, by_status = 0, {}, {}
    mode_names = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}
    for h in hashes:
        b = by_md5.get((h or "").lower())
        if not b:
            continue
        installed += 1
        mn = mode_names.get(b.get("mode", 0), "osu")
        by_mode[mn] = by_mode.get(mn, 0) + 1
        st = int(b.get("status", 0) or 0)
        by_status[st] = by_status.get(st, 0) + 1
    return {"total": total, "installed": installed, "missing": total - installed,
            "by_mode": by_mode, "by_status": by_status}


# ---------------------------------------------------------------------------
# Download verification + size estimation + dedupe
# ---------------------------------------------------------------------------
def verify_osz(osz_path, s: "Beatmapset") -> dict:
    """Check a downloaded .osz against the set's authoritative per-diff md5s.
    Returns {'ok', 'matched', 'missing', 'extra'}: 'ok' is True if every current
    checksum is present, False if any is missing, None if we can't tell (no
    known checksums, or the file can't be read)."""
    current = set_current_checksums(s)
    local = set(md5s_from_osz(osz_path))
    if not current:
        return {"ok": None, "matched": sorted(local), "missing": [], "extra": []}
    if not local:                          # unreadable / not a zip
        return {"ok": None, "matched": [], "missing": sorted(current),
                "extra": []}
    missing = current - local
    return {"ok": not missing,
            "matched": sorted(current & local),
            "missing": sorted(missing),
            "extra": sorted(local - current)}


# Rough average size of an .osz with video stripped -- used only to warn before a
# big batch, never for exactness. Tuned to typical ranked sets (~4-8 MB/diff).
AVG_OSZ_BYTES = 12 * 1024 * 1024


def estimate_download_size(sets, avg_bytes: int = AVG_OSZ_BYTES) -> int:
    """Heuristic total byte size for a batch, so callers can do a disk-space
    guard. Uses diff count where known (a multi-diff set is bigger), else a flat
    per-set average."""
    total = 0
    for s in sets:
        n = len(getattr(s, "diffs", []) or [])
        total += avg_bytes if n <= 1 else int(avg_bytes * (1 + 0.4 * (n - 1)))
    return total


def duplicate_targets(sets, owned_ids) -> list:
    """Subset of `sets` whose id is already owned -- for a 'you already have N of
    these' warning before queueing. Preserves order."""
    owned = set(owned_ids or [])
    return [s for s in sets if s.id in owned]


# ---------------------------------------------------------------------------
# Mirror health tracking -- prefer the mirror that's actually fast & reliable
# ---------------------------------------------------------------------------
class MirrorStats:
    """In-memory tally of per-mirror outcomes so the app can prefer the fastest,
    most reliable download source. Not persisted -- resets each run. Thread-safe
    for the download workers."""

    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def _row(self, name):
        return self._d.setdefault(
            name, {"ok": 0, "fail": 0, "bytes": 0, "secs": 0.0})

    def record(self, name: str, ok: bool, nbytes: int = 0, secs: float = 0.0):
        with self._lock:
            row = self._row(name)
            if ok:
                row["ok"] += 1
                row["bytes"] += max(0, int(nbytes))
                row["secs"] += max(0.0, float(secs))
            else:
                row["fail"] += 1

    def success_rate(self, name: str) -> float:
        row = self._d.get(name)
        if not row:
            return 0.0
        n = row["ok"] + row["fail"]
        return row["ok"] / n if n else 0.0

    def speed(self, name: str) -> float:
        """Average bytes/sec over successful downloads (0 if none measured)."""
        row = self._d.get(name)
        if not row or row["secs"] <= 0:
            return 0.0
        return row["bytes"] / row["secs"]

    def order(self, names: list) -> list:
        """`names` reordered best-first: proven-fast and reliable mirrors lead,
        untried mirrors keep their given order in the middle, repeat failers sink.
        Deterministic and total, so it's safe as a fallback chain."""
        def key(n):
            row = self._d.get(n)
            if not row or (row["ok"] + row["fail"]) == 0:
                return (1, 0.0, 0.0)          # untried: neutral middle
            return (0 if self.success_rate(n) >= 0.5 else 2,
                    -self.success_rate(n), -self.speed(n))
        return sorted(names, key=key)


def order_mirrors(mirrors, stats) -> list:
    """Reorder a list of mirror dicts (each with a 'name') best-first by their
    recorded health. Returns a new list; `mirrors` unchanged. `stats` may be None
    (returns a copy in the given order). Pure so it's unit-tested -- the download
    worker calls this instead of open-coding it (that open-coding shipped a crash:
    stats.order() returns names, which were wrongly indexed as dicts)."""
    if stats is None:
        return list(mirrors)
    order = stats.order([m["name"] for m in mirrors])
    rank = {name: i for i, name in enumerate(order)}
    return sorted(mirrors, key=lambda m: rank.get(m["name"], len(order)))


# ============================================================================
# BATCH 2 -- smart (rule-based) collections, osu!Collector import, and an
# offline search-result cache. Pure/Qt-free; exercised by the test suite.
# ============================================================================

# ---------------------------------------------------------------------------
# Smart / dynamic collections: a saved rule that re-materialises on demand
# ---------------------------------------------------------------------------
def _rule_defaults(rule: dict) -> dict:
    """Fill the keys passes_range() reads so a partial rule is safe to evaluate."""
    f = {"bpm_min": 0, "bpm_max": 0, "sr_min": 0, "sr_max": 0,
         "len_min": 0, "len_max": 0, "mode": None}
    f.update({k: v for k, v in (rule or {}).items() if v not in (None, "")})
    if "mode" not in rule:
        f["mode"] = None
    return f


def set_matches_rule(s: "Beatmapset", rule: dict) -> bool:
    """True if a set satisfies a smart-collection rule. The rule reuses the search
    filter shape (q, option, mode, sr/bpm/len min-max). Text is matched against the
    scoped field (option) or, unscoped, against artist/title/creator/tags."""
    f = _rule_defaults(rule)
    q = (rule.get("q") or "").strip()
    if q:
        getter = _FIELD_GETTERS.get(rule.get("option") or "")
        if getter:
            if not _field_match(getter(s), q):
                return False
        else:
            hay = " ".join(x for x in (s.artist, s.title, s.creator, s.tags) if x)
            if q.lower() not in hay.lower():
                return False
    return passes_range(s, f)


def filter_sets_by_rule(sets: list, rule: dict) -> list:
    """Materialise a smart collection: the subset of `sets` matching the rule,
    order preserved."""
    return [s for s in sets if set_matches_rule(s, rule)]


# ---------------------------------------------------------------------------
# osu!Collector import  (https://osucollector.com)
# ---------------------------------------------------------------------------
def osucollector_to_collections(data) -> dict:
    """Convert an osu!Collector collection (its API JSON) into the import format
    understood by import_collections_into_db: {'collections': [{name, hashes}]}.

    Tolerant of the two shapes the API returns -- a single collection object, or a
    list/paged wrapper of them. Per-diff md5s come from each beatmap's 'checksum'."""
    def _one(coll):
        name = (coll.get("name") or coll.get("title") or "").strip()
        hashes = []
        for bs in coll.get("beatmapsets", []) or []:
            for b in bs.get("beatmaps", []) or []:
                h = b.get("checksum") or b.get("md5") or b.get("hash")
                if isinstance(h, str) and h:
                    hashes.append(h)
        # some exports carry a flat 'beatmaps' list instead of nested beatmapsets
        for b in coll.get("beatmaps", []) or []:
            h = b.get("checksum") or b.get("md5")
            if isinstance(h, str) and h:
                hashes.append(h)
        return {"name": name, "hashes": _dedup(hashes)} if name and hashes else None

    if isinstance(data, dict) and "collections" in data and "beatmapsets" not in data:
        candidates = data["collections"]
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = [data]

    out = []
    for c in candidates:
        if isinstance(c, dict):
            one = _one(c)
            if one:
                out.append(one)
    return {"app": "osucollector", "type": "collections",
            "version": 1, "collections": out}


OSUCOLLECTOR_API = "https://osucollector.com/api/collections/{id}"
_OSUCOLLECTOR_ID = re.compile(r"osucollector\.com/collections/(\d+)")


def parse_osucollector_ref(text: str):
    """Extract a collection id from an osu!Collector URL or a bare number."""
    t = (text or "").strip()
    m = _OSUCOLLECTOR_ID.search(t)
    if m:
        return int(m.group(1))
    return int(t) if t.isdigit() else None


def fetch_osucollector(coll_id: int) -> dict:
    """Fetch one osu!Collector collection and return it in import format."""
    r = SESSION.get(OSUCOLLECTOR_API.format(id=int(coll_id)),
                    headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return osucollector_to_collections(r.json())


# ---------------------------------------------------------------------------
# Offline search cache -- persist result pages so browsing works (and is
# instant) without a round trip; entries expire after a TTL.
# ---------------------------------------------------------------------------
def _set_to_full_dict(s: "Beatmapset") -> dict:
    """Full serialisation (incl. diffs) so cached results still support the
    star/BPM/length filters that a minimal queue dict would drop."""
    return {
        "id": s.id, "title": s.title, "artist": s.artist, "creator": s.creator,
        "status": s.status, "bpm": s.bpm, "play_count": s.play_count,
        "favourite_count": s.favourite_count, "cover_url": s.cover_url,
        "tags": s.tags, "minimal": s.minimal,
        "diffs": [{"mode": d.mode, "sr": d.sr, "bpm": d.bpm, "length": d.length,
                   "version": d.version, "checksum": d.checksum} for d in s.diffs],
    }


def _set_from_full_dict(d: dict) -> "Beatmapset":
    diffs = [Diff(mode=x.get("mode", "osu"), sr=float(x.get("sr", 0) or 0),
                  bpm=float(x.get("bpm", 0) or 0), length=int(x.get("length", 0) or 0),
                  version=x.get("version", ""), checksum=x.get("checksum", ""))
             for x in d.get("diffs", []) or []]
    return Beatmapset(
        id=int(d.get("id", 0) or 0), title=d.get("title", "(unknown)"),
        artist=d.get("artist", ""), creator=d.get("creator", ""),
        status=d.get("status", ""), bpm=float(d.get("bpm", 0) or 0),
        play_count=int(d.get("play_count", 0) or 0),
        favourite_count=int(d.get("favourite_count", 0) or 0),
        cover_url=d.get("cover_url", ""), tags=d.get("tags", ""),
        minimal=bool(d.get("minimal", False)), diffs=diffs)


def search_cache_key(filters: dict) -> str:
    """Stable key for a first-page search: the filters that affect results, hashed.
    Excludes client-only toggles (hide_owned/no_video) that don't change the query."""
    relevant = {k: v for k, v in (filters or {}).items()
                if k not in ("hide_owned", "no_video")}
    blob = json.dumps(relevant, sort_keys=True, default=str)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def save_search_cache(cache_dir, filters: dict, sets: list) -> None:
    """Cache a first result page keyed by its filters. Best-effort."""
    try:
        d = Path(cache_dir)
        d.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.time(), "key": search_cache_key(filters),
                   "sets": [_set_to_full_dict(s) for s in sets]}
        (d / f"search_{search_cache_key(filters)}.json").write_text(json.dumps(payload))
    except OSError as e:
        log.info("could not write search cache: %s", e)


def load_search_cache(cache_dir, filters: dict, max_age: float = 86400.0):
    """Return cached sets for these filters if present and fresher than max_age
    seconds, else None."""
    try:
        p = Path(cache_dir) / f"search_{search_cache_key(filters)}.json"
        payload = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if time.time() - float(payload.get("ts", 0)) > max_age:
        return None
    return [_set_from_full_dict(d) for d in payload.get("sets", [])]


# ============================================================================
# BATCH 3 -- app self-update check against GitHub Releases.
# ============================================================================
GITHUB_REPO = "AmarilloNL/CircleWave"
GITHUB_LATEST_RELEASE = "https://api.github.com/repos/{repo}/releases/latest"


def parse_version(s: str) -> tuple:
    """Parse a version string (optionally 'v'-prefixed, e.g. 'v2.1.0') into a tuple
    of ints for comparison. Non-numeric trailers (e.g. '-rc1') are ignored."""
    s = (s or "").strip().lstrip("vV")
    parts = []
    for chunk in s.split("."):
        m = re.match(r"\d+", chunk)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def version_is_newer(candidate: str, current: str) -> bool:
    """True if `candidate` is a strictly newer version than `current`."""
    return parse_version(candidate) > parse_version(current)


def fetch_latest_release(repo: str = GITHUB_REPO) -> dict:
    """Query GitHub for the latest release. Returns {'tag', 'name', 'url'} (empty
    strings if unavailable). Never raises for the common failure modes -- returns
    empties so an update check can fail silently."""
    try:
        r = SESSION.get(GITHUB_LATEST_RELEASE.format(repo=repo),
                        headers={"User-Agent": USER_AGENT,
                                 "Accept": "application/vnd.github+json"},
                        timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.info("latest-release check failed: %s", e)
        return {"tag": "", "name": "", "url": ""}
    return {"tag": str(data.get("tag_name", "") or ""),
            "name": str(data.get("name", "") or ""),
            "url": str(data.get("html_url", "") or "")}


def check_for_app_update(current: str = APP_VERSION, repo: str = GITHUB_REPO):
    """Return {'tag','name','url'} of a newer release, or None if up to date /
    unreachable. Suitable to run in a background worker on startup."""
    rel = fetch_latest_release(repo)
    if rel.get("tag") and version_is_newer(rel["tag"], current):
        return rel
    return None


# ============================================================================
# BATCH 4 -- practice-set generator, smart-collection rule persistence,
# library dashboard + duplicate finder, and mapper/search follows.
# Pure/Qt-free; exercised by the test suite.
# ============================================================================

# ---------------------------------------------------------------------------
# Practice / progression set: N unowned maps in a target star (and mode) band
# ---------------------------------------------------------------------------
def build_practice_pool(sets, star_min=0.0, star_max=0.0, mode=None,
                        owned_ids=None, limit=30) -> list:
    """Pick up to `limit` sets in the given star (and optional mode) range that
    aren't already owned -- a ready-made progression collection. Order is
    preserved (the caller can pre-shuffle for variety)."""
    owned = set(owned_ids or [])
    f = {"sr_min": star_min or 0, "sr_max": star_max or 0,
         "bpm_min": 0, "bpm_max": 0, "len_min": 0, "len_max": 0, "mode": mode}
    mode_str = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}.get(mode)
    out = []
    for s in sets:
        if s.id in owned:
            continue
        # Strict mode: a std practice set shouldn't include mania-only maps.
        # (passes_range is lenient -- it falls back to all diffs -- so enforce
        # the mode ourselves before the star-range check.)
        if mode_str and not any(d.mode == mode_str for d in s.diffs):
            continue
        if not passes_range(s, f):
            continue
        out.append(s)
        if limit and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Smart-collection rules: saved dynamic filters that re-materialise on demand
# ---------------------------------------------------------------------------
def load_smart_rules(path) -> list:
    """Return saved smart-collection rules: [{'name', 'rule'}, ...]. [] if none."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, dict) and it.get("name") and isinstance(it.get("rule"), dict):
            out.append({"name": str(it["name"]), "rule": it["rule"]})
    return out


def save_smart_rules(path, rules) -> None:
    """Persist smart-collection rules (best-effort)."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        clean = [{"name": str(r["name"]), "rule": r["rule"]}
                 for r in rules if r.get("name") and isinstance(r.get("rule"), dict)]
        Path(path).write_text(json.dumps(clean, indent=2))
    except OSError as e:
        log.info("could not save smart rules: %s", e)


def upsert_smart_rule(rules, name, rule) -> list:
    """Add or replace a named rule in the list, returning a new list."""
    out = [r for r in rules if r.get("name") != name]
    out.append({"name": name, "rule": rule})
    return out


# ---------------------------------------------------------------------------
# Library dashboard + duplicate finder (from osu!.db / the Songs folder)
# ---------------------------------------------------------------------------
def library_dashboard(db_beatmaps) -> dict:
    """Summarise an osu!.db beatmap list: total difficulties, unique sets, and
    breakdowns by mode and by ranked status. (Star ratings aren't stored in the
    parsed rows, so no star histogram here.)"""
    mode_names = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}
    status_names = {0: "unknown", 1: "unsubmitted", 2: "pending",
                    4: "ranked", 5: "approved", 6: "qualified", 7: "loved"}
    by_mode, by_status, sets = {}, {}, set()
    for b in db_beatmaps:
        mn = mode_names.get(b.get("mode", 0), "osu")
        by_mode[mn] = by_mode.get(mn, 0) + 1
        sn = status_names.get(int(b.get("status", 0) or 0), "other")
        by_status[sn] = by_status.get(sn, 0) + 1
        if b.get("set_id", 0) > 0:
            sets.add(b["set_id"])
    return {"difficulties": len(db_beatmaps), "sets": len(sets),
            "by_mode": by_mode, "by_status": by_status}


_SETID_PREFIX = re.compile(r"^(\d+)\b")


def find_duplicate_song_folders(songs_dir) -> dict:
    """Find beatmapset ids that have more than one folder in the Songs directory
    (the classic osu! 'I downloaded this twice' clutter). Returns
    {set_id: [folder_name, ...]} only for ids with 2+ folders. osu! names each
    folder '<set_id> Artist - Title', so we group on the leading number."""
    groups = {}
    try:
        entries = list(os.scandir(songs_dir))
    except OSError:
        return {}
    for e in entries:
        if not e.is_dir():
            continue
        m = _SETID_PREFIX.match(e.name)
        if m:
            groups.setdefault(int(m.group(1)), []).append(e.name)
    return {sid: sorted(names) for sid, names in groups.items() if len(names) > 1}


def find_duplicate_osz(folder) -> dict:
    """Same idea for a download folder of .osz files: {set_id: [filename, ...]}
    for set ids with more than one .osz (e.g. leftover older versions)."""
    groups = {}
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return {}
    for e in entries:
        if not e.is_file() or not e.name.lower().endswith(".osz"):
            continue
        m = _SETID_PREFIX.match(e.name)
        if m:
            groups.setdefault(int(m.group(1)), []).append(e.name)
    return {sid: sorted(names) for sid, names in groups.items() if len(names) > 1}


# ---------------------------------------------------------------------------
# Follows: watch a mapper or a saved search for new maps
# ---------------------------------------------------------------------------
def follow_to_filters(follow) -> dict:
    """Build a search filter dict from a follow spec. A 'mapper' follow scopes the
    search to that creator; a 'search' follow carries its own filter dict."""
    base = {"q": "", "status": "ranked", "sort": "ranked_desc", "mode": None,
            "option": "", "genre": 0, "language": 0, "bpm_min": 0, "bpm_max": 0,
            "sr_min": 0, "sr_max": 0, "len_min": 0, "len_max": 0,
            "hide_owned": False, "no_video": False}
    if follow.get("type") == "mapper":
        base["q"] = follow.get("value", "")
        base["option"] = "creator"
    elif isinstance(follow.get("filters"), dict):
        base.update(follow["filters"])
    return base


def load_follows(path) -> list:
    """Return saved follows: [{'type','value'/'filters','label','seen':[ids]}]."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, dict) and it.get("type"):
            it.setdefault("seen", [])
            out.append(it)
    return out


def save_follows(path, follows) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(follows, indent=2))
    except OSError as e:
        log.info("could not save follows: %s", e)


def check_follow(follow, search_fn) -> tuple:
    """Run a follow's search via `search_fn(filters) -> (sets, token)` and return
    (new_sets, updated_follow). 'new' = sets whose id isn't in follow['seen'];
    the returned follow has its 'seen' list refreshed to the current result ids.
    On the very first check (no prior 'seen'), nothing is 'new' -- we just record
    the baseline, so the user isn't spammed with the whole back-catalogue."""
    sets, _ = search_fn(follow_to_filters(follow))
    ids = [s.id for s in sets]
    prior = follow.get("seen")
    updated = dict(follow, seen=ids)
    if not prior:                      # first run: establish baseline, no alerts
        return [], updated
    seen = set(prior)
    new = [s for s in sets if s.id not in seen]
    return new, updated


# ============================================================================
# BATCH 5 -- tournament/mappool importer, collection tracklist export,
# and duplicate cleanup. Pure/Qt-free; exercised by the test suite.
# ============================================================================

# ---------------------------------------------------------------------------
# Mappool / bulk-link importer: pull every beatmap reference out of pasted text
# ---------------------------------------------------------------------------
# The set pattern also swallows a trailing '#osu/<diff>' fragment so its span
# covers the whole URL -- otherwise that fragment is mis-read as a separate beatmap.
_SET_REF = re.compile(r"beatmapsets/(\d+)(?:#(?:osu|taiko|fruits|mania)/\d+)?|/s/(\d+)")
_MAP_REF = re.compile(r"(?:beatmaps|/b)/(\d+)|#(?:osu|taiko|fruits|mania)/(\d+)")


def parse_beatmap_refs(text: str) -> list:
    """Extract every osu! beatmap reference from a block of text (a forum mappool
    post, a spreadsheet paste, a list of links...). Returns an ordered, de-duped
    list of ('set', id) / ('beatmap', id) tuples.

    A beatmapset link wins for its span; a '#osu/<diff>' fragment on the same URL
    is ignored (the set link already covers it). Bare 5+ digit numbers on their
    own are treated as set ids so a plain id list still works."""
    refs, seen = [], set()

    def add(kind, num):
        key = (kind, num)
        if num and key not in seen:
            seen.add(key)
            refs.append(key)

    # spans already consumed by a beatmapset match, so we don't also read their
    # trailing #osu/<diff> fragment as a separate beatmap.
    consumed = []
    for m in _SET_REF.finditer(text or ""):
        consumed.append((m.start(), m.end()))
        add("set", int(m.group(1) or m.group(2)))
    for m in _MAP_REF.finditer(text or ""):
        if any(a <= m.start() < b for a, b in consumed):
            continue
        add("beatmap", int(m.group(1) or m.group(2)))
    # bare id list: numbers not part of any URL match
    urlspans = consumed + [(m.start(), m.end()) for m in _MAP_REF.finditer(text or "")]
    for m in re.finditer(r"(?<![\w/#])(\d{5,})(?![\w])", text or ""):
        if not any(a <= m.start() < b for a, b in urlspans):
            add("set", int(m.group(1)))
    return refs


# ---------------------------------------------------------------------------
# Collection tracklist export (needs osu!.db for names)
# ---------------------------------------------------------------------------
def collection_tracklist(hashes, db_beatmaps) -> list:
    """Human-readable lines for a collection's maps, resolving md5s to
    'Artist - Title [diff]' via osu!.db. Maps not installed are marked. Order
    follows the collection's hash order."""
    by_md5 = {b["md5"].lower(): b for b in db_beatmaps if b.get("md5")}
    lines = []
    for h in hashes:
        b = by_md5.get((h or "").lower())
        if b:
            name = f"{b.get('artist', '')} - {b.get('title', '')}".strip(" -")
            diff = b.get("diff", "")
            lines.append(f"{name} [{diff}]" if diff else name)
        else:
            lines.append(f"(not installed)  {h}")
    return lines


def format_tracklist(name, lines) -> str:
    """Wrap tracklist lines into a shareable text block with a header."""
    head = f"{name} — {len(lines)} maps"
    body = "\n".join(f"{i:>3}. {ln}" for i, ln in enumerate(lines, 1))
    return f"{head}\n{'=' * len(head)}\n{body}\n"


# ---------------------------------------------------------------------------
# Duplicate cleanup: pick which duplicate folders/files are redundant
# ---------------------------------------------------------------------------
def redundant_duplicates(dup_map, keep="first") -> list:
    """Given {set_id: [names...]} (from find_duplicate_song_folders/_osz), return
    the flat list of names that are safe to remove -- all but one kept per set.
    keep='first' keeps the alphabetically-first (usually the un-suffixed original,
    e.g. 'X' over 'X (1)')."""
    out = []
    for _sid, names in dup_map.items():
        ordered = sorted(names)
        keeper = ordered[0] if keep == "first" else ordered[-1]
        out.extend(n for n in ordered if n != keeper)
    return out


def move_paths_to_trash(base_dir, names, trash_name="_CircleWave_trash") -> tuple:
    """Move the given entries (folders or files, relative to base_dir) into a
    trash subfolder rather than hard-deleting, so a mistake is recoverable.
    Returns (moved_count, trash_dir). Best-effort per item."""
    import shutil
    base = Path(base_dir)
    trash = base / trash_name
    trash.mkdir(parents=True, exist_ok=True)
    moved = 0
    for name in names:
        src = base / name
        if not src.exists():
            continue
        dest = trash / name
        try:
            if dest.exists():                  # avoid clobbering a prior trashed copy
                dest = trash / f"{name}__{int(time.time())}"
            shutil.move(str(src), str(dest))
            moved += 1
        except OSError as e:
            log.info("could not trash %s: %s", src, e)
    return moved, str(trash)


# ============================================================================
# BATCH 6 -- download missing maps from a collection, per-mapper library stats.
# ============================================================================
OSU_DIRECT_MD5 = "https://osu.direct/api/v2/md5/{md5}"


def resolve_md5_to_set(md5: str) -> int:
    """Resolve a single .osu md5 checksum to its beatmapset id via osu.direct.
    Used to turn a collection's hashes (which is all a collection.db stores) back
    into downloadable sets. Raises on failure."""
    r = SESSION.get(OSU_DIRECT_MD5.format(md5=md5),
                    headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        data = data[0] if data else {}
    sid = (data.get("beatmapset_id") or data.get("set_id")
           or (data.get("beatmapset") or {}).get("id"))
    if not sid:
        raise RuntimeError(f"no beatmapset for md5 {md5}")
    return int(sid)


def missing_hashes(hashes, known_md5s) -> list:
    """The subset of `hashes` not present in `known_md5s` (case-insensitive),
    order-preserved and de-duped -- e.g. a collection's maps not in osu!.db."""
    known = {h.lower() for h in known_md5s}
    out, seen = [], set()
    for h in hashes:
        lo = (h or "").lower()
        if lo and lo not in known and lo not in seen:
            seen.add(lo)
            out.append(h)
    return out


def resolve_missing_to_sets(hashes, progress=None) -> list:
    """Resolve a list of md5s to distinct beatmapset ids (order-preserved), for
    queueing a download. Unresolvable hashes are skipped. `progress(i, total)` is
    called as it goes, if given."""
    out, seen = [], set()
    total = len(hashes)
    for i, h in enumerate(hashes, 1):
        try:
            sid = resolve_md5_to_set(h)
        except Exception as e:  # noqa: BLE001
            log.info("md5 %s did not resolve: %s", h, e)
            sid = None
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
        if progress:
            progress(i, total)
    return out


def top_mappers(db_beatmaps, limit=15) -> list:
    """Rank creators by how many distinct beatmapsets of theirs are installed.
    Returns [(creator, set_count), ...] descending. Counts sets, not difficulties,
    so a mapper with one huge set doesn't dominate."""
    seen, counts = set(), {}
    for b in db_beatmaps:
        creator = b.get("creator", "")
        sid = b.get("set_id", 0) or 0
        if not creator or sid <= 0:
            continue
        key = (creator, sid)
        if key in seen:
            continue
        seen.add(key)
        counts[creator] = counts.get(creator, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:limit]


# ============================================================================
# BATCH 7 -- recommendations ("For You"), similar maps, library length.
# ============================================================================
def similar_search_filter(s: "Beatmapset", band: float = 0.6) -> dict:
    """Build a search filter for maps 'like this one': the same mode and a star
    band around the set's hardest difficulty. Meant for a 'more like this' action
    from the detail panel."""
    lo, hi = s.sr_range
    center = hi or lo
    mode = None
    if s.diffs:
        top = max(s.diffs, key=lambda d: d.sr)
        mode = {"osu": 0, "taiko": 1, "fruits": 2, "mania": 3}.get(top.mode)
    return {"q": "", "status": "ranked", "sort": "plays_desc", "mode": mode,
            "option": "", "genre": 0, "language": 0,
            "bpm_min": 0, "bpm_max": 0,
            "sr_min": round(max(0.0, center - band), 2),
            "sr_max": round(center + band, 2),
            "len_min": 0, "len_max": 0, "hide_owned": True, "no_video": False}


def pick_unowned(sets, owned_ids, limit=0) -> list:
    """Filter out owned sets (and de-dupe by id), optionally capping to `limit`.
    Used to assemble recommendation results."""
    owned = set(owned_ids or [])
    out, seen = [], set()
    for s in sets:
        if s.id in owned or s.id in seen:
            continue
        seen.add(s.id)
        out.append(s)
        if limit and len(out) >= limit:
            break
    return out


def recommendation_mappers(db_beatmaps, top_n=6) -> list:
    """The creator names you own the most sets from -- the seeds for 'For You'
    recommendations. Thin wrapper over top_mappers returning just the names."""
    return [name for name, _ in top_mappers(db_beatmaps, limit=top_n)]


def library_total_length(db_beatmaps) -> int:
    """Approximate total mapped-audio length of the library, in seconds: one
    difficulty's total_time per beatmapset (so multi-diff sets aren't counted
    many times). osu!.db stores total_time in ms."""
    per_set = {}
    for b in db_beatmaps:
        sid = b.get("set_id", 0) or 0
        if sid > 0:
            per_set[sid] = max(per_set.get(sid, 0), int(b.get("total_time", 0) or 0))
    return sum(per_set.values()) // 1000


def fmt_hours(seconds: int) -> str:
    """'3h 42m' / '58m' from a second count."""
    m = max(0, seconds) // 60
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m"
