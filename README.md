# replayd

Wayland instant replay for Linux. Records continuously in the background — press a hotkey after something happens and it saves the last N seconds as a clip. Think ShadowPlay or Outplayed, but for any Wayland desktop.

Built on Bazzite, works on anything running Wayland + PipeWire.

---

<div align="center">
  <img src="assets/main-window.png" width="400"/>
  <br/><sub>Main window</sub>
</div>

---


<div align="center">
  <img src="assets/settings.png" width="860"/>
  <br/><sub>Settings</sub>
</div>

---


<div align="center">
  <img src="assets/clip-editor-window.png" width="860"/>
  <br/><sub>Clip editor — trim, mix audio, export</sub>
</div>

---

## Features

- Rolling buffer with configurable before/after window
- Global hotkey via **xdg-desktop-portal GlobalShortcuts** — no `input` group, no root
- Screen capture via **xdg-desktop-portal ScreenCast** — Flatpak-compatible
- Hardware encoding: VA-API (Intel/AMD), NVENC (NVIDIA), software H.264 fallback
- Game audio + mic as separate tracks — remix them freely in the clip editor
- Built-in clip editor: trim, per-track volume control, export
- Settings UI — no need to touch `config.json` by hand

---

## Requirements

- Linux + Wayland (KDE Plasma 6 or GNOME 45+ recommended — see [Hotkey support](#hotkey-support))
- PipeWire with `pipewire-pulse`
- Python 3.10+
- Any GPU (hardware encoding preferred, software always works)

---

## Installation

### Flatpak (recommended)

```bash
flatpak install flathub io.github.rgtd_faustino.replayd
```

### From source

```bash
git clone https://github.com/rgtd-faustino/replayd.git
cd replayd
bash install.sh
```

Log out and back in after installing if prompted.

> **Immutable distros (Bazzite, Silverblue, etc.):** the installer skips system packages automatically since GStreamer, ffmpeg and PipeWire are already pre-installed. Python deps go to your home directory as usual.

---

## Usage

```bash
python3 main.py
```

The window appears bottom-right and the tray icon goes live. On first launch a screen picker appears — select your monitor.

To set your hotkey, open **Settings (⚙)** and click **Open KDE Shortcuts…**, or go to **KDE System Settings → Shortcuts → Global Shortcuts → replayd** and bind the `save-clip` action to any key you like. Press it after something happens and the clip saves automatically.

---

## Hotkey support

The global hotkey uses the **xdg-desktop-portal GlobalShortcuts** portal (requires xdg-desktop-portal ≥ 1.18):

| Compositor | Hotkey support |
|---|---|
| KDE Plasma 6 | ✅ Full support |
| GNOME 45+ | ✅ Full support |
| Hyprland | ⚠️ Partial — depends on xdph version; may not work |
| Sway / wlroots | ❌ GlobalShortcuts portal not supported |

On unsupported compositors the app starts normally and logs a warning — the **Save Clip** button in the window always works as a fallback.

---

## Configuration

Use the **⚙** button in the app. Changes take effect after a restart (the app restarts itself).

| Field | Default | Description |
|---|---|---|
| `seconds_before` | `30` | Seconds before hotkey to include |
| `seconds_after` | `30` | Seconds after hotkey before saving |
| `output_dir` | `~/Videos/Replayd` | Where clips go |
| `output_format` | `mp4` | `mp4` or `mkv` |
| `video_codec` | `h264` | `h264`, `h265`, `av1`, `h264_soft` |
| `audio_mode` | `both` | `game`, `mic`, or `both` |
| `recording_resolution` | `native` | Downscale before encoding (`1280x720`, etc.) |
| `video_bitrate_kbps` | `0` | Bitrate cap — `0` means encoder default |

---

## Troubleshooting

**Hotkey not working** — check that `xdg-desktop-portal` is running and supports GlobalShortcuts (requires xdg-desktop-portal ≥ 1.18, KDE Plasma 6 or GNOME 45+). On first launch the compositor may ask you to confirm the binding. The **Save Clip** button always works as fallback. On Sway/wlroots the portal is not supported — use the button.

**Black screen / no video** — a screen picker appears on first launch, select your monitor. If it never showed up: `systemctl --user status xdg-desktop-portal`. To change source later, open Settings.

**"Wayland not detected"** — make sure you're on a native Wayland session (e.g. "Plasma (Wayland)", not "Plasma (X11)").

**"PipeWire is not running"** — `systemctl --user start pipewire pipewire-pulse wireplumber`

**Codec not found** — open Settings, switch to H.264 (Software). The app also auto-detects and falls back on startup.

---

## Dependencies

Python: `dbus-next`, `pulsectl`, `PyQt6`, `qasync`

System: `ffmpeg`, `gstreamer` + plugins (base, good, bad, libav, vaapi, pipewire), `pipewire`, `wireplumber`

---

## License

GPL v3 — see [LICENSE](LICENSE).

© 2026 rgtd-faustino