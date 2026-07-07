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


# --------------------------------------------------------------------------
# Collection manager operations
# --------------------------------------------------------------------------
def _make_db(tmp_path, cols):
    db = tmp_path / "collection.db"
    c.write_collection_db(db, c.DEFAULT_DB_VERSION, cols)
    return db


def test_list_collections(tmp_path):
    db = _make_db(tmp_path, [("A", ["1" * 32, "2" * 32]), ("B", ["3" * 32])])
    assert c.list_collections(db) == [("A", 2), ("B", 1)]


def test_delete_collection(tmp_path):
    db = _make_db(tmp_path, [("A", ["1" * 32]), ("B", ["2" * 32])])
    assert c.delete_collection(db, "A") is True
    assert c.list_collections(db) == [("B", 1)]
    assert c.delete_collection(db, "nope") is False


def test_rename_collection_simple(tmp_path):
    db = _make_db(tmp_path, [("Old", ["1" * 32]), ("Keep", ["2" * 32])])
    assert c.rename_collection(db, "Old", "New") is True
    names = dict(c.list_collections(db))
    assert names == {"New": 1, "Keep": 1}
    assert c.rename_collection(db, "ghost", "X") is False


def test_rename_into_existing_merges(tmp_path):
    db = _make_db(tmp_path, [("A", ["1" * 32, "2" * 32]), ("B", ["2" * 32, "3" * 32])])
    assert c.rename_collection(db, "A", "B") is True   # merge A into B, dedup shared "2"
    _, cols = c.read_collection_db(db)
    names = dict(cols)
    assert "A" not in names
    assert names["B"] == ["2" * 32, "3" * 32, "1" * 32]  # B's order first, then A's new


def test_merge_collections(tmp_path):
    db = _make_db(tmp_path, [("A", ["1" * 32]), ("B", ["2" * 32]), ("C", ["9" * 32])])
    count = c.merge_collections(db, ["A", "B"], into="Combined")
    assert count == 2
    names = dict(c.list_collections(db))
    assert names == {"C": 1, "Combined": 2}


def test_collection_ops_write_backup(tmp_path):
    db = _make_db(tmp_path, [("A", ["1" * 32])])
    before = db.read_bytes()
    c.delete_collection(db, "A")
    assert db.with_suffix(".db.bak").read_bytes() == before


# --------------------------------------------------------------------------
# Update detection
# --------------------------------------------------------------------------
def _osz(tmp_path, name, osu_contents):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        for i, data in enumerate(osu_contents):
            z.writestr(f"d{i}.osu", data)
    return p


def test_local_osz_up_to_date(tmp_path):
    osz = _osz(tmp_path, "s.osz", [b"aaa", b"bbb"])
    import hashlib
    hashes = [hashlib.md5(b).hexdigest() for b in (b"aaa", b"bbb")]
    s = c.Beatmapset(id=1, title="", artist="", creator="", status="", bpm=0,
                     play_count=0, favourite_count=0, cover_url="",
                     diffs=[c.Diff("osu", 1.0, 0, 0, "d", checksum=h) for h in hashes])
    assert c.local_osz_is_outdated(osz, s) is False


def test_local_osz_outdated(tmp_path):
    osz = _osz(tmp_path, "s.osz", [b"aaa"])
    s = c.Beatmapset(id=1, title="", artist="", creator="", status="", bpm=0,
                     play_count=0, favourite_count=0, cover_url="",
                     diffs=[c.Diff("osu", 1.0, 0, 0, "d", checksum="f" * 32)])
    assert c.local_osz_is_outdated(osz, s) is True


def test_update_detection_unknown_without_checksums(tmp_path):
    osz = _osz(tmp_path, "s.osz", [b"aaa"])
    s = c.Beatmapset(id=1, title="", artist="", creator="", status="", bpm=0,
                     play_count=0, favourite_count=0, cover_url="",
                     diffs=[c.Diff("osu", 1.0, 0, 0, "d")])  # no checksum
    assert c.local_osz_is_outdated(osz, s) is None


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
@pytest.mark.parametrize("n,out", [
    (0, "0 B"), (512, "512 B"), (1536, "1.5 KB"),
    (5 * 1024 * 1024, "5.0 MB"), (3 * 1024 ** 3, "3.0 GB"),
])
def test_fmt_size(n, out):
    assert c.fmt_size(n) == out


