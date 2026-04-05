"""
clip.py – Saves a clip from the rolling buffer.
Flow:
  1. Record trigger timestamp T.
  2. Wait seconds_after (buffer is still recording).
  3. Collect segments covering [T - seconds_before, T + seconds_after].
  4. Concatenate with ffmpeg -c copy → output file.
  5. Call self.on_saved(path, size_mb) if set (used by the tray for notifications).
"""

import asyncio
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from buffer import BufferManager


class ClipSaver:
    def __init__(self, config: dict, buffer: BufferManager):
        self.cfg   = config
        self.buf   = buffer
        self.out   = Path(config['output_dir']).expanduser()
        self.fmt   = config.get('output_format', 'mp4')
        self.out.mkdir(parents=True, exist_ok=True)
        self._busy = False
        self._post_trigger_start: Optional[float] = None
        self._post_trigger_duration: float = float(config.get('seconds_after', 0))

        # Optional callback fired on successful save: (path: Path, size_mb: float)
        self.on_saved: Optional[Callable[[Path, float], None]] = None

    def post_trigger_state(self) -> tuple[float, float]:
        """Return (elapsed_seconds, target_seconds) for post-trigger capture."""
        if not self.cfg.get('capture_after_hotkey', True):
            return 0.0, 0.0

        target = max(float(self.cfg.get('seconds_after', 0)), 0.0)
        if self._post_trigger_start is None or target <= 0:
            return 0.0, target

        elapsed = min(time.time() - self._post_trigger_start, target)
        return elapsed, target

    async def save(self):
        if self._busy:
            print('[Clip] Already saving - ignoring trigger.')
            return

        if not self.buf.seg_log:
            print('[Clip] Buffer not ready yet (no segments) - ignoring trigger.')
            return

        self._busy   = True
        trigger_time = time.time()
        sec_before   = self.cfg['seconds_before']
        sec_after    = self.cfg['seconds_after'] if self.cfg.get('capture_after_hotkey', True) else 0
        seg_dur      = self.cfg.get('segment_duration', 5)
        self._post_trigger_duration = float(sec_after)
        self._post_trigger_start = time.time() if sec_after > 0 else None

        print(f'[Clip] Triggered! Buffering {sec_after}s more...')
        try:
            # Wait for the "after" window + one extra segment so ffmpeg
            # finishes writing the last segment before we touch it.
            await asyncio.sleep(sec_after + seg_dur)

            segments, ss, total_dur = self.buf.get_segments_for_clip(
                trigger_time, sec_before, sec_after
            )

            if not segments:
                print('[Clip] No segments found - nothing to save.')
                return

            print(f'[Clip] Stitching {len(segments)} segment(s) '
                  f'(trim: ss={ss:.1f}s, duration={total_dur:.0f}s)...')

            # Build ffmpeg concat list.
            # Use `inpoint` on the first segment instead of an output-side
            # `-ss`.  Output-side `-ss` with `-c copy` and multiple audio
            # streams is a known ffmpeg pitfall: it uses the video / primary
            # audio stream as the seek reference and can silently discard
            # packets from secondary streams (mic, audio_1).  `inpoint` is
            # applied by the concat demuxer before packets reach the muxer,
            # so the seek covers every stream simultaneously.
            concat_file = self.buf.seg_dir / '_concat.txt'
            lines: list[str] = []
            for i, seg in enumerate(segments):
                lines.append(f"file '{seg.absolute()}'")
                if i == 0 and ss > 0.001:
                    lines.append(f'inpoint {ss:.3f}')
            concat_file.write_text('\n'.join(lines))

            # Output filename
            ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
            out_path = self.out / f'clip_{ts}.{self.fmt}'

            audio_mode = self.cfg.get('audio_mode', 'game')

            if audio_mode == 'both':
                # Goal: raw clip plays game+mic in any media player, AND the
                # replayd viewer can still offer per-track volume control.
                #
                # Layout written to the output file:
                #   a:0  amix(game+mic)  disposition=default   ← what all
                #                                               players hear
                #   a:1  game only       disposition=none      ← viewer track
                #   a:2  mic  only       disposition=none      ← viewer track
                #
                # The viewer detects the 3-audio-stream layout and exposes
                # only a:1 / a:2 as mixer controls (see viewer.py).
                cmd = [
                    'ffmpeg', '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', str(concat_file),
                    '-t', f'{total_dur:.3f}',
                    '-filter_complex',
                    '[0:a:0][0:a:1]amix=inputs=2:normalize=0[mixed]',
                    '-map', '0:v',
                    '-map', '[mixed]',    # a:0 = pre-mix  (default)
                    '-map', '0:a:0',      # a:1 = game     (non-default)
                    '-map', '0:a:1',      # a:2 = mic      (non-default)
                    '-c:v', 'copy',
                    '-c:a:0', 'aac', '-b:a:0', '192k',
                    '-c:a:1', 'copy',
                    '-c:a:2', 'copy',
                    '-disposition:a:0', 'default',
                    '-disposition:a:1', '0',
                    '-disposition:a:2', '0',
                    str(out_path),
                ]
            else:
                # Single audio stream: stream-copy everything as-is.
                cmd = [
                    'ffmpeg', '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', str(concat_file),
                    '-t',  f'{total_dur:.3f}',
                    '-map', '0',
                    '-c', 'copy',
                    str(out_path),
                ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                size_mb = out_path.stat().st_size / 1_048_576
                print(f'[Clip] Saved: {out_path}  ({size_mb:.1f} MB)')
                if self.on_saved is not None:
                    self.on_saved(out_path, size_mb)
            else:
                print(f'[Clip] ffmpeg error:\n{result.stderr[-400:]}')

        except Exception as e:
            print(f'[Clip] Exception: {e}')
        finally:
            self._post_trigger_start = None
            self._busy = False