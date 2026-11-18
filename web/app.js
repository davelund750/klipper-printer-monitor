// Polls the local server's /api/status (which relays the printer) and fills
// in the dashboard. The camera is the printer's own webcam: an MJPEG stream
// shown as an image, Creality's WebRTC stream connected to directly, or any
// other kind's own page in a frame.
"use strict";

const POLL_MS = 2000;
// Camera streams can quietly stall over days of running, so start the camera
// afresh now and then, and whenever the printer comes back after a dropout.
// (A WebRTC camera is watched more closely instead; see checkCamera.)
const CAMERA_REFRESH_MS = 60 * 60 * 1000;
// CFS humidity at or above this shows in amber: time to dry the filament or
// swap the desiccant.
const HUMID_PCT = 40;
// A spool with this much or less left shows its percentage in red.
const LOW_PCT = 15;
// How long the settings button has to be held.
const SETTINGS_HOLD_MS = 1500;
// Missed status checks in a row before showing "Can't reach the printer".
const MISSES_BEFORE_UNREACHABLE = 3;
// Failed status checks in a row before checking whether the kiosk is online.
const FAILURES_BEFORE_WIFI_CHECK = 5;

const $ = (id) => document.getElementById(id);

const STATE_LABELS = {
  printing: "Printing",
  paused: "Paused",
  complete: "Complete",
  cancelled: "Cancelled",
  error: "Error",
  standby: "Idle",
  busy: "Busy", // no print job, but running commands (calibration, a bed mesh, ...)
  offline: "Klipper offline",
  unreachable: "Can't reach printer",
};

let config = null;
let wasConnected = null;
let failures = 0;
let hour12 = true; // the clock setting, "12" or "24"

// kiosk.sh opens the dashboard with ?kiosk, which hides the mouse pointer.
const kiosk = new URLSearchParams(location.search).has("kiosk");
if (kiosk) document.body.classList.add("kiosk");

// ---- the Klingon look's words ----
// What the dashboard (and its cast screens) says, in Klingon (tlhIngan Hol, as
// The Klingon Dictionary spells it). The Controls view, its confirmations and
// Settings stay in English: nobody should cancel a print by guessing.
const KLINGON = {
  // The printer's state.
  Printing: "ghItlhtaH", // "is writing"
  Paused: "mevpu'", // "has stopped"
  Complete: "rInpu'", // "has been finished"
  Cancelled: "qIlpu'",
  Error: "Qagh",
  Idle: "loStaH", // "is waiting"
  Busy: "vumtaH", // "is working"
  "Klipper offline": "Klipper Heghpu'", // "has died"
  "Can't reach printer": "ghoSlaHbe'", // "can't approach"
  // Readouts.
  Layer: "patlh", // "level"
  Elapsed: "poH", // "period of time"
  Nozzle: "ghIch", // "nose"
  Bed: "QongDaq",
  Chamber: "pa'", // "room"
  RH: "yIQ", // "wet"
  left: "ratlh", // "remains"
  Done: "rIn",
  Finished: "rInpu'",
  Completed: "rInpu'",
  Today: "DaHjaj",
  Tomorrow: "wa'leS",
  Yesterday: "wa'Hu'",
  "No print": "ghItlh pagh",
  "Last:": "Qav:",
  // Alerts.
  "Print paused": "mevpu' ghItlh",
  "Print complete:": "rInpu' ghItlh:",
  // The camera and layer preview.
  "Camera unavailable": "mIllogh qonwI' Qapbe'", // "the picture recorder isn't working"
  "No camera set up": "mIllogh qonwI' tu'lu'be'",
  "Connecting to the camera…": "mIllogh qonwI' rarlI'…",
  "Restarting the camera…": "mIllogh qonwI' taghqa'lI'…",
  "Preparing preview…": "yIloS…", // "wait!"
  "Loading layer…": "yIloS…",
};

// A word or phrase from the dashboard, in the look's language.
function say(text) {
  return speakKlingon() ? KLINGON[text] ?? text : text;
}

// The Klingon look speaks Klingon unless its words are set to English.
let language = "klingon";

function speakKlingon() {
  return theme === "klingon" && language === "klingon";
}

// ---- looks (Settings → Display) ----
// The standard look, or a stylesheet over it, scoped to a body class
// (lcars.css, klingon.css), with its font. ?theme= overrides the setting, for
// trying one out.
const THEMES = {
  lcars: { css: "lcars.css", font: "Antonio:wght@400;700" },
  klingon: { css: "klingon.css", font: "Chakra+Petch:wght@500;700" },
};
const themeOverride = new URLSearchParams(location.search).get("theme");
let theme = "standard";

function applyTheme(name) {
  if (THEMES[themeOverride]) name = themeOverride;
  if (!THEMES[name]) name = "standard";
  if (name === theme) return;
  document.body.classList.remove(theme);
  theme = name;
  relabel();
  if (!THEMES[name]) return;
  if (!document.getElementById(`theme-${name}`)) {
    const { css, font } = THEMES[name];
    for (const [id, href] of [[`theme-${name}-font`, `https://fonts.googleapis.com/css2?family=${font}&display=swap`],
                              [`theme-${name}`, css]]) {
      const link = document.createElement("link");
      link.id = id;
      link.rel = "stylesheet";
      link.href = href;
      document.head.append(link);
    }
  }
  document.body.classList.add(name);
}

// The fixed labels; the rest is redrawn with the next status.
function relabel() {
  for (const node of document.querySelectorAll("[data-say]")) node.textContent = say(node.dataset.say);
}

// ---- formatting ----

function clockTime(epochSecs) {
  return new Date(epochSecs * 1000).toLocaleTimeString(hour12 ? "en-US" : "en-GB", { hour: "numeric", minute: "2-digit", hour12 });
}

