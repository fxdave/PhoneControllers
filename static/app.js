"use strict";

// The keys keep the project's old name so phones keep their layouts.
const SETTINGS_KEY = "phone-wheel-settings";
const SETTINGS_VERSION = 2;
// The PC's settings as last received, so the page starts right even before it connects.
const SHARED_KEY = "phone-wheel-shared";
// Ignore touches this close to the wheel center: the angle is too jumpy there.
const DEAD_RADIUS_FRACTION = 0.12;
const PEDAL_KEYS = ["c", "b", "t"];

// What only this phone decides: which controls it shows, where they sit, vibration and the
// tilt neutral. Everything else comes from the PC (see phonecontrollers/config.py).
const settings = loadSettings();
let shared = loadShared();
let curves = buildCurves();

const state = {
  angle: 0,          // degrees, cumulative (can be many turns)
  pedals: { t: 0, b: 0, c: 0 },   // what each pedal shows, after its input curve
  raw: { s: 0, t: 0, b: 0, c: 0 }, // the input before the curve, for the PC's curve editor
  buttons: 0,
};

// From the PC: force feedback and the game's own view of the pedals (OutGauge).
const feedback = { force: 0, spring: 0, rumble: 0 };
const game = { packet: null, time: 0 };
const GAME_STALE_MS = 1000;

const deck = document.getElementById("deck");
const wheelZone = document.getElementById("wheel-zone");
const wheelEl = document.getElementById("wheel");
const wheelSvg = wheelEl.querySelector("svg.rotor");
const forceArc = document.getElementById("force-arc");
const hubLabel = document.getElementById("hub-mode");
const angleReadout = document.getElementById("angle-readout");
const gameReadout = document.getElementById("game-readout");
const statusEl = document.getElementById("status");

// ---------- settings ----------

function newDeviceId() {
  return Math.random().toString(36).slice(2, 10);
}

function loadSettings() {
  const defaults = {
    // Controls shown on this phone. Another phone can handle the rest. The shift lever does
    // what the paddles do, so the paddles start hidden.
    controls: {
      wheel: true, paddles: false, lever: true, shifter: true, buttons: true,
      c: true, b: true, t: true,
    },
    haptics: true,
    // { pitch, roll } captured with "Set neutral", kept per orientation since holding the
    // phone upright and sideways are different poses.
    neutrals: { portrait: null, landscape: null },
    // Customized widget rectangles per orientation, { widgetId: [x, y, width, height] } in
    // percent of the control area. No entry means the automatic layout.
    layouts: { portrait: null, landscape: null },
    // Camera pedal light levels (0..1) with the lens uncovered and fully covered (null
    // means measured automatically), the chosen lens (null is the default back camera) and
    // how much the reading is smoothed (0..1).
    camera: { open: null, covered: null, lens: null, smoothing: 0.5 },
    // Buttons (by number) removed one by one in the layout editor.
    hiddenButtons: [],
    deviceId: newDeviceId(),
    // Bumped when a default changes in a way stored settings should pick up.
    version: SETTINGS_VERSION,
  };
  try {
    const stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    // Earlier versions were landscape only and had a single neutral.
    if (stored.neutral && !stored.neutrals) stored.neutrals = { landscape: stored.neutral };
    // Before version 2 the paddles showed next to the shift lever by default.
    if (stored.controls && !stored.version && stored.controls.shifter !== false) {
      stored.controls.paddles = false;
    }
    // Older versions also stored the wheel, pedal and curve settings now kept on the PC.
    return {
      version: SETTINGS_VERSION,
      controls: { ...defaults.controls, ...stored.controls },
      haptics: stored.haptics ?? defaults.haptics,
      neutrals: { ...defaults.neutrals, ...stored.neutrals },
      layouts: { ...defaults.layouts, ...stored.layouts },
      camera: cameraSettings({ ...defaults.camera, ...stored.camera }),
      hiddenButtons: Array.isArray(stored.hiddenButtons) ? stored.hiddenButtons : [],
      deviceId: typeof stored.deviceId === "string" ? stored.deviceId : defaults.deviceId,
    };
  } catch {
    return defaults;
  }
}

function cameraSettings(camera) {
  const level = (value) => (Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : null);
  return {
    open: level(camera.open),
    covered: level(camera.covered),
    lens: typeof camera.lens === "string" && camera.lens ? camera.lens : null,
    smoothing: level(camera.smoothing) ?? 0.5,
  };
}

function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // Private mode or blocked storage: settings just won't persist.
  }
}

// Mirrors DEFAULTS in phonecontrollers/config.py, for a phone that never reached the PC yet.
function sharedDefaults() {
  const linear = [[0, 0], [1, 1]];
  return {
    wheel: {
      range: 900, autocenter: false, steerMode: "touch", gyroLock: 60,
      tiltSteerSpeed: 540, tiltSteerFull: 30, tiltSteerDeadzone: 5,
    },
    pedals: {
      modes: { c: "touch", b: "touch", t: "touch" },
      tiltFull: 25, tiltDeadzone: 3, clutchReturn: 0.5,
    },
    curves: {
      pre: { s: linear, t: linear, b: linear, c: linear },
      post: {
        s: linear, t: linear, b: linear,
        c: Array.from({ length: 13 }, (_, i) => [i / 12, 1 - (1 - i / 12) ** 4]),
      },
    },
    ffb: { enabled: true, gain: 1, invert: false, pull: true, pullSpeed: 720, vibrate: true },
    buttons: [
      "Shift −", "Shift +", "Handbrake", "Horn", "Lights", "Camera",
      "Look back", "Wipers", "Ignition", "Hazard", "Map", "Pause",
      "Left signal", "Right signal",
    ],
    lever: { mode: "pulse", autoCancel: true },
    shifter: { upShifts: true },
    look: { enabled: false, gain: 3, deadzone: 5, invert: false },
  };
}

function loadShared() {
  try {
    const stored = JSON.parse(localStorage.getItem(SHARED_KEY) || "null");
    if (stored && stored.wheel && stored.pedals && stored.curves) {
      return { ...sharedDefaults(), ...stored };
    }
  } catch {
    // Nothing stored or blocked storage.
  }
  return sharedDefaults();
}

function applyShared(config) {
  // A PC running an older version may leave out newer sections.
  shared = { ...sharedDefaults(), ...config };
  curves = buildCurves();
  try {
    localStorage.setItem(SHARED_KEY, JSON.stringify(config));
  } catch {
    // Blocked storage: the page starts with defaults until it connects.
  }
  state.angle = clampAngle(state.angle);
  applyButtonLabels();
  syncLayout();
  syncSettingsForm();
}

function buildCurves() {
  const build = (points) => PhoneControllersCurves.build(points);
  const result = { pre: {}, post: {} };
  for (const key of ["s", ...PEDAL_KEYS]) {
    result.pre[key] = build(shared.curves.pre[key]);
    result.post[key] = build(shared.curves.post[key]);
  }
  return result;
}

function halfRange() {
  return shared.wheel.range / 2;
}

// ---------- steering wheel ----------

const grab = { pointerId: null, lastAngle: 0 };

function pointerAngle(event) {
  const rect = wheelEl.getBoundingClientRect();
  const dx = event.clientX - (rect.left + rect.width / 2);
  const dy = event.clientY - (rect.top + rect.height / 2);
  const radius = Math.min(rect.width, rect.height) / 2;
  const tooClose = Math.hypot(dx, dy) < radius * DEAD_RADIUS_FRACTION;
  return { angle: (Math.atan2(dy, dx) * 180) / Math.PI, tooClose };
}

// The hub of the wheel is a switch between touch and gyro steering.
const HUB_RADIUS = 0.26; // of the wheel's radius
const hub = { pointerId: null };

function isOnHub(event) {
  const rect = wheelEl.getBoundingClientRect();
  const dx = event.clientX - (rect.left + rect.width / 2);
  const dy = event.clientY - (rect.top + rect.height / 2);
  return Math.hypot(dx, dy) < (Math.min(rect.width, rect.height) / 2) * HUB_RADIUS;
}

function toggleSteerMode() {
  const next = shared.wheel.steerMode === "gyro" ? "touch" : "gyro";
  if (next === "gyro") {
    requestMotionPermission();
    // The way the phone is held right now counts as straight.
    setNeutral();
  }
  grab.pointerId = null;
  wheelEl.classList.remove("grabbed");
  // Switch at once; the PC stores it and sends the settings back to every phone.
  shared.wheel.steerMode = next;
  send({ set: { path: ["wheel", "steerMode"], value: next } });
  buzz(40);
  syncLayout();
}

function showHub() {
  const text = shared.wheel.steerMode === "gyro" ? "Gyro" : "Touch";
  if (hubLabel.textContent !== text) hubLabel.textContent = text;
}

