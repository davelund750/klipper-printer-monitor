import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

import server  # noqa: E402

NOW = 1_800_000_000


def printing_status(**overrides):
    status = {
        "webhooks": {"state": "ready"},
        "print_stats": {"state": "printing", "filename": "part.gcode", "print_duration": 3600, "message": ""},
        "virtual_sdcard": {
            "progress": 0.5, "layer": 100, "layer_count": 200,
            "cur_print_data": {"filename": "part.gcode", "start_time": NOW - 4000, "metadata": {"estimated_time": 8000, "filament_used_g": ["12.5", "2.5"]}},
        },
        "extruder": {"temperature": 219.6, "target": 220.0},
        "heater_bed": {"temperature": 60.1, "target": 60.0},
        "temperature_sensor chamber_temp": {"temperature": 35.4},
        "heater_generic chamber_heater": {"target": 0.0},
        "box": {"T1": {"state": "connect", "filament": "B", "color_value": ["02B2A2D", "0C33F31", "-1", "-1"]}},
    }
    status.update(overrides)
    return status


class TimeLeftTest(unittest.TestCase):
    def test_uses_slicer_estimate_at_the_start(self):
        self.assertEqual(server.time_left(60, 0.01, 8000), 7940)

    def test_blends_toward_file_progress(self):
        # Slicer: 8000 - 3600 = 4400. By file: 3600 at 50%. Halfway between.
        self.assertAlmostEqual(server.time_left(3600, 0.5, 8000), 4000)

    def test_without_slicer_estimate(self):
        self.assertAlmostEqual(server.time_left(3600, 0.5, None), 3600)
        self.assertIsNone(server.time_left(10, 0.0, None))


class CfsUnitsTest(unittest.TestCase):
    def test_spool_colours_by_the_jobs_map(self):
        # The slicer's 1A printing from 2B, its 1B from 1A; the rest as named.
        box = {
            "T1": {"state": "connect", "color_value": ["0C33F31", "0F2D64A", "0E6E7E5", "0234F96"]},
            "T2": {"state": "connect", "color_value": ["02B2A2D", "02B2A2D", "0BEB39E", "-1"]},
            "map": {"T1A": "T2B", "T1B": "T1A", "T1C": "T1C", "T1D": "T1D", "T2A": "T2A", "T2D": "T2D", "T3A": "T3A"},
        }
        self.assertEqual(server.spool_colours(box), ["#2B2A2D", "#C33F31", "#E6E7E5", "#234F96", "#2B2A2D"])
        self.assertIsNone(server.spool_colours({"T1": box["T1"]}))  # no map
        self.assertIsNone(server.spool_colours(None))

    def test_lists_connected_units_only(self):
        box = {
            "T1": {"state": "connect", "dry_and_humidity": "26", "temperature": "24"},
            "T2": {"state": "connect", "dry_and_humidity": "-1", "temperature": "bad"},
            "T3": {"state": "None", "dry_and_humidity": "None"},
        }
        units = server.cfs_units(box)
        self.assertEqual([(u["unit"], u["humidity"], u["temp"]) for u in units], [(1, 26.0, 24.0), (2, None, None)])
        self.assertEqual(server.cfs_units(None), [])

    def test_slots(self):
        # The printer's real readings: 1D loaded; 1B ran out; 2B is an untagged
        # spool; 2D is empty.
        box = {
            "T1": {"state": "connect", "filament": "D", "dry_and_humidity": "26",
                   "color_value": ["02B2A2D", "0C33F31", "0E6E7E5", "0C33F31"],
                   "remain_len": ["86", "0", "86", "57"],
                   "vender": ["AB12", "none", "AB12", "AB12"]},
            "T2": {"state": "connect", "filament": "None", "dry_and_humidity": "20",
                   "color_value": ["02D76A6", "0000000", "02D76A6", "-1"],
                   "remain_len": ["1", "0", "31", "0"],
                   "vender": ["AB12", "none", "unknown", "none"]},
        }
        t1, t2 = server.cfs_units(box)
        self.assertEqual(t1["slots"][0], {"letter": "A", "color": "#2B2A2D", "remaining": 86.0, "loaded": False})
        self.assertIsNone(t1["slots"][1]["remaining"])
        self.assertEqual([s["loaded"] for s in t1["slots"]], [False, False, False, True])
        self.assertEqual(t2["slots"][0]["remaining"], 1.0)
        self.assertEqual((t2["slots"][1]["color"], t2["slots"][1]["remaining"]), ("#000000", None))
        self.assertEqual(t2["slots"][2]["remaining"], 31.0)
        self.assertIsNone(t2["slots"][3]["color"])
        self.assertFalse(any(s["loaded"] for s in t2["slots"]))


