"""
buffer.py – Circular segment buffer via GStreamer (PipeWire) + ffmpeg (mux)

GStreamer captures screen via pipewiresrc (Wayland-compatible) and audio via
pulsesrc, encodes to x264/aac, and writes rolling .mkv segments.
ffmpeg is only used later by clip.py to concatenate segments.

Audio modes (config['audio_mode']):
    'game' - default sink monitor (desktop/game audio)
    'mic'  - input device (microphone)
    'both' - both mixed via GStreamer audiomixer
"""

from __future__ import annotations

import asyncio
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
        """Auto-detect the first microphone (non-monitor) source."""
        try:
            r = subprocess.run(
                ['pactl', 'list', 'short', 'sources'],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and 'monitor' not in parts[1].lower():
                    return parts[1]
        except Exception:
            pass
        print('[Buffer] Warning: could not detect the microphone, using default')
        return 'default'

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

    def _build_pipeline(self, game_src: str, mic_src: str, audio_mode: str) -> str:
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
                f'pulsesrc device={game_src} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'fdkaacenc ! queue ! mux.audio_0'
            )

        elif audio_mode == 'mic':
            audio = (
                f'pulsesrc device={mic_src} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'fdkaacenc ! queue ! mux.audio_0'
            )

        else:  # 'both' – game on audio_0, mic on audio_1 (separate tracks for post-mix)
            audio = (
                f'pulsesrc device={game_src} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'fdkaacenc ! queue ! mux.audio_0 '
                f'pulsesrc device={mic_src} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'fdkaacenc ! queue ! mux.audio_1'
            )

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

        mic_src = self.cfg.get('mic_source', 'auto')
        if mic_src == 'auto':
            mic_src = self._find_mic_source()

        mode_label = {
            'game': 'Game/Desktop',
            'mic':  'Microphone',
            'both': 'Both (game + microphone)',
        }.get(audio_mode, audio_mode)

        print(f'[Buffer] Audio mode    : {mode_label}')
        if audio_mode in ('game', 'both'):
            print(f'[Buffer] Game source   : {game_src}')
        if audio_mode in ('mic', 'both'):
            print(f'[Buffer] Microphone    : {mic_src}')

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
        # Quantos segmentos cobrem cada janela
        segs_before = ceil(seconds_before / self.seg_duration) + 1
        segs_after  = ceil(seconds_after  / self.seg_duration) + 1

        # Todos os segmentos existentes, ordenados por nome (seg_00001, seg_00002, ...)
        all_segs = sorted(
            [path for path, _ in self.seg_log if path.exists()],
            key=lambda p: p.name,
        )

        if not all_segs:
            return []

        # Estimar qual o segmento do trigger pelo timestamp de gravação
        # e pela duração de cada segmento
        started_at = self.recording_started_at or trigger_time
        trigger_offset_secs = trigger_time - started_at
        trigger_seg_idx = int(trigger_offset_secs / self.seg_duration)

        # Clamp ao range real de segmentos disponíveis no buffer
        trigger_seg_idx = min(trigger_seg_idx, len(all_segs) - 1)

        first = max(0, trigger_seg_idx - segs_before)
        last  = min(len(all_segs) - 1, trigger_seg_idx + segs_after)

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