// "Today 3:42 PM", "Tomorrow 1:05 AM", or "Sat 9:10 AM".
function dayAndTime(epochSecs) {
  const when = new Date(epochSecs * 1000);
  const days = Math.round((startOfDay(when) - startOfDay(new Date())) / 86400000);
  const day = days === 0 ? say("Today") : days === 1 ? say("Tomorrow") : days === -1 ? say("Yesterday")
    : when.toLocaleDateString([], { weekday: "short" });
  return `${day} ${clockTime(epochSecs)}`;
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

// 4363 -> "1h 12m"; 300 -> "5m".
function duration(secs) {
  if (secs == null) return "—";
  const mins = Math.round(secs / 60);
  const h = Math.floor(mins / 60);
  return h ? `${h}h ${String(mins % 60).padStart(2, "0")}m` : `${mins}m`;
}

// For short spans, to the second: 42 -> "42s"; 80 -> "1m 20s"; an hour or
// more as duration().
function shortDuration(secs) {
  secs = Math.round(secs);
  if (secs >= 3600) return duration(secs);
  return secs >= 60 ? `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, "0")}s` : `${secs}s`;
}

// Drops the slicer's "_PLA_2h21m9s.gcode" tail: "SPEED_A_smooth_ellipse".
function jobName(filename) {
  if (!filename) return "—";
  return filename.replace(/\.gcode$/i, "").replace(/_[A-Z]+_(\d+h)?(\d+m)?(\d+s)?$/, "").replace(/_/g, " ");
}

// Each temperature's change over the last minute, from the graph's history:
// "+1.4/min" rising, "−0.8/min" falling, nothing when steadier than 0.2°.
// (Per reading, every 2 s, a slowly cooling bed changes by only a few
// hundredths, lost in sensor noise.)
const CHANGE_WINDOW_MS = 60 * 1000;
const CHANGE_MIN = 0.2;
let tempChanges = {};

function noteTempChanges() {
  tempChanges = {};
  for (const [key, data] of Object.entries(graphData)) {
    if (data.length < 2) continue;
    const [now, temp] = data[data.length - 1];
    // The newest reading at least a minute old. (Not findLast: cast screens
    // run browsers too old for it.)
    let before = null;
    for (const point of data) if (point[0] <= now - CHANGE_WINDOW_MS) before = point;
    if (before) tempChanges[key] = temp - before[1];
  }
}

// Shown beside the heater's label, not its temperature, so the temperature
// itself never shifts as the change comes and goes.
function showChange(label, key) {
  let badge = label.querySelector(".temp-change");
  if (!badge) {
    badge = el("span", "temp-change");
    label.append(badge);
  }
  const change = tempChanges[key];
  const shown = change != null && Math.abs(change) >= CHANGE_MIN;
  badge.textContent = shown ? `${change > 0 ? "+" : "−"}${Math.abs(change).toFixed(1)}/min` : "";
  badge.className = `temp-change ${shown ? (change > 0 ? "up" : "down") : ""}`;
}

function temp(t, el) {
  if (!t) {
    el.textContent = "—";
    return;
  }
  // Klipper reports Celsius.
  el.textContent = `${t.temp.toFixed(1)}°C`;
  if (t.target) {
    const target = document.createElement("span");
    target.className = "target";
    target.textContent = `/ ${Math.round(t.target)}°C`;
    el.append(" ", target);
  }
}

// ---- temperature graphs ----
// A faint line behind each temperature row: the last 10 minutes, with the
// target dashed when one is set. Starts from the server's history (Moonraker
// keeps 10 minutes), then adds each reading.

const GRAPH_WINDOW_MS = 10 * 60 * 1000;
const GRAPH_KEYS = ["nozzle", "bed", "chamber"];
const graphData = { nozzle: [], bed: [], chamber: [] }; // [time ms, temp, target]
const SVG = "http://www.w3.org/2000/svg";

async function loadHistory() {
  let history;
  try {
    history = await (await fetch("/api/history", { cache: "no-store" })).json();
  } catch {
    return;
  }
  const now = Date.now();
  for (const key of GRAPH_KEYS) {
    const h = history[key];
    if (!h?.temps) continue;
    const n = h.temps.length;
    graphData[key] = h.temps.map((temp, i) => [now - (n - 1 - i) * history.step * 1000, temp, h.targets?.[i] || 0]);
  }
  drawGraphs();
}

function recordTemps(temps) {
  const now = Date.now();
  for (const key of GRAPH_KEYS) {
    const t = temps?.[key];
    if (!t) continue;
    const data = graphData[key];
    data.push([now, t.temp, t.target || 0]);
    while (data.length && data[0][0] < now - GRAPH_WINDOW_MS) data.shift();
  }
}

function drawGraphs() {
  const now = Date.now();
  for (const key of GRAPH_KEYS) {
    const row = $(`temp-${key}`)?.closest(".row");
    if (!row) continue;
    let svg = row.querySelector("svg.spark");
    if (!svg) {
      svg = document.createElementNS(SVG, "svg");
      svg.setAttribute("class", "spark");
      svg.setAttribute("viewBox", "0 0 100 100");
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("aria-hidden", "true");
      for (const part of ["area", "line", "target"]) {
        const path = document.createElementNS(SVG, "path");
        path.setAttribute("class", part);
        path.setAttribute("vector-effect", "non-scaling-stroke");
        svg.append(path);
      }
      row.prepend(svg);
    }
    const data = graphData[key].filter(([t]) => t >= now - GRAPH_WINDOW_MS);
    if (data.length < 2) {
      for (const path of svg.children) path.setAttribute("d", "");
      continue;
    }
    // Scale to what's shown (at least 10° tall, so sensor noise stays flat).
    const values = data.flatMap(([, temp, target]) => (target ? [temp, target] : [temp]));
    let lo = Math.min(...values), hi = Math.max(...values);
    if (hi - lo < 10) { const mid = (hi + lo) / 2; lo = mid - 5; hi = mid + 5; }
    const x = (t) => ((t - (now - GRAPH_WINDOW_MS)) / GRAPH_WINDOW_MS * 100).toFixed(2);
    const y = (v) => (92 - (v - lo) / (hi - lo) * 84).toFixed(2);
    const line = data.map(([t, temp], i) => `${i ? "L" : "M"}${x(t)},${y(temp)}`).join("");
    svg.querySelector(".line").setAttribute("d", line);
    svg.querySelector(".area").setAttribute("d", `${line}L${x(data[data.length - 1][0])},100L${x(data[0][0])},100Z`);
    // The target, only where one is set.
    let target = "", drawing = false;
    for (const [t, , goal] of data) {
      if (goal) { target += `${drawing ? "L" : "M"}${x(t)},${y(goal)}`; drawing = true; }
      else drawing = false;
    }
    svg.querySelector(".target").setAttribute("d", target);
  }
}

// ---- rendering ----

function render(s) {
  const state = s.connected ? s.state : "unreachable";
  const active = state === "printing" || state === "paused";

  const pill = $("state");
  pill.textContent = say(STATE_LABELS[state] || state);
  pill.className = `state-pill ${state}`;

  renderAlert(s, state);

  $("filename").textContent = active || state === "complete" ? jobName(s.filename)
    : s.lastJob ? `${say("Last:")} ${jobName(s.lastJob.filename)}` : say("No print");

  // The model's preview picture, once its job has started.
  const thumb = $("job-thumb");
  if (s.thumbnail !== thumb.dataset.src) {
    thumb.dataset.src = s.thumbnail || "";
    if (s.thumbnail) thumb.src = s.thumbnail;
    else thumb.removeAttribute("src");
  }
  thumb.hidden = !s.thumbnail || thumb.dataset.failed === s.thumbnail;

  const fill = $("bar-fill");
  const pct = state === "complete" ? 100 : active ? s.progress * 100 : 0;
  fill.style.width = `${pct}%`;
  fill.className = `bar-fill ${state}`;

  if (active) {
    $("percent").textContent = `${Math.floor(s.progress * 100)}%`;
    $("time-left").textContent = s.timeLeft != null ? `${duration(s.timeLeft)} ${say("left")}` : "";
    $("finish-at").textContent = s.finishAt ? `${say("Done")} ${dayAndTime(s.finishAt)}` : "";
  } else if (state === "complete") {
    $("percent").textContent = say("Done");
    $("time-left").textContent = duration(s.printDuration);
    $("finish-at").textContent = s.lastJob?.endTime ? `${say("Finished")} ${dayAndTime(s.lastJob.endTime)}` : "";
  } else {
    $("percent").textContent = !s.connected ? "—" : state === "busy" ? say("Busy") : say("Idle");
    $("time-left").textContent = "";
    // The last job's end, while idle. Not while busy: that's usually the next
    // job getting ready (the K2 names a new job only once printing starts).
    $("finish-at").textContent = s.lastJob?.endTime && state !== "busy"
      ? `${say(s.lastJob.status === "completed" ? "Finished" : capitalize(s.lastJob.status || ""))} ${dayAndTime(s.lastJob.endTime)}`
      : "";
  }

  $("layer").textContent = active && s.layer != null ? `${s.layer} / ${s.layerCount ?? "?"}` : "—";
  $("elapsed").textContent = active ? duration(s.printDuration) : "—";
  // How far through the current layer, faint behind its row, and the time
  // it has left beside the label (from the slicer's per-layer times).
  const layerProgress = active ? s.layerProgress : null;
  $("layer-fill").hidden = !layerProgress;
  if (layerProgress) $("layer-fill").style.width = `${layerProgress.done * 100}%`;
  $("layer-left").textContent = layerProgress?.left != null ? `${shortDuration(layerProgress.left)} ${say("left")}` : "";

  if (s.connected) recordTemps(s.temps);
  noteTempChanges();
  drawGraphs();
  temp(s.temps?.nozzle, $("temp-nozzle"));
  temp(s.temps?.bed, $("temp-bed"));
  temp(s.temps?.chamber, $("temp-chamber"));
  for (const key of ["nozzle", "bed", "chamber"]) showChange($(`temp-${key}`).closest(".row").querySelector("dt"), key);
  // Printers without a chamber sensor don't get a row for it.
  $("chamber-row").hidden = s.connected && !s.temps?.chamber;

  renderCfs(s.cfs || []);
  renderLight(s.light);
  renderPreview(s);
  renderControls(s, state);
}

// ---- Controls view ----
// Print, temperature and machine controls, in place of the dashboard. Only on
// the kiosk itself (like the light); the server checks that too.

// Presets per heater (°C), as well as Off and ±5.
const HEATER_PRESETS = { nozzle: [200, 220, 250], bed: [60, 80, 100], chamber: [40, 60] };
const HEATER_LABELS = { nozzle: "Nozzle", bed: "Bed", chamber: "Chamber" };
// Untouched this long, the Controls view goes back to the dashboard.
const CONTROLS_IDLE_MS = 2 * 60 * 1000;

let controlsTimer = null;
let heaterKeys = ""; // which heaters the rows were built for
let lastControls = null; // { status, state } from the last update

function showControls(show) {
  document.body.classList.toggle("show-controls", show);
  $("controls").hidden = !show;
  $("controls-toggle").setAttribute("aria-pressed", String(show));
  $("controls-toggle-label").textContent = show ? "Dashboard" : "Controls";
  touchControls();
}

function touchControls() {
  clearTimeout(controlsTimer);
  if (!$("controls").hidden) controlsTimer = setTimeout(() => showControls(false), CONTROLS_IDLE_MS);
}

$("controls-toggle").addEventListener("click", () => showControls($("controls").hidden));
$("controls").addEventListener("pointerdown", touchControls);

// Temperatures, moving and homing only then (the server checks too).
const IDLE_STATES = ["standby", "complete", "cancelled"];

function renderControls(s, state) {
  lastControls = { s, state };
  const ready = s.connected && state !== "offline";
  const printing = state === "printing" || state === "paused";
  const idle = ready && IDLE_STATES.includes(state);

  $("ctl-job").textContent = printing ? `${STATE_LABELS[state]}: ${jobName(s.filename)}`
    : ready ? `No print running (${STATE_LABELS[state] || state})` : "Can't reach the printer";
  const pause = $("ctl-pause");
  pause.textContent = state === "paused" ? "Resume" : "Pause";
  pause.dataset.action = state === "paused" ? "resume" : "pause";
  pause.disabled = !ready || !printing;
  $("ctl-cancel").disabled = !ready || !printing;
  for (const button of document.querySelectorAll("#controls [data-action]")) {
    if (button === pause) continue;
    button.disabled = !ready || (button.hasAttribute("data-idle-only") && !idle);
  }
  $("ctl-idle-note").hidden = !ready || idle;

  // Move pad: each axis only once it's homed, and not during a print.
  const homed = s.toolhead?.homed || "";
  for (const [i, axis] of ["x", "y", "z"].entries()) {
    const box = document.querySelector(`.jog-axis[data-axis="${axis}"]`);
    const position = s.toolhead?.position?.[i];
    box.querySelector(".value-box").textContent = homed.includes(axis) && position != null ? position.toFixed(1) : "—";
    for (const button of box.querySelectorAll("button")) button.disabled = !idle || !homed.includes(axis);
  }
  $("ctl-home-note").hidden = !idle || homed === "xyz";

  // Heater rows: built when the set of heaters changes, then just updated, so
  // a tap never lands on a row that's being replaced.
  const limits = s.heaterLimits || {};
  const keys = Object.keys(limits).join();
  if (keys !== heaterKeys) buildHeaterRows(limits);
  heaterKeys = keys;
  for (const key of Object.keys(limits)) {
    const t = s.temps?.[key];
    const target = Math.round(t?.target || 0);
    const now = document.querySelector(`#heater-${key} .now`);
    now.textContent = t ? `${t.temp.toFixed(1)}°C` : "—";
    showChange(document.querySelector(`#heater-${key} .heater-label`), key);
    const box = document.querySelector(`#heater-${key} .value-box`);
    box.textContent = target ? `${target}°C` : "Off";
    box.disabled = !idle;
    for (const button of document.querySelectorAll(`#heater-${key} .ctl-btn`)) {
      const step = button.dataset.step;
      const value = Number(button.dataset.target);
      button.disabled = !idle || (step && !target);
      button.classList.toggle("selected", !step && value === target);
    }
  }
}

function buildHeaterRows(limits) {
  const box = $("ctl-heaters");
  box.textContent = "";
  for (const [key, max] of Object.entries(limits)) {
    const row = el("div", "heater");
    row.id = `heater-${key}`;
    const head = el("div", "heater-head");
    // Now / target: tap the target to type one.
    const targetBox = el("button", "value-box", "—");
    targetBox.addEventListener("click", () => openNumpad({
      title: `${HEATER_LABELS[key] || key} target`,
      value: Math.round(lastControls?.s.temps?.[key]?.target || 0),
      unit: "°C", min: 0, max, decimals: false,
      hint: `0 (off) to ${max}°C`,
      onSet: (target) => sendControl({ action: "set_temp", heater: key, target },
        target ? `${HEATER_LABELS[key]} → ${target}°C` : `${HEATER_LABELS[key]} off`),
    }));
    const reading = el("span", "heater-reading");
    reading.append(el("span", "now", "—"), el("span", "target", " / "), targetBox);
    head.append(el("span", "heater-label", HEATER_LABELS[key] || key), reading);
    const buttons = el("div", "ctl-row");
    const add = (label, attrs) => {
      const button = el("button", "ctl-btn", label);
      Object.assign(button.dataset, attrs);
      buttons.append(button);
    };
    add("−5", { step: "-5" });
    add("Off", { target: "0" });
    for (const preset of HEATER_PRESETS[key] || []) if (preset <= max) add(`${preset}°`, { target: String(preset) });
    add("+5", { step: "5" });
    buttons.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      const current = Math.round(lastControls?.s.temps?.[key]?.target || 0);
      const target = button.dataset.step
        ? Math.min(max, Math.max(0, current + Number(button.dataset.step)))
        : Number(button.dataset.target);
      sendControl({ action: "set_temp", heater: key, target },
        target ? `${HEATER_LABELS[key]} → ${target}°C` : `${HEATER_LABELS[key]} off`);
    });
    row.append(head, buttons);
    box.append(row);
  }
}