function clampAngle(angle) {
  const limit = halfRange();
  return Math.max(-limit, Math.min(limit, angle));
}

wheelZone.addEventListener("pointerdown", (event) => {
  if (grab.pointerId !== null || event.target.closest(".paddle, .stalk")) return;
  if (!settings.controls.wheel) return;
  if (hub.pointerId === null && isOnHub(event)) {
    hub.pointerId = event.pointerId;
    wheelEl.classList.add("hub-pressed");
    (deck.classList.contains("custom") ? wheelEl : wheelZone).setPointerCapture(event.pointerId);
    event.preventDefault();
    return;
  }
  // In gyro mode the on-screen wheel only shows the angle.
  if (shared.wheel.steerMode === "gyro") return;
  grab.pointerId = event.pointerId;
  grab.lastAngle = pointerAngle(event).angle;
  // A customized layout lays the zone out as nothing but its children, so hold on to the
  // wheel itself there.
  (deck.classList.contains("custom") ? wheelEl : wheelZone).setPointerCapture(event.pointerId);
  wheelEl.classList.add("grabbed");
  event.preventDefault();
});

wheelZone.addEventListener("pointermove", (event) => {
  if (event.pointerId !== grab.pointerId) return;
  const { angle, tooClose } = pointerAngle(event);
  if (!tooClose) {
    // Unwrap across the ±180° seam so turning past a full circle keeps accumulating.
    let delta = angle - grab.lastAngle;
    if (delta > 180) delta -= 360;
    if (delta < -180) delta += 360;
    const before = state.angle;
    state.angle = clampAngle(state.angle + delta);
    if (before !== state.angle && Math.abs(state.angle) === halfRange()) buzz(40);
  }
  grab.lastAngle = angle;
});

function releaseHub(event) {
  if (event.pointerId !== hub.pointerId) return false;
  hub.pointerId = null;
  wheelEl.classList.remove("hub-pressed");
  if (event.type === "pointerup" && isOnHub(event)) toggleSteerMode();
  return true;
}

function releaseWheel(event) {
  if (releaseHub(event)) return;
  if (event.pointerId !== grab.pointerId) return;
  grab.pointerId = null;
  wheelEl.classList.remove("grabbed");
}
wheelZone.addEventListener("pointerup", releaseWheel);
wheelZone.addEventListener("pointercancel", releaseWheel);

document.getElementById("recenter").addEventListener("click", () => {
  state.angle = 0;
  centerLook();
});

function springBack(dt) {
  if (!shared.wheel.autocenter || grab.pointerId !== null || state.angle === 0) return;
  const step = Math.max(Math.abs(state.angle) * 6 * dt, 180 * dt);
  state.angle = Math.abs(state.angle) <= step ? 0 : state.angle - Math.sign(state.angle) * step;
}

// A released wheel follows the game's force feedback: its steering force turns it, its
// centering spring pulls it back.
function followForce(dt) {
  const { ffb } = shared;
  if (!ffb.enabled || !ffb.pull || grab.pointerId !== null) return;
  if (!feedback.force && !feedback.spring) return;
  const centering = feedback.spring * (state.angle / halfRange());
  state.angle = clampAngle(state.angle + (feedback.force - centering) * ffb.pullSpeed * dt);
}

function showForce() {
  const force = shared.ffb.enabled ? feedback.force : 0;
  if (Math.abs(force) < 0.02) {
    forceArc.setAttribute("d", "");
    return;
  }
  // An arc along the top of the rim, growing towards the side the game pushes.
  const sweep = force * 90;
  const radians = ((sweep - 90) * Math.PI) / 180;
  const x = (96 * Math.cos(radians)).toFixed(2);
  const y = (96 * Math.sin(radians)).toFixed(2);
  forceArc.setAttribute("d", `M 0 -96 A 96 96 0 0 ${force > 0 ? 1 : 0} ${x} ${y}`);
}

// ---------- pedals ----------

const pedalEls = {};

// Sets what a pedal shows, from its input before the curve.
function setPedalInput(key, input) {
  const raw = Math.max(0, Math.min(1, input));
  state.raw[key] = raw;
  showPedal(key, curves.pre[key](raw));
}

function showPedal(key, value) {
  state.pedals[key] = Math.max(0, Math.min(1, value));
  pedalEls[key].querySelector(".fill").style.height = `${state.pedals[key] * 100}%`;
}

// Pedals the phone drives from a sensor (tilt or camera) only show their value.
function isSensorPedal(key) {
  return shared.pedals.modes[key] !== "touch";
}

function isTiltPedal(key) {
  return shared.pedals.modes[key] in TILT_DIRECTION;
}

for (const pedal of document.querySelectorAll(".pedal")) {
  const key = pedal.dataset.pedal;
  pedalEls[key] = pedal;
  let activePointer = null;

  const update = (event) => {
    const rect = pedal.getBoundingClientRect();
    setPedalInput(key, 1 - (event.clientY - rect.top) / rect.height);
  };
  const release = (event) => {
    if (event.pointerId !== activePointer) return;
    activePointer = null;
    state.raw[key] = 0;
    // Lifting off the clutch lets it come up gradually (see returnClutch) instead of dumping it.
    if (key !== "c" || shared.pedals.clutchReturn <= 0) showPedal(key, 0);
  };

  pedal.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    // A tilt pedal is only an indicator; touching it does nothing.
    if (activePointer !== null || isSensorPedal(key)) return;
    activePointer = event.pointerId;
    pedal.setPointerCapture(event.pointerId);
    update(event);
  });
  pedal.addEventListener("pointermove", (event) => {
    if (event.pointerId === activePointer) update(event);
  });
  pedal.addEventListener("pointerup", release);
  pedal.addEventListener("pointercancel", release);
  pedal.resetPedal = () => {
    activePointer = null;
    setPedalInput(key, 0);
  };
  pedal.isHeld = () => activePointer !== null;
}

function returnClutch(dt) {
  if (isSensorPedal("c") || pedalEls.c.isHeld() || state.pedals.c === 0) return;
  const { clutchReturn } = shared.pedals;
  showPedal("c", clutchReturn > 0 ? state.pedals.c - dt / clutchReturn : 0);
}

// Thin marks on each pedal: what the PC sends the game (after the output curve) and, with
// OutGauge telemetry, what the game itself reports.
function showPedalMarks() {
  const live = game.packet && performance.now() - game.time < GAME_STALE_MS;
  for (const key of PEDAL_KEYS) {
    const pedal = pedalEls[key];
    const output = curves.post[key](state.pedals[key]);
    pedal.querySelector(".out").style.bottom = `${output * 100}%`;
    const gameMark = pedal.querySelector(".game");
    gameMark.hidden = !live;
    if (live) gameMark.style.bottom = `${game.packet[key] * 100}%`;
  }
}

function usesTilt() {
  const { controls } = settings;
  return (controls.wheel && shared.wheel.steerMode !== "touch") ||
    PEDAL_KEYS.some((key) => controls[key] && isTiltPedal(key));
}

function syncLayout() {
  const { controls } = settings;
  const controller = document.getElementById("controller");
  const pedals = document.getElementById("pedals");
  for (const key of PEDAL_KEYS) {
    pedalEls[key].classList.toggle("tilt", isSensorPedal(key));
    pedalEls[key].querySelector("em").textContent =
      shared.pedals.modes[key] === "camera" ? "camera" : "tilt";
    pedalEls[key].hidden = !controls[key];
  }
  const pedalCount = PEDAL_KEYS.filter((key) => controls[key]).length;
  pedals.hidden = pedalCount === 0;
  pedals.style.setProperty("--pedal-count", pedalCount);
  wheelEl.hidden = !controls.wheel;
  for (const button of document.querySelectorAll("[data-button]")) {
    button.hidden = !widgetShown(`button-${button.dataset.button}`);
  }
  leverEl.hidden = !controls.lever;
  shifterEl.hidden = !controls.shifter;
  wheelZone.hidden = !controls.wheel && !controls.paddles && !controls.lever &&
    !controls.shifter;
  wheelZone.classList.toggle("no-wheel", !controls.wheel);
  // Stalks beside the wheel: shrink the wheel so they don't sit on its rim.
  wheelZone.classList.toggle("has-stalks", controls.wheel && (controls.lever || controls.shifter));
  document.getElementById("buttons").hidden = !controls.buttons;
  controller.classList.toggle("wide-buttons", wheelZone.hidden);
  const anyControl = Object.values(controls).some(Boolean);
  deck.hidden = !anyControl;
  document.getElementById("no-controls").hidden = anyControl;
  const gyro = controls.wheel && shared.wheel.steerMode === "gyro";
  controller.classList.toggle("gyro", gyro);
  angleReadout.hidden = !controls.wheel;
  document.getElementById("recenter").hidden = !controls.wheel || gyro;
  document.getElementById("level").hidden = !usesTilt();
  syncCamera();
  applyLayout();
}

