"""Reads a G-code file into layers of line segments, for the dashboard's
layer preview: a top-down drawing of the current layer, with the part
already printed shown bright.

Only printing moves are kept (moves that extrude), as (x1, y1, x2, y2), each
with the byte offset in the file where it ends, so the dashboard can tell
how much of a layer is done from Klipper's file position. Arcs (G2/G3, which
Creality Print and Orca use for curves) are split into short straight
segments.

Layers start at the slicer's layer markers (";LAYER_CHANGE", or Cura's
";LAYER:"); a file without them is one layer per new Z height.

The slicer's time marks (";TIME_ELAPSED:296.8", from Creality Print and Orca,
at the end of each layer) are kept too, for how far through the print and
the current layer it is. Between two marks, which can be hours apart, the
time is shared out by the moves' own time (distance / feed rate), sampled
every few KB: bytes alone go by quickly through detail and slowly through
ironing's long, slow lines.

Standard library only; one pass over the file.
"""

import bisect
import math
import re
from array import array

# How finely arcs are split: no segment turns more than this.
ARC_STEP = math.radians(10)
# How often (bytes) the moves' running time is sampled.
CLOCK_STEP = 1024
DEFAULT_FEED = 3000  # mm/min, until the G-code sets one
LAYER_MARKERS = (b";LAYER_CHANGE", b";LAYER:")
TIME_MARKER = b";TIME_ELAPSED:"
COLOUR_SETTING = re.compile(rb"^; *(?:filament_colour|extruder_colour) *= *(.+)$", re.M)


class Layer:
    __slots__ = ("start", "z", "segs", "ends", "tools", "marked")

    def __init__(self, start, z, marked=False):
        self.start = start  # byte offset where the layer begins
        self.z = z
        self.marked = marked  # began at a slicer's layer marker
        self.segs = array("f")  # x1, y1, x2, y2, ...
        self.ends = array("l")  # byte offset each segment ends at
        self.tools = array("b")  # the tool (T0, T1, ...) printing each segment


class GcodeIndex:
    """A G-code file, read into layers. Build with read_gcode()."""

    def __init__(self):
        self.layers = []
        self.bounds = None  # [min x, min y, max x, max y] of the model's layers
        self.colours = []  # per tool, "#RRGGBB", from the slicer's settings
        self.tools = []  # the tools (T0, T1, ...) the whole job prints with
        self.file_size = 0  # bytes
        # The slicer's time marks: the byte offset of each, and its estimate of
        # the seconds into the print there.
        self.time_offsets = array("l")
        self.times = array("f")
        # The moves' running time (seconds), sampled: the byte offset of each.
        self.clock_offsets = array("l")
        self.clock_times = array("d")

    def layer_at(self, position):
        """The index of the layer that a file position (bytes) falls in."""
        starts = [layer.start for layer in self.layers]
        return max(0, bisect.bisect_right(starts, position) - 1)

    def time_at(self, position):
        """The slicer's estimate of the seconds into the print at a file
        position, between its time marks (from 0 at the start of the file);
        None if the file has none."""
        if not self.times:
            return None
        i = bisect.bisect_right(self.time_offsets, position)
        if i == len(self.times):
            return self.times[-1]
        before, at = (self.time_offsets[i - 1], self.times[i - 1]) if i else (0, 0.0)
        after = self.time_offsets[i]
        if after <= before:
            return at
        moves = self.moves_at(after) - self.moves_at(before)
        if moves > 0:
            share = (self.moves_at(position) - self.moves_at(before)) / moves
        else:
            share = (position - before) / (after - before)
        return at + (self.times[i] - at) * share

    def moves_at(self, position):
        """The moves' running time (seconds) at a file position."""
        i = bisect.bisect_right(self.clock_offsets, position)
        if i == len(self.clock_offsets):
            return self.clock_times[-1] if i else 0.0
        before, at = (self.clock_offsets[i - 1], self.clock_times[i - 1]) if i else (0, 0.0)
        after = self.clock_offsets[i]
        if after <= before:
            return at
        return at + (self.clock_times[i] - at) * (position - before) / (after - before)

    def start_code_time(self):
        """The slicer's seconds for the start code, before the first layer
        (it guesses about a minute; heating and calibration take longer)."""
        first = next((layer.start for layer in self.layers if layer.marked), None)
        return (self.time_at(first) or 0) if first is not None else 0

    def layer_progress(self, position):
        """How far through its layer a file position is: (the fraction done,
        the slicer's estimated seconds left in the layer). By time if the file
        has time marks, else by bytes, with no time left (None). None without
        layers."""
        if not self.layers:
            return None
        i = self.layer_at(position)
        start = self.layers[i].start
        end = self.layers[i + 1].start if i + 1 < len(self.layers) else self.layers[i].ends[-1]
        position = min(max(position, start), end)
        t0, now, t1 = self.time_at(start), self.time_at(position), self.time_at(end)
        if t0 is not None and t1 > t0:
            return (now - t0) / (t1 - t0), t1 - now
        return ((position - start) / (end - start) if end > start else 0.0), None

    def layer_json(self, index):
        layer = self.layers[index]
        return {
            "layer": index,
            "layers": len(self.layers),
            "z": layer.z,
            "segs": [round(v, 2) for v in layer.segs],
            "ends": list(layer.ends),
            "tools": list(layer.tools) if any(layer.tools) else None,
            "colours": self.colours,
            "jobTools": self.tools,
            "bounds": self.bounds,
        }


