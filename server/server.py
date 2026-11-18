#!/usr/bin/env python3
"""Serves the dashboard, plus a small read-only API that relays the printer:
any printer running Klipper and Moonraker, with extras for Creality's (the
CFS, and Creality's own layer and job details).

The printer's Moonraker API doesn't send CORS headers, so the page can't ask
it directly; this server asks on the page's behalf and hands back a trimmed,
ready-to-show summary.

  GET /api/config     {printerHost, printerName, uiUrl (its Fluidd or
                      Mainsail), camera ({type, url, flipH, flipV, rotation}
                      or null), castStream (go2rtc's stream for the camera
                      on cast screens, or null), canChange}
  GET /api/status     {connected, ...}: print state, progress, layers, time
                      left, temperatures, each CFS unit's humidity and
                      slots, recent errors, and (when idle) the last
                      finished job. See summarize().
  GET /api/thumbnail?file=<name>  the preview picture of a G-code file
                      on the printer (see file_thumbnail())
  GET /api/gcode-layer?file=<name>&layer=<n>  one layer of the printing
                      file, for the layer preview (see gcode_preview.py);
                      /api/status's "preview" says which layer is printing
  GET /api/camera-stream  (WebSocket) the camera for cast screens: go2rtc's
                      stream, relayed (see casting.relay_stream())
  GET /api/cast       the screens casting can send the dashboard to (TVs,
                      Nest Hubs), and which are being cast to (see
                      casting.py); ?refresh searches again
  PUT /api/cast       {action: "start" | "stop", device}
  GET /api/history    the last 10 minutes of nozzle, bed and chamber
                      temperatures (and targets), for the dashboard's graphs.
                      See temperature_history().
  GET /api/settings   the saved settings: setupComplete, printerHost,
                      printerName (shown on the dashboard), clock ("12"/"24"),
                      previewLayout ("switch": camera or layer preview,
                      with a button; "split": both, stacked, while printing),
                      previewBackground ("auto": the dark one, lighter when
                      a filament wouldn't show on it; or a shade, "a" (darkest)
                      to "e"),
                      theme ("standard", "lcars" or "klingon": the look of
                      the dashboard and cast screens), language ("klingon"
                      or "english": the Klingon look's words)
  PUT /api/settings   merges in the given settings (not printerHost)
  PUT /api/printer    {printerHost, force}: checks that a printer answers at
                      that address, then saves it. If nothing answers, it's
                      only saved with force: true. Says the name the printer
                      goes by (suggestedName), for the wizard to offer.
  PUT /api/light      {on}: turns the printer's light on or off (see
                      find_light()); /api/status says whether it's on
  PUT /api/control    {action, ...}: the Controls view. action: "pause",
                      "resume", "cancel", "cooldown", "home", "motors_off",
                      "set_temp" with heater ("nozzle" | "bed" | "chamber")
                      and target (°C, up to the heater's max_temp), or
                      "jog" with axis ("x" | "y" | "z") and distance (mm,
                      ±0.1/1/10), or "move_to" with axis and position (mm,
                      within axis_limits()). See control_request().
  GET /api/changelog  the installed version's CHANGELOG.md, as a list of
                      releases (for Settings' About tab)

For the setup wizard (see system.py). These change the Pi itself:

  GET /api/system/status      current Wi-Fi network, online?, country, time
                              zone, and the app's version
  GET /api/system/networks    nearby Wi-Fi networks, strongest first
  GET /api/system/countries   countries with their time zones
  PUT /api/system/wifi        {ssid, password, hidden} -> {ok, message}
  PUT /api/system/country     {code}       e.g. "US"; sets the Wi-Fi region
  PUT /api/system/timezone    {timezone}   e.g. "America/New_York"
  PUT /api/system/update      check for updates now (the weekly update's
                              update.sh), then reboot
  PUT /api/system/reset       factory reset: erases the settings and saved
                              Wi-Fi networks, so the kiosk starts over with
                              the setup wizard

Every PUT, and the /api/system reads, only answer requests from the kiosk
itself, never from elsewhere on the network.

Besides reading from the printer, it sends commands for exactly two things,
both only from the kiosk's own screen: the light button (SET_PIN or SET_LED,
or a Moonraker power device's on/off), and the Controls view's actions
(pause, resume, cancel, heater targets, cooldown, home, motors off, moving
an axis). Nothing
else, and never on its own.

Settings are saved in ~/.local/share/klipper-printer-monitor/settings.json
(outside the project folder, so updating the app never overwrites them).
Only the web/ folder is served; the rest of the project isn't reachable over
the network.

Environment variables:
  PORT               this server's port (default 8000)
  KPM_HOST           the printer's address until one is saved in settings
                     (default: none; the setup wizard asks)
  KPM_MOONRAKER_PORT Moonraker's port (default 7125)
  KPM_CAMERA_URL     a camera page to show instead of the printer's own
                     webcam (default: the first one set up in Fluidd/Mainsail)
  KPM_DATA_DIR       where settings.json lives
  KPM_FAKE_SYSTEM    1: pretend to change Wi-Fi etc. (automatic off a Pi)

Usage: python3 server/server.py
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import casting
import gcode_preview
import system

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PROJECT_DIR, "web")

PORT = int(os.environ.get("PORT", "8000"))
DEFAULT_HOST = os.environ.get("KPM_HOST") or None  # until one is saved (the setup wizard asks)
MOONRAKER_PORT = os.environ.get("KPM_MOONRAKER_PORT", "7125")
DATA_DIR = os.environ.get("KPM_DATA_DIR", os.path.expanduser("~/.local/share/klipper-printer-monitor"))
SETTINGS_FILE = "settings.json"
REQUEST_TIMEOUT_SECS = 3
MAX_BODY_BYTES = 64 * 1024
MAX_NAME_LENGTH = 24
SYSTEM_PREFIX = "/api/system/"
SYSTEM = system.make_system()
CASTER = casting.Caster(PORT)
LOCAL_ADDRESSES = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
# An IPv4 address or a host name such as k2pro.local.
HOST_PATTERN = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")

# The Klipper objects the dashboard needs, in one query, plus the printer's
# chamber sensor and heater if it has them (see printer_profile()). "box" is
# a Creality CFS; elsewhere it just comes back empty, like any object a
# printer doesn't have.
OBJECTS = ["webhooks", "print_stats", "idle_timeout", "virtual_sdcard", "toolhead", "extruder", "heater_bed", "box"]

# How long what's learned about a printer (its objects, web interface and
# camera) is kept before asking again.
PROFILE_TTL_SECS = 600
# Where Fluidd and Mainsail usually live: port 80 (or 81 for the second of
# the two), or 4408/4409 on Creality printers.
UI_PORTS = (80, 81, 4408, 4409)
# Webcam services (Moonraker's names) that are a plain MJPEG stream, shown as
# an image; anything else is a page of its own, shown in a frame.
MJPEG_SERVICES = {"mjpegstreamer", "mjpegstreamer-adaptive", "uv4l-mjpeg", "ipstream"}
# Lights: an output pin or a Moonraker power device with one of these words in
# its name (the K2's is "output_pin LED"), or any LED strip Klipper drives.
LIGHT_WORDS = ("led", "light", "lamp")
LED_KINDS = ("led", "neopixel", "dotstar", "pca9533", "pca9632")
# Heater limits when the printer's config doesn't say (it normally does).
DEFAULT_MAX_TEMPS = {"nozzle": 300, "bed": 120, "chamber": 60}
# Pause, resume, cancel and homing can take a while to finish; Moonraker only
# answers once they're done.
CONTROL_TIMEOUT_SECS = 60
# When the Controls view can change temperatures and move things: the printer
# is doing nothing (idle, or a print finished or cancelled).
IDLE_STATES = ("standby", "complete", "cancelled")
# Moving the toolhead or bed from the Controls view: the step sizes it
# offers (mm), and feed rates (mm/min).
JOG_STEPS = (0.1, 1, 10)
JOG_SPEEDS = {"x": 6000, "y": 6000, "z": 600}

# Console lines that count as errors: Klipper's "!!" prefix, or Creality's
# error reports: {"code":"key863","msg":"TR2863 ..."}, or a line starting with
# a code such as TR2863 or FR0122. (Only at the start: board names such as
# SB2209 or RP2040 turn up mid-line on other printers.)
ERROR_CODE = re.compile(r'"code"\s*:\s*"key\d{3,}"|^[A-Z]{2}\d{4}\b')
MAX_ERRORS = 3
# The printer doesn't say when an error is cleared, so one only stays up this
# long, unless the printer is stopped on it (paused, or in its error state).
ERROR_HOLD_SECS = 2 * 60
CONSOLE_LINES = 100


def read_settings():
    try:
        with open(os.path.join(DATA_DIR, SETTINGS_FILE)) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return {k: v for k, v in data.items() if valid_setting(k, v)} if isinstance(data, dict) else {}


def write_settings(settings):
    # Write to a temp file and swap it in, so a power cut mid-write can't
    # leave a half-written file behind.
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f)
    os.replace(tmp_path, os.path.join(DATA_DIR, SETTINGS_FILE))


THEMES = ("standard", "lcars", "klingon")  # see web/lcars.css and web/klingon.css
LANGUAGES = ("klingon", "english")  # the Klingon look's words
PREVIEW_BACKGROUNDS = ("auto", "a", "b", "c", "d", "e")  # see web/app.js's PREVIEW_SHADES


def valid_setting(key, value):
    if key == "setupComplete":
        return isinstance(value, bool)
    if key == "clock":
        return value in ("12", "24")
    if key == "previewLayout":
        return value in ("switch", "split")
    if key == "previewBackground":
        return value in PREVIEW_BACKGROUNDS
    if key == "theme":
        return value in THEMES
    if key == "language":
        return value in LANGUAGES
    if key == "printerHost":
        return is_host(value)
    if key == "printerName":
        return isinstance(value, str) and 0 < len(value.strip()) <= MAX_NAME_LENGTH and value.isprintable()
    return False


def is_host(value):
    return isinstance(value, str) and bool(HOST_PATTERN.match(value))


def printer_host():
    """The saved printer address, else KPM_HOST, else None (not set up yet)."""
    return read_settings().get("printerHost", DEFAULT_HOST)


def moonraker_get(path, host):
    """GETs a Moonraker endpoint and returns its "result"."""
    if not host:
        raise OSError("no printer address is set up yet")
    url = f"http://{host}:{MOONRAKER_PORT}{path}"
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECS) as response:
        return json.load(response)["result"]


def check_printer(host):
    """Whether a Klipper printer answers at `host`, as {ok, message,
    suggestedName}."""
    try:
        info = moonraker_get("/printer/info", host)
    except (OSError, ValueError, KeyError):
        return {"ok": False, "message": f"Nothing answered at {host}."}
    name = suggested_name(host, info.get("hostname"))
    return {"ok": True, "message": f"Found {name} at {host}.", "suggestedName": name}


# Where web interfaces keep the printer's name, if one was set there:
# (Moonraker database namespace, key).
UI_NAMES = [("fluidd", "uiSettings.general.instanceName"), ("mainsail", "general.printername")]


def suggested_name(host, hostname):
    """The name the printer goes by: the one set in Fluidd or Mainsail, else
    its host name ("K2Pro"), else "Printer"."""
    for namespace, key in UI_NAMES:
        try:
            value = moonraker_get(f"/server/database/item?namespace={namespace}&key={key}", host).get("value")
        except (OSError, ValueError, KeyError, AttributeError):
            continue  # that interface isn't installed, or has no name set
        if valid_setting("printerName", value):
            return value.strip()
    if valid_setting("printerName", hostname):
        return hostname.strip()
    return "Printer"


def describe_version(described):
    """The app's version for people, from `git describe` output.

    A kiosk sits on a release tag ("v1.2.0" -> "1.2.0"); a developer's copy
    can be past one ("v1.2.0-3-gabc1234" -> "1.2.0 + 3 changes (abc1234)").
    """
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)(?:-(\d+)-g([0-9a-f]+))?(-dirty)?", described)
    if not match:
        # No release tags yet: just the commit.
        commit = described.removesuffix("-dirty")
        return f"development ({commit})" if re.fullmatch(r"[0-9a-f]+", commit) else "unknown"
    version, ahead, commit, dirty = match.groups()
    if ahead:
        version += f" + {ahead} change{'s' if ahead != '1' else ''} ({commit})"
    if dirty:
        version += ", edited"
    return version


def app_version():
    try:
        described = subprocess.run(
            ["git", "describe", "--tags", "--match", "v[0-9]*", "--always", "--dirty"],
            cwd=PROJECT_DIR, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"  # no git, e.g. a downloaded copy
    return describe_version(described)


# Worked out once: an update reboots the kiosk, which restarts the server.
VERSION = app_version()

CHANGELOG_PATH = os.path.join(PROJECT_DIR, "CHANGELOG.md")


def plain_text(markdown):
    """An entry's text without its Markdown: [links](...), `code`, **bold**, *italics*."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", markdown)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return re.sub(r"\*([^*\s][^*]*)\*", r"\1", text)