// ---------- layout ----------

// Removing a whole control (the wheel, a lever, a pedal) turns it off for this phone, so the
// PC takes it from another phone. Buttons can also be removed one by one.
const WIDGET_CONTROL = {
  wheel: "wheel", lever: "lever", shifter: "shifter", "pedal-c": "c", "pedal-b": "b", "pedal-t": "t",
};

function buttonGroup(index) {
  return index < 2 ? "paddles" : "buttons";
}

function widgetShown(id) {
  if (WIDGET_CONTROL[id]) return settings.controls[WIDGET_CONTROL[id]];
  const index = Number(id.slice(7));
  return settings.controls[buttonGroup(index)] && !settings.hiddenButtons.includes(index);
}

function buttonIndexes() {
  return [...document.querySelectorAll("[data-button]")].map((button) => Number(button.dataset.button));
}

function groupIndexes(group) {
  return buttonIndexes().filter((index) => buttonGroup(index) === group);
}

function setWidgetShown(id, shown) {
  if (WIDGET_CONTROL[id]) {
    settings.controls[WIDGET_CONTROL[id]] = shown;
    if (id === "wheel" && !shown) state.angle = 0;
  } else {
    const index = Number(id.slice(7));
    const group = buttonGroup(index);
    const members = groupIndexes(group);
    if (shown && !settings.controls[group]) {
      // The group was off: bring back just this button.
      settings.controls[group] = true;
      settings.hiddenButtons = [
        ...settings.hiddenButtons.filter((i) => !members.includes(i)),
        ...members.filter((i) => i !== index),
      ];
    } else if (shown) {
      settings.hiddenButtons = settings.hiddenButtons.filter((i) => i !== index);
    } else {
      settings.hiddenButtons = [...new Set([...settings.hiddenButtons, index])];
      // The last one gone turns the group off.
      if (members.every((i) => settings.hiddenButtons.includes(i))) {
        settings.controls[group] = false;
        settings.hiddenButtons = settings.hiddenButtons.filter((i) => !members.includes(i));
      }
    }
  }
  releaseEverything();
  syncLayout();
  syncSettingsForm();
  saveSettings();
  sendHello();
}

// Every piece of the controller that can be moved and resized on its own.
const widgets = {};
widgets.wheel = wheelEl;
widgets.lever = document.getElementById("lever");
widgets.shifter = document.getElementById("shifter");
for (const button of document.querySelectorAll("[data-button]")) {
  widgets[`button-${button.dataset.button}`] = button;
}
for (const key of PEDAL_KEYS) widgets[`pedal-${key}`] = pedalEls[key];

const editor = {
  active: false,
  orientation: null,
  draft: null,
  drag: null,
  boxes: {},
};
const editorEl = document.getElementById("layout-editor");
const MIN_WIDGET_SIZE = 5; // percent of the control area

function isShown(element) {
  return element.offsetParent !== null || element.getClientRects().length > 0;
}

// Where the automatic layout puts each visible widget, in percent of the control area.
function measureWidgets() {
  const area = deck.getBoundingClientRect();
  const rects = {};
  if (!area.width || !area.height) return rects;
  for (const [id, element] of Object.entries(widgets)) {
    if (!isShown(element)) continue;
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    rects[id] = [
      ((rect.left - area.left) / area.width) * 100,
      ((rect.top - area.top) / area.height) * 100,
      (rect.width / area.width) * 100,
      (rect.height / area.height) * 100,
    ].map((value) => Math.round(value * 10) / 10);
  }
  return rects;
}

function placeElement(element, [x, y, width, height]) {
  element.style.left = `${x}%`;
  element.style.top = `${y}%`;
  element.style.width = `${width}%`;
  element.style.height = `${height}%`;
}

function applyLayout() {
  const saved = settings.layouts[orientationKind()];
  deck.classList.remove("custom");
  for (const element of Object.values(widgets)) {
    element.style.left = element.style.top = element.style.width = element.style.height = "";
  }
  if (!saved && !editor.active) return;
  // Widgets without a saved place (turned on after customizing) keep their automatic one.
  const measured = measureWidgets();
  if (editor.active && !editor.draft) editor.draft = { ...measured, ...saved };
  // A widget put back while editing starts where the automatic layout has it.
  if (editor.active) {
    for (const [id, rect] of Object.entries(measured)) editor.draft[id] ??= rect;
  }
  const rects = editor.active ? { ...measured, ...editor.draft } : { ...measured, ...saved };
  deck.classList.add("custom");
  for (const [id, element] of Object.entries(widgets)) {
    if (rects[id]) placeElement(element, rects[id]);
  }
  if (editor.active) renderEditor(rects);
}

function widgetLabel(id) {
  if (id === "wheel") return "Wheel";
  if (id === "lever") return "Signal lever";
  if (id === "shifter") return "Shift lever";
  if (id.startsWith("pedal-")) return { c: "Clutch", b: "Brake", t: "Gas" }[id.slice(6)];
  return shared.buttons[Number(id.slice(7))] || id;
}

function renderEditor(rects) {
  editorEl.replaceChildren();
  editor.boxes = {};
  for (const [id, element] of Object.entries(widgets)) {
    if (!rects[id] || !isShown(element)) continue;
    const box = document.createElement("div");
    box.className = "edit-box";
    box.dataset.widget = id;
    box.innerHTML = `<span></span><button class="edit-delete" type="button"
      aria-label="Remove">×</button><i class="edit-handle"></i>`;
    // Buttons, pedals and the levers' arrows already say what they are; label the wheel.
    if (id === "wheel") {
      box.querySelector("span").textContent = widgetLabel(id);
    } else {
      box.querySelector("span").remove();
    }
    placeElement(box, rects[id]);
    editorEl.append(box);
    editor.boxes[id] = box;
  }
  renderTray();
}

// Removed widgets, to put back.
function renderTray() {
  const tray = document.getElementById("edit-tray");
  const removed = Object.keys(widgets).filter((id) => !widgetShown(id));
  document.getElementById("edit-add").hidden = removed.length === 0;
  if (!removed.length) tray.hidden = true;
  tray.replaceChildren(...removed.map((id) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "tool";
    chip.textContent = `+ ${widgetLabel(id)}`;
    chip.addEventListener("click", () => setWidgetShown(id, true));
    return chip;
  }));
}
document.getElementById("edit-add").addEventListener("click", () => {
  const tray = document.getElementById("edit-tray");
  tray.hidden = !tray.hidden;
});

function startEditing() {
  dialog.close();
  releaseEverything();
  editor.active = true;
  editor.orientation = orientationKind();
  editor.draft = null;
  // Cancel puts removed widgets back too.
  editor.before = {
    controls: { ...settings.controls },
    hiddenButtons: [...settings.hiddenButtons],
  };
  document.getElementById("controller").classList.add("editing");
  editorEl.hidden = false;
  applyLayout();
}

function stopEditing(save) {
  if (!editor.active) return;
  if (save && editor.draft) {
    settings.layouts[editor.orientation] = editor.draft;
    saveSettings();
  } else if (!save && editor.before) {
    settings.controls = editor.before.controls;
    settings.hiddenButtons = editor.before.hiddenButtons;
    saveSettings();
    sendHello();
  }
  editor.before = null;
  document.getElementById("edit-tray").hidden = true;
  editor.active = false;
  editor.draft = null;
  editor.drag = null;
  document.getElementById("controller").classList.remove("editing");
  editorEl.hidden = true;
  editorEl.replaceChildren();
  syncLayout();
  syncSettingsForm();
}

function snap(value) {
  return Math.round(value * 2) / 2; // half a percent
}

editorEl.addEventListener("pointerdown", (event) => {
  const box = event.target.closest(".edit-box");
  if (!box || editor.drag) return;
  event.preventDefault();
  const id = box.dataset.widget;
  // The box last touched comes to the front, so overlapping widgets can all be reached.
  for (const other of Object.values(editor.boxes)) other.classList.toggle("front", other === box);
  if (event.target.closest(".edit-delete")) {
    setWidgetShown(id, false);
    return;
  }
  editor.drag = {
    id,
    pointerId: event.pointerId,
    resize: Boolean(event.target.closest(".edit-handle")),
    startX: event.clientX,
    startY: event.clientY,
    start: [...editor.draft[id]],
  };
  box.setPointerCapture(event.pointerId);
  box.classList.add("active");
});

