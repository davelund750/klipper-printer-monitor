// Setup wizard: country (and with it the Wi-Fi region), time zone, Wi-Fi,
// the printer's address, and the clock, all from the touchscreen, with a
// built-in keyboard (the desktop's own on-screen keyboard can't appear over the
// full-screen kiosk). The dashboard opens it:
//   - on first start, before setup is complete (all steps);
//   - with ?wifi when the printer keeps not answering because the kiosk
//     isn't online (just the Wi-Fi step);
//   - with ?settings from its settings button, once setup is done: a
//     tabbed Settings screen (Printer, Wi-Fi, Location, Display, About,
//     Reset), where each change is saved as soon as it's made;
//   - with ?printer from the "Can't reach the printer" banner: Settings,
//     open on the Printer tab.
// The /api/system endpoints it uses are in server.py and system.py.

const params = new URLSearchParams(location.search);
const kiosk = params.has("kiosk");
if (kiosk) document.body.classList.add("kiosk");
const dashboardUrl = kiosk ? "index.html?kiosk" : "index.html";

// Countries offered first; the rest are under "More countries".
const COMMON_COUNTRIES = ["US", "JP", "GB", "CA", "AU", "MX", "DE", "FR", "BR", "NZ", "IE", "ES"];
// Where clocks are usually 12-hour: the default picked when a country is
// chosen (it can be changed on the Display step).
const TWELVE_HOUR = new Set(["US", "CA", "AU", "NZ", "IN", "PH", "PK", "EG", "SA", "CO"]);

const els = {
  step: document.getElementById("step"),
  progress: document.getElementById("progress"),
  footer: document.getElementById("footer"),
  back: document.getElementById("back"),
  next: document.getElementById("next"),
  cancel: document.getElementById("cancel"),
  tabs: document.getElementById("tabs"),
};

const wizard = {
  steps: [],
  index: 0,
  countries: [],
  country: null, // "JP"
  timezone: null, // "Asia/Tokyo"
  clock: "12",
  previewLayout: "switch", // "switch" | "split": see the dashboard's renderPreview
  previewBackground: "auto", // "auto" | "a"…"e": see the dashboard's PREVIEW_SHADES
  theme: "standard", // "standard" | "lcars" | "klingon": see the dashboard's applyTheme
  language: "klingon", // "klingon" | "english": the Klingon look's words
  printerHost: "", // the address saved now (or the default)
  printerName: null, // the name saved now, if one has been
  status: {}, // /api/system/status
  // The countries and system status load in the background (the status is
  // slow on a Pi 3), so the first screen can show right away.
  loaded: false,
  loading: null, // a promise, while they load
  settingsMode: false, // the tabbed Settings screen, rather than the steps
};

async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// A country's flag, from its two letters (regional indicator symbols).
function flag(code) {
  return String.fromCodePoint(...[...code].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

function countryInfo(code) {
  return wizard.countries.find((c) => c.code === code);
}

// "America/Indiana/Indianapolis" -> "Indianapolis (Indiana)"
function zoneLabel(zone) {
  const parts = zone.split("/").slice(1).map((p) => p.replace(/_/g, " "));
  return parts.length > 1 ? `${parts.at(-1)} (${parts.slice(0, -1).join(", ")})` : parts[0] || zone;
}

function timeIn(zone, hour12) {
  try {
    return new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12, timeZone: zone });
  } catch (err) {
    return "";
  }
}

// ---- navigation ----

function setFooter({ next = "Next", nextEnabled = true, back = wizard.index > 0, hidden = false } = {}) {
  els.footer.hidden = hidden || wizard.settingsMode; // Settings saves each tap instead
  els.next.textContent = next;
  els.next.disabled = !nextEnabled;
  els.back.hidden = !back;
}

function renderProgress() {
  if (wizard.settingsMode) return renderSettingsTabs();
  els.progress.innerHTML = "";
  wizard.steps.forEach((step, i) => {
    const dot = el("span", "dot" + (i === wizard.index ? " current" : i < wizard.index ? " done" : ""));
    els.progress.appendChild(dot);
  });
}

// Draws a step. Redrawing the current one (after a tap) keeps the scroll
// position, so picking from a long list doesn't jump back to the top.
function showStep(index) {
  const scroll = index === wizard.index ? els.step.scrollTop : 0;
  wizard.index = index;
  renderProgress();
  els.step.innerHTML = "";
  // Every step but the welcome needs the background data; if someone gets
  // there first, show a moment's wait, then the step.
  if (wizard.steps[index] !== welcomeStep && !wizard.loaded) {
    const box = el("div", "setup-center");
    box.append(el("div", "setup-spinner"), el("p", "setup-text dim", params.has("wifi") ? "Checking the connection…" : "One moment…"));
    els.step.appendChild(box);
    setFooter({ hidden: true });
    wizard.loading.then(() => wizard.index === index && showStep(index));
    return;
  }
  wizard.steps[index].render();
  els.step.scrollTop = scroll;
}

const redraw = () => showStep(wizard.index);