def parse_changelog(text):
    """CHANGELOG.md's releases, newest first, for people to read on the kiosk.

    Each is {"version": "1.2.0" or None (the dated beta sections before
    1.0.0), "date": "YYYY-MM-DD", "label": extra heading text or None,
    "notes": [paragraphs], "sections": [{"title": "Added", "items": [...]}]}.
    "Unreleased" is left out: those changes aren't installed.
    """
    releases = []
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        heading, _, body = block.partition("\n")
        heading = heading.strip()
        match = re.fullmatch(r"(\d+\.\d+\.\d+) - (\d{4}-\d{2}-\d{2})", heading)
        if match:
            version, date, label = match[1], match[2], None
        else:
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?::\s*(.+))?", heading)
            if not match:
                continue  # "Unreleased", or anything else that isn't a release
            version, date, label = None, match[1], match[2]
        release = {"version": version, "date": date, "label": label, "notes": [], "sections": []}
        section = None
        lines = []  # the entry or paragraph being read

        def finish():
            if lines:
                entry = plain_text(" ".join(lines))
                (section["items"] if section else release["notes"]).append(entry)
                lines.clear()

        for line in body.splitlines():
            stripped = line.strip()
            if line.startswith("### "):
                finish()
                section = {"title": line[4:].strip(), "items": []}
                release["sections"].append(section)
            elif line.startswith("- "):
                finish()
                lines.append(line[2:].strip())
            elif not stripped:
                finish()
            else:
                lines.append(stripped)  # a wrapped line of the same entry
        finish()
        releases.append(release)
    return releases