// Move pad: the step size, and a move per tap.
let jogStep = 1;
$("jog-steps").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  jogStep = Number(button.dataset.step);
  for (const b of $("jog-steps").children) b.classList.toggle("selected", b === button);
});
for (const box of document.querySelectorAll(".jog-axis")) {
  box.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const axis = box.dataset.axis;
    if (button.classList.contains("value-box")) {
      // Tap the position to type one: the axis moves straight there.
      const i = "xyz".indexOf(axis);
      const limit = lastControls?.s.axisLimits?.[axis];
      if (!limit) return toast(`This printer's ${axis.toUpperCase()} travel isn't known.`, true);
      openNumpad({
        title: `Move ${axis.toUpperCase()} to`,
        value: lastControls?.s.toolhead?.position?.[i] ?? 0,
        unit: "mm", min: Math.max(0, limit.min), max: limit.max, decimals: true,
        hint: `${Math.max(0, limit.min)} to ${limit.max} mm`,
        onSet: (position) => sendControl({ action: "move_to", axis, position }, `${axis.toUpperCase()} → ${position} mm`),
      });
      return;
    }
    const distance = jogStep * Number(button.dataset.dir);
    sendControl({ action: "jog", axis: box.dataset.axis, distance },
      `${box.dataset.axis.toUpperCase()} ${distance > 0 ? "+" : "−"}${jogStep} mm`);
  });
}