K2_PROFILE = {"chamberSensor": "temperature_sensor chamber_temp", "chamberHeater": "heater_generic chamber_heater"}


class ChamberObjectsTest(unittest.TestCase):
    def test_k2(self):
        names = ["temperature_sensor mcu_temp", "temperature_sensor chamber_temp", "heater_generic chamber_heater",
                 "temperature_fan chamber_fan", "extruder"]
        self.assertEqual(server.chamber_objects(names),
                         ("temperature_sensor chamber_temp", "heater_generic chamber_heater"))

    def test_a_sensor_alone(self):
        self.assertEqual(server.chamber_objects(["temperature_sensor Chamber", "temperature_sensor raspberry_pi"]),
                         ("temperature_sensor Chamber", None))

    def test_a_heater_is_its_own_sensor(self):
        self.assertEqual(server.chamber_objects(["heater_generic chamber"]),
                         ("heater_generic chamber", "heater_generic chamber"))

    def test_a_temperature_fan_when_nothing_else(self):
        self.assertEqual(server.chamber_objects(["temperature_fan chamber_exhaust"]),
                         ("temperature_fan chamber_exhaust", None))

    def test_none(self):
        self.assertEqual(server.chamber_objects(["extruder", "heater_bed", "temperature_sensor mcu_temp"]),
                         (None, None))


class StandardKlipperTest(unittest.TestCase):
    """A printer that isn't a Creality: no virtual_sdcard layers or job
    details, no CFS."""

    def status(self, **info):
        return {
            "webhooks": {"state": "ready"},
            "print_stats": {"state": "printing", "filename": "benchy.gcode", "print_duration": 600,
                            "total_duration": 700, "info": {"current_layer": None, "total_layer": None, **info}},
            "virtual_sdcard": {"progress": 0.25, "is_active": True},
            "extruder": {"temperature": 210.0, "target": 210.0},
            "heater_bed": {"temperature": 60.0, "target": 60.0},
            "box": {},
        }

    def test_layers_from_print_stats_info(self):
        s = server.summarize(self.status(current_layer=12, total_layer=80), [], None, NOW)
        self.assertEqual((s["layer"], s["layerCount"]), (12, 80))

    def test_layer_count_from_file_metadata(self):
        s = server.summarize(self.status(), [], None, NOW, None, {"layer_count": 90})
        self.assertEqual((s["layer"], s["layerCount"]), (None, 90))

    def test_time_and_filament_from_file_metadata(self):
        s = server.summarize(self.status(), [], None, NOW, None, {"estimated_time": 2400, "filament_weight_total": 12.5})
        # Slicer: 2400 - 600 = 1800 left. By file: 600 at 25% = 1800 left.
        self.assertAlmostEqual(s["timeLeft"], 1800)
        self.assertEqual(s["filamentGrams"], 12.5)

    def test_no_chamber_and_no_cfs(self):
        s = server.summarize(self.status(), [], None, NOW, {"chamberSensor": None, "chamberHeater": None})
        self.assertIsNone(s["temps"]["chamber"])
        self.assertEqual(s["cfs"], [])

    def test_a_chamber_sensor_by_another_name(self):
        status = {**self.status(), "temperature_sensor Chamber": {"temperature": 41.2}}
        s = server.summarize(status, [], None, NOW, {"chamberSensor": "temperature_sensor Chamber", "chamberHeater": None})
        self.assertEqual(s["temps"]["chamber"], {"temp": 41.2, "target": None})

    def test_errors_since_the_job_started(self):
        lines = [{"time": NOW - 800, "message": "!! from the job before"},
                 {"time": NOW - 50, "message": "!! Move out of range"}]
        s = server.summarize(self.status(), lines, None, NOW)  # started 700 s ago
        self.assertEqual([e["message"] for e in s["errors"]], ["Move out of range"])


