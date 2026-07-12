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


# ==========================================================================
# BATCH 1 -- fuzzy search, query syntax, collection tools, verification,
# download estimation, mirror health.
# ==========================================================================
def _mkset(sid=1, title="", artist="", creator="", diffs=None):
    return c.Beatmapset(
        id=sid, title=title, artist=artist, creator=creator, status="ranked",
        bpm=0, play_count=0, favourite_count=0, cover_url="", diffs=diffs or [])


def test_fuzzy_score_exact_and_substring():
    assert c.fuzzy_score("freedom dive", "FREEDOM DiVE") == 1.0
    assert c.fuzzy_score("freedom", "FREEDOM DiVE") >= 0.9
    assert c.fuzzy_score("", "anything") == 0.0


def test_fuzzy_score_typo_tolerant():
    # a one-char transposition should still score well above noise
    assert c.fuzzy_score("freedom dvie", "freedom dive") > 0.8
    assert c.fuzzy_score("freedom dvie", "brass beat") < 0.5


def test_fuzzy_rank_orders_and_filters():
    a = _mkset(1, title="FREEDOM DiVE", artist="xi")
    b = _mkset(2, title="Blue Zenith", artist="xi")
    ranked = c.fuzzy_rank_sets([b, a], "freedom dvie", cutoff=0.6)
    assert [s.id for s in ranked] == [1]        # b filtered out, a kept


@pytest.mark.parametrize("text,expected", [
    ("star>6", {"sr_min": 6.0}),
    ("star<8", {"sr_max": 8.0}),
    ("star=7", {"sr_min": 7.0, "sr_max": 7.0}),
    ("bpm>180", {"bpm_min": 180.0}),
    ("length<120", {"len_max": 120.0}),
    ("mode=mania", {"mode": 3}),
    ("mode=ctb", {"mode": 2}),
    ("status=loved", {"status": "loved"}),
])
def test_parse_query_operators(text, expected):
    assert c.parse_query(text) == expected


def test_parse_query_field_scope_and_freetext():
    out = c.parse_query('artist=camellia star>6')
    assert out["option"] == "artist" and out["q"] == "camellia"
    assert out["sr_min"] == 6.0
    assert c.parse_query("mapper=sotarks")["option"] == "creator"
    out2 = c.parse_query("blue zenith bpm<210")
    assert out2["q"] == "blue zenith" and out2["bpm_max"] == 210.0


def test_parse_query_ignores_garbage_number():
    out = c.parse_query("star>abc hello")
    assert "sr_min" not in out
    assert out["q"] == "star>abc hello"


def test_diff_collections():
    d = c.diff_collections(["a", "b", "c"], ["b", "c", "d"])
    assert d == {"added": ["d"], "removed": ["a"], "common": ["b", "c"]}


def test_find_empty_and_orphans():
    cols = [("full", ["a", "b"]), ("empty", []), ("half", ["a", "z"])]
    assert c.find_empty_collections(cols) == ["empty"]
    orph = c.find_orphan_hashes(cols, known_md5s={"a", "b"})
    assert orph == {"half": ["z"]}


def test_find_subset_collections():
    cols = [("big", ["a", "b", "c"]), ("small", ["a", "b"]), ("other", ["x"])]
    subs = c.find_subset_collections(cols)
    assert ("small", "big") in subs
    assert not any(n == "other" for n, _ in subs)


def test_collection_stats():
    db = [{"md5": "A", "mode": 3, "status": 4},
          {"md5": "B", "mode": 0, "status": 5}]
    st = c.collection_stats(["a", "b", "zzz"], db)
    assert st["total"] == 3 and st["installed"] == 2 and st["missing"] == 1
    assert st["by_mode"] == {"mania": 1, "osu": 1}