async function goNext() {
  const step = wizard.steps[wizard.index];
  els.next.disabled = true;
  try {
    if (step.save) await step.save();
  } catch (err) {
    // Couldn't apply this step (e.g. no permission on this device). Say so,
    // but don't trap anyone in the wizard over it.
    showNotice("That setting couldn't be saved on this device, so it was skipped.");
  }
  let next = wizard.index + 1;
  while (next < wizard.steps.length && wizard.steps[next].skip && wizard.steps[next].skip()) next++;
  if (next < wizard.steps.length) showStep(next);
}

function goBack() {
  let prev = wizard.index - 1;
  while (prev > 0 && wizard.steps[prev].skip && wizard.steps[prev].skip()) prev--;
  if (prev >= 0) showStep(prev);
}

// Connected (or already were): move on in setup; in Settings, stay on the
// Wi-Fi tab with the updated list.
function wifiFinished() {
  if (wizard.settingsMode) redraw();
  else goNext();
}

// A choice was tapped. During setup it's saved when moving to the next step;
// in Settings it's saved right away.
async function changed(step) {
  redraw();
  if (!wizard.settingsMode) return;
  try {
    if (step === countryStep) {
      // The country can move the time zone (to one of its own), so save both.
      await step.save();
      await timezoneStep.save();
    } else {
      await step.save();
    }
    showNotice("Saved", "ok");
  } catch (err) {
    showNotice("That setting couldn't be saved on this device.");
  }
}

function showNotice(text, kind = "warn") {
  const notice = el("div", `setup-notice ${kind}`, text);
  document.body.appendChild(notice);
  setTimeout(() => notice.remove(), 4000);
}

async function finish() {
  await api("/api/settings", "PUT", { setupComplete: true }).catch(() => {});
  location.replace(dashboardUrl);
}

// ---- steps ----

const welcomeStep = {
  render() {
    const box = el("div", "setup-center");
    box.append(
      el("h1", "setup-title", "Welcome!"),
      el("p", "setup-text", "Let's set up your printer display. It takes about a minute."),
      el("p", "setup-text dim", "You'll pick your country and time zone, connect to Wi-Fi, then enter your printer's address.")
    );
    els.step.appendChild(box);
    setFooter({ next: "Get started", back: false });
  },
};

const countryStep = {
  showAll: false,
  render() {
    const pick = (code) => {
      if (code !== wizard.country) wizard.clock = TWELVE_HOUR.has(code) ? "12" : "24";
      wizard.country = code;
      fitTimezoneToCountry();
      changed(countryStep);
    };

    els.step.appendChild(el("h1", "setup-title", "Where is this display?"));
    els.step.appendChild(el("p", "setup-text dim", "This sets the Wi-Fi region, so it can find your network."));

    const grid = el("div", "choice-grid");
    const common = COMMON_COUNTRIES.map(countryInfo).filter(Boolean);
    const selected = countryInfo(wizard.country);
    if (selected && !common.includes(selected)) common.unshift(selected);
    for (const country of common) grid.appendChild(countryButton(country, pick));
    els.step.appendChild(grid);

    const toggle = el("button", "setup-link", countryStep.showAll ? "Fewer countries" : "More countries…");
    toggle.addEventListener("click", () => {
      countryStep.showAll = !countryStep.showAll;
      redraw();
    });
    els.step.appendChild(toggle);

    if (countryStep.showAll) {
      const list = el("div", "choice-list");
      for (const country of wizard.countries) list.appendChild(countryButton(country, pick));
      els.step.appendChild(list);
    }
    setFooter({ nextEnabled: !!wizard.country });
  },
  async save() {
    fitTimezoneToCountry();
    await api("/api/system/country", "PUT", { code: wizard.country });
    // One time zone (e.g. Japan): the time zone step is skipped, so set it here.
    if (timezoneStep.skip() && wizard.timezone) {
      await api("/api/system/timezone", "PUT", { timezone: wizard.timezone });
    }
  },
};

// Keeps the time zone one of the chosen country's (e.g. a Pi set up in the
// US and shipped to Japan must not keep New York time).
function fitTimezoneToCountry() {
  const zones = countryInfo(wizard.country)?.timezones || [];
  if (zones.length && !zones.includes(wizard.timezone)) wizard.timezone = zones[0];
}

function countryButton(country, pick) {
  const btn = el("button", "choice" + (country.code === wizard.country ? " selected" : ""));
  btn.append(el("span", "choice-flag", flag(country.code)), el("span", "choice-label", country.name));
  btn.addEventListener("click", () => pick(country.code));
  return btn;
}