editorEl.addEventListener("pointermove", (event) => {
  const drag = editor.drag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  const area = deck.getBoundingClientRect();
  const dx = ((event.clientX - drag.startX) / area.width) * 100;
  const dy = ((event.clientY - drag.startY) / area.height) * 100;
  let [x, y, width, height] = drag.start;
  if (drag.resize) {
    width = Math.min(Math.max(snap(width + dx), MIN_WIDGET_SIZE), 100 - x);
    height = Math.min(Math.max(snap(height + dy), MIN_WIDGET_SIZE), 100 - y);
  } else {
    x = Math.min(Math.max(snap(x + dx), 0), 100 - width);
    y = Math.min(Math.max(snap(y + dy), 0), 100 - height);
  }
  const rect = [x, y, width, height];
  editor.draft[drag.id] = rect;
  placeElement(widgets[drag.id], rect);
  placeElement(editor.boxes[drag.id], rect);
});

function endDrag(event) {
  const drag = editor.drag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  editor.boxes[drag.id]?.classList.remove("active");
  editor.drag = null;
}
editorEl.addEventListener("pointerup", endDrag);
editorEl.addEventListener("pointercancel", endDrag);

document.getElementById("edit-save").addEventListener("click", () => stopEditing(true));
document.getElementById("edit-cancel").addEventListener("click", () => stopEditing(false));
document.getElementById("edit-auto").addEventListener("click", () => {
  settings.layouts[editor.orientation] = null;
  saveSettings();
  stopEditing(false);
});
document.getElementById("customize").addEventListener("click", startEditing);
document.getElementById("auto-layout").addEventListener("click", () => {
  settings.layouts[orientationKind()] = null;
  saveSettings();
  applyLayout();
  syncSettingsForm();
});

let layoutFrame = 0;
function relayout() {
  // The draft belongs to the orientation it was made in.
  if (editor.active && orientationKind() !== editor.orientation) stopEditing(false);
  cancelAnimationFrame(layoutFrame);
  layoutFrame = requestAnimationFrame(applyLayout);
}
window.addEventListener("resize", relayout);

// ---------- tilt ----------

// Gravity in screen coordinates: r = towards screen right, u = towards screen top,
// z = out of the screen towards the user. Low-pass filtered to calm sensor noise.
const tilt = { gravity: null, lastEvent: 0 };
const TILT_SMOOTHING = 0.25;
// No sensor reading for this long means the sensor stopped: release tilt pedals.
const TILT_STALE_MS = 500;

function toScreenFrame({ x, y, z }) {
  switch (screen.orientation?.angle ?? window.orientation ?? 0) {
    case 90: return { r: -y, u: x, z };
    case 270: case -90: return { r: y, u: -x, z };
    case 180: return { r: -x, u: -y, z };
    default: return { r: x, u: y, z };
  }
}

function orientationKind() {
  return matchMedia("(orientation: portrait)").matches ? "portrait" : "landscape";
}

// The screen axes swap on rotation: restart the filter so it doesn't blend the two poses
// (the first reading in a new orientation may also become its neutral).
function resetTiltFilter() {
  tilt.gravity = null;
}
screen.orientation?.addEventListener("change", resetTiltFilter);
window.addEventListener("orientationchange", resetTiltFilter);

window.addEventListener("devicemotion", (event) => {
  const raw = event.accelerationIncludingGravity;
  if (!raw || raw.x === null) return;
  const sample = toScreenFrame(raw);
  if (!tilt.gravity) {
    tilt.gravity = sample;
  } else {
    for (const axis of ["r", "u", "z"]) {
      tilt.gravity[axis] += (sample[axis] - tilt.gravity[axis]) * TILT_SMOOTHING;
    }
  }
  tilt.lastEvent = performance.now();
  if (!settings.neutrals[orientationKind()]) setNeutral();
});

// Pitch: positive when the top edge tilts away from you (0 = screen upright, 90 = flat).
// Roll: positive when tilted right. Both are angles against the horizontal plane, so each
// stays accurate however far the other one is tilted (dividing by u instead blew up roll
// as the phone leaned back, turning sensor noise into steering).
function tiltAngles() {
  const { r, u, z } = tilt.gravity;
  const toDegrees = 180 / Math.PI;
  return {
    pitch: Math.atan2(z, Math.hypot(u, r)) * toDegrees,
    roll: Math.atan2(-r, Math.hypot(u, z)) * toDegrees,
  };
}

function setNeutral() {
  centerLook();
  if (!tilt.gravity) return;
  settings.neutrals[orientationKind()] = tiltAngles();
  saveSettings();
}

function tiltOffsets() {
  const { pitch, roll } = tiltAngles();
  const neutral = settings.neutrals[orientationKind()] || { pitch, roll };
  return { pitch: pitch - neutral.pitch, roll: roll - neutral.roll };
}

const TILT_DIRECTION = {
  away: (o) => o.pitch,
  toward: (o) => -o.pitch,
  right: (o) => o.roll,
  left: (o) => -o.roll,
};

function applyTilt() {
  const live = tilt.gravity && performance.now() - tilt.lastEvent < TILT_STALE_MS;
  const offsets = live ? tiltOffsets() : null;
  const { tiltFull, tiltDeadzone, modes } = shared.pedals;
  for (const key of PEDAL_KEYS) {
    if (!settings.controls[key] || !isTiltPedal(key)) continue;
    if (!offsets) {
      setPedalInput(key, 0);
      continue;
    }
    const degrees = TILT_DIRECTION[modes[key]](offsets);
    setPedalInput(key, (degrees - tiltDeadzone) / (tiltFull - tiltDeadzone));
  }
  return offsets;
}

// Tilting the top this far away from neutral pauses tilt-speed steering.
const TILT_STEER_PAUSE_PITCH = 45;
// Small wobble around level that gyro steering ignores.
const GYRO_DEADZONE = 1.5;

// Returns true while tilt is driving the wheel (so spring-back stays off).
function applyTiltSteering(offsets, dt) {
  const { wheel } = shared;
  if (wheel.steerMode === "gyro" && !offsets) {
    // Sensor stopped: straighten up rather than hold the last lock.
    state.angle = 0;
    state.raw.s = 0;
    return true;
  }
  if (!offsets) return false;
  if (wheel.steerMode === "gyro") {
    const beyond = Math.max(Math.abs(offsets.roll) - GYRO_DEADZONE, 0);
    const fraction = Math.min(beyond / (wheel.gyroLock - GYRO_DEADZONE), 1);
    state.raw.s = Math.sign(offsets.roll) * fraction;
    state.angle = curves.pre.s.signed(state.raw.s) * halfRange();
    return true;
  }
  return applyTiltSpeedSteering(offsets, dt);
}

// Tilt-speed steering is a rate control: the further you tilt, the faster the wheel turns.
// A finger on the wheel takes priority.
function applyTiltSpeedSteering(offsets, dt) {
  const { wheel } = shared;
  if (wheel.steerMode !== "tilt-speed") return false;
  state.raw.s = 0;
  if (grab.pointerId !== null || offsets.pitch > TILT_STEER_PAUSE_PITCH) return false;
  const { tiltSteerDeadzone: deadzone, tiltSteerFull: full } = wheel;
  const beyond = Math.abs(offsets.roll) - deadzone;
  if (beyond <= 0) return false;
  const fraction = Math.min(beyond / (full - deadzone), 1);
  state.raw.s = Math.sign(offsets.roll) * fraction;
  const strength = curves.pre.s(fraction);
  const rate = Math.sign(offsets.roll) * strength * wheel.tiltSteerSpeed;
  state.angle = clampAngle(state.angle + rate * dt);
  return true;
}

// iOS only delivers motion events after an explicit permission request from a tap.
async function requestMotionPermission() {
  // The events don't exist at all on insecure (http://) pages.
  for (const type of [globalThis.DeviceMotionEvent, globalThis.DeviceOrientationEvent]) {
    if (typeof type?.requestPermission !== "function") continue;
    try {
      await type.requestPermission();
    } catch {
      // Denied or not from a gesture: tilt and look simply stay still.
    }
  }
}

// ---------- looking around ----------

// Turning the phone you steer with left or right looks around in the game. The compass
// gives the direction the back of the phone faces; rolling the phone to steer doesn't change
// that direction, so steering and looking stay apart.
const LOOK_SMOOTHING_S = 0.12;
const LOOK_STALE_MS = 500;
// Below this share of the back facing sideways (phone close to flat) the direction is noise.
const LOOK_MIN_LEVEL = 0.4;
const look = { east: null, north: 0, time: 0, center: null };
const lookReadout = document.getElementById("look-readout");

function usesLook() {
  return shared.look.enabled && settings.controls.wheel;
}

function onCompass(event) {
  if (event.alpha === null || (event.type === "deviceorientation" && !event.absolute &&
      event.webkitCompassHeading === undefined)) return;
  const rad = Math.PI / 180;
  let east;
  let north;
  if (event.webkitCompassHeading !== undefined && event.type === "deviceorientation") {
    // iOS: the heading of the top edge; good enough while the phone is held upright.
    const heading = event.webkitCompassHeading * rad;
    [east, north] = [Math.sin(heading), Math.cos(heading)];
  } else {
    const [a, b, g] = [event.alpha * rad, event.beta * rad, event.gamma * rad];
    // The screen's outward axis in earth coordinates (east, north, up); the back faces the
    // opposite way.
    east = -(Math.cos(a) * Math.sin(g) + Math.sin(a) * Math.sin(b) * Math.cos(g));
    north = -(Math.sin(a) * Math.sin(g) - Math.cos(a) * Math.sin(b) * Math.cos(g));
  }
  const level = Math.hypot(east, north);
  if (level < LOOK_MIN_LEVEL) return;
  [east, north] = [east / level, north / level];
  const now = performance.now();
  if (look.east === null || now - look.time > LOOK_STALE_MS) {
    Object.assign(look, { east, north });
  } else {
    const k = 1 - Math.exp(-(now - look.time) / 1000 / LOOK_SMOOTHING_S);
    look.east += (east - look.east) * k;
    look.north += (north - look.north) * k;
  }
  look.time = now;
  look.center ??= lookHeading();
}
window.addEventListener("deviceorientationabsolute", onCompass);
window.addEventListener("deviceorientation", onCompass);

function lookHeading() {
  return Math.atan2(look.east, look.north) * 180 / Math.PI;
}

// Degrees the phone points right of straight ahead, or null without a compass reading.
function lookYaw() {
  if (!usesLook() || look.east === null || performance.now() - look.time > LOOK_STALE_MS) {
    return null;
  }
  look.center ??= lookHeading();
  return ((lookHeading() - look.center + 540) % 360) - 180;
}

// Straight ahead is wherever the phone points now; the PC puts the game's camera back too.
function centerLook() {
  look.center = look.east === null ? null : lookHeading();
  if (usesLook()) send({ lookCenter: true });
}

function showLook() {
  const yaw = lookYaw();
  lookReadout.hidden = !usesLook();
  if (lookReadout.hidden) return;
  let text = `look ${formatDegrees(yaw ?? 0)}`;
  if (yaw === null) text = window.isSecureContext ? "look: no compass" : "look: needs https";
  if (lookReadout.textContent !== text) lookReadout.textContent = text;
}

// ---------- signal lever ----------

// A turn signal stalk with three detents: up = right, down = left, middle = off. Most games
// (Rigs of Rods among them) toggle a signal on each button press, so by default every move
// of the lever sends short presses: one to switch the old side off, one for the new side.
// The PC can switch it to holding the button while the lever is out instead.
const leverEl = document.getElementById("lever");
const LEVER_BITS = { left: 1 << 12, right: 1 << 13 };
const LEVER_OFFSET = { left: -1, center: 0, right: 1 };
const LEVER_PULSE_MS = 150;
const LEVER_PULSE_GAP_MS = 100;
const LEVER_TAP_PX = 8;
// Auto cancel: after the wheel turned this far towards the signal, coming back under the
// second angle returns the lever, as in a car.
const LEVER_CANCEL_TURN = 90;
const LEVER_CANCEL_BACK = 20;
// The game's own signal state (OutGauge) wins after the lever has rested this long.
const LEVER_SYNC_MS = 1000;

const lever = {
  position: "center",
  pointerId: null,
  startY: 0,
  startOffset: 0,
  moved: false,
  pulses: [],
  pulse: null,
  peak: 0,
  since: 0,
};

function setLever(position, { pulse = true } = {}) {
  if (position === lever.position) {
    showLever(LEVER_OFFSET[position]);
    return;
  }
  if (pulse && shared.lever.mode === "pulse") {
    if (lever.position !== "center") lever.pulses.push(LEVER_BITS[lever.position]);
    if (position !== "center") lever.pulses.push(LEVER_BITS[position]);
  }
  lever.position = position;
  lever.peak = 0;
  lever.since = performance.now();
  buzz(25);
  showLever(LEVER_OFFSET[position]);
}

function showLever(offset) {
  leverEl.dataset.position = lever.position;
  leverEl.style.setProperty("--offset", offset);
}

leverEl.addEventListener("pointerdown", (event) => {
  if (lever.pointerId !== null) return;
  event.preventDefault();
  lever.pointerId = event.pointerId;
  lever.startY = event.clientY;
  lever.startOffset = LEVER_OFFSET[lever.position];
  lever.moved = false;
  leverEl.setPointerCapture(event.pointerId);
  leverEl.classList.add("held");
});

leverEl.addEventListener("pointermove", (event) => {
  if (event.pointerId !== lever.pointerId) return;
  const dy = lever.startY - event.clientY;
  if (Math.abs(dy) > LEVER_TAP_PX) lever.moved = true;
  if (!lever.moved) return;
  const travel = leverEl.getBoundingClientRect().height * 0.3;
  showLever(Math.max(-1, Math.min(1, lever.startOffset + dy / travel)));
});

function releaseLever(event) {
  if (event.pointerId !== lever.pointerId) return;
  lever.pointerId = null;
  leverEl.classList.remove("held");
  if (!lever.moved) {
    // A tap flicks it: the top half signals right, the bottom half left, and a tap on a
    // lever that is out cancels.
    const rect = leverEl.getBoundingClientRect();
    const upper = event.clientY < rect.top + rect.height / 2;
    setLever(lever.position !== "center" ? "center" : upper ? "right" : "left");
    return;
  }
  const offset = Number(leverEl.style.getPropertyValue("--offset")) || 0;
  setLever(offset > 0.5 ? "right" : offset < -0.5 ? "left" : "center");
}
leverEl.addEventListener("pointerup", releaseLever);
leverEl.addEventListener("pointercancel", releaseLever);

function autoCancelLever() {
  if (!shared.lever.autoCancel || lever.position === "center" || !settings.controls.wheel) return;
  const toward = lever.position === "right" ? state.angle : -state.angle;
  lever.peak = Math.max(lever.peak, toward);
  if (lever.peak > LEVER_CANCEL_TURN && toward < LEVER_CANCEL_BACK) setLever("center");
}

// With telemetry, follow the game: it may cancel a signal itself.
function syncLeverWithGame(now) {
  const live = game.packet?.lights && now - game.time < GAME_STALE_MS;
  if (!live || shared.lever.mode !== "pulse" || lever.pulse || lever.pulses.length) return;
  if (lever.pointerId !== null || now - lever.since < LEVER_SYNC_MS) return;
  const { left, right } = game.packet.lights;
  if (left && right) return; // hazard lights
  const side = left ? "left" : right ? "right" : "center";
  if (side !== lever.position) {
    lever.position = side;
    lever.since = now;
    showLever(LEVER_OFFSET[side]);
  }
}

function leverButtons(now) {
  if (!lever.pulse && lever.pulses.length) {
    lever.pulse = { bit: lever.pulses.shift(), until: now + LEVER_PULSE_MS };
  }
  let bits = 0;
  if (lever.pulse) {
    if (now < lever.pulse.until) bits = lever.pulse.bit;
    else if (now >= lever.pulse.until + LEVER_PULSE_GAP_MS) lever.pulse = null;
  }
  if (shared.lever.mode === "hold" && lever.position !== "center") bits |= LEVER_BITS[lever.position];
  state.buttons = (state.buttons & ~(LEVER_BITS.left | LEVER_BITS.right)) | bits;
}

// ---------- shift lever ----------

// A sequential shifter: push it up or down and it springs back. The shift button is held while
// the lever is out, like a paddle; a tap on its top or bottom half is a quick shift.
const shifterEl = document.getElementById("shifter");
const SHIFT_BITS = { up: 1 << 1, down: 1 << 0 };
const SHIFT_ENGAGE = 0.6;
const SHIFT_RELEASE = 0.35;
const SHIFT_TAP_MS = 120;
const shifter = { pointerId: null, startY: 0, moved: false, bit: 0, tapBit: 0, tapUntil: 0 };

function shiftBit(direction) {
  // Rally style flips it: pulling down shifts up.
  const up = (direction === "up") === shared.shifter.upShifts;
  return up ? SHIFT_BITS.up : SHIFT_BITS.down;
}

function showShifter(offset) {
  shifterEl.style.setProperty("--offset", offset);
  shifterEl.dataset.shift = shifter.bit === SHIFT_BITS.up ? "up"
    : shifter.bit === SHIFT_BITS.down ? "down" : "none";
}

shifterEl.addEventListener("pointerdown", (event) => {
  if (shifter.pointerId !== null) return;
  event.preventDefault();
  shifter.pointerId = event.pointerId;
  shifter.startY = event.clientY;
  shifter.moved = false;
  shifterEl.setPointerCapture(event.pointerId);
  shifterEl.classList.add("held");
});

