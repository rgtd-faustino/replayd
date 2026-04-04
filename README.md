# replayd

**Wayland instant replay for Linux — a Outplayed/ShadowPlay alternative.**

Built specifically for [Bazzite](https://bazzite.gg/) but should work on any distro running Wayland + PipeWire.

---

<div align="center">
  <img src="assets/main-window.png" width="45%" />
  &nbsp;&nbsp;&nbsp;
  <img src="assets/settings.png" width="45%" />
</div>

---

## What it does

- Records your screen continuously into a rolling buffer (no files pile up — it self-cleans).
- Press your hotkey *after* something happens and it saves the last N seconds automatically.
- System tray icon with live buffer status.
- Settings UI built-in — no need to edit JSON by hand.
- Supports desktop/game audio, microphone, or both mixed together.

---

## Requirements

**Hardware**
- A GPU with VA-API support (Intel, AMD, or recent NVIDIA via nouveau/NVK) for hardware-accelerated encoding.
  If your GPU doesn't support VA-API, select **H.264 (Software)** in the settings — it uses the CPU instead and works on any machine.

**System**
- Wayland compositor (KDE Plasma, GNOME, etc.)
- PipeWire + WirePlumber (standard on Bazzite and most modern distros)
- PulseAudio compatibility layer (`pipewire-pulse`) for audio capture

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/replayd.git
cd replayd
bash install.sh
```

Then **log out and back in** (required once, so your user gets `/dev/input` access for the global hotkey).

---

## Running

```bash
python3 main.py
```

A window appears in the bottom-right corner and a dot appears in your system tray. The app records immediately in the background. Press your hotkey (default **F5**) after something happens to save a clip.

---

## Configuration

Edit `config.json` or use the ⚙️ settings button in the app window. All changes take effect after the app restarts (it restarts itself automatically when you save settings).

| Field | Default | Description |
|---|---|---|
| `seconds_before` | `30` | How many seconds before the hotkey press to include |
| `seconds_after` | `30` | How many seconds after the hotkey press to wait before saving |
| `capture_after_hotkey` | `true` | If false, saves immediately on hotkey (no "after" buffer) |
| `hotkey` | `KEY_F5` | evdev key name — see full list below |
| `output_dir` | `~/Videos/Replayd` | Where clips are saved |
| `output_format` | `mp4` | `mp4` or `mkv` |
| `video_codec` | `h264` | `h264`, `h265`, `av1`, or `h264_soft` (software) — app auto-detects what your GPU supports |
| `segment_duration` | `5` | Internal segment length in seconds (don't change unless you know why) |
| `audio_mode` | `both` | `game` (desktop audio), `mic` (microphone), or `both` |
| `audio_source` | `auto` | PulseAudio monitor device, or `auto` to detect |
| `mic_source` | `auto` | PulseAudio mic device, or `auto` to detect |

**Available hotkeys (examples):**
`KEY_F5`, `KEY_F9`, `KEY_F10`, `KEY_F11`, `KEY_F12`, `KEY_HOME`, `KEY_INSERT`, `KEY_SCROLLLOCK`

Full list:
```bash
python3 -c "import evdev; print([k for k in evdev.ecodes.ecodes if k.startswith('KEY_')])"
```

---

## How the buffer works

The app writes short `.mkv` segments (default 5 seconds each) to `/tmp/replayd_buffer/`. It keeps only as many as needed to cover your `seconds_before + seconds_after` window, deleting older ones automatically. When you trigger a save, it waits for the "after" window, then stitches the relevant segments into a single clip with ffmpeg. The `/tmp` buffer is also cleared on exit.

---

## Troubleshooting

**No hotkey / "No device found"**
→ You need to be in the `input` group. Run `sudo usermod -a -G input $USER` then log out and back in.

**Black screen or no video**
→ A screen picker dialog should appear when you start the app. If it doesn't, check that `xdg-desktop-portal` is running: `systemctl --user status xdg-desktop-portal`

**Audio encoding fails (`fdkaacenc`)**
→ Your distro may not have the fdkaac GStreamer plugin (patent issues on some repos). The installer tries to install it and falls back gracefully to `gst-libav` AAC.

**Video encoding fails / codec not found**
→ Open settings and switch to **H.264 (Software)** — this works on any machine without VA-API. The app also auto-detects available codecs on startup and falls back automatically if the selected one isn't supported.

---

## Dependencies

**Python packages** (installed by `install.sh` or `pip3 install -r requirements.txt`):
- `dbus-next` — D-Bus / xdg-desktop-portal for Wayland screen capture
- `evdev` — global hotkey listener via `/dev/input` (requires `input` group)
- `PyQt6` — GUI: tray icon, main window, settings overlay
- `qasync` — bridges asyncio with the Qt event loop

**System packages** (installed by `install.sh`):
- `ffmpeg` — final clip stitching
- `gstreamer` + plugins (base, good, bad, ugly, vaapi, pipewire, libav)
- `pipewire`, `pipewire-pulse`, `wireplumber`
- `notify-send` (for desktop notifications — usually pre-installed)

---

## License

GPL v3 — see [LICENSE](LICENSE).

You are free to use, modify and distribute this software, but any modified version must also be released under GPL v3 with attribution to the original author.

© 2026 rgtd-faustino