const timezoneStep = {
  // Only asked when the country has more than one time zone.
  skip() {
    return (countryInfo(wizard.country)?.timezones || []).length <= 1;
  },
  render() {
    els.step.appendChild(el("h1", "setup-title", "Which time zone?"));
    const list = el("div", "choice-list");
    for (const zone of countryInfo(wizard.country)?.timezones || []) {
      const btn = el("button", "choice" + (zone === wizard.timezone ? " selected" : ""));
      btn.append(el("span", "choice-label", zoneLabel(zone)), el("span", "choice-meta", timeIn(zone, wizard.clock === "12")));
      btn.addEventListener("click", () => {
        wizard.timezone = zone;
        changed(timezoneStep);
      });
      list.appendChild(btn);
    }
    els.step.appendChild(list);
    setFooter({ nextEnabled: !!wizard.timezone });
  },
  async save() {
    await api("/api/system/timezone", "PUT", { timezone: wizard.timezone });
  },
};


// The printer: first its IP address, typed on a number pad. Saving checks
// that a printer answers there; if nothing does, it says so and offers to
// save it anyway (e.g. while the printer is switched off). Then its name, for
// the top of the dashboard, starting from the name the printer goes by.
const printerStep = {
  view: "address", // "address" | "name"
  draft: null, // the address being typed
  checking: false,
  result: null, // { ok, message } from the last check
  forceNext: false, // the last check found nothing: next save saves anyway
  suggested: null, // the name the printer goes by, from the last check
  nameDraft: null, // the name being typed

  render() {
    if (printerStep.view === "name") return printerName();
    if (printerStep.draft === null) printerStep.draft = wizard.printerHost;
    const extra = el("div");
    if (printerStep.checking) {
      extra.appendChild(el("p", "setup-text dim printer-result", `Looking for the printer at ${printerStep.draft}…`));
    } else if (printerStep.result) {
      const kind = printerStep.result.ok ? "ok" : "warn";
      extra.appendChild(el("p", `setup-text ${kind} printer-result`, printerStep.result.message));
    } else {
      extra.appendChild(el("p", "setup-text dim printer-result",
        "It's in the printer's network settings on its touchscreen, or in your router's list of devices."));
    }
    const actions = el("div", "setup-actions");
    if (!wizard.settingsMode && wizard.index > 0) {
      const back = el("button", "setup-link", "Back");
      back.addEventListener("click", goBack);
      actions.appendChild(back);
    }
    if (printerStep.forceNext) {
      const anyway = el("button", "setup-link", "Save anyway");
      anyway.addEventListener("click", () => savePrinter(true));
      actions.appendChild(anyway);
    }
    if (!wizard.settingsMode && printerStep.forceNext) {
      const skip = el("button", "setup-link", "Skip for now");
      skip.addEventListener("click", goNext);
      actions.appendChild(skip);
    }
    if (wizard.settingsMode && !printerStep.checking) {
      // In Settings, the name can be changed on its own.
      const rename = el("button", "setup-link", `Name: ${wizard.printerName || "Printer"} · Rename`);
      rename.addEventListener("click", () => showPrinterName());
      actions.appendChild(rename);
    }
    if (actions.children.length) extra.appendChild(actions);
    textEntry({
      title: "Printer address",
      value: printerStep.draft,
      secret: false,
      numeric: true,
      doneLabel: wizard.settingsMode ? "Save" : "Check",
      extra,
      onChange(text) {
        printerStep.draft = text.slice(0, 63);
        printerStep.result = null;
        printerStep.forceNext = false;
      },
      onDone: () => printerStep.draft && !printerStep.checking && savePrinter(false),
    });
  },
};

async function savePrinter(force) {
  printerStep.checking = true;
  redraw();
  let result;
  try {
    const res = await fetch("/api/printer", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ printerHost: printerStep.draft, force }),
    });
    result = await res.json();
  } catch (err) {
    result = { saved: false, ok: false, message: "Something went wrong. Please try again." };
  }
  printerStep.checking = false;
  printerStep.result = result;
  printerStep.forceNext = !result.saved && result.ok === false && !!result.message?.startsWith("Nothing answered");
  if (printerStep.forceNext) result.message += " Check the address, or save it anyway.";
  if (result.saved) {
    wizard.printerHost = printerStep.draft;
    if (!result.ok) result.message = `Saved ${printerStep.draft}.`;
    if (wizard.settingsMode) showNotice("Saved", "ok");
    // Then the name, starting from what this printer goes by.
    return showPrinterName(result.suggestedName);
  }
  redraw();
}

function showPrinterName(suggested) {
  if (suggested) printerStep.suggested = suggested;
  // A name already chosen is kept (say, the printer just moved to a new
  // address); otherwise start from the printer's own.
  printerStep.nameDraft = wizard.printerName || printerStep.suggested || "Printer";
  printerStep.view = "name";
  redraw();
}

function printerName() {
  // One row (buttons, then the hint), so the full keyboard still fits below.
  const actions = el("div", "setup-actions name-actions");
  const back = el("button", "setup-link", "Back");
  back.addEventListener("click", () => {
    printerStep.view = "address";
    redraw();
  });
  actions.appendChild(back);
  const suggested = printerStep.suggested;
  if (suggested && suggested !== printerStep.nameDraft) {
    const use = el("button", "setup-link", `Use "${suggested}"`);
    use.addEventListener("click", () => {
      printerStep.nameDraft = suggested;
      redraw();
    });
    actions.appendChild(use);
  }
  actions.appendChild(el("span", "setup-text dim", "Shown at the top of the dashboard."));
  textEntry({
    title: "Printer name",
    value: printerStep.nameDraft,
    secret: false,
    doneLabel: wizard.settingsMode ? "Save" : "Next",
    extra: actions,
    onChange: (text) => (printerStep.nameDraft = text.slice(0, 24)),
    onDone: savePrinterName,
  });
}

