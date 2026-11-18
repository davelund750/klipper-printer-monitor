#!/usr/bin/env bash
# Klipper Printer Monitor - Raspberry Pi kiosk installer.
#
# Run this ON THE PI, from inside the project folder:
#   ./install.sh [printer-address]
#
# The address is optional: without one, it keeps the address already saved
# (or none on a first install: the setup wizard asks). It can also be changed later from
# the gear button on the kiosk's screen.
#
# Sets up: (1) a systemd service that serves the dashboard over
# http://localhost:8000 and relays the printer's data, (2) a kiosk autostart
# entry that launches Chromium against it, (3) a tidy kiosk desktop ("Homing..."
# wallpaper, no icons, hidden taskbar, no notifications, invisible pointer),
# (4) a "Warming Up..." boot splash, (5) permissions for the on-screen setup
# wizard (Wi-Fi, country, time zone), (6) casting: go2rtc (the camera for
# cast screens) and pychromecast, (7) a weekly self-update from GitHub
# (Sundays 4 AM, then a reboot), (8) screen blanking disabled and desktop
# autologin enabled so it comes up hands-free after a reboot.
#
# It leaves Wi-Fi alone: Raspberry Pi Imager sets it up, and after that the
# kiosk's setup wizard does, on the touchscreen.

set -euo pipefail

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [printer-address]" >&2
  exit 1
fi
PRINTER_HOST="${1:-}"
if [ -n "$PRINTER_HOST" ] && ! [[ "$PRINTER_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "That doesn't look like an IP address or host name: $PRINTER_HOST" >&2
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="${SUDO_USER:-$USER}"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"

if [ -z "$APP_HOME" ]; then
  echo "Couldn't resolve a home directory for user $APP_USER." >&2
  exit 1
fi

echo "==> Installing Klipper Printer Monitor kiosk"
echo "    App folder : $APP_DIR"
echo "    Running as : $APP_USER ($APP_HOME)"
echo

# ---- printer address ----
# Saved where the settings screen saves it (server.py reads it from there).
if [ -n "$PRINTER_HOST" ]; then
  echo "==> Saving the printer address ($PRINTER_HOST)"
  if ! curl -fsS -o /dev/null --max-time 5 "http://$PRINTER_HOST:7125/server/info"; then
    echo "    Warning: the printer didn't answer at http://$PRINTER_HOST:7125."
    echo "    Saving it anyway; the dashboard will keep retrying."
  fi
  sudo -u "$APP_USER" python3 - "$APP_HOME/.local/share/klipper-printer-monitor/settings.json" "$PRINTER_HOST" <<'PY'
import json, os, sys
path, host = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        settings = json.load(f)
except (FileNotFoundError, ValueError):
    settings = {}
settings["printerHost"] = host
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".tmp", "w") as f:
    json.dump(settings, f)
os.replace(path + ".tmp", path)
PY
  echo
fi

# ---- emoji font ----
# The setup wizard's country flags and Wi-Fi lock are emoji, and Raspberry Pi
# OS ships without a color emoji font, so Chromium would draw them as empty
# boxes.
echo "==> Installing emoji font (fonts-noto-color-emoji)"
sudo apt-get install -y fonts-noto-color-emoji
echo

# ---- server (systemd) ----
echo "==> Installing systemd service (klipper-printer-monitor.service)"
sudo tee /etc/systemd/system/klipper-printer-monitor.service >/dev/null <<EOF
[Unit]
Description=Klipper Printer Monitor server
After=network-online.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/server/server.py
Restart=on-failure
User=$APP_USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable klipper-printer-monitor
# restart (not just start) so a re-run picks up a changed server
sudo systemctl restart klipper-printer-monitor
echo

# ---- chromium kiosk autostart ----
echo "==> Installing kiosk autostart entry"
CHROMIUM_BIN="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$CHROMIUM_BIN" ]; then
  echo "Couldn't find chromium-browser or chromium on PATH." >&2
  echo "Install it first, e.g.: sudo apt install -y chromium-browser" >&2
  echo "...then re-run this script." >&2
  exit 1
fi

