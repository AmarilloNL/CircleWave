"""Unit tests for circlewave_core -- the Qt-free logic.

These import circlewave_core directly and must run without PySide6 installed,
so the whole suite stays fast and CI-light.
"""
import sys
import zipfile

import pytest

import circlewave_core as c


# --------------------------------------------------------------------------
# The core module must not drag in Qt -- that's the whole point of the split.
# --------------------------------------------------------------------------
def test_core_has_no_qt():
    assert "PySide6" not in sys.modules


# --------------------------------------------------------------------------
# HTTP session: pooling + retry/backoff
# --------------------------------------------------------------------------
def test_session_has_retry_adapter():
    import requests
    assert isinstance(c.SESSION, requests.Session)
    adapter = c.SESSION.get_adapter("https://example.com")
    retry = adapter.max_retries
    assert retry.total == 3
    assert 429 in retry.status_forcelist and 503 in retry.status_forcelist
    assert retry.backoff_factor == 0.5


def test_osu_throttle_enforces_min_interval(monkeypatch):
    # Each call reads monotonic() twice (wait calc + timestamp update).
    ticks = iter([100.0, 100.0,     # 1st call: elapsed since last(=0) is huge -> no wait
                  100.1, 100.1])    # 2nd call: only 0.1s since last -> waits 0.4s
    slept = []
    monkeypatch.setattr(c.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(c.time, "sleep", slept.append)
    c._osu_last = 0.0
    c._osu_throttle()
    c._osu_throttle()
    assert slept and abs(slept[0] - (c._OSU_MIN_INTERVAL - 0.1)) < 1e-6


# --------------------------------------------------------------------------
# ULEB128 / osu-string primitives
# --------------------------------------------------------------------------
@pytest.mark.parametrize("n", [0, 1, 0x7f, 0x80, 0x81, 300, 16384, 1_000_000])
def test_uleb128_roundtrip(n):
    buf = c._write_uleb128(n)
    val, pos = c._read_uleb128(buf, 0)
    assert val == n
    assert pos == len(buf)


@pytest.mark.parametrize("s", ["", "a", "hello world", "ümläut", "\U0001f3b5 osu"])
def test_osu_string_roundtrip(s):
    buf = c._write_osu_string(s)
    out, pos = c._read_osu_string(buf, 0)
    assert out == s
    assert pos == len(buf)


def test_osu_string_none_is_null_marker():
    assert c._write_osu_string(None) == b"\x00"
    out, pos = c._read_osu_string(b"\x00", 0)
    assert out == "" and pos == 1


def test_osu_string_bad_marker_raises():
    with pytest.raises(ValueError):
        c._read_osu_string(b"\x05", 0)


# --------------------------------------------------------------------------
# collection.db round-trip / merge (this is the data most likely to corrupt
# a user's osu! install, so it gets the most coverage)
# --------------------------------------------------------------------------
def test_collection_db_roundtrip(tmp_path):
    db = tmp_path / "collection.db"
    cols = [("Pack A", ["a" * 32, "b" * 32]), ("Pack B", [])]
    c.write_collection_db(db, c.DEFAULT_DB_VERSION, cols)
    version, out = c.read_collection_db(db)
    assert version == c.DEFAULT_DB_VERSION
    assert out == cols


def test_read_missing_db_returns_empty(tmp_path):
    version, cols = c.read_collection_db(tmp_path / "nope.db")
    assert version == c.DEFAULT_DB_VERSION
    assert cols == []


def test_upsert_creates_and_dedupes(tmp_path):
    db = tmp_path / "collection.db"
    h = "f" * 32
    msg = c.upsert_collection(db, "My Pack", [h, h, "", "e" * 32])
    assert "2 maps" in msg  # duplicate + empty dropped
    _, cols = c.read_collection_db(db)
    assert cols == [("My Pack", [h, "e" * 32])]


def test_upsert_preserves_other_collections_and_replaces_same_name(tmp_path):
    db = tmp_path / "collection.db"
    c.write_collection_db(db, c.DEFAULT_DB_VERSION,
                          [("Keep", ["1" * 32]), ("Target", ["old" + "0" * 29])])
    c.upsert_collection(db, "Target", ["2" * 32, "3" * 32])
    _, cols = c.read_collection_db(db)
    names = dict(cols)
    assert names["Keep"] == ["1" * 32]                 # untouched
    assert names["Target"] == ["2" * 32, "3" * 32]     # replaced, not appended
    assert len(cols) == 2                              # no duplicate "Target"


def test_upsert_writes_backup(tmp_path):
    db = tmp_path / "collection.db"
    c.write_collection_db(db, c.DEFAULT_DB_VERSION, [("Old", ["a" * 32])])
    original = db.read_bytes()
    c.upsert_collection(db, "New", ["b" * 32])
    bak = db.with_suffix(".db.bak")
    assert bak.exists()
    assert bak.read_bytes() == original                # backup is the pre-write state


def test_preview_collection_merge_new_db(tmp_path):
    prev = c.preview_collection_merge(tmp_path / "collection.db", "Pack",
                                      ["a" * 32, "a" * 32, "", "b" * 32])
    assert prev["db_exists"] is False
    assert prev["replacing"] is False
    assert prev["new_maps"] == 2          # dupe + empty dropped
    assert prev["old_maps"] == 0
    assert prev["kept"] == []


def test_preview_collection_merge_replacing(tmp_path):
    db = tmp_path / "collection.db"
    c.write_collection_db(db, c.DEFAULT_DB_VERSION,
                          [("Keep", ["1" * 32, "2" * 32]), ("Pack", ["9" * 32])])
    prev = c.preview_collection_merge(db, "Pack", ["a" * 32])
    assert prev["db_exists"] is True
    assert prev["replacing"] is True
    assert prev["old_maps"] == 1
    assert prev["new_maps"] == 1
    assert prev["kept"] == [("Keep", 2)]


def test_preview_does_not_write(tmp_path):
    db = tmp_path / "collection.db"
    c.preview_collection_merge(db, "Pack", ["a" * 32])
    assert not db.exists()                # dry-run must not create the file


def test_default_collection_db_path():
    p = c.default_collection_db_path("/home/u/osu/Songs")
    assert p.name == "collection.db"
    assert p.parent.name == "osu"


# --------------------------------------------------------------------------
# md5s_from_osz
# --------------------------------------------------------------------------
def test_md5s_from_osz(tmp_path):
    osz = tmp_path / "x.osz"
    with zipfile.ZipFile(osz, "w") as z:
        z.writestr("song.mp3", b"not a beatmap")
        z.writestr("easy.osu", b"osu file format v14")
        z.writestr("hard.OSU", b"another diff")
    hashes = c.md5s_from_osz(osz)
    assert len(hashes) == 2                       # both .osu (case-insensitive), not the mp3
    assert all(len(h) == 32 for h in hashes)


def test_md5s_from_bad_zip_is_empty(tmp_path):
    bad = tmp_path / "bad.osz"
    bad.write_bytes(b"this is not a zip")
    assert c.md5s_from_osz(bad) == []


# --------------------------------------------------------------------------
# Wiki / pack-page / pack-list parsers
# --------------------------------------------------------------------------
def test_parse_pack_medals():
    md = (
        "| Medal name | Requirement |\n"
        "| :-- | :-- |\n"
        "| Video Game Pack vol.1 | [pack](/beatmaps/packs/S1) |\n"
        "| Multi | [a](/beatmaps/packs/P2) and [b](/beatmaps/packs/P3) |\n"
        "| No pack here | just text |\n"
    )
    medals = c.parse_pack_medals(md)
    assert medals == [
        {"medal": "Video Game Pack vol.1", "tags": ["S1"]},
        {"medal": "Multi", "tags": ["P2", "P3"]},
    ]


def test_parse_pack_page_full():
    html = (
        '<a href="/beatmapsets/123" class="beatmap-pack-items__link">'
        '<span class="beatmap-pack-items__artist">xi</span>'
        '<span class="beatmap-pack-items__title"> - FREEDOM DiVE</span></a>'
        '<a href="/beatmapsets/123" class="beatmap-pack-items__link">'  # dup id
        '<span class="beatmap-pack-items__artist">xi</span>'
        '<span class="beatmap-pack-items__title"> - FREEDOM DiVE</span></a>'
    )
    assert c.parse_pack_page(html) == [(123, "xi - FREEDOM DiVE")]


def test_parse_pack_page_fallback_ids_only():
    html = '<a href="/beatmapsets/999">whatever</a> /beatmapsets/1000'
    out = c.parse_pack_page(html)
    assert (999, "") in out and (1000, "") in out


def test_parse_pack_list():
    html = (
        'data-pack-tag="S1"><a class="beatmap-pack__header foo">'
        '<div class="beatmap-pack__name">osu!taiko Beatmap Pack #410</div>'
        '<div class="beatmap-pack__details"><span class="beatmap-pack__date">2024-01-02</span>'
    )
    out = c.parse_pack_list(html)
    assert out == [{"tag": "S1", "name": "osu!taiko Beatmap Pack #410",
                    "date": "2024-01-02", "mode": "taiko"}]


@pytest.mark.parametrize("name,mode", [
    ("osu!taiko Beatmap Pack #1", "taiko"),
    ("osu!catch Pack", "fruits"),
    ("osu!mania Pack", "mania"),
    ("Standard osu! Pack", "osu"),
    ("Featured Artist: Camellia", ""),
])
def test_pack_mode(name, mode):
    assert c._pack_mode(name) == mode


# --------------------------------------------------------------------------
# Beatmapset model
# --------------------------------------------------------------------------
def test_beatmapset_from_json_and_properties():
    js = {
        "id": 42, "title": "T", "artist": "A", "creator": "C",
        "status": "RANKED", "bpm": 180, "play_count": 5, "favourite_count": 3,
        "tags": "x y", "covers": {"card@2x": "http://img"},
        "beatmaps": [
            {"mode": "osu", "difficulty_rating": 5.5, "bpm": 180, "total_length": 200, "version": "Insane"},
            {"mode": "osu", "difficulty_rating": 2.1, "bpm": 180, "total_length": 100, "version": "Normal"},
            {"mode": "taiko", "difficulty_rating": 3.0, "bpm": 180, "total_length": 150, "version": "Muzu"},
        ],
    }
    s = c.Beatmapset.from_json(js)
    assert s.id == 42 and s.status == "ranked" and s.cover_url == "http://img"
    assert s.sr_range == (2.1, 5.5)
    assert s.length == 200
    assert s.modes == ["osu", "taiko"]     # order of first appearance after sort


def test_beatmapset_from_json_synthesizes_cover():
    s = c.Beatmapset.from_json({"id": 7})
    assert s.cover_url.endswith("/beatmaps/7/covers/card@2x.jpg")


def test_beatmapset_from_pack_splits_name():
    s = c.Beatmapset.from_pack(500, "Camellia - Ghost")
    assert (s.artist, s.title) == ("Camellia", "Ghost")
    assert s.minimal and s.status == "pack"


def test_beatmapset_from_pack_no_separator():
    s = c.Beatmapset.from_pack(500, "JustATitle")
    assert (s.artist, s.title) == ("", "JustATitle")


# --------------------------------------------------------------------------
# Field ranking / matching
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value,query,rank", [
    ("xi", "xi", 0),               # exact
    ("xi feat. yy", "xi", 1),      # starts-with
    ("the xi band", "xi", 2),      # whole word
    ("the xiao", "xi", 3),         # loose: a non-leading word prefixes the token
    ("nothing", "xi", -1),         # no match
])
def test_field_rank(value, query, rank):
    assert c._field_rank(value, query) == rank
    assert c._field_match(value, query) == (rank >= 0)