def test_fmt_speed_and_eta():
    assert c.fmt_speed(0) == ""
    assert c.fmt_speed(2 * 1024 * 1024) == "2.0 MB/s"
    assert c.fmt_eta(None) == ""
    assert c.fmt_eta(65) == "1:05"
    assert c.fmt_eta(3725) == "1:02:05"


def test_library_stats(tmp_path):
    (tmp_path / "123 Artist - Title.osz").write_bytes(b"x" * 2048)
    (tmp_path / "456 Other - Song.osz").write_bytes(b"y" * 1024)
    (tmp_path / "notes.txt").write_bytes(b"ignore me")
    st = c.library_stats(str(tmp_path))
    assert st["count"] == 2          # two setid-prefixed entries
    assert st["osz_files"] == 2
    assert st["osz_bytes"] == 3072


# --------------------------------------------------------------------------
# Download queue persistence
# --------------------------------------------------------------------------
def test_queue_roundtrip(tmp_path):
    sets = [
        c.Beatmapset(id=10, title="T1", artist="A1", creator="C1", status="ranked",
                     bpm=0, play_count=0, favourite_count=0, cover_url="http://c1"),
        c.Beatmapset(id=20, title="T2", artist="A2", creator="C2", status="loved",
                     bpm=0, play_count=0, favourite_count=0, cover_url="http://c2"),
    ]
    qf = tmp_path / "queue.json"
    c.save_queue(qf, sets)
    out = c.load_queue(qf)
    assert [s.id for s in out] == [10, 20]
    assert out[0].title == "T1" and out[0].artist == "A1"
    assert out[1].status == "loved"


def test_load_queue_missing(tmp_path):
    assert c.load_queue(tmp_path / "nope.json") == []


def test_queue_from_dict_synthesizes_cover():
    s = c.queue_item_from_dict({"id": 77})
    assert s.cover_url.endswith("/beatmaps/77/covers/card@2x.jpg")


# --------------------------------------------------------------------------
# Optional official osu! API (client-credentials) -- mocked HTTP
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


def test_fetch_oauth_token(monkeypatch):
    captured = {}
    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"access_token": "tok123", "expires_in": 86400})
    monkeypatch.setattr(c.SESSION, "post", fake_post)
    tok = c.fetch_oauth_token("15", "secret")
    assert tok == "tok123"
    assert captured["url"] == c.OAUTH_TOKEN_URL
    assert captured["json"]["grant_type"] == "client_credentials"
    assert captured["json"]["client_id"] == 15   # coerced to int


def test_fetch_oauth_token_requires_credentials():
    with pytest.raises(ValueError):
        c.fetch_oauth_token("", "")