def test_verify_osz(tmp_path):
    import hashlib as _h
    osz = tmp_path / "x.osz"
    data = b"osu file format v14\n"
    with zipfile.ZipFile(osz, "w") as z:
        z.writestr("map [Easy].osu", data)
    md5 = _h.md5(data).hexdigest()
    good = _mkset(diffs=[c.Diff("osu", 3.0, 180, 60, "Easy", md5)])
    res = c.verify_osz(osz, good)
    assert res["ok"] is True and res["matched"] == [md5]
    bad = _mkset(diffs=[c.Diff("osu", 3.0, 180, 60, "Easy", "deadbeef")])
    assert c.verify_osz(osz, bad)["ok"] is False
    unknown = _mkset(diffs=[c.Diff("osu", 3.0, 180, 60, "Easy", "")])
    assert c.verify_osz(osz, unknown)["ok"] is None


def test_estimate_download_size():
    one = _mkset(diffs=[c.Diff("osu", 3, 180, 60, "E", "")])
    assert c.estimate_download_size([one]) == c.AVG_OSZ_BYTES
    multi = _mkset(diffs=[c.Diff("osu", 3, 180, 60, str(i), "") for i in range(5)])
    assert c.estimate_download_size([multi]) > c.AVG_OSZ_BYTES


def test_duplicate_targets():
    a, b = _mkset(1), _mkset(2)
    assert [s.id for s in c.duplicate_targets([a, b], {2})] == [2]


def test_mirror_stats_order():
    ms = c.MirrorStats()
    ms.record("fast", ok=True, nbytes=10_000_000, secs=1.0)
    ms.record("slow", ok=True, nbytes=1_000_000, secs=2.0)
    ms.record("broken", ok=False)
    ms.record("broken", ok=False)
    order = ms.order(["broken", "slow", "fast", "untried"])
    assert order[0] == "fast"                 # fastest reliable leads
    assert order[-1] == "broken"              # repeat failer sinks
    assert ms.success_rate("fast") == 1.0
    assert ms.speed("fast") == 10_000_000.0


# ==========================================================================
# BATCH 2 -- smart collections, osu!Collector import, offline search cache.
# ==========================================================================
def _mkset2(sid=1, title="", artist="", creator="", tags="", diffs=None):
    return c.Beatmapset(
        id=sid, title=title, artist=artist, creator=creator, status="ranked",
        bpm=0, play_count=0, favourite_count=0, cover_url="", tags=tags,
        diffs=diffs or [])


def test_set_matches_rule_stars_and_text():
    s = _mkset2(1, title="FREEDOM DiVE", artist="xi",
               diffs=[c.Diff("osu", 7.5, 222, 253, "FOUR DIMENSIONS", "")])
    assert c.set_matches_rule(s, {"sr_min": 7.0})
    assert not c.set_matches_rule(s, {"sr_min": 8.0})
    assert c.set_matches_rule(s, {"q": "freedom"})
    assert not c.set_matches_rule(s, {"q": "zenith"})
    # field-scoped text
    assert c.set_matches_rule(s, {"q": "xi", "option": "artist"})
    assert not c.set_matches_rule(s, {"q": "xi", "option": "title"})


def test_filter_sets_by_rule():
    a = _mkset2(1, diffs=[c.Diff("osu", 6.5, 180, 100, "A", "")])
    b = _mkset2(2, diffs=[c.Diff("osu", 3.0, 180, 100, "B", "")])
    out = c.filter_sets_by_rule([a, b], {"sr_min": 5.0})
    assert [s.id for s in out] == [1]


def test_osucollector_import_shape():
    data = {
        "name": "hard jumps",
        "beatmapsets": [
            {"id": 10, "beatmaps": [{"checksum": "aa"}, {"checksum": "bb"}]},
            {"id": 11, "beatmaps": [{"checksum": "aa"}]},   # dup md5 -> deduped
        ],
    }
    out = c.osucollector_to_collections(data)
    assert out["collections"] == [{"name": "hard jumps", "hashes": ["aa", "bb"]}]
    # and it plugs straight into the existing importer format
    assert "collections" in out