shifterEl.addEventListener("pointermove", (event) => {
  if (event.pointerId !== shifter.pointerId) return;
  const dy = shifter.startY - event.clientY;
  if (Math.abs(dy) > LEVER_TAP_PX) shifter.moved = true;
  if (!shifter.moved) return;
  const travel = shifterEl.getBoundingClientRect().height * 0.3;
  const offset = Math.max(-1, Math.min(1, dy / travel));
  if (!shifter.bit && Math.abs(offset) > SHIFT_ENGAGE) {
    shifter.bit = shiftBit(offset > 0 ? "up" : "down");
    buzz(30);
  } else if (shifter.bit && Math.abs(offset) < SHIFT_RELEASE) {
    shifter.bit = 0;
  }
  showShifter(offset);
});

function releaseShifter(event) {
  if (event.pointerId !== shifter.pointerId) return;
  shifter.pointerId = null;
  shifterEl.classList.remove("held");
  if (!shifter.moved && event.type === "pointerup") {
    const rect = shifterEl.getBoundingClientRect();
    shifter.tapBit = shiftBit(event.clientY < rect.top + rect.height / 2 ? "up" : "down");
    shifter.tapUntil = performance.now() + SHIFT_TAP_MS;
    buzz(30);
  }
  shifter.bit = 0;
  showShifter(0);
}
shifterEl.addEventListener("pointerup", releaseShifter);
shifterEl.addEventListener("pointercancel", releaseShifter);

function shifterButtons(now) {
  if (now >= shifter.tapUntil) shifter.tapBit = 0;
  return shifter.bit | shifter.tapBit;
}

// ---------- camera pedal ----------

// Covering the back camera presses the pedal: the darker the picture, the further. The page
// measures the average brightness of a tiny copy of each new camera frame.
const CAMERA_DEFAULT_COVERED = 0.03;
// While uncovered, the open level follows slow lighting changes.
const CAMERA_FOLLOW = 0.02;
// The pedal snaps to fully up or down this close to either end, so sensor noise can't hold
// it slightly pressed.
const CAMERA_SNAP = 0.04;
const cameraCanvas = document.getElementById("camera-preview");
const camera = {
  stream: null, video: null, context: null, starting: false, error: "",
  level: null, open: null, value: 0, lenses: [], lens: null,
  // "locked", "unsupported" (the phone keeps adjusting brightness) or "" before trying.
  exposure: "",
  filter: { time: null, value: 0, speed: 0 },
};

function usesCamera() {
  return PEDAL_KEYS.some((key) => settings.controls[key] && shared.pedals.modes[key] === "camera");
}

function syncCamera() {
  const wanted = usesCamera() && !document.hidden;
  if (wanted && !camera.stream && !camera.starting) startCamera();
  if (!wanted && camera.stream) stopCamera();
}
document.addEventListener("visibilitychange", syncCamera);

function cameraConstraints(lens) {
  const size = { width: { ideal: 160 }, height: { ideal: 120 }, frameRate: { ideal: 30 } };
  return lens ? { deviceId: { exact: lens }, ...size }
    : { facingMode: { ideal: "environment" }, ...size };
}

async function startCamera() {
  if (!navigator.mediaDevices?.getUserMedia) {
    camera.error = window.isSecureContext ? "This browser can't use the camera"
      : "The camera needs the https address";
    return;
  }
  camera.starting = true;
  try {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false, video: cameraConstraints(settings.camera.lens),
      });
    } catch (error) {
      // The chosen lens is gone (or busy): fall back to the default back camera.
      if (!settings.camera.lens) throw error;
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false, video: cameraConstraints(null),
      });
    }
    camera.video ??= Object.assign(document.createElement("video"), {
      muted: true, playsInline: true,
    });
    camera.video.srcObject = stream;
    await camera.video.play();
    camera.context ??= cameraCanvas.getContext("2d", { willReadFrequently: true });
    camera.stream = stream;
    camera.lens = stream.getVideoTracks()[0]?.getSettings().deviceId ?? null;
    camera.error = "";
    camera.open = null;
    camera.level = null;
    camera.filter.time = null;
    if (camera.exposure === "locked") camera.exposure = "";
    watchCameraFrames(stream);
    listLenses();
    // Lock the exposure once it settled, so the camera doesn't brighten the picture back up
    // as the finger covers it.
    setTimeout(lockExposure, 1500);
    if (!usesCamera() || document.hidden) stopCamera();
  } catch (error) {
    camera.error = `Camera unavailable: ${error.message || error.name}`;
  } finally {
    camera.starting = false;
  }
}

function stopCamera() {
  camera.stream?.getTracks().forEach((track) => track.stop());
  camera.stream = null;
  camera.level = null;
  camera.value = 0;
}

// Measures each camera frame once, as it arrives. Without frame callbacks the render loop
// measures instead.
function watchCameraFrames(stream) {
  const { video } = camera;
  if (!video.requestVideoFrameCallback) return;
  const onFrame = (now) => {
    if (camera.stream !== stream) return;
    measureCamera(now);
    video.requestVideoFrameCallback(onFrame);
  };
  video.requestVideoFrameCallback(onFrame);
}

async function lockExposure() {
  const stream = camera.stream;
  const track = stream?.getVideoTracks()[0];
  if (!track?.getCapabilities || camera.exposure === "unsupported") return;
  const capabilities = track.getCapabilities();
  const { exposureTime } = track.getSettings();
  // Without the current exposure time, manual mode falls back to whatever the driver picks,
  // which on some phones is the shortest exposure: a black picture.
  if (!(capabilities.exposureMode || []).includes("manual") || !(exposureTime > 0)) {
    camera.exposure = "unsupported";
    return;
  }
  const before = camera.level;
  try {
    await track.applyConstraints({ advanced: [{ exposureMode: "manual", exposureTime }] });
  } catch {
    camera.exposure = "unsupported";
    return;
  }
  // Some phones still turn the picture black once locked. Check, and undo it if so.
  await new Promise((resolve) => setTimeout(resolve, 700));
  if (camera.stream !== stream) return;
  if (before !== null && camera.level !== null && camera.level < before * 0.5) {
    camera.exposure = "unsupported";
    try {
      await track.applyConstraints({ advanced: [{ exposureMode: "continuous" }] });
    } catch {
      // Nothing more to try; the restart below gets automatic exposure back.
      stopCamera();
      syncCamera();
    }
    return;
  }
  camera.exposure = "locked";
}

// Phones with several back cameras let the driver pick the lens their finger covers.
async function listLenses() {
  let devices;
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch {
    return;
  }
  const cameras = devices.filter((device) => device.kind === "videoinput" && device.deviceId);
  const back = cameras.filter((device) => /back|rear|environment/i.test(device.label));
  camera.lenses = (back.length ? back : cameras).map((device, index) => ({
    id: device.deviceId, name: back.length ? `Back camera ${index + 1}` : `Camera ${index + 1}`,
  }));
  showLenses();
}

function showLenses() {
  const select = document.getElementById("camera-lens");
  const options = [["", "Default back camera"], ...camera.lenses.map((lens) => [lens.id, lens.name])];
  select.replaceChildren(...options.map(([value, text]) => {
    const option = new Option(text, value);
    if (value && value === camera.lens && !settings.camera.lens) option.text += " (default)";
    return option;
  }));
  select.value = camera.lenses.some((lens) => lens.id === settings.camera.lens)
    ? settings.camera.lens : "";
  document.getElementById("camera-lens-row").hidden = camera.lenses.length < 2;
}

document.getElementById("camera-lens").addEventListener("change", (event) => {
  settings.camera.lens = event.target.value || null;
  // Each lens sees a different amount of light, so its calibration starts over.
  settings.camera.open = null;
  settings.camera.covered = null;
  saveSettings();
  stopCamera();
  syncCamera();
});

function readCamera() {
  const { video, context } = camera;
  if (!camera.stream || !video || video.readyState < 2) return null;
  context.drawImage(video, 0, 0, cameraCanvas.width, cameraCanvas.height);
  const { data } = context.getImageData(0, 0, cameraCanvas.width, cameraCanvas.height);
  let sum = 0;
  for (let i = 0; i < data.length; i += 4) {
    sum += 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
  }
  return sum / (data.length / 4) / 255;
}

