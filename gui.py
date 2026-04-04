"""
gui.py – System tray + main window for replayd

Tray: left-click toggles the window, right-click shows menu.
Window: frameless, draggable, positioned bottom-right of screen on first launch.
"""

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QSystemTrayIcon, QMenu, QSizePolicy,
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QFont, QCursor, QDesktopServices,
)
from PyQt6.QtCore import Qt, QTimer, QRectF, QPoint, QSize, pyqtSignal, QUrl

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

# ── helpers ───────────────────────────────────────────────────────────────────

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
    """Circular arc showing how many seconds are buffered."""

    def __init__(self, label: str, fill_color: str = ACC, parent=None):
        super().__init__(parent)
        self.setFixedSize(172, 172)
        self._label = label
        self._fill_color = fill_color
        self._value = 0
        self._max   = 40

    def set_value(self, v: float, max_v: float):
        self._value = v
        self._max   = max(max_v, 1)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        r    = 66.0
        cx   = self.width()  / 2
        cy   = self.height() / 2
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        # Track ring
        pen_track = QPen(QColor(S2))
        pen_track.setWidthF(9)
        pen_track.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen_track)
        p.drawEllipse(rect)

        # Filled arc (starts at 12 o'clock, goes clockwise)
        ratio = min(self._value / self._max, 1.0)
        if ratio > 0:
            pen_fill = QPen(QColor(self._fill_color))
            pen_fill.setWidthF(9)
            pen_fill.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen_fill)
            start_angle = 90 * 16                  # 12 o'clock in Qt units
            span_angle  = -int(ratio * 360 * 16)   # clockwise = negative
            p.drawArc(rect, start_angle, span_angle)

        # Big number
        p.setPen(QColor(TX))
        f_big = QFont()
        f_big.setFamily('Bebas Neue')
        f_big.setPixelSize(60)
        p.setFont(f_big)
        p.drawText(
            QRectF(cx - 60, cy - 42, 120, 60),
            Qt.AlignmentFlag.AlignCenter,
            f'{self._value:.1f}',
        )

        # Label
        p.setPen(QColor(TX3))
        f_lbl = QFont()
        f_lbl.setFamily('DM Sans')
        f_lbl.setPixelSize(9)
        f_lbl.setWeight(QFont.Weight.Medium)
        f_lbl.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
        p.setFont(f_lbl)
        p.drawText(
            QRectF(cx - 70, cy + 20, 140, 20),
            Qt.AlignmentFlag.AlignCenter,
            self._label,
        )

        p.end()


# ── clip row ──────────────────────────────────────────────────────────────────

class ClipRow(QWidget):
    delete_clip_req = pyqtSignal(str)
    open_clip_req = pyqtSignal(str)

    def __init__(self, path: Path, size_mb: float, elapsed: str, parent=None):
        super().__init__(parent)
        self.path = path
        self.setFixedHeight(52)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 0, 11, 0)
        lay.setSpacing(11)

        # Thumbnail placeholder
        thumb = QLabel('▶')
        thumb.setFixedSize(54, 34)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(f'background:{S3};border-radius:6px;color:rgba(255,255,255,0.28);font-size:11px;')
        lay.addWidget(thumb)

        # Name + meta
        info = QVBoxLayout()
        info.setSpacing(2)
        info.setContentsMargins(0, 0, 0, 0)
        name_lbl = QLabel(path.name)
        name_lbl.setStyleSheet(f'color:{TX};font-size:12px;font-weight:600;background:transparent;border:none;')
        meta_lbl = QLabel(f'{size_mb:.0f} MB · {elapsed}')
        meta_lbl.setStyleSheet(f'color:{TX3};font-size:10px;background:transparent;border:none;')
        info.addWidget(name_lbl)
        info.addWidget(meta_lbl)
        lay.addLayout(info, stretch=1)

        # Delete button
        del_btn = QPushButton('🗑')
        del_btn.setFixedSize(26, 26)
        del_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        del_btn.setToolTip('Delete clip')
        del_btn.setStyleSheet(f'''
            QPushButton {{
                background: {S3}; border: none; border-radius: 7px;
                color: {TX3}; font-size: 12px;
            }}
            QPushButton:hover {{ background: rgba(220,70,70,0.16); color: #e45a5a; }}
        ''')
        del_btn.clicked.connect(lambda: self.delete_clip_req.emit(str(self.path)))
        lay.addWidget(del_btn)

        self.setStyleSheet(f'''
            ClipRow {{
                background: {S1};
                border-radius: 10px;
                border: 1px solid rgba(255,255,255,0.032);
            }}
            ClipRow:hover {{ background: {S2}; }}
        ''')

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.open_clip_req.emit(str(self.path))
        super().mousePressEvent(e)