def test_osucollector_list_and_empty():
    out = c.osucollector_to_collections([{"name": "x", "beatmapsets": [
        {"beatmaps": [{"md5": "zz"}]}]}])
    assert out["collections"] == [{"name": "x", "hashes": ["zz"]}]
    assert c.osucollector_to_collections({"name": "empty"})["collections"] == []


@pytest.mark.parametrize("text,expected", [
    ("https://osucollector.com/collections/1234", 1234),
    ("osucollector.com/collections/9/anything", 9),
    ("42", 42),
    ("nope", None),
])
def test_parse_osucollector_ref(text, expected):
    assert c.parse_osucollector_ref(text) == expected


def test_search_cache_roundtrip(tmp_path):
    f = {"q": "camellia", "sr_min": 6, "hide_owned": True}
    s = _mkset2(1, title="T", artist="A",
                diffs=[c.Diff("mania", 5.5, 200, 120, "NM", "abc")])
    assert c.load_search_cache(tmp_path, f) is None      # cold
    c.save_search_cache(tmp_path, f, [s])
    got = c.load_search_cache(tmp_path, f)
    assert got and got[0].id == 1 and got[0].diffs[0].checksum == "abc"
    # hide_owned/no_video don't change the key -> same cache hit
    assert c.load_search_cache(tmp_path, dict(f, hide_owned=False)) is not None


def test_search_cache_expiry(tmp_path):
    f = {"q": "x"}
    c.save_search_cache(tmp_path, f, [_mkset2(1)])
    assert c.load_search_cache(tmp_path, f, max_age=-1) is None   # already stale


# ==========================================================================
# BATCH 3 -- app self-update version comparison.
# ==========================================================================
@pytest.mark.parametrize("s,expected", [
    ("2.1.0", (2, 1, 0)),
    ("v2.1.0", (2, 1, 0)),
    ("2.10.3", (2, 10, 3)),
    ("v3.0.0-rc1", (3, 0, 0)),
    ("", (0,)),
])
def test_parse_version(s, expected):
    assert c.parse_version(s) == expected


def test_version_is_newer():
    assert c.version_is_newer("2.2.0", "2.1.0")
    assert c.version_is_newer("v2.1.1", "2.1.0")
    assert c.version_is_newer("2.10.0", "2.9.9")   # numeric, not lexical
    assert not c.version_is_newer("2.1.0", "2.1.0")
    assert not c.version_is_newer("2.0.0", "2.1.0")


def test_check_for_app_update(monkeypatch):
    monkeypatch.setattr(c, "fetch_latest_release",
                        lambda repo=c.GITHUB_REPO: {"tag": "v9.9.9", "name": "x", "url": "u"})
    assert c.check_for_app_update("2.1.0")["tag"] == "v9.9.9"
    monkeypatch.setattr(c, "fetch_latest_release",
                        lambda repo=c.GITHUB_REPO: {"tag": "v2.1.0", "name": "", "url": ""})
    assert c.check_for_app_update("2.1.0") is None
    monkeypatch.setattr(c, "fetch_latest_release",
                        lambda repo=c.GITHUB_REPO: {"tag": "", "name": "", "url": ""})
    assert c.check_for_app_update("2.1.0") is None


