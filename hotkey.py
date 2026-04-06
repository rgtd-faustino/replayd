"""
hotkey.py – Global shortcut listener via xdg-desktop-portal GlobalShortcuts.

Works inside Flatpak sandboxes (and plain desktop sessions) on KDE Plasma and
GNOME with xdg-desktop-portal >= 1.18.  Unlike the old evdev approach, this
never touches /dev/input and requires no 'input' group membership.

Flow
----
1.  CreateSession  -> get a GlobalShortcuts session handle.
2.  BindShortcuts  -> register the 'save-clip' shortcut with a preferred trigger
                     hint derived from the config key (e.g. KEY_F5 -> "F5").
                     The compositor may show a rebind dialog on first run.
3.  Subscribe to the Activated signal on the session object path.
4.  Fire self.callback() whenever 'save-clip' is activated.

Degradation
-----------
If the portal is unavailable (old xdg-desktop-portal, unsupported compositor,
or D-Bus error), the listener logs a warning and parks indefinitely without
crashing.  The Save Clip button in the GUI still works in that case.
"""

import asyncio
import time
from typing import Callable, Coroutine, Optional

from dbus_next.aio import MessageBus
from dbus_next.message import Message
from dbus_next import BusType, Variant


_SHORTCUT_ID = 'save-clip'

_EVDEV_TO_TRIGGER: dict[str, str] = {
    'PAGEUP':    'PgUp',     'PAGEDOWN':   'PgDown',
    'HOME':      'Home',     'END':        'End',
    'INSERT':    'Ins',      'DELETE':     'Del',
    'BACKSPACE': 'Backspace','ENTER':      'Return',
    'ESCAPE':    'Escape',   'TAB':        'Tab',
    'SPACE':     'Space',    'PRINT':      'Print',
    'SCROLLLOCK':'ScrollLock','PAUSE':     'Pause',
    'NUMLOCK':   'NumLock',
    'LEFTCTRL':  'Ctrl',     'RIGHTCTRL':  'Ctrl',
    'LEFTSHIFT': 'Shift',    'RIGHTSHIFT': 'Shift',
    'LEFTALT':   'Alt',      'RIGHTALT':   'Alt',
    'LEFTMETA':  'Meta',     'RIGHTMETA':  'Meta',
}


def _evdev_to_trigger(key_name: str) -> str:
    """Convert 'KEY_F5' -> 'F5', 'KEY_HOME' -> 'Home', etc."""
    name = key_name.upper()
    if name.startswith('KEY_'):
        name = name[4:]
    if name in _EVDEV_TO_TRIGGER:
        return _EVDEV_TO_TRIGGER[name]
    return name


