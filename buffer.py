"""
buffer.py – Circular segment buffer via GStreamer (PipeWire) + ffmpeg (mux)

GStreamer captures screen via pipewiresrc (Wayland-compatible) and audio via
pulsesrc, encodes to x264/aac, and writes rolling .mkv segments.
ffmpeg is only used later by clip.py to concatenate segments.

Audio modes (config['audio_mode']):
    'game' - default sink monitor (desktop/game audio)
    'mic'  - input device (microphone)
    'both' - game on audio_0 via pulsesrc, mic on audio_1 via pulsesrc
             (both with provide-clock=false so video pipewiresrc remains
              the master clock for stable muxing)
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import time
from collections import deque
from math import ceil
from pathlib import Path


class BufferManager:
    def __init__(self, config: dict, node_id: int):
        self.cfg          = config
        self.node_id      = node_id
        self.seg_duration = config.get('segment_duration', 5)
        self.seg_dir      = Path('/tmp/replayd_buffer')
        self.seg_dir.mkdir(parents=True, exist_ok=True)

        max_secs      = config['seconds_before'] + config['seconds_after'] + 30
        self.max_segs = ceil(max_secs / self.seg_duration) + 2

        self._seg_index = 0
        self.seg_log: deque = deque()   # (Path, float)
        self._gst_proc = None
        self.recording_started_at: float | None = None

    # ── audio detection ───────────────────────────────────────────────────────

    @staticmethod
    def _is_valid_mic_source_name(name: str) -> bool:
        """Return True if source name looks like a real mic (not monitor/virtual mix)."""
        n = (name or '').strip().lower()
        if not n:
            return False
        if 'monitor' in n:
            return False
        # Avoid virtual mixed/processed nodes that frequently include game audio.
        blocked = ('echo-cancel', 'echo_cancel', 'remap', 'loopback', 'combine')
        if any(tok in n for tok in blocked):
            return False
        return True

    def _find_audio_source(self) -> str:
        """Auto-detect the desktop/game monitor source."""
        try:
            r = subprocess.run(
                ['pactl', 'get-default-sink'],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip() + '.monitor'
        except Exception:
            pass
        try:
            r = subprocess.run(
                ['pactl', 'list', 'short', 'sources'],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and 'monitor' in parts[1].lower():
                    return parts[1]
        except Exception:
            pass
        print('[Buffer] Warning: could not detect the audio monitor, using default.monitor')
        return 'default.monitor'

    def _find_mic_source(self) -> str:
        """Auto-detect the default microphone (non-monitor) source.

        On PipeWire, pactl get-default-source is unreliable — it can return a
        monitor of the default sink or an echo-cancel virtual node that mixes
        desktop + mic audio, causing the mic track to contain game audio too.

        Detection order (most to least reliable):
          1. System default source (if valid).
          2. First valid alsa_input.* entry.
          3. Any valid non-monitor source.
        """
        try:
            # 1) Prefer user/system default source.
            try:
                rd = subprocess.run(
                    ['pactl', 'get-default-source'],
                    capture_output=True, text=True, timeout=5,
                )
                if rd.returncode == 0:
                    raw = rd.stdout.strip()
                    if self._is_valid_mic_source_name(raw):
                        return raw
            except Exception:
                pass

            r = subprocess.run(
                ['pactl', 'list', 'short', 'sources'],
                capture_output=True, text=True, timeout=5,
            )
            lines = r.stdout.splitlines()
            sources: list[str] = []

            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[1]
                    if self._is_valid_mic_source_name(name):
                        sources.append(name)

            # 2) Prefer real hardware ALSA input sources.
            for name in sources:
                if name.startswith('alsa_input'):
                    return name

            # 3) Last resort: any valid source.
            if sources:
                return sources[0]
        except Exception:
            pass
        print('[Buffer] Warning: could not detect the microphone, using default')
        return 'default'

    @staticmethod
    def _pulse_src(device: str | None) -> str:
        """Build pulsesrc with optional explicit device.

        If device is None, Pulse/PipeWire chooses the current default source.
        """
        if device:
            return f'pulsesrc device={device} do-timestamp=true provide-clock=false'
        return 'pulsesrc do-timestamp=true provide-clock=false'

    def _find_pw_mic_node_id(self, preferred_source: str | None = None) -> int | None:
        """
        Find the PipeWire node ID for microphone capture via pw-dump.

        This is the preferred method for mic capture in 'both' mode.
        pipewiresrc path=<id> shares the PipeWire clock with the video
        pipewiresrc, eliminating the clock-mismatch that causes splitmuxsink
        to silently drop the mic track.

        Looks for Audio/Source nodes that are NOT monitors.
        Prefers alsa_input.* nodes (real hardware) over virtual nodes.
        If preferred_source is provided, tries to match that source first.
        Returns None if pw-dump is unavailable or no mic node found.
        """
        try:
            r = subprocess.run(
                ['pw-dump'],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return None

            objects = json.loads(r.stdout)
            pref = (preferred_source or '').strip().lower()
            candidates: list[tuple[int, int, str]] = []   # (priority, id, name)

            for obj in objects:
                if obj.get('type') != 'PipeWire:Interface:Node':
                    continue
                info = obj.get('info', {})
                props = info.get('props', {})
                if props.get('media.class') != 'Audio/Source':
                    continue
                node_name = props.get('node.name', '')
                if 'monitor' in node_name.lower():
                    continue

                node_l = node_name.lower()

                # Highest priority: explicit source chosen by user/auto resolution.
                if pref and node_l == pref:
                    priority = -2
                elif pref and pref in node_l:
                    priority = -1
                # Otherwise prefer real hardware ALSA input nodes.
                elif node_name.startswith('alsa_input'):
                    priority = 0
                else:
                    priority = 1

                candidates.append((priority, obj.get('id'), node_name))

            if candidates:
                candidates.sort()
                _, node_id, node_name = candidates[0]
                print(f'[Buffer] PipeWire mic node: id={node_id}, name={node_name}')
                return node_id

        except Exception as e:
            print(f'[Buffer] pw-dump mic detection failed: {e}')

        return None

    # ── GStreamer pipeline ────────────────────────────────────────────────────

    CODEC_MAP = {
        'h264':       ('vah264enc',  'h264parse'),
        'h265':       ('vah265enc',  'h265parse'),
        'av1':        ('vav1enc',    'av1parse'),
        'h264_soft':  ('x264enc',    'h264parse'),
        'nvenc_h264': ('nvh264enc',  'h264parse'),
        'nvenc_h265': ('nvh265enc',  'h265parse'),
        'nvenc_av1':  ('nvav1enc',   'av1parse'),
    }

    @staticmethod
    def _probe_codecs() -> list[str]:
        """Return list of available codec keys based on installed GStreamer elements."""
        checks = {
            'h264':       'vah264enc',
            'h265':       'vah265enc',
            'av1':        'vav1enc',
            'h264_soft':  'x264enc',
            'nvenc_h264': 'nvh264enc',
            'nvenc_h265': 'nvh265enc',
            'nvenc_av1':  'nvav1enc',
        }
        available = []
        for key, element in checks.items():
            r = subprocess.run(
                ['gst-inspect-1.0', element],
                capture_output=True,
            )
            if r.returncode == 0:
                available.append(key)
        return available

    def _build_pipeline(self, game_src: str, mic_src: str | None, audio_mode: str) -> str:
        seg_pattern = str(self.seg_dir / 'seg_%05d.mkv')
        dur_ns      = self.seg_duration * 1_000_000_000
        caps        = 'audio/x-raw,rate=48000,channels=2'

        codec_key        = self.cfg.get('video_codec', 'h264')
        encoder, parser  = self.CODEC_MAP.get(codec_key, ('vah264enc', 'h264parse'))

        video = (
            f'pipewiresrc path={self.node_id} do-timestamp=true ! '
            f'videoconvert ! '
            f'{encoder} ! '
            f'{parser} ! '
            f'queue ! '
            f'splitmuxsink name=mux '
            f'location={seg_pattern} '
            f'max-size-time={dur_ns} '
            f'muxer-factory=matroskamux '
            f'async-finalize=true'
        )

        if audio_mode == 'game':
            audio = (
                f'{self._pulse_src(game_src)} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'fdkaacenc ! queue ! mux.audio_0'
            )

        elif audio_mode == 'mic':
            audio = (
                f'{self._pulse_src(mic_src)} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'fdkaacenc ! queue ! mux.audio_0'
            )

        else:  # 'both'
            # Keep pulsesrc branches slaved to the PipeWire video clock.
            game_audio = (
                f'{self._pulse_src(game_src)} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'fdkaacenc ! queue max-size-time=3000000000 leaky=downstream ! mux.audio_0'
            )

            # In practice, pipewiresrc audio can be unstable on some stacks
            # (caps/runtime issues). Prefer pulsesrc for reliability.
            mic_audio = (
                f'{self._pulse_src(mic_src)} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'fdkaacenc ! queue max-size-time=3000000000 leaky=downstream ! mux.audio_1'
            )
            if mic_src:
                print(f'[Buffer] Mic audio: pulsesrc (device {mic_src})')
            else:
                print('[Buffer] Mic audio: pulsesrc (default source)')

            audio = game_audio + ' ' + mic_audio

        return video + ' ' + audio

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        loop      = asyncio.get_running_loop()

        # ── PipeWire sanity check ─────────────────────────────────────────────
        pw_check = await asyncio.create_subprocess_exec(
            'pw-cli', 'info',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await pw_check.wait()
        if pw_check.returncode != 0:
            raise RuntimeError(
                'PipeWire is not running or pw-cli is not installed.\n'
                '  Start it with:  systemctl --user start pipewire pipewire-pulse\n'
                '  Install it with your package manager (see install.sh).'
            )

        available = await loop.run_in_executor(None, self._probe_codecs)

        codec_key = self.cfg.get('video_codec', 'h264')
        if codec_key not in available:
            fallback = 'h264_soft' if 'h264_soft' in available else (available[0] if available else 'h264_soft')
            print(f'[Buffer] Codec "{codec_key}" not available, falling back to "{fallback}"')
            codec_key = fallback
            self.cfg['video_codec'] = codec_key
        print(f'[Buffer] Codec: {codec_key}  (available: {available})')

        audio_mode = self.cfg.get('audio_mode', 'both')

        game_src = self.cfg.get('audio_source', 'auto')
        if game_src == 'auto':
            game_src = self._find_audio_source()

        mic_cfg = self.cfg.get('mic_source', 'auto')
        if mic_cfg == 'auto':
            mic_src: str | None = None
            mic_log = f'auto (default: {self._find_mic_source()})'
        else:
            mic_src = mic_cfg
            mic_log = mic_src

        mode_label = {
            'game': 'Game/Desktop',
            'mic':  'Microphone',
            'both': 'Both (game + microphone)',
        }.get(audio_mode, audio_mode)

        print(f'[Buffer] Audio mode    : {mode_label}')
        if audio_mode in ('game', 'both'):
            print(f'[Buffer] Game source   : {game_src}')
        if audio_mode in ('mic', 'both'):
            print(f'[Buffer] Microphone    : {mic_log}')

        pipeline = self._build_pipeline(game_src, mic_src, audio_mode)
        print(f'[Buffer] GStreamer pipeline:\n  {pipeline}\n')

        # shlex.split handles quoted strings correctly when not going through a shell
        cmd = ['gst-launch-1.0', '-e'] + shlex.split(pipeline)
        self.recording_started_at = time.time()
        self._gst_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        print(f'[Buffer] Recording started (node={self.node_id}, '
              f'max_segs={self.max_segs} x {self.seg_duration}s)')

        await asyncio.gather(
            self._monitor_segments(),
            self._log_gst_output(),
        )

    async def _log_gst_output(self):
        async for line in self._gst_proc.stderr:
            text = line.decode(errors='replace').rstrip()
            if text:
                print(f'[gst] {text}')
        async for line in self._gst_proc.stdout:
            text = line.decode(errors='replace').rstrip()
            if text:
                print(f'[gst] {text}')

    async def _monitor_segments(self):
        """Poll /tmp/replayd_buffer for new seg_*.mkv files."""
        seen = set()
        while True:
            await asyncio.sleep(0.5)
            try:
                files = sorted(self.seg_dir.glob('seg_*.mkv'))
            except OSError:
                continue

            for path in files:
                if path not in seen:
                    seen.add(path)
                    self.seg_log.append((path, time.time()))
                    print(f'[Buffer] Segment: {path.name}  '
                        f'(total: {len(self.seg_log)})')

            # Prune oldest beyond max window
            while len(self.seg_log) > self.max_segs + 2:
                old_path, _ = self.seg_log.popleft()
                seen.discard(old_path)
                try:
                    old_path.unlink(missing_ok=True)
                except OSError:
                    pass

    # ── clip query ────────────────────────────────────────────────────────────

    def get_segments_for_clip(self, trigger_time, seconds_before, seconds_after):
        segs_before = ceil(seconds_before / self.seg_duration) + 1
        segs_after  = ceil(seconds_after  / self.seg_duration) + 1

        # Read segments directly from disk so we include any written since the
        # last _monitor_segments poll, and exclude tiny files that are still
        # being written by GStreamer (< 16 KB = almost certainly still open).
        MIN_BYTES = 16_384
        all_segs = sorted(
            [
                p for p in self.seg_dir.glob('seg_*.mkv')
                if p.exists() and p.stat().st_size >= MIN_BYTES
            ],
            key=lambda p: p.name,
        )

        if not all_segs:
            return []

        # Determine which segment contains the trigger moment.
        #
        # Bug in previous approach: trigger_seg_idx was calculated from
        # absolute recording time (trigger_time - started_at) and then used as
        # a direct index into all_segs. After pruning, all_segs[0] is no longer
        # segment #1 — it can be segment #104 — so the index was off by
        # (first_seg_number - 1), causing almost no "after" segments to be
        # returned.
        #
        # Fix: extract the absolute segment number from the first filename
        # (e.g. "seg_00104.mkv" → 104), then map the trigger's absolute segment
        # number to a valid index in all_segs.
        def _seg_num(p: Path) -> int:
            try:
                return int(p.stem.replace('seg_', ''))
            except ValueError:
                return 0

        first_seg_num = _seg_num(all_segs[0])   # e.g. 104
        started_at    = self.recording_started_at or trigger_time
        trigger_offset_secs = trigger_time - started_at
        trigger_abs_seg     = int(trigger_offset_secs / self.seg_duration) + 1  # 1-based
        # Convert absolute segment number to index in all_segs
        trigger_seg_idx = trigger_abs_seg - first_seg_num
        trigger_seg_idx = max(0, min(trigger_seg_idx, len(all_segs) - 1))

        first = max(0, trigger_seg_idx - segs_before)
        last  = min(len(all_segs) - 1, trigger_seg_idx + segs_after)

        print(f'[Buffer] Clip window: segs[{first}:{last+1}] '
              f'(trigger_idx={trigger_seg_idx}, '
              f'total_available={len(all_segs)})')
        return all_segs[first : last + 1]

    def stop(self):
        if self._gst_proc and self._gst_proc.returncode is None:
            self._gst_proc.terminate()
            print('[Buffer] Stopped.')

    def clear_segments(self):
        """Delete all temporary buffer artifacts created during runtime."""
        try:
            for path in self.seg_dir.glob('*'):
                if path.is_file():
                    path.unlink(missing_ok=True)
            self.seg_log.clear()
            print('[Buffer] Temporary segments cleared.')
        except OSError as e:
            print(f'[Buffer] Failed to clear segments: {e}')