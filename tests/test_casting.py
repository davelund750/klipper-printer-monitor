"""Tests for casting.py, with a stand-in for pychromecast (no real devices)
and a stand-in go2rtc. Run with: python3 -m unittest discover -s tests"""

import json
import os
import re
import socket
import sys
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, "server"))

import casting  # noqa: E402

DASHCAST = "84912283"


class FakeDevice:
    """A Cast device: runs whatever app it's told to; on being sent a page,
    "loads" it by calling on_load (the test makes that a status check-in)."""

    def __init__(self, name, cast_type="cast", host="10.0.0.5", takes_tries=1):
        self.cast_info = SimpleNamespace(uuid=uuid.uuid4(), friendly_name=name, model_name="Chromecast",
                                         cast_type=cast_type, host=host)
        self.status = SimpleNamespace(app_id=None)
        self.loads = []
        self.handlers = []
        self.takes_tries = takes_tries  # how many loads until one "takes"
        self.on_load = None
        self.disconnected = False
        self.socket_client = SimpleNamespace(disconnect=lambda: setattr(self, "disconnected", True))

    def again(self):
        """The same device, as a new search finds it: a new connection."""
        copy = FakeDevice(self.cast_info.friendly_name, self.cast_info.cast_type, self.cast_info.host)
        copy.cast_info.uuid = self.cast_info.uuid
        return copy

    def disconnect(self, timeout=None):
        # As pychromecast does for a connection it never started (one to a
        # screen that wasn't cast to).
        raise RuntimeError("cannot join thread before it is started")

    def wait(self, timeout=None):
        pass

    def register_handler(self, handler):
        self.handlers.append(handler)
        handler.device = self

    def start_app(self, app_id):
        self.status.app_id = app_id

    def quit_app(self):
        self.status.app_id = None


class FakeDashCast:
    def load_url(self, url, force=False):
        device = self.device
        device.loads.append(url)
        if len(device.loads) >= device.takes_tries and device.on_load:
            device.on_load(url)


class FakeBrowser:
    def __init__(self):
        self.stopped = False

    def stop_discovery(self):
        self.stopped = True


class FakePychromecast:
    def __init__(self, devices):
        self.devices = devices
        self.browsers = []

    def get_chromecasts(self, timeout=None):
        self.browsers.append(FakeBrowser())
        return list(self.devices), self.browsers[-1]