# ── settings overlay ──────────────────────────────────────────────────────────
# Imported here to avoid circular import; SettingsOverlay is defined in settings.py

class _DragHandle(QWidget):
    """Widget que arrasta a janela de topo quando clicado e movido."""
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
    """Main replayd window – frameless, draggable, dark amber design."""

    def __init__(self, config: dict, clip_saver, buffer_manager):
        super().__init__()
        self.cfg    = config
        self.clip   = clip_saver
        self.buf    = buffer_manager
        self._capture_after_hotkey = bool(config.get('capture_after_hotkey', True))
        self._ready = False
        self._on_quit = None
        self._before_frozen_secs: Optional[float] = None
        self._was_busy = False
        self._settings = None

        self._setup_window()
        self._build_ui()

        timer = QTimer(self)
        timer.timeout.connect(self._refresh_status)
        timer.start(100)

    # ── window setup ──────────────────────────────────────────────────────────

    def _setup_window(self):
        self.setWindowTitle('replayd')
        self.setFixedWidth(436)              # 416 + 10px margin each side
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint,
        )

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Outer layout adds a shadow/margin gap
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(0)

        self._card = QWidget(self)
        self._card.setObjectName('card')
        self._card.setStyleSheet(f'''
            QWidget#card {{
                background: {BG};
                border-radius: 18px;
                border: 1px solid rgba(255,255,255,0.055);
            }}
        ''')
        outer.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        card_lay.addWidget(self._build_header())
        card_lay.addWidget(self._build_arc_section())
        card_lay.addWidget(self._build_save_section())
        card_lay.addWidget(self._build_clips_section())

        # Settings popup is created lazily as a standalone top-level widget.

    # ── header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        hdr = _DragHandle()
        hdr.setObjectName('hdr')
        hdr.setFixedHeight(58)
        hdr.setStyleSheet('QWidget#hdr { border-bottom: 1px solid rgba(255,255,255,0.048); }')

        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(10)

        logo = QLabel('R')
        logo.setFixedSize(30, 30)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(f'''
            background: {ACC}; border-radius: 9px;
            color: #12100d; font-size: 17px; font-weight: 700;
        ''')
        lay.addWidget(logo)

        app_name = QLabel('replayd')
        app_name.setStyleSheet(f'color:{TX}; font-size:15px; font-weight:600;')
        lay.addWidget(app_name)
        lay.addStretch()

        self._pill = QLabel('● Starting…')
        self._pill.setFixedHeight(24)
        self._pill.setStyleSheet(self._pill_style('start'))
        lay.addWidget(self._pill)

        sett_btn = QPushButton()
        sett_btn.setFixedSize(32, 32)
        sett_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        # Gear icon via SVG unicode approximation
        sett_btn.setText('⚙')
        sett_btn.setStyleSheet(f'''
            QPushButton {{
                background: {S2}; border: none; border-radius: 9px;
                color: {TX2}; font-size: 14px;
            }}
            QPushButton:hover {{ background: {S3}; color: {TX}; }}
        ''')
        sett_btn.clicked.connect(self._open_settings)
        lay.addWidget(sett_btn)

        close_btn = QPushButton('✕')
        close_btn.setFixedSize(32, 32)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.setStyleSheet(f'''
            QPushButton {{
                background: {S2}; border: none; border-radius: 9px;
                color: {TX2}; font-size: 13px;
            }}
            QPushButton:hover {{ background: rgba(220,70,70,0.16); color: #e45a5a; }}
        ''')
        close_btn.clicked.connect(self._request_quit)
        lay.addWidget(close_btn)

        return hdr

    @staticmethod
    def _pill_style(mode: str) -> str:
        styles = {
            'rec':   (f'rgba(90,184,122,0.10)',  f'rgba(90,184,122,0.22)',  GRN),
            'sav':   (f'rgba(218,123,36,0.12)',  f'rgba(218,123,36,0.28)',  ACC),
            'start': (f'rgba(58,142,246,0.12)',  f'rgba(58,142,246,0.22)',  '#3a8ef6'),
        }
        bg, border, color = styles.get(mode, styles['start'])
        return f'''
            background: {bg}; border: 1px solid {border}; border-radius: 12px;
            color: {color}; font-size: 10px; font-weight: 600;
            padding: 0 10px; letter-spacing: 0.4px;
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
            self._arc_after = ArcWidget('POST HOTKEY', ACC)
            arc_row.addWidget(self._arc_before)
            arc_row.addWidget(self._arc_after)
            lay.addLayout(arc_row)
        else:
            self._arc_before = ArcWidget('SEC BUFFERED', GRN)
            self._arc_after = None
            lay.addWidget(self._arc_before, alignment=Qt.AlignmentFlag.AlignHCenter)

        lay.addWidget(self._build_meta_strip())
        return sec

    def _build_meta_strip(self) -> QWidget:
        strip = QWidget()
        strip.setFixedHeight(54)
        strip.setStyleSheet(f'''
            background: {S1}; border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.04);
        ''')
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        audio_txt = {'game': 'Game', 'mic': 'Mic', 'both': 'Both'}.get(
            self.cfg.get('audio_mode', 'both'), 'Both'
        )
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
                sep.setStyleSheet('background: rgba(255,255,255,0.04); border: none;')
                lay.addWidget(sep)

            cell = QWidget()
            cell.setStyleSheet('background:transparent;border:none;')
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(6, 0, 6, 0)
            cl.setSpacing(4)
            cl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            v = QLabel(val)
            v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.setStyleSheet(f'color:{TX}; font-size:20px; font-weight:700; background:transparent; border:none;')
            self._meta_vals[key] = v

            k = QLabel(key.upper())
            k.setAlignment(Qt.AlignmentFlag.AlignCenter)
            k.setStyleSheet(f'color:{TX3}; font-size:9px; font-weight:600; letter-spacing:1.5px; background:transparent; border:none;')

            cl.addWidget(v)
            cl.addWidget(k)
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

        hotkey_key = self.cfg.get('hotkey', 'KEY_F9').replace('KEY_', '')
        hint = QLabel(f'or press  <b>{hotkey_key}</b>  anywhere')
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setStyleSheet(f'color:{TX3}; font-size:11px; margin-bottom:8px; background:transparent;')
        lay.addWidget(hint)

        return sec

    @staticmethod
    def _save_btn_style(mode: str) -> str:
        if mode == 'busy':
            return f'''
                QPushButton {{
                    background: {S2}; color: {TX2}; border: none;
                    border-radius: 13px; font-size: 15px; font-weight: 700;
                }}
            '''
        return f'''
            QPushButton {{
                background: {ACC}; color: #12100d; border: none;
                border-radius: 13px; font-size: 15px; font-weight: 700;
                padding-bottom: 4px;
            }}
            QPushButton:hover  {{ background: {ACC_H}; }}
            QPushButton:pressed {{ background: {ACC_S}; padding-top: 4px; padding-bottom: 0px; }}
            QPushButton:disabled {{ background: {S2}; color: {TX2}; }}
        '''

    # ── clips section ─────────────────────────────────────────────────────────

    def _build_clips_section(self) -> QWidget:
        sec = QWidget()
        sec.setObjectName('clips')
        sec.setStyleSheet('QWidget#clips { border-top: 1px solid rgba(255,255,255,0.048); }')

        lay = QVBoxLayout(sec)
        lay.setContentsMargins(18, 13, 18, 18)
        lay.setSpacing(9)

        hdr_row = QHBoxLayout()
        title = QLabel('RECENT CLIPS')
        title.setStyleSheet(f'color:{TX3}; font-size:10px; font-weight:600; letter-spacing:1.8px; background:transparent;')
        open_btn = QPushButton('Open folder →')
        open_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        open_btn.setStyleSheet(f'''
            QPushButton {{ background: none; border: none; color: {TX3}; font-size: 11px; }}
            QPushButton:hover {{ color: {ACC}; }}
        ''')
        open_btn.clicked.connect(self._open_folder)
        hdr_row.addWidget(title)
        hdr_row.addStretch()
        hdr_row.addWidget(open_btn)
        lay.addLayout(hdr_row)

        self._clips_lay = QVBoxLayout()
        self._clips_lay.setSpacing(5)
        lay.addLayout(self._clips_lay)

        self._refresh_clips()
        return sec

    def _refresh_clips(self):
        while self._clips_lay.count():
            item = self._clips_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        out_dir = Path(self.cfg['output_dir']).expanduser()
        fmt = self.cfg.get('output_format', 'mp4')
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
                'just now'           if age < 60    else
                f'{int(age/60)} min ago'  if age < 3600  else
                f'{int(age/3600)} hr ago'
            )
            row = ClipRow(f, size_mb, elapsed)
            row.delete_clip_req.connect(self._delete_clip)
            row.open_clip_req.connect(self._open_clip)
            self._clips_lay.addWidget(row)

        if not files:
            empty = QLabel('No clips yet — press Save Clip to capture one.')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f'color:{TX3}; font-size:11px; padding:12px 0; background:transparent;')
            self._clips_lay.addWidget(empty)

    # ── public ────────────────────────────────────────────────────────────────

    def mark_ready(self):
        self._ready = True

    def set_quit_callback(self, callback):
        self._on_quit = callback

    def on_clip_saved(self, path: Path, _size_mb: float):
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
        # Prefer Qt URL handling for local files/folders; fallback to xdg-open.
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            subprocess.Popen(['xdg-open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _open_folder(self):
        folder = str(Path(self.cfg['output_dir']).expanduser())
        self._open_path(folder)

    def _open_specific_folder(self, path: str):
        self._open_path(path)

    def _open_clip(self, path: str):
        self._open_path(path)

    def _delete_clip(self, path: str):
        try:
            Path(path).unlink(missing_ok=True)
            print(f'[GUI] Deleted clip: {Path(path).name}')
        except OSError as e:
            print(f'[GUI] Could not delete clip: {e}')
        self._refresh_clips()

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
            self._save_btn.setEnabled(False)
            return

        before_max = max(int(self.cfg.get('seconds_before', 0)), 1)
        started_at = getattr(self.buf, 'recording_started_at', None)
        live_before_secs = 0.0 if started_at is None else min(time.time() - started_at, before_max)

        if self._capture_after_hotkey and self.clip._busy and not self._was_busy:
            # Freeze the pre-trigger gauge at the exact trigger moment.
            self._before_frozen_secs = live_before_secs
        elif not self.clip._busy or not self._capture_after_hotkey:
            self._before_frozen_secs = None

        before_secs = self._before_frozen_secs if self._before_frozen_secs is not None else live_before_secs
        self._arc_before.set_value(before_secs, before_max)

        if self._capture_after_hotkey and self._arc_after is not None:
            post_elapsed, post_target = self.clip.post_trigger_state()
            post_max = max(int(post_target), 1)
            self._arc_after.set_value(post_elapsed, post_max)

        if self.clip._busy:
            self._set_pill('sav', '● Saving')
            self._save_btn.setText('⏳  Saving…')
            self._save_btn.setStyleSheet(self._save_btn_style('busy'))
            self._save_btn.setEnabled(False)
        else:
            self._set_pill('rec', '● Recording')
            self._save_btn.setText('⏺  Save Clip')
            self._save_btn.setStyleSheet(self._save_btn_style('idle'))
            self._save_btn.setEnabled(True)

        self._was_busy = self.clip._busy

    # ── drag-to-move (frameless) ──────────────────────────────────────────────

    def closeEvent(self, e):
        if self._settings is not None and self._settings.isVisible():
            self._settings.hide()
        self._request_quit()
        e.ignore()

    # ── resize: keep settings overlay covering the card ───────────────────────

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._settings is not None and self._settings.isVisible():
            self._settings.open_over(self.frameGeometry())

    # ── paint: transparent window background (shadow effect) ─────────────────

    def paintEvent(self, _event):
        pass   # WA_TranslucentBackground handles it


# ── tray app (wrapper) ────────────────────────────────────────────────────────

class TrayApp:
    COLOR_RECORDING = '#22cc55'
    COLOR_SAVING    = '#f5a623'
    COLOR_STARTUP   = '#3a8ef6'

    def __init__(self, config: dict, clip_saver, buffer_manager, on_quit=None):
        self.cfg  = config
        self.clip = clip_saver
        self.buf  = buffer_manager
        self._on_quit = on_quit

        # Main window
        self.window = ReplaydWindow(config, clip_saver, buffer_manager)
        self.window.set_quit_callback(self._request_quit)
        self._position_window()
        self.window.show()

        # System tray
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(_tray_icon(self.COLOR_STARTUP))
        self.tray.setToolTip('replayd')

        menu = QMenu()

        save_act = menu.addAction(f'Save Clip  [{config.get("hotkey","KEY_F9")}]')
        save_act.triggered.connect(self.window._trigger_save)

        show_act = menu.addAction('Show / Hide Window')
        show_act.triggered.connect(self._toggle_window)

        menu.addSeparator()
        quit_act = menu.addAction('Quit')
        quit_act.triggered.connect(self._request_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_tray)
        self._timer.start(500)

    def _position_window(self):
        """Place the window in the bottom-right corner of the primary screen."""
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            w = self.window.sizeHint().width()
            h = self.window.sizeHint().height()
            self.window.move(geom.right() - w - 20, geom.bottom() - h - 20)

    # ── public ────────────────────────────────────────────────────────────────

    def mark_ready(self):
        self.window.mark_ready()

    # ── slots ─────────────────────────────────────────────────────────────────

    def _toggle_window(self):
        if self.window.isVisible():
            self.window.hide()
        else:
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()

    def _request_quit(self):
        if callable(self._on_quit):
            self._on_quit()
        else:
            QApplication.quit()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()

    def _refresh_tray(self):
        if self.clip._busy:
            self.tray.setIcon(_tray_icon(self.COLOR_SAVING))
            self.tray.setToolTip('replayd — Saving…')
        elif self.window._ready:
            started_at = getattr(self.buf, 'recording_started_at', None)
            secs = 0.0 if started_at is None else min(
                time.time() - started_at,
                self.cfg['seconds_before'],
            )
            self.tray.setIcon(_tray_icon(self.COLOR_RECORDING))
            self.tray.setToolTip(f'replayd - {secs:.1f}s buffered')

    # ── desktop notification ──────────────────────────────────────────────────

    @staticmethod
    def notify(title: str, body: str):
        try:
            subprocess.Popen(
                ['notify-send', '--app-name=replayd', '--icon=media-record',
                 '--expire-time=4000', title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass
