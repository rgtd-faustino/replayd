#!/usr/bin/env bash
set -e

echo "=== Replayd Linux - Installer (native / non-Flatpak) ==="
echo
echo "If you installed via Flatpak, you do not need this script."
echo

# ── detect immutable / atomic distro ─────────────────────────────────────────
IS_IMMUTABLE=false
if [ -f /run/ostree-booted ] || \
   grep -qiE 'bazzite|silverblue|kinoite|ublue|aurora|startingpoint' /etc/os-release 2>/dev/null || \
   { mountpoint -q / && findmnt -n -o OPTIONS / 2>/dev/null | grep -q '\bro\b'; }; then
    IS_IMMUTABLE=true
fi

# ── detect package manager ────────────────────────────────────────────────────
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v pacman &>/dev/null; then
    PKG_MGR="pacman"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v zypper &>/dev/null; then
    PKG_MGR="zypper"
else
    echo "ERROR: unsupported distro (no apt/pacman/dnf/zypper found)"
    exit 1
fi

echo "[1/4] Installing system packages..."

if $IS_IMMUTABLE; then
    echo ""
    echo "  *** Immutable / atomic distro detected (e.g. Bazzite, Silverblue, Kinoite) ***"
    echo "  Skipping system package installation — the rootfs is read-only."
    echo "  The required packages (GStreamer, ffmpeg, PipeWire) should already be"
    echo "  present on Bazzite and most ublue-based images."
    echo ""
    echo "  If something is missing, install it with:"
    echo "    rpm-ostree install <package>  (then reboot)"
    echo "  or via a Distrobox container for testing."
    echo ""
else
    case "$PKG_MGR" in
        apt)
            sudo apt-get update -qq
            sudo apt-get install -y \
                ffmpeg \
                python3-pip \
                python3-dev \
                python3-pyqt6 \
                libdbus-1-dev \
                pipewire \
                pipewire-pulse \
                wireplumber \
                gstreamer1.0-tools \
                gstreamer1.0-plugins-base \
                gstreamer1.0-plugins-good \
                gstreamer1.0-plugins-bad \
                gstreamer1.0-plugins-ugly \
                gstreamer1.0-vaapi \
                gstreamer1.0-pipewire \
                gstreamer1.0-libav

            # fdkaac is not in the main repos on Ubuntu/Debian due to patents.
            echo "[apt] Attempting to install gstreamer1.0-fdkaac (may not be available)..."
            sudo apt-get install -y gstreamer1.0-fdkaac 2>/dev/null || {
                echo ""
                echo "  WARNING: gstreamer1.0-fdkaac not found in your repos (patent restrictions)."
                echo "     Audio encoding will fall back to AAC via gstreamer1.0-libav."
                echo "     If you want fdkaac: add a third-party PPA or rebuild GStreamer bad plugins."
                echo ""
            }
            ;;

        pacman)
            sudo pacman -Sy --noconfirm \
                ffmpeg \
                python-pip \
                python-pyqt6 \
                dbus \
                pipewire \
                pipewire-pulse \
                wireplumber \
                gstreamer \
                gst-plugins-base \
                gst-plugins-good \
                gst-plugins-bad \
                gst-plugins-ugly \
                gst-plugin-pipewire \
                gst-plugin-va \
                gst-libav

            # fdkaac via AUR (optional, requires an AUR helper)
            if command -v yay &>/dev/null; then
                echo "[pacman] Installing gst-plugin-fdkaac from AUR..."
                yay -S --noconfirm gst-plugin-fdkaac 2>/dev/null || \
                    echo "  WARNING: AUR install failed - audio will fall back to gst-libav AAC."
            elif command -v paru &>/dev/null; then
                echo "[pacman] Installing gst-plugin-fdkaac from AUR..."
                paru -S --noconfirm gst-plugin-fdkaac 2>/dev/null || \
                    echo "  WARNING: AUR install failed - audio will fall back to gst-libav AAC."
            else
                echo "  WARNING: No AUR helper found (yay/paru). Skipping fdkaac."
                echo "     Install manually from AUR if needed: gst-plugin-fdkaac"
            fi
            ;;

        dnf)
            # Enable RPM Fusion repos if they are not already enabled (needed for ffmpeg + gstreamer ugly/bad)
            if ! rpm -q rpmfusion-free-release &>/dev/null; then
                echo "[dnf] Enabling RPM Fusion (free)..."
                sudo dnf install -y \
                    "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
            fi
            if ! rpm -q rpmfusion-nonfree-release &>/dev/null; then
                echo "[dnf] Enabling RPM Fusion (nonfree)..."
                sudo dnf install -y \
                    "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm"
            fi

            sudo dnf install -y \
                ffmpeg \
                python3-pip \
                python3-pyqt6 \
                python3-devel \
                dbus-devel \
                pipewire \
                pipewire-pulseaudio \
                wireplumber \
                gstreamer1 \
                gstreamer1-plugins-base \
                gstreamer1-plugins-good \
                gstreamer1-plugins-bad-free \
                gstreamer1-plugins-bad-nonfree \
                gstreamer1-plugins-ugly \
                gstreamer1-vaapi \
                gstreamer1-plugin-pipewire \
                gstreamer1-libav \
                gstreamer1-plugin-libav \
                fdk-aac-free \
                gstreamer1-plugins-bad-freeworld
            ;;

        zypper)
            # openSUSE Tumbleweed / Leap
            # Packman repo provides ffmpeg and GStreamer ugly/bad codecs
            if ! zypper repos | grep -q packman; then
                echo "[zypper] Adding Packman repository..."
                sudo zypper ar -cfp 90 \
                    "https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Tumbleweed/" \
                    packman
                sudo zypper --gpg-auto-import-keys refresh
            fi

            sudo zypper install -y \
                ffmpeg \
                python3-pip \
                python3-qt6 \
                python3-devel \
                dbus-1-devel \
                pipewire \
                pipewire-pulseaudio \
                wireplumber \
                gstreamer \
                gstreamer-plugins-base \
                gstreamer-plugins-good \
                gstreamer-plugins-bad \
                gstreamer-plugins-ugly \
                gstreamer-plugins-vaapi \
                gstreamer-plugin-pipewire \
                gstreamer-plugins-libav

            # fdkaac via Packman
            sudo zypper install -y gstreamer-plugins-bad-fdk 2>/dev/null || {
                echo "  WARNING: gstreamer-plugins-bad-fdk not found."
                echo "     Audio encoding will fall back to gstreamer-plugins-libav AAC."
            }
            ;;
    esac