class LightTest(unittest.TestCase):
    def test_k2_led_pin(self):
        names = ["output_pin fan0", "output_pin LED", "output_pin power", "extruder"]
        self.assertEqual(server.find_light(names), {"kind": "pin", "name": "LED", "object": "output_pin LED"})

    def test_case_light_pin_before_a_strip(self):
        light = server.find_light(["neopixel toolhead", "output_pin caselight"])
        self.assertEqual(light["object"], "output_pin caselight")

    def test_led_strip(self):
        self.assertEqual(server.find_light(["neopixel sb_leds", "output_pin fan"]),
                         {"kind": "led", "name": "sb_leds", "object": "neopixel sb_leds"})

    def test_power_device(self):
        self.assertEqual(server.find_light(["extruder"], ["printer", "Enclosure Light"]),
                         {"kind": "power", "name": "Enclosure Light", "object": None})

    def test_no_light(self):
        self.assertIsNone(server.find_light(["output_pin fan0", "extruder"], ["printer"]))

    def test_is_it_on(self):
        pin = {"kind": "pin", "name": "LED", "object": "output_pin LED"}
        self.assertTrue(server.light_on({"output_pin LED": {"value": 1.0}}, pin))
        self.assertFalse(server.light_on({"output_pin LED": {"value": 0.0}}, pin))
        self.assertIsNone(server.light_on({}, pin))
        strip = {"kind": "led", "name": "sb", "object": "neopixel sb"}
        self.assertTrue(server.light_on({"neopixel sb": {"color_data": [[0, 0, 0, 0], [0.5, 0, 0, 0]]}}, strip))
        self.assertFalse(server.light_on({"neopixel sb": {"color_data": [[0, 0, 0, 0]]}}, strip))
        power = {"kind": "power", "name": "Light", "object": None}
        self.assertTrue(server.light_on({}, power, "on"))
        self.assertIsNone(server.light_on({}, power, "error"))

    def test_commands(self):
        pin = {"kind": "pin", "name": "LED", "object": "output_pin LED", "scale": 1.0}
        self.assertEqual(server.light_command(pin, True), "SET_PIN PIN=LED VALUE=1")
        self.assertEqual(server.light_command(pin, False), "SET_PIN PIN=LED VALUE=0")
        self.assertEqual(server.light_command({**pin, "name": "caselight", "scale": 255.0}, True),
                         "SET_PIN PIN=caselight VALUE=255")
        strip = {"kind": "led", "name": "sb", "object": "neopixel sb"}
        self.assertEqual(server.light_command(strip, False), "SET_LED LED=sb RED=0 GREEN=0 BLUE=0 WHITE=0")


class ControlRequestTest(unittest.TestCase):
    HEATERS = {"nozzle": {"name": "extruder", "max": 390.0}, "bed": {"name": "heater_bed", "max": 135.0},
               "chamber": {"name": "chamber_heater", "max": 80.0}}

    AXES = {"x": {"min": -13.0, "max": 302.0}, "y": {"min": -6.5, "max": 332.0}, "z": {"min": 0.0, "max": 303.0}}

    def request(self, action, state, homed="xyz", **body):
        return server.control_request(action, body, state, self.HEATERS, homed, self.AXES)

    def script(self, path):
        return server.urllib.parse.unquote(path.split("script=", 1)[1])

    def test_pause_resume_cancel_need_a_print(self):
        self.assertEqual(self.request("pause", "printing"), ("/printer/print/pause", None))
        self.assertEqual(self.request("resume", "paused"), ("/printer/print/resume", None))
        self.assertEqual(self.request("cancel", "paused"), ("/printer/print/cancel", None))
        for action, state in [("pause", "paused"), ("pause", "standby"), ("resume", "printing"), ("cancel", "complete")]:
            path, problem = self.request(action, state)
            self.assertIsNone(path, (action, state))
            self.assertTrue(problem)

    def test_home_and_motors_off_only_without_a_print(self):
        self.assertEqual(self.script(self.request("home", "standby")[0]), "G28")
        self.assertEqual(self.script(self.request("motors_off", "complete")[0]), "M84")
        for state in ("printing", "paused"):
            self.assertIsNone(self.request("home", state)[0])
            self.assertIsNone(self.request("motors_off", state)[0])

    def test_cooldown_any_time(self):
        self.assertEqual(self.script(self.request("cooldown", "printing")[0]), "TURN_OFF_HEATERS")

    def test_heater_targets(self):
        path, _ = self.request("set_temp", "complete", heater="nozzle", target=220)
        self.assertEqual(self.script(path), "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=220")
        path, _ = self.request("set_temp", "standby", heater="chamber", target=0)
        self.assertEqual(self.script(path), "SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET=0")

    def test_heater_targets_within_limits(self):
        for body in [{"heater": "bed", "target": 136}, {"heater": "nozzle", "target": -5},
                     {"heater": "nozzle", "target": "220"}, {"heater": "nozzle", "target": True},
                     {"heater": "fan", "target": 50}, {"heater": "nozzle"}]:
            path, problem = self.request("set_temp", "standby", **body)
            self.assertIsNone(path, body)
            self.assertTrue(problem)

    def test_moving_an_axis(self):
        path, _ = self.request("jog", "standby", axis="z", distance=-0.1)
        self.assertEqual(self.script(path), "G91\nG1 Z-0.1 F600\nG90")
        path, _ = self.request("jog", "complete", axis="x", distance=10)
        self.assertEqual(self.script(path), "G91\nG1 X10 F6000\nG90")

    def test_moving_needs_homing_and_no_print(self):
        self.assertIsNone(self.request("jog", "standby", homed="xy", axis="z", distance=1)[0])
        self.assertIsNone(self.request("jog", "standby", homed="", axis="x", distance=1)[0])
        for state in ("printing", "paused"):
            self.assertIsNone(self.request("jog", state, axis="x", distance=1)[0])

    def test_moving_only_the_offered_steps(self):
        for body in [{"axis": "x", "distance": 5}, {"axis": "x", "distance": 100}, {"axis": "e", "distance": 1},
                     {"axis": "x", "distance": "1"}, {"axis": "x", "distance": True}, {"axis": "x"}]:
            path, problem = self.request("jog", "standby", **body)
            self.assertIsNone(path, body)
            self.assertTrue(problem)

    def test_moving_to_a_typed_position(self):
        path, _ = self.request("move_to", "standby", axis="y", position=150.25)
        self.assertEqual(self.script(path), "G90\nG1 Y150.25 F6000")
        path, _ = self.request("move_to", "standby", axis="x", position=-13)
        self.assertEqual(self.script(path), "G90\nG1 X-13 F6000")

    def test_typed_positions_stay_within_travel(self):
        for body in [{"axis": "z", "position": -0.5}, {"axis": "x", "position": 302.5}, {"axis": "y", "position": "10"},
                     {"axis": "x", "position": True}, {"axis": "e", "position": 1}, {"axis": "x"}]:
            path, problem = self.request("move_to", "standby", **body)
            self.assertIsNone(path, body)
            self.assertTrue(problem)
        self.assertIsNone(self.request("move_to", "printing", axis="x", position=10)[0])
        self.assertIsNone(self.request("move_to", "standby", homed="xy", axis="z", position=10)[0])

    def test_temperatures_and_moving_only_when_idle(self):
        for action, body in [("set_temp", {"heater": "nozzle", "target": 200}), ("jog", {"axis": "x", "distance": 1}),
                             ("move_to", {"axis": "x", "position": 10}), ("home", {}), ("motors_off", {})]:
            for state in ("standby", "complete", "cancelled"):
                self.assertIsNotNone(self.request(action, state, **body)[0], (action, state))
            for state in ("printing", "paused", "busy", "error", None):
                path, problem = self.request(action, state, **body)
                self.assertIsNone(path, (action, state))
                self.assertIn("idle", problem)

    def test_pause_cancel_and_cooldown_unaffected(self):
        self.assertIsNotNone(self.request("cooldown", "busy")[0])
        self.assertIsNotNone(self.request("cancel", "printing")[0])

    def test_unknown_action(self):
        self.assertIsNone(self.request("eject", "standby")[0])


