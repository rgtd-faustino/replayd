# replayd

**Wayland instant replay for Linux — a Outplayed/ShadowPlay alternative.**

Built specifically for [Bazzite](https://bazzite.gg/) but should work on any distro running Wayland + PipeWire.

---

## Interface Overview

### Main Window & Settings

The main interface and settings panel cover all core functionality.

<table align="center">
  <tr>
    <td align="center">
      <img src="assets/main-window.png" width="450"/><br/>
      <b>Main Window</b><br/>
      Live buffer status, quick controls, and instant replay capture (F5).
    </td>
    <td align="center">
      <img src="assets/settings.png" width="450"/><br/>
      <b>Settings Panel</b><br/>
      Configure buffer, hotkey, audio sources, codec, and output options.
    </td>
  </tr>
</table>

---

### Clip Editor

Trim and export clips without leaving the app.

<div align="center">
  <img src="assets/clip-editor-window.png" width="100%" />
</div>

- **Timeline trimming** — set exact in/out points  
- **Preview player** — review clips before exporting  
- **Audio mixer** — control game and mic tracks independently  
- **Export** — saves a new processed clip without overwriting the original  

---

## What it does

- Records your screen continuously into a rolling buffer (no files pile up — it self-cleans).
- Press your hotkey *after* something happens and it saves the last N seconds automatically.
- Global hotkey via the **xdg-desktop-portal GlobalShortcuts** API — no `input` group or root access needed.
- Screen capture via the **xdg-desktop-portal ScreenCast** API — works inside Flatpak sandboxes.
- The screen picker appears only once; a restore token skips it on every subsequent launch.
- System tray icon with live buffer status (seconds buffered, saving state).
- **Built-in clip editor** — trim, mix audio tracks and export, all without leaving the app.
- Settings UI built-in — no need to edit JSON by hand.
- Supports desktop/game audio, microphone, or both as separate tracks. When recording both, the clip stores them as independent streams so you can remix them freely in the editor.
- Auto-detects available hardware encoders (VA-API for Intel/AMD, NVENC for NVIDIA) and falls back to software H.264 automatically.

---

## Requirements

| | Minimum |
|---|---|
| **OS** | Linux |
| **Display** | Wayland (KDE Plasma, GNOME, Hyprland, Sway, …) — X11 is **not** supported |
| **Audio** | PipeWire with `pipewire-pulse` compatibility layer |
| **Python** | 3.10 or newer |
| **GPU** | Any — hardware encoding (VA-API or NVENC) is preferred but software fallback always works |

**Hardware encoding support:**
- **Intel / AMD** — VA-API (enabled by default on most distros)
- **NVIDIA (proprietary driver)** — NVENC (`nvenc_h264`, `nvenc_h265`, `nvenc_av1`)
- **NVIDIA (nouveau / NVK)** — VA-API via NVK (Fedora 40+ / Mesa 24+)

If none of the above apply, select **H.264 (Software)** in the settings — it uses the CPU and works on any machine.

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/replayd.git
cd replayd
bash install.sh
```

Then **log out and back in** (only needed if you were already logged in when the install ran, to refresh your environment).

> **Bazzite / Silverblue / Kinoite and other immutable distros:**
> The installer detects a read-only rootfs and skips system package installation automatically.
> All required packages (GStreamer, ffmpeg, PipeWire) ship pre-installed on Bazzite and most
> ublue-based images. The Python dependencies are installed to your home directory as usual.
> If a system package is missing: `rpm-ostree install <package>` then reboot.

---

## Running

```bash
python3 main.py
```

A window appears in the bottom-right corner and a dot appears in your system tray. The app records immediately in the background. Press your hotkey (default **F5**) after something happens to save a clip.

---

## Clip Editor

Click **"View Clips"** in the tray window (or the tray icon itself) to open the clip editor.

<div align="center">
  <img src="assets/clip-editor.png" width="80%" />
</div>

The editor has three main areas:

**Clip list (left sidebar)**
Scrollable list of all saved clips with thumbnails and file sizes. Click any clip to load it. Use **"Open folder →"** to open the output directory in your file manager.

**Video player (top)**
Full preview of the selected clip. Use the **▶ Play / ⏸ Pause** button or click anywhere on the timeline to seek.

**Trim timeline**
The orange handles at each end of the timeline set the in/out points. Drag them to trim the clip — the timecodes below update in real time to show the current in point, playhead position and out point.

**Audio Mixer**
Appears below the controls whenever a clip has audio. Drag the handle at the top of the mixer panel to resize it.
- Each audio track (Game Audio 🎮, Microphone 🎤, or generic Track N) has its own volume slider (0–200%) and a mute button.
- Volume changes only affect the exported file, not the live preview.
- Use **Reset all** to restore all tracks to 100%.

**Export Clip**
The centered **Export Clip** button exports the clip applying both the current trim (in/out points) and the audio mixer settings in one step. The result is saved as a new file in the same folder — the original is never overwritten. The button is disabled while the export is running.

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
| `video_codec` | `h264` | `h264`, `h265`, `av1`, `h264_soft` (software), `nvenc_h264`, `nvenc_h265`, `nvenc_av1` (NVIDIA) — app auto-detects what your GPU supports |
| `segment_duration` | `5` | Internal segment length in seconds (don't change unless you know why) |
| `audio_mode` | `both` | `game` (desktop audio), `mic` (microphone), or `both` |
| `audio_source` | `auto` | PulseAudio monitor device, or `auto` to detect |
| `mic_source` | `auto` | PulseAudio mic device, or `auto` to detect |

**Available hotkeys (examples):**
`KEY_F5`, `KEY_F9`, `KEY_F10`, `KEY_F11`, `KEY_F12`, `KEY_HOME`, `KEY_INSERT`, `KEY_SCROLLLOCK`

The key names follow the Linux evdev naming convention (`KEY_` prefix). The hotkey is registered via the **xdg-desktop-portal GlobalShortcuts** API — no special permissions or group membership required. The compositor may show a one-time confirmation dialog when you first set a hotkey.

---

## How the buffer works

The app writes short `.mkv` segments (default 5 seconds each) to `$XDG_RUNTIME_DIR/replayd/buffer/` (memory-backed on most systems, falling back to `/tmp/replayd/buffer/`). It keeps only as many segments as needed to cover your `seconds_before + seconds_after` window, deleting older ones automatically. When you trigger a save, it waits for the "after" window, then stitches the relevant segments into a single clip with ffmpeg. The buffer is also cleared on exit.

The screen picker only appears once — a restore token is saved to `~/.config/replayd/` so subsequent launches skip the dialog and connect to the same monitor automatically. To pick a different source, open Settings and click the source button.

---

## Troubleshooting

**No hotkey response**
→ replayd uses the **xdg-desktop-portal GlobalShortcuts** API — no `input` group or `/dev/input` access is needed. If the hotkey doesn't work, check that `xdg-desktop-portal` is running and supports GlobalShortcuts (requires xdg-desktop-portal ≥ 1.18 and a supporting compositor such as KDE Plasma 6 or GNOME 45+).
→ On first launch the compositor may show a dialog asking you to confirm the shortcut binding — accept it.
→ If your compositor doesn't support GlobalShortcuts, the Save Clip button in the app window always works as a fallback.

**Black screen or no video**
→ On the very first launch a screen picker dialog appears — select your monitor and it won't show again. If the picker never appears, check that `xdg-desktop-portal` is running: `systemctl --user status xdg-desktop-portal`. To pick a different source later, open Settings and click the source button.

**"Wayland not detected" on startup**
→ replayd requires a native Wayland session. At your login screen, make sure you select the **Wayland** variant of your desktop (e.g. "Plasma (Wayland)" not "Plasma (X11)"). Running inside XWayland is not supported.

**"PipeWire is not running" on startup**
→ Start PipeWire with: `systemctl --user start pipewire pipewire-pulse wireplumber`
→ To make it start automatically: `systemctl --user enable pipewire pipewire-pulse wireplumber`
→ On older distros (Ubuntu 20.04, Debian 11) PipeWire may not be available — upgrade to a newer release.

**"Python 3.10+ required"**
→ Check your version with `python3 --version`. Install a newer Python via your package manager, or use [pyenv](https://github.com/pyenv/pyenv) to manage versions alongside your system Python.

**Audio encoding fails (`fdkaacenc`)**
→ Your distro may not have the fdkaac GStreamer plugin (patent issues on some repos). The installer tries to install it and falls back gracefully to `gst-libav` AAC.

**Video encoding fails / codec not found**
→ Open settings and switch to **H.264 (Software)** — this works on any machine without VA-API. The app also auto-detects available codecs on startup and falls back automatically if the selected one isn't supported.

**Export Clip produces no video stream**
→ This can happen if the clip only has audio tracks. Check the terminal output for the ffmpeg error — it will say which stream is missing.

---

## Dependencies

**Python packages** (installed by `install.sh` or `pip3 install -r requirements.txt`):
- `dbus-next` — D-Bus / xdg-desktop-portal for Wayland screen capture and global shortcuts
- `pulsectl` — PulseAudio/PipeWire audio device enumeration
- `PyQt6` — GUI: tray icon, main window, clip editor, settings overlay
- `qasync` — bridges asyncio with the Qt event loop

**System packages** (installed by `install.sh`):
- `ffmpeg` — final clip stitching and export
- `gstreamer` + plugins (base, good, bad, ugly, vaapi, pipewire, libav)
- `pipewire`, `pipewire-pulse`, `wireplumber`
- `notify-send` (for desktop notifications — usually pre-installed)

---

## License

GPL v3 — see [LICENSE](LICENSE).

You are free to use, modify and distribute this software, but any modified version must also be released under GPL v3 with attribution to the original author.

© 2026 rgtd-faustino