# ==========================================================================
# Regression: Approved maps (e.g. the original FREEDOM DiVE, set 39804) are
# "ranked" for leaderboard purposes but the mirror only returns them on a
# no-status query. They must be merged into ranked/any results.
# ==========================================================================
def test_approved_maps_merged_under_ranked(monkeypatch):
    ranked = _mkset(1); ranked.status = "ranked"
    approved = _mkset(39804); approved.status = "approved"
    also_ranked = _mkset(3); also_ranked.status = "ranked"   # from no-status query

    def fake_hina_get(params, status_code=None):
        if status_code == 1:            # the ranked page
            return [ranked]
        if status_code is None:         # the supplementary no-status merge query
            return [approved, also_ranked]
        return []

    monkeypatch.setattr(c, "_hina_get", fake_hina_get)
    f = {"q": "freedom dive", "status": "ranked", "sort": "ranked_asc", "mode": None,
         "option": "", "genre": 0, "language": 0, "bpm_min": 0, "bpm_max": 0,
         "sr_min": 0, "sr_max": 0, "len_min": 0, "len_max": 0}
    sets, _ = c._search_hinamizawa(f, None)
    ids = [s.id for s in sets]
    assert 39804 in ids                 # approved map pulled in
    assert 3 not in ids                 # non-approved hits from no-status query ignored
    assert 1 in ids                     # original ranked results preserved


def test_approved_merge_only_first_page(monkeypatch):
    # On a paged (token != None) fetch we must NOT re-add approved maps.
    calls = []

    def fake_hina_get(params, status_code=None):
        calls.append(status_code)
        return []

    monkeypatch.setattr(c, "_hina_get", fake_hina_get)
    f = {"q": "freedom dive", "status": "ranked", "sort": "ranked_asc", "mode": None,
         "option": "", "genre": 0, "language": 0, "bpm_min": 0, "bpm_max": 0,
         "sr_min": 0, "sr_max": 0, "len_min": 0, "len_max": 0}
    c._search_hinamizawa(f, token="100")
    assert None not in calls            # no supplementary no-status query on page 2+


def test_modes_canonical_order():
    # A hybrid set whose taiko diff is *easier* than its osu! diff must still list
    # osu! first (canonical game order), not star-rating order. Regression for the
    # "taiko osu!" label on The Big Black (set 41823).
    s = _mkset2(41823, title="The Big Black",
                diffs=[c.Diff("taiko", 5.2, 0, 260, "Ono's Taiko Oni", ""),
                       c.Diff("osu", 6.8, 0, 500, "WHO'S AFRAID OF THE BIG BLACK", "")])
    # diffs are unsorted here on purpose; modes must be canonical regardless
    assert s.modes == ["osu", "taiko"]
    s2 = _mkset2(2, diffs=[c.Diff("mania", 3.0, 0, 100, "N", ""),
                           c.Diff("fruits", 2.0, 0, 100, "C", ""),
                           c.Diff("osu", 1.0, 0, 100, "E", "")])
    assert s2.modes == ["osu", "fruits", "mania"]


# ==========================================================================
# BATCH 4 -- practice pool, smart-rule persistence, library dashboard +
# duplicate finder, and follows.
# ==========================================================================
def test_build_practice_pool():
    def mk(i, sr, mode="osu"):
        return c.Beatmapset(id=i, title="", artist="", creator="", status="ranked",
                            bpm=0, play_count=0, favourite_count=0, cover_url="",
                            diffs=[c.Diff(mode, sr, 180, 120, "N", "")])
    sets = [mk(1, 5.2), mk(2, 6.9), mk(3, 5.5), mk(4, 3.0), mk(5, 5.4)]
    pool = c.build_practice_pool(sets, star_min=5.0, star_max=5.6,
                                 owned_ids={5}, limit=10)
    assert [s.id for s in pool] == [1, 3]          # 2 too hard, 4 too easy, 5 owned
    assert len(c.build_practice_pool(sets, 5.0, 5.6, limit=1)) == 1   # cap honoured
    # mode filter
    mania = [mk(9, 5.2, "mania")]
    assert c.build_practice_pool(mania, 5.0, 5.6, mode=0) == []       # no std diff
    assert len(c.build_practice_pool(mania, 5.0, 5.6, mode=3)) == 1


def test_smart_rules_roundtrip(tmp_path):
    p = tmp_path / "rules.json"
    assert c.load_smart_rules(p) == []
    rules = c.upsert_smart_rule([], "6star farm", {"sr_min": 6.0, "mode": 0})
    rules = c.upsert_smart_rule(rules, "streams", {"bpm_min": 180})
    c.save_smart_rules(p, rules)
    got = c.load_smart_rules(p)
    assert {r["name"] for r in got} == {"6star farm", "streams"}
    # upsert replaces by name
    rules = c.upsert_smart_rule(got, "streams", {"bpm_min": 200})
    assert sum(1 for r in rules if r["name"] == "streams") == 1
    assert next(r for r in rules if r["name"] == "streams")["rule"]["bpm_min"] == 200


def test_library_dashboard():
    db = [{"md5": "a", "set_id": 1, "mode": 0, "status": 4},
          {"md5": "b", "set_id": 1, "mode": 1, "status": 4},
          {"md5": "c", "set_id": 2, "mode": 0, "status": 7}]
    d = c.library_dashboard(db)
    assert d["difficulties"] == 3 and d["sets"] == 2
    assert d["by_mode"] == {"osu": 2, "taiko": 1}
    assert d["by_status"] == {"ranked": 2, "loved": 1}


def test_find_duplicate_song_folders(tmp_path):
    for name in ["41823 TQBF - The Big Black", "41823 TQBF - The Big Black (1)",
                 "129891 xi - FREEDOM DiVE", "notanumber folder"]:
        (tmp_path / name).mkdir()
    dups = c.find_duplicate_song_folders(tmp_path)
    assert set(dups) == {41823}
    assert len(dups[41823]) == 2
    assert c.find_duplicate_song_folders(tmp_path / "nope") == {}


def test_find_duplicate_osz(tmp_path):
    for name in ["100 a.osz", "100 a v2.osz", "200 b.osz", "notes.txt"]:
        (tmp_path / name).write_text("x")
    dups = c.find_duplicate_osz(tmp_path)
    assert set(dups) == {100} and len(dups[100]) == 2


def test_follow_to_filters():
    f = c.follow_to_filters({"type": "mapper", "value": "Sotarks"})
    assert f["q"] == "Sotarks" and f["option"] == "creator"
    f2 = c.follow_to_filters({"type": "search", "filters": {"q": "camellia", "sr_min": 6}})
    assert f2["q"] == "camellia" and f2["sr_min"] == 6


def test_check_follow_baseline_then_new():
    def mk(i):
        return c.Beatmapset(id=i, title="", artist="", creator="", status="ranked",
                            bpm=0, play_count=0, favourite_count=0, cover_url="", diffs=[])
    state = {"result": [mk(1), mk(2)]}
    search_fn = lambda filters: (state["result"], None)
    follow = {"type": "mapper", "value": "x"}
    new, follow = c.check_follow(follow, search_fn)
    assert new == [] and follow["seen"] == [1, 2]      # first run = baseline, no spam
    state["result"] = [mk(3), mk(1), mk(2)]            # a new map (3) appears
    new, follow = c.check_follow(follow, search_fn)
    assert [s.id for s in new] == [3]
    assert set(follow["seen"]) == {1, 2, 3}


# ==========================================================================
# Regression: osu!.db star-rating pairs. Current osu! builds (db version
# 20260711) store the value as a Single/float (tag 0x0c, 4 bytes); older builds
# used a Double (tag 0x0d, 8 bytes). Reading the wrong size desyncs the whole
# file -- the parser must branch on the tag.
# ==========================================================================
import struct as _struct


def test_int_double_pair_reads_float_and_double():
    # new float format: 0x08 <int32> 0x0c <float32> -> 10 bytes
    buf = bytes([0x08]) + _struct.pack("<i", 0) + bytes([0x0c]) + _struct.pack("<f", 2.67)
    r = c._DbReader(buf); r.int_double_pair()
    assert r.p == len(buf) == 10
    # legacy double format: 0x08 <int32> 0x0d <double> -> 14 bytes
    buf2 = bytes([0x08]) + _struct.pack("<i", 0) + bytes([0x0d]) + _struct.pack("<d", 5.5)
    r2 = c._DbReader(buf2); r2.int_double_pair()
    assert r2.p == len(buf2) == 14