def read_changelog():
    try:
        with open(CHANGELOG_PATH, encoding="utf-8") as f:
            return parse_changelog(f.read())
    except OSError:
        return []


_profiles = {}  # host -> (when, profile)
_cameras = {}  # host -> (when, {"uiUrl", "camera"})
_file_metadata = {}  # (host, filename) -> the slicer's metadata for that file
_last_busy = {}  # host -> when Klipper was last seen running commands

# A K2's pre-print routine (calibration, heating, loading filament) leaves
# Klipper idle for up to half a minute between steps; count those pauses as
# busy too, rather than flicking to Idle.
BUSY_HOLD_SECS = 60

# Klipper says "complete" until the next job starts, which can be hours. The
# dashboard shows it as Idle instead once the job's done with: the printer's
# light went off after it finished (the K2 turns it off a while after a
# print), or, failing that, DONE_HOLD_SECS after the end.
DONE_HOLD_SECS = 30 * 60
_light_was_on = {}  # host -> whether the light was on at the last reading
_light_went_off = {}  # host -> when the light last went from on to off


def chamber_objects(names):
    """The chamber's (sensor, heater) among a printer's Klipper objects, either
    None if it has none: a temperature_sensor (or temperature_fan) with
    "chamber" in its name, and a heater_generic with "chamber" in its name.
    A chamber heater with no separate sensor is its own sensor."""
    def named_chamber(name, *kinds):
        kind, _, rest = name.partition(" ")
        return kind in kinds and "chamber" in rest.lower()

    heaters = [n for n in names if named_chamber(n, "heater_generic")]
    sensors = [n for n in names if named_chamber(n, "temperature_sensor")]
    sensors += [n for n in names if named_chamber(n, "temperature_fan")]
    heater = heaters[0] if heaters else None
    return (sensors[0] if sensors else heater), heater


def find_light(names, power_devices=()):
    """The printer's light, or None: {"kind": "pin" | "led" | "power", "name":
    what its command calls it, "object": its Klipper object (not for power)}.
    An output pin named for a light comes first, then an LED strip, then a
    Moonraker power device named for one."""
    def named_light(name):
        return any(word in name.lower() for word in LIGHT_WORDS)

    for obj in names:
        kind, _, name = obj.partition(" ")
        if kind == "output_pin" and named_light(name):
            return {"kind": "pin", "name": name, "object": obj}
    for obj in names:
        kind, _, name = obj.partition(" ")
        if kind in LED_KINDS and name:
            return {"kind": "led", "name": name, "object": obj}
    for device in power_devices:
        if named_light(device):
            return {"kind": "power", "name": device, "object": None}
    return None


def printer_profile(host):
    """What this printer has, learned from its object list and config (and
    cached): {"objects": [names to query], "chamberSensor": name,
    "chamberHeater": name, "light": find_light()'s answer (plus "scale" for a
    pin), "heaters": {"nozzle" | "bed" | "chamber": {"name", "max"}},
    "axes": {"x" | "y" | "z": {"min", "max"}}}."""
    cached = _profiles.get(host)
    if cached and time.time() - cached[0] < PROFILE_TTL_SECS:
        return cached[1]
    names = moonraker_get("/printer/objects/list", host)["objects"]
    sensor, heater = chamber_objects(names)
    try:
        power = [d["device"] for d in moonraker_get("/machine/device_power/devices", host)["devices"]]
    except (OSError, ValueError, KeyError, TypeError):
        power = []  # no power devices set up in Moonraker
    settings = config_settings(host)
    light = find_light(names, power)
    if light and light["kind"] == "pin":
        light["scale"] = as_number(settings.get(f"output_pin {light['name'].lower()}", {}).get("scale")) or 1.0
    extra = [n for n in dict.fromkeys([sensor, heater, light and light["object"]]) if n]
    profile = {"objects": OBJECTS + extra, "chamberSensor": sensor, "chamberHeater": heater, "light": light,
               "heaters": controllable_heaters(names, heater, settings), "axes": axis_limits(settings)}
    _profiles[host] = (time.time(), profile)
    return profile