async function savePrinterName() {
  const name = printerStep.nameDraft.trim();
  if (!name) return;
  try {
    await api("/api/settings", "PUT", { printerName: name });
    wizard.printerName = name;
  } catch (err) {
    showNotice("The name couldn't be saved on this device.");
    return;
  }
  printerStep.view = "address";
  if (wizard.settingsMode) {
    showNotice("Saved", "ok");
    redraw();
  } else {
    goNext();
  }
}

const displayStep = {
  render() {
    els.step.appendChild(el("h1", "setup-title", "How should things look?"));
    const sample = new Date();
    sample.setHours(14, 30, 0, 0);
    const time = (hour12) => sample.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12 });

    // choices: [value, text, swatch colour (optional)]
    const option = (label, choices, current, onPick, note) => {
      const row = el("div", "setting-row");
      row.appendChild(el("div", "setting-label", label));
      const group = el("div", "segmented");
      for (const [value, text, swatch] of choices) {
        const btn = el("button", "segment" + (value === current ? " selected" : ""), text);
        if (swatch) {
          const chip = el("span", "swatch");
          chip.style.background = swatch;
          btn.prepend(chip);
        }
        btn.addEventListener("click", () => {
          onPick(value);
          changed(displayStep);
        });
        group.appendChild(btn);
      }
      row.appendChild(group);
      els.step.append(row, el("p", "setup-text dim", note));
    };
    option("Clock", [["12", `12-hour (${time(true)})`], ["24", `24-hour (${time(false)})`]], wizard.clock,
      (v) => (wizard.clock = v), "For the clock, and for when a print will finish.");
    option("Camera and layer preview", [["switch", "Switch"], ["split", "Split screen"]], wizard.previewLayout,
      (v) => (wizard.previewLayout = v),
      "While printing: one at a time, with a button to switch, or the camera on top and the layer preview below.");
    option("Layer preview background", [["auto", "Auto"], ["a", "A", "#161b22"], ["b", "B", "#1e242b"],
      ["c", "C", "#262c33"], ["d", "D", "#2f363e"], ["e", "E", "#3a414a"]], wizard.previewBackground,
      (v) => (wizard.previewBackground = v),
      "Auto: dark, or C when a filament (black, say) wouldn't show on dark. A to E: always that shade, darkest first.");
    option("Look", [["standard", "Standard"], ["lcars", "LCARS"], ["klingon", "Klingon"]], wizard.theme,
      (v) => (wizard.theme = v), "Star Trek panels, if you like: LCARS (Federation) or Klingon. Cast screens follow it.");
    if (wizard.theme === "klingon") {
      option("Words", [["klingon", "Klingon"], ["english", "English"]], wizard.language,
        (v) => (wizard.language = v), "The dashboard's labels and messages. Controls and Settings stay in English.");
    }
    setFooter();
  },
  async save() {
    await api("/api/settings", "PUT", {
      clock: wizard.clock, previewLayout: wizard.previewLayout, previewBackground: wizard.previewBackground,
      theme: wizard.theme, language: wizard.language,
    });
  },
};

const wifiStep = {
  view: "list", // "list" | "password" | "name" | "connecting"
  networks: null,
  chosen: null, // { ssid, secure, hidden }
  password: "",
  showPassword: false,
  error: "",

  render() {
    ({ list: wifiList, password: wifiPassword, name: wifiName, connecting: wifiConnecting })[wifiStep.view]();
  },
};

async function refreshStatus() {
  wizard.status = await api("/api/system/status").catch(() => wizard.status);
}

