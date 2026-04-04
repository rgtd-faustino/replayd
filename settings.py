"""
settings.py – Settings overlay for replayd
Matches the dark amber design from replayd-design.html.

SettingsOverlay is a QWidget child of the main card — it covers it with a
semi-transparent scrim and slides a card in from the centre.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox,
    QLineEdit, QComboBox, QPushButton, QFileDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QRect
from PyQt6.QtGui import QPainter, QColor, QCursor, QBrush, QPen

CONFIG_PATH = Path(__file__).parent / 'config.json'

# ── colour palette (mirrored from gui.py to avoid circular import) ────────────
BG    = '#13110f'
S1    = '#1b1814'
S2    = '#232019'
S3    = '#2e2a23'
ACC   = '#da7b24'
ACC_H = '#e68f38'
TX    = '#ece7df'
TX2   = '#8a8078'
TX3   = '#4e4840'

# ── audio device helpers ──────────────────────────────────────────────────────

def _pa_sources() -> list[str]:
    try:
        r = subprocess.run(
            ['pactl', 'list', 'short', 'sources'],
            capture_output=True, text=True, timeout=5,
        )
        return [
            line.split()[1]
            for line in r.stdout.splitlines()
            if len(line.split()) >= 2
        ]
    except Exception:
        return []

def _list_monitors() -> list[str]:
    return [s for s in _pa_sources() if 'monitor'     in s.lower()]

def _list_mics()     -> list[str]:
    return [s for s in _pa_sources() if 'monitor' not in s.lower()]

# ── grab bar ──────────────────────────────────────────────────────────────────

class _GrabBar(QWidget):
    """
    Dedicated drag strip at the top of the settings card.

    Paints a row of 5 small rounded dots — universally understood as a
    drag handle. Cursor changes to SizeAllCursor on hover to reinforce the
    affordance. Uses startSystemMove() for Wayland + X11 compatibility.
    """

    _DOT_W   = 14
    _DOT_H   = 4
    _DOT_GAP = 5
    _N_DOTS  = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hovered = False
        self.setFixedHeight(22)
        self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        self.setMouseTracking(True)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        color = QColor(ACC)
        color.setAlpha(160 if self._hovered else 60)
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)

        total_w = self._N_DOTS * self._DOT_W + (self._N_DOTS - 1) * self._DOT_GAP
        x0 = (self.width() - total_w) // 2
        y0 = (self.height() - self._DOT_H) // 2

        for i in range(self._N_DOTS):
            x = x0 + i * (self._DOT_W + self._DOT_GAP)
            p.drawRoundedRect(x, y0, self._DOT_W, self._DOT_H,
                              self._DOT_H / 2, self._DOT_H / 2)
        p.end()

    def enterEvent(self, e):
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle:
                handle.startSystemMove()
        super().mousePressEvent(e)

# ── key capture widget ────────────────────────────────────────────────────────

class _KeyCapture(QWidget):
    """
    Click to enter listening mode → next keypress becomes the new hotkey.
    Displays the current binding as a badge; while listening shows 'Press a key…'.
    Escape cancels. Converts Qt key names to evdev KEY_* format.
    """

    IDLE      = 0
    LISTENING = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state   = self.IDLE
        self._key     = 'KEY_F9'
        self.setFixedSize(110, 32)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # ── public API ────────────────────────────────────────────────────────────

    def text(self) -> str:
        return self._key

    def setText(self, key: str):
        self._key = key
        self.update()

    # ── interaction ───────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._state = self.LISTENING
            self.setFocus()
            self.update()
        super().mousePressEvent(e)

    def focusOutEvent(self, e):
        self._state = self.IDLE
        self.update()
        super().focusOutEvent(e)

    def keyPressEvent(self, e):
        if self._state != self.LISTENING:
            return

        key = e.key()

        # Escape → cancel
        if key == Qt.Key.Key_Escape:
            self._state = self.IDLE
            self.update()
            return

        # Try to get the evdev name from Qt's enum
        try:
            qt_name  = Qt.Key(key).name          # e.g. "Key_F9", "Key_Home"
            evdev    = 'KEY_' + qt_name[4:].upper()   # → "KEY_F9", "KEY_HOME"
        except Exception:
            return   # unknown key — keep listening

        self._key   = evdev
        self._state = self.IDLE
        self.clearFocus()
        self.update()

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        listening = self._state == self.LISTENING

        # Background
        if listening:
            bg = QColor(ACC)
            bg.setAlpha(30)
        else:
            bg = QColor(S2)
        border = QColor(ACC) if listening else QColor(255, 255, 255, 20)

        p.setBrush(QBrush(bg))
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 8, 8)

        # Label
        if listening:
            label = 'Press a key…'
            color = QColor(ACC)
        else:
            label = self._key
            color = QColor(TX)

        p.setPen(QPen(color))
        font = p.font()
        font.setPointSize(9)
        font.setBold(not listening)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, label)
        p.end()

class SettingsRow(QWidget):
    """One settings row: label/sublabel on the left, control on the right."""

    def __init__(self, label: str, sub: str, control: QWidget, parent=None):
        super().__init__(parent)
        self.setFixedHeight(60)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(3)
        info.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel(label)
        lbl.setStyleSheet(f'color:{TX}; font-size:13px; font-weight:500; background:transparent; border:none;')
        info.addWidget(lbl)

        if sub:
            sub_lbl = QLabel(sub)
            sub_lbl.setStyleSheet(f'color:{TX3}; font-size:11px; background:transparent; border:none;')
            info.addWidget(sub_lbl)

        lay.addLayout(info, stretch=1)
        lay.addWidget(control)

        self.setStyleSheet('border-bottom: 1px solid rgba(255,255,255,0.04);')


# ── settings overlay ──────────────────────────────────────────────────────────

class SettingsOverlay(QWidget):
    """
    Semi-transparent overlay covering the main card.
    Emits `closed` when the user dismisses it (× button or click outside card).
    """

    closed = pyqtSignal()

    AUDIO_MODES = ['game', 'mic', 'both']   # index ↔ combo index (game=1, mic=2, both=0)
    COMBO_AUDIO = ['Game + Mic', 'Game only', 'Mic only']
    COMBO_AFTER = ['Enabled', 'Disabled']
    FORMATS     = ['mp4', 'mkv']

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = dict(config)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint,
        )
        self._build_ui()
        self._populate()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # The visible card – not in a layout so it can be centred manually
        self._card = QWidget(self)
        self._card.setFixedWidth(388)
        self._card.setStyleSheet(f'''
            QWidget {{
                background: {S1};
                border-radius: 18px;
                border: 1px solid rgba(255,255,255,0.07);
            }}
        ''')

        lay = QVBoxLayout(self._card)
        lay.setContentsMargins(22, 8, 22, 20)
        lay.setSpacing(0)

        # Grab bar – visually indicates draggable area
        grab = _GrabBar()
        grab.setStyleSheet('background:transparent; border:none;')
        lay.addWidget(grab)
        lay.addSpacing(6)

        # Header row – title + close button (not a drag zone)
        hdr_w = QWidget()
        hdr_w.setStyleSheet('background:transparent; border:none;')
        hdr = QHBoxLayout(hdr_w)
        hdr.setContentsMargins(0, 0, 0, 0)
        ttl = QLabel('Settings')
        ttl.setStyleSheet(f'color:{TX}; font-size:15px; font-weight:600; background:transparent; border:none;')
        x_btn = QPushButton('✕')
        x_btn.setFixedSize(28, 28)
        x_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        x_btn.setStyleSheet(f'''
            QPushButton {{
                background: {S2}; border: none; border-radius: 7px;
                color: {TX2}; font-size: 13px;
            }}
            QPushButton:hover {{ background: {S3}; color: {TX}; }}
        ''')
        x_btn.clicked.connect(self.closed.emit)
        hdr.addWidget(ttl)
        hdr.addStretch()
        hdr.addWidget(x_btn)
        lay.addWidget(hdr_w)
        lay.addSpacing(14)

        # ── Rows ──────────────────────────────────────────────────────────

        # Buffer before
        self.sb_before = self._make_spinbox(5, 600)
        lay.addWidget(SettingsRow('Buffer before', 'Seconds captured before trigger', self._spinbox_with_unit(self.sb_before, 'sec')))

        # Buffer after
        self.sb_after = self._make_spinbox(1, 120)
        lay.addWidget(SettingsRow('Buffer after', 'Extra seconds after trigger', self._spinbox_with_unit(self.sb_after, 'sec')))

        # Capture after hotkey
        self.cb_after = QComboBox()
        self.cb_after.addItems(self.COMBO_AFTER)
        self.cb_after.setStyleSheet(self._select_css())
        self.cb_after.currentIndexChanged.connect(self._update_after_controls)
        lay.addWidget(SettingsRow('After hotkey capture', 'Record extra time after trigger', self.cb_after))

        # Hotkey
        self.le_hotkey = _KeyCapture()
        lay.addWidget(SettingsRow('Hotkey', 'Click to rebind', self.le_hotkey))

        # Audio mode
        self.cb_audio = QComboBox()
        self.cb_audio.addItems(self.COMBO_AUDIO)
        self.cb_audio.setStyleSheet(self._select_css())
        lay.addWidget(SettingsRow('Audio capture', 'Sources to record', self.cb_audio))

        # Game source (shown when audio ≠ mic-only)
        self.cb_game_src = self._make_source_combo(_list_monitors)
        self._row_game = SettingsRow('Game source', 'PulseAudio monitor device', self.cb_game_src)
        lay.addWidget(self._row_game)

        # Mic source (shown when audio ≠ game-only)
        self.cb_mic_src = self._make_source_combo(_list_mics)
        self._row_mic = SettingsRow('Mic source', 'PulseAudio input device', self.cb_mic_src)
        lay.addWidget(self._row_mic)

        self.cb_audio.currentIndexChanged.connect(self._update_source_rows)

        # Output format
        self.cb_format = QComboBox()
        self.cb_format.addItems(['MP4', 'MKV'])
        self.cb_format.setStyleSheet(self._select_css())
        lay.addWidget(SettingsRow('Output format', 'Video container', self.cb_format))

        # Output folder
        dir_w = self._make_folder_row()
        lay.addWidget(SettingsRow('Output folder', '', dir_w))

        lay.addSpacing(16)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)

        self.discard_btn = QPushButton('Discard')
        self.discard_btn.setFixedHeight(44)
        self.discard_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.discard_btn.setStyleSheet(f'''
            QPushButton {{
                background: {S2}; border: 1px solid rgba(255,255,255,0.08); border-radius: 10px;
                color: {TX2}; font-size: 13px; font-weight: 700;
                padding: 0 14px;
            }}
            QPushButton:hover  {{ background: {S3}; color: {TX}; }}
            QPushButton:pressed {{ background: #1e1b16; }}
        ''')
        self.discard_btn.clicked.connect(self.closed.emit)

        self.apply_btn = QPushButton('Confirm && Restart')
        self.apply_btn.setFixedHeight(44)
        self.apply_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.apply_btn.setStyleSheet(f'''
            QPushButton {{
                background: {ACC}; border: none; border-radius: 10px;
                color: #12100d; font-size: 14px; font-weight: 700;
            }}
            QPushButton:hover  {{ background: {ACC_H}; }}
            QPushButton:pressed {{ background: #c0691a; }}
        ''')
        self.apply_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self.discard_btn)
        btn_row.addWidget(self.apply_btn)
        lay.addLayout(btn_row)

    # ── widget factories ──────────────────────────────────────────────────────

    @staticmethod
    def _make_spinbox(lo: int, hi: int) -> QSpinBox:
        sb = QSpinBox()
        sb.setRange(lo, hi)
        sb.setFixedWidth(70)
        sb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return sb

    @staticmethod
    def _spinbox_with_unit(sb: QSpinBox, unit: str) -> QWidget:
        w = QWidget()
        w.setStyleSheet('background:transparent;border:none;')
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        sb.setStyleSheet(f'''
            QSpinBox {{
                background: {S2}; border: 1px solid rgba(255,255,255,0.08);
                border-radius: 8px; color: {TX}; font-size: 14px; font-weight: 600;
                padding: 4px 6px; text-align: center;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background: {S3}; border: none; width: 16px;
            }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: #3a352d; }}
        ''')
        unit_lbl = QLabel(unit)
        unit_lbl.setStyleSheet(f'color:{TX3}; font-size:12px; background:transparent; border:none;')
        h.addWidget(sb)
        h.addWidget(unit_lbl)
        return w

    def _make_source_combo(self, list_fn) -> QWidget:
        w = QWidget()
        w.setStyleSheet('background:transparent;border:none;')
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        cb = QComboBox()
        cb.setFixedWidth(180)
        cb.setStyleSheet(self._select_css(width=180))
        cb.addItem('auto')
        cb.addItems(list_fn())

        refresh = QPushButton('↻')
        refresh.setFixedSize(28, 28)
        refresh.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        refresh.setToolTip('Refresh device list')
        refresh.setStyleSheet(f'''
            QPushButton {{
                background: {S2}; border: 1px solid rgba(255,255,255,0.08);
                border-radius: 7px; color: {TX2}; font-size: 14px;
            }}
            QPushButton:hover {{ background: {S3}; color: {TX}; }}
        ''')
        # Store combo ref on widget so we can grab it later
        w._combo = cb
        refresh.clicked.connect(lambda: self._refresh_combo(cb, list_fn))

        h.addWidget(cb)
        h.addWidget(refresh)
        return w

    @staticmethod
    def _refresh_combo(cb: QComboBox, list_fn):
        current = cb.currentText()
        cb.blockSignals(True)
        cb.clear()
        cb.addItem('auto')
        cb.addItems(list_fn())
        idx = cb.findText(current)
        cb.setCurrentIndex(idx if idx >= 0 else 0)
        cb.blockSignals(False)

    def _make_folder_row(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet('background:transparent;border:none;')
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        self.le_outdir = QLineEdit()
        self.le_outdir.setFixedWidth(160)
        self.le_outdir.setStyleSheet(self._input_css())

        browse = QPushButton('Browse…')
        browse.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        browse.setStyleSheet(f'''
            QPushButton {{
                background: {S2}; border: 1px solid rgba(255,255,255,0.08);
                border-radius: 8px; color: {TX2}; font-size: 12px; padding: 4px 10px;
            }}
            QPushButton:hover {{ background: {S3}; color: {TX}; }}
        ''')
        browse.clicked.connect(self._browse_dir)
        h.addWidget(self.le_outdir)
        h.addWidget(browse)
        return w

    @staticmethod
    def _input_css() -> str:
        return f'''
            QLineEdit {{
                background: {S2}; border: 1px solid rgba(255,255,255,0.08);
                border-radius: 8px; color: {TX}; font-size: 13px; font-weight: 600;
                padding: 5px 10px;
            }}
            QLineEdit:focus {{ border-color: {ACC}; }}
        '''

    @staticmethod
    def _select_css(width: int = 130) -> str:
        return f'''
            QComboBox {{
                background: {S2}; border: 1px solid rgba(255,255,255,0.08);
                border-radius: 8px; color: {TX}; font-size: 13px; font-weight: 500;
                padding: 5px 10px; min-width: {width}px;
            }}
            QComboBox:focus {{ border-color: {ACC}; }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox QAbstractItemView {{
                background: {S2}; color: {TX};
                border: 1px solid rgba(255,255,255,0.08); border-radius: 8px;
                selection-background-color: {S3};
            }}
        '''

    # ── populate ──────────────────────────────────────────────────────────────

    def _populate(self):
        self.sb_before.setValue(self.config.get('seconds_before', 30))
        self.sb_after.setValue(self.config.get('seconds_after', 10))
        self.cb_after.setCurrentIndex(0 if self.config.get('capture_after_hotkey', True) else 1)
        self.le_hotkey.setText(self.config.get('hotkey', 'KEY_F9'))
        self.le_outdir.setText(self.config.get('output_dir', '~/Videos/Replayd'))

        mode = self.config.get('audio_mode', 'both')
        mode_idx = {'both': 0, 'game': 1, 'mic': 2}.get(mode, 0)
        self.cb_audio.setCurrentIndex(mode_idx)

        fmt = self.config.get('output_format', 'mp4').upper()
        self.cb_format.setCurrentIndex(0 if fmt == 'MP4' else 1)

        self._set_combo_text(self.cb_game_src._combo, self.config.get('audio_source', 'auto'))
        self._set_combo_text(self.cb_mic_src._combo,  self.config.get('mic_source',   'auto'))
        self._update_after_controls()
        self._update_source_rows()

    def _update_after_controls(self):
        self.sb_after.setEnabled(self.cb_after.currentIndex() == 0)

    def set_config(self, config: dict):
        self.config = dict(config)
        self._populate()

    def open_over(self, target_geom: QRect):
        """Open as a centered popup over the main window geometry."""
        self._card.adjustSize()
        pad = 20
        ow = self._card.width() + pad * 2
        oh = self._card.height() + pad * 2
        self.resize(ow, oh)
        x = target_geom.center().x() - ow // 2
        y = target_geom.center().y() - oh // 2

        screen = QApplication.screenAt(target_geom.center()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - ow + 1))
            y = max(area.top(), min(y, area.bottom() - oh + 1))

        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _set_combo_text(cb: QComboBox, text: str):
        idx = cb.findText(text)
        if idx >= 0:
            cb.setCurrentIndex(idx)
        else:
            cb.insertItem(1, text)   # insert after 'auto', select it
            cb.setCurrentIndex(1)

    def _update_source_rows(self):
        idx = self.cb_audio.currentIndex()   # 0=both, 1=game, 2=mic
        self._row_game.setVisible(idx in (0, 1))
        self._row_mic.setVisible(idx in (0, 2))
        # Recompute card height
        QTimer.singleShot(0, self._reposition_card)

    # ── save ──────────────────────────────────────────────────────────────────

    def _browse_dir(self):
        start  = str(Path(self.le_outdir.text()).expanduser())
        folder = QFileDialog.getExistingDirectory(self, 'Choose output folder', start)
        if folder:
            self.le_outdir.setText(folder)

    def _on_save(self):
        hotkey = self.le_hotkey.text().strip()
        if not hotkey.upper().startswith('KEY_'):
            hotkey = 'KEY_' + hotkey.upper()

        mode_map = {0: 'both', 1: 'game', 2: 'mic'}
        self.config.update({
            'seconds_before': self.sb_before.value(),
            'seconds_after':  self.sb_after.value(),
            'capture_after_hotkey': self.cb_after.currentIndex() == 0,
            'hotkey':         hotkey,
            'audio_mode':     mode_map[self.cb_audio.currentIndex()],
            'audio_source':   self.cb_game_src._combo.currentText(),
            'mic_source':     self.cb_mic_src._combo.currentText(),
            'output_format':  self.cb_format.currentText().lower(),
            'output_dir':     self.le_outdir.text(),
        })

        try:
            CONFIG_PATH.write_text(json.dumps(self.config, indent=2))
        except OSError as e:
            print(f'[Settings] Error saving config: {e}')
            return

        print('[Settings] Config saved — restarting…')
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── geometry ──────────────────────────────────────────────────────────────

    def _reposition_card(self):
        """Centre the card over the overlay."""
        self._card.adjustSize()
        cw = self._card.width()
        ch = self._card.height()
        x  = max((self.width()  - cw) // 2, 0)
        y  = max((self.height() - ch) // 2, 0)
        self._card.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reposition_card()

    def showEvent(self, e):
        super().showEvent(e)
        self._reposition_card()

    # ── paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))
        p.end()

    def mousePressEvent(self, e):
        """Click outside the card → close overlay."""
        if not self._card.geometry().contains(e.pos()):
            self.closed.emit()
        else:
            super().mousePressEvent(e)