// One-euro filter: smooths hard while the finger holds still and follows quickly while it
// moves, so pressing doesn't lag. A small dead band on top keeps a steady finger's pedal from
// flickering with the sensor noise.
function smoothCamera(value, time) {
  const filter = camera.filter;
  if (filter.time === null) {
    Object.assign(filter, { time, value, speed: 0, held: value });
    return value;
  }
  const dt = Math.max((time - filter.time) / 1000, 0.001);
  filter.time = time;
  const smoothing = settings.camera.smoothing;
  const alpha = (cutoff) => 1 / (1 + 1 / (2 * Math.PI * cutoff * dt));
  filter.speed += ((value - filter.value) / dt - filter.speed) * alpha(0.5);
  const cutoff = 3 * 0.04 ** smoothing + (0.8 * (1 - smoothing) + 0.1) * Math.abs(filter.speed);
  filter.value += (value - filter.value) * alpha(cutoff);
  const band = 0.04 * smoothing;
  if (filter.value > filter.held + band) filter.held = filter.value - band;
  else if (filter.value < filter.held - band) filter.held = filter.value + band;
  return filter.held;
}

function measureCamera(time) {
  const raw = readCamera();
  if (raw === null) return;
  // A light average keeps calibration readings and the automatic open level steady.
  camera.level = camera.level === null ? raw : camera.level + (raw - camera.level) * 0.3;
  const covered = settings.camera.covered ?? CAMERA_DEFAULT_COVERED;
  let open = settings.camera.open;
  if (open === null) {
    camera.open ??= camera.level;
    // Follow the room's light while clearly uncovered.
    if (camera.level > camera.open) camera.open = camera.level;
    else if (camera.level > camera.open * 0.8) {
      camera.open += (camera.level - camera.open) * CAMERA_FOLLOW;
    }
    open = camera.open;
  }
  const pressed = open > covered ? (open - raw) / (open - covered) : 0;
  const value = smoothCamera(Math.max(0, Math.min(1, pressed)), time);
  camera.value = value < CAMERA_SNAP ? 0 : value > 1 - CAMERA_SNAP ? 1
    : (value - CAMERA_SNAP) / (1 - 2 * CAMERA_SNAP);
}

function applyCamera(now) {
  if (!usesCamera()) return;
  if (camera.stream && !camera.video.requestVideoFrameCallback) measureCamera(now);
  for (const key of PEDAL_KEYS) {
    if (settings.controls[key] && shared.pedals.modes[key] === "camera") {
      setPedalInput(key, camera.value);
    }
  }
}

function showCameraStatus() {
  const box = document.getElementById("camera-settings");
  box.hidden = !usesCamera();
  if (box.hidden) return;
  const status = document.getElementById("camera-status");
  let text;
  if (camera.error) text = camera.error;
  else if (camera.level === null) text = "Starting the camera…";
  else {
    text = `Covered ${Math.round(camera.value * 100)}% (light ${Math.round(camera.level * 100)}%)`;
    if (camera.exposure === "unsupported") {
      text += ". This camera keeps adjusting its brightness, so calibrate with both buttons.";
    }
  }
  if (status.textContent !== text) status.textContent = text;
}

for (const [id, key] of [["camera-open", "open"], ["camera-covered", "covered"]]) {
  document.getElementById(id).addEventListener("click", () => {
    if (camera.level === null) return;
    settings.camera[key] = camera.level;
    saveSettings();
    if (key === "open") lockExposure();
    buzz(30);
  });
}
document.getElementById("camera-auto").addEventListener("click", () => {
  settings.camera.open = null;
  settings.camera.covered = null;
  camera.open = null;
  saveSettings();
});

const smoothingInput = document.getElementById("camera-smoothing");
smoothingInput.value = Math.round(settings.camera.smoothing * 100);
smoothingInput.addEventListener("input", () => {
  settings.camera.smoothing = smoothingInput.value / 100;
  saveSettings();
});

// ---------- buttons ----------

for (const button of document.querySelectorAll("[data-button]")) {
  const bit = 1 << Number(button.dataset.button);
  const pointers = new Set();

  const press = (event) => {
    pointers.add(event.pointerId);
    button.setPointerCapture(event.pointerId);
    if (!(state.buttons & bit)) buzz(30);
    state.buttons |= bit;
    button.classList.add("down");
    event.preventDefault();
  };
  const release = (event) => {
    pointers.delete(event.pointerId);
    if (pointers.size) return;
    state.buttons &= ~bit;
    button.classList.remove("down");
  };

  button.addEventListener("pointerdown", press);
  button.addEventListener("pointerup", release);
  button.addEventListener("pointercancel", release);
  button.addEventListener("contextmenu", (event) => event.preventDefault());
  button.resetButton = () => {
    pointers.clear();
    button.classList.remove("down");
  };
}

function applyButtonLabels() {
  for (const button of document.querySelectorAll("[data-button]")) {
    const label = shared.buttons[Number(button.dataset.button)];
    if (label !== undefined) button.textContent = label;
  }
}

// Android drops vibrations of 1 s or less from the browser when the phone's "Touch
// feedback" setting is off; a page cannot work around it (cancelling a longer buzz early
// is ignored too), so short buzzes need that setting on.
function vibrateFor(ms) {
  if (typeof navigator.vibrate !== "function") return false;
  return navigator.vibrate(ms);
}

function buzz(ms) {
  if (settings.haptics) vibrateFor(ms);
}

// Rumble from the game: a buzz each window, longer the stronger it is (the browser can't
// set vibration strength).
const RUMBLE_WINDOW_MS = 250;
let rumbleUntil = 0;

function rumble(now) {
  const strength = shared.ffb.enabled && shared.ffb.vibrate ? feedback.rumble : 0;
  if (!settings.haptics || strength < 0.05) {
    if (rumbleUntil) {
      vibrateFor(0);
      rumbleUntil = 0;
    }
    return;
  }
  if (now < rumbleUntil) return;
  rumbleUntil = now + RUMBLE_WINDOW_MS;
  vibrateFor(Math.round(40 + (RUMBLE_WINDOW_MS - 40) * strength));
}

// Lifting off the page (app switch, screen off) must not leave anything held down.
function releaseEverything() {
  grab.pointerId = null;
  wheelEl.classList.remove("grabbed");
  document.querySelectorAll(".pedal").forEach((pedal) => pedal.resetPedal());
  document.querySelectorAll("[data-button]").forEach((button) => button.resetButton());
  state.buttons = 0;
  touches.clear();
  sendState(true);
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) releaseEverything();
});
window.addEventListener("blur", releaseEverything);
document.addEventListener("contextmenu", (event) => event.preventDefault());

// ---------- connection ----------

let socket = null;
let lastSent = "";
let lastRtt = null;
let programs = [];
const PING_INTERVAL_MS = 2000;

function setStatus(stateName, text) {
  statusEl.dataset.state = stateName;
  statusEl.textContent = text;
}

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function sendHello() {
  const controls = Object.keys(settings.controls).filter((key) => settings.controls[key]);
  const buttons = buttonIndexes().filter((index) => widgetShown(`button-${index}`));
  send({ hello: { id: settings.deviceId, controls, buttons } });
}