function wifiList() {
  const online = wizard.status.online;
  els.step.appendChild(el("h1", "setup-title", "Connect to Wi-Fi"));
  if (online && wizard.status.ssid) {
    els.step.appendChild(el("p", "setup-text ok", `✓ Connected to ${wizard.status.ssid}`));
  } else if (params.has("wifi")) {
    els.step.appendChild(el("p", "setup-text warn", "This display isn't connected to the internet."));
  }
  if (wifiStep.error) els.step.appendChild(el("p", "setup-text warn", wifiStep.error));

  const list = el("div", "choice-list");
  if (wifiStep.networks === null) {
    list.appendChild(el("div", "setup-text dim", "Looking for Wi-Fi networks…"));
    api("/api/system/networks")
      .then((networks) => (wifiStep.networks = networks))
      .catch(() => (wifiStep.networks = []))
      .then(() => wizard.steps[wizard.index] === wifiStep && wifiStep.view === "list" && redraw());
  } else {
    if (wifiStep.networks.length === 0) list.appendChild(el("div", "setup-text dim", "No networks found."));
    for (const net of wifiStep.networks) {
      const current = online && net.ssid === wizard.status.ssid;
      const btn = el("button", "choice" + (current ? " selected" : ""));
      btn.append(
        el("span", "choice-label", net.ssid),
        el("span", "choice-meta", `${net.saved ? "Saved  " : net.secure ? "🔒 " : ""}${signalBars(net.signal)}`)
      );
      btn.addEventListener("click", () =>
        chooseNetwork({ ssid: net.ssid, secure: net.secure, saved: net.saved, hidden: false })
      );
      list.appendChild(btn);
    }
  }
  els.step.appendChild(list);

  const actions = el("div", "setup-actions");
  const refresh = el("button", "setup-link", "Refresh");
  refresh.addEventListener("click", () => {
    wifiStep.networks = null;
    wifiStep.error = "";
    redraw();
  });
  const other = el("button", "setup-link", "Other network…");
  other.addEventListener("click", () => {
    wifiStep.chosen = { ssid: "", secure: true, hidden: true };
    wifiStep.view = "name";
    redraw();
  });
  actions.append(refresh, other);
  els.step.appendChild(actions);

  setFooter({ next: online ? "Continue" : "Skip for now" });
}

function signalBars(signal) {
  return signal >= 70 ? "▂▄▆█" : signal >= 45 ? "▂▄▆" : signal >= 20 ? "▂▄" : "▂";
}

function chooseNetwork(network) {
  wifiStep.chosen = network;
  wifiStep.password = "";
  wifiStep.showPassword = false;
  wifiStep.error = "";
  // Already on this network: nothing to do. (Reconnecting would try to
  // rewrite the network's saved settings, which can fail, e.g. for a network
  // set up by Raspberry Pi Imager.)
  if (wizard.status.online && network.ssid === wizard.status.ssid) {
    showNotice(`Already connected to ${network.ssid}.`, "ok");
    wifiFinished();
    return;
  }
  // A saved network connects with its saved settings (the server asks for a
  // password only if they don't work); a new, locked one needs a password.
  if (network.secure && !network.saved) {
    wifiStep.view = "password";
    redraw();
  } else {
    connect();
  }
}

// Text entry with the built-in keyboard: the network name (for a hidden
// network) or its password.
// numeric: a number pad (for the printer's address) beside the field, rather
// than a full keyboard under it, so the field's messages fit beside it too.
function textEntry({ title, value, secret, numeric = false, doneLabel, onChange, onDone, extra }) {
  const side = numeric ? el("div", "entry-side") : els.step;
  side.appendChild(el("h1", "setup-title small", title));
  const row = el("div", "entry-row");
  const field = el("div", "entry-field");
  const shown = secret && !wifiStep.showPassword ? "•".repeat(value.length) : value;
  field.append(el("span", "entry-text", shown), el("span", "entry-caret"));
  row.appendChild(field);
  if (secret) {
    const toggle = el("button", "setup-link", wifiStep.showPassword ? "Hide" : "Show");
    toggle.addEventListener("click", () => {
      wifiStep.showPassword = !wifiStep.showPassword;
      redraw();
    });
    row.appendChild(toggle);
  }
  side.appendChild(row);
  // (The Wi-Fi screens' error; the printer's messages come in `extra`.)
  if (wifiStep.error && !numeric) side.appendChild(el("p", "setup-text warn", wifiStep.error));
  if (extra) side.appendChild(extra);
  const board = keyboard({
    numeric,
    doneLabel,
    onKey(key) {
      onChange(key === "back" ? value.slice(0, -1) : value + key);
      redraw();
    },
    onDone,
  });
  if (numeric && !board.classList.contains("numeric")) {
    // Switched to letters: the full keyboard needs the whole width.
    els.step.append(side, board);
  } else if (numeric) {
    const split = el("div", "entry-split");
    split.append(side, board);
    els.step.appendChild(split);
  } else {
    els.step.appendChild(board);
  }
  setFooter({ hidden: true });
}

function wifiName() {
  const cancel = el("button", "setup-link", "Cancel");
  cancel.addEventListener("click", () => {
    wifiStep.view = "list";
    redraw();
  });
  textEntry({
    title: "Network name",
    value: wifiStep.chosen.ssid,
    secret: false,
    doneLabel: "Next",
    extra: cancel,
    onChange: (text) => (wifiStep.chosen.ssid = text.slice(0, 32)),
    onDone() {
      if (!wifiStep.chosen.ssid) return;
      wifiStep.view = "password";
      redraw();
    },
  });
}

