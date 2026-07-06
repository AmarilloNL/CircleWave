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
import logging
import tempfile
import threading
import zipfile
from pathlib import Path
# Config, data model, networking, collection.db and parsers live in
# circlewave_core (see the star-import just below the Qt imports).

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


# Core (Qt-free) logic lives in circlewave_core so it can be unit-tested
# without importing PySide6. Pull its public API into this namespace.
from circlewave_core import *  # noqa: F401,F403
from circlewave_core import (  # non-exported internals the GUI needs
    _has_client_filter, log,
)


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
            r = SESSION.get(self.url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            if r.status_code == 200 and r.content:
                try:
                    cache_file.write_bytes(r.content)
                except OSError as e:
                    log.debug("cover cache write failed for %s: %s", self.setid, e)
                self.signals.done.emit(self.setid, r.content)
        except Exception as e:  # noqa: BLE001 - a missing cover is non-fatal
            log.debug("cover fetch failed for %s: %s", self.setid, e)


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
        # threading.Event, not a bare bool: the flag is set from the GUI thread
        # and read from the worker thread, and Event makes that hand-off explicit.
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:      # kept for callers that inspect the flag
        return self._cancel.is_set()

    @staticmethod
    def _looks_like_zip(path: Path) -> bool:
        """A complete .osz is a zip; is_zipfile validates the central directory,
        so it also rejects truncated (interrupted) downloads and HTML error pages."""
        try:
            return zipfile.is_zipfile(path)
        except OSError:
            return False

    @staticmethod
    def _discard(*paths: Path):
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    @Slot()
    def run(self):
        sid = self.s.id
        fname = sanitize_filename(f"{sid} {self.s.artist} - {self.s.title}.osz")
        dest_dir = Path(self.dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / fname
        # Stage the partial file *inside* the destination dir so the final move
        # is a same-filesystem atomic rename (avoids cross-device errors when the
        # system temp dir is a tmpfs and the download folder is on disk). A sidecar
        # .part.url records which mirror URL produced the partial, so a resume only
        # ever extends a partial from the *same* URL (mirrors aren't interchangeable).
        tmp = dest_dir / (fname + ".part")
        meta = dest_dir / (fname + ".part.url")
        last_err = "no mirror succeeded"

        for mirror in self.mirrors:
            if self._cancel.is_set():
                self.signals.failed.emit(sid, "cancelled")
                return
            url = mirror["novideo"] if (self.no_video and mirror["novideo"]) else mirror["full"]
            url = url.format(id=sid)

            # Resume only if we have a partial tagged with this exact URL.
            resume_from = 0
            if tmp.exists() and meta.exists() and _read_text(meta) == url:
                resume_from = tmp.stat().st_size
            else:
                self._discard(tmp, meta)   # stale / foreign partial

            headers = {"User-Agent": DOWNLOAD_UA}
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"
            note = "resuming" if resume_from else "trying"
            self.signals.progress.emit(sid, -1, f"{note} {mirror['name']}\u2026")

            try:
                with SESSION.get(url, headers=headers, stream=True,
                                 timeout=HTTP_TIMEOUT, allow_redirects=True) as r:
                    ctype = r.headers.get("Content-Type", "").lower()
                    if r.status_code not in (200, 206) or "text/html" in ctype or "json" in ctype:
                        last_err = f"{mirror['name']} HTTP {r.status_code}"
                        self._discard(tmp, meta)      # response body is not an .osz
                        continue

                    if r.status_code == 206 and resume_from:
                        mode, got = "ab", resume_from            # server honoured Range
                        total = resume_from + int(r.headers.get("Content-Length", 0) or 0)
                    else:                                        # full body (Range ignored / fresh)
                        mode, got, resume_from = "wb", 0, 0
                        total = int(r.headers.get("Content-Length", 0) or 0)

                    meta.write_text(url)
                    with open(tmp, mode) as fh:
                        for chunk in r.iter_content(chunk_size=65536):
                            if self._cancel.is_set():
                                fh.flush()
                                # Keep tmp + meta so this mirror can resume later.
                                self.signals.failed.emit(sid, "cancelled")
                                return
                            if chunk:
                                fh.write(chunk)
                                got += len(chunk)
                                pct = int(got * 100 / total) if total else -1
                                self.signals.progress.emit(sid, pct, mirror["name"])

                    if got < 1024 or not self._looks_like_zip(tmp):
                        last_err = f"{mirror['name']} returned an invalid/partial file"
                        self._discard(tmp, meta)
                        continue
                    tmp.replace(dest)
                    self._discard(meta)
                    self.signals.done.emit(sid, str(dest))
                    return
            except Exception as e:  # noqa: BLE001
                # Network hiccup: keep the partial so the same mirror can resume.
                log.warning("download %s via %s failed: %s", sid, mirror["name"], e)
                last_err = f"{mirror['name']}: {e}"
                continue

        self.signals.failed.emit(sid, last_err)


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


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

    def _refresh_badges(self):
        """Update the status colour + star-range badges from self.s (used after
        full metadata arrives, since the lazy enrich only fills text otherwise)."""
        s = self.s
        color = STATUS_COLORS.get(s.status, "#8a8a8a")
        self.status_badge.setText(s.status or "?")
        self.status_badge.setStyleSheet(
            f"background:{color}; color:#15151a; border-radius:5px;"
            "padding:2px 8px; font-size:11px; font-weight:700;")
        self.status_badge.adjustSize()
        self.status_badge.move(8, 8)
        lo, hi = s.sr_range
        if hi > 0:
            self.sr_badge.setText(f"\u2605 {lo:.1f}" if abs(hi - lo) < 0.05
                                  else f"\u2605 {lo:.1f}\u2013{hi:.1f}")
            self.sr_badge.adjustSize()
            self.sr_badge.move(max(self.width(), self.CARD_W) - self.sr_badge.width() - 8, 8)
            self.sr_badge.show()
        else:
            self.sr_badge.hide()
        self.sr_badge.raise_()
        self.status_badge.raise_()

    def apply_full(self, full: Beatmapset):
        """Upgrade a minimal pack card in place once full metadata arrives:
        real status colour, star badge, mapper and play/favourite stats."""
        self.s = full
        self._refresh_badges()
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
        self.list.verticalScrollBar().valueChanged.connect(self._pack_scrolled)
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

    def _pack_scrolled(self, value):
        # auto-load the next page as the list nears the bottom
        bar = self.list.verticalScrollBar()
        if not self.loading and self.has_more and bar.maximum() - value < 160:
            self._load_next()

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

        # Dry-run: show exactly what will change before we touch collection.db.
        name = pk["medal"]
        prev = preview_collection_merge(db_path, name, hashes)
        verb = "Replace" if prev["replacing"] else "Create"
        lines = [f"{verb} collection “{name}” with {prev['new_maps']} maps."]
        if prev["replacing"]:
            lines.append(f"The existing “{name}” ({prev['old_maps']} maps) "
                         "will be overwritten.")
        if prev["kept"]:
            lines.append(f"{len(prev['kept'])} other collection(s) "
                         f"({sum(c for _, c in prev['kept'])} maps) will be kept.")
        if prev["db_exists"]:
            lines.append("A .bak backup of the current collection.db will be written first.")
        else:
            lines.append("A new collection.db will be created (osu! not detected here — "
                         "check the path in Settings if this looks wrong).")
        lines.append(f"\nFile: {db_path}")
        confirm = QMessageBox.question(
            self, "Write collection?", "\n".join(lines),
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes)
        if confirm != QMessageBox.Yes:
            return

        try:
            status = upsert_collection(db_path, name, hashes)
        except Exception as e:  # noqa: BLE001
            log.exception("collection write to %s failed", db_path)
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
        card._refresh_badges()         # most-played payload has no diffs -> stars now show

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
    # Logging goes to stderr. Level defaults to WARNING; set CIRCLEWAVE_LOG=DEBUG
    # (or INFO) to see mirror fallbacks, cover/download failures, etc.
    logging.basicConfig(
        level=os.environ.get("CIRCLEWAVE_LOG", "WARNING").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Make Windows show our own taskbar icon instead of grouping under python.exe.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{ORG_NAME}.{APP_NAME}")
        except Exception as e:
            log.debug("could not set Windows AppUserModelID: %s", e)

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