def _make_osu_db(star_tag=0x0c, version=20260711):
    """Build a minimal one-beatmap osu!.db using the given star-pair value tag."""
    def s(x): return c._write_osu_string(x)
    def i(x): return _struct.pack("<i", x)
    def L(x): return _struct.pack("<q", x)
    def f(x): return _struct.pack("<f", x)
    def d(x): return _struct.pack("<d", x)

    def star_pair(mod, val):
        v = f(val) if star_tag == 0x0c else d(val)
        return bytes([0x08]) + i(mod) + bytes([star_tag]) + v

    bm = b""
    bm += s("Artist") + s("Artist") + s("Title") + s("Title") + s("Creator") + s("Hard")
    bm += s("audio.mp3") + s("a" * 32) + s("map.osu")
    bm += bytes([4])                                   # status = ranked
    bm += _struct.pack("<h", 10) + _struct.pack("<h", 5) + _struct.pack("<h", 0)
    bm += L(637000000000000000)                        # last modified
    bm += f(7.0) + f(4.0) + f(6.0) + f(5.0)            # AR CS HP OD
    bm += d(1.5)                                        # slider velocity
    for _ in range(4):                                 # star pairs per mode
        bm += i(1) + star_pair(0, 5.25)
    bm += i(60000) + i(65000) + i(1000)                # drain / total / preview
    bm += i(0)                                          # timing points count
    bm += i(131891) + i(41823) + i(0)                  # beatmap_id / set_id / thread
    bm += bytes([0, 0, 0, 0])                          # grades
    bm += _struct.pack("<h", 0)                        # local offset
    bm += f(0.7)                                        # stack leniency
    bm += bytes([0])                                    # mode = osu!
    bm += s("src") + s("tags")
    bm += _struct.pack("<h", 0)                        # online offset
    bm += s("font")
    bm += bytes([0])                                    # unplayed
    bm += L(0)                                          # last played
    bm += bytes([0])                                    # is osz2
    bm += s("folder")
    bm += L(0)                                          # last checked
    bm += bytes([0, 0, 0, 0, 0])                       # 5 flags
    bm += i(0)                                          # last mod time
    bm += bytes([0])                                    # mania scroll speed

    header = i(version) + i(1) + bytes([0]) + L(0) + s("Player") + i(1)
    return header + bm


@pytest.mark.parametrize("tag", [0x0c, 0x0d])
def test_read_osu_db_both_star_formats(tmp_path, tag):
    p = tmp_path / "osu!.db"
    p.write_bytes(_make_osu_db(star_tag=tag))
    db = c.read_osu_db(p)
    assert db["version"] == 20260711 and db["player"] == "Player"
    assert len(db["beatmaps"]) == 1
    b = db["beatmaps"][0]
    assert b["set_id"] == 41823 and b["beatmap_id"] == 131891
    assert b["md5"] == "a" * 32 and b["mode"] == 0 and b["status"] == 4
    assert c.osu_db_set_ids(p) == {41823}


# ==========================================================================
# BATCH 5 -- mappool importer, tracklist export, duplicate cleanup.
# ==========================================================================
def test_parse_beatmap_refs_mixed():
    text = (
        "NM1: https://osu.ppy.sh/beatmapsets/39804#osu/129891\n"
        "HD1: https://osu.ppy.sh/b/252238\n"
        "DT1: check https://osu.ppy.sh/beatmapsets/41823 and old /s/1234\n"
        "plain set id 987654 on its own line\n"
        "dupe: https://osu.ppy.sh/beatmapsets/39804\n"   # duplicate -> ignored
    )
    refs = c.parse_beatmap_refs(text)
    assert ("set", 39804) in refs
    assert ("beatmap", 252238) in refs
    assert ("set", 41823) in refs and ("set", 1234) in refs
    assert ("set", 987654) in refs
    # 129891 was the #osu fragment on the 39804 set link -> not a separate beatmap
    assert ("beatmap", 129891) not in refs
    # de-duped: 39804 appears once
    assert sum(1 for k, i in refs if i == 39804) == 1


