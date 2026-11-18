# Klipper Printer Monitor

A kiosk dashboard for a 3D printer running Klipper and Moonraker, built to
run full-screen on a Raspberry Pi with the same 7-inch 800x480 touchscreen as
[Disney Parks Wait Times](../disney-parks-wait-times). It shows the printer's
camera next to the print's progress, time left, layer and temperatures, with
a banner for errors, pauses and finished prints. It was made for a Creality
K2 Pro, and shows its CFS too.

## Which printers

Any printer running Klipper with Moonraker (the API behind Fluidd and
Mainsail) on your network. What it shows depends on what the printer has:

| | Creality K2 (and similar Creality printers) | Other Klipper printers |
|---|---|---|
| Progress, time left, temperatures, errors | Yes | Yes |
| Layer X of Y | Yes | When the slicer's G-code reports layers (`SET_PRINT_STATS_INFO`); otherwise the total only, from the file's details |
| Progress through the current layer | Yes, with its time left (Creality Print's per-layer times) | Yes; its time left too with Orca or another slicer that writes `;TIME_ELAPSED:` marks |
| Chamber temperature | Yes | When there's a `temperature_sensor`, `heater_generic` or `temperature_fan` with "chamber" in its name; otherwise the row is hidden |
| Camera | Creality's WebRTC camera, connected directly and watched for stalls | The first webcam set up in Fluidd or Mainsail: an MJPEG stream (crowsnest, mjpg-streamer), or another kind's own page in a frame |
| CFS panel | Yes | Hidden |
| Light button | Yes (its `LED` pin) | An `output_pin` with "led", "light" or "lamp" in its name, else an LED strip (`neopixel`, `dotstar`, `led`, ...), else a Moonraker power device named for a light; otherwise hidden |

It finds the printer's Fluidd or Mainsail on the usual ports (80, 81, 4408,
4409), so camera links set up there work. The server checks what a printer
has when it first talks to it, and again every 10 minutes.

It's plain HTML/CSS/JS with no build step, plus a small Python server
(standard library only) that runs on the Pi.

## How it works

```
Chromium (kiosk) ──> server.py on the Pi ──> Moonraker on the printer (port 7125)
       └──────── camera ────────────────────> the printer's webcam
```

- **The server** (`server/server.py`) serves the page and relays the printer's
  Moonraker API, which doesn't allow cross-origin requests from a browser.
  Besides **reading** from the printer (status, the console, the job
  history), it sends commands for exactly two things, both only from the
  kiosk's own screen: the light button, and the Controls view (below).
  - `GET /api/status`: state, progress, layer X of Y, elapsed and time left,
    finish time, nozzle/bed/chamber temperatures, each CFS unit's humidity
    and slots (color, filament remaining, which one is loaded), the last 3
    errors, and the last job when idle.
  - `GET /api/config`: the printer's address and name, its Fluidd or
    Mainsail, and its camera.
  - `GET`/`PUT /api/settings`, `PUT /api/printer`, `/api/system/*` and
    `GET /api/changelog`: for the setup wizard and Settings (see the top of
    `server.py`). Every change only answers the kiosk itself.
- **The page** polls `/api/status` every 2 seconds.
- **Status:** Printing, Paused, Complete, Cancelled, Error, Idle, or
  **Busy**: no print job, but Klipper is running commands (calibration, a
  bed mesh, homing, a macro). Klipper's print status alone says "standby"
  then, so the dashboard also checks whether it's executing anything. A
  K2's pre-print routine pauses for up to half a minute between steps, so
  Busy holds for 60 seconds after Klipper was last running commands. (Its
  post-print cutting and unloading happen while it still reports
  Printing, then it goes straight to Complete.) Klipper keeps saying
  Complete until the next job starts, so the dashboard shows **Idle**
  instead once the printer's light goes off after the print (the K2 turns
  it off a while later), or 30 minutes after the end if the light doesn't
  go off or the printer has none.
- **Temperatures** show their change over the last minute beside their
  label ("Bed −0.8/min"; nothing when steadier than 0.2°), and a faint graph
  behind each row of the last 10 minutes, with the target dashed when one is
  set. The graph starts from Moonraker's own temperature history
  (`GET /api/history`), so it's full as soon as the page loads.