class CasterTest(unittest.TestCase):
    def setUp(self):
        self.saved = (casting.APP_DASHCAST, casting.DashCastController, casting.START_WAIT_SECS,
                      casting.LOAD_WAIT_SECS, casting.READY_SECS, casting.local_address)
        casting.APP_DASHCAST, casting.DashCastController = DASHCAST, FakeDashCast
        casting.START_WAIT_SECS, casting.LOAD_WAIT_SECS, casting.READY_SECS = 1, 0.6, 0
        self.tv = FakeDevice("Office TV")
        self.hub = FakeDevice("Living Room Display", host="10.0.0.6")
        self.speaker = FakeDevice("Kitchen speaker", cast_type="audio", host="10.0.0.7")
        self.group = FakeDevice("All Speakers", cast_type="group", host="10.0.0.8")
        self.cc = FakePychromecast([self.tv, self.hub, self.speaker, self.group])
        self.caster = casting.Caster(8000, cast_module=self.cc)
        self.caster.watch = lambda: None  # no background watchdog in tests
        casting.local_address = lambda host: "10.0.0.2"

    def tearDown(self):
        (casting.APP_DASHCAST, casting.DashCastController, casting.START_WAIT_SECS,
         casting.LOAD_WAIT_SECS, casting.READY_SECS, casting.local_address) = self.saved

    def id_of(self, device):
        return str(device.cast_info.uuid)

    def test_finds_screens_not_speakers(self):
        names = [d["name"] for d in self.caster.find()]
        self.assertEqual(names, ["Living Room Display", "Office TV"])

    def test_searches_only_when_asked(self):
        self.caster.find()
        self.caster.found_at -= 24 * 3600  # however long ago
        self.caster.find()
        self.assertEqual(len(self.cc.browsers), 1)
        self.caster.find(refresh=True)
        self.assertEqual(len(self.cc.browsers), 2)

    def test_not_available_without_pychromecast(self):
        caster = casting.Caster(8000, cast_module=None)
        caster.cc = None
        self.assertFalse(caster.available)
        self.assertEqual(caster.find(), [])

    def test_keeps_the_discovery_running_for_the_devices(self):
        # The devices rely on it to be reached; it's only stopped once a new
        # search replaces it.
        self.caster.find()
        self.assertFalse(self.cc.browsers[0].stopped)
        self.caster.find(refresh=True)
        self.assertTrue(self.cc.browsers[0].stopped)
        self.assertFalse(self.cc.browsers[1].stopped)

    def test_closes_connections_a_new_search_replaces(self):
        # A new search makes new connections. The old ones are closed and
        # their discovery stopped; left open, a dropped one retried through
        # the stopped discovery in a tight loop (a 5 GB log).
        self.caster.find()
        old_tv = self.tv
        self.cc.devices = [d.again() for d in self.cc.devices]
        self.caster.find(refresh=True)
        self.assertTrue(old_tv.disconnected)
        self.assertTrue(self.cc.browsers[0].stopped)
        self.assertFalse(self.cc.browsers[1].stopped)
        self.assertFalse(any(d.disconnected for d in self.cc.devices))

    def test_a_screen_cast_to_keeps_its_connection_when_a_search_misses_it(self):
        self.caster.find()
        tv_id = self.id_of(self.tv)
        self.caster.active[tv_id] = {"name": "Office TV", "host": self.tv.cast_info.host, "tried": 0}
        old_tv = self.tv
        self.cc.devices = [self.hub.again()]  # the TV missed this search
        self.caster.find(refresh=True)
        self.assertIs(self.caster.devices[tv_id], old_tv)
        self.assertFalse(old_tv.disconnected)
        self.assertFalse(self.cc.browsers[0].stopped)  # the TV still relies on it
        # Found again: the old connection and its discovery are done with.
        self.cc.devices = [old_tv.again()]
        self.caster.find(refresh=True)
        self.assertTrue(old_tv.disconnected)
        self.assertTrue(self.cc.browsers[0].stopped)
        self.assertTrue(self.cc.browsers[1].stopped)  # its only screen (the hub) is gone
        self.assertFalse(self.cc.browsers[2].stopped)

    def test_a_search_finding_nothing_stops(self):
        self.cc.devices = []
        self.assertEqual(self.caster.find(), [])
        self.assertTrue(self.cc.browsers[0].stopped)

    def test_casts_the_dashboard_and_confirms_it_loaded(self):
        self.caster.find()
        self.tv.on_load = lambda url: self.caster.note_seen(self.tv.cast_info.host)
        self.caster.active[self.id_of(self.tv)] = {"name": "Office TV", "host": self.tv.cast_info.host, "tried": 0}
        self.assertTrue(self.caster.cast(self.id_of(self.tv)))
        self.assertEqual(self.tv.loads, ["http://10.0.0.2:8000/?cast"])
        self.assertEqual(self.tv.status.app_id, DASHCAST)
        self.assertEqual(len(self.tv.handlers), 1)

    def test_tries_again_when_the_page_is_dropped(self):
        # The first address is silently dropped ("Waiting for address").
        self.caster.find()
        self.tv.takes_tries = 2
        self.tv.on_load = lambda url: self.caster.note_seen(self.tv.cast_info.host)
        self.caster.active[self.id_of(self.tv)] = {"name": "Office TV", "host": self.tv.cast_info.host, "tried": 0}
        self.assertTrue(self.caster.cast(self.id_of(self.tv)))
        self.assertEqual(len(self.tv.loads), 2)
        self.assertEqual(len(self.tv.handlers), 1)  # one page loader per device, however many tries

    def test_gives_up_after_three_tries(self):
        self.caster.find()
        self.caster.active[self.id_of(self.tv)] = {"name": "Office TV", "host": self.tv.cast_info.host, "tried": 0}
        self.assertFalse(self.caster.cast(self.id_of(self.tv)))
        self.assertEqual(len(self.tv.loads), 3)

    def test_start_and_stop(self):
        self.caster.find()
        device_id = self.id_of(self.tv)
        self.assertTrue(self.caster.start(device_id))
        self.assertTrue([d for d in self.caster.find() if d["id"] == device_id][0]["casting"])
        self.caster.stop(device_id)
        self.assertNotIn(device_id, self.caster.active)
        self.assertIsNone(self.tv.status.app_id)
        self.assertFalse(self.caster.start("no such device"))

    def test_lets_go_of_a_screen_someone_else_is_using(self):
        self.caster.find()
        cast_to = []
        self.caster.cast = lambda device_id: cast_to.append(device_id)
        now = time.time()
        tv = self.id_of(self.tv)
        self.caster.active = {tv: {"name": "TV", "host": "10.0.0.5", "tried": now - 300}}
        self.caster.seen = {"10.0.0.5": now - 120}
        self.tv.status.app_id = "233637DE"  # e.g. YouTube, cast from a phone
        self.assertEqual(self.caster.check(now), [])
        self.assertNotIn(tv, self.caster.active)

    def test_recasts_quiet_screens_only(self):
        self.caster.find()
        cast_to = []
        self.caster.cast = lambda device_id: cast_to.append(device_id)
        now = time.time()
        tv, hub = self.id_of(self.tv), self.id_of(self.hub)
        self.caster.active = {tv: {"name": "TV", "host": "10.0.0.5", "tried": now - 300},
                              hub: {"name": "Hub", "host": "10.0.0.6", "tried": now - 300}}
        self.caster.seen = {"10.0.0.5": now - 5, "10.0.0.6": now - 120}  # the Hub went quiet
        self.assertEqual(self.caster.check(now), [hub])
        # Not again straight away: it was just tried.
        self.assertEqual(self.caster.check(now + 10), [])