mkdir -p "$APP_HOME/.config/autostart"
cat > "$APP_HOME/.config/autostart/klipper-printer-monitor.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Klipper Printer Monitor
Exec=$APP_DIR/kiosk/kiosk.sh
X-GNOME-Autostart-enabled=true
EOF
sudo chown "$APP_USER":"$APP_USER" "$APP_HOME/.config/autostart/klipper-printer-monitor.desktop"
chmod +x "$APP_DIR/kiosk/kiosk.sh"
echo "    Using $CHROMIUM_BIN"
echo

# ---- kiosk desktop ----
# Tidies what shows around the kiosk, mostly while the Pi boots: a "Homing..."
# wallpaper with no desktop icons, an auto-hiding taskbar with its pop-up
# notifications off, and an invisible mouse pointer. Each is a personal setting layered
# over the system defaults, which are left untouched.
echo "==> Setting up the kiosk desktop (wallpaper, taskbar, notifications, pointer)"

PCMANFM_DIR="$APP_HOME/.config/pcmanfm/default"
mkdir -p "$PCMANFM_DIR"
for src in /etc/xdg/pcmanfm/default/desktop-items-*.conf; do
  [ -e "$src" ] || continue
  sed -e "s|^wallpaper=.*|wallpaper=$APP_DIR/kiosk/wallpaper.png|" \
      -e 's|^wallpaper_mode=.*|wallpaper_mode=crop|' \
      -e 's|^desktop_bg=.*|desktop_bg=#0d0d11111717|' \
      -e 's|^show_trash=.*|show_trash=0|' \
      -e 's|^show_mounts=.*|show_mounts=0|' \
      "$src" > "$PCMANFM_DIR/$(basename "$src")"
done

PANEL_INI="$APP_HOME/.config/wf-panel-pi/wf-panel-pi.ini"
mkdir -p "$(dirname "$PANEL_INI")"
[ -s "$PANEL_INI" ] || printf '[panel]\n' > "$PANEL_INI"
# Sets key=value, assuming [panel] is the file's only section.
set_panel_key() {
  if grep -q "^$1=" "$PANEL_INI"; then
    sed -i "s|^$1=.*|$1=$2|" "$PANEL_INI"
  else
    echo "$1=$2" >> "$PANEL_INI"
  fi
}
set_panel_key autohide true
set_panel_key notify_enable false
set_panel_key notify_libnotify false

# Pointer: an invisible pointer theme, since hiding it in the page only works
# once the pointer moves, and on a touchscreen it never does.
HOME="$APP_HOME" python3 "$APP_DIR/kiosk/make_blank_cursor_theme.py"
LABWC_ENV="$APP_HOME/.config/labwc/environment"
mkdir -p "$(dirname "$LABWC_ENV")"
touch "$LABWC_ENV"
sed -i '/^XCURSOR_THEME=/d' "$LABWC_ENV"
echo "XCURSOR_THEME=klipper-monitor-blank" >> "$LABWC_ENV"

sudo chown -R "$APP_USER":"$APP_USER" \
  "$APP_HOME/.config/pcmanfm" "$APP_HOME/.config/wf-panel-pi" \
  "$APP_HOME/.config/labwc" "$APP_HOME/.local/share/icons/klipper-monitor-blank"
echo

# ---- boot splash ----
# Replaces the "Welcome to the Raspberry Pi Desktop" boot screen with a
# custom splash theme (kiosk/boot-splash/). Switching themes rebuilds the
# initramfs, which takes a minute, so skip it when the theme is already
# installed and unchanged. Undo with: sudo plymouth-set-default-theme -R pix
echo "==> Installing boot splash"
SPLASH_DIR=/usr/share/plymouth/themes/klipper-monitor
if [ "$(plymouth-set-default-theme 2>/dev/null)" = "klipper-monitor" ] \
  && diff -rq "$APP_DIR/kiosk/boot-splash" "$SPLASH_DIR" >/dev/null 2>&1; then
  echo "    Already installed"