function connect() {
  setStatus("connecting", "connecting");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/ws`);
  socket.addEventListener("open", () => {
    setStatus("online", "connected");
    lastSent = "";
    sendHello();
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.version) checkVersion(message.version);
    if (message.config) applyShared(message.config);
    if (message.ff) Object.assign(feedback, message.ff);
    if (message.game) {
      game.packet = message.game;
      game.time = performance.now();
    }
    if (message.programs) {
      programs = message.programs;
      syncSettingsForm();
    }
    if (message.pong !== undefined) lastRtt = Math.round(performance.now() - message.pong);
    // The PC's phone list: show or hide one control here.
    if (message.show && widgets[message.show.widget]) {
      setWidgetShown(message.show.widget, Boolean(message.show.shown));
    }
  });
  socket.addEventListener("close", () => {
    setStatus("offline", "reconnecting");
    Object.assign(feedback, { force: 0, spring: 0, rumble: 0 });
    setTimeout(connect, 1000);
  });
}

setInterval(() => send({ ping: performance.now(), rtt: lastRtt }), PING_INTERVAL_MS);

function showGame() {
  const live = game.packet && performance.now() - game.time < GAME_STALE_MS;
  gameReadout.hidden = !live;
  if (!live) return;
  const { gear, speed, rpm } = game.packet;
  const text = `${gear} · ${Math.round(speed)} km/h · ${rpm} rpm`;
  if (gameReadout.textContent !== text) gameReadout.textContent = text;
}

// ---------- auto update ----------

// The server sends the page version on connect and again whenever a page file changes.
// A new version reloads the page, but only once no finger is down, Settings is closed and
// no layout is being edited, so an update never yanks a pedal mid-press.
const RESTORE_FULLSCREEN_KEY = "phone-wheel-restore-fullscreen";
let pageVersion = null;
let updatePending = false;
const touches = new Set();

document.addEventListener("pointerdown", (event) => touches.add(event.pointerId), true);
for (const type of ["pointerup", "pointercancel"]) {
  document.addEventListener(type, (event) => touches.delete(event.pointerId), true);
}

function checkVersion(version) {
  pageVersion ??= version;
  if (version === pageVersion || updatePending) return;
  updatePending = true;
  setStatus("connecting", "updating");
}

function reloadWhenIdle() {
  if (!updatePending || touches.size || dialog.open || editor.active) return;
  updatePending = false;
  try {
    sessionStorage.setItem(RESTORE_FULLSCREEN_KEY, document.fullscreenElement ? "1" : "");
  } catch {
    // Blocked storage: the page just comes back without fullscreen.
  }
  location.reload();
}

// Fullscreen needs a user gesture, so after an update it comes back on the next tap.
function restoreFullscreenOnTap() {
  let restore = false;
  try {
    restore = sessionStorage.getItem(RESTORE_FULLSCREEN_KEY) === "1";
    sessionStorage.removeItem(RESTORE_FULLSCREEN_KEY);
  } catch {
    return;
  }
  if (!restore) return;
  document.addEventListener("pointerup", (event) => {
    if (!document.fullscreenElement && !event.target.closest("#fullscreen")) enterFullscreen();
  }, { once: true, capture: true });
}

// Only the controls shown on this phone are sent; the server takes the rest from other phones.
// Values are what the phone shows; the PC applies the output curves.
function encodeState() {
  const { controls } = settings;
  const message = {};
  const raw = {};
  if (controls.wheel) {
    message.s = Number((state.angle / halfRange()).toFixed(4));
    raw.s = Number((shared.wheel.steerMode === "touch" ? message.s : state.raw.s).toFixed(4));
  }
  for (const key of PEDAL_KEYS) {
    if (!controls[key]) continue;
    message[key] = Number(state.pedals[key].toFixed(3));
    raw[key] = Number(state.raw[key].toFixed(3));
  }
  if (controls.paddles || controls.buttons || controls.lever || controls.shifter) {
    message.k = state.buttons | (state.shiftBits || 0);
  }
  const yaw = lookYaw();
  if (yaw !== null) message.l = Number(yaw.toFixed(1));
  message.r = raw;
  return JSON.stringify(message);
}

function sendState(force = false) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  const message = encodeState();
  if (!force && message === lastSent) return;
  socket.send(message);
  lastSent = message;
}

// ---------- render loop ----------

let lastFrame = performance.now();

function frame(now) {
  const dt = Math.min((now - lastFrame) / 1000, 0.1);
  lastFrame = now;
  const tiltNow = applyTilt();
  if (settings.controls.wheel) {
    if (!applyTiltSteering(tiltNow, dt)) {
      followForce(dt);
      springBack(dt);
    }
  }
  applyCamera(now);
  state.shiftBits = shifterButtons(now);
  returnClutch(dt);
  autoCancelLever();
  syncLeverWithGame(now);
  leverButtons(now);
  wheelSvg.style.transform = `rotate(${state.angle}deg)`;
  angleReadout.textContent = `${Math.round(state.angle)}°`;
  showForce();
  showHub();
  showLook();
  showPedalMarks();
  showGame();
  rumble(now);
  if (dialog.open) {
    showTiltStatus(tiltNow);
    showCameraStatus();
  }
  sendState();
  reloadWhenIdle();
  requestAnimationFrame(frame);
}

// ---------- settings dialog ----------

const dialog = document.getElementById("settings");
const hapticsInput = document.getElementById("haptics");
const controlInputs = document.querySelectorAll("[data-control]");
const tiltStatus = document.getElementById("tilt-status");
let httpsUrl = null;

// One checkbox per button, so each phone can show its own set.
function syncButtonToggles() {
  const box = document.getElementById("button-toggles");
  const indexes = buttonIndexes();
  if (box.children.length !== indexes.length) {
    box.replaceChildren(...indexes.map((index) => {
      const label = document.createElement("label");
      label.className = "check";
      const input = Object.assign(document.createElement("input"), { type: "checkbox" });
      input.dataset.buttonToggle = index;
      input.addEventListener("change", () => setWidgetShown(`button-${index}`, input.checked));
      label.append(input, document.createElement("span"));
      return label;
    }));
  }
  for (const input of box.querySelectorAll("input")) {
    const index = Number(input.dataset.buttonToggle);
    input.checked = widgetShown(`button-${index}`);
    const name = shared.buttons[index] || `Button ${index + 1}`;
    const span = input.nextElementSibling;
    if (span.textContent !== name) span.textContent = name;
  }
}

function syncSettingsForm() {
  for (const input of controlInputs) input.checked = settings.controls[input.dataset.control];
  syncButtonToggles();
  hapticsInput.checked = settings.haptics;
  document.getElementById("auto-layout").hidden = !settings.layouts[orientationKind()];
  const used = document.getElementById("programs");
  used.textContent = programs.length
    ? `Game device open in: ${programs.join(", ")}`
    : "No game has the device open.";
}

for (const input of controlInputs) {
  input.addEventListener("change", () => {
    const control = input.dataset.control;
    settings.controls[control] = input.checked;
    // Turning a button group on shows all of it, including buttons removed one by one.
    if (input.checked && (control === "paddles" || control === "buttons")) {
      const members = groupIndexes(control);
      settings.hiddenButtons = settings.hiddenButtons.filter((i) => !members.includes(i));
    }
    // Start from center rather than jumping to wherever the wheel was when it was turned off.
    if (!settings.controls.wheel) state.angle = 0;
    releaseEverything();
    syncLayout();
    syncSettingsForm();
    saveSettings();
    sendHello();
    if (usesTilt() || usesLook()) requestMotionPermission();
  });
}

function formatDegrees(value) {
  const rounded = Math.round(value);
  return `${rounded > 0 ? "+" : ""}${rounded}°`;
}

function showTiltStatus(offsets) {
  let html;
  if (!window.isSecureContext) {
    const link = httpsUrl ? ` <a href="${httpsUrl}">${httpsUrl}</a>` : "";
    html = `Tilt needs HTTPS. Open${link || " the https:// address the server printed"}`;
  } else if (offsets) {
    html = `Away/toward ${formatDegrees(offsets.pitch)}, left/right ${formatDegrees(offsets.roll)}`;
  } else {
    html = "Waiting for the motion sensor…";
  }
  if (tiltStatus.innerHTML !== html) tiltStatus.innerHTML = html;
}

for (const id of ["set-neutral", "level"]) {
  document.getElementById(id).addEventListener("click", () => {
    setNeutral();
    buzz(30);
  });
}

fetch("/info")
  .then((response) => response.json())
  .then((info) => {
    if (info.https_port) httpsUrl = `https://${location.hostname}:${info.https_port}/`;
  })
  .catch(() => {});

hapticsInput.addEventListener("change", () => {
  settings.haptics = hapticsInput.checked;
  saveSettings();
});

// Shows what the browser says, so a silent phone can be told apart from a blocked API.
for (const button of document.querySelectorAll("[data-vibrate-ms]")) {
  button.addEventListener("click", () => {
    const result = document.getElementById("vibration-result");
    if (typeof navigator.vibrate !== "function") {
      result.textContent = "Not supported by this browser";
      return;
    }
    const ms = Number(button.dataset.vibrateMs);
    const accepted = vibrateFor(ms);
    result.textContent = accepted
      ? `${ms} ms accepted. No buzz? The phone dropped it.`
      : `${ms} ms blocked by the browser`;
  });
}

document.getElementById("open-settings").addEventListener("click", () => {
  syncSettingsForm();
  dialog.showModal();
  // Turning tilt on from the PC can't ask for sensor access (iOS needs a tap here).
  if (usesTilt() || usesLook()) requestMotionPermission();
  syncCamera();
});

async function enterFullscreen() {
  try {
    await document.documentElement.requestFullscreen({ navigationUI: "hide" });
    // Keep the current orientation, so tilting the phone for a pedal doesn't rotate the page.
    await screen.orientation?.lock?.(screen.orientation.type);
  } catch {
    // Not supported (e.g. iPhone Safari): the page still works without it.
  }
  requestWakeLock();
}

document.getElementById("fullscreen").addEventListener("click", async () => {
  if (!document.fullscreenElement) {
    await enterFullscreen();
    return;
  }
  try {
    await document.exitFullscreen();
  } catch {
    // Already left fullscreen.
  }
});

async function requestWakeLock() {
  try {
    await navigator.wakeLock?.request("screen");
  } catch {
    // Wake Lock needs HTTPS on most browsers; ignore when unavailable.
  }
}

saveSettings(); // keeps a newly made device id
applyButtonLabels();
syncSettingsForm();
syncLayout();
restoreFullscreenOnTap();
connect();
requestAnimationFrame(frame);
