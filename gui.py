"""
gui.py – System tray + main window for replayd

Tray: left-click toggles the window, right-click shows menu.
Window: frameless, draggable, positioned bottom-right of screen on first launch.
Arrow tab on right edge opens/closes the ClipViewer as a separate top-level window
positioned immediately to the left — this avoids inheriting WA_TranslucentBackground
and gives QVideoWidget its own native rendering surface (required on Wayland).
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QSizePolicy,
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QFont, QCursor, QDesktopServices,
)
from PyQt6.QtCore import (
    Qt, QTimer, QRectF, QPoint, QSize, pyqtSignal, QUrl,
    QPropertyAnimation, QEasingCurve, QFileSystemWatcher, QThread,
)

# ── colour palette ────────────────────────────────────────────────────────────
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
GRN   = '#5ab87a'

CARD_W = 416

# ── thumbnail helpers ─────────────────────────────────────────────────────────

from paths import THUMB_DIR
_active_thumb_workers: list = []


def get_thumb_path(video_path: Path) -> Path:
    return THUMB_DIR / (video_path.stem + '.jpg')


def _generate_thumb(video_path: Path) -> Optional[Path]:
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
        dur  = float(raw) if probe.returncode == 0 and raw else 10.0
        seek = max(0.5, dur * 0.25)
        subprocess.run(
            ['ffmpeg', '-y', '-ss', f'{seek:.2f}', '-i', str(video_path),
             '-frames:v', '1', '-q:v', '4', '-vf', 'scale=160:-1', str(thumb)],
            capture_output=True, timeout=15,
        )
        return thumb if thumb.exists() else None
    except Exception:
        return None


class ThumbWorker(QThread):
    done = pyqtSignal(str, str)

    def __init__(self, video_path: Path):
        super().__init__()
        self._path = video_path

    def run(self):
        result = _generate_thumb(self._path)
        if result:
            self.done.emit(str(self._path), str(result))


# ── helpers ───────────────────────────────────────────────────────────────────

_APP_ICON_NAME = 'io.github.rgtd_faustino.replayd'


def _find_icon_path() -> Optional[Path]:
    """Locate the app icon PNG/SVG, checking the dev tree and XDG icon dirs."""
    import os
    here = Path(__file__).parent
    candidates = [
        here / 'icons' / 'hicolor' / '256x256' / 'apps' / f'{_APP_ICON_NAME}.png',
        here / 'icons' / 'hicolor' / 'scalable' / 'apps' / f'{_APP_ICON_NAME}.svg',
        # Common Flatpak install location (icons are not under /app/share/replayd).
        Path('/app/share/icons/hicolor/256x256/apps') / f'{_APP_ICON_NAME}.png',
        Path('/app/share/icons/hicolor/scalable/apps') / f'{_APP_ICON_NAME}.svg',
    ]

    # When running from an installed tree, Python modules may be in
    # /app/share/replayd while icons live in /app/share/icons.
    parent = here.parent
    candidates.extend([
        parent / 'icons' / 'hicolor' / '256x256' / 'apps' / f'{_APP_ICON_NAME}.png',
        parent / 'icons' / 'hicolor' / 'scalable' / 'apps' / f'{_APP_ICON_NAME}.svg',
    ])

    xdg_data_dirs = os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')
    for data_dir in xdg_data_dirs.split(':'):
        candidates.append(
            Path(data_dir) / 'icons' / 'hicolor' / '256x256' / 'apps' / f'{_APP_ICON_NAME}.png'
        )
        candidates.append(
            Path(data_dir) / 'icons' / 'hicolor' / 'scalable' / 'apps' / f'{_APP_ICON_NAME}.svg'
        )
    return next((p for p in candidates if p.exists()), None)


def _load_app_icon(size: int) -> Optional[QPixmap]:
    """Return a QPixmap of the app icon scaled to *size* px, or None."""
    # First, try themed icon lookup. This is what desktop shells typically use.
    themed = QIcon.fromTheme(_APP_ICON_NAME)
    if not themed.isNull():
        themed_px = themed.pixmap(size, size)
        if not themed_px.isNull():
            return themed_px

    icon_path = _find_icon_path()
    if icon_path is None:
        return None
    px = QPixmap(str(icon_path))
    if px.isNull():
        return None
    return px.scaled(size, size,
                     Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)


def _tray_icon(hex_color: str, size: int = 22) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(hex_color)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, size - 4, size - 4)
    p.end()
    return QIcon(px)


# ── arc widget ────────────────────────────────────────────────────────────────

class ArcWidget(QWidget):
    def __init__(self, label: str, fill_color: str = ACC, parent=None):
        super().__init__(parent)
        self.setFixedSize(172, 172)
        self._label      = label
        self._fill_color = fill_color
        self._value      = 0
        self._max        = 40

    def set_value(self, v: float, max_v: float):
        self._value = v
        self._max   = max(max_v, 1)
        self.update()

    def paintEvent(self, _event):
        p  = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r  = 66.0
        cx = self.width()  / 2
        cy = self.height() / 2
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        pen_track = QPen(QColor(S2))
        pen_track.setWidthF(9)
        pen_track.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen_track)
        p.drawEllipse(rect)

        ratio = min(self._value / self._max, 1.0)
        if ratio > 0:
            pen_fill = QPen(QColor(self._fill_color))
            pen_fill.setWidthF(9)
            pen_fill.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen_fill)
            p.drawArc(rect, 90 * 16, -int(ratio * 360 * 16))

        p.setPen(QColor(TX))
        f_big = QFont(); f_big.setFamily('Bebas Neue'); f_big.setPixelSize(60)
        p.setFont(f_big)
        p.drawText(QRectF(cx-60, cy-42, 120, 60), Qt.AlignmentFlag.AlignCenter,
                   f'{self._value:.1f}')

        p.setPen(QColor(TX3))
        f_lbl = QFont(); f_lbl.setFamily('DM Sans'); f_lbl.setPixelSize(9)
        f_lbl.setWeight(QFont.Weight.Medium)
        f_lbl.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
        p.setFont(f_lbl)
        p.drawText(QRectF(cx-70, cy+20, 140, 20), Qt.AlignmentFlag.AlignCenter,
                   self._label)
        p.end()


# ── clip row ──────────────────────────────────────────────────────────────────

class ClipRow(QWidget):
    delete_clip_req = pyqtSignal(str)
    open_clip_req   = pyqtSignal(str)

    def __init__(self, path: Path, size_mb: float, elapsed: str, parent=None):
        super().__init__(parent)
        self.path    = path
        self._worker: Optional[ThumbWorker] = None
        self.setFixedHeight(58)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 0, 11, 0)
        lay.setSpacing(10)

        self._thumb_lbl = QLabel()
        self._thumb_lbl.setFixedSize(72, 45)
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_lbl.setText('▶')
        self._thumb_lbl.setStyleSheet(
            f'background:{S3};border-radius:7px;color:rgba(255,255,255,0.28);font-size:11px;'
        )
        lay.addWidget(self._thumb_lbl)

        info = QVBoxLayout()
        info.setSpacing(2); info.setContentsMargins(0, 0, 0, 0)
        name_lbl = QLabel(path.name)
        name_lbl.setStyleSheet(
            f'color:{TX};font-size:12px;font-weight:600;background:transparent;border:none;'
        )
        meta_lbl = QLabel(f'{size_mb:.0f} MB · {elapsed}')
        meta_lbl.setStyleSheet(
            f'color:{TX3};font-size:10px;background:transparent;border:none;'
        )
        info.addWidget(name_lbl); info.addWidget(meta_lbl)
        lay.addLayout(info, stretch=1)

        del_btn = QPushButton('✕')
        del_btn.setFixedSize(26, 26)
        del_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        del_btn.setToolTip('Delete clip')
        del_btn.setStyleSheet(f'''
            QPushButton {{
                background:{S3};border:none;border-radius:7px;
                color:{TX3};font-size:12px;
            }}
            QPushButton:hover {{ background:rgba(220,70,70,0.16);color:#e45a5a; }}
        ''')
        del_btn.clicked.connect(lambda: self.delete_clip_req.emit(str(self.path)))
        lay.addWidget(del_btn)

        self.setStyleSheet(f'''
            ClipRow {{
                background:{S1};border-radius:10px;
                border:1px solid rgba(255,255,255,0.032);
            }}
            ClipRow:hover {{ background:{S2}; }}
        ''')
        self._load_thumb()

    def _load_thumb(self):
        thumb = get_thumb_path(self.path)
        if thumb.exists():
            self._apply_pixmap(str(thumb)); return
        worker = ThumbWorker(self.path)
        worker.done.connect(self._on_thumb_ready)
        worker.finished.connect(lambda: (
            _active_thumb_workers.remove(worker) if worker in _active_thumb_workers else None
        ))
        _active_thumb_workers.append(worker)
        self._worker = worker
        worker.start()

    def _on_thumb_ready(self, video_path: str, thumb_path: str):
        if video_path == str(self.path):
            self._apply_pixmap(thumb_path)

    def _apply_pixmap(self, thumb_path: str):
        px = QPixmap(thumb_path)
        if not px.isNull():
            scaled = px.scaled(72, 45, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            self._thumb_lbl.setPixmap(scaled)
            self._thumb_lbl.setText('')
            self._thumb_lbl.setStyleSheet('background:#080808;border-radius:7px;')

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.open_clip_req.emit(str(self.path))
        super().mousePressEvent(e)


# ── drag handle ───────────────────────────────────────────────────────────────

class _DragHandle(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle:
                handle.startSystemMove()
        super().mousePressEvent(e)


# ── main window ───────────────────────────────────────────────────────────────

class ReplaydWindow(QWidget):
    """Main replayd widget — frameless, draggable, dark amber design."""

    def __init__(self, config: dict, clip_saver, buffer_manager):
        super().__init__()
        self.cfg    = config
        self.clip   = clip_saver
        self.buf    = buffer_manager
        self._capture_after_hotkey = bool(config.get('capture_after_hotkey', True))
        self._ready               = False
        self._on_quit             = None
        self._before_frozen_secs: Optional[float] = None
        self._was_busy            = False
        self._settings            = None
        self._viewer: Optional['ClipViewer'] = None  # top-level window
        self._viewer_open         = False

        self._setup_window()

        # Watcher must exist before _build_ui (which calls _refresh_clips)
        self._watcher = QFileSystemWatcher(self)
        _out = Path(config['output_dir']).expanduser()
        if _out.exists():
            self._watcher.addPath(str(_out))

        self._build_ui()
        self._watcher.directoryChanged.connect(lambda _: self._refresh_clips())

        timer = QTimer(self)
        timer.timeout.connect(self._refresh_status)
        timer.start(100)

    # ── window setup ──────────────────────────────────────────────────────────

    def _setup_window(self):
        self.setWindowTitle('replayd')
        self.setFixedWidth(CARD_W + 20)  # card + margins
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint,
        )
        _icon_px = _load_app_icon(256)
        if _icon_px:
            self.setWindowIcon(QIcon(_icon_px))

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        # Main card
        self._card = QWidget(self)
        self._card.setObjectName('card')
        self._card.setFixedWidth(CARD_W)
        self._card.setStyleSheet(f'''
            QWidget#card {{
                background:{BG};border-radius:18px;
                border:1px solid rgba(255,255,255,0.055);
            }}
        ''')
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        card_lay.addWidget(self._build_header())
        card_lay.addWidget(self._build_arc_section())
        card_lay.addWidget(self._build_save_section())
        card_lay.addWidget(self._build_clips_section())
        outer.addWidget(self._card)


    # ── header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        hdr = _DragHandle()
        hdr.setObjectName('hdr')
        hdr.setFixedHeight(58)
        hdr.setStyleSheet('QWidget#hdr { border-bottom:1px solid rgba(255,255,255,0.048); }')

        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(10)

        logo = QLabel()
        logo.setFixedSize(30, 30)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet('background:transparent;')
        _icon_px = _load_app_icon(30)
        if _icon_px:
            logo.setPixmap(_icon_px)
        else:
            # Fallback: orange square with 'R' if icon file not found
            logo.setText('R')
            logo.setStyleSheet(f'background:{ACC};border-radius:9px;color:#12100d;font-size:17px;font-weight:700;')
        lay.addWidget(logo)

        app_name = QLabel('replayd')
        app_name.setStyleSheet(f'color:{TX};font-size:15px;font-weight:600;')
        lay.addWidget(app_name)
        lay.addStretch()

        self._pill = QLabel('● Starting…')
        self._pill.setFixedHeight(24)
        self._pill.setStyleSheet(self._pill_style('start'))
        lay.addWidget(self._pill)

        sett_btn = QPushButton('⚙')
        sett_btn.setFixedSize(32, 32)
        sett_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        sett_btn.setStyleSheet(f'''
            QPushButton {{ background:{S2};border:none;border-radius:9px;color:{TX2};font-size:14px; }}
            QPushButton:hover {{ background:{S3};color:{TX}; }}
        ''')
        sett_btn.clicked.connect(self._open_settings)
        lay.addWidget(sett_btn)

        close_btn = QPushButton('✕')
        close_btn.setFixedSize(32, 32)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.setStyleSheet(f'''
            QPushButton {{ background:{S2};border:none;border-radius:9px;color:{TX2};font-size:13px; }}
            QPushButton:hover {{ background:rgba(220,70,70,0.16);color:#e45a5a; }}
        ''')
        close_btn.clicked.connect(self._request_quit)
        lay.addWidget(close_btn)
        return hdr

    @staticmethod
    def _pill_style(mode: str) -> str:
        styles = {
            'rec':   ('rgba(90,184,122,0.10)',  'rgba(90,184,122,0.22)',  GRN),
            'sav':   ('rgba(218,123,36,0.12)',  'rgba(218,123,36,0.28)',  ACC),
            'start': ('rgba(58,142,246,0.12)',  'rgba(58,142,246,0.22)',  '#3a8ef6'),
            'buf':   ('rgba(138,128,120,0.10)', 'rgba(138,128,120,0.22)', TX2),
        }
        bg, border, color = styles.get(mode, styles['start'])
        return f'''
            background:{bg};border:1px solid {border};border-radius:12px;
            color:{color};font-size:10px;font-weight:600;
            padding:0 10px;letter-spacing:0.4px;
        '''

    def _set_pill(self, mode: str, text: str):
        self._pill.setText(text)
        self._pill.setStyleSheet(self._pill_style(mode))

    # ── arc section ───────────────────────────────────────────────────────────

    def _build_arc_section(self) -> QWidget:
        sec = QWidget()
        lay = QVBoxLayout(sec)
        lay.setContentsMargins(22, 26, 22, 14)
        lay.setSpacing(16)
        lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        if self._capture_after_hotkey:
            arc_row = QHBoxLayout()
            arc_row.setContentsMargins(0, 0, 0, 0)
            arc_row.setSpacing(14)
            arc_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._arc_before = ArcWidget('SEC BEFORE', GRN)
            self._arc_after  = ArcWidget('POST HOTKEY', ACC)
            arc_row.addWidget(self._arc_before)
            arc_row.addWidget(self._arc_after)
            lay.addLayout(arc_row)
        else:
            self._arc_before = ArcWidget('SEC BUFFERED', GRN)
            self._arc_after  = None
            lay.addWidget(self._arc_before, alignment=Qt.AlignmentFlag.AlignHCenter)

        lay.addWidget(self._build_meta_strip())
        return sec

    def _build_meta_strip(self) -> QWidget:
        strip = QWidget()
        strip.setFixedHeight(54)
        strip.setStyleSheet(f'background:{S1};border-radius:12px;border:1px solid rgba(255,255,255,0.04);')
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        audio_txt = {'game': 'Game', 'mic': 'Mic', 'both': 'Both'}.get(
            self.cfg.get('audio_mode', 'both'), 'Both')
        after_val = str(self.cfg.get('seconds_after', 10)) if self._capture_after_hotkey else 'Off'
        meta_items = [
            (str(self.cfg.get('seconds_before', 30)), 'Before'),
            (after_val, 'After'),
            (self.cfg.get('output_format', 'mp4').upper(), 'Format'),
            (audio_txt, 'Audio'),
        ]

        self._meta_vals: dict[str, QLabel] = {}
        for i, (val, key) in enumerate(meta_items):
            if i > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFixedWidth(1)
                sep.setStyleSheet('background:rgba(255,255,255,0.04);border:none;')
                lay.addWidget(sep)
            cell = QWidget(); cell.setStyleSheet('background:transparent;border:none;')
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(6, 0, 6, 0); cl.setSpacing(4)
            cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v = QLabel(val)
            v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.setStyleSheet(f'color:{TX};font-size:20px;font-weight:700;background:transparent;border:none;')
            self._meta_vals[key] = v
            k = QLabel(key.upper())
            k.setAlignment(Qt.AlignmentFlag.AlignCenter)
            k.setStyleSheet(f'color:{TX3};font-size:9px;font-weight:600;letter-spacing:1.5px;background:transparent;border:none;')
            cl.addWidget(v); cl.addWidget(k)
            lay.addWidget(cell, stretch=1)
        return strip

    # ── save section ──────────────────────────────────────────────────────────

    def _build_save_section(self) -> QWidget:
        sec = QWidget()
        lay = QVBoxLayout(sec)
        lay.setContentsMargins(18, 16, 18, 5)
        lay.setSpacing(9)

        self._save_btn = QPushButton('⏺  Save Clip')
        self._save_btn.setFixedHeight(52)
        self._save_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._save_btn.setStyleSheet(self._save_btn_style('idle'))
        self._save_btn.clicked.connect(self._trigger_save)
        lay.addWidget(self._save_btn)

        # The actual key binding is owned by KDE System Settings (Global Shortcuts).
        # config['hotkey'] is only a preferred-trigger *hint* sent to the portal
        # on first registration — it is not the live bound key.  Show a generic
        # label instead of a stale / wrong key name.
        hint = QLabel('or use your  <b>KDE global shortcut</b>')
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setStyleSheet(f'color:{TX3};font-size:11px;margin-bottom:8px;background:transparent;')
        lay.addWidget(hint)
        return sec

    @staticmethod
    def _save_btn_style(mode: str) -> str:
        if mode == 'busy':
            return f'QPushButton {{ background:{S2};color:{TX2};border:none;border-radius:13px;font-size:15px;font-weight:700; }}'
        return f'''
            QPushButton {{
                background:{ACC};color:#12100d;border:none;
                border-radius:13px;font-size:15px;font-weight:700;padding-bottom:4px;
            }}
            QPushButton:hover  {{ background:{ACC_H}; }}
            QPushButton:pressed {{ background:{ACC_S};padding-top:4px;padding-bottom:0; }}
            QPushButton:disabled {{ background:{S2};color:{TX2}; }}
        '''

    # ── clips section ─────────────────────────────────────────────────────────

    def _build_clips_section(self) -> QWidget:
        sec = QWidget()
        sec.setObjectName('clips')
        sec.setStyleSheet('QWidget#clips { border-top:1px solid rgba(255,255,255,0.048); }')
        lay = QVBoxLayout(sec)
        lay.setContentsMargins(18, 13, 18, 16)
        lay.setSpacing(9)

        hdr_row = QHBoxLayout()
        title = QLabel('RECENT CLIPS')
        title.setStyleSheet(
            f'color:{TX3};font-size:10px;font-weight:600;letter-spacing:1.8px;background:transparent;'
        )

        _link_css = f'''
            QPushButton {{ background:none;border:none;color:{TX3};font-size:11px; }}
            QPushButton:hover {{ color:{ACC}; }}
        '''
        open_btn = QPushButton('Open folder →')
        open_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        open_btn.setStyleSheet(_link_css)
        open_btn.clicked.connect(self._open_folder)

        hdr_row.addWidget(title)
        hdr_row.addStretch()
        hdr_row.addWidget(open_btn)
        lay.addLayout(hdr_row)

        self._clips_lay = QVBoxLayout()
        self._clips_lay.setSpacing(5)
        lay.addLayout(self._clips_lay)

        # ── full-width Clip Viewer button ──────────────────────────────────────
        self._viewer_btn = QPushButton('View All Clips')
        self._viewer_btn.setFixedHeight(42)
        self._viewer_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._viewer_btn.setStyleSheet(self._viewer_btn_css(active=False))
        self._viewer_btn.clicked.connect(self._toggle_viewer)
        lay.addWidget(self._viewer_btn)

        self._refresh_clips()
        return sec

    def _refresh_clips(self):
        while self._clips_lay.count():
            item = self._clips_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        out_dir = Path(self.cfg['output_dir']).expanduser()
        fmt = self.cfg.get('output_format', 'mp4')
        if out_dir.exists() and str(out_dir) not in self._watcher.directories():
            self._watcher.addPath(str(out_dir))

        try:
            files = sorted(
                out_dir.glob(f'clip_*.{fmt}'),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )[:3]
        except OSError:
            files = []

        now = time.time()
        for f in files:
            size_mb = f.stat().st_size / 1_048_576
            age     = now - f.stat().st_mtime
            elapsed = (
                'just now'               if age < 60   else
                f'{int(age/60)} min ago' if age < 3600 else
                f'{int(age/3600)} hr ago'
            )
            row = ClipRow(f, size_mb, elapsed)
            row.delete_clip_req.connect(self._delete_clip)
            row.open_clip_req.connect(self._open_clip)
            self._clips_lay.addWidget(row)

        if not files:
            empty = QLabel('No clips yet — press Save Clip to capture one.')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(
                f'color:{TX3};font-size:11px;padding:12px 0;background:transparent;'
            )
            self._clips_lay.addWidget(empty)

    # ── viewer toggle ─────────────────────────────────────────────────────────

    @staticmethod
    def _viewer_btn_css(active: bool) -> str:
        if active:
            return f'''
                QPushButton {{
                    background:{ACC_S};color:{TX};border:none;
                    border-radius:13px;font-size:14px;font-weight:700;
                }}
                QPushButton:hover {{ background:{ACC};color:#12100d; }}
                QPushButton:pressed {{ background:{ACC_S}; }}
            '''
        return f'''
            QPushButton {{
                background:{ACC};color:#12100d;border:none;
                border-radius:13px;font-size:14px;font-weight:700;
            }}
            QPushButton:hover {{ background:{ACC_H}; }}
            QPushButton:pressed {{ background:{ACC_S};color:{TX}; }}
        '''

    def _get_viewer(self) -> 'ClipViewer':
        if self._viewer is None:
            from viewer import ClipViewer
            self._viewer = ClipViewer(self.cfg)
        return self._viewer

    def _toggle_viewer(self):
        viewer = self._get_viewer()
        if self._viewer_open and viewer.isVisible():
            viewer.hide()
            self._viewer_open = False
        else:
            self._show_viewer()
        self._viewer_btn.setStyleSheet(self._viewer_btn_css(active=self._viewer_open))

    def _show_viewer(self, path: Optional[Path] = None):
        viewer = self._get_viewer()
        # Position the viewer window to the left of the main card
        geom   = self.frameGeometry()
        vw     = viewer.width()
        vh     = viewer.height()
        gap    = 8
        # Try to align tops, but keep on screen
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            vx = max(avail.left(), geom.left() - vw - gap)
            vy = max(avail.top(), min(geom.top(), avail.bottom() - vh))
        else:
            vx = max(0, geom.left() - vw - gap)
            vy = geom.top()
        viewer.move(vx, vy)
        viewer.open_viewer(path)
        self._viewer_open = True
        self._viewer_btn.setStyleSheet(self._viewer_btn_css(active=True))

    # ── public ────────────────────────────────────────────────────────────────

    def mark_ready(self):
        self._ready = True

    def set_quit_callback(self, callback):
        self._on_quit = callback

    def on_clip_saved(self, path: Path, _size_mb: float):
        out_dir = str(path.parent)
        if out_dir not in self._watcher.directories():
            self._watcher.addPath(out_dir)
        self._refresh_clips()

    # ── slots ─────────────────────────────────────────────────────────────────

    def _trigger_save(self):
        if self._ready and not self.clip._busy:
            asyncio.ensure_future(self.clip.save())

    def _request_quit(self):
        if callable(self._on_quit):
            self._on_quit()
        else:
            QApplication.quit()

    @staticmethod
    def _open_path(path: str):
        resolved = Path(os.path.expandvars(path)).expanduser()
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f'[GUI] Could not create output folder: {e}')
            return

        # Inside Flatpak, Qt routes this through org.freedesktop.portal.OpenURI automatically.
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved.resolve())))
        if ok:
            return

        # Keep Flatpak runs portal-only; use native fallback only outside sandbox.
        if os.path.exists('/.flatpak-info'):
            print('[GUI] Could not open output folder via portal/Qt.')
            return

        # Fallback for native environments where QDesktopServices has no handler.
        try:
            subprocess.Popen(
                ['xdg-open', str(resolved.resolve())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            print(f'[GUI] Could not open output folder: {e}')

    def _open_folder(self):
        self._open_path(self.cfg['output_dir'])

    def _open_clip(self, path: str):
        self._show_viewer(Path(path))

    def _delete_clip(self, path: str):
        try:
            Path(path).unlink(missing_ok=True)
            print(f'[GUI] Deleted clip: {Path(path).name}')
        except OSError as e:
            print(f'[GUI] Could not delete clip: {e}')
        self._refresh_clips()
        if self._viewer is not None:
            self._viewer._refresh_list()

    def _open_settings(self):
        from settings import SettingsOverlay
        if self._settings is None:
            self._settings = SettingsOverlay(self.cfg, parent=None)
            self._settings.closed.connect(self._settings.hide)
        self._settings.set_config(self.cfg)
        self._settings.open_over(self.frameGeometry())

    # ── periodic refresh ──────────────────────────────────────────────────────

    def _refresh_status(self):
        if not self._ready:
            self._set_pill('start', '● Starting…')
            self._save_btn.setText('● Starting app…')
            self._save_btn.setStyleSheet(self._save_btn_style('busy'))
            self._save_btn.setEnabled(False)
            return

        # GStreamer is running but hasn't written the first segment yet
        has_segments = len(self.buf.seg_log) > 0
        if not has_segments:
            self._set_pill('buf', '● Buffering…')
            self._save_btn.setText('● Loading up…')
            self._save_btn.setStyleSheet(self._save_btn_style('busy'))
            self._save_btn.setEnabled(False)
            return

        before_max       = max(int(self.cfg.get('seconds_before', 0)), 1)
        started_at       = getattr(self.buf, 'recording_started_at', None)
        live_before_secs = 0.0 if started_at is None else min(
            time.time() - started_at, before_max)

        if self._capture_after_hotkey and self.clip._busy and not self._was_busy:
            self._before_frozen_secs = live_before_secs
        elif not self.clip._busy or not self._capture_after_hotkey:
            self._before_frozen_secs = None

        before_secs = (
            self._before_frozen_secs
            if self._before_frozen_secs is not None
            else live_before_secs
        )
        self._arc_before.set_value(before_secs, before_max)

        if self._capture_after_hotkey and self._arc_after is not None:
            post_elapsed, post_target = self.clip.post_trigger_state()
            self._arc_after.set_value(post_elapsed, max(int(post_target), 1))

        if self.clip._busy:
            self._set_pill('sav', '● Saving')
            self._save_btn.setText('● Saving…')
            self._save_btn.setStyleSheet(self._save_btn_style('busy'))
            self._save_btn.setEnabled(False)
        else:
            self._set_pill('rec', '● Recording')
            self._save_btn.setText('⏺  Save Clip')
            self._save_btn.setStyleSheet(self._save_btn_style('idle'))
            self._save_btn.setEnabled(True)

        self._was_busy = self.clip._busy

    # ── window events ─────────────────────────────────────────────────────────

    def closeEvent(self, e):
        if self._settings is not None and self._settings.isVisible():
            self._settings.hide()
        self._request_quit()
        e.ignore()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._settings is not None and self._settings.isVisible():
            self._settings.open_over(self.frameGeometry())

    def paintEvent(self, _event):
        pass


# ── tray app ──────────────────────────────────────────────────────────────────

class TrayApp:
    """Window manager — no system tray icon.  The main window is the only UI surface."""

    def __init__(self, config: dict, clip_saver, buffer_manager, on_quit=None):
        self.cfg      = config
        self.clip     = clip_saver
        self.buf      = buffer_manager
        self._on_quit = on_quit

        self.window = ReplaydWindow(config, clip_saver, buffer_manager)
        self.window.set_quit_callback(self._request_quit)
        self._position_window()
        self.window.show()

    def _position_window(self):
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            w    = self.window.sizeHint().width()
            h    = self.window.sizeHint().height()
            self.window.move(geom.right() - w - 20, geom.bottom() - h - 20)

    def mark_ready(self):
        self.window.mark_ready()

    def _request_quit(self):
        if callable(self._on_quit):
            self._on_quit()
        else:
            QApplication.quit()

    @staticmethod
    def notify(title: str, body: str):
        asyncio.ensure_future(_dbus_notify(title, body))