- **Missed readings:** the printer is occasionally slow to answer (e.g. as
  a print starts), so "Can't reach the printer" only shows after 3 missed
  readings in a row, about 6 seconds.
- **The last job** ("Finished Today 10:08 AM") shows while the printer is
  Idle, until the next print starts. It's hidden while Busy, which is
  usually the next job getting ready: the K2 only names a new job once its
  pre-print routine is done and printing begins.
- **Progress and time left** go by the slicer's own times when the file has
  them (`;TIME_ELAPSED:` marks at each layer's end, from Creality Print and
  Orca): how far through the slicer's estimate Klipper's position in the
  file is, and the rest of it, scaled by how the print is keeping pace once
  it's 5 minutes in, and counted from the first layer. Between two marks,
  which can be hours apart, the time is shared out by the moves' own time
  (distance ÷ feed rate), not the bytes. Klipper's own percentage is by the
  file's bytes, which goes wrong when layers differ: an ironed top can take
  hours for few bytes. Until the file has been read (a few seconds per job), and for files
  without the marks, the percentage is Klipper's, and time left blends the
  slicer's total estimate (better early on) with the time per percent so far
  (better later).
- **Errors** are console lines starting with `!!` or containing a Creality
  code (TR2863, FR0122, key712, ...). The printer doesn't report when one is
  cleared, so an error stays up while the job is paused or the printer is in
  its error state, and otherwise for 2 minutes.
- **The CFS panel** has a half per connected unit: its humidity (amber at
  40% or more), then a bar per slot filled to the filament remaining, in the
  filament's color. A caret points at the slot feeding the toolhead. Spools
  without a Creality RFID tag have no remaining reading, so their bar is
  striped with "?"; empty slots are dashed. 15% or less shows in red.
  With three or four units, the second pair goes underneath the first
  (CFS 3 under CFS 1, CFS 4 under CFS 2), with shorter bars so both rows
  fit.
- **The light button** (the bulb in the title bar, lit while the light is
  on) switches the printer's light: `SET_PIN` for a pin (fully on is the
  pin's `scale`), `SET_LED` to white or off for a strip, or the power
  device's on/off. Like every other change, it only works on the kiosk's
  own screen.
- **The preview picture** of the model being printed (from its slicer)
  shows in the camera's bottom-left corner once printing starts, and stays
  after it completes. Moonraker's own copy is used when it has one (most
  slicers); for Creality Print, the server reads it from the top of the
  G-code file. A file without one just shows none.
- **The current layer:** a fill behind the Layer row shows how far
  through its layer the print is, and the time the layer has left shows
  beside the label ("1m 20s left"). Both come from the same reading of the
  file as the layer preview (below): the slicer marks its estimated time at
  the end of every layer (`;TIME_ELAPSED:`), and Klipper's position in the
  file, with the moves' own time, says where between them the print is. The slicer's time is scaled by
  how the print is keeping pace with its estimate, once it's 5 minutes in.
  Without the marks, the fill goes by the file's bytes and no time shows.
- **Layer preview:** while a job prints, a **Preview** button in the
  camera's corner swaps it for a top-down drawing of the current layer:
  what's printed so far in the filament's colour (each colour for
  multi-colour prints): with a CFS, the colour of the spool it's printing
  from, else the slicer's. When the job's colours wouldn't show on the dark
  background (black filament), it turns a few shades lighter for that job. The
  rest of the layer is faint, a dot marks the nozzle,
  and the layer number and height show. **Camera** swaps back. Or, with
  **Split screen** under Settings → Display, both show at once while a job
  prints: the camera on top and the layer preview below. The
  server reads the printing file once per job, in the background (a second
  or two for a typical file; "Preparing preview…" until then), splitting it
  at the slicer's layer markers and turning arc moves into short lines
  (`server/gcode_preview.py`); progress through the layer comes from
  Klipper's position in the file.
