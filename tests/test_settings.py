import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

import server  # noqa: E402


class QuietHandler(server.Handler):
    def log_message(self, format, *args):
        pass


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory()
        self.saved = (server.DATA_DIR, server.check_printer, server.SYSTEM, server.printer_extras, server.switch_light,
                      server.control)
        # The Controls view's actions are noted, not sent.
        self.controlled = []
        server.control = lambda host, action, body: (self.controlled.append(action) or (action != "eject"),
                                                     "Done." if action != "eject" else "Unknown action.")
        # A printer with a light; what was switched is noted instead.
        self.switched = []
        server.switch_light = lambda host, on: self.switched.append((host, on)) or True
        # The web interface and camera a printer would have.
        server.printer_extras = lambda host: {"uiUrl": f"http://{host}:4408", "camera": None}
        server.SYSTEM = server.system.FakeSystem()
        server.DATA_DIR = self.data_dir.name
        # Pretend a printer answers only at 192.168.1.40.
        server.check_printer = lambda host: (
            {"ok": True, "message": f"Found K2Pro at {host}."} if host == "192.168.1.40"
            else {"ok": False, "message": f"Nothing answered at {host}."})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        (server.DATA_DIR, server.check_printer, server.SYSTEM, server.printer_extras,
         server.switch_light, server.control) = self.saved
        self.data_dir.cleanup()

    def put(self, body, path="/api/printer"):
        request = urllib.request.Request(f"{self.base}{path}", data=json.dumps(body).encode(), method="PUT")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, None

    def get(self, path):
        with urllib.request.urlopen(f"{self.base}{path}") as response:
            return json.load(response)

    def config(self):
        return self.get("/api/config")

    def test_default_until_saved(self):
        self.assertEqual(server.printer_host(), server.DEFAULT_HOST)
        config = self.config()
        self.assertEqual(config["printerHost"], server.DEFAULT_HOST)
        self.assertTrue(config["canChange"])

    def test_saves_an_address_where_a_printer_answers(self):
        status, result = self.put({"printerHost": "192.168.1.40"})
        self.assertEqual(status, 200)
        self.assertTrue(result["saved"])
        self.assertEqual(server.printer_host(), "192.168.1.40")
        self.assertEqual(self.config()["uiUrl"], "http://192.168.1.40:4408")

    def test_nothing_answers_needs_force(self):
        status, result = self.put({"printerHost": "192.168.1.99"})
        self.assertEqual((status, result["saved"], result["ok"]), (200, False, False))
        self.assertEqual(server.printer_host(), server.DEFAULT_HOST)

        status, result = self.put({"printerHost": "192.168.1.99", "force": True})
        self.assertTrue(result["saved"])
        self.assertEqual(server.printer_host(), "192.168.1.99")

    def test_rejects_bad_addresses(self):
        for host in ["", "192.168.1.40/x", "http://192.168.1.40", "a b", ".", None, 7]:
            status, _ = self.put({"printerHost": host})
            self.assertEqual(status, 400, host)
        self.assertEqual(server.printer_host(), server.DEFAULT_HOST)

    def test_ignores_a_damaged_settings_file(self):
        with open(os.path.join(self.data_dir.name, "settings.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(server.printer_host(), server.DEFAULT_HOST)

    def test_other_settings_merge_in(self):
        self.put({"printerHost": "192.168.1.40"})
        status, settings = self.put({"setupComplete": True}, "/api/settings")
        self.assertEqual(status, 200)
        status, settings = self.put({"clock": "24"}, "/api/settings")
        self.assertEqual(settings, {"printerHost": "192.168.1.40", "setupComplete": True, "clock": "24"})
        status, settings = self.put({"previewLayout": "split"}, "/api/settings")
        self.assertEqual(settings["previewLayout"], "split")
        for shade in ["c", "a", "auto"]:
            status, settings = self.put({"previewBackground": shade}, "/api/settings")
            self.assertEqual(settings["previewBackground"], shade)
        for theme in ["lcars", "klingon", "standard"]:
            status, settings = self.put({"theme": theme}, "/api/settings")
            self.assertEqual(settings["theme"], theme)
        for language in ["english", "klingon"]:
            status, settings = self.put({"language": language}, "/api/settings")
            self.assertEqual(settings["language"], language)
        self.assertEqual(self.get("/api/settings"), settings)

    def test_printer_name(self):
        self.assertEqual(self.config()["printerName"], "Printer")
        status, settings = self.put({"printerName": "Workshop K2"}, "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(self.config()["printerName"], "Workshop K2")
        for name in ["", "   ", "x" * 25, "tab\there", 7]:
            status, _ = self.put({"printerName": name}, "/api/settings")
            self.assertEqual(status, 400, name)
        self.assertEqual(self.config()["printerName"], "Workshop K2")

    def test_rejects_unknown_settings_and_address_changes_there(self):
        for body in [{"clock": "25"}, {"setupComplete": "yes"}, {"color": "red"}, {"printerHost": "192.168.1.40"},
                     {"previewLayout": "side"}, {"previewBackground": "#000000"}, {"theme": "borg"}, {"language": "vulcan"}]:
            status, _ = self.put(body, "/api/settings")
            self.assertEqual(status, 400, body)
        self.assertEqual(self.get("/api/settings"), {})

    def test_factory_reset_erases_settings(self):
        self.put({"printerHost": "192.168.1.40"})
        self.put({"setupComplete": True}, "/api/settings")
        status, result = self.put({}, "/api/system/reset")
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.get("/api/settings"), {})
        self.assertEqual(server.printer_host(), server.DEFAULT_HOST)

    def test_system_status_and_countries(self):
        status = self.get("/api/system/status")
        self.assertIn("online", status)
        self.assertIn("version", status)
        self.assertIsInstance(self.get("/api/system/countries"), list)

    def test_switching_the_light(self):
        status, result = self.put({"on": False}, "/api/light")
        self.assertEqual((status, result), (200, {"ok": True, "on": False}))
        self.assertEqual(self.switched, [(server.DEFAULT_HOST, False)])
        for body in [{}, {"on": "yes"}, {"on": 1}]:
            self.assertEqual(self.put(body, "/api/light")[0], 400, body)

    def test_controls(self):
        status, result = self.put({"action": "pause"}, "/api/control")
        self.assertEqual((status, result), (200, {"ok": True, "message": "Done."}))
        self.assertEqual(self.put({"action": "eject"}, "/api/control")[0], 409)
        self.assertEqual(self.controlled, ["pause", "eject"])

    def test_no_light_to_switch(self):
        server.switch_light = lambda host, on: False
        self.assertEqual(self.put({"on": True}, "/api/light")[0], 404)

    def test_only_the_kiosk_can_change_it(self):
        original = server.LOCAL_ADDRESSES
        server.LOCAL_ADDRESSES = ()  # as if the request came from elsewhere
        try:
            status, _ = self.put({"printerHost": "192.168.1.40", "force": True})
            self.assertEqual(status, 403)
            for body, path in [({"setupComplete": True}, "/api/settings"), ({}, "/api/system/reset"),
                               ({"on": False}, "/api/light"), ({"action": "cancel"}, "/api/control"),
                               ({"code": "US"}, "/api/system/country")]:
                self.assertEqual(self.put(body, path)[0], 403, path)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get("/api/system/status")
            caught.exception.close()
            self.assertFalse(self.config()["canChange"])
        finally:
            server.LOCAL_ADDRESSES = original
        self.assertEqual(server.printer_host(), server.DEFAULT_HOST)


if __name__ == "__main__":
    unittest.main()
