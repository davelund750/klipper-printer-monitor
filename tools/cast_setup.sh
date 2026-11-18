#!/usr/bin/env bash
# Sets up casting for `make run` on your own computer (the kiosk gets it from
# install.sh): a .venv in this folder with pychromecast, which finds and
# casts to the screens, and go2rtc beside it, which converts the printer's
# camera for them (see server/casting.py). `make run` uses both once they're
# there. Run it again any time; it only fetches what's missing.
set -euo pipefail
cd "$(dirname "$0")/.."

PYCHROMECAST_VERSION=14.0.10
GO2RTC_VERSION=v1.9.14  # the same as install.sh

if [ ! -x .venv/bin/python ]; then
  echo "Making .venv"
  python3 -m venv .venv
fi
if .venv/bin/python -c "import pychromecast" 2>/dev/null; then
  echo "pychromecast is already installed"
else
  echo "Installing pychromecast $PYCHROMECAST_VERSION"
  .venv/bin/pip install --quiet "pychromecast==$PYCHROMECAST_VERSION"
fi

if [ -x .venv/bin/go2rtc ]; then
  echo "go2rtc is already installed"
  exit 0
fi
# Only builds whose checksum is known here are fetched; elsewhere, install
# go2rtc yourself (`make run` also finds one on your PATH).
case "$(uname -s)/$(uname -m)" in
  Darwin/arm64)
    asset=go2rtc_mac_arm64.zip
    sha256=919b78adc759d6b3883d1e1b2ac915ac0985bb903ff1897b4d228527bd64690c ;;
  *)
    echo "No go2rtc download known for $(uname -s)/$(uname -m): install go2rtc $GO2RTC_VERSION yourself"
    echo "for the camera on cast screens (https://github.com/AlexxIT/go2rtc/releases)."
    exit 0 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "Downloading go2rtc $GO2RTC_VERSION"
curl -fsSL -o "$tmp/$asset" "https://github.com/AlexxIT/go2rtc/releases/download/$GO2RTC_VERSION/$asset"
echo "$sha256  $tmp/$asset" | shasum -a 256 -c --status - || { echo "go2rtc's download doesn't match its checksum; not installed." >&2; exit 1; }
unzip -q "$tmp/$asset" -d "$tmp/unpacked"
install -m 755 "$tmp/unpacked/go2rtc" .venv/bin/go2rtc
echo "Installed go2rtc in .venv/bin"