- **The camera** is the printer's own webcam, flipped or rotated as set up
  in Fluidd or Mainsail. Tap it to fill the screen; tap again to go back.
  Press and hold it for a second to restart it.
  - A K2's camera (Creality's WebRTC stream) is connected to by the
    dashboard itself, rather than shown in Creality's page, so it can tell
    when the picture stops: if no new frame arrives for 20 seconds, or the
    connection drops, it reconnects, and keeps trying until the camera
    answers. Like Creality's page, it uses Google's STUN server to connect,
    so the kiosk needs internet access for the camera.
  - Other cameras are restarted every hour, and an MJPEG stream that drops
    is retried after 10 seconds.
  - Every camera restarts whenever the printer comes back after a dropout.

## Controls

The **Controls** button in the title bar (kiosk only) swaps the dashboard
for a view of printer controls; tap it again (it now says **Dashboard**) to
go back. Left untouched for 2 minutes, it goes back by itself.

- **Print:** what's printing, **Pause** / **Resume**, and **Cancel print**,
  which asks first ("It can't be resumed").
- **Temperatures:** nozzle, bed, and the chamber if the printer has a
  chamber heater. Each shows its current temperature and target, with
  **Off**, presets (nozzle 200/220/250°, bed 60/80/100°, chamber 40/60°)
  and **−5 / +5**. Targets can't go past the heater's `max_temp` in the
  printer's config.
- **Machine:** **Cool down** (all heaters off), **Home all** and **Motors
  off**, and moving each axis: pick a step (0.1, 1 or 10 mm), then **X −**,
  **X +**, and so on, under each axis's current position. An axis can only
  be moved once it's homed. A move past the printer's travel
  limits is refused by the printer, and its reason shows on screen.
- **Typing a value:** tap a heater's target, or an axis's position, to
  type a new one on a number pad. A typed target goes up to the heater's
  `max_temp`; a typed position moves the axis straight there, within its
  travel from the printer's config (`position_min`/`position_max`), except
  that Z can't be typed below 0, which would drive the nozzle into the bed.

Temperatures, moving, Home all and Motors off only work while the printer
is idle: Idle, Complete or Cancelled. While it's Printing, Paused or Busy
they're greyed out (and the server refuses them too); Pause, Resume, Cancel
and Cool down still work.

They use Moonraker's pause/resume/cancel and the standard G-code
(`SET_HEATER_TEMPERATURE`, `TURN_OFF_HEATERS`, `G28`, `M84`), so they work on
any Klipper printer. Every change still only works on the kiosk's own
screen, which the server checks too.

## Casting to TVs and displays

The **cast** button in the title bar (kiosk only) lists the Chromecasts,
Google TVs and Nest Hubs on your network (not speakers). Tap **Cast** beside
one to send it the dashboard, and **Stop** to end it; several can show it at
once. A cast screen is view-only: while a job prints, its left side shows the
camera on top and the layer preview below.

- **It stays up by itself.** A cast screen checks the printer's status every
  2 seconds; if one goes quiet for a minute (the TV dropped the page, or a
  Nest Hub went back to its photo frame), the kiosk casts to it again. If
  someone starts something else on that screen (a video cast from a
  phone, say), the kiosk lets it go instead. Casting isn't remembered
  across a restart of the kiosk (such as its weekly update), so it never
  switches a TV on by itself overnight.
- **The camera** on cast screens comes through go2rtc, which the installer
  sets up: a Cast device can't play the K2's WebRTC camera, but plays the
  same video re-wrapped. The player there re-times the K2's frames (it
  stamps them coarsely) and keeps about 1.2 seconds buffered. If frames are
  lost between the camera and go2rtc (e.g. a weak Wi-Fi link), the picture
  can stall for a few seconds until the camera's next full frame.
- **Security:** go2rtc only listens on the Pi itself (its API can run
  commands); the dashboard's server relays just the one camera stream to
  cast screens.

How it was worked out is in [docs/casting.md](docs/casting.md).

## Setup wizard

The first time the kiosk starts, it runs a setup wizard on the touchscreen,
with its own on-screen keyboard:

1. **Country**, which sets the Wi-Fi region so it can find your network.
2. **Time zone**, if the country has more than one.
3. **Wi-Fi**: pick a network and type its password.
4. **Printer address**: the printer's IP address, on a number pad (ABC switches
   to letters for a name like `k2pro.local`). Check makes sure a printer
   answers there before saving it; if nothing does, it says so and offers
   **Save anyway** (e.g. while the printer is off) or **Skip for now**. The
   Most printers show their IP address in their network settings (a K2's
   are on its own touchscreen), or look in your router's list of devices.