class PrinterStateTest(unittest.TestCase):
    def setUp(self):
        server._last_busy.clear()

    def status(self, state, idle):
        return {"print_stats": {"state": state}, "idle_timeout": {"state": idle}}

    def test_busy_and_its_hold(self):
        self.assertEqual(server.printer_state("p", self.status("standby", "Ready"), NOW), "standby")
        self.assertEqual(server.printer_state("p", self.status("standby", "Printing"), NOW), "busy")
        # A pause between steps, within the hold: still busy.
        self.assertEqual(server.printer_state("p", self.status("standby", "Ready"), NOW + 30), "busy")
        self.assertEqual(server.printer_state("p", self.status("standby", "Ready"), NOW + 61), "standby")

    def test_a_print_is_never_busy(self):
        self.assertEqual(server.printer_state("p", self.status("printing", "Printing"), NOW), "printing")
        self.assertEqual(server.printer_state("p", self.status("complete", "Ready"), NOW + 5), "complete")


class LayerProgressTest(unittest.TestCase):
    class Index:
        file_size = 1000

        def __init__(self, progress, elapsed, total=None, before=0):
            self.progress, self.elapsed, self.total, self.before = progress, elapsed, total, before

        def start_code_time(self):
            return self.before

        def layer_progress(self, position):
            return self.progress

        def time_at(self, position):
            return self.total if position == self.file_size else self.elapsed

    def test_the_slicers_time_left_in_the_layer(self):
        self.assertEqual(server.layer_progress(self.Index((0.25, 60), 100), 0, 300), {"done": 0.25, "left": 60})

    def test_scaled_by_the_pace_so_far(self):
        # 15 minutes in by the slicer's estimate, 18 by the clock: 20% slower.
        self.assertEqual(server.layer_progress(self.Index((0.5, 60), 900), 0, 1080)["left"], 72)
        # Never more than twice, or less than half, the slicer's time.
        self.assertEqual(server.layer_progress(self.Index((0.5, 60), 900), 0, 9000)["left"], 120)

    def test_without_the_slicers_times(self):
        self.assertEqual(server.layer_progress(self.Index((0.5, None), None), 0, 300), {"done": 0.5, "left": None})
        self.assertIsNone(server.layer_progress(self.Index(None, None), 0, 300))