function wifiPassword() {
  const actions = el("div", "setup-actions");
  const cancel = el("button", "setup-link", "Cancel");
  cancel.addEventListener("click", () => {
    wifiStep.view = "list";
    wifiStep.error = "";
    redraw();
  });
  actions.appendChild(cancel);
  if (wifiStep.chosen.hidden) {
    const none = el("button", "setup-link", "No password");
    none.addEventListener("click", () => {
      wifiStep.password = "";
      connect();
    });
    actions.appendChild(none);
  }
  textEntry({
    title: `Password for ${wifiStep.chosen.ssid}`,
    value: wifiStep.password,
    secret: true,
    doneLabel: "Connect",
    extra: actions,
    onChange: (text) => (wifiStep.password = text.slice(0, 63)),
    onDone() {
      if (wifiStep.password.length >= 8) connect();
      else {
        wifiStep.error = "Wi-Fi passwords are at least 8 characters.";
        redraw();
      }
    },
  });
}

function wifiConnecting() {
  const box = el("div", "setup-center");
  box.append(el("div", "setup-spinner"), el("p", "setup-text", `Connecting to ${wifiStep.chosen.ssid}…`));
  els.step.appendChild(box);
  setFooter({ hidden: true });
}

async function connect() {
  wifiStep.view = "connecting";
  redraw();
  let result;
  try {
    result = await api("/api/system/wifi", "PUT", {
      ssid: wifiStep.chosen.ssid,
      password: wifiStep.password,
      hidden: wifiStep.chosen.hidden,
    });
  } catch (err) {
    result = { ok: false, message: "Something went wrong. Please try again." };
  }
  if (result.ok) {
    await refreshStatus();
    wifiStep.view = "list";
    wifiStep.error = "";
    if (wizard.settingsMode) showNotice(`Connected to ${wifiStep.chosen.ssid}.`, "ok");
    wifiFinished();
  } else {
    wifiStep.error = result.message;
    wifiStep.view = wifiStep.chosen.secure ? "password" : "list";
    redraw();
  }
}

const doneStep = {
  updating: false,
  render() {
    const box = el("div", "setup-center");
    if (doneStep.updating) {
      box.append(
        el("div", "setup-spinner"),
        el("h1", "setup-title small", "Checking for updates…"),
        el("p", "setup-text", "The display will restart in about a minute and a half, then show the printer.")
      );
      els.step.appendChild(box);
      setFooter({ hidden: true });
      return;
    }
    box.appendChild(el("h1", "setup-title", "You're all set!"));
    if (wizard.status.online) {
      // Offer the latest version now, rather than at the next weekly update.
      box.append(
        el("p", "setup-text", "Check for updates and restart? It takes about a minute and a half."),
        el("p", "setup-text dim", "Otherwise it updates itself automatically once a week.")
      );
      const actions = el("div", "setup-actions");
      const update = el("button", "setup-btn primary", "Check for updates and restart");
      update.addEventListener("click", updateAndRestart);
      const start = el("button", "setup-btn secondary", "Start using it now");
      start.addEventListener("click", finish);
      actions.append(update, start);
      box.appendChild(actions);
      setFooter({ hidden: true });
    } else {
      box.appendChild(el("p", "setup-text", "Once it's online and the printer is on, its status will appear."));
      setFooter({ next: "Start" });
    }
    box.appendChild(el("p", "setup-text dim", "To change these settings later, press and hold the ⚙ button on the dashboard."));
    els.step.appendChild(box);
  },
  save: finish,
};

async function updateAndRestart() {
  // Mark setup done first, so the restart comes back to the dashboard.
  await api("/api/settings", "PUT", { setupComplete: true }).catch(() => {});
  doneStep.updating = true;
  redraw();
  const result = await api("/api/system/update", "PUT", {}).catch(() => ({ ok: false }));
  if (!result.ok) {
    showNotice("Couldn't check for updates now; it will update itself later.");
    setTimeout(finish, 3000);
  } else if (wizard.status.fake) {
    // Not a Pi: nothing will restart, so carry on to the dashboard.
    setTimeout(finish, 3000);
  }
}

// ---- Settings (after setup) ----

// Country and time zone, together on one tab.
const locationTab = {
  render() {
    countryStep.render();
    if (!timezoneStep.skip()) timezoneStep.render();
  },
};

// The version, a way to update now instead of waiting for Sunday, and what
// changed in each release (the installed copy's CHANGELOG.md).
const aboutTab = {
  changelog: null, // releases from /api/changelog, newest first, once loaded
  loading: null,
  open: new Set(), // releases shown expanded, by releaseKey()
  render() {
    if (doneStep.updating) {
      doneStep.render(); // the "Checking for updates…" screen
      return;
    }
    els.step.appendChild(el("h1", "setup-title", "Klipper Printer Monitor"));
    els.step.appendChild(el("p", "setup-text", `Version: ${wizard.status.version || "unknown"}`));
    els.step.appendChild(el("p", "setup-text dim", "It installs new versions by itself every Sunday at 4 AM, then restarts."));
    if (wizard.status.online) {
      const update = el("button", "setup-btn primary", "Check for updates and restart");
      update.addEventListener("click", updateAndRestart);
      els.step.appendChild(update);
    } else {
      els.step.appendChild(el("p", "setup-text dim", "Checking for updates needs an internet connection."));
    }
    renderWhatsNew();
  },
};

const releaseKey = (release) => release.version || `${release.date} ${release.label || ""}`;

