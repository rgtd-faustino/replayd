"""
main.py – replayd (Wayland instant replay)
Usage:  python3 main.py
Press the configured hotkey (or click the tray icon / Save Clip button) to save a clip.
"""

import asyncio
import json
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication
import qasync

from portal import WaylandPortal
from buffer import BufferManager
from hotkey import HotkeyListener
from clip   import ClipSaver
from gui    import TrayApp


def load_config() -> dict:
    path = Path(__file__).parent / 'config.json'
    with open(path) as f:
        return json.load(f)


async def main():
    config = load_config()

    print('╔══════════════════════════════════╗')
    print('║            replayd               ║')
    print('╠══════════════════════════════════╣')
    print(f'║  Before : {config["seconds_before"]:>3}s                    ║')
    print(f'║  After  : {config["seconds_after"]:>3}s                    ║')
    print(f'║  Hotkey : {config["hotkey"]:<22}  ║')
    print(f'║  Output : {str(Path(config["output_dir"]).expanduser())[:21]:<21}  ║')
    print('╚══════════════════════════════════╝')
    print()

    # 1. Wayland portal → PipeWire node ID
    portal = WaylandPortal()
    await portal.setup()
    node_id = await portal.get_node_id()

    # 2. Core components
    buffer = BufferManager(config, node_id)
    clip   = ClipSaver(config, buffer)
    hotkey = HotkeyListener(config)
    shutdown_event = asyncio.Event()

    # 3. Tray + window (QApplication must already exist — created in __main__)
    def request_quit():
        if not shutdown_event.is_set():
            print('[Exit] Shutdown requested by user.')
            shutdown_event.set()

    tray = TrayApp(config, clip, buffer, on_quit=request_quit)
    hotkey.callback = clip.save

    # Wire up the "clip saved" callback: notification + refresh the clips list
    def on_clip_saved(path: Path, size_mb: float):
        TrayApp.notify('Clip saved ✓', f'{path.name}  ({size_mb:.1f} MB)')
        tray.window.on_clip_saved(path, size_mb)

    clip.on_saved = on_clip_saved

    print(f'[Ready] Press {config["hotkey"]} or click the window button to save a clip.\n')
    tray.mark_ready()

    buffer_task   = asyncio.create_task(buffer.start())   # records segments forever
    hotkey_task   = asyncio.create_task(hotkey.start())   # listens for keypress forever
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    try:
        done, _ = await asyncio.wait(
            {buffer_task, hotkey_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_task not in done:
            # Bubble up runtime failures from worker tasks.
            for task in (buffer_task, hotkey_task):
                if task in done and task.exception() is not None:
                    raise task.exception()
    finally:
        buffer.stop()
        for task in (buffer_task, hotkey_task, shutdown_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(buffer_task, hotkey_task, shutdown_task, return_exceptions=True)

        await portal.close()
        buffer.clear_segments()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    with loop:
        try:
            loop.run_until_complete(main())
        except KeyboardInterrupt:
            print('\n[Exit] I hope you had a simple and welcoming experience, bye!')
        except PermissionError as e:
            print(f'\n[Permission Error] {e}')
            sys.exit(1)
        except Exception as e:
            print(f'\n[Fatal] {e}')
            raise
