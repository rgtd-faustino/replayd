"""
viewer.py – Clip viewer, trimmer and audio mixer for replayd

Layout
------
  Left sidebar  : scrollable clip list with thumbnails
  Main area     : video player (top) + control panel (bottom)
    Control panel:
      - Trim timeline (drag handles + playhead)
      - Timecode labels + playback buttons
      - AudioMixerPanel (drag-resizable from the top handle)
          - Per-track sliders: Game Audio / Microphone (or Track N)
          - Mute toggle per track
          - "Reset all" button

Audio notes
-----------
  Clips recorded with audio_mode='both' contain two separate audio streams:
    stream 0 = Game / Desktop audio
    stream 1 = Microphone
    The viewer plays all audio tracks at the same time.
    Mixer sliders/mute update playback in real time and also affect export.
  Single-track clips show one "Audio" strip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QMessageBox, QSlider, QStyle,
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame
from PyQt6.QtCore import Qt, QUrl, pyqtSignal, QFileSystemWatcher, QThread, QTimer
from PyQt6.QtGui import QPainter, QColor, QCursor, QPixmap, QDesktopServices, QIcon

# ── palette ────────────────────────────────────────────────────────────────────
BG    = '#13110f'
S1    = '#1b1814'
S2    = '#232019'
S3    = '#2e2a23'
S4    = '#3a352d'
ACC   = '#da7b24'
ACC_H = '#e68f38'
ACC_S = '#7b3f0e'
TX    = '#ece7df'
TX2   = '#8a8078'
TX3   = '#4e4840'
RED   = '#e45a5a'


# ── thumbnails ─────────────────────────────────────────────────────────────────
from paths import THUMB_DIR
_active_thumb_workers: list = []


def get_thumb_path(video_path: Path) -> Path:
    return THUMB_DIR / (video_path.stem + '.jpg')


def generate_thumb(video_path: Path) -> Optional[Path]:
    thumb = get_thumb_path(video_path)
    if thumb.exists():
        return thumb
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)],
            capture_output=True, text=True, timeout=5,
        )
        raw = probe.stdout.strip()
        dur = float(raw) if probe.returncode == 0 and raw else 10.0
        seek = max(0.5, dur * 0.25)
        subprocess.run(
            ['ffmpeg', '-y', '-ss', f'{seek:.2f}', '-i', str(video_path),
             '-frames:v', '1', '-q:v', '4', '-vf', 'scale=160:-1', str(thumb)],
            capture_output=True, timeout=15,
        )
        return thumb if thumb.exists() else None
    except Exception:
        return None


class _ThumbWorker(QThread):
    done = pyqtSignal(str, str)

    def __init__(self, video_path: Path):
        super().__init__()
        self._path = video_path

    def run(self):
        result = generate_thumb(self._path)
        if result:
            self.done.emit(str(self._path), str(result))


# ── shared styles ──────────────────────────────────────────────────────────────

def _btn_css(accent=False, danger=False, small=False) -> str:
    pad = '0 10px' if small else '0 14px'
    if accent:
        return (
            f'QPushButton{{background:{ACC};border:none;border-radius:9px;'
            f'color:#12100d;font-size:12px;font-weight:600;padding:{pad};}}'
            f'QPushButton:hover{{background:{ACC_H};}}'
            f'QPushButton:pressed{{background:{ACC_S};}}'
            f'QPushButton:disabled{{background:{S3};color:{TX3};}}'
        )
    if danger:
        return (
            f'QPushButton{{background:transparent;border:1px solid rgba(228,90,90,0.35);'
            f'border-radius:9px;color:{RED};font-size:12px;padding:{pad};}}'
            f'QPushButton:hover{{background:rgba(228,90,90,0.12);}}'
        )
    return (
        f'QPushButton{{background:{S2};border:none;border-radius:9px;'
        f'color:{TX};font-size:12px;padding:{pad};}}'
        f'QPushButton:hover{{background:{S3};}}'
        f'QPushButton:disabled{{color:{TX3};}}'
    )


_SLIDER_CSS = (
    f'QSlider::groove:horizontal{{background:{S3};height:4px;border-radius:2px;}}'
    f'QSlider::handle:horizontal{{background:{ACC};width:14px;height:14px;'
    f'margin:-5px 0;border-radius:7px;}}'
    f'QSlider::sub-page:horizontal{{background:{ACC};height:4px;border-radius:2px;}}'
    f'QSlider:disabled::handle:horizontal{{background:{S4};}}'
    f'QSlider:disabled::sub-page:horizontal{{background:{S4};}}'
)


def _ts(ms: int) -> str:
    s = int(ms / 1000)
    return f'{s // 60}:{s % 60:02d}'


# ── trim timeline ──────────────────────────────────────────────────────────────

class TrimTimeline(QWidget):
    in_changed  = pyqtSignal(float)
    out_changed = pyqtSignal(float)
    seek        = pyqtSignal(float)
    HW = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._in = 0.0; self._out = 1.0; self._pos = 0.0; self._drag = None

    def set_in(self, v):  self._in  = max(0.0, min(v, self._out - 0.01)); self.update()
    def set_out(self, v): self._out = max(self._in + 0.01, min(v, 1.0)); self.update()
    def set_pos(self, v): self._pos = max(0.0, min(v, 1.0)); self.update()

    def _r2x(self, r): return self.HW + r * (self.width() - 2 * self.HW)
    def _x2r(self, x): return max(0.0, min(1.0, (x - self.HW) / max(1, self.width() - 2*self.HW)))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cy = h // 2; ty, th = cy - 4, 8
        ix = int(self._r2x(self._in)); ox = int(self._r2x(self._out)); px = int(self._r2x(self._pos))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(S2));   p.drawRoundedRect(self.HW, ty, w-2*self.HW, th, 4, 4)
        p.setBrush(QColor(ACC_S)); p.drawRect(ix, ty, ox-ix, th)
        p.setBrush(QColor(ACC))
        p.drawRoundedRect(ix - self.HW//2, 4, self.HW, h-8, 3, 3)
        p.drawRoundedRect(ox - self.HW//2, 4, self.HW, h-8, 3, 3)
        p.setBrush(QColor(RED)); p.drawRect(px-1, 0, 2, h); p.drawEllipse(px-4, 0, 8, 8)
        p.end()

    def _hit(self, x):
        if abs(x - self._r2x(self._in))  < self.HW + 4: return 'in'
        if abs(x - self._r2x(self._out)) < self.HW + 4: return 'out'
        return 'seek'

    def mousePressEvent(self, e):
        self._drag = self._hit(e.position().x()); self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor)); self._apply(e.position().x())
    def mouseMoveEvent(self, e):
        if self._drag: self._apply(e.position().x())
    def mouseReleaseEvent(self, _):
        self._drag = None; self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def _apply(self, x):
        r = self._x2r(x)
        if self._drag == 'in':    self._in  = max(0.0, min(r, self._out - 0.01)); self.in_changed.emit(self._in);  self.seek.emit(self._in)
        elif self._drag == 'out': self._out = max(self._in + 0.01, min(r, 1.0)); self.out_changed.emit(self._out); self.seek.emit(self._out)
        elif self._drag == 'seek': self._pos = r; self.seek.emit(r)
        self.update()


# ── sidebar item ───────────────────────────────────────────────────────────────

class SidebarItem(QWidget):
    selected = pyqtSignal(str)

    def __init__(self, path: Path, active: bool = False, parent=None):
        super().__init__(parent)
        self.path = path; self._active = active
        self._worker: Optional[_ThumbWorker] = None
        self.setFixedHeight(68); self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        lay = QHBoxLayout(self); lay.setContentsMargins(10, 0, 10, 0); lay.setSpacing(10)
        self._thumb = QLabel(); self._thumb.setFixedSize(88, 55)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter); self._thumb.setText('▶')
        self._thumb.setStyleSheet(f'background:{S3};border-radius:6px;color:rgba(255,255,255,0.28);font-size:11px;')
        lay.addWidget(self._thumb)

        info = QVBoxLayout(); info.setSpacing(3); info.setContentsMargins(0,0,0,0)
        nl = QLabel(path.stem); nl.setStyleSheet(f'color:{TX};font-size:11px;font-weight:600;')
        try:    ml = QLabel(f'{path.stat().st_size/1_048_576:.0f} MB')
        except: ml = QLabel('—')
        ml.setStyleSheet(f'color:{TX3};font-size:10px;')
        info.addWidget(nl); info.addWidget(ml); lay.addLayout(info, stretch=1)
        self._refresh_style(); self._load_thumb()

    def _load_thumb(self):
        t = get_thumb_path(self.path)
        if t.exists(): self._apply_px(str(t)); return
        w = _ThumbWorker(self.path)
        w.done.connect(self._on_done)
        w.finished.connect(lambda: _active_thumb_workers.remove(w) if w in _active_thumb_workers else None)
        _active_thumb_workers.append(w); self._worker = w; w.start()

    def _on_done(self, vp, tp):
        if vp == str(self.path): self._apply_px(tp)

    def _apply_px(self, tp):
        px = QPixmap(tp)
        if not px.isNull():
            self._thumb.setPixmap(px.scaled(88, 55, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self._thumb.setText(''); self._thumb.setStyleSheet('background:#080808;border-radius:6px;')

    def _refresh_style(self):
        bg = S3 if self._active else S1; a = '0.07' if self._active else '0.03'
        self.setStyleSheet(f'SidebarItem{{background:{bg};border-radius:8px;border:1px solid rgba(255,255,255,{a});}}SidebarItem:hover{{background:{S2};}}')

    def set_active(self, v: bool): self._active = v; self._refresh_style()
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton: self.selected.emit(str(self.path))
        super().mousePressEvent(e)


# ── video display ──────────────────────────────────────────────────────────────

class VideoLabel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._px: Optional[QPixmap] = None
        self.setStyleSheet('background:#000;')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_frame(self, px: QPixmap): self._px = px; self.update()
    def clear_frame(self): self._px = None; self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.fillRect(self.rect(), QColor('#000'))
        if self._px:
            s = self._px.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
            p.drawPixmap((self.width()-s.width())//2, (self.height()-s.height())//2, s)
        p.end()


class ClickSlider(QSlider):
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), e.position().toPoint().x(), self.width()))
            e.accept()
        super().mousePressEvent(e)


# ── audio mixer widgets ────────────────────────────────────────────────────────

class AudioTrackStrip(QWidget):
    """Single row: icon · label · volume slider (0–200%) · pct · mute btn."""
    changed = pyqtSignal()

    def __init__(self, label: str, icon: str, index: int, parent=None):
        super().__init__(parent)
        self.index = index; self._muted = False
        lay = QHBoxLayout(self); lay.setContentsMargins(18, 0, 18, 0); lay.setSpacing(10)
        lay.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        il = QLabel(icon); il.setFixedWidth(22)
        il.setStyleSheet(f'color:{TX2};font-size:15px;background:transparent;')
        lay.addWidget(il)

        nl = QLabel(label); nl.setFixedWidth(96)
        nl.setStyleSheet(f'color:{TX};font-size:11px;font-weight:600;background:transparent;')
        lay.addWidget(nl)

        self._slider = ClickSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 200); self._slider.setValue(100)
        self._slider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._slider.setStyleSheet(_SLIDER_CSS)
        self._slider.valueChanged.connect(self._on_val)
        lay.addWidget(self._slider, stretch=1)

        self._pct = QLabel('100%'); self._pct.setFixedWidth(40)
        self._pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._pct.setStyleSheet(f'color:{ACC};font-size:11px;font-family:monospace;background:transparent;')
        lay.addWidget(self._pct)

        self._mute = QPushButton('M'); self._mute.setFixedSize(30, 30)
        self._mute.setCheckable(True)
        self._mute.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._mute.setToolTip('Mute this track in the exported file')
        self._mute.toggled.connect(self._on_mute)
        self._sync_mute()
        lay.addWidget(self._mute)
        self.setFixedHeight(44)

    def _on_val(self, v: int):
        self._pct.setText(f'{v}%')
        if v == 0:
            col = TX3
        elif v > 100:
            col = ACC_H   # brighter amber to signal boost mode
        else:
            col = ACC
        self._pct.setStyleSheet(f'color:{col};font-size:11px;font-family:monospace;background:transparent;')
        self.changed.emit()

    def _on_mute(self, checked: bool):
        self._muted = checked; self._slider.setEnabled(not checked); self._sync_mute()
        self.changed.emit()

    def _sync_mute(self):
        if self._muted:
            self._mute.setStyleSheet(f'QPushButton{{background:{RED};border:none;border-radius:7px;color:#fff;font-size:10px;font-weight:700;}}')
        else:
            self._mute.setStyleSheet(f'QPushButton{{background:{S4};border:none;border-radius:7px;color:{TX3};font-size:10px;font-weight:700;}}QPushButton:hover{{background:{S3};color:{TX2};}}')

    @property
    def volume(self) -> float:
        return 0.0 if self._muted else self._slider.value() / 100.0

    def reset(self):
        self._slider.setValue(100); self._mute.setChecked(False)


class MixerDragHandle(QWidget):
    """
    Thin horizontal strip at the top of AudioMixerPanel.
    Drag UP → panel expands (video area shrinks).
    Drag DOWN → panel collapses.
    """
    def __init__(self, target: 'AudioMixerPanel', parent=None):
        super().__init__(parent)
        self._target = target; self._dy = None; self._sh = None
        self.setFixedHeight(10); self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
        self.setToolTip('Drag to resize the audio mixer')

    def paintEvent(self, _):
        p = QPainter(self); p.fillRect(self.rect(), QColor(S2))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(TX3))
        cx, cy = self.width()//2, self.height()//2
        for dx in range(-14, 15, 4): p.drawEllipse(cx+dx-1, cy-1, 2, 2)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dy = e.globalPosition().y(); self._sh = self._target.height()

    def mouseMoveEvent(self, e):
        if self._dy is None: return
        self._target.resize_to(int(self._sh - (e.globalPosition().y() - self._dy)))

    def mouseReleaseEvent(self, _): self._dy = None


class AudioMixerPanel(QWidget):
    """
    Collapsible, drag-resizable panel below the trim controls.

    SNAP_H   = 46px   (handle 10 + header 36) — fully collapsed
    open height = SNAP_H + n*44 + 16 (body pad) + 46 (footer)
    MAX_H    = 380px
    """
    HANDLE_H = 10; HEADER_H = 36; FOOTER_H = 46; TRACK_H = 44; PAD = 8
    SNAP_H   = HANDLE_H + HEADER_H   # 46
    mix_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tracks: list[AudioTrackStrip] = []
        self._n = 0; self._expanded = True

        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # drag handle
        self._handle = MixerDragHandle(self); root.addWidget(self._handle)

        # header
        hdr = QWidget(); hdr.setFixedHeight(self.HEADER_H)
        hdr.setStyleSheet(f'background:{S2};')
        hl = QHBoxLayout(hdr); hl.setContentsMargins(18,0,12,0); hl.setSpacing(0)
        t = QLabel('AUDIO MIXER')
        t.setStyleSheet(f'color:{TX3};font-size:10px;font-weight:600;letter-spacing:1.8px;background:transparent;')
        hl.addWidget(t); hl.addStretch()
        hint = QLabel('sliders affect exported file · track 0 controls preview volume')
        hint.setStyleSheet(f'color:{TX3};font-size:9px;background:transparent;')
        hl.addWidget(hint); hl.addSpacing(10)
        self._tog = QPushButton('▲'); self._tog.setFixedSize(24,24)
        self._tog.setStyleSheet(f'QPushButton{{background:transparent;border:none;color:{TX3};font-size:11px;}}QPushButton:hover{{color:{TX};}}')
        self._tog.clicked.connect(self._toggle); hl.addWidget(self._tog)
        root.addWidget(hdr)

        # body
        self._body = QWidget(); self._body.setStyleSheet(f'background:{S1};')
        self._blay = QVBoxLayout(self._body)
        self._blay.setContentsMargins(0, self.PAD, 0, self.PAD); self._blay.setSpacing(0)
        root.addWidget(self._body)

        # footer
        self._foot = QWidget(); self._foot.setFixedHeight(self.FOOTER_H)
        self._foot.setStyleSheet(f'background:{S1};border-top:1px solid rgba(255,255,255,0.04);')
        fl = QHBoxLayout(self._foot); fl.setContentsMargins(18,0,18,0); fl.setSpacing(8)
        rb = QPushButton('Reset all'); rb.setFixedHeight(32)
        rb.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        rb.setStyleSheet(_btn_css(small=True)); rb.clicked.connect(self._reset)
        fl.addWidget(rb)
        # boost hint — visible only when any track is above 100 %
        self._boost_hint = QLabel('⬆ volume boost · audible after export')
        self._boost_hint.setStyleSheet(
            f'color:{ACC_H};font-size:9px;font-weight:600;background:transparent;'
        )
        self._boost_hint.setVisible(False)
        fl.addWidget(self._boost_hint)
        fl.addStretch()
        root.addWidget(self._foot)

        self.setStyleSheet(f'background:{S1};')
        self.mix_changed.connect(self._update_boost_hint)
        self._calc_h()

    # ── public ─────────────────────────────────────────────────────────────────

    def load_tracks(self, n: int):
        while self._blay.count():
            item = self._blay.takeAt(0)
            if w := item.widget(): w.deleteLater()
        self._tracks.clear(); self._n = n

        if n == 0: self.setVisible(False); return
        self.setVisible(True)

        DEFS = {1: [('Audio','🔊')], 2: [('Game Audio','🎮'),('Microphone','🎤')]}
        defs = DEFS.get(n, [(f'Track {i+1}','🎵') for i in range(n)])
        for i, (lbl, ico) in enumerate(defs):
            s = AudioTrackStrip(lbl, ico, i, self)
            s.changed.connect(self.mix_changed)
            self._tracks.append(s)
            self._blay.addWidget(s)

        if not self._expanded:
            self._expanded = True; self._body.setVisible(True)
            self._foot.setVisible(True); self._tog.setText('▲')
        self._calc_h()

    def get_volumes(self) -> list[float]:
        return [t.volume for t in self._tracks]

    def _update_boost_hint(self):
        boosted = any(
            t._slider.value() > 100 and not t._muted
            for t in self._tracks
        )
        self._boost_hint.setVisible(boosted)

    def resize_to(self, h: int):
        if h <= self.SNAP_H + 20:
            if self._expanded:
                self._expanded = False; self._body.setVisible(False)
                self._foot.setVisible(False); self._tog.setText('▼')
            self.setFixedHeight(self.SNAP_H)
        else:
            if not self._expanded:
                self._expanded = True; self._body.setVisible(True)
                self._foot.setVisible(True); self._tog.setText('▲')
            self.setFixedHeight(max(self.SNAP_H, min(380, h)))

    # ── private ────────────────────────────────────────────────────────────────

    def _toggle(self):
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded); self._foot.setVisible(self._expanded)
        self._tog.setText('▲' if self._expanded else '▼'); self._calc_h()

    def _calc_h(self):
        if not self._expanded:
            self.setFixedHeight(self.SNAP_H)
            return
        self.setFixedHeight(
            self.HANDLE_H + self.HEADER_H +
            max(self._n, 1) * self.TRACK_H + self.PAD * 2 + self.FOOTER_H
        )

    def _reset(self):
        for t in self._tracks: t.reset()


# ── main clip viewer window ────────────────────────────────────────────────────

class ClipViewer(QWidget):

    def __init__(self, config: dict, parent=None):
        super().__init__(None)   # own top-level window (Wayland native surface)
        self.cfg = config
        self._current_path: Optional[Path] = None
        self._duration_ms = 1; self._in_ms = 0; self._out_ms = 1
        self._sidebar_items: list[SidebarItem] = []
        self._audio_players: list[QMediaPlayer] = []
        self._audio_outputs: list[QAudioOutput] = []
        self._base_audio_idx: int = 0

        self.setWindowTitle('replayd — Clip Viewer')
        self.setMinimumSize(960, 600); self.resize(1160, 740)
        self.setStyleSheet(f'QWidget{{background:{BG};color:{TX};}}')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # Apply the custom app icon so the viewer shows it in the taskbar /
        # window switcher instead of the Python default icon.
        try:
            from gui import _load_app_icon
            _icon_px = _load_app_icon(256)
            if _icon_px:
                self.setWindowIcon(QIcon(_icon_px))
        except Exception:
            pass

        self._build_ui()

        self._watcher = QFileSystemWatcher(self)
        self._watcher.addPath(str(Path(config['output_dir']).expanduser()))
        self._watcher.directoryChanged.connect(lambda _: QTimer.singleShot(300, self._refresh_list))

    # ── build ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QHBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_main(), stretch=1)

    def _build_sidebar(self) -> QWidget:
        sb = QWidget(); sb.setFixedWidth(240)
        sb.setStyleSheet(f'background:{S1};border-right:1px solid rgba(255,255,255,0.048);')
        lay = QVBoxLayout(sb); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)

        hdr = QWidget(); hdr.setFixedHeight(52)
        hdr.setStyleSheet('border-bottom:1px solid rgba(255,255,255,0.048);')
        hl = QHBoxLayout(hdr); hl.setContentsMargins(14,0,10,0); hl.setSpacing(8)
        lbl = QLabel('CLIPS'); lbl.setStyleSheet(f'color:{TX3};font-size:10px;font-weight:600;letter-spacing:1.8px;')
        hl.addWidget(lbl); hl.addStretch()
        fb = QPushButton('Open folder →')
        fb.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        fb.setStyleSheet(f'QPushButton{{background:none;border:none;color:{TX3};font-size:10px;}}QPushButton:hover{{color:{ACC};}}')
        fb.clicked.connect(self._open_folder); hl.addWidget(fb); lay.addWidget(hdr)

        sc = QScrollArea(); sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sc.setStyleSheet('QScrollArea{border:none;background:transparent;}')
        self._list_container = QWidget(); self._list_container.setStyleSheet('background:transparent;')
        self._list_lay = QVBoxLayout(self._list_container)
        self._list_lay.setContentsMargins(8,8,8,8); self._list_lay.setSpacing(4); self._list_lay.addStretch()
        sc.setWidget(self._list_container); lay.addWidget(sc, stretch=1)
        return sb

    def _build_main(self) -> QWidget:
        main = QWidget()
        lay  = QVBoxLayout(main); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)

        # header
        hdr = QWidget(); hdr.setFixedHeight(52)
        hdr.setStyleSheet('border-bottom:1px solid rgba(255,255,255,0.048);')
        hl = QHBoxLayout(hdr); hl.setContentsMargins(18,0,18,0)
        self._title_lbl = QLabel('No clip selected')
        self._title_lbl.setStyleSheet(f'color:{TX};font-size:13px;font-weight:600;')
        self._meta_lbl  = QLabel(''); self._meta_lbl.setStyleSheet(f'color:{TX3};font-size:11px;')
        hl.addWidget(self._title_lbl); hl.addWidget(self._meta_lbl); hl.addStretch()
        lay.addWidget(hdr)

        # video
        self._video  = VideoLabel()
        self._sink   = QVideoSink()
        self._player = QMediaPlayer()
        self._audio  = QAudioOutput()
        # Main player drives video/timeline only; mixed playback comes from
        # per-track audio players created in _setup_audio_players.
        self._audio.setMuted(True); self._audio.setVolume(0.0)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.durationChanged.connect(self._on_duration)
        self._player.positionChanged.connect(self._on_position)
        self._player.playbackStateChanged.connect(self._on_state)
        lay.addWidget(self._video, stretch=1)

        # controls
        lay.addWidget(self._build_controls())
        return main

    def _build_controls(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(f'background:{S1};border-top:1px solid rgba(255,255,255,0.048);')
        lay = QVBoxLayout(panel); lay.setContentsMargins(18,14,18,0); lay.setSpacing(10)

        # timeline
        self._timeline = TrimTimeline()
        self._timeline.in_changed.connect(self._on_in_changed)
        self._timeline.out_changed.connect(self._on_out_changed)
        self._timeline.seek.connect(self._on_seek)
        lay.addWidget(self._timeline)

        # timecodes
        tr = QHBoxLayout()
        self._in_lbl  = QLabel('in: 0:00'); self._in_lbl.setStyleSheet(f'color:{ACC};font-size:11px;font-family:monospace;')
        self._pos_lbl = QLabel('0:00 / 0:00'); self._pos_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter); self._pos_lbl.setStyleSheet(f'color:{TX2};font-size:11px;font-family:monospace;')
        self._out_lbl = QLabel('out: 0:00'); self._out_lbl.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter); self._out_lbl.setStyleSheet(f'color:{ACC};font-size:11px;font-family:monospace;')
        tr.addWidget(self._in_lbl,1); tr.addWidget(self._pos_lbl,1); tr.addWidget(self._out_lbl,1)
        lay.addLayout(tr)

        # playback + export row
        br = QHBoxLayout(); br.setSpacing(8)
        self._play_btn = QPushButton('▶  Play')
        self._play_btn.setFixedHeight(36); self._play_btn.setMinimumWidth(90)
        self._play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._play_btn.setStyleSheet(_btn_css()); self._play_btn.clicked.connect(self._toggle_play)
        br.addWidget(self._play_btn)

        br.addStretch(1)
        self._export_btn = QPushButton('Export Clip')
        self._export_btn.setFixedHeight(36); self._export_btn.setMinimumWidth(140)
        self._export_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._export_btn.setToolTip('Export clip with current trim and audio mix settings')
        self._export_btn.setStyleSheet(_btn_css(accent=True))
        self._export_btn.clicked.connect(self._export_with_mix)
        br.addWidget(self._export_btn)
        br.addStretch(1)

        db = QPushButton('delete'); db.setFixedHeight(36)
        db.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        db.setStyleSheet(_btn_css(danger=True)); db.clicked.connect(self._delete_clip)
        br.addWidget(db)
        lay.addLayout(br)

        # audio mixer (hidden until a clip with audio tracks is loaded)
        self._mixer = AudioMixerPanel()
        self._mixer.setVisible(False)
        # mix_changed drives both: the boost hint (inside the panel) and live
        # per-track playback volume/mute.
        self._mixer.mix_changed.connect(self._on_mix_changed)
        lay.addWidget(self._mixer)

        return panel

    # ── mixer → live preview sync ──────────────────────────────────────────────

    def _on_mix_changed(self):
        """Apply mixer values to all playback audio tracks in real time."""
        if not self._audio_outputs:
            return
        volumes = self._mixer.get_volumes()
        for i, out in enumerate(self._audio_outputs):
            v = volumes[i] if i < len(volumes) else 1.0
            out.setMuted(v <= 0.0001)
            # QAudioOutput is capped at 1.0; >100% remains export-only boost.
            out.setVolume(min(1.0, max(0.0, v)))

    def _clear_audio_players(self):
        for p in self._audio_players:
            try:
                p.stop()
            except Exception:
                pass
            p.deleteLater()
        for out in self._audio_outputs:
            out.deleteLater()
        self._audio_players.clear(); self._audio_outputs.clear()

    def _setup_audio_players(self, path: Path, n_tracks: int):
        self._clear_audio_players()
        if n_tracks <= 0:
            return

        src = QUrl.fromLocalFile(str(path))
        for i in range(n_tracks):
            p = QMediaPlayer(self)
            out = QAudioOutput(self)
            out.setMuted(False); out.setVolume(1.0)
            p.setAudioOutput(out)
            p.setSource(src)

            # Each player decodes one audio stream only.
            def _bind_tracks(status, player=p, idx=i):
                if status == QMediaPlayer.MediaStatus.LoadedMedia:
                    try:
                        player.setActiveVideoTrack(-1)
                    except Exception:
                        pass
                    try:
                        player.setActiveSubtitleTrack(-1)
                    except Exception:
                        pass
                    try:
                        player.setActiveAudioTrack(self._base_audio_idx + idx)
                    except Exception:
                        pass
                    try:
                        player.mediaStatusChanged.disconnect(_bind_tracks)
                    except Exception:
                        pass

            p.mediaStatusChanged.connect(_bind_tracks)
            self._audio_players.append(p)
            self._audio_outputs.append(out)

        self._on_mix_changed()

    def _audio_set_position(self, ms: int):
        for p in self._audio_players:
            p.setPosition(ms)

    def _audio_play(self):
        for p in self._audio_players:
            p.play()

    def _audio_pause(self):
        for p in self._audio_players:
            p.pause()

    def _audio_stop(self):
        for p in self._audio_players:
            p.stop()

    # ── folder ─────────────────────────────────────────────────────────────────

    def _open_folder(self):
        p = str(Path(self.cfg['output_dir']).expanduser())
        # Inside Flatpak, Qt routes this through org.freedesktop.portal.OpenURI automatically.
        QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    # ── clip list ──────────────────────────────────────────────────────────────

    def _refresh_list(self):
        while self._list_lay.count() > 1:
            item = self._list_lay.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._sidebar_items.clear()

        out_dir = Path(self.cfg['output_dir']).expanduser()
        fmt = self.cfg.get('output_format', 'mp4')
        try:
            files = sorted(out_dir.glob(f'clip_*.{fmt}'), key=lambda f: f.stat().st_mtime, reverse=True)
        except OSError:
            files = []

        if not files:
            e = QLabel('No clips yet'); e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e.setStyleSheet(f'color:{TX3};font-size:11px;padding:16px 0;')
            self._list_lay.insertWidget(0, e)
            if self._current_path is not None and not self._current_path.exists():
                self._player.stop(); self._audio_stop(); self._clear_audio_players(); self._current_path = None
                self._title_lbl.setText('No clip selected'); self._meta_lbl.setText('')
                self._mixer.setVisible(False)
            return

        for f in files:
            item = SidebarItem(f, active=(f == self._current_path))
            item.selected.connect(lambda p: self._load_clip(Path(p)))
            self._sidebar_items.append(item)
            self._list_lay.insertWidget(self._list_lay.count()-1, item)

        if self._current_path is None or not self._current_path.exists():
            self._load_clip(self._sidebar_items[0].path)

    def _set_active(self, path: Path):
        for item in self._sidebar_items: item.set_active(item.path == path)

    # ── clip load ──────────────────────────────────────────────────────────────

    @staticmethod
    def _probe_track_layout(path: Path) -> tuple[int, int]:
        """Return (n_mixer_tracks, base_audio_index) for *path*.

        replayd clips saved with audio_mode='both' contain three audio streams:
          a:0  amix(game+mic)  disposition=default  <- heard by all players
          a:1  game only                            <- editable in viewer
          a:2  mic  only                            <- editable in viewer

        For that 3-stream layout we hide a:0 from the mixer and only expose
        a:1 / a:2, so (2, 1) is returned.
        For a normal 2-stream file: (2, 0).  Single-track: (1, 0).
        """
        try:
            r = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'a',
                 '-show_entries', 'stream=index', '-of', 'csv=p=0', str(path)],
                capture_output=True, text=True, timeout=8,
            )
            if r.returncode == 0:
                n = len([l for l in r.stdout.strip().splitlines() if l.strip()])
                if n == 3:
                    # replayd 'both' format: a:0 is pre-mixed default,
                    # skip it and expose only the individual tracks a:1/a:2.
                    return 2, 1
                return n, 0
        except Exception:
            pass
        return 1, 0

    def _load_clip(self, path: Path):
        self._current_path = path; self._set_active(path)
        self._player.stop(); self._audio_stop(); self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._title_lbl.setText(path.name)
        try:    self._meta_lbl.setText(f'  ·  {path.stat().st_size/1_048_576:.0f} MB')
        except: self._meta_lbl.setText('')
        self._in_ms = 0; self._out_ms = 1
        self._timeline.set_in(0.0); self._timeline.set_out(1.0); self._timeline.set_pos(0.0)

        n_tracks, self._base_audio_idx = self._probe_track_layout(path)
        self._mixer.load_tracks(n_tracks)
        self._setup_audio_players(path, n_tracks)

        try:    self._player.mediaStatusChanged.disconnect(self._on_prime)
        except: pass
        self._player.mediaStatusChanged.connect(self._on_prime)

    def _on_prime(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            try: self._player.mediaStatusChanged.disconnect(self._on_prime)
            except: pass
            self._player.play()
            self._audio_set_position(self._player.position())
            self._audio_play()

    def _on_frame(self, frame: QVideoFrame):
        if not frame.isValid(): return
        img = frame.toImage()
        if not img.isNull(): self._video.set_frame(QPixmap.fromImage(img))

    # ── player signals ─────────────────────────────────────────────────────────

    def _on_duration(self, ms: int):
        self._duration_ms = max(ms, 1); self._out_ms = self._duration_ms; self._update_lbl()

    def _on_position(self, ms: int):
        if self._duration_ms > 0: self._timeline.set_pos(ms / self._duration_ms)
        self._update_lbl(ms)
        if ms >= self._out_ms and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause(); self._audio_pause()
            self._player.setPosition(self._out_ms); self._audio_set_position(self._out_ms)

    def _on_state(self, state):
        self._play_btn.setText('⏸  Pause' if state == QMediaPlayer.PlaybackState.PlayingState else '▶  Play')

    def _update_lbl(self, pos_ms: int | None = None):
        if pos_ms is None: pos_ms = self._player.position()
        self._in_lbl.setText(f'in: {_ts(self._in_ms)}')
        self._out_lbl.setText(f'out: {_ts(self._out_ms)}')
        self._pos_lbl.setText(f'{_ts(pos_ms)} / {_ts(self._duration_ms)}')

    def _on_in_changed(self, r):  self._in_ms  = int(r * self._duration_ms); self._update_lbl()
    def _on_out_changed(self, r): self._out_ms = int(r * self._duration_ms); self._update_lbl()
    def _on_seek(self, r):
        ms = int(r * self._duration_ms)
        self._player.setPosition(ms)
        self._audio_set_position(ms)

    def _toggle_play(self):
        if not self._current_path: return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause(); self._audio_pause()
        else:
            if self._player.position() >= self._out_ms - 100:
                self._player.setPosition(self._in_ms)
                self._audio_set_position(self._in_ms)
            else:
                self._audio_set_position(self._player.position())
            self._player.play(); self._audio_play()

    # ── trim ───────────────────────────────────────────────────────────────────

    def _no_trim(self) -> bool:
        return self._in_ms == 0 and self._out_ms >= self._duration_ms - 200

    def _run_trim(self, src: Path, dst: Path) -> bool:
        r = subprocess.run([
            'ffmpeg', '-y',
            '-ss', f'{self._in_ms/1000:.3f}', '-to', f'{self._out_ms/1000:.3f}',
            '-i', str(src), '-map', '0', '-c', 'copy', str(dst),
        ], capture_output=True, text=True)
        if r.returncode != 0: print(f'[Viewer] trim error:\n{r.stderr[-600:]}')
        return r.returncode == 0

    # ── export with mix ────────────────────────────────────────────────────────

    def _export_with_mix(self):
        """
        Export the current clip with per-track volume mix applied.
        Uses ffmpeg filter_complex so video is copied (no re-encode).
        Respects the current trim in/out points if set.
        Output: new '_mixed' file in the same folder.
        """
        if not self._current_path: return

        volumes = self._mixer.get_volumes()
        n = len(volumes)
        if n == 0:
            QMessageBox.warning(self, 'No audio', 'No audio tracks to mix.'); return

        src = self._current_path
        ts  = datetime.now().strftime('%Y%m%d_%H%M%S')
        dst = src.parent / f'clip_{ts}_mixed{src.suffix}'

        cmd = ['ffmpeg', '-y']
        if not self._no_trim():
            cmd += ['-ss', f'{self._in_ms/1000:.3f}', '-to', f'{self._out_ms/1000:.3f}']
        cmd += ['-i', str(src)]

        base = getattr(self, '_base_audio_idx', 0)
        if n == 1:
            fc = f'[0:a:{base}]volume={volumes[0]:.4f}[aout]'
        else:
            parts  = [f'[0:a:{base + i}]volume={v:.4f}[a{i}]' for i, v in enumerate(volumes)]
            mix_in = ''.join(f'[a{i}]' for i in range(n))
            parts.append(f'{mix_in}amix=inputs={n}:normalize=0[aout]')
            fc = ';'.join(parts)

        cmd += ['-filter_complex', fc, '-map', '0:v', '-map', '[aout]',
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', str(dst)]

        self._export_btn.setEnabled(False)
        result = subprocess.run(cmd, capture_output=True, text=True)
        self._export_btn.setEnabled(True)

        if result.returncode == 0:
            mb = dst.stat().st_size / 1_048_576
            print(f'[Viewer] Exported: {dst.name}  ({mb:.1f} MB)')
            self._refresh_list(); self._load_clip(dst)
        else:
            QMessageBox.critical(self, 'Export failed',
                f'ffmpeg returned an error:\n\n{result.stderr[-800:]}')

    # ── delete ─────────────────────────────────────────────────────────────────

    def _delete_clip(self):
        if not self._current_path: return
        if QMessageBox.question(self, 'Delete clip?', f'Permanently delete {self._current_path.name}?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        ) != QMessageBox.StandardButton.Yes: return
        self._player.stop(); self._audio_stop(); get_thumb_path(self._current_path).unlink(missing_ok=True)
        try: self._current_path.unlink(missing_ok=True)
        except OSError as e: QMessageBox.critical(self, 'Error', str(e)); return
        self._current_path = None; self._refresh_list()

    # ── public API ─────────────────────────────────────────────────────────────

    def on_clip_saved(self, path: Path, size_mb: float):
        self._refresh_list()

    def open_viewer(self, path: Path | None = None):
        self.show(); self.raise_(); self.activateWindow()
        self._refresh_list()
        if path is not None and isinstance(path, Path) and path.exists():
            self._load_clip(path)
        elif self._current_path is None and self._sidebar_items:
            self._load_clip(self._sidebar_items[0].path)

    def closeEvent(self, e):
        self._player.pause(); self._audio_pause(); self.hide(); e.ignore()

    @staticmethod
    def _btn_css(accent=False, danger=False) -> str:
        return _btn_css(accent=accent, danger=danger)