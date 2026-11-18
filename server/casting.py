"""Casting: sending the dashboard to TVs and displays on the network
(Chromecasts, Google TVs, Nest Hubs), view-only, with the camera on top and
the layer preview below while a job prints. See docs/casting.md for how this
was worked out.

Two parts:

- The camera, through go2rtc. A Cast device's browser can't take the K2's
  WebRTC camera (H.264 over WebRTC isn't offered; only VP8/VP9), but plays
  the same H.264 re-wrapped for MSE. go2rtc (a separate service, port 1984)
  does that; register_stream() points it at the printer's camera.
- The casting, through pychromecast: find the screens, tell one to open the
  dashboard (the DashCast receiver, loading the page directly), and keep it
  there. A device can drop the page without ending its Cast session, and a
  Nest Hub goes back to its photo frame after a while; both stop the page's
  status checks, so any screen quiet for a minute is cast to again.

pychromecast is optional: without it, casting just isn't offered.
"""

import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import pychromecast
    from pychromecast.controllers.dashcast import APP_DASHCAST, DashCastController
except ImportError:  # not installed: no casting
    pychromecast = None
    APP_DASHCAST = DashCastController = None

# go2rtc listens only on this machine: its API can add sources that run
# commands, so it must not be reachable from the network. Cast screens get the
# camera through relay_stream() instead.
GO2RTC_HOST = os.environ.get("KPM_GO2RTC_HOST", "127.0.0.1")
GO2RTC_PORT = int(os.environ.get("KPM_GO2RTC_PORT", "1984"))
GO2RTC_API = f"http://{GO2RTC_HOST}:{GO2RTC_PORT}"
STREAM = "camera"  # the go2rtc stream name cast screens play

# A cast screen checks the printer's status every 2 seconds; quiet this long,
# it's lost the page, and is cast to again.
QUIET_SECS = 60
CHECK_EVERY_SECS = 15
# Starting a receiver on a device that's been idle can be slow; a page address
# sent before it's ready is silently dropped ("Waiting for address").
START_WAIT_SECS = 20
READY_SECS = 2  # after the device says the receiver's running, before it truly listens
LOAD_WAIT_SECS = 15
TRIES = 3


# ---- go2rtc ----

_registered = None  # the source go2rtc was last given


def go2rtc_source(camera):
    """What go2rtc should read for a camera from find_camera(), or None when
    cast screens can play it as it is (an MJPEG stream) or it's unknown."""
    if camera and camera.get("type") == "webrtc" and camera.get("signalUrl"):
        return f"webrtc:{camera['signalUrl']}#format=creality"
    return None


def register_stream(source):
    """Points go2rtc's STREAM at a camera source. Returns whether go2rtc has
    it (False if go2rtc isn't running)."""
    global _registered
    if not source:
        return False
    if source == _registered and go2rtc_has_stream():
        return True
    query = urllib.parse.urlencode({"name": STREAM, "src": source})
    try:
        request = urllib.request.Request(f"{GO2RTC_API}/api/streams?{query}", method="PUT")
        with urllib.request.urlopen(request, timeout=3):
            pass
    except OSError:
        return False
    _registered = source
    return True


def go2rtc_has_stream():
    """Whether go2rtc still has STREAM (it forgets it if restarted)."""
    try:
        with urllib.request.urlopen(f"{GO2RTC_API}/api/streams?src={STREAM}", timeout=2):
            return True
    except urllib.error.HTTPError as e:
        e.close()
        return False
    except OSError:
        return False


def relay_stream(client, request_headers):
    """Relays a cast screen's WebSocket for the camera (the dashboard's
    /api/camera-stream) to go2rtc's, for STREAM only: the request goes on to
    go2rtc as /api/ws?src=STREAM, then bytes are passed both ways until either
    side closes. client: the screen's socket, after its request was read."""
    upstream = socket.create_connection((GO2RTC_HOST, GO2RTC_PORT), timeout=5)
    upstream.settimeout(None)
    # Not the screen's Host or Origin: go2rtc turns away a WebSocket from a
    # page on another site (the dashboard's), and only this relay reaches it.
    headers = "".join(f"{k}: {v}\r\n" for k, v in request_headers.items() if k.lower() not in ("host", "origin"))
    upstream.sendall(f"GET /api/ws?src={STREAM} HTTP/1.1\r\nHost: {GO2RTC_HOST}:{GO2RTC_PORT}\r\n{headers}\r\n".encode())

    def pump(source, sink):
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for s in (source, sink):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
    back.start()
    pump(client, upstream)
    back.join()
    upstream.close()


# ---- casting ----