class PrintProgressTest(unittest.TestCase):
    Index = LayerProgressTest.Index

    def test_by_the_slicers_times(self):
        # A quarter of the slicer's time in, at its own pace.
        self.assertEqual(server.print_progress(self.Index(None, 200, 800), 0, 200), (0.25, 600))

    def test_scaled_by_the_pace_so_far(self):
        self.assertEqual(server.print_progress(self.Index(None, 1000, 4000), 0, 1100), (0.25, 3300))

    def test_from_the_first_layer(self):
        # Still in the start code (75 s of the slicer's 1100): nothing done yet.
        self.assertEqual(server.print_progress(self.Index(None, 73, 1100, 75), 0, 0), (0.0, 1025))
        # A quarter through the rest; 400 s of the slicer's time took 480.
        self.assertEqual(server.print_progress(self.Index(None, 475, 1675, 75), 0, 400), (0.25, 1200))
        self.assertEqual(server.print_progress(self.Index(None, 475, 1675, 75), 0, 480), (0.25, 1440))

    def test_without_the_slicers_times(self):
        self.assertIsNone(server.print_progress(self.Index(None, None), 0, 100))


class DoneWithTest(unittest.TestCase):
    JOB = {"endTime": NOW}

    def setUp(self):
        server._light_was_on.clear()
        server._light_went_off.clear()

    def test_when_the_light_goes_off_after_the_end(self):
        self.assertFalse(server.done_with("p", self.JOB, True, NOW + 60))
        self.assertTrue(server.done_with("p", self.JOB, False, NOW + 120))
        # And it stays done with if the light comes back on.
        self.assertTrue(server.done_with("p", self.JOB, True, NOW + 180))

    def test_a_light_switched_off_during_the_print_doesnt_count(self):
        server.done_with("p", None, True, NOW - 600)
        server.done_with("p", None, False, NOW - 300)
        self.assertFalse(server.done_with("p", self.JOB, False, NOW + 60))

    def test_otherwise_after_the_hold(self):
        for light in (False, None):
            self.assertFalse(server.done_with("p", self.JOB, light, NOW + server.DONE_HOLD_SECS - 1))
            self.assertTrue(server.done_with("p", self.JOB, light, NOW + server.DONE_HOLD_SECS))

    def test_no_finished_job(self):
        self.assertFalse(server.done_with("p", None, False, NOW + server.DONE_HOLD_SECS))


class ThumbnailTest(unittest.TestCase):
    @staticmethod
    def block(kind, size, data):
        encoded = server.base64.b64encode(data).decode()
        lines = [encoded[i:i + 20] for i in range(0, len(encoded), 20)]
        tag = "thumbnail" if kind == "PNG" else f"thumbnail_{kind}"
        return (f"; {tag} begin {size}x{size} {len(encoded)}\n" + "".join(f"; {line}\n" for line in lines)
                + f"; {tag} end\n;\n").encode()

    def test_the_largest_picture(self):
        head = (b"; generated by Creality_Print V7.3.0\n" + self.block("PNG", 96, b"small png")
                + self.block("PNG", 300, b"large png") + b"G28\n")
        self.assertEqual(server.embedded_thumbnail(head), ("image/png", b"large png"))

    def test_jpeg_pictures(self):
        head = self.block("JPG", 200, b"jpeg data") + self.block("PNG", 32, b"tiny")
        self.assertEqual(server.embedded_thumbnail(head), ("image/jpeg", b"jpeg data"))

    def test_none(self):
        self.assertIsNone(server.embedded_thumbnail(b"; no pictures here\nG28\n"))

    def test_shown_once_a_job_has_started(self):
        for state, shown in [("printing", True), ("paused", True), ("complete", True), ("standby", False)]:
            status = printing_status(print_stats={"state": state, "filename": "my part.gcode", "print_duration": 10})
            thumbnail = server.summarize(status, [], None, NOW)["thumbnail"]
            self.assertEqual(thumbnail, "/api/thumbnail?file=my%20part.gcode" if shown else None, state)