fi

echo ""
echo "[2/4] Installing Python dependencies..."
# PyQt6 is installed above via the system package manager where possible.
# pip install covers dbus-next, pulsectl, qasync, and PyQt6 as fallback.
pip3 install --user -r requirements.txt

echo ""
echo "[3/4] Creating output directory..."
mkdir -p ~/Videos/Replayd

echo ""
echo "Done! Next steps:"
echo "  1. Edit config.json to set seconds_before/after, codec, etc."
echo "     (or use the Settings button inside the app)"
echo "  2. Run:  python3 main.py"
echo "  3. On first launch the screen picker appears once — select your monitor."
echo ""
echo "  4. Setting the hotkey:"
echo "     - KDE Plasma: Settings (⚙) → Open KDE Shortcuts…"
echo "       or: System Settings → Shortcuts → Global Shortcuts → replayd"
echo "     - GNOME: Settings (⚙) opens a note; bind via GNOME Settings → Keyboard → Shortcuts"
echo "     - Other compositors: use the Save Clip button in the window (hotkey portal may not be supported)"
echo ""
echo "How it works:"
echo "  - main.py records your screen in a rolling buffer the whole time it runs."
echo "  - Press your configured hotkey AFTER something happens."
echo "  - It saves the last seconds_before seconds + waits seconds_after more,"
echo "    then drops the clip in ~/Videos/Replayd/ automatically."
echo "  - The buffer is pruned automatically - it won't fill your disk."
echo ""
echo "Hotkeys use xdg-desktop-portal GlobalShortcuts (no 'input' group needed)."
echo "Requires xdg-desktop-portal >= 1.18 with KDE Plasma 6 or GNOME 45+."