def test_osu_api_beatmapset(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer tok"
        return _Resp({"id": 5, "title": "T", "artist": "A",
                      "beatmaps": [{"mode": "osu", "difficulty_rating": 4.2,
                                    "checksum": "d" * 32, "version": "X"}]})
    monkeypatch.setattr(c.SESSION, "get", fake_get)
    s = c.osu_api_beatmapset(5, "tok")
    assert s.id == 5
    assert s.diffs[0].checksum == "d" * 32     # checksums flow through for update checks


# --------------------------------------------------------------------------
# Local update scanning
# --------------------------------------------------------------------------
def test_find_local_osz(tmp_path):
    (tmp_path / "123 A - B.osz").write_bytes(b"PK\x03\x04")
    (tmp_path / "999 X - Y.osz").write_bytes(b"PK\x03\x04")
    got = c.find_local_osz(str(tmp_path), 123)
    assert got is not None and got.name.startswith("123")
    assert c.find_local_osz(str(tmp_path), 555) is None


def test_scan_local_updates(tmp_path):
    import hashlib
    # set 100 is up to date; set 200 is outdated (remote checksum not on disk)
    up_to_date = _osz(tmp_path, "100 A - B.osz", [b"one", b"two"])
    _osz(tmp_path, "200 C - D.osz", [b"old"])
    hashes_100 = [hashlib.md5(b).hexdigest() for b in (b"one", b"two")]

    def fetch(sid):
        if sid == 100:
            diffs = [c.Diff("osu", 1.0, 0, 0, "d", checksum=h) for h in hashes_100]
        else:
            diffs = [c.Diff("osu", 1.0, 0, 0, "d", checksum="f" * 32)]
        return c.Beatmapset(id=sid, title="", artist="", creator="", status="",
                            bpm=0, play_count=0, favourite_count=0, cover_url="",
                            diffs=diffs)

    seen = []
    out = c.scan_local_updates(str(tmp_path), fetch, progress=lambda d, t: seen.append((d, t)))
    assert [s.id for s in out] == [200]        # only the outdated one
    assert seen[-1][0] == seen[-1][1]          # progress reached total


def test_scan_local_updates_can_stop(tmp_path):
    _osz(tmp_path, "1 A - B.osz", [b"x"])
    _osz(tmp_path, "2 A - B.osz", [b"y"])
    calls = []
    def fetch(sid):
        calls.append(sid)
        raise RuntimeError("stop after first")
    c.scan_local_updates(str(tmp_path), fetch, should_stop=lambda: len(calls) >= 1)
    assert len(calls) == 1                      # stopped before the second file


# --------------------------------------------------------------------------
# Paste-a-link parsing
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,exp", [
    ("https://osu.ppy.sh/beatmapsets/12345", ("set", 12345)),
    ("https://osu.ppy.sh/beatmapsets/12345#osu/999", ("set", 12345)),
    ("osu.ppy.sh/s/777", ("set", 777)),
    ("https://osu.ppy.sh/beatmaps/456", ("beatmap", 456)),
    ("https://osu.ppy.sh/b/456", ("beatmap", 456)),
    ("  42  ", ("set", 42)),
    ("freedom dive", None),
    ("", None),
])
def test_parse_beatmap_ref(text, exp):
    assert c.parse_beatmap_ref(text) == exp


# --------------------------------------------------------------------------
# Collection export / import + hashing from sets
# --------------------------------------------------------------------------
def test_collections_export_import(tmp_path):
    db = tmp_path / "collection.db"
    c.write_collection_db(db, c.DEFAULT_DB_VERSION,
                          [("A", ["1" * 32]), ("B", ["2" * 32, "3" * 32])])
    data = c.collections_to_json(db)
    assert data["type"] == "collections" and len(data["collections"]) == 2
    assert [x["name"] for x in c.collections_to_json(db, names=["B"])["collections"]] == ["B"]

    db2 = tmp_path / "c2.db"
    c.write_collection_db(db2, c.DEFAULT_DB_VERSION, [("A", ["9" * 32])])
    n = c.import_collections_into_db(db2, data)
    assert n == 2
    got = dict(c.list_collections(db2))
    assert got["A"] == 2 and got["B"] == 2      # A merged (9.. + 1..), B added


def test_import_from_bare_list(tmp_path):
    db = tmp_path / "c.db"
    n = c.import_collections_into_db(db, [{"name": "X", "hashes": ["a" * 32, "a" * 32]}])
    assert n == 1 and dict(c.list_collections(db)) == {"X": 1}


def test_collection_hashes_for_sets():
    s1 = c.Beatmapset(id=1, title="", artist="", creator="", status="", bpm=0,
                      play_count=0, favourite_count=0, cover_url="",
                      diffs=[c.Diff("osu", 1, 0, 0, "d", checksum="a" * 32),
                             c.Diff("osu", 2, 0, 0, "d2", checksum="b" * 32)])
    s2 = c.Beatmapset(id=2, title="", artist="", creator="", status="", bpm=0,
                      play_count=0, favourite_count=0, cover_url="", diffs=[])

    def fetch(sid):
        return c.Beatmapset(id=sid, title="", artist="", creator="", status="", bpm=0,
                            play_count=0, favourite_count=0, cover_url="",
                            diffs=[c.Diff("osu", 3, 0, 0, "d", checksum="c" * 32),
                                   c.Diff("osu", 1, 0, 0, "dup", checksum="a" * 32)])

    out = c.collection_hashes_for_sets([s1, s2], fetch_fn=fetch)
    assert out == ["a" * 32, "b" * 32, "c" * 32]   # deduped; s2 enriched; dup dropped