class FakeGo2rtc(BaseHTTPRequestHandler):
    streams = {}

    def do_PUT(self):
        query = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&"))
        FakeGo2rtc.streams[query["name"]] = query["src"]
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        name = self.path.split("src=", 1)[-1]
        self.send_response(200 if name in FakeGo2rtc.streams else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


class Go2rtcTest(unittest.TestCase):
    def setUp(self):
        FakeGo2rtc.streams = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGo2rtc)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.saved = casting.GO2RTC_API, casting._registered
        casting.GO2RTC_API = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        casting._registered = None

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        casting.GO2RTC_API, casting._registered = self.saved

    def test_source_for_the_k2_camera_only(self):
        k2 = {"type": "webrtc", "signalUrl": "http://10.0.0.9:8000/call/webrtc_local"}
        self.assertEqual(casting.go2rtc_source(k2), "webrtc:http://10.0.0.9:8000/call/webrtc_local#format=creality")
        self.assertIsNone(casting.go2rtc_source({"type": "mjpeg", "url": "http://x/stream"}))
        self.assertIsNone(casting.go2rtc_source(None))

    def test_registers_and_re_registers_after_a_restart(self):
        source = "webrtc:http://10.0.0.9:8000/call/webrtc_local#format=creality"
        self.assertTrue(casting.register_stream(source))
        self.assertIn("camera", FakeGo2rtc.streams)
        FakeGo2rtc.streams.clear()  # go2rtc restarted and forgot it
        self.assertTrue(casting.register_stream(source))
        self.assertIn("camera", FakeGo2rtc.streams)

    def test_go2rtc_not_running(self):
        casting.GO2RTC_API = "http://127.0.0.1:9"  # nothing listens there
        self.assertFalse(casting.register_stream("webrtc:http://x#format=creality"))


class RelayTest(unittest.TestCase):
    def test_passes_the_camera_stream_both_ways_and_only_that_stream(self):
        upstream = socket.socket()
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        seen = {}

        def go2rtc():
            conn, _ = upstream.accept()
            seen["request"] = conn.recv(4096).decode()
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\nvideo bytes")
            seen["from screen"] = conn.recv(4096)
            conn.close()

        threading.Thread(target=go2rtc, daemon=True).start()
        saved = casting.GO2RTC_HOST, casting.GO2RTC_PORT
        casting.GO2RTC_HOST, casting.GO2RTC_PORT = "127.0.0.1", upstream.getsockname()[1]
        screen, server_side = socket.socketpair()
        try:
            relay = threading.Thread(target=casting.relay_stream, daemon=True,
                                     args=(server_side, {"Upgrade": "websocket", "Host": "pi:8000",
                                                         "Sec-WebSocket-Key": "abc", "Origin": "http://pi:8000"}))
            relay.start()
            reply = b""
            while b"video bytes" not in reply:
                reply += screen.recv(4096)
            screen.sendall(b"hello")
            relay.join(timeout=5)
        finally:
            casting.GO2RTC_HOST, casting.GO2RTC_PORT = saved
            screen.close()
            server_side.close()
            upstream.close()
        self.assertTrue(seen["request"].startswith("GET /api/ws?src=camera HTTP/1.1\r\n"))
        self.assertIn("Sec-WebSocket-Key: abc", seen["request"])
        # go2rtc would turn away the dashboard's origin.
        self.assertNotIn("Origin", seen["request"])
        self.assertNotIn("pi:8000", seen["request"])
        self.assertEqual(seen["from screen"], b"hello")


class InstallTest(unittest.TestCase):
    def test_go2rtc_only_listens_on_the_pi_itself(self):
        # Its API can add sources that run commands: never on the network.
        with open(os.path.join(PROJECT_DIR, "install.sh")) as f:
            install = f.read()
        config = install.split("go2rtc.yaml >/dev/null <<'EOF'", 1)[1].split("\nEOF", 1)[0]
        self.assertEqual(re.findall(r'^  listen: "([^"]*)"', config, re.M), ["127.0.0.1:1984", "", ""])
        self.assertEqual((casting.GO2RTC_HOST, casting.GO2RTC_PORT), ("127.0.0.1", 1984))

    def test_go2rtc_download_is_pinned_and_checked(self):
        with open(os.path.join(PROJECT_DIR, "install.sh")) as f:
            install = f.read()
        self.assertRegex(install, r"GO2RTC_VERSION=v\d+\.\d+\.\d+\n")
        self.assertEqual(len(re.findall(r"GO2RTC_SHA256=[0-9a-f]{64}\b", install)), 2)
        self.assertIn('sha256sum -c --status', install)


if __name__ == "__main__":
    unittest.main()
