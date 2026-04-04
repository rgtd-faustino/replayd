"""
hotkey.py – Global hotkey listener via evdev
Works on both X11 and Wayland (reads directly from /dev/input).
Requires the user to be in the 'input' group:
    sudo usermod -a -G input $USER  (then logout/login)
"""

import asyncio
from typing import Callable, Coroutine, Optional

import evdev
from evdev import ecodes


class HotkeyListener:
    def __init__(self, config: dict):
        key_name = config.get('hotkey', 'KEY_F9')
        self.key_name = key_name
        self.key_code = ecodes.ecodes.get(key_name)
        if self.key_code is None:
            raise ValueError(
                f'Unknown key name: "{key_name}". '
                'Find yours with: python3 -c "import evdev; '
                'print(list(evdev.ecodes.ecodes.keys())[:40])"'
            )
        self.callback: Optional[Callable[[], Coroutine]] = None
        self._devices: list[evdev.InputDevice] = []

    # ── device discovery ─────────────────────────────────────────────────────

    def _find_devices(self) -> list[evdev.InputDevice]:
        """Return all input devices that have our hotkey."""
        found = []
        for path in evdev.list_devices():
            try:
                dev  = evdev.InputDevice(path)
                caps = dev.capabilities()
                if ecodes.EV_KEY in caps and self.key_code in caps[ecodes.EV_KEY]:
                    found.append(dev)
            except (PermissionError, OSError):
                continue
        return found

    # ── reading ──────────────────────────────────────────────────────────────

    async def _read_device(self, device: evdev.InputDevice):
        print(f'[Hotkey] Watching: {device.name} ({device.path})')
        try:
            async for event in device.async_read_loop():
                if (
                    event.type  == ecodes.EV_KEY and
                    event.code  == self.key_code  and
                    event.value == 1              # 1 = key-down, 2 = repeat
                ):
                    print(f'[Hotkey] {self.key_name} pressed!')
                    if self.callback is not None:
                        asyncio.ensure_future(self.callback())
        except OSError as e:
            print(f'[Hotkey] Lost device {device.path}: {e}')

    # ── public ───────────────────────────────────────────────────────────────

    async def start(self):
        self._devices = self._find_devices()
        if not self._devices:
            raise PermissionError(
                f'No device found with key {self.key_name}.\n'
                '  Fix: sudo usermod -a -G input $USER  then logout and back in.\n'
                '  List devices: python3 -m evdev.evtest'
            )
        await asyncio.gather(*[self._read_device(d) for d in self._devices])