// ---- number pad ----
// For typing a heater target or an axis position. The first key replaces the
// current value; Set is only possible for a value within min..max.

let numpad = null; // the open pad's options and typed text

function openNumpad(options) {
  numpad = { ...options, text: String(Math.round(options.value * 10) / 10), fresh: true };
  $("numpad-title").textContent = options.title;
  $("numpad-unit").textContent = options.unit;
  $("numpad").hidden = false;
  showNumpad();
}

function closeNumpad() {
  $("numpad").hidden = true;
  numpad = null;
}

function numpadValue() {
  const value = Number(numpad.text);
  return numpad.text !== "" && !numpad.text.endsWith(".") && Number.isFinite(value) &&
    value >= numpad.min && value <= numpad.max ? value : null;
}

function showNumpad() {
  $("numpad-value").textContent = numpad.text;
  const ok = numpadValue() !== null;
  $("numpad-set").disabled = !ok;
  $("numpad-hint").textContent = numpad.hint;
  $("numpad-hint").className = ok || numpad.text === "" ? "numpad-hint" : "numpad-hint bad";
}

$("numpad-keys").addEventListener("click", (event) => {
  const key = event.target.closest("button")?.dataset.key;
  if (!key || !numpad) return;
  if (numpad.fresh && key !== "back") numpad.text = "";
  numpad.fresh = false;
  if (key === "back") numpad.text = numpad.text.slice(0, -1);
  else if (key === ".") {
    if (numpad.decimals && !numpad.text.includes(".")) numpad.text = (numpad.text || "0") + ".";
  } else if (numpad.text.replace(".", "").length < 5 && !/\.\d{2}$/.test(numpad.text)) {
    numpad.text = numpad.text === "0" ? key : numpad.text + key;
  }
  showNumpad();
});
$("numpad-cancel").addEventListener("click", closeNumpad);
$("numpad-set").addEventListener("click", () => {
  const value = numpadValue();
  if (value === null) return;
  const onSet = numpad.onSet;
  closeNumpad();
  onSet(value);
});
$("numpad").addEventListener("click", (event) => {
  if (event.target === $("numpad")) closeNumpad(); // tapped outside the pad
});
$("numpad").addEventListener("pointerdown", touchControls);

// The Machine card's buttons, and Pause/Resume.
for (const button of document.querySelectorAll("#controls [data-action]")) {
  button.addEventListener("click", () => {
    const labels = { pause: "Pausing…", resume: "Resuming…", cooldown: "Heaters off", home: "Homing…", motors_off: "Motors off" };
    sendControl({ action: button.dataset.action }, labels[button.dataset.action]);
  });
}

$("ctl-cancel").addEventListener("click", () => {
  confirmThen("Cancel this print? It can't be resumed.", "Keep printing", "Cancel print",
    () => sendControl({ action: "cancel" }, "Cancelling…"));
});

function confirmThen(question, no, yes, onYes) {
  $("confirm-text").textContent = question;
  $("confirm-no").textContent = no;
  $("confirm-yes").textContent = yes;
  $("confirm").hidden = false;
  const close = () => { $("confirm").hidden = true; };
  $("confirm-no").onclick = close;
  $("confirm-yes").onclick = () => { close(); onYes(); };
}

async function sendControl(body, doing) {
  toast(doing);
  let result;
  try {
    const res = await fetch("/api/control", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    result = await res.json();
  } catch {
    result = { ok: false, message: "Couldn't reach the server." };
  }
  if (!result.ok) toast(result.message || "That didn't work.", true);
}

let toastTimer = null;
function toast(text, bad = false) {
  const box = $("toast");
  box.textContent = text;
  box.className = bad ? "toast bad" : "toast";
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), bad ? 5000 : 2500);
}

// ---- light ----

let lightOn = null; // as last reported, or as just switched

function renderLight(light) {
  const button = $("light");
  // Only on the kiosk itself, and only for a printer with a light it can tell is on or off.
  button.hidden = !config?.canChange || !light;
  if (!light || button.disabled) return; // a switch is on its way
  lightOn = light.on;
  button.classList.toggle("on", lightOn);
  button.setAttribute("aria-pressed", String(lightOn));
  button.setAttribute("aria-label", lightOn ? "Turn the light off" : "Turn the light on");
}

$("light").addEventListener("click", async () => {
  const button = $("light");
  const on = !lightOn;
  button.disabled = true;
  button.classList.toggle("on", on); // straight away; the next update confirms it
  try {
    const res = await fetch("/api/light", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    });
    if (res.ok) lightOn = on;
  } catch {
    // Unchanged; the next update shows the light as it is.
  }
  button.disabled = false;
  button.classList.toggle("on", !!lightOn);
});