5. **Printer name**, shown at the top of the dashboard. It starts as the
   name the printer goes by: the one set in Fluidd or Mainsail, if any, else
   its host name (`K2Pro`). Edit it on the keyboard, or keep it.
6. **Clock**: 12- or 24-hour, defaulting to what's usual in your country.

At the end it offers to check for updates and restart right away; otherwise
it updates itself on Sunday.

If the printer stops answering and the kiosk itself has lost Wi-Fi, the
dashboard opens the wizard's Wi-Fi step by itself.

## Settings

Press and hold the gear in the dashboard's top-right corner for about a
second and a half (so a stray tap doesn't open it). Settings has a tab for
each thing, and saves each change as soon as it's made:

- **Printer**: change the printer's address, with the same check, or its
  name (**Rename**). Tapping the red "Can't reach the printer" banner opens
  this tab directly.
- **Wi-Fi**: join another network.
- **Location**: country and time zone.
- **Display**: 12- or 24-hour clock, and the camera and layer preview:
  **Switch** (one at a time, with a button) or **Split screen** (camera on
  top, preview below, while printing), the **Layer preview background**:
  **Auto** (dark, or shade C when a filament such as black wouldn't show on
  dark) or always one of five dark shades, **A** (darkest) to **E**, and the **Look**: **Standard**, or
  Star Trek panels, **LCARS** (The Next Generation's Federation consoles:
  black with orange, peach and lavender bars) or **Klingon** (black and deep
  red, sharp angles, amber readouts, and the dashboard's words in Klingon:
  `ghItlhtaH` for Printing, `QongDaq` for Bed and so on; the Controls view
  and Settings stay in English). With Klingon, **Words** picks Klingon or
  English for the dashboard's labels and messages. Screens being cast to follow the
  setting within half a minute. To try a look on one screen without
  changing it, add `?theme=lcars` or `?theme=klingon` to the page address.
- **About**: the installed version, **Check for updates and restart**, and
  what changed in each release.
- **Reset**: a factory reset. It erases the printer's address and name,
  the clock choice and every saved Wi-Fi network, and the wizard runs
  again, as if the kiosk were new. Do it on the kiosk itself: a remote
  connection drops when the Wi-Fi is forgotten.

Settings are saved in `~/.local/share/klipper-printer-monitor/settings.json` on
the Pi, so they survive reboots and updates. Only the kiosk's own screen can
change them: other devices on the network can view the dashboard, but the
gear is hidden for them and the server refuses their changes.

To stop the printer's address changing in the first place, give it a DHCP
reservation in your router.

## Server settings

Environment variables for `server.py`:

| Variable | Default |
|---|---|
| `PORT` | `8000` |
| `KPM_HOST` | none (the setup wizard asks); used until an address is saved in settings |
| `KPM_MOONRAKER_PORT` | `7125` |
| `KPM_CAMERA_URL` | a camera page to show instead of the printer's own webcam |
| `KPM_DATA_DIR` | `~/.local/share/klipper-printer-monitor` |
| `KPM_FAKE_SYSTEM` | `1` pretends to change Wi-Fi, country and time zone; automatic when `nmcli` isn't installed |

## Running it on your computer

```bash
make start PORT=8017
```

That runs the server in the background, with its output in
`.run/server-8017.log` (`make logs PORT=8017` follows it; `make run` runs it
in the terminal instead). Then open http://localhost:8017 and shrink the window to about 800x480.
Off a Pi, the wizard and Settings only pretend to change Wi-Fi, the country
and the time zone, so they're safe to try. To start over with the wizard,
use Settings → Reset, or delete `settings.json` from `KPM_DATA_DIR`.
`make stop PORT=8017` stops that server, and `make restart PORT=8017` stops
it and starts it again (after changing the server's code, say). Only a
server started from this folder is stopped.
To cast from your computer, run `make cast-setup` once: it puts the Cast
library and go2rtc (for the camera on cast screens) in a `.venv` here, and
`make start` and `make run` use them from then on.
`make test` runs the tests.

## Installing on a Pi

On a Raspberry Pi 4 or 5 running **Raspberry Pi OS with desktop**, with the
touchscreen connected and Wi-Fi set up in Raspberry Pi Imager:

```bash
git clone https://github.com/davelund750/klipper-printer-monitor.git ~/klipper-printer-monitor
cd ~/klipper-printer-monitor
./install.sh
sudo reboot
```

The Pi starts the setup wizard. (`./install.sh 192.168.1.50` saves the
printer's address up front instead, and the wizard starts with it filled in.)
Install from a git clone, as above: that's what lets it update itself. The
installer:

- runs `server.py` as a systemd service (`klipper-printer-monitor`);
- starts Chromium in kiosk mode at login via `kiosk/kiosk.sh`, which waits
  for the server and printer, starts from a fresh profile, and reloads the
  page if it doesn't load (logs to `~/.cache/klipper-monitor-kiosk.log`);
- tidies the desktop around the kiosk: a "Homing…" wallpaper
  (`kiosk/wallpaper.png`) behind Chromium while it starts, an auto-hiding
  taskbar, no notifications, and an invisible pointer;
- replaces the "Welcome to the Raspberry Pi Desktop" boot screen with a
  "Warming Up…" splash (a small Plymouth theme in `kiosk/boot-splash/`).
  Switching it takes about a minute, so re-runs skip it when nothing has
  changed. To restore the stock splash:
  `sudo plymouth-set-default-theme -R pix`;
- installs a color emoji font, for the wizard's country flags;
- lets the wizard (the server, running as your user) join Wi-Fi networks,
  set the time zone, set the Wi-Fi country with `raspi-config`, and start
  the update service, without a password, and nothing more (a polkit rule
  and a sudo rule);
- sets up casting: go2rtc (pinned to one release, checked against its
  SHA-256, listening only on the Pi) as the `klipper-printer-monitor-go2rtc`
  service, and pychromecast;
- installs the weekly update (below);
- turns off screen blanking and turns on desktop autologin.

Running `./install.sh` again later is safe: every step overwrites its own
earlier output.

## Updates

A Pi installed from a git clone keeps itself up to date. Every Sunday at 4:00
AM (the Pi's local time), `kiosk/update.sh` switches to the newest
**release** in the repository it was cloned from, re-runs `./install.sh` if
anything changed, and reboots. It reboots every week even when there's
nothing new, which also clears Chromium's memory build-up.

A release is a version tag, like `v1.0.0`, on a commit on `main`. Pushing to
`main` alone doesn't reach the kiosk, so work in progress can sit there
safely. Version numbers follow [semantic versioning](https://semver.org).

- **Publish a release:** add what changed under "Unreleased" in
  [CHANGELOG.md](CHANGELOG.md), push to `main`, wait for GitHub's tests to
  pass, then run `make release VERSION=1.0.0`. It checks all of that, dates
  the changelog section, tags the release, pushes it, and creates a GitHub
  Release with the same notes. It needs the [GitHub CLI](https://cli.github.com)
  (`gh`), signed in.
- **Withdraw a bad release:** delete its tag on GitHub
  (`git push origin :refs/tags/v1.0.0`). The kiosk goes back to the newest
  remaining release at its next update. Pre-release tags like
  `v1.1.0-beta.1` are never installed.
- **Update now:** hold the gear, then About, then "Check for updates and
  restart", or `sudo systemctl start klipper-printer-monitor-update.service`
  over SSH.
- **See what happened:** `/var/log/klipper-printer-monitor-update.log` on the Pi.

## Boot splash and wallpaper

Both images are drawn in `tools/splash_art.html` (a hotend laying down a
layer in the CFS colors). To change them, edit it and run
`tools/make_images.sh` on a Mac with Chrome; then re-run `./install.sh` on
the Pi.

The kiosk scripts, setup wizard and updater are adapted from Disney Parks
Wait Times.

## License

The code is released under the [MIT License](LICENSE). That covers this
project's own code and artwork only; the fonts the Star Trek looks load from
Google Fonts keep their own licenses, and Klipper, Moonraker, Creality and
Star Trek names belong to their owners.