class TemperatureHistoryTest(unittest.TestCase):
    def test_every_fifth_reading_newest_last(self):
        store = {
            "extruder": {"temperatures": [float(i) for i in range(12)], "targets": [220.0] * 12},
            "heater_bed": {"temperatures": [60.0] * 12},
            "temperature_sensor chamber_temp": {"temperatures": [35.04] * 12},
            "heater_generic chamber_heater": {"temperatures": [35.0] * 12, "targets": [0.0] * 12},
        }
        saved = server.moonraker_get, server.printer_profile
        server.moonraker_get = lambda path, host: store
        server.printer_profile = lambda host: {"chamberSensor": "temperature_sensor chamber_temp",
                                              "chamberHeater": "heater_generic chamber_heater"}
        try:
            history = server.temperature_history("p")
        finally:
            server.moonraker_get, server.printer_profile = saved
        self.assertEqual(history["step"], 5)
        self.assertEqual(history["nozzle"], {"temps": [1.0, 6.0, 11.0], "targets": [220.0, 220.0, 220.0]})
        self.assertEqual(history["bed"], {"temps": [60.0, 60.0, 60.0], "targets": None})
        self.assertEqual(history["chamber"], {"temps": [35.0, 35.0, 35.0], "targets": [0.0, 0.0, 0.0]})


class ToolheadTest(unittest.TestCase):
    def test_position_and_homing(self):
        self.assertEqual(server.toolhead({"homed_axes": "xyz", "position": [5.0, 41.2499, 1.0, 1234.5]}),
                         {"homed": "xyz", "position": [5.0, 41.25, 1.0]})
        self.assertEqual(server.toolhead({"homed_axes": "", "position": [0, 0, 0, 0]})["homed"], "")
        self.assertIsNone(server.toolhead(None))


class AxisLimitsTest(unittest.TestCase):
    def test_from_the_config_with_z_never_below_zero(self):
        settings = {"stepper_x": {"position_min": -13.0, "position_max": 302.0},
                    "stepper_y": {"position_min": -6.5, "position_max": 332.0},
                    "stepper_z": {"position_min": -10.0, "position_max": 303.0}}
        self.assertEqual(server.axis_limits(settings), {
            "x": {"min": -13.0, "max": 302.0}, "y": {"min": -6.5, "max": 332.0}, "z": {"min": 0.0, "max": 303.0}})

    def test_unknown_travel_left_out(self):
        self.assertEqual(server.axis_limits({"stepper_x": {"position_max": 300}}), {})


class ControllableHeatersTest(unittest.TestCase):
    def test_limits_from_the_config(self):
        names = ["extruder", "heater_bed", "heater_generic chamber_heater"]
        settings = {"extruder": {"max_temp": 390.0}, "heater_bed": {"max_temp": 135.0},
                    "heater_generic chamber_heater": {"max_temp": 80.0}}
        self.assertEqual(server.controllable_heaters(names, "heater_generic chamber_heater", settings), {
            "nozzle": {"name": "extruder", "max": 390.0}, "bed": {"name": "heater_bed", "max": 135.0},
            "chamber": {"name": "chamber_heater", "max": 80.0}})

    def test_no_chamber_heater_and_no_config(self):
        self.assertEqual(server.controllable_heaters(["extruder", "heater_bed"], None, {}), {
            "nozzle": {"name": "extruder", "max": 300}, "bed": {"name": "heater_bed", "max": 120}})


class FindCameraTest(unittest.TestCase):
    def find(self, webcams, ui_url="http://192.168.1.50", creality_pages=()):
        """creality_pages: camera page URLs that are Creality's WebRTC page."""
        saved = server.moonraker_get, server.creality_webrtc
        server.moonraker_get = lambda path, host: {"webcams": webcams}
        server.creality_webrtc = lambda url, host: (
            f"http://{host}:8000/call/webrtc_local" if url in creality_pages else None)
        try:
            return server.find_camera("192.168.1.50", ui_url)
        finally:
            server.moonraker_get, server.creality_webrtc = saved

    def test_k2_webrtc_camera_next_to_its_fluidd(self):
        page = "http://192.168.1.50:4408/camera.html"
        cam = self.find([{"name": "Default", "enabled": True, "service": "iframe", "stream_url": "/camera.html"}],
                        "http://192.168.1.50:4408", creality_pages=[page])
        self.assertEqual((cam["type"], cam["url"], cam["signalUrl"]),
                         ("webrtc", page, "http://192.168.1.50:8000/call/webrtc_local"))

    def test_another_page_goes_in_a_frame(self):
        cam = self.find([{"enabled": True, "service": "iframe", "stream_url": "/cam/view.html"}])
        self.assertEqual(cam["type"], "frame")
        self.assertNotIn("signalUrl", cam)

    def test_mjpeg_stream_with_flips(self):
        cam = self.find([{"enabled": True, "service": "mjpegstreamer-adaptive", "stream_url": "/webcam/?action=stream",
                          "flip_horizontal": True, "flip_vertical": False, "rotation": 90}])
        self.assertEqual(cam, {"type": "mjpeg", "url": "http://192.168.1.50/webcam/?action=stream",
                               "flipH": True, "flipV": False, "rotation": 90})

    def test_skips_disabled_cameras_and_keeps_full_urls(self):
        cam = self.find([{"enabled": False, "service": "mjpegstreamer", "stream_url": "/webcam/?action=stream"},
                         {"enabled": True, "service": "webrtc-camerastreamer", "stream_url": "http://10.0.0.9/webrtc"}])
        self.assertEqual((cam["type"], cam["url"]), ("frame", "http://10.0.0.9/webrtc"))

    def test_no_web_interface_found(self):
        cam = self.find([{"service": "mjpegstreamer", "stream_url": "/webcam/?action=stream"}], None)
        self.assertEqual(cam["url"], "http://192.168.1.50/webcam/?action=stream")

    def test_no_cameras(self):
        self.assertIsNone(self.find([]))