class HotkeyListener:
    BUS_NAME    = 'org.freedesktop.portal.Desktop'
    OBJECT_PATH = '/org/freedesktop/portal/desktop'
    GS_IFACE    = 'org.freedesktop.portal.GlobalShortcuts'

    def __init__(self, config: dict):
        self.key_name = config.get('hotkey', 'KEY_F9')
        self.callback: Optional[Callable[[], Coroutine]] = None
        self._bus:          Optional[MessageBus] = None
        self._session_path: Optional[str]        = None
        self._sender:       Optional[str]        = None

    def _token(self, prefix: str = 'tok') -> str:
        return f'{prefix}_{str(int(time.time() * 1_000_000))[-8:]}'

    def _request_path(self, token: str) -> str:
        return f'/org/freedesktop/portal/desktop/request/{self._sender}/{token}'

    async def _wait_response(self, request_path: str, timeout: float = 60.0) -> dict:
        loop   = asyncio.get_event_loop()
        future = loop.create_future()

        await self._bus.call(Message(
            destination = 'org.freedesktop.DBus',
            path        = '/org/freedesktop/DBus',
            interface   = 'org.freedesktop.DBus',
            member      = 'AddMatch',
            signature   = 's',
            body        = [
                f"type='signal',"
                f"path='{request_path}',"
                f"interface='org.freedesktop.portal.Request',"
                f"member='Response'"
            ],
        ))

        def _handler(msg):
            if (
                msg.message_type.name == 'SIGNAL'
                and msg.path   == request_path
                and msg.member == 'Response'
                and not future.done()
            ):
                future.set_result(msg.body)

        self._bus.add_message_handler(_handler)
        try:
            body = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._bus.remove_message_handler(_handler)

        code    = body[0]
        results = body[1] if len(body) > 1 else {}
        if code != 0:
            raise RuntimeError(
                f'[Hotkey] Portal request failed with code {code}. '
                'The compositor may not support GlobalShortcuts.'
            )
        return results

    async def start(self):
        """Connect to the GlobalShortcuts portal and listen for activations.
        Never raises -- degrades gracefully if the portal is unavailable.
        """
        try:
            await self._start_portal()
        except Exception as exc:
            print(f'[Hotkey] GlobalShortcuts portal unavailable: {exc}')
            print('[Hotkey] Running without a global hotkey -- '
                  'use the Save Clip button in the UI.')
            await asyncio.Event().wait()

    async def _start_portal(self):
        self._bus    = await MessageBus(bus_type=BusType.SESSION).connect()
        self._sender = self._bus.unique_name.lstrip(':').replace('.', '_')
        print(f'[Hotkey] D-Bus connected as {self._bus.unique_name}')

        # 1. CreateSession
        t_req = self._token('req')
        t_ses = self._token('ses')
        req1  = self._request_path(t_req)

        wait1 = asyncio.create_task(self._wait_response(req1, timeout=30))
        await asyncio.sleep(0.05)

        reply = await self._bus.call(Message(
            destination = self.BUS_NAME,
            path        = self.OBJECT_PATH,
            interface   = self.GS_IFACE,
            member      = 'CreateSession',
            signature   = 'a{sv}',
            body        = [{
                'handle_token':         Variant('s', t_req),
                'session_handle_token': Variant('s', t_ses),
            }],
        ))
        if reply.message_type.name == 'ERROR':
            raise RuntimeError(f'CreateSession: {reply.body}')

        r1 = await wait1
        session_handle = r1.get('session_handle')
        if session_handle is not None:
            self._session_path = (
                session_handle.value
                if hasattr(session_handle, 'value')
                else str(session_handle)
            )
        else:
            self._session_path = (
                f'/org/freedesktop/portal/desktop/session/{self._sender}/{t_ses}'
            )
        print(f'[Hotkey] Session: {self._session_path}')

        # 2. BindShortcuts
        preferred = _evdev_to_trigger(self.key_name)
        t_req2    = self._token('req')
        req2      = self._request_path(t_req2)

        wait2 = asyncio.create_task(self._wait_response(req2, timeout=60))
        await asyncio.sleep(0.05)

        reply2 = await self._bus.call(Message(
            destination = self.BUS_NAME,
            path        = self.OBJECT_PATH,
            interface   = self.GS_IFACE,
            member      = 'BindShortcuts',
            signature   = 'oa(sa{sv})sa{sv}',
            body        = [
                self._session_path,
                [
                    [
                        _SHORTCUT_ID,
                        {
                            'description':       Variant('s', 'Save instant replay clip'),
                            'preferred-trigger': Variant('s', preferred),
                        },
                    ]
                ],
                '',
                {'handle_token': Variant('s', t_req2)},
            ],
        ))
        if reply2.message_type.name == 'ERROR':
            raise RuntimeError(f'BindShortcuts: {reply2.body}')

        await wait2
        print(f'[Hotkey] Shortcut "{_SHORTCUT_ID}" registered '
              f'(preferred trigger: {preferred}). '
              'The compositor may prompt the user to bind a key on first run.')

        # 3. Subscribe to Activated signals on the session
        await self._bus.call(Message(
            destination = 'org.freedesktop.DBus',
            path        = '/org/freedesktop/DBus',
            interface   = 'org.freedesktop.DBus',
            member      = 'AddMatch',
            signature   = 's',
            body        = [
                f"type='signal',"
                f"sender='{self.BUS_NAME}',"
                f"path='{self._session_path}',"
                f"interface='{self.GS_IFACE}',"
                f"member='Activated'"
            ],
        ))

        def _on_signal(msg):
            if (
                msg.message_type.name == 'SIGNAL'
                and msg.path      == self._session_path
                and msg.interface == self.GS_IFACE
                and msg.member    == 'Activated'
            ):
                # body: (session_handle, shortcut_id, timestamp, options)
                shortcut_id = msg.body[1] if len(msg.body) > 1 else ''
                if shortcut_id == _SHORTCUT_ID:
                    print(f'[Hotkey] "{_SHORTCUT_ID}" activated!')
                    if self.callback is not None:
                        asyncio.ensure_future(self.callback())

        self._bus.add_message_handler(_on_signal)
        print('[Hotkey] Listening for shortcut activations...')

        await asyncio.Event().wait()