else
  sudo mkdir -p "$SPLASH_DIR"
  sudo cp "$APP_DIR"/kiosk/boot-splash/* "$SPLASH_DIR/"
  sudo plymouth-set-default-theme -R klipper-monitor
fi
echo

# ---- casting ----
# Sending the dashboard to TVs and Nest Hubs (server/casting.py). Two parts:
#  - go2rtc, which re-wraps the K2's camera so a Cast device can play it. A
#    single program, pinned to one release and checked against its SHA-256.
#    It listens only on this Pi (its API can add sources that run commands),
#    and the dashboard's server relays the one camera stream to cast screens.
#  - pychromecast, which finds the screens and casts to them.
# Casting is skipped (not offered) if either can't be installed.
echo "==> Setting up casting (go2rtc, pychromecast)"
GO2RTC_VERSION=v1.9.14
case "$(uname -m)" in
  aarch64) GO2RTC_ASSET=go2rtc_linux_arm64
           GO2RTC_SHA256=359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50 ;;
  armv7l)  GO2RTC_ASSET=go2rtc_linux_arm
           GO2RTC_SHA256=4d7e1639af5a2722a28e864468fd8099b3c1682565446c798bf9e3b38fde12e4 ;;
  *)       GO2RTC_ASSET="" ;;
esac
GO2RTC_BIN=/usr/local/bin/go2rtc
if [ -z "$GO2RTC_ASSET" ]; then
  echo "    No go2rtc build listed for $(uname -m); cast screens won't show the K2's camera."
elif [ -x "$GO2RTC_BIN" ] && echo "$GO2RTC_SHA256  $GO2RTC_BIN" | sha256sum -c --status; then
  echo "    go2rtc $GO2RTC_VERSION already installed"
else
  GO2RTC_TMP="$(mktemp)"
  if curl -fsSL --max-time 120 -o "$GO2RTC_TMP" \
      "https://github.com/AlexxIT/go2rtc/releases/download/$GO2RTC_VERSION/$GO2RTC_ASSET" \
    && echo "$GO2RTC_SHA256  $GO2RTC_TMP" | sha256sum -c --status; then
    sudo install -m 0755 -o root -g root "$GO2RTC_TMP" "$GO2RTC_BIN"
    echo "    Installed go2rtc $GO2RTC_VERSION"
  else
    echo "    Couldn't download go2rtc (or its checksum didn't match); cast screens won't show the K2's camera." >&2
  fi
  rm -f "$GO2RTC_TMP"
fi
if [ -x "$GO2RTC_BIN" ]; then
  sudo mkdir -p /etc/klipper-printer-monitor
  sudo tee /etc/klipper-printer-monitor/go2rtc.yaml >/dev/null <<'EOF'
# Installed by klipper-printer-monitor/install.sh. Only on this Pi: the dashboard's
# server registers the printer's camera and relays it to cast screens.
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: ""
webrtc:
  listen: ""
log:
  level: warn
EOF
  sudo tee /etc/systemd/system/klipper-printer-monitor-go2rtc.service >/dev/null <<EOF
[Unit]
Description=Klipper Printer Monitor camera relay for cast screens (go2rtc)
After=network-online.target

[Service]
ExecStart=$GO2RTC_BIN -config /etc/klipper-printer-monitor/go2rtc.yaml
Restart=always
User=$APP_USER

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable klipper-printer-monitor-go2rtc
  sudo systemctl restart klipper-printer-monitor-go2rtc
fi
# Raspberry Pi OS packages pychromecast; otherwise, pip for this user only.
if ! sudo apt-get install -y python3-pychromecast; then
  sudo -u "$APP_USER" python3 -m pip install --user --break-system-packages pychromecast \
    || echo "    Couldn't install pychromecast; casting won't be offered." >&2
fi
sudo systemctl restart klipper-printer-monitor  # so the server finds pychromecast
echo

# ---- setup wizard permissions ----
# The on-screen setup wizard (setup.html) is served by server.py, running as
# $APP_USER without anyone logged in, so by default every change it asks for
# needs a password. Grant exactly what it uses, nothing more:
#  - a polkit rule: scan for and join Wi-Fi networks, set the time zone;
#  - a polkit rule: start the update service (klipper-printer-monitor-update.service, below), so
#    setup can offer to check for updates right away;
#  - a sudo rule: read and set the Wi-Fi country with raspi-config, and only
#    with a two-letter code (the server checks that too).
echo "==> Allowing the setup wizard to change Wi-Fi, country, and time zone"
sudo tee /etc/polkit-1/rules.d/50-klipper-printer-monitor.rules >/dev/null <<EOF
// Installed by klipper-printer-monitor/install.sh: lets the kiosk's setup wizard
// (server.py, running as $APP_USER) manage Wi-Fi, set the time zone, and
// start the update service, without a password.
polkit.addRule(function (action, subject) {
  var allowed = [
    "org.freedesktop.NetworkManager.wifi.scan",
    "org.freedesktop.NetworkManager.network-control",
    "org.freedesktop.NetworkManager.enable-disable-wifi",
    "org.freedesktop.NetworkManager.settings.modify.system",
    "org.freedesktop.timedate1.set-timezone"
  ];
  if (subject.user === "$APP_USER" && allowed.indexOf(action.id) >= 0) {
    return polkit.Result.YES;
  }
  // Starting the update service, and only that service.
  if (subject.user === "$APP_USER" && action.id === "org.freedesktop.systemd1.manage-units" &&
      action.lookup("unit") === "klipper-printer-monitor-update.service" && action.lookup("verb") === "start") {
    return polkit.Result.YES;
  }
});
EOF
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
# Installed by klipper-printer-monitor/install.sh: lets the kiosk's setup wizard read
# and set the Wi-Fi country (two capital letters only, e.g. US).
$APP_USER ALL=(root) NOPASSWD: /usr/bin/raspi-config nonint get_wifi_country, /usr/bin/raspi-config nonint do_wifi_country [A-Z][A-Z]
EOF
# Only install it if sudo accepts it; a broken sudoers file can lock out sudo.
if sudo visudo -cf "$SUDOERS_TMP" >/dev/null; then
  sudo install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/klipper-printer-monitor
else
  echo "    Couldn't install the sudo rule; the wizard won't be able to set the country." >&2
fi
rm -f "$SUDOERS_TMP"
echo

# ---- weekly self-update ----
# Every Sunday at 4 AM (the Pi's local time), update.sh switches to the
# newest release on GitHub, re-runs this installer if anything changed, and
# reboots. Only possible when this folder is a git checkout.
echo "==> Installing weekly update timer (Sundays 4:00 AM)"
if [ -d "$APP_DIR/.git" ]; then
  chmod +x "$APP_DIR/kiosk/update.sh"
  sudo tee /etc/systemd/system/klipper-printer-monitor-update.service >/dev/null <<EOF
[Unit]
Description=Klipper Printer Monitor weekly update and reboot
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=$APP_DIR/kiosk/update.sh
EOF
  sudo tee /etc/systemd/system/klipper-printer-monitor-update.timer >/dev/null <<EOF
[Unit]
Description=Run the Klipper Printer Monitor weekly update

[Timer]
OnCalendar=Sun *-*-* 04:00:00
# If the Pi was off at 4 AM, don't catch up at power-on (it would reboot
# right after starting).
Persistent=false

[Install]
WantedBy=timers.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now klipper-printer-monitor-update.timer
  echo "    Next run: $(systemctl show klipper-printer-monitor-update.timer -p NextElapseUSecRealtime --value)"
else
  echo "    Skipped: $APP_DIR isn't a git checkout, so it can't fetch releases."
  echo "    Clone the project from GitHub instead of copying it to enable updates."
fi
echo

# ---- screen blanking + autologin ----
echo "==> Disabling screen blanking and enabling desktop autologin"
sudo raspi-config nonint do_blanking 1 \
  || echo "    Couldn't set automatically — set manually via: sudo raspi-config > Display Options > Screen Blanking"
sudo raspi-config nonint do_boot_behaviour B4 \
  || echo "    Couldn't set automatically — set manually via: sudo raspi-config > System Options > Boot / Auto Login > Desktop Autologin"
echo

echo "==> Done. Reboot to launch the kiosk:"
echo "    sudo reboot"
