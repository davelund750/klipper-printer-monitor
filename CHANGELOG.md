# Changelog

Notable changes to this project, newest first. Installed kiosks install the
newest release in their weekly Sunday update (see
[Updates](README.md#updates)). "Unreleased" lists changes on `main` that
aren't in a release yet, so no kiosk has them.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and version numbers follow [Semantic Versioning](https://semver.org).

## Unreleased

### Changed
- Renamed from K2 Printer Monitor to Klipper Printer Monitor
  (`klipper-printer-monitor`), since it works with any Klipper printer. Its
  environment variables start with `KPM_` (`KPM_HOST`, `KPM_DATA_DIR`, ...).
- No printer address is built in: until one is saved, the dashboard says
  none is set up, and the setup wizard asks for it.
- Only Creality's own error reports count as errors, besides Klipper's `!!`
  lines: board names such as SB2209 or RP2040 in other printers' messages
  no longer show as errors.

### Security
- The camera's address (from the printer's webcam settings, which anyone on
  the network can usually change) must be an ordinary http(s) address, and
  the camera frame is sandboxed, so it can't run code in the dashboard.
- The preview picture endpoint only reads from the printer's G-code folder.
- The kiosk's go2rtc no longer accepts requests from other web pages.
- The test workflow on GitHub has read-only access.

### Added
- Settings → Display → Layer preview background: Auto (as before), or always
  one of five dark shades, A (darkest) to E.
- `make start` runs the dashboard in the background on your computer, with
  its output in `.run/`; `make logs` follows it. `make restart` now starts it
  in the background too. `make run` still runs it in the terminal.
- Star Trek looks under Settings → Display → Look: LCARS (TNG's Federation
  consoles) or Klingon, besides the standard one. Cast screens follow it.
  The Klingon look's dashboard speaks Klingon, or English if you'd rather
  (Words, under the Look).
- Progress through the current layer: a fill behind the Layer row, and the
  time the layer has left beside its label.
- Progress and time left go by the slicer's per-layer times rather than the
  file's bytes, which could be hours out (a clock print read 9% and 11h left
  when it was 44% through with under 6h to go).
- A finished print turns to Idle once the printer's light goes off after
  it, or 30 minutes after it ends, rather than staying Complete until the
  next job.
- A dashboard for a Klipper printer, made for the 7-inch 800x480
  touchscreen: the printer's camera, the print's progress, time left and
  finish time, layer, nozzle, bed and chamber temperatures, and a banner for
  errors, pauses and finished prints. Tap the camera to fill the screen.
  Made for the Creality K2 Pro; works with any printer running Klipper and
  Moonraker, finding its chamber sensor, web interface and webcam itself.
- A CFS panel: each unit's humidity, and a bar per slot showing the
  filament's color and how much is left, with a caret under the slot in use.
- First-time setup on the touchscreen: country, time zone, Wi-Fi, the
  printer's address (checked before it's saved), its name for the top of
  the dashboard (starting from the name the printer goes by), and a 12- or
  24-hour clock.
- Settings (press and hold the gear): Printer, Wi-Fi, Location, Display,
  About (version, update now, what's new), and a factory reset.
- Weekly self-update: every Sunday at 4 AM the kiosk installs the newest
  release, then restarts.
- Casting: a cast button sends the dashboard to Chromecasts, Google TVs
  and Nest Hubs on the network, view-only, with the camera and layer preview
  stacked while printing. Screens that drop it are cast to again.
- The K2's camera reconnects by itself when its picture stalls (for
  example after a long idle night), and pressing and holding the camera
  restarts it by hand.
- A Controls view, opened from the title bar: pause, resume and cancel
  (with a confirmation), heater targets with presets and ±5°, cool down,
  home, motors off, and moving X, Y and Z in 0.1, 1 or 10 mm steps. Tap a
  heater's target or an axis's position to type a value on a number pad.
- A layer preview while printing: a Preview button in the camera's corner
  swaps it for a drawing of the current layer, with what's printed so far
  highlighted and a dot at the nozzle.
- The model's preview picture, from its slicer, in the corner of the
  camera while it prints.
- Each temperature shows its change over the last minute, over a faint
  graph of the last 10 minutes.
- A light button in the title bar, for the printer's light (the K2's LED,
  or a light pin, LED strip or power device on other printers).
- A "Warming Up…" boot splash and a "Homing…" desktop wallpaper.

### Fixed
- The layer preview draws each filament in the colour of the spool it's
  printing from (by the CFS's map for the job), not the colour picked in the
  slicer. When the job's colours wouldn't show on the dark background (black
  filament, say), the preview's background turns a few shades lighter for that job
  instead, so black stays black and no longer disappears.
- The kiosk searches the network for cast screens only when the Cast menu
  opens or its Refresh button is tapped, not every minute while the menu is
  open.
- With casting set up, the server no longer logs "couldn't close the old
  connection" for every screen each minute (thousands of lines a day). The
  connections were closed; only the report was wrong.
- The finish time no longer swings by hours on prints with long stretches
  between the slicer's time marks (one clock had 5 hours of ironing in one).
  Inside a stretch, time went by the file's bytes, which race through fine
  detail and crawl through ironing; it now goes by the moves' own time
  (distance ÷ feed rate). The layer countdown is fixed the same way.
- Progress counts from the first layer: it read about 6% on a short print
  while the printer was still heating and calibrating, from the slicer's
  time for the start code.
- The layer total (and time left and filament) could come from the job
  before, e.g. "Layer 28 / 4", when the printer left that job's details in
  place. They now come from the file printing.
- An error no longer stays on the dashboard after it's cleared. It stays up
  while the job is paused or the printer is in its error state, and otherwise
  for 2 minutes, because the printer doesn't report when an error is cleared.