class SuggestedNameTest(unittest.TestCase):
    """The printer's name comes from Fluidd or Mainsail if one was set there,
    else its host name."""

    def suggest(self, database, hostname="K2Pro"):
        def fake_get(path, host):
            for (namespace, key), value in database.items():
                if f"namespace={namespace}&key={key}" in path:
                    return {"value": value}
            raise OSError("404: not found")  # what urlopen raises (HTTPError) is an OSError

        saved = server.moonraker_get
        server.moonraker_get = fake_get
        try:
            return server.suggested_name("192.168.1.34", hostname)
        finally:
            server.moonraker_get = saved

    def test_host_name_when_no_interface_has_one(self):
        self.assertEqual(self.suggest({}), "K2Pro")

    def test_fluidd_name_first(self):
        self.assertEqual(self.suggest({("fluidd", "uiSettings.general.instanceName"): "Workshop K2",
                                       ("mainsail", "general.printername"): "Other"}), "Workshop K2")

    def test_mainsail_name(self):
        self.assertEqual(self.suggest({("mainsail", "general.printername"): " Voron 2.4 "}), "Voron 2.4")

    def test_skips_empty_or_odd_names(self):
        self.assertEqual(self.suggest({("fluidd", "uiSettings.general.instanceName"): ""}), "K2Pro")
        self.assertEqual(self.suggest({("fluidd", "uiSettings.general.instanceName"): 7}), "K2Pro")
        self.assertEqual(self.suggest({}, hostname=None), "Printer")


class ErrorsTest(unittest.TestCase):
    def test_picks_error_lines_since_the_job_started(self):
        lines = [
            {"time": NOW - 5000, "message": "!! old error"},
            {"time": NOW - 100, "message": "B:60.0 /60.0 T0:220.0 /220.0"},
            {"time": NOW - 90, "message": "// [box] cut sensor detected"},
            {"time": NOW - 80, "message": '{"code":"key863","msg":"TR2863 retract issue"}'},
            {"time": NOW - 70, "message": "!! Move out of range"},
        ]
        errors = server.recent_errors(lines, NOW - 4000)
        self.assertEqual([e["message"] for e in errors],
                         ['{"code":"key863","msg":"TR2863 retract issue"}', "Move out of range"])


    def test_board_names_mid_line_are_not_errors(self):
        lines = [
            {"time": NOW - 30, "message": "mcu 'EBBCan': Starting on RP2040 (SB2209 toolhead)"},
            {"time": NOW - 20, "message": "TR2863 retract issue, extruder filament sensor anomaly"},
        ]
        self.assertEqual([e["message"] for e in server.recent_errors(lines, NOW - 100)],
                         ["TR2863 retract issue, extruder filament sensor anomaly"])


class NoPrinterYetTest(unittest.TestCase):
    def test_no_address_means_no_requests(self):
        with self.assertRaises(OSError):
            server.moonraker_get("/printer/info", None)
        with self.assertRaises(OSError):
            server.moonraker_post("/printer/print/pause", None)
        self.assertEqual(server.printer_extras(None), {"uiUrl": None, "camera": None, "castStream": None})
        self.assertIsNone(server.file_thumbnail(None, "Cube.gcode"))


class SafetyTest(unittest.TestCase):
    def test_thumbnails_stay_in_the_gcode_folder(self):
        for name in ["../config/printer.cfg", "a/../../x.gcode", "/etc/passwd"]:
            self.assertIsNone(server.file_thumbnail("printer.local", name), name)

    def test_only_web_camera_addresses(self):
        webcams = [{"enabled": True, "service": "iframe", "stream_url": "javascript:alert(1)"},
                   {"enabled": True, "service": "mjpegstreamer", "stream_url": "/webcam/?action=stream"}]
        with mock.patch.object(server, "moonraker_get", return_value={"webcams": webcams}):
            cam = server.find_camera("printer.local", "http://printer.local")
        self.assertEqual(cam["url"], "http://printer.local/webcam/?action=stream")
        self.assertFalse(server.web_url("javascript:alert(1)"))
        self.assertFalse(server.web_url("data:text/html,hi"))
        self.assertTrue(server.web_url("https://cam.local/stream"))


