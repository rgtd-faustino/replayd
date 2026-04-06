"""
portal.py – Wayland ScreenCast via xdg-desktop-portal (dbus-next, raw messages)

Uses raw Message calls to avoid dbus-next's introspection bug with
hyphenated property names (e.g. power-saver-enabled) present in
KDE Plasma's portal introspection XML.
"""

import asyncio
import time
from pathlib import Path

from dbus_next.aio import MessageBus
from dbus_next.message import Message
from dbus_next import BusType, Variant

from paths import restore_token_file as _restore_token_file


class WaylandPortal:
    BUS_NAME    = 'org.freedesktop.portal.Desktop'
    OBJECT_PATH = '/org/freedesktop/portal/desktop'
    SC_IFACE    = 'org.freedesktop.portal.ScreenCast'

    def __init__(self):
        self.bus          = None
        self.session_path = None
        self._sender      = None   # e.g. "1_178"
        self.source_label = 'Desktop'

    def _token(self, prefix: str) -> str:
        ts = str(int(time.time() * 1_000_000))[-8:]
        return f'{prefix}_{ts}'

    def _request_path(self, token: str) -> str:
        return f'/org/freedesktop/portal/desktop/request/{self._sender}/{token}'

    # ── connect ──────────────────────────────────────────────────────────────

    async def setup(self):
        self.bus     = await MessageBus(bus_type=BusType.SESSION).connect()
        self._sender = self.bus.unique_name.lstrip(':').replace('.', '_')
        print(f'[Portal] D-Bus connected as {self.bus.unique_name}')

    # ── raw portal call ──────────────────────────────────────────────────────

    async def _call(self, member: str, signature: str, body: list) -> str:
        """Call a ScreenCast portal method; returns the request object path."""
        reply = await self.bus.call(Message(
            destination = self.BUS_NAME,
            path        = self.OBJECT_PATH,
            interface   = self.SC_IFACE,
            member      = member,
            signature   = signature,
            body        = body,
        ))
        if reply.message_type.name == 'ERROR':
            raise RuntimeError(f'[Portal] {member} error: {reply.body}')
        request_path = reply.body[0]
        print(f'[Portal] {member} → {request_path}')
        return request_path

    # ── signal waiter ────────────────────────────────────────────────────────

    async def _wait_response(self, request_path: str, timeout: float = 120) -> dict:
        """Wait for the Response signal on request_path."""
        loop   = asyncio.get_event_loop()
        future = loop.create_future()

        # Subscribe to the signal
        await self.bus.call(Message(
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

        def handler(msg):
            if (
                msg.message_type.name == 'SIGNAL'
                and msg.path          == request_path
                and msg.member        == 'Response'
                and not future.done()
            ):
                future.set_result(msg.body)

        self.bus.add_message_handler(handler)
        try:
            body = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.bus.remove_message_handler(handler)

        code    = body[0]
        results = body[1] if len(body) > 1 else {}

        if code != 0:
            raise RuntimeError(
                f'Portal returned error code {code} '
                '(cancelled screen picker?)'
            )
        return results

    # ── main flow ────────────────────────────────────────────────────────────

    async def get_node_id(self, screen_source: str = 'desktop') -> int:
        if self.bus is None:
            await self.setup()

        # 1. CreateSession
        t_req = self._token('req')
        t_ses = self._token('ses')
        req1  = self._request_path(t_req)

        wait1 = asyncio.create_task(self._wait_response(req1))
        await asyncio.sleep(0.1)   # let the match rule register

        await self._call(
            'CreateSession',
            'a{sv}',
            [{
                'handle_token':         Variant('s', t_req),
                'session_handle_token': Variant('s', t_ses),
            }],
        )

        r1 = await wait1
        session_handle = r1.get('session_handle')
        # dbus-next returns dict values as Variant objects — unwrap them
        if session_handle is not None:
            self.session_path = (
                session_handle.value
                if hasattr(session_handle, 'value')
                else str(session_handle)
            )
        else:
            # Reconstruct path from token (some backends omit it from results)
            self.session_path = (
                f'/org/freedesktop/portal/desktop/session/{self._sender}/{t_ses}'
            )
        print(f'[Portal] Session: {self.session_path}')

        # 2. SelectSources
        t_req2 = self._token('req')
        req2   = self._request_path(t_req2)

        wait2 = asyncio.create_task(self._wait_response(req2))
        await asyncio.sleep(0.1)

        select_opts: dict = {
            'handle_token': Variant('s', t_req2),
            'multiple':     Variant('b', False),
            'cursor_mode':  Variant('u', 2),      # EMBEDDED
        }

        if screen_source == 'custom':
            # Let user pick any monitor or window; always show picker
            select_opts['types']        = Variant('u', 3)        # MONITOR | WINDOW
            select_opts['persist_mode'] = Variant('u', 2)        # persist permanently
            _restore_token_file().unlink(missing_ok=True)                  # clear saved token
            print('[Portal] Custom source mode — screen/window picker will appear.')
        else:
            # Desktop mode: monitor only + restore_token so picker only appears once
            select_opts['types']        = Variant('u', 1)  # MONITOR only
            select_opts['persist_mode'] = Variant('u', 2)  # persist permanently

            restore_token: str | None = None
            if _restore_token_file().exists():
                try:
                    restore_token = _restore_token_file().read_text().strip() or None
                except OSError:
                    restore_token = None

            if restore_token:
                select_opts['restore_token'] = Variant('s', restore_token)
                print('[Portal] Desktop mode — restore token found, skipping picker.')
            else:
                print('[Portal] Desktop mode — first run, picker appears once to select your monitor.')

        await self._call('SelectSources', 'oa{sv}', [self.session_path, select_opts])
        await wait2
        print('[Portal] Sources selected.')

        # 3. Start → shows KDE screen picker
        t_req3 = self._token('req')
        req3   = self._request_path(t_req3)

        wait3 = asyncio.create_task(self._wait_response(req3, timeout=120))
        await asyncio.sleep(0.1)

        print('[Portal] Screen-picker dialog should appear — select your monitor.')
        await self._call(
            'Start',
            'osa{sv}',
            [
                self.session_path,
                '',   # parent_window
                {
                    'handle_token': Variant('s', t_req3),
                },
            ],
        )

        r3 = await wait3

        # Persist restore_token for all modes — on the next launch this skips the picker.
        raw_token = r3.get('restore_token')
        if raw_token is not None:
            token_str = raw_token.value if hasattr(raw_token, 'value') else str(raw_token)
            if token_str:
                try:
                    _restore_token_file().write_text(token_str)
                    print('[Portal] Restore token saved — picker will be skipped on next launch.')
                except OSError as exc:
                    print(f'[Portal] Warning: could not save restore token: {exc}')
        else:
            print('[Portal] Note: portal did not return a restore_token '
                  '(xdg-desktop-portal < 1.17 — picker will appear each launch).')

        streams = r3.get('streams')
        if not streams:
            raise RuntimeError('No streams in Start response.')

        # dbus-next wraps values in Variant — unwrap recursively
        if hasattr(streams, 'value'):
            streams = streams.value

        first = streams[0]
        if hasattr(first, 'value'):
            first = first.value

        node_id = int(first[0].value if hasattr(first[0], 'value') else first[0])
        print(f'[Portal] PipeWire node ID: {node_id}')

        # Try to build a human-readable label for the selected source.
        try:
            props = first[1].value if hasattr(first[1], 'value') else first[1]
            if isinstance(props, dict):
                size_v = props.get('size')
                type_v = props.get('source-type')
                if size_v is not None:
                    size   = size_v.value if hasattr(size_v, 'value') else size_v
                    w, h   = int(size[0]), int(size[1])
                    t_val  = (type_v.value if hasattr(type_v, 'value') else type_v) if type_v else 1
                    kind   = 'Window' if t_val == 2 else 'Monitor'
                    self.source_label = f'{kind} ({w}\u00d7{h})'
                else:
                    self.source_label = 'Desktop'
        except Exception:
            self.source_label = 'Desktop'
        print(f'[Portal] Source label: {self.source_label}')

        return node_id

    async def close(self):
        if self.session_path and self.bus:
            try:
                await self.bus.call(Message(
                    destination = self.BUS_NAME,
                    path        = self.session_path,
                    interface   = 'org.freedesktop.portal.Session',
                    member      = 'Close',
                    signature   = '',
                    body        = [],
                ))
            except Exception:
                pass
        if self.bus:
            self.bus.disconnect()