// "2026-09-27" -> "September 27, 2026"
function formatReleaseDate(isoDate) {
  return new Date(`${isoDate}T12:00:00Z`).toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

// Each release as a row that opens to its changes; the newest starts open.
function renderWhatsNew() {
  els.step.appendChild(el("h2", "setup-title small whats-new-title", "What's new"));
  if (!aboutTab.changelog) {
    els.step.appendChild(el("p", "setup-text dim", "Loading…"));
    aboutTab.loading ||= api("/api/changelog")
      .catch(() => [])
      .then((releases) => {
        aboutTab.changelog = releases;
        if (releases.length) aboutTab.open.add(releaseKey(releases[0]));
        if (wizard.steps[wizard.index] === aboutTab) redraw();
      });
    return;
  }
  if (aboutTab.changelog.length === 0) {
    els.step.appendChild(el("p", "setup-text dim", "No list of changes is available."));
    return;
  }
  for (const release of aboutTab.changelog) {
    const key = releaseKey(release);
    const open = aboutTab.open.has(key);
    const toggle = el("button", "release-toggle");
    toggle.setAttribute("aria-expanded", String(open));
    const title = release.version
      ? `Version ${release.version}`
      : `Beta${release.label ? ` (${release.label})` : ""}`;
    toggle.appendChild(el("span", "release-name", `${open ? "▾" : "▸"} ${title}`));
    if (release.version && release.version === wizard.status.version) {
      toggle.appendChild(el("span", "release-installed", "Installed"));
    }
    toggle.appendChild(el("span", "release-date", formatReleaseDate(release.date)));
    toggle.addEventListener("click", () => {
      if (open) aboutTab.open.delete(key);
      else aboutTab.open.add(key);
      redraw();
    });
    els.step.appendChild(toggle);
    if (!open) continue;
    const details = el("div", "release-details");
    for (const note of release.notes) details.appendChild(el("p", "setup-text", note));
    for (const section of release.sections) {
      details.appendChild(el("h3", "release-section", section.title));
      const list = el("ul", "release-items");
      for (const item of section.items) list.appendChild(el("li", "", item));
      details.appendChild(list);
    }
    els.step.appendChild(details);
  }
}

// Factory reset: erases everything the user has set up, saved Wi-Fi networks
// included, so the kiosk starts over with first-time setup as if new (e.g.
// before giving it to someone else).
const resetTab = {
  confirming: false,
  render() {
    if (!resetTab.confirming) {
      els.step.appendChild(el("h1", "setup-title", "Factory reset"));
      els.step.appendChild(el("p", "setup-text", "Start over as if this display were brand new: the setup steps run again on the next screen."));
      els.step.appendChild(el("p", "setup-text dim", "Saved Wi-Fi networks are forgotten too."));
      const start = el("button", "setup-link danger", "Factory reset…");
      start.addEventListener("click", () => {
        resetTab.confirming = true;
        redraw();
      });
      els.step.appendChild(start);
      return;
    }
    els.step.appendChild(el("h1", "setup-title", "Erase everything?"));
    const list = el("ul", "setup-text reset-list");
    for (const item of [
      "The printer's address and name, and the clock choice",
      "Saved Wi-Fi networks: the display goes offline until it's set up again",
    ]) {
      list.appendChild(el("li", "", item));
    }
    els.step.appendChild(list);
    els.step.appendChild(el("p", "setup-text dim", "This can't be undone. Do it on the display itself: a remote connection to it will drop."));
    const actions = el("div", "setup-actions");
    const cancel = el("button", "setup-link", "Cancel");
    cancel.addEventListener("click", () => {
      resetTab.confirming = false;
      redraw();
    });
    const erase = el("button", "setup-link danger strong", "Erase everything");
    erase.addEventListener("click", factoryReset);
    actions.append(cancel, erase);
    els.step.appendChild(actions);
  },
};

async function factoryReset() {
  try {
    await api("/api/system/reset", "PUT", {});
  } catch (err) {
    showNotice("The reset couldn't be done on this device.");
    return;
  }
  // Anything this browser remembers too.
  try {
    localStorage.clear();
  } catch (err) {
    // Nothing saved here, or storage is off; nothing to clear.
  }
  // The dashboard sees setup isn't done and starts first-time setup.
  location.replace(dashboardUrl);
}

const SETTINGS_TABS = [
  { label: "Printer", step: printerStep },
  { label: "Wi-Fi", step: wifiStep },
  { label: "Location", step: locationTab },
  { label: "Display", step: displayStep },
  { label: "About", step: aboutTab },
  { label: "Reset", step: resetTab },
];

function renderSettingsTabs() {
  els.tabs.innerHTML = "";
  SETTINGS_TABS.forEach((tab, i) => {
    const btn = el("button", "settings-tab" + (i === wizard.index ? " selected" : ""), tab.label);
    btn.addEventListener("click", () => {
      if (tab.step === wifiStep) wifiStep.view = "list";
      if (tab.step === printerStep) Object.assign(printerStep, { view: "address", draft: null, result: null, forceNext: false });
      resetTab.confirming = false;
      showStep(i);
    });
    els.tabs.appendChild(btn);
  });
}

// ---- keyboard ----

const KEY_LAYOUTS = {
  lower: [
    ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
    ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
    ["shift", "z", "x", "c", "v", "b", "n", "m", "back"],
    ["123", "space", "done"],
  ],
  symbols: [
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
    ["-", "/", ":", ";", "(", ")", "$", "&", "@", '"'],
    ["more", ".", ",", "?", "!", "'", "_", "back"],
    ["abc", "space", "done"],
  ],
  more: [
    ["[", "]", "{", "}", "#", "%", "^", "*", "+", "="],
    ["\\", "|", "~", "<", ">", "€", "£", "¥", "•"],
    ["123", ".", ",", "?", "!", "'", "`", "back"],
    ["abc", "space", "done"],
  ],
};
// For an IP address. "abc" switches to letters, for a host name such as
// k2pro.local.
const NUMERIC_LAYOUT = [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"], [".", "0", "back"], ["abc", "done"]];
const keyboardState = { layout: "lower", shift: false, letters: false };

// A full-width on-screen keyboard. Shift applies to the next letter only.
function keyboard({ numeric = false, doneLabel, onKey, onDone }) {
  const pad = numeric && !keyboardState.letters;
  const board = el("div", "keyboard" + (pad ? " numeric" : ""));
  const rows = pad ? NUMERIC_LAYOUT : KEY_LAYOUTS[keyboardState.layout];
  for (const row of rows) {
    const rowEl = el("div", "key-row");
    for (const key of row) {
      const special = { shift: "⇧", back: "⌫", space: "space", "123": "123", more: "#+=", abc: "ABC", done: doneLabel }[key];
      const letter = keyboardState.shift && key.length === 1 ? key.toUpperCase() : key;
      const btn = el("button", `key key-${special ? key : "char"}` + (key === "shift" && keyboardState.shift ? " on" : ""), special || letter);
      btn.addEventListener("click", () => {
        if (key === "shift") keyboardState.shift = !keyboardState.shift;
        else if (key === "123" && numeric) keyboardState.letters = false;
        else if (key === "123") keyboardState.layout = "symbols";
        else if (key === "more") keyboardState.layout = "more";
        else if (key === "abc" && pad) keyboardState.letters = true;
        else if (key === "abc") keyboardState.layout = "lower";
        else if (key === "done") return onDone();
        else if (key === "back") return onKey("back");
        else {
          // Shift is for one letter. Turn it off before onKey redraws the
          // keyboard, or the redrawn keys would still show it on.
          keyboardState.shift = false;
          onKey(key === "space" ? " " : letter);
          return;
        }
        redraw();
      });
      rowEl.appendChild(btn);
    }
    board.appendChild(rowEl);
  }
  return board;
}

// ---- start ----

// Loads the countries and the Pi's current settings, and fills in the
// wizard's starting choices from them.
async function loadSystemData(settings) {
  const [countries, config] = await Promise.all([
    api("/api/system/countries").catch(() => []),
    api("/api/config").catch(() => ({})),
    refreshStatus(),
  ]);
  wizard.countries = countries;
  wizard.country = wizard.status.country || null;
  wizard.timezone = wizard.status.timezone || null;
  wizard.clock = settings.clock || (TWELVE_HOUR.has(wizard.country) ? "12" : "24");
  wizard.previewLayout = settings.previewLayout || "switch";
  wizard.previewBackground = settings.previewBackground || "auto";
  wizard.theme = settings.theme || "standard";
  wizard.language = settings.language || "klingon";
  wizard.printerHost = config.printerHost || "";
  wizard.printerName = settings.printerName || null;
  wizard.loaded = true;
}

async function start() {
  // Only the saved settings (a quick file read) are needed to pick the first
  // screen; everything else loads while it's showing.
  const settings = await api("/api/settings").catch(() => ({}));
  wizard.loading = loadSystemData(settings);

  if (params.has("wifi")) {
    // Offline: just get connected, then back to the dashboard.
    wizard.steps = [wifiStep, { render: () => finish() }];
  } else {
    wizard.steps = [welcomeStep, countryStep, timezoneStep, wifiStep, printerStep, displayStep, doneStep];
    if ((params.has("settings") || params.has("printer")) && settings.setupComplete) {
      // Already set up: a tabbed Settings screen, to change one thing at a time.
      wizard.settingsMode = true;
      wizard.steps = SETTINGS_TABS.map((tab) => tab.step);
      els.progress.hidden = true;
      els.tabs.hidden = false;
      els.cancel.textContent = "Done";
      els.cancel.hidden = false;
      els.cancel.addEventListener("click", () => location.replace(dashboardUrl));
    }
  }
  // Swipes on the kiosk's touchscreen arrive as mouse drags (see dragscroll.js).
  enableDragScroll(els.step);
  els.next.addEventListener("click", goNext);
  els.back.addEventListener("click", goBack);
  showStep(0);
}

start();