def test_parse_beatmap_refs_empty():
    assert c.parse_beatmap_refs("") == []
    assert c.parse_beatmap_refs("no links here, just words and 42") == []  # <5 digits


def test_collection_tracklist_and_format():
    db = [{"md5": "aa", "artist": "xi", "title": "FREEDOM DiVE", "diff": "FOUR DIMENSIONS"},
          {"md5": "bb", "artist": "Camellia", "title": "GHOST", "diff": "Ascension"}]
    lines = c.collection_tracklist(["aa", "zz", "bb"], db)
    assert lines[0] == "xi - FREEDOM DiVE [FOUR DIMENSIONS]"
    assert lines[1].startswith("(not installed)")
    assert lines[2] == "Camellia - GHOST [Ascension]"
    text = c.format_tracklist("My pool", lines)
    assert "My pool — 3 maps" in text and "1. xi - FREEDOM DiVE" in text


def test_redundant_duplicates():
    dup = {41823: ["X", "X (1)", "X (2)"], 100: ["a", "b"]}
    rem = c.redundant_duplicates(dup)
    assert set(rem) == {"X (1)", "X (2)", "b"}          # keep the first of each
    assert "X" not in rem and "a" not in rem


def test_move_paths_to_trash(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "dupe1").mkdir()
    (tmp_path / "dupe2.osz").write_text("x")
    moved, trash = c.move_paths_to_trash(tmp_path, ["dupe1", "dupe2.osz", "missing"])
    assert moved == 2
    assert not (tmp_path / "dupe1").exists() and not (tmp_path / "dupe2.osz").exists()
    assert (tmp_path / "keep").exists()
    assert (c.Path(trash) / "dupe1").exists() and (c.Path(trash) / "dupe2.osz").exists()


def test_osu_db_captures_artist_title(tmp_path):
    p = tmp_path / "osu!.db"
    p.write_bytes(_make_osu_db(star_tag=0x0c))
    b = c.read_osu_db(p)["beatmaps"][0]
    assert b["artist"] == "Artist" and b["title"] == "Title" and b["creator"] == "Creator"


# ==========================================================================
# BATCH 6 -- missing-map resolution + per-mapper stats.
# ==========================================================================
def test_missing_hashes():
    got = c.missing_hashes(["AA", "bb", "cc", "aa"], known_md5s={"aa"})
    assert got == ["bb", "cc"]                 # 'AA'/'aa' owned (case-insensitive), deduped


def test_resolve_missing_to_sets(monkeypatch):
    table = {"h1": 100, "h2": 200, "h3": 100, "h4": None}

    def fake(md5):
        v = table.get(md5)
        if v is None:
            raise RuntimeError("nope")
        return v
    monkeypatch.setattr(c, "resolve_md5_to_set", fake)
    seen = []
    out = c.resolve_missing_to_sets(["h1", "h2", "h3", "h4"],
                                    progress=lambda i, t: seen.append((i, t)))
    assert out == [100, 200]                   # 100 deduped, h4 skipped
    assert seen[-1] == (4, 4)                   # progress ran to completion


def test_top_mappers():
    db = [{"creator": "Sotarks", "set_id": 1}, {"creator": "Sotarks", "set_id": 1},
          {"creator": "Sotarks", "set_id": 2}, {"creator": "Monstrata", "set_id": 3},
          {"creator": "", "set_id": 4}, {"creator": "x", "set_id": 0}]
    top = c.top_mappers(db)
    assert top[0] == ("Sotarks", 2)            # 2 distinct sets (diffs of set 1 not double-counted)
    assert ("Monstrata", 1) in top
    assert all(name for name, _ in top)        # blank creator / bad set_id excluded