class SummarizeTest(unittest.TestCase):
    def test_ignores_the_job_before_left_in_cur_print_data(self):
        status = printing_status()
        status["virtual_sdcard"]["layer_count"] = 0
        status["virtual_sdcard"]["cur_print_data"].update(
            filename="Cube.gcode", metadata={"layer_count": 4, "estimated_time": 450, "filament_used_g": ["1.91"]})
        moonraker = {"layer_count": 75, "estimated_time": 8000, "filament_used_g": ["15"]}
        s = server.summarize(status, [], None, NOW, K2_PROFILE, moonraker)
        self.assertEqual((s["layer"], s["layerCount"], s["filamentGrams"]), (100, 75, 15.0))
        self.assertAlmostEqual(s["timeLeft"], 4000)

    def errors_when(self, state):
        status = printing_status()
        status["print_stats"]["state"] = state
        lines = [{"time": NOW - 900, "message": "!! old"}, {"time": NOW - 60, "message": "!! new"}]
        return [e["message"] for e in server.summarize(status, lines, None, NOW, K2_PROFILE)["errors"]]

    def test_an_error_stays_while_the_printer_is_stopped_on_it(self):
        self.assertEqual(self.errors_when("paused"), ["old", "new"])
        self.assertEqual(self.errors_when("error"), ["old", "new"])

    def test_an_error_goes_once_the_printer_has_moved_on(self):
        self.assertEqual(self.errors_when("printing"), ["new"])
        self.assertEqual(self.errors_when("standby"), ["new"])

    def test_printing(self):
        s = server.summarize(printing_status(), [], None, NOW, K2_PROFILE)
        self.assertEqual(s["state"], "printing")
        self.assertEqual((s["layer"], s["layerCount"]), (100, 200))
        self.assertAlmostEqual(s["timeLeft"], 4000)
        self.assertAlmostEqual(s["finishAt"], NOW + 4000)
        self.assertEqual(s["filamentGrams"], 15.0)
        self.assertEqual(s["temps"]["nozzle"], {"temp": 219.6, "target": 220.0})
        self.assertEqual(s["temps"]["chamber"], {"temp": 35.4, "target": 0.0})
        self.assertTrue(s["cfs"][0]["slots"][1]["loaded"])
        self.assertIsNone(s["lastJob"])

    def test_idle_shows_last_job_and_no_time_left(self):
        status = printing_status(print_stats={"state": "standby", "filename": "", "print_duration": 0})
        last = {"filename": "a.gcode", "status": "completed", "endTime": NOW - 60, "duration": 100}
        s = server.summarize(status, [], last, NOW)
        self.assertEqual(s["state"], "standby")
        self.assertIsNone(s["timeLeft"])
        self.assertEqual(s["lastJob"], last)

    def test_print_error_message_becomes_an_error(self):
        status = printing_status(print_stats={"state": "error", "filename": "p.gcode", "message": "Heater extruder not heating at expected rate"})
        s = server.summarize(status, [], None, NOW)
        self.assertEqual(s["errors"][-1]["message"], "Heater extruder not heating at expected rate")

    def test_busy_outside_a_print(self):
        # Calibration or a macro: no print job, but Klipper is running commands.
        status = printing_status(print_stats={"state": "standby", "filename": ""},
                                 idle_timeout={"state": "Printing"})
        self.assertEqual(server.summarize(status, [], None, NOW)["state"], "busy")
        status["idle_timeout"] = {"state": "Ready"}
        self.assertEqual(server.summarize(status, [], None, NOW)["state"], "standby")
        # A pause between pre-print steps: Klipper's idle for a moment, but it
        # was busy within the last minute, so it still counts as busy.
        self.assertEqual(server.summarize(status, [], None, NOW, recently_busy=True)["state"], "busy")
        # After a print, a recent busy spell doesn't override "complete".
        done = printing_status(print_stats={"state": "complete", "filename": "p.gcode"}, idle_timeout={"state": "Ready"})
        self.assertEqual(server.summarize(done, [], None, NOW, recently_busy=True)["state"], "complete")
        # During a print, idle_timeout says "Printing" too; it's still printing.
        self.assertEqual(server.summarize(printing_status(idle_timeout={"state": "Printing"}), [], None, NOW)["state"],
                         "printing")

    def test_klipper_down_is_offline(self):
        status = printing_status(webhooks={"state": "shutdown", "state_message": "MCU 'mcu' shutdown"})
        s = server.summarize(status, [], None, NOW)
        self.assertEqual(s["state"], "offline")
        self.assertEqual(s["errors"][-1]["message"], "MCU 'mcu' shutdown")


if __name__ == "__main__":
    unittest.main()