def read_gcode(lines):
    """Reads G-code (an iterable of byte lines, as from a file opened "rb")
    into a GcodeIndex."""
    index = GcodeIndex()
    x = y = z = 0.0
    e = 0.0
    absolute = True  # G90/G91: positions
    absolute_e = True  # M82/M83: extrusion
    tool = 0
    offset = 0
    has_markers = False
    layer = None
    feed = DEFAULT_FEED
    clock = 0.0  # the moves' running time, seconds
    sampled = 0  # where it was last sampled

    def new_layer(start, marked=False):
        nonlocal layer
        layer = Layer(start, z, marked)
        index.layers.append(layer)

    def add(x1, y1, x2, y2, start, end):
        # Without the slicer's markers, a layer is printing at a new height.
        if layer is None or (not has_markers and len(layer.segs) and abs(layer.z - z) > 1e-6):
            new_layer(start)
        # A layer's height is where it first prints: a marker comes just
        # before the move up to it.
        if not len(layer.segs):
            layer.z = z
        layer.segs.extend((x1, y1, x2, y2))
        layer.ends.append(end)
        layer.tools.append(tool)

    for raw in lines:
        start, offset = offset, offset + len(raw)
        line = raw.strip()
        if not line:
            continue
        if line[0] == 59:  # ";": a comment
            if line.startswith(LAYER_MARKERS):
                if not has_markers:
                    has_markers = True
                    # Anything before the first marker (start code, purge
                    # lines) stays in a layer of its own only if it printed.
                    if layer is not None and not len(layer.segs):
                        index.layers.pop()
                new_layer(start, marked=True)
            elif line.startswith(TIME_MARKER):
                try:
                    seconds = float(line[len(TIME_MARKER):])
                except ValueError:
                    continue
                index.time_offsets.append(start)
                index.times.append(seconds)
                index.clock_offsets.append(start)
                index.clock_times.append(clock)
                sampled = start
            elif b"_colour" in line:
                # The slicer's settings, usually at the end of the file. (An
                # empty extruder_colour can come after filament_colour.)
                match = COLOUR_SETTING.match(line)
                colours = [c.decode().strip() for c in match[1].split(b";") if c.strip().startswith(b"#")] if match else []
                if colours and (not index.colours or b"filament_colour" in line):
                    index.colours = colours
            continue
        code, _, rest = line.partition(b";")[0].partition(b" ")
        if code in (b"G1", b"G0", b"G2", b"G3"):
            words = {}
            for word in rest.split():
                try:
                    words[word[:1].upper()] = float(word[1:])
                except ValueError:
                    pass
            nx = (words[b"X"] if absolute else x + words[b"X"]) if b"X" in words else x
            ny = (words[b"Y"] if absolute else y + words[b"Y"]) if b"Y" in words else y
            nz = (words[b"Z"] if absolute else z + words[b"Z"]) if b"Z" in words else z
            extruding = False
            extruded = 0.0
            if b"E" in words:
                if absolute_e:
                    extruded = words[b"E"] - e
                    e = words[b"E"]
                else:
                    extruded = words[b"E"]
                extruding = extruded > 0
            if words.get(b"F", 0) > 0:
                feed = words[b"F"]
            arcing = code in (b"G2", b"G3") and (b"I" in words or b"J" in words)
            if arcing:
                *_, radius, _, sweep = arc_geometry(x, y, nx, ny, words.get(b"I", 0.0), words.get(b"J", 0.0),
                                                    clockwise=code == b"G2")
                distance = math.hypot(radius * sweep, nz - z)
            else:
                distance = math.dist((x, y, z), (nx, ny, nz))
            clock += (distance or abs(extruded)) / feed * 60
            z = nz
            if extruding and (nx != x or ny != y or code in (b"G2", b"G3")):
                if arcing:
                    for x1, y1, x2, y2 in arc(x, y, nx, ny, words.get(b"I", 0.0), words.get(b"J", 0.0),
                                              clockwise=code == b"G2"):
                        add(x1, y1, x2, y2, start, offset)
                else:
                    add(x, y, nx, ny, start, offset)
            x, y = nx, ny
        elif code == b"G4":  # a pause: P milliseconds or S seconds
            for word in rest.split():
                try:
                    if word[:1] in (b"P", b"p"):
                        clock += float(word[1:]) / 1000
                    elif word[:1] in (b"S", b"s"):
                        clock += float(word[1:])
                except ValueError:
                    pass
        elif code == b"G90":
            absolute = True
        elif code == b"G91":
            absolute = False
        elif code == b"M82":
            absolute_e = True
        elif code == b"M83":
            absolute_e = False
        elif code == b"G92":
            for word in rest.split():
                if word[:1].upper() == b"E":
                    try:
                        e = float(word[1:])
                    except ValueError:
                        pass
        elif len(code) >= 2 and code[0] in (84, 116) and code[1:].isdigit():  # "T0", "T1", ...
            tool = int(code[1:])
        if offset - sampled >= CLOCK_STEP:
            index.clock_offsets.append(offset)
            index.clock_times.append(clock)
            sampled = offset

    index.file_size = offset
    index.clock_offsets.append(offset)
    index.clock_times.append(clock)
    # Layers that printed nothing (e.g. a final one with only end code) aren't
    # worth showing.
    index.layers = [layer for layer in index.layers if len(layer.segs)]
    index.tools = sorted({tool for layer in index.layers for tool in layer.tools})
    # The view's extent: the model's own layers, not purge lines printed before
    # the slicer's first layer marker.
    model = [layer for layer in index.layers if layer.marked] or index.layers
    if model:
        xs = [v for layer in model for v in layer.segs[0::2]]
        ys = [v for layer in model for v in layer.segs[1::2]]
        index.bounds = [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]
    return index


def arc(x1, y1, x2, y2, i, j, clockwise):
    """An arc from (x1, y1) to (x2, y2) around (x1 + i, y1 + j), as short
    straight segments."""
    cx, cy, radius, a1, sweep = arc_geometry(x1, y1, x2, y2, i, j, clockwise)
    steps = max(1, int(abs(sweep) / ARC_STEP))
    px, py = x1, y1
    for k in range(1, steps + 1):
        if k == steps:
            nx, ny = x2, y2
        else:
            angle = a1 + sweep * k / steps
            nx, ny = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        yield px, py, nx, ny
        px, py = nx, ny


def arc_geometry(x1, y1, x2, y2, i, j, clockwise):
    """An arc's centre, radius, start angle and sweep (radians, negative
    clockwise)."""
    cx, cy = x1 + i, y1 + j
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = math.atan2(y2 - cy, x2 - cx) - a1
    if clockwise and sweep >= 0:
        sweep -= 2 * math.pi
    elif not clockwise and sweep <= 0:
        sweep += 2 * math.pi
    return cx, cy, math.hypot(i, j), a1, sweep