def config_settings(host):
    """The printer's parsed config (section names in lower case); {} if it
    can't be read."""
    try:
        return moonraker_get("/printer/objects/query?configfile=settings", host)["status"]["configfile"]["settings"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def axis_limits(settings):
    """Where each axis can be sent by typing a position, from the printer's
    config: {"x": {"min": -13, "max": 302}, ...}. Z never below 0, though the
    config allows it (for probing): typed by mistake, it drives the nozzle
    into the bed."""
    limits = {}
    for axis in ("x", "y", "z"):
        stepper = settings.get(f"stepper_{axis}") or {}
        low, high = as_number(stepper.get("position_min")), as_number(stepper.get("position_max"))
        if low is None or high is None:
            continue
        limits[axis] = {"min": max(low, 0.0) if axis == "z" else low, "max": high}
    return limits


def controllable_heaters(names, chamber_heater, settings):
    """The heaters the Controls view can set, with each one's max_temp from
    the printer's config: {"nozzle": {"name": "extruder", "max": 390}, ...}.
    "name" is what SET_HEATER_TEMPERATURE calls it."""
    heaters = {}
    for key, obj in (("nozzle", "extruder"), ("bed", "heater_bed"), ("chamber", chamber_heater)):
        if not obj or obj not in names:
            continue
        limit = as_number((settings.get(obj.lower()) or {}).get("max_temp"))
        heaters[key] = {"name": obj.split(" ")[-1], "max": limit or DEFAULT_MAX_TEMPS[key]}
    return heaters


def light_on(status, light, power_state=None):
    """Whether the light is on, from the printer's status (or, for a power
    device, its state: "on"/"off"); None if it can't tell."""
    if not light:
        return None
    if light["kind"] == "power":
        return power_state == "on" if power_state in ("on", "off") else None
    info = status.get(light["object"]) or {}
    if light["kind"] == "pin":
        value = as_number(info.get("value"))
        return None if value is None else value > 0
    colors = info.get("color_data")
    if not isinstance(colors, list):
        return None
    return any(as_number(c) for led in colors if isinstance(led, list) for c in led)


def light_command(light, on):
    """The G-code that switches a pin or LED light on or off."""
    if light["kind"] == "pin":
        value = light.get("scale", 1.0) if on else 0
        return f"SET_PIN PIN={light['name']} VALUE={value:g}"
    level = 1 if on else 0
    return f"SET_LED LED={light['name']} RED={level} GREEN={level} BLUE={level} WHITE={level}"


def moonraker_post(path, host, timeout=REQUEST_TIMEOUT_SECS):
    """POSTs to a Moonraker endpoint (its parameters in the query string)."""
    if not host:
        raise OSError("no printer address is set up yet")
    request = urllib.request.Request(f"http://{host}:{MOONRAKER_PORT}{path}", data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["result"]


def switch_light(host, on):
    """Turns the printer's light on or off. Returns False if it has none."""
    light = printer_profile(host)["light"]
    if not light:
        return False
    if light["kind"] == "power":
        action = "on" if on else "off"
        moonraker_post(f"/machine/device_power/device?device={urllib.parse.quote(light['name'])}&action={action}", host)
    else:
        moonraker_post(f"/printer/gcode/script?script={urllib.parse.quote(light_command(light, on))}", host)
    return True


def control_request(action, body, state, heaters, homed="", axes=None):
    """What a Controls-view action should send, as (Moonraker path, None) or
    (None, a reason it can't be done now). state: the dashboard's state
    (print_stats' state, or "busy": see printer_state()); homed: the
    toolhead's homed axes, e.g. "xyz"; axes: axis_limits().

    Temperatures, moving, homing and motors off only while the printer is
    idle (IDLE_STATES); pause, resume, cancel and cool down whenever they
    make sense."""
    printing = state in ("printing", "paused")
    if action in ("home", "motors_off", "jog", "move_to", "set_temp") and state not in IDLE_STATES:
        return None, "Only while the printer is idle (not printing, paused or busy)."
    if action == "pause":
        return ("/printer/print/pause", None) if state == "printing" else (None, "Nothing is printing.")
    if action == "resume":
        return ("/printer/print/resume", None) if state == "paused" else (None, "The print isn't paused.")
    if action == "cancel":
        return ("/printer/print/cancel", None) if printing else (None, "Nothing is printing.")
    if action == "cooldown":
        return gcode_path("TURN_OFF_HEATERS"), None
    if action in ("home", "motors_off"):
        return gcode_path("G28" if action == "home" else "M84"), None
    if action == "jog":
        axis, distance = body.get("axis"), body.get("distance")
        if axis not in JOG_SPEEDS:
            return None, "Pick X, Y or Z."
        if isinstance(distance, bool) or not isinstance(distance, (int, float)) or abs(distance) not in JOG_STEPS:
            return None, "Pick a step of " + ", ".join(f"{d:g}" for d in JOG_STEPS) + " mm."
        if axis not in (homed or ""):
            return None, f"Home {axis.upper()} first."
        # A relative move, then back to absolute positioning for whatever's next.
        return gcode_path(f"G91\nG1 {axis.upper()}{distance:g} F{JOG_SPEEDS[axis]}\nG90"), None
    if action == "move_to":
        axis, position = body.get("axis"), body.get("position")
        limit = (axes or {}).get(axis)
        if axis not in JOG_SPEEDS:
            return None, "Pick X, Y or Z."
        if not limit:
            return None, f"This printer's {axis.upper()} travel isn't known."
        if isinstance(position, bool) or not isinstance(position, (int, float)) or not limit["min"] <= position <= limit["max"]:
            return None, f"Pick {axis.upper()} from {limit['min']:g} to {limit['max']:g} mm."
        if axis not in (homed or ""):
            return None, f"Home {axis.upper()} first."
        return gcode_path(f"G90\nG1 {axis.upper()}{round(position, 2):g} F{JOG_SPEEDS[axis]}"), None
    if action == "set_temp":
        heater = heaters.get(body.get("heater"))
        target = body.get("target")
        if not heater:
            return None, "This printer has no such heater."
        if isinstance(target, bool) or not isinstance(target, (int, float)) or not 0 <= target <= heater["max"]:
            return None, f"Pick a temperature from 0 to {heater['max']:g}°C."
        return gcode_path(f"SET_HEATER_TEMPERATURE HEATER={heater['name']} TARGET={target:g}"), None
    return None, "Unknown action."


def gcode_path(script):
    return f"/printer/gcode/script?script={urllib.parse.quote(script)}"


def control(host, action, body):
    """Does a Controls-view action on the printer. Returns (ok, message)."""
    profile = printer_profile(host)
    status = objects_query(host, ["webhooks", "print_stats", "idle_timeout", "toolhead"])
    if (status.get("webhooks") or {}).get("state", "ready") != "ready":
        return False, "Klipper isn't ready."
    path, problem = control_request(action, body, printer_state(host, status), profile["heaters"],
                                    (status.get("toolhead") or {}).get("homed_axes", ""), profile.get("axes"))
    if problem:
        return False, problem
    try:
        moonraker_post(path, host, timeout=CONTROL_TIMEOUT_SECS)
    except TimeoutError:
        return True, "Sent; the printer is still working on it."
    except urllib.error.HTTPError as e:
        # Klipper refused it, e.g. "Move out of range": pass its reason on.
        try:
            reason = json.load(e)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            reason = f"The printer refused it (HTTP {e.code})."
        finally:
            e.close()
        return False, reason.strip().removeprefix("!! ")
    return True, "Done."


def find_web_ui(host):
    """The printer's Fluidd or Mainsail, e.g. "http://192.168.1.50:4408", or
    None. Checks each usual port for one, since port 80 can be something
    else (on a K2, Creality's own page)."""
    for port in UI_PORTS:
        base = f"http://{host}" + ("" if port == 80 else f":{port}")
        try:
            with urllib.request.urlopen(base + "/", timeout=2) as response:
                page = response.read(65536).decode("utf-8", "replace").lower()
        except OSError:
            continue
        if "fluidd" in page or "mainsail" in page:
            return base
    return None


def creality_webrtc(url, host):
    """For Creality's camera page (a K2's /camera.html), the address it
    negotiates its WebRTC stream with, so the dashboard can connect itself
    and notice when the stream stalls; None for any other page."""
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECS) as response:
            page = response.read(65536).decode("utf-8", "replace")
    except OSError:
        return None
    # It POSTs its offer to port 8000 on the printer, at this path.
    return f"http://{host}:8000/call/webrtc_local" if "/call/webrtc_local" in page else None


def find_camera(host, ui_url):
    """The printer's first enabled webcam, from Moonraker's list (the cameras
    set up in Fluidd or Mainsail), as {type: "mjpeg" | "webrtc" | "frame",
    url, flipH, flipV, rotation, and signalUrl for "webrtc"}, or None.
    "webrtc" is Creality's camera, which the dashboard connects to itself.
    KPM_CAMERA_URL, if set, is shown in a frame."""
    if web_url(os.environ.get("KPM_CAMERA_URL")):
        return {"type": "frame", "url": os.environ["KPM_CAMERA_URL"], "flipH": False, "flipV": False, "rotation": 0}
    try:
        webcams = moonraker_get("/server/webcams/list", host)["webcams"]
    except (OSError, ValueError, KeyError):
        return None
    for cam in webcams:
        url = cam.get("stream_url")
        if cam.get("enabled") is False or not isinstance(url, str) or not url:
            continue
        # Relative URLs are relative to the web interface the camera was set
        # up in (e.g. Creality's "/camera.html" lives next to its Fluidd).
        url = urllib.parse.urljoin((ui_url or f"http://{host}") + "/", url)
        if not web_url(url):
            continue  # e.g. a javascript: address, which would run in the dashboard
        mjpeg = cam.get("service") in MJPEG_SERVICES
        signal_url = None if mjpeg else creality_webrtc(url, host)
        return {
            "type": "mjpeg" if mjpeg else "webrtc" if signal_url else "frame",
            "url": url,
            **({"signalUrl": signal_url} if signal_url else {}),
            "flipH": bool(cam.get("flip_horizontal")),
            "flipV": bool(cam.get("flip_vertical")),
            "rotation": cam.get("rotation") if cam.get("rotation") in (0, 90, 180, 270) else 0,
        }
    return None


def web_url(url):
    """Whether `url` is a plain http(s) address. The camera's address comes
    from the printer, which anyone on the network can usually change, and the
    dashboard shows it in a frame."""
    return isinstance(url, str) and urllib.parse.urlsplit(url).scheme in ("http", "https")


def printer_extras(host):
    """{"uiUrl", "camera", "castStream"} for the dashboard. The web interface
    and camera are cached (not while the printer doesn't answer, so they're
    found once it does); the cast stream is checked every time, since go2rtc
    forgets it if it restarts."""
    if not host:
        return {"uiUrl": None, "camera": None, "castStream": None}
    cached = _cameras.get(host)
    if cached and time.time() - cached[0] < PROFILE_TTL_SECS:
        extras = cached[1]
    else:
        ui_url = find_web_ui(host)
        extras = {"uiUrl": ui_url, "camera": find_camera(host, ui_url)}
        if ui_url or extras["camera"]:
            _cameras[host] = (time.time(), extras)
    return {**extras, "castStream": cast_stream(extras["camera"])}


def cast_stream(camera):
    """The go2rtc stream cast screens play the camera from, if they need one
    (the K2's WebRTC camera) and go2rtc has it; else None."""
    source = casting.go2rtc_source(camera)
    return casting.STREAM if source and casting.register_stream(source) else None


def file_metadata(host, filename):
    """The slicer's metadata for a G-code file (estimated time, layers,
    filament), from Moonraker; {} if there's none. Cached per file."""
    key = (host, filename)
    if key not in _file_metadata:
        try:
            _file_metadata[key] = moonraker_get(
                f"/server/files/metadata?filename={urllib.parse.quote(filename)}", host)
        except (OSError, ValueError, KeyError):
            return {}  # not cached: try again next time
    return _file_metadata[key]


def objects_query(host, objects):
    query = "&".join(urllib.parse.quote(name) for name in objects)
    return moonraker_get(f"/printer/objects/query?{query}", host)["status"]


def time_left(print_duration, progress, slicer_estimate):
    """Seconds left in the print, or None if there's nothing to go on.

    Blends two guesses: the slicer's estimate minus time spent so far, which
    is better early on, and the time per percent so far (progress is by file
    position), which gets better as the print goes on.
    """
    by_slicer = max(slicer_estimate - print_duration, 0) if slicer_estimate else None
    by_file = print_duration / progress - print_duration if progress >= 0.02 else None
    if by_slicer is None:
        return by_file
    if by_file is None:
        return by_slicer
    return (1 - progress) * by_slicer + progress * by_file


# The slicer's times (see gcode_preview) are scaled by how the print is
# keeping pace with them, once there's PACE_AFTER_SECS of it to go on: never
# below half, or above twice, the slicer's.
PACE_AFTER_SECS = 5 * 60


def pace(index, position, print_duration):
    """How much longer than the slicer's estimate the print is taking: 1.2
    for 20% slower."""
    # Both from the first layer: Klipper's print_duration starts at the first
    # extrusion, not during the start code.
    estimated = (index.time_at(position) or 0) - index.start_code_time()
    if estimated < PACE_AFTER_SECS:
        return 1.0
    return min(max(print_duration / estimated, 0.5), 2)


def layer_progress(index, position, print_duration):
    """How far through the current layer the print is: {"done": 0 to 1,
    "left": seconds, or None if the slicer gave no times}."""
    progress = index.layer_progress(position)
    if progress is None:
        return None
    done, left = progress
    if left is not None:
        left *= pace(index, position, print_duration)
    return {"done": round(done, 3), "left": None if left is None else round(left)}


def print_progress(index, position, print_duration):
    """How far through the whole print, by the slicer's times rather than the
    file's bytes (a layer of slow ironing can take hours for few bytes):
    (0 to 1, seconds left), or None if the file has no times."""
    estimated, total = index.time_at(position), index.time_at(index.file_size)
    if estimated is None or not total:
        return None
    # From the first layer: 0% until it starts printing.
    before = index.start_code_time()
    total, done = total - before, max(estimated - before, 0)
    if total <= 0:
        return None
    return min(done / total, 1.0), round(max(total - done, 0) * pace(index, position, print_duration))


def cfs_units(box):
    """Each connected CFS unit with its four slots:

        [{unit: 1, humidity: 26, temp: 24, slots: [
            {letter: "A", color: "#2B2A2D", remaining: 86, loaded: False}, ...]}]

    A slot's color is None when it's empty. Its remaining is None when the
    CFS can't tell: spools without a Creality RFID tag (and ones that have
    just run out) report 0% whatever is on them.

    The slot feeding the toolhead is the one named by its unit's "filament"
    letter. Units without hardware report a state of "None" and are left out.
    """
    units = []
    if not isinstance(box, dict):
        return units
    for unit in range(1, 5):
        info = box.get(f"T{unit}")
        if not isinstance(info, dict) or info.get("state") != "connect":
            continue
        humidity = as_number(info.get("dry_and_humidity"))
        if humidity is not None and humidity < 0:
            humidity = None  # -1: no reading
        slots = []
        for index, letter in enumerate("ABCD"):
            color = slot_value(info.get("color_value"), index)
            remaining = as_number(slot_value(info.get("remain_len"), index))
            tag = slot_value(info.get("vender"), index)
            if remaining is not None and (remaining < 0 or tag == "none"):
                remaining = None
            slots.append({
                "letter": letter,
                # Colors come as "0RRGGBB" (a leading flag digit); "-1" is empty.
                "color": "#" + color[-6:] if isinstance(color, str) and len(color) >= 6 else None,
                "remaining": remaining,
                "loaded": info.get("filament") == letter,
            })
        units.append({"unit": unit, "humidity": humidity, "temp": as_number(info.get("temperature")),
                      "slots": slots})
    return units


def spool_colours(box):
    """The colour of the spool each of the slicer's filaments is printing
    from, by the CFS's map for the job ({"T1A": "T2B"}: the slicer's first
    filament, from unit 2 slot B), so the layer preview matches what's
    loaded rather than the colours picked in the slicer. A list by slicer
    tool (T0, T1, ...), None where not known; None without a CFS."""
    mapping = box.get("map") if isinstance(box, dict) else None
    units = {unit["unit"]: unit for unit in cfs_units(box)}
    if not isinstance(mapping, dict) or not units:
        return None
    colours = []
    for tool in range(16):
        slot = mapping.get(f"T{tool // 4 + 1}{'ABCD'[tool % 4]}")
        unit = units.get(int(slot[1])) if isinstance(slot, str) and len(slot) == 3 and slot[1].isdigit() else None
        letter = "ABCD".find(slot[2]) if unit else -1
        colours.append(unit["slots"][letter]["color"] if letter >= 0 else None)
    while colours and colours[-1] is None:
        colours.pop()
    return colours or None


def slot_value(values, index):
    """The CFS reports per-slot fields as four-item lists; one slot's item."""
    return values[index] if isinstance(values, list) and index < len(values) else None


def as_number(value):
    """The CFS reports numbers as strings ("26"); None if it isn't one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def recent_errors(lines, since):
    """Error lines from the console since `since` (epoch secs), newest last."""
    errors = []
    for line in lines:
        message = (line.get("message") or "").strip()
        if line.get("time", 0) < since or not message:
            continue
        if message.startswith("!!") or ERROR_CODE.search(message):
            errors.append({"time": line["time"], "message": message.lstrip("! ").splitlines()[0][:200]})
    return errors[-MAX_ERRORS:]


def toolhead(info):
    """{"homed": "xyz", "position": [x, y, z]}, or None if not reported."""
    if not isinstance(info, dict) or not isinstance(info.get("position"), list):
        return None
    return {"homed": info.get("homed_axes") or "", "position": [round(v, 2) for v in info["position"][:3]]}


def heater(status, name):
    info = status.get(name) or {}
    if "temperature" not in info:
        return None
    return {"temp": info["temperature"], "target": info.get("target")}


def current_job(status):
    """virtual_sdcard.cur_print_data (Creality), if it's about the file
    printing. The printer can leave the job before's there, for a job it
    didn't start itself."""
    job = (status.get("virtual_sdcard") or {}).get("cur_print_data") or {}
    printing = (status.get("print_stats") or {}).get("filename")
    if job.get("filename") and printing and os.path.basename(job["filename"]) != os.path.basename(printing):
        return {}
    return job


def summarize(status, console, last_job, now, profile=None, metadata=None, recently_busy=False):
    """Turns raw Moonraker data into what the dashboard shows.

    profile: from printer_profile() (which chamber sensor to read).
    metadata: the slicer's metadata for the file printing, from Moonraker,
    for printers that don't include it in their status (Creality printers
    do, in virtual_sdcard.cur_print_data).
    recently_busy: Klipper was running commands within BUSY_HOLD_SECS (see
    fetch_status), so a short pause between steps still counts as busy.
    """
    profile = profile or {}
    stats = status.get("print_stats") or {}
    card = status.get("virtual_sdcard") or {}
    job = current_job(status)
    metadata = job.get("metadata") or metadata or {}
    info = stats.get("info") or {}
    klippy = (status.get("webhooks") or {}).get("state", "ready")

    state = stats.get("state", "standby")
    if klippy != "ready":
        state = "offline"  # Klipper itself is down or in a fault ("shutdown")
    elif state == "standby" and (recently_busy or (status.get("idle_timeout") or {}).get("state") == "Printing"):
        # No print job, but Klipper is running commands: calibration, a bed
        # mesh, homing, a macro. (idle_timeout says "Printing" for any of them.)
        state = "busy"
    active = state in ("printing", "paused")

    progress = card.get("progress") or 0
    print_duration = stats.get("print_duration") or 0
    left = time_left(print_duration, progress, metadata.get("estimated_time")) if active else None

    chamber = heater(status, profile["chamberSensor"]) if profile.get("chamberSensor") else None
    if chamber is not None and profile.get("chamberHeater"):
        chamber["target"] = (status.get(profile["chamberHeater"]) or {}).get("target")

    # Layers: Creality printers count them in virtual_sdcard; standard Klipper
    # has them in print_stats.info, when the slicer's G-code reports them
    # (SET_PRINT_STATS_INFO).
    layer = card.get("layer") if card.get("layer") is not None else info.get("current_layer")
    layer_count = card.get("layer_count") or info.get("total_layer") or metadata.get("layer_count")

    # Filament for the job: per-tool grams (Creality), else the total.
    grams = sum(as_number(g) or 0 for g in metadata.get("filament_used_g") or [])
    grams = grams or as_number(metadata.get("filament_weight_total")) or 0

    # Errors from this job while it's stopped on them, otherwise just new ones.
    started = job.get("start_time") or now - (stats.get("total_duration") or 0)
    held = now - ERROR_HOLD_SECS
    since = started if state in ("paused", "error") else max(started, held) if active else held
    errors = recent_errors(console, since)
    if state == "error" and stats.get("message"):
        errors.append({"time": now, "message": stats["message"]})
    if klippy != "ready":
        errors.append({"time": now, "message": (status.get("webhooks") or {}).get("state_message", "Klipper isn't ready")})

    return {
        "connected": True,
        "state": state,
        "filename": stats.get("filename") or None,
        "progress": progress,
        "layer": layer,
        "layerCount": layer_count,
        "printDuration": print_duration,
        "timeLeft": left,
        "finishAt": now + left if left is not None else None,
        "filamentGrams": grams,
        "temps": {
            "nozzle": heater(status, "extruder"),
            "bed": heater(status, "heater_bed"),
            "chamber": chamber,
        },
        # The model's preview picture, once its job has started.
        "thumbnail": (f"/api/thumbnail?file={urllib.parse.quote(stats['filename'])}"
                      if stats.get("filename") and state in ("printing", "paused", "complete") else None),
        "cfs": cfs_units(status.get("box")),
        "spoolColours": spool_colours(status.get("box")),
        "toolhead": toolhead(status.get("toolhead")),
        "errors": errors[-MAX_ERRORS:],
        "lastJob": last_job if not active else None,
        "time": now,
    }


def printer_state(host, status, now=None):
    """print_stats' state, or "busy": no print job, but Klipper is running
    commands now or was within BUSY_HOLD_SECS (see summarize())."""
    now = now or time.time()
    if (status.get("idle_timeout") or {}).get("state") == "Printing":
        _last_busy[host] = now
    state = (status.get("print_stats") or {}).get("state", "standby")
    if state == "standby" and now - _last_busy.get(host, 0) < BUSY_HOLD_SECS:
        return "busy"
    return state


def done_with(host, last_job, light_is_on, now=None):
    """Whether a finished job is done with (see DONE_HOLD_SECS). last_job: the
    job that finished ({"endTime"}); light_is_on: True, False or None (no
    light, or it can't tell)."""
    now = now or time.time()
    if light_is_on is not None:
        if _light_was_on.get(host) and not light_is_on:
            _light_went_off[host] = now
        _light_was_on[host] = light_is_on
    ended = (last_job or {}).get("endTime")
    if not ended:
        return False
    return _light_went_off.get(host, 0) > ended or now - ended >= DONE_HOLD_SECS


# Preview pictures of the model being printed. Slicers embed them at the top
# of the G-code as base64 comments ("; thumbnail begin 300x300 16160", then
# "; iVBOR...", then "; thumbnail end"); Moonraker extracts them for the
# slicers it knows, but not Creality Print's, so the server also reads them
# from the file itself. Only the file's first THUMBNAIL_READ_BYTES are read.
THUMBNAIL_READ_BYTES = 512 * 1024
THUMBNAIL_BLOCK = re.compile(
    rb"^; ?thumbnail(?:_(JPG|PNG))? begin (\d+)x(\d+) \d+\r?\n(.*?)^; ?thumbnail(?:_(?:JPG|PNG))? end", re.M | re.S)
_thumbnails = {}  # (host, filename) -> (content type, image bytes), or None if it has none


def embedded_thumbnail(head):
    """The largest preview picture in the start of a G-code file, as
    (content type, image bytes), or None. PNG or JPEG."""
    best = None
    for kind, width, height, body in ((m[1], int(m[2]), int(m[3]), m[4]) for m in THUMBNAIL_BLOCK.finditer(head)):
        if best and width * height <= best[0]:
            continue
        data = b"".join(line.lstrip(b";").strip() for line in body.splitlines())
        try:
            image = base64.b64decode(data, validate=True)
        except ValueError:
            continue
        best = (width * height, "image/jpeg" if kind == b"JPG" else "image/png", image)
    return best[1:] if best else None


def file_thumbnail(host, filename):
    """The preview picture for a G-code file on the printer (cached): the
    largest one Moonraker extracted, else one read from the file."""
    if not host or ".." in filename.split("/") or filename.startswith("/"):
        return None  # only files in the printer's G-code folder
    key = (host, filename)
    if key in _thumbnails:
        return _thumbnails[key]
    quoted = urllib.parse.quote(filename)
    thumbnail = None
    thumbs = file_metadata(host, filename).get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        folder = os.path.dirname(filename)
        path = urllib.parse.quote(f"{folder}/{best['relative_path']}" if folder else best["relative_path"])
        with urllib.request.urlopen(f"http://{host}:{MOONRAKER_PORT}/server/files/gcodes/{path}",
                                    timeout=REQUEST_TIMEOUT_SECS) as response:
            thumbnail = (response.headers.get("Content-Type") or "image/png", response.read())
    else:
        request = urllib.request.Request(f"http://{host}:{MOONRAKER_PORT}/server/files/gcodes/{quoted}",
                                         headers={"Range": f"bytes=0-{THUMBNAIL_READ_BYTES - 1}"})
        with urllib.request.urlopen(request, timeout=10) as response:
            thumbnail = embedded_thumbnail(response.read(THUMBNAIL_READ_BYTES))
    _thumbnails[key] = thumbnail
    return thumbnail


# The layer preview: the printing file, read into layers (gcode_preview.py)
# in the background, once per job. Only the latest job is kept.
_previews = {}  # (host, filename) -> GcodeIndex, "reading", or "failed"
_previews_lock = threading.Lock()


def gcode_preview_for(host, filename):
    """The printing file's layers, or None while they're being read (or if
    reading failed). Starts reading on first ask."""
    key = (host, filename)
    with _previews_lock:
        found = _previews.get(key)
        if found is None:
            _previews.clear()  # a new job: drop the last one's
            _previews[key] = "reading"
            threading.Thread(target=read_preview, args=(key,), daemon=True).start()
    return found if isinstance(found, gcode_preview.GcodeIndex) else None


def read_preview(key):
    host, filename = key
    try:
        url = f"http://{host}:{MOONRAKER_PORT}/server/files/gcodes/{urllib.parse.quote(filename)}"
        with urllib.request.urlopen(url, timeout=60) as response:
            result = gcode_preview.read_gcode(response)
    except (OSError, ValueError) as e:
        print(f"Layer preview: couldn't read {filename}: {e}", flush=True)
        result = "failed"
    with _previews_lock:
        if key in _previews:
            _previews[key] = result


# Temperature history for the dashboard's graphs: one point every
# HISTORY_STEP_SECS from Moonraker's store (one reading a second, kept for
# 10 minutes by default).
HISTORY_STEP_SECS = 5


def temperature_history(host):
    """Recent temperatures, oldest first: {"step": 5, "nozzle": {"temps":
    [...], "targets": [...]}, "bed": ..., "chamber": ...}. The chamber's
    targets come from its heater, if it has one."""
    profile = printer_profile(host)
    store = moonraker_get("/server/temperature_store?include_monitors=false", host)
    sources = {"nozzle": ("extruder", "extruder"), "bed": ("heater_bed", "heater_bed"),
               "chamber": (profile.get("chamberSensor"), profile.get("chamberHeater"))}
    history = {"step": HISTORY_STEP_SECS}
    for key, (sensor, heater) in sources.items():
        temps = (store.get(sensor) or {}).get("temperatures") if sensor else None
        if not temps:
            continue
        targets = (store.get(heater) or {}).get("targets") if heater else None
        # The newest reading last; every HISTORY_STEP_SECS-th one, counting back from it.
        picks = range(len(temps) - 1, -1, -HISTORY_STEP_SECS)
        history[key] = {
            "temps": [round(temps[i], 1) for i in reversed(picks)],
            "targets": [round(targets[i], 1) for i in reversed(picks)] if targets and len(targets) == len(temps) else None,
        }
    return history


def fetch_status():
    host = printer_host()
    profile = printer_profile(host)
    status = objects_query(host, profile["objects"])
    stats = status.get("print_stats") or {}
    metadata = None
    if stats.get("filename") and not current_job(status).get("metadata"):
        metadata = file_metadata(host, stats["filename"])
    try:
        console = moonraker_get(f"/server/gcode_store?count={CONSOLE_LINES}", host)["gcode_store"]
    except (OSError, ValueError, KeyError):
        console = []
    last_job = None
    if (status.get("print_stats") or {}).get("state") not in ("printing", "paused"):
        try:
            jobs = moonraker_get("/server/history/list?limit=1&order=desc", host)["jobs"]
            if jobs:
                last_job = {
                    "filename": jobs[0].get("filename"),
                    "status": jobs[0].get("status"),
                    "endTime": jobs[0].get("end_time"),
                    "duration": jobs[0].get("print_duration") or jobs[0].get("total_duration"),
                }
        except (OSError, ValueError, KeyError):
            pass
    now = time.time()
    recently_busy = printer_state(host, status, now) == "busy"
    summary = summarize(status, console, last_job, now, profile, metadata, recently_busy)
    light = profile.get("light")
    power_state = None
    if light and light["kind"] == "power":
        try:
            power_state = moonraker_get(
                f"/machine/device_power/device?device={urllib.parse.quote(light['name'])}", host).get(light["name"])
        except (OSError, ValueError, KeyError, AttributeError):
            pass
    on = light_on(status, light, power_state)
    summary["light"] = None if on is None else {"on": on}
    if done_with(host, last_job, on, now) and summary["state"] == "complete":
        summary.update(state="standby", filename=None, thumbnail=None)
    summary["heaterLimits"] = {key: heater["max"] for key, heater in profile.get("heaters", {}).items()}
    # The layer preview, while a job is printing: which layer, and how far
    # through the file (bytes) the printer is.
    summary["preview"] = None
    summary["layerProgress"] = None
    if summary["state"] in ("printing", "paused") and stats.get("filename"):
        index = gcode_preview_for(host, stats["filename"])
        position = (status.get("virtual_sdcard") or {}).get("file_position") or 0
        summary["preview"] = {"ready": index is not None, "position": position,
                              "layer": index.layer_at(position) if index else None,
                              "layers": len(index.layers) if index else None}
        if index:
            summary["layerProgress"] = layer_progress(index, position, stats.get("print_duration") or 0)
            overall = print_progress(index, position, stats.get("print_duration") or 0)
            if overall:
                progress, left = overall
                summary.update(progress=progress, timeLeft=left, finishAt=summary["time"] + left)
    summary["axisLimits"] = profile.get("axes", {})
    return summary


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def send_json(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/config":
            host = printer_host()
            self.send_json({"printerHost": host, "printerName": read_settings().get("printerName", "Printer"),
                            **printer_extras(host), "canChange": self.is_local()})
        elif path == "/api/status":
            CASTER.note_seen(self.client_address[0])  # a cast screen checking in
            try:
                self.send_json(fetch_status())
            except (OSError, ValueError, KeyError) as e:
                self.send_json({"connected": False, "error": str(e), "time": time.time()})
        elif path == "/api/thumbnail":
            filename = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("file", [""])[0]
            try:
                thumbnail = file_thumbnail(printer_host(), filename) if filename else None
            except (OSError, ValueError, KeyError):
                thumbnail = None
            if not thumbnail:
                self.send_error(404, "no preview picture")
                return
            content_type, image = thumbnail
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(image)
        elif path == "/api/gcode-layer":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            index = _previews.get((printer_host(), query.get("file", [""])[0]))
            try:
                layer = int(query.get("layer", ["-1"])[0])
            except ValueError:
                layer = -1
            if not isinstance(index, gcode_preview.GcodeIndex) or not 0 <= layer < len(index.layers):
                self.send_error(404, "no such layer (yet)")
                return
            self.send_json(index.layer_json(layer))
        elif path == "/api/camera-stream":
            # A cast screen's camera: go2rtc's stream, relayed (go2rtc itself
            # isn't reachable from the network).
            if self.headers.get("Upgrade", "").lower() != "websocket":
                self.send_error(400, "a WebSocket request")
                return
            try:
                printer_extras(printer_host())  # (re-)registers the camera with go2rtc if it forgot it
                casting.relay_stream(self.connection, self.headers)
            except OSError as e:
                print(f"Cast: camera relay: {e}", flush=True)
            self.close_connection = True
        elif path == "/api/cast":
            if not self.is_local():
                self.send_error(403, "only available on the kiosk itself")
                return
            refresh = "refresh" in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                devices = CASTER.find(refresh)
            except Exception as e:  # pychromecast raises its own types
                print(f"Cast: couldn't search for screens: {e}", flush=True)
                devices = []
            self.send_json({"available": CASTER.available, "devices": devices})
        elif path == "/api/history":
            try:
                self.send_json(temperature_history(printer_host()))
            except (OSError, ValueError, KeyError) as e:
                self.send_json({"step": HISTORY_STEP_SECS, "error": str(e)})
        elif path == "/api/settings":
            self.send_json(read_settings())
        elif path == "/api/changelog":
            self.send_json(read_changelog())
        elif path.startswith(SYSTEM_PREFIX):
            self.system_get(path[len(SYSTEM_PREFIX):])
        else:
            super().do_GET()

    def do_PUT(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/api/settings", "/api/printer", "/api/light", "/api/control", "/api/cast") \
                and not path.startswith(SYSTEM_PREFIX):
            self.send_error(404)
            return
        if not self.is_local():
            self.send_error(403, "settings can only be changed from the kiosk itself")
            return
        body = self.read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self.send_error(400, "body must be a JSON object")
        elif path == "/api/settings":
            # The printer's address goes through /api/printer, which checks it.
            if "printerHost" in body or not all(valid_setting(k, v) for k, v in body.items()):
                self.send_error(400, "unknown setting or value")
                return
            settings = {**read_settings(), **body}
            write_settings(settings)
            self.send_json(settings)
        elif path == "/api/printer":
            self.put_printer(body)
        elif path == "/api/control":
            try:
                ok, message = control(printer_host(), body.get("action"), body)
            except (OSError, ValueError, KeyError) as e:
                ok, message = False, f"The printer didn't answer ({e})."
            self.send_json({"ok": ok, "message": message}, 200 if ok else 409)
        elif path == "/api/cast":
            action, device = body.get("action"), body.get("device")
            if action == "start" and isinstance(device, str):
                ok = CASTER.start(device)
            elif action == "stop" and isinstance(device, str):
                ok = CASTER.stop(device)
            else:
                self.send_error(400, 'action ("start" or "stop") and device required')
                return
            self.send_json({"ok": ok}, 200 if ok else 404)
        elif path == "/api/light":
            if not isinstance(body.get("on"), bool):
                self.send_error(400, "on must be true or false")
                return
            try:
                found = switch_light(printer_host(), body["on"])
            except (OSError, ValueError, KeyError):
                self.send_json({"ok": False, "message": "The printer didn't answer."}, 502)
                return
            if found:
                self.send_json({"ok": True, "on": body["on"]})
            else:
                self.send_json({"ok": False, "message": "This printer has no light to switch."}, 404)
        else:
            self.system_put(path[len(SYSTEM_PREFIX):], body)

    def read_json_body(self):
        """The request's JSON body, or None after sending an error."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self.send_error(413)
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            self.send_error(400, "body must be JSON")
            return None

    def put_printer(self, body):
        host = body.get("printerHost")
        if not is_host(host):
            self.send_json({"saved": False, "ok": False, "message": "That isn't a valid address."}, 400)
            return
        check = check_printer(host)
        saved = check["ok"] or body.get("force") is True
        if saved:
            write_settings({**read_settings(), "printerHost": host})
        self.send_json({**check, "saved": saved, "printerHost": host})

    def system_get(self, action):
        if not self.is_local():
            self.send_error(403, "only available on the kiosk itself")
        elif action == "status":
            self.send_json({**SYSTEM.status(), "fake": SYSTEM.fake, "version": VERSION})
        elif action == "networks":
            self.send_json(SYSTEM.networks())
        elif action == "countries":
            self.send_json(system.countries())
        else:
            self.send_error(404)

    def system_put(self, action, body):
        if action == "wifi":
            ssid, password = body.get("ssid"), body.get("password", "")
            if not isinstance(ssid, str) or not ssid or len(ssid) > 32 or not isinstance(password, str):
                self.send_error(400, "ssid (1-32 characters) and password (text) required")
                return
            self.send_json(SYSTEM.connect(ssid, password, bool(body.get("hidden"))))
        elif action == "country":
            code = body.get("code")
            if not isinstance(code, str) or not system.COUNTRY_CODE.match(code):
                self.send_error(400, "code must be a two-letter country code, e.g. US")
                return
            self.send_json({"ok": SYSTEM.set_country(code)})
        elif action == "timezone":
            name = body.get("timezone")
            if not isinstance(name, str) or not system.is_timezone(name):
                self.send_error(400, "unknown time zone")
                return
            self.send_json({"ok": SYSTEM.set_timezone(name)})
        elif action == "update":
            self.send_json({"ok": SYSTEM.start_update()})
        elif action == "reset":
            try:
                os.remove(os.path.join(DATA_DIR, SETTINGS_FILE))
            except FileNotFoundError:
                pass
            # Last, since it takes the kiosk offline.
            self.send_json({"ok": True, "forgotWifi": SYSTEM.forget_wifi()})
        else:
            self.send_error(404)

    def is_local(self):
        return self.client_address[0] in LOCAL_ADDRESSES

    def end_headers(self):
        # The kiosk runs for days; always pick up an updated page on reload.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format, *args):
        # The page polls every couple of seconds; only log problems.
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            super().log_message(format, *args)


if __name__ == "__main__":
    mode = " (pretend system: nothing on this computer is changed)" if SYSTEM.fake else ""
    print(f"Klipper Printer Monitor {VERSION} at http://localhost:{PORT} (printer: {printer_host() or 'not set up yet'}; "
          f"saved data in {DATA_DIR}){mode}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
