"""
paths.py – XDG-compliant path constants for replayd.

In a Flatpak sandbox the env vars are set by the runtime:
  XDG_CONFIG_HOME  → ~/.var/app/io.github.rgtd-faustino.replayd/config
  XDG_RUNTIME_DIR  → /run/user/<uid>/app/io.github.rgtd-faustino.replayd

Outside a sandbox (dev / plain pip install) they fall back to
  ~/.config/replayd  and  /tmp/replayd  respectively.

All directories are created on first access.
"""

import os
from pathlib import Path

_APP = 'replayd'


def _config_base() -> Path:
    base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    d = base / _APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def _runtime_base() -> Path:
    base = os.environ.get('XDG_RUNTIME_DIR', '/tmp')
    d = Path(base) / _APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file() -> Path:
    """~/.config/replayd/config.json (or XDG equivalent inside Flatpak)."""
    return _config_base() / 'config.json'


def restore_token_file() -> Path:
    """Persisted portal restore token so the screen picker only appears once."""
    return _config_base() / '.portal_restore_token'


def buffer_dir() -> Path:
    """Temporary directory for rolling GStreamer segments.

    Uses XDG_RUNTIME_DIR which is memory-backed on most systems and is cleaned
    up automatically on logout — better than /tmp for short-lived binary data.
    """
    d = _runtime_base() / 'buffer'
    d.mkdir(parents=True, exist_ok=True)
    return d


def thumb_dir() -> Path:
    """Thumbnail cache directory."""
    d = _runtime_base() / 'thumbs'
    d.mkdir(parents=True, exist_ok=True)
    return d


# Eagerly-resolved constants so modules can do `from paths import THUMB_DIR`
# without calling a function.  These are computed once at import time.
THUMB_DIR = thumb_dir()