# --------------------------------------------------------------------------
# passes_range (client-side BPM / star / length filter)
# --------------------------------------------------------------------------
def _mk(diffs):
    return c.Beatmapset(id=1, title="", artist="", creator="", status="",
                        bpm=0, play_count=0, favourite_count=0, cover_url="", diffs=diffs)


def _f(**kw):
    base = {"bpm_min": 0, "bpm_max": 0, "sr_min": 0, "sr_max": 0,
            "len_min": 0, "len_max": 0, "mode": None}
    base.update(kw)
    return base


def test_passes_range_stars():
    s = _mk([c.Diff("osu", 3.0, 180, 120, "N"), c.Diff("osu", 6.0, 180, 200, "X")])
    assert c.passes_range(s, _f(sr_min=5, sr_max=0))       # has a 6-star diff
    assert c.passes_range(s, _f(sr_min=0, sr_max=4))       # has a 3-star diff
    assert not c.passes_range(s, _f(sr_min=7, sr_max=0))   # nothing >= 7


def test_passes_range_bpm_and_length():
    s = _mk([c.Diff("osu", 3.0, 150, 90, "N")])
    assert c.passes_range(s, _f(bpm_min=140, bpm_max=160))
    assert not c.passes_range(s, _f(bpm_min=200, bpm_max=0))
    assert c.passes_range(s, _f(len_min=60, len_max=120))
    assert not c.passes_range(s, _f(len_min=120, len_max=0))
