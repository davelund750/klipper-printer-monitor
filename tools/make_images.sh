#!/usr/bin/env bash
# Renders the boot splash and desktop wallpaper from tools/splash_art.html
# with headless Chrome (on a Mac; needs internet for the fonts):
#   kiosk/boot-splash/splash.png   "Warming Up…"   shown while the Pi boots
#   kiosk/wallpaper.png            "Homing…"       the desktop behind Chromium,
#                                                  seen while it starts
# Usage: tools/make_images.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PAGE="file://$ROOT/tools/splash_art.html"

render() {  # render <text> <output.png>
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size=800,480 --virtual-time-budget=5000 \
    --screenshot="$2" "$PAGE?text=$1" 2>/dev/null
  echo "Wrote $2"
}

render "Warming%20Up" "$ROOT/kiosk/boot-splash/splash.png"
render "Homing" "$ROOT/kiosk/wallpaper.png"