def local_address(host):
    """This machine's address on the network a device is on (for the page
    address it's sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 8009))  # no packets are sent for a UDP connect
        return s.getsockname()[0]


class Caster:
    """The screens found, and which are being cast to."""

    def __init__(self, port, cast_module=None):
        self.port = port
        self.cc = cast_module if cast_module is not None else pychromecast
        self.devices = {}  # id -> Chromecast
        self.browser_of = {}  # id -> the discovery its device came from (it relies on it to reconnect)
        self.loaders = {}  # id -> its DashCastController (one per device)
        self.found_at = 0
        self.active = {}  # id -> {"name", "host", "tried"}
        self.seen = {}  # device address -> when it last checked the printer's status
        self.lock = threading.Lock()
        self.watching = False

    @property
    def available(self):
        return self.cc is not None

    def note_seen(self, address):
        """A request from a device: a cast screen checking the status."""
        self.seen[address] = time.time()

    def find(self, refresh=False):
        """The screens on the network: Chromecasts, Google TVs and Nest Hubs,
        not speakers or speaker groups. Searched for only when asked
        (refresh: the Cast menu opening, or its Refresh button) or the first
        time; otherwise the last search's."""
        if not self.available:
            return []
        with self.lock:
            if refresh or not self.found_at:
                casts, browser = self.cc.get_chromecasts(timeout=5)
                found = {str(c.cast_info.uuid): c for c in casts if c.cast_info.cast_type == "cast"}
                browser_of = {device_id: browser for device_id in found}
                # Screens being cast to that missed this search keep their old
                # connection, and with it the old discovery.
                for device_id in self.active:
                    if device_id not in found and device_id in self.devices:
                        found[device_id] = self.devices[device_id]
                        browser_of[device_id] = self.browser_of[device_id]
                # Connections this search replaced are closed before their
                # discovery is stopped: left open, one whose screen drops off
                # (a TV switched off) retries through the stopped discovery
                # in a tight loop, forever.
                # (Through its socket client: Chromecast.disconnect() then
                # waits for the connection's thread, and raises if that hasn't
                # stopped yet or never started, as for a screen not cast to.)
                for device_id, old in self.devices.items():
                    if found.get(device_id) is not old:
                        try:
                            old.socket_client.disconnect()
                        except Exception as e:  # pychromecast raises its own types
                            print(f"Cast: couldn't close the old connection to {old.cast_info.friendly_name}: {e}", flush=True)
                # Then any discovery no screen relies on now (this one too, if
                # it found none) is stopped.
                in_use = set(map(id, browser_of.values()))
                for each in {id(b): b for b in [*self.browser_of.values(), browser]}.values():
                    if id(each) not in in_use:
                        each.stop_discovery()
                self.browser_of = browser_of
                self.loaders = {i: d for i, d in self.loaders.items() if found.get(i) is self.devices.get(i)}
                self.devices = found
                self.found_at = time.time()
            return [{
                "id": device_id,
                "name": c.cast_info.friendly_name,
                "model": c.cast_info.model_name,
                "casting": device_id in self.active,
            } for device_id, c in sorted(self.devices.items(), key=lambda item: item[1].cast_info.friendly_name)]

    def start(self, device_id):
        """Casts to a screen (in the background). False if it isn't known."""
        device = self.devices.get(device_id)
        if not device:
            return False
        self.active[device_id] = {"name": device.cast_info.friendly_name, "host": device.cast_info.host, "tried": 0}
        threading.Thread(target=self.cast, args=(device_id,), daemon=True).start()
        self.watch()
        return True

    def stop(self, device_id):
        """Stops casting to a screen."""
        self.active.pop(device_id, None)
        device = self.devices.get(device_id)
        if device:
            try:
                device.wait(timeout=10)
                device.quit_app()
            except Exception as e:  # pychromecast raises its own types
                print(f"Cast: couldn't stop {device.cast_info.friendly_name}: {e}", flush=True)
        return True

    def cast(self, device_id):
        """Gets the dashboard showing on a screen, patiently: start the
        receiver, wait until it's running, send the page address, and check
        the screen actually asks for the page. Returns whether it did."""
        device, entry = self.devices.get(device_id), self.active.get(device_id)
        if not device or not entry:
            return False
        entry["tried"] = time.time()
        host = device.cast_info.host
        url = f"http://{local_address(host)}:{self.port}/?cast"
        try:
            device.wait(timeout=10)
            dash = self.loaders.get(device_id)
            if dash is None:
                dash = self.loaders[device_id] = DashCastController()
                device.register_handler(dash)
            for attempt in range(1, TRIES + 1):
                if device_id not in self.active:
                    return False  # stopped meanwhile
                if device.status and device.status.app_id:
                    device.quit_app()
                    wait_for(lambda: not device.status.app_id, 10)
                device.start_app(APP_DASHCAST)
                if not wait_for(lambda: device.status.app_id == APP_DASHCAST, START_WAIT_SECS):
                    continue
                time.sleep(READY_SECS)
                sent = time.time()
                dash.load_url(url, force=True)
                if wait_for(lambda: self.seen.get(host, 0) > sent, LOAD_WAIT_SECS):
                    print(f"Cast: showing on {entry['name']} (try {attempt})", flush=True)
                    return True
        except Exception as e:  # pychromecast raises its own types
            print(f"Cast: couldn't cast to {entry['name']}: {e}", flush=True)
            return False
        print(f"Cast: {entry['name']} didn't take it after {TRIES} tries", flush=True)
        return False

    def watch(self):
        """Starts the watchdog (once): re-casts any screen gone quiet."""
        if self.watching:
            return
        self.watching = True
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while True:
            time.sleep(CHECK_EVERY_SECS)
            self.check()

    def check(self, now=None):
        """One round of the watchdog. Returns the ids it re-cast to.

        A quiet screen now running some other app was taken over by someone
        (say, casting a video from a phone): it's let go, not cast over."""
        now = now or time.time()
        recast = []
        for device_id, entry in list(self.active.items()):
            quiet = now - self.seen.get(entry["host"], 0) > QUIET_SECS
            device = self.devices.get(device_id)
            app = device.status.app_id if device is not None and device.status else None
            if quiet and app and app != APP_DASHCAST:
                print(f"Cast: {entry['name']} is showing something else now; letting it go", flush=True)
                self.active.pop(device_id, None)
                continue
            if quiet and now - entry["tried"] > QUIET_SECS:
                entry["tried"] = now
                recast.append(device_id)
                threading.Thread(target=self.cast, args=(device_id,), daemon=True).start()
        return recast


def wait_for(check, secs):
    end = time.time() + secs
    while time.time() < end:
        if check():
            return True
        time.sleep(0.5)
    return False
