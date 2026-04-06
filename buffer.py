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
import shlex
import subprocess
import time
from collections import deque
from math import ceil
from pathlib import Path

from paths import buffer_dir as _buffer_dir


class BufferManager:
    def __init__(self, config: dict, node_id: int):
        self.cfg          = config
        self.node_id      = node_id
        self.seg_duration = config.get('segment_duration', 5)
        self.seg_dir      = _buffer_dir()

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
        """Auto-detect the desktop/game monitor source via PipeWire/PulseAudio."""
        try:
            import pulsectl
            with pulsectl.Pulse('replayd-audio') as pulse:
                default_sink = pulse.server_info().default_sink_name
                if default_sink:
                    return default_sink + '.monitor'
        except Exception:
            pass
        try:
            import pulsectl
            with pulsectl.Pulse('replayd-audio-fb') as pulse:
                for src in pulse.source_list():
                    if 'monitor' in src.name.lower():
                        return src.name
        except Exception:
            pass
        print('[Buffer] Warning: could not detect the audio monitor, using default.monitor')
        return 'default.monitor'

    def _find_mic_source(self) -> str:
        """Auto-detect the default microphone (non-monitor) source via pulsectl."""
        try:
            import pulsectl
            with pulsectl.Pulse('replayd-mic') as pulse:
                # Try the system default source first
                default_src = pulse.server_info().default_source_name
                if default_src and self._is_valid_mic_source_name(default_src):
                    return default_src

                sources = pulse.source_list()
                valid = [s.name for s in sources
                         if self._is_valid_mic_source_name(s.name)]

                # Prefer real hardware ALSA input nodes
                for name in valid:
                    if name.startswith('alsa_input'):
                        return name

                if valid:
                    return valid[0]
        except Exception as e:
            print(f'[Buffer] pulsectl error in _find_mic_source: {e}')

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

        # ── Resolution scaling ────────────────────────────────────────────────
        res = self.cfg.get('recording_resolution', 'native')
        scale_str = ''
        if res and res != 'native':
            try:
                w_str, h_str = res.lower().split('x')
                w, h = int(w_str), int(h_str)
                scale_str = (
                    f'videoscale ! '
                    f'video/x-raw,width={w},height={h},pixel-aspect-ratio=1/1 ! '
                    f'videoconvert ! '
                )
                print(f'[Buffer] Resolution override: {w}\u00d7{h}')
            except (ValueError, AttributeError):
                print(f'[Buffer] Warning: invalid recording_resolution "{res}", using native.')

        # ── Bitrate / rate-control ────────────────────────────────────────────
        # video_bitrate_kbps = 0 leaves the encoder at its own default.
        # Any positive value injects a bitrate cap; useful to rein in VA-API /
        # NVENC encoders whose defaults can be 10-20 Mbps at 1080p.
        bitrate_kbps = max(0, int(self.cfg.get('video_bitrate_kbps', 0)))
        if bitrate_kbps > 0:
            if encoder.startswith('va'):
                # VA-API: explicit CBR mode + bitrate (kbps)
                enc_str = f'{encoder} rate-control=cbr bitrate={bitrate_kbps}'
            else:
                # x264enc and NVENC: both accept `bitrate` in kbps directly
                enc_str = f'{encoder} bitrate={bitrate_kbps}'
            print(f'[Buffer] Video bitrate cap: {bitrate_kbps} kbps')
        else:
            enc_str = encoder

        video = (
            f'pipewiresrc path={self.node_id} do-timestamp=true ! '
            f'videoconvert ! '
            f'{scale_str}'
            f'{enc_str} ! '
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
                f'avenc_aac bitrate=192000 ! queue ! mux.audio_0'
            )

        elif audio_mode == 'mic':
            audio = (
                f'{self._pulse_src(mic_src)} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'avenc_aac bitrate=192000 ! queue ! mux.audio_0'
            )

        else:  # 'both'
            # Keep pulsesrc branches slaved to the PipeWire video clock.
            game_audio = (
                f'{self._pulse_src(game_src)} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'avenc_aac bitrate=192000 ! queue max-size-time=3000000000 leaky=downstream ! mux.audio_0'
            )

            # In practice, pipewiresrc audio can be unstable on some stacks
            # (caps/runtime issues). Prefer pulsesrc for reliability.
            mic_audio = (
                f'{self._pulse_src(mic_src)} ! '
                f'audioconvert ! audioresample ! {caps} ! '
                f'avenc_aac bitrate=192000 ! queue max-size-time=3000000000 leaky=downstream ! mux.audio_1'
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

        # ── PipeWire / PulseAudio sanity check ──────────────────────────────────
        def _check_pipewire():
            import pulsectl
            with pulsectl.Pulse('replayd-check') as pulse:
                pulse.server_info()

        try:
            await loop.run_in_executor(None, _check_pipewire)
            print('[Buffer] PipeWire/PulseAudio connection OK')
        except Exception as _e:
            raise RuntimeError(
                f'PipeWire is not running or pulsectl cannot connect: {_e}\n'
                '  Start it with: systemctl --user start pipewire pipewire-pulse'
            ) from _e

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

    def get_segments_for_clip(
        self,
        trigger_time: float,
        seconds_before: float,
        seconds_after: float,
    ) -> tuple[list[Path], float, float]:
        """Return (segments, ffmpeg_ss, total_duration).

        segments       – ordered list of .mkv files to concat
        ffmpeg_ss      – seek offset (seconds) to pass as -ss to ffmpeg so the
                         output clip starts exactly at trigger_time - seconds_before
        total_duration – pass as -t to ffmpeg (= seconds_before + seconds_after)

        How trigger detection works
        ---------------------------
        We use each segment's mtime (the time GStreamer finished writing it) to
        find the first completed segment whose mtime is *after* trigger_time.
        That segment was the one in-progress at the moment of the trigger.

        The previous approach computed an index from
        (trigger_time - recording_started_at) / seg_duration which broke in
        practice because GStreamer takes a variable amount of time to initialise
        its pipeline, so the offset never mapped cleanly to segment numbers and
        trigger_seg_idx was always clamped to 0.

        How precise trimming works
        --------------------------
        Even after finding the right segments, a segment-level window is off by
        up to ±seg_duration seconds because triggers almost never fall exactly
        on a segment boundary.  We calculate an exact seek offset (ss) into the
        concatenated segments so ffmpeg starts output at trigger_time -
        seconds_before and stops after exactly seconds_before + seconds_after.
        """
        # +2 extra segments on each side to give headroom for the trim.
        segs_before = ceil(seconds_before / self.seg_duration) + 2
        segs_after  = ceil(seconds_after  / self.seg_duration) + 2

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
            return [], 0.0, float(seconds_before + seconds_after)

        # ── 1. Find the trigger segment ───────────────────────────────────────
        # The first segment whose mtime > trigger_time is the one that was being
        # written when the hotkey was pressed.
        trigger_seg_idx = len(all_segs) - 1   # fallback: treat trigger as end
        for i, p in enumerate(all_segs):
            try:
                if p.stat().st_mtime > trigger_time:
                    trigger_seg_idx = i
                    break
            except OSError:
                continue

        # ── 2. Select the segment window ─────────────────────────────────────
        first = max(0, trigger_seg_idx - segs_before)
        last  = min(len(all_segs) - 1, trigger_seg_idx + segs_after)
        selected = all_segs[first : last + 1]

        # ── 3. Calculate precise ffmpeg seek offset ───────────────────────────
        # The mtime of the segment *before* the first selected one is the exact
        # wall-clock time when the first selected segment started being written.
        # Using the predecessor's mtime is more accurate than subtracting
        # seg_duration from the first segment's own mtime.
        try:
            if first > 0:
                first_seg_start = all_segs[first - 1].stat().st_mtime
            else:
                first_seg_start = all_segs[first].stat().st_mtime - self.seg_duration
        except OSError:
            first_seg_start = trigger_time - seconds_before

        clip_start   = trigger_time - seconds_before
        ss           = max(0.0, clip_start - first_seg_start)
        total_dur    = float(seconds_before + seconds_after)

        print(f'[Buffer] Clip window: segs[{first}:{last+1}] '
              f'(trigger_idx={trigger_seg_idx}, '
              f'total_available={len(all_segs)}, '
              f'trim_ss={ss:.1f}s, duration={total_dur:.0f}s)')
        return selected, ss, total_dur

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