// One panel per CFS unit: its humidity, then a bar per slot, filled to the
// spool's remaining filament in the filament's color.
function renderCfs(units) {
  const cfs = $("cfs");
  cfs.textContent = "";
  cfs.hidden = !units.length;
  cfs.classList.toggle("two-rows", units.length > 2);
  for (const u of units) {
    const panel = el("div", "cfs-unit");
    const head = el("div", "cfs-head");
    head.append(el("span", "cfs-name", `CFS ${u.unit}`));
    if (u.humidity != null) {
      head.append(el("span", u.humidity >= HUMID_PCT ? "cfs-hum humid" : "cfs-hum", `${Math.round(u.humidity)}% ${say("RH")}`));
    }
    panel.append(head);

    const slots = el("div", "slots");
    for (const slot of u.slots) {
      const empty = !slot.color;
      const unknown = !empty && slot.remaining == null;
      const classes = ["slot", empty && "empty", unknown && "unknown", slot.loaded && "loaded",
        slot.remaining != null && slot.remaining <= LOW_PCT && "low"];
      const box = el("div", classes.filter(Boolean).join(" "));

      const tube = el("div", "tube");
      if (!empty) {
        const fill = el("div", "fill");
        fill.style.background = slot.color;
        fill.style.height = `${unknown ? 100 : Math.max(slot.remaining, 0)}%`;
        tube.append(fill);
      }
      // The caret points up at the slot feeding the toolhead.
      box.append(tube, el("div", "caret"), el("div", "slot-letter", slot.letter),
        el("div", "slot-pct", empty ? "—" : unknown ? "?" : `${Math.round(slot.remaining)}%`));
      slots.append(box);
    }
    panel.append(slots);
    cfs.append(panel);
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function renderAlert(s, state) {
  const alert = $("alert");
  let text = null;
  let kind = "error";
  const latest = s.errors?.[s.errors.length - 1];
  if (!s.connected) {
    text = speakKlingon() ? `${config?.printerHost ?? "ghItlhwI'"} ghoSlaHbe'. nIDqa'taH…` // "can't approach; trying again"
      : `Can't reach the printer at ${config?.printerHost ?? "its address"}. Retrying…`;
    if (config?.canChange) text += " Tap here to change the address.";
  } else if (latest) {
    text = `${clockTime(latest.time)}  ${latest.message}`;
  } else if (state === "paused") {
    text = say("Print paused");
    kind = "paused";
  } else if (state === "complete") {
    text = `${say("Print complete:")} ${jobName(s.filename)}`;
    kind = "complete";
  }
  alert.hidden = !text;
  alert.className = `alert ${kind}${!s.connected && config?.canChange ? " tappable" : ""}`;
  $("alert-text").textContent = text || "";
}

// ---- layer preview ----
// While a job prints, the camera area can switch to a top-down drawing of the
// current layer (from the G-code, read by the server): what's printed so far
// bright, in the filament's colour; the rest of the layer faint.

let viewMode = "camera"; // or "preview"
// Settings → Display, "Camera and layer preview": "switch" (one at a time,
// with a button) or "split" (while a job prints, the camera on top and the
// preview below, no button).
let previewLayout = "switch";
// A cast screen (opened with ?cast by the server's casting): nobody can tap
// it, so it's always split while printing, and its camera comes from go2rtc
// (see loadCamera).
const castView = new URLSearchParams(location.search).has("cast");
if (castView) document.body.classList.add("cast-view");
const layerCache = { key: "", data: null, loading: false };

$("view-switch").addEventListener("click", (event) => {
  event.stopPropagation(); // not a tap on the camera (full screen)
  viewMode = viewMode === "camera" ? "preview" : "camera";
  renderPreview(lastStatus);
});
// Nor the start of a press-and-hold (restart the camera).
$("view-switch").addEventListener("pointerdown", (event) => event.stopPropagation());

let lastStatus = null;

function renderPreview(s) {
  lastStatus = s;
  const preview = s?.preview;
  const split = previewLayout === "split" || castView;
  $("view-switch").hidden = !preview || split;
  if (!preview) viewMode = "camera";
  if (split) viewMode = preview ? "preview" : "camera";
  document.body.classList.toggle("split", split && !!preview);
  const showing = viewMode === "preview";
  $("view-switch").textContent = showing ? "Camera" : "Preview";
  $("preview").hidden = !showing;
  $("preview-note").hidden = !showing;
  showCameraNote();
  if (!showing) return;
  if (!preview.ready) {
    $("preview-note").textContent = say("Preparing preview…");
    clearPreview();
    return;
  }
  const key = `${s.filename}#${preview.layer}`;
  if (layerCache.key !== key && !layerCache.loading) {
    layerCache.loading = true;
    fetch(`/api/gcode-layer?file=${encodeURIComponent(s.filename)}&layer=${preview.layer}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        layerCache.key = key;
        layerCache.data = data;
      })
      .catch(() => {})
      .finally(() => {
        layerCache.loading = false;
        renderPreview(lastStatus);
      });
  }
  const data = layerCache.key === key ? layerCache.data : null;
  if (data) drawLayer(data, preview.position, s.spoolColours);
  // The printer's own layer count when it has one (it leaves out purge lines).
  const shown = s.layer != null && s.layerCount ? `${s.layer} / ${s.layerCount}` : `${preview.layer} / ${preview.layers}`;
  $("preview-note").textContent = data ? `${say("Layer")} ${shown} · Z ${data.z.toFixed(2)} mm` : say("Loading layer…");
}

function clearPreview() {
  const canvas = $("preview");
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  canvas.style.background = "";
}

// The preview's background (Settings → Display): one of these shades, or
// "auto": the theme's dark one, unless one of the job's filaments wouldn't
// show on it (black, say); then shade C, a few shades lighter, still dark
// enough to sit with the rest of the dashboard. Picked from all of the job's
// colours, so it doesn't change from layer to layer.
const PREVIEW_SHADES = { a: "#161b22", b: "#1e242b", c: "#262c33", d: "#2f363e", e: "#3a414a" };
const PREVIEW_LIFTED = PREVIEW_SHADES.c;
let previewShade = "auto";
const PREVIEW_DARK = "#0d1117"; // about the themes' own
const PREVIEW_ENOUGH_CONTRAST = 1.8;

function luminance(hex) {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!match) return null;
  const [r, g, b] = [0, 2, 4].map((i) => {
    const v = parseInt(match[1].slice(i, i + 2), 16) / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function previewBackground(colours) {
  if (PREVIEW_SHADES[previewShade]) return PREVIEW_SHADES[previewShade];
  const dark = luminance(PREVIEW_DARK);
  const hidden = colours.map(luminance).some((l) => l != null && (l + 0.05) / (dark + 0.05) < PREVIEW_ENOUGH_CONTRAST);
  return hidden ? PREVIEW_LIFTED : "";
}

// spools: the colour of the spool each tool is printing from (the CFS), which
// wins over the colours picked in the slicer.
function drawLayer(data, position, spools) {
  const canvas = $("preview");
  const scale = window.devicePixelRatio || 1;
  const width = canvas.clientWidth * scale, height = canvas.clientHeight * scale;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, height);
  if (!data.bounds) return;
  // Fit the whole model (all layers) with a margin, Y up as on the bed.
  const [x0, y0, x1, y1] = data.bounds;
  const margin = 0.08 * Math.min(width, height);
  const fit = Math.min((width - 2 * margin) / Math.max(x1 - x0, 1), (height - 2 * margin) / Math.max(y1 - y0, 1));
  const ox = (width - (x1 - x0) * fit) / 2, oy = (height - (y1 - y0) * fit) / 2;
  const px = (x) => ox + (x - x0) * fit;
  const py = (y) => height - (oy + (y - y0) * fit);

  const segs = data.segs, ends = data.ends;
  const toolOf = (i) => (data.tools ? data.tools[i] : 0);
  const colourOf = (tool) => spools?.[tool] || data.colours?.[tool] || "#ff9a4d";
  const colour = (i) => colourOf(toolOf(i));
  const background = previewBackground((data.jobTools?.length ? data.jobTools : [0]).map(colourOf));
  canvas.style.background = background;
  ctx.lineCap = "round";
  // The whole layer, faint.
  ctx.strokeStyle = "rgba(139, 152, 165, 0.28)";
  ctx.lineWidth = Math.max(1, 1.2 * scale);
  ctx.beginPath();
  for (let i = 0; i < ends.length; i++) {
    ctx.moveTo(px(segs[4 * i]), py(segs[4 * i + 1]));
    ctx.lineTo(px(segs[4 * i + 2]), py(segs[4 * i + 3]));
  }
  ctx.stroke();
  // What's printed so far, bright, in each segment's filament colour: one
  // path per run of the same colour (a single one for one filament).
  let done = 0;
  const runs = [];
  while (done < ends.length && ends[done] <= position) {
    const from = done;
    while (done < ends.length && ends[done] <= position && toolOf(done) === toolOf(from)) done++;
    runs.push({ from, to: done, colour: colour(from) });
  }
  const trace = (run) => {
    ctx.beginPath();
    for (let i = run.from; i < run.to; i++) {
      ctx.moveTo(px(segs[4 * i]), py(segs[4 * i + 1]));
      ctx.lineTo(px(segs[4 * i + 2]), py(segs[4 * i + 3]));
    }
    ctx.stroke();
  };
  ctx.lineWidth = Math.max(1.5, 2 * scale);
  for (const run of runs) {
    ctx.strokeStyle = run.colour;
    trace(run);
  }
  // The nozzle: where the last printed segment ended.
  if (done) {
    ctx.fillStyle = "#fff";
    ctx.beginPath();
    ctx.arc(px(segs[4 * done - 2]), py(segs[4 * done - 1]), 4 * scale, 0, 2 * Math.PI);
    ctx.fill();
  }
}

// ---- camera on cast screens: go2rtc's stream through MSE ----
// The K2 stamps its frames coarsely (bursts a millisecond apart, then a jump
// of up to a second) though they arrive evenly, and a video player trusting
// those times drops most frames. So each frame is given a steady time from
// the average arrival rate, back to back with the one before (eased toward
// its actual arrival, so it never drifts); playback waits for MSE_TARGET seconds of buffer, then runs at
// normal speed, easing up to 3% faster when behind. It only jumps when far
// behind, or when playback has stuck for a second with video waiting (a frame
// lost between the camera and go2rtc stalls the decoder until the next full
// frame, which can take seconds): a jump otherwise freezes it until then too.
// (Worked out in docs/casting.md.)

const MSE_TARGET = 1.2;
const MSE_MAX_BEHIND = 6;
const MSE_STUCK_SECS = 1;
const MSE_SETTLE_SECS = 5;
const MSE_CODECS = ["avc1.640029", "avc1.64002A", "avc1.640033", "avc1.42E01E", "avc1.4D401F"];
let mse = null; // { ws, timer } while playing

function stopMse() {
  if (!mse) return;
  if (mse.ws) {
    mse.ws.onclose = null;
    mse.ws.close();
  }
  clearInterval(mse.timer);
  mse = null;
}

function startMse(video, url) {
  stopMse();
  const ms = new MediaSource();
  video.srcObject = null;
  video.src = URL.createObjectURL(ms);
  video.autoplay = false;
  const state = { ws: null, timer: null };
  mse = state;
  framesSeen = -1;
  lastFrameAt = Date.now();

  let interval = 66, lastArrival = 0, t0 = 0, ts = -1;
  function retime(buf) {
    const now = performance.now();
    if (!t0) t0 = now;
    if (lastArrival) interval = interval * 0.95 + Math.min(200, Math.max(20, now - lastArrival)) * 0.05;
    lastArrival = now;
    const arrived = now - t0;
    if (ts < 0) ts = arrived;
    // Back to back: this frame starts where the last one ended, and lasts the
    // average spacing, eased toward its actual arrival so it never drifts. (A
    // frame starting later than the last one ends leaves a gap, and the
    // player stalls at it.)
    const step = Math.max(10, interval + (arrived - ts) * 0.02);
    const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const find = (name, from) => {
      for (let i = from; i < buf.length - 4; i++) {
        if (buf[i] === name.charCodeAt(0) && buf[i + 1] === name.charCodeAt(1) &&
            buf[i + 2] === name.charCodeAt(2) && buf[i + 3] === name.charCodeAt(3)) return i - 4;
      }
      return -1;
    };
    const moof = find("moof", 0);
    if (moof < 0) return buf; // the init segment
    const tfhd = find("tfhd", moof);
    const tfdt = find("tfdt", moof);
    // In 90 kHz ticks, rounded once each, so they add up exactly.
    const start = Math.round(ts * 90);
    const length = Math.round((ts + step) * 90) - start;
    ts += step;
    if (tfdt >= 0) {
      if (buf[tfdt + 8] === 1) view.setBigUint64(tfdt + 12, BigInt(start));
      else view.setUint32(tfdt + 12, start);
    }
    if (tfhd >= 0) {
      const flags = view.getUint32(tfhd + 8) & 0xffffff;
      if (flags & 0x08) view.setUint32(tfhd + 16 + (flags & 0x01 ? 8 : 0) + (flags & 0x02 ? 4 : 0), length);
    }
    return buf;
  }

  ms.addEventListener("sourceopen", () => {
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.binaryType = "arraybuffer";
    let sb = null;
    const queue = [];
    const pump = () => {
      if (!sb || sb.updating || !queue.length) return;
      try { sb.appendBuffer(queue.shift()); } catch {}
    };
    ws.onopen = () => ws.send(JSON.stringify({ type: "mse",
      value: MSE_CODECS.filter((c) => MediaSource.isTypeSupported(`video/mp4; codecs="${c}"`)).join() }));
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "mse") {
          sb = ms.addSourceBuffer(msg.value);
          sb.mode = "segments";
          sb.addEventListener("updateend", () => {
            if (sb.buffered.length && !sb.updating) {
              const end = sb.buffered.end(sb.buffered.length - 1);
              if (end - sb.buffered.start(0) > 60) { try { sb.remove(0, end - 30); } catch {} }
            }
            pump();
          });
        }
        return;
      }
      queue.push(retime(new Uint8Array(ev.data)));
      pump();
    };
    // Dropped: start again shortly (the camera watchdog also catches stalls).
    ws.onclose = () => { if (mse === state) setTimeout(() => mse === state && startMse(video, url), 3000); };
  }, { once: true });

  let waiting = true;
  let lastTime = -1, stuckSince = 0, jumpedAt = 0;
  state.timer = setInterval(() => {
    const b = video.buffered;
    if (!b.length) return;
    const end = b.end(b.length - 1);
    if (video.currentTime < b.start(0)) video.currentTime = b.start(0);
    const ahead = end - video.currentTime;
    // Stuck with video waiting to play: the decoder lost its place (a frame
    // lost on the way from the camera stalls it until the next full frame).
    // Jump to the newest video after MSE_STUCK_SECS rather than waiting.
    const now = performance.now();
    const moving = video.currentTime !== lastTime;
    lastTime = video.currentTime;
    if (moving || waiting || ahead < 0.5) stuckSince = 0;
    else if (!stuckSince) stuckSince = now;
    // (Once jumped, give the decoder MSE_SETTLE_SECS to reach the next full
    // frame rather than jumping again and again.)
    const stuck = stuckSince && now - stuckSince > MSE_STUCK_SECS * 1000 && now - jumpedAt > MSE_SETTLE_SECS * 1000;
    if (ahead > MSE_MAX_BEHIND || stuck) {
      video.currentTime = end - MSE_TARGET;
      stuckSince = 0;
      jumpedAt = now;
    }
    if (waiting && ahead >= MSE_TARGET) { waiting = false; video.play().catch(() => {}); }
    else if (!waiting && ahead < 0.15) { waiting = true; video.pause(); }
    else if (!waiting && video.paused) video.play().catch(() => {});
    video.playbackRate = ahead > MSE_TARGET + 0.2 ? 1.03 : 1;
  }, 250);
}

// ---- Cast: sending the dashboard to screens (kiosk only) ----

let castTimer = null;

async function showCastList(refresh = false) {
  const list = $("cast-list");
  if (!list.children.length || refresh) list.replaceChildren(el("div", "cast-empty", "Looking for screens…"));
  let found;
  try {
    found = await (await fetch(`/api/cast${refresh ? "?refresh" : ""}`, { cache: "no-store" })).json();
  } catch {
    found = { available: true, devices: [] };
  }
  if ($("cast").hidden) return;
  $("cast-open").classList.toggle("on", found.devices.some((d) => d.casting));
  if (!found.available) {
    list.replaceChildren(el("div", "cast-empty", "Casting isn't set up on this kiosk (install.sh sets it up)."));
    return;
  }
  if (!found.devices.length) {
    list.replaceChildren(el("div", "cast-empty", "No TVs or displays found. Check they're on and on the same network."));
    return;
  }
  list.replaceChildren(...found.devices.map((device) => {
    const row = el("div", device.casting ? "cast-row on" : "cast-row");
    const name = el("div", "cast-name", device.name);
    name.append(el("span", "cast-model", device.casting ? `${device.model} · showing the dashboard` : device.model));
    const button = el("button", device.casting ? "ctl-btn danger" : "ctl-btn", device.casting ? "Stop" : "Cast");
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = device.casting ? "Stopping…" : "Starting…";
      await fetch("/api/cast", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: device.casting ? "stop" : "start", device: device.id }),
      }).catch(() => {});
      if (!device.casting) toast(`Casting to ${device.name}; it can take half a minute to appear.`);
      showCastList();
    });
    row.append(name, button);
    return row;
  }));
}

// The network's searched for screens when the menu opens (or on Refresh);
// while it's open, the list is updated from that search, for what's casting.
$("cast-open").addEventListener("click", () => {
  $("cast").hidden = false;
  showCastList(true);
  clearInterval(castTimer);
  castTimer = setInterval(() => !$("cast").hidden && showCastList(), 10000);
});
$("cast-close").addEventListener("click", () => {
  $("cast").hidden = true;
  clearInterval(castTimer);
});
$("cast-refresh").addEventListener("click", () => showCastList(true));

// ---- camera ----

// Creality's WebRTC camera (a K2's) is connected to here, rather than shown
// in its own page in a frame, so a stalled stream can be noticed: every
// CAMERA_CHECK_MS, if no new frame has arrived for CAMERA_STALL_MS, it
// reconnects, and keeps trying until the camera answers again.
const CAMERA_CHECK_MS = 10 * 1000;
const CAMERA_STALL_MS = 20 * 1000;
// Pressing and holding the camera restarts it.
const CAMERA_HOLD_MS = 1000;

let rtc = null; // the WebRTC connection, while there is one
let framesSeen = -1; // decoded frames, at the last check
let lastFrameAt = 0; // when a new frame was last seen
let cameraNote = null; // shown over the camera while there's no picture

function stopCamera() {
  stopMse();
  const frame = $("camera-frame");
  const img = $("camera-img");
  const video = $("camera-video");
  frame.hidden = img.hidden = video.hidden = true;
  frame.removeAttribute("src");
  img.removeAttribute("src"); // also closes an MJPEG stream's connection
  if (rtc) rtc.close();
  rtc = null;
  video.srcObject = null;
}

function loadCamera() {
  stopCamera();
  // A cast screen's browser can't play the K2's WebRTC camera; go2rtc
  // re-wraps it for MSE, which it can.
  const camera = castView && config?.castStream && config?.camera?.type === "webrtc"
    ? { ...config.camera, type: "mse" } : config?.camera;
  // Printer offline: no camera until it's back (then the poll restarts it).
  if (!camera || wasConnected === false) return;
  const shown = { mjpeg: $("camera-img"), webrtc: $("camera-video"), mse: $("camera-video") }[camera.type]
    || $("camera-frame");
  if (camera.type === "mse") {
    startMse(shown, `ws://${location.host}/api/camera-stream`);
  } else if (camera.type === "webrtc") {
    connectWebrtc(camera);
  } else {
    // A fresh query string reloads it even when the URL is unchanged.
    const url = new URL(camera.url);
    if (!["http:", "https:"].includes(url.protocol)) return; // the server checks too
    url.searchParams.set("t", Date.now());
    shown.src = url.href;
  }
  // As set up in Fluidd or Mainsail. (A frame is someone else's page, so
  // only an image or video can be turned.)
  const turns = [];
  if (camera.type !== "frame" && camera.rotation) turns.push(`rotate(${camera.rotation}deg)`);
  if (camera.flipH) turns.push("scaleX(-1)");
  if (camera.flipV) turns.push("scaleY(-1)");
  shown.style.transform = turns.join(" ");
  shown.hidden = false;
}

// The same exchange Creality's camera page makes: a WebRTC offer, base64
// JSON, POSTed to the printer, which answers in kind.
async function connectWebrtc(camera) {
  // Creality's page uses this STUN server too; without it the browser offers
  // only an obscured local address, and the printer's stream never starts.
  const pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
  rtc = pc;
  cameraNote = "Connecting to the camera…";
  framesSeen = -1;
  lastFrameAt = Date.now(); // a new connection gets the full time to start
  const video = $("camera-video");
  pc.addTransceiver("video", { direction: "sendrecv" });
  pc.ontrack = (event) => {
    if (rtc !== pc) return;
    video.srcObject = event.streams[0];
    video.play().catch(() => {});
  };
  try {
    await pc.setLocalDescription(await pc.createOffer());
    await iceGathered(pc);
    const res = await fetch(camera.signalUrl, {
      method: "POST",
      headers: { "Content-Type": "plain/text" },
      body: btoa(JSON.stringify({ type: "offer", sdp: pc.localDescription.sdp })),
    });
    const answer = JSON.parse(atob(await res.text()));
    if (rtc !== pc) return; // replaced while waiting
    await pc.setRemoteDescription(answer);
  } catch {
    // The watchdog tries again.
  }
}

// Resolves once the offer has all its network addresses (or after 3 s).
function iceGathered(pc) {
  return new Promise((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    const done = () => pc.iceGatheringState === "complete" && resolve();
    pc.addEventListener("icegatheringstatechange", done);
    setTimeout(resolve, 3000);
  });
}

// Every CAMERA_CHECK_MS: is the WebRTC camera still delivering frames?
function checkCamera() {
  if (config?.camera?.type !== "webrtc" || !wasConnected) return; // printer away: restarted when it's back
  const viaMse = castView && config?.castStream;
  const video = $("camera-video");
  const frames = video.getVideoPlaybackQuality?.().totalVideoFrames ?? 0;
  if (frames !== framesSeen) {
    framesSeen = frames;
    lastFrameAt = Date.now();
  }
  if (frames > 0 && video.videoWidth) cameraNote = null;
  const state = rtc?.connectionState;
  const lost = viaMse ? !mse : !rtc || state === "failed" || state === "closed";
  if (lost || Date.now() - lastFrameAt > CAMERA_STALL_MS) {
    loadCamera();
  }
  showCameraNote();
}

function showCameraNote() {
  const msg = $("camera-msg");
  const text = !wasConnected ? say("Camera unavailable")
    : !config?.camera ? say("No camera set up")
    : config.camera.type === "webrtc" ? say(cameraNote)
    : null;
  msg.textContent = text || "";
  // (The camera's own news: not over the preview, unless they're split.)
  msg.hidden = !text || (viewMode === "preview" && previewLayout !== "split" && !castView);
}

// The picture has started: no need for "Connecting to the camera…".
$("camera-video").addEventListener("playing", () => {
  cameraNote = null;
  showCameraNote();
});

// An MJPEG stream that drops: try again shortly.
// A file with no preview picture: hide the corner rather than show a broken image.
$("job-thumb").addEventListener("error", () => {
  $("job-thumb").dataset.failed = $("job-thumb").dataset.src;
  $("job-thumb").hidden = true;
});

$("camera-img").addEventListener("error", () => {
  if (!$("camera-img").hidden) setTimeout(loadCamera, CAMERA_CHECK_MS);
});

async function reloadConfig() {
  try {
    config = await (await fetch("/api/config", { cache: "no-store" })).json();
  } catch {
    // Keep the last one.
  }
}

// Tap: full screen and back. Press and hold: restart the camera.
let holdTimer = null;
let held = false;
$("camera").addEventListener("pointerdown", () => {
  held = false;
  holdTimer = setTimeout(() => {
    held = true;
    cameraNote = "Restarting the camera…";
    showCameraNote();
    reloadConfig().then(loadCamera);
  }, CAMERA_HOLD_MS);
});
for (const type of ["pointerup", "pointerleave", "pointercancel"]) {
  $("camera").addEventListener(type, () => clearTimeout(holdTimer));
}
$("camera").addEventListener("click", () => {
  if (held) held = false; // that was a hold, not a tap
  else document.body.classList.toggle("camera-full");
});

// ---- settings ----

// Opens the setup wizard: setupUrl("settings") -> "setup.html?settings&kiosk".
const setupUrl = (mode) => "setup.html?" + [mode, kiosk && "kiosk"].filter(Boolean).join("&");

// The settings button opens Settings, but only after being held, so a stray
// tap doesn't land there.
function initSettingsButton() {
  const button = $("settings-open");
  let timer = null;
  const cancel = () => {
    clearTimeout(timer);
    timer = null;
    button.classList.remove("holding");
  };
  button.addEventListener("pointerdown", () => {
    button.classList.add("holding");
    timer = setTimeout(() => location.assign(setupUrl("settings")), SETTINGS_HOLD_MS);
  });
  for (const type of ["pointerup", "pointerleave", "pointercancel"]) button.addEventListener(type, cancel);
}

// Tapping the "Can't reach the printer" banner goes straight to the
// printer's address in Settings.
$("alert").addEventListener("click", () => {
  if ($("alert").classList.contains("tappable")) location.assign(setupUrl("printer"));
});

// The printer keeps not answering: if that's because the kiosk itself has no
// Wi-Fi, open the setup wizard's Wi-Fi step.
async function checkWifi() {
  try {
    const res = await fetch("/api/system/status", { cache: "no-store" });
    // Only the kiosk itself may ask; anywhere else, this just stays put.
    if (res.ok && !(await res.json()).online) location.replace(setupUrl("wifi"));
  } catch {
    // Can't tell; keep retrying.
  }
}

// ---- polling ----

async function poll() {
  let status;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    status = await response.json();
  } catch {
    status = { connected: false };
  }
  failures = status.connected ? 0 : failures + 1;
  // A missed reading or two (the printer is slow to answer at times, e.g. as
  // a print starts): keep showing the last one rather than "Can't reach".
  if (status.connected || failures >= MISSES_BEFORE_UNREACHABLE || wasConnected === null) {
    render(status);
    // Back after a dropout: look for the camera again (the printer may not
    // have answered when the page started), then restart it.
    if (status.connected && wasConnected === false) {
    reloadConfig().then(loadCamera);
    loadHistory();
  }
    // Gone: clear the picture rather than leave its last frame up, frozen.
    if (!status.connected && wasConnected !== false) stopCamera();
    wasConnected = status.connected;
    showCameraNote();
  }
  if (failures === FAILURES_BEFORE_WIFI_CHECK && config?.canChange) checkWifi();
  setTimeout(poll, POLL_MS);
}

function tickClock() {
  $("clock").textContent = clockTime(Date.now() / 1000);
}

const SETTINGS_CHECK_MS = 30000;

function applySettings(settings) {
  hour12 = settings.clock !== "24";
  previewLayout = settings.previewLayout === "split" ? "split" : "switch";
  previewShade = settings.previewBackground || "auto";
  applyTheme(settings.theme);
  const words = settings.language === "english" ? "english" : "klingon";
  if (words !== language) {
    language = words;
    relabel();
  }
}

async function start() {
  try {
    config = await (await fetch("/api/config")).json();
  } catch {
    config = null;
  }
  // Until setup is done, the wizard comes first (on the kiosk itself; other
  // devices just view).
  const settings = await fetch("/api/settings", { cache: "no-store" })
    .then((res) => res.json())
    .catch(() => ({}));
  if (config?.canChange && !settings.setupComplete) {
    location.replace(setupUrl());
    return;
  }
  applySettings(settings);
  $("printer-name").textContent = config?.printerName || "Printer";
  tickClock();
  setInterval(tickClock, 10000);
  // Cast screens stay up for days: pick up changed settings (a new look, say).
  setInterval(() => fetch("/api/settings", { cache: "no-store" })
    .then((res) => res.json()).then(applySettings).catch(() => {}), SETTINGS_CHECK_MS);
  loadHistory();
  // Only the kiosk itself can change settings (see server.py).
  $("settings-open").hidden = !config?.canChange;
  initSettingsButton();
  $("controls-toggle").hidden = !config?.canChange;
  $("cast-open").hidden = !config?.canChange;
  loadCamera();
  setInterval(() => config?.camera?.type !== "webrtc" && loadCamera(), CAMERA_REFRESH_MS);
  setInterval(checkCamera, CAMERA_CHECK_MS);
  poll();
}

start();
