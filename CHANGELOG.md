# Changelog

Notable changes to this project, newest first. Installed kiosks install the
newest release in their weekly Sunday update (see
[Updates](README.md#updates)). "Unreleased" lists changes on `main` that
aren't in a release yet, so no kiosk has them.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and version numbers follow [Semantic Versioning](https://semver.org).

## Unreleased

No current unreleased changes.

## 1.0.0 - 2026-11-17

First release.

### Added
- A kiosk dashboard for any 3D printer running Klipper and Moonraker, made
  for a Raspberry Pi with the 7-inch 800x480 touchscreen: the printer's
  camera, the print's progress, time left and finish time, layer, nozzle, bed
  and chamber temperatures, and a banner for errors, pauses and finished
  prints. It finds the printer's chamber sensor, light, web interface and
  webcam itself. Tap the camera to fill the screen.
- Progress and time left from the slicer's per-layer times and the moves'
  own time (distance ÷ feed rate), counted from the first layer, rather than
  the file's bytes. Progress through the current layer shows as a fill
  behind the Layer row, with the time the layer has left.
- A layer preview while printing: a drawing of the current layer, with
  what's printed so far in each filament's colour and a dot at the nozzle,
  beside or instead of the camera. Its background (Settings → Display) is
  Auto, which lightens a few shades for black filament, or one of five dark
  shades.
- The model's preview picture, from its slicer, in the corner of the camera.
- Each temperature shows its change over the last minute, over a faint graph
  of the last 10 minutes.
- A Controls view: pause, resume and cancel (with a confirmation), heater
  targets with presets and ±5°, cool down, home, motors off, and moving X, Y
  and Z in 0.1, 1 or 10 mm steps, or to a typed position.
- A light button for the printer's light (a light pin, LED strip or power
  device).
- Errors: Klipper's `!!` lines and Creality's error reports, kept up while
  the job is paused or stopped on the error, and otherwise for 2 minutes. A
  finished print turns to Idle once the printer's light goes off, or 30
  minutes after it ends.
- For Creality printers such as the K2: a CFS panel (each unit's humidity,
  and a bar per slot with the filament's colour, how much is left and which
  slot is in use), the layer preview in the colours of the spools actually
  printing, and Creality's WebRTC camera, which reconnects by itself when its
  picture stalls.
- Casting: a cast button sends the dashboard to Chromecasts, Google TVs and
  Nest Hubs on the network, view-only, with the camera and layer preview
  stacked while printing. Screens that drop it are cast to again.
- Looks: Standard, or Star Trek panels, LCARS (TNG's Federation consoles) or
  Klingon, which can speak Klingon too. Cast screens follow it.
- First-time setup on the touchscreen: country, time zone, Wi-Fi, the
  printer's address (checked before it's saved), its name, and a 12- or
  24-hour clock. Settings (press and hold the gear) covers the same, plus
  About (version, update now, what's new) and a factory reset.
- A Raspberry Pi installer (`install.sh`): the dashboard as a service,
  Chromium in kiosk mode, a "Warming Up…" boot splash and a "Homing…"
  wallpaper, and a weekly self-update to the newest release (Sundays at
  4 AM, then a restart).
- `make start`, `make stop`, `make restart` and `make logs` run the
  dashboard in the background on your computer; `make run` runs it in the
  terminal.

### Security
- Settings, printer controls and the Pi's own settings can only be changed
  from the kiosk itself, not from other devices on the network.
- The camera's address must be an ordinary http(s) address and its frame is
  sandboxed, so a changed webcam setting on the printer can't run code in
  the dashboard. The camera relay for cast screens (go2rtc) only answers the
  dashboard's own server.