# --------------------------------------------------------------------------
# osu!.db parser -- validated against a hand-built synthetic DB
# --------------------------------------------------------------------------
def _osu_db_bytes(beatmaps, version=20211111, player="me"):
    import struct as st

    def s(x):
        return c._write_osu_string(x)

    def i(n):
        return n.to_bytes(4, "little")

    def sh(n):
        return n.to_bytes(2, "little")

    def lg(n):
        return n.to_bytes(8, "little")

    out = bytearray()
    out += i(version) + i(3) + b"\x01" + lg(0) + s(player) + i(len(beatmaps))
    for bm in beatmaps:
        out += s("Artist") + s("") + s("Title") + s("") + s("creator")
        out += s(bm.get("diff", "Hard")) + s("a.mp3") + s(bm["md5"]) + s("f.osu")
        out += bytes([bm.get("status", 4)]) + sh(0) + sh(0) + sh(0) + lg(0)
        out += st.pack("<ffff", 5, 4, 6, 7) + st.pack("<d", 1.4)   # AR CS HP OD, SV
        out += i(1) + b"\x08" + i(0) + b"\x0d" + st.pack("<d", 3.5)  # std: 1 star pair
        out += i(0) + i(0) + i(0)                                    # taiko/ctb/mania: none
        out += i(60000) + i(90000) + i(1000)                        # drain/total/preview
        out += i(1) + st.pack("<dd", 500.0, 0.0) + b"\x01"          # 1 timing point
        out += i(bm.get("beatmap_id", 111)) + i(bm["set_id"]) + i(0)  # ids, thread
        out += b"\x00\x00\x00\x00" + sh(0) + st.pack("<f", 0.7)     # grades, offset, stack
        out += bytes([bm.get("mode", 0)]) + s("") + s("tags") + sh(0) + s("")  # mode, src, tags, online, font
        out += b"\x00" + lg(0) + b"\x00"                            # unplayed, lastplayed, osz2
        out += s(bm.get("folder", "123 X - Y")) + lg(0)            # folder, last checked
        out += b"\x00\x00\x00\x00\x00" + i(0) + b"\x00"           # 5 flags, last-mod, mania speed
    return bytes(out)


def test_read_osu_db_roundtrip(tmp_path):
    p = tmp_path / "osu!.db"
    p.write_bytes(_osu_db_bytes([
        {"md5": "a" * 32, "set_id": 100, "beatmap_id": 11, "folder": "100 X - Y", "status": 4},
        {"md5": "b" * 32, "set_id": 200, "beatmap_id": 22, "folder": "200 P - Q", "status": 1, "mode": 3},
    ]))
    db = c.read_osu_db(p)
    assert db["player"] == "me" and db["version"] == 20211111
    assert [b["set_id"] for b in db["beatmaps"]] == [100, 200]
    assert db["beatmaps"][0]["md5"] == "a" * 32
    assert db["beatmaps"][1]["mode"] == 3 and db["beatmaps"][1]["folder"] == "200 P - Q"
    assert c.osu_db_set_ids(p) == {100, 200}


def test_read_osu_db_with_entry_size(tmp_path):
    # Older layout (< 20191106) prefixes each beatmap with an entry-size int.
    import struct as st
    body = _osu_db_bytes([{"md5": "c" * 32, "set_id": 5, "folder": "5 A - B"}], version=20160408)
    # splice a (bogus) entry-size int right after the header (before the first record)
    # header = 4(ver)+4(folders)+1(bool)+8(long)+string(player)+4(count)
    hdr_end = 4 + 4 + 1 + 8 + len(c._write_osu_string("me")) + 4
    patched = body[:hdr_end] + st.pack("<i", 0) + body[hdr_end:]
    p = tmp_path / "osu!.db"
    p.write_bytes(patched)
    assert c.osu_db_set_ids(p) == {5}


def test_osu_db_bad_file_is_empty(tmp_path):
    p = tmp_path / "osu!.db"
    p.write_bytes(b"not a real database")
    assert c.osu_db_set_ids(p) == set()      # graceful -> caller falls back to folder scan
