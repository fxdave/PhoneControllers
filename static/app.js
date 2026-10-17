"use strict";

const SETTINGS_KEY = "phone-wheel-settings";
// Ignore touches this close to the wheel center: the angle is too jumpy there.
const DEAD_RADIUS_FRACTION = 0.12;

const settings = loadSettings();

const state = {
  angle: 0,          // degrees, cumulative (can be many turns)
  pedals: { t: 0, b: 0, c: 0 },
  buttons: 0,
};

const wheelZone = document.getElementById("wheel-zone");
const wheelEl = document.getElementById("wheel");
const wheelSvg = wheelEl.querySelector("svg");
const angleReadout = document.getElementById("angle-readout");
const statusEl = document.getElementById("status");

// ---------- settings ----------

function loadSettings() {
  const defaults = { range: 900, autocenter: false, haptics: true };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") };
  } catch {
    return defaults;
  }
}

function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // Private mode or blocked storage: settings just won't persist.
  }
}

function halfRange() {
  return settings.range / 2;
}

// ---------- steering wheel ----------

const grab = { pointerId: null, lastAngle: 0 };

function pointerAngle(event) {
  const rect = wheelEl.getBoundingClientRect();
  const dx = event.clientX - (rect.left + rect.width / 2);
  const dy = event.clientY - (rect.top + rect.height / 2);
  const tooClose = Math.hypot(dx, dy) < (rect.width / 2) * DEAD_RADIUS_FRACTION;
  return { angle: (Math.atan2(dy, dx) * 180) / Math.PI, tooClose };
}

function clampAngle(angle) {
  const limit = halfRange();
  return Math.max(-limit, Math.min(limit, angle));
}

wheelZone.addEventListener("pointerdown", (event) => {
  if (grab.pointerId !== null || event.target.closest(".paddle")) return;
  grab.pointerId = event.pointerId;
  grab.lastAngle = pointerAngle(event).angle;
  wheelZone.setPointerCapture(event.pointerId);
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

function releaseWheel(event) {
  if (event.pointerId !== grab.pointerId) return;
  grab.pointerId = null;
  wheelEl.classList.remove("grabbed");
}
wheelZone.addEventListener("pointerup", releaseWheel);
wheelZone.addEventListener("pointercancel", releaseWheel);

document.getElementById("recenter").addEventListener("click", () => {
  state.angle = 0;
});

function springBack(dt) {
  if (!settings.autocenter || grab.pointerId !== null || state.angle === 0) return;
  const step = Math.max(Math.abs(state.angle) * 6 * dt, 180 * dt);
  state.angle = Math.abs(state.angle) <= step ? 0 : state.angle - Math.sign(state.angle) * step;
}

// ---------- pedals ----------

for (const pedal of document.querySelectorAll(".pedal")) {
  const key = pedal.dataset.pedal;
  const fill = pedal.querySelector(".fill");
  let activePointer = null;

  const update = (event) => {
    const rect = pedal.getBoundingClientRect();
    const value = 1 - (event.clientY - rect.top) / rect.height;
    state.pedals[key] = Math.max(0, Math.min(1, value));
    fill.style.height = `${state.pedals[key] * 100}%`;
  };
  const release = (event) => {
    if (event.pointerId !== activePointer) return;
    activePointer = null;
    state.pedals[key] = 0;
    fill.style.height = "0%";
  };

  pedal.addEventListener("pointerdown", (event) => {
    if (activePointer !== null) return;
    activePointer = event.pointerId;
    pedal.setPointerCapture(event.pointerId);
    update(event);
    event.preventDefault();
  });
  pedal.addEventListener("pointermove", (event) => {
    if (event.pointerId === activePointer) update(event);
  });
  pedal.addEventListener("pointerup", release);
  pedal.addEventListener("pointercancel", release);
  pedal.resetPedal = () => {
    activePointer = null;
    state.pedals[key] = 0;
    fill.style.height = "0%";
  };
}

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

// Lifting off the page (app switch, screen off) must not leave anything held down.
function releaseEverything() {
  grab.pointerId = null;
  wheelEl.classList.remove("grabbed");
  document.querySelectorAll(".pedal").forEach((pedal) => pedal.resetPedal());
  document.querySelectorAll("[data-button]").forEach((button) => button.resetButton());
  state.buttons = 0;
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

function setStatus(stateName, text) {
  statusEl.dataset.state = stateName;
  statusEl.textContent = text;
}

function connect() {
  setStatus("connecting", "connecting");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/ws`);
  socket.addEventListener("open", () => {
    setStatus("online", "connected");
    lastSent = "";
  });
  socket.addEventListener("close", () => {
    setStatus("offline", "reconnecting");
    setTimeout(connect, 1000);
  });
}

function encodeState() {
  return JSON.stringify({
    s: Number((state.angle / halfRange()).toFixed(4)),
    t: Number(state.pedals.t.toFixed(3)),
    b: Number(state.pedals.b.toFixed(3)),
    c: Number(state.pedals.c.toFixed(3)),
    k: state.buttons,
  });
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
  springBack(dt);
  wheelSvg.style.transform = `rotate(${state.angle}deg)`;
  angleReadout.textContent = `${Math.round(state.angle)}°`;
  sendState();
  requestAnimationFrame(frame);
}

// ---------- toolbar & settings ----------

const dialog = document.getElementById("settings");
const rangeInput = document.getElementById("range");
const rangeValue = document.getElementById("range-value");
const autocenterInput = document.getElementById("autocenter");
const hapticsInput = document.getElementById("haptics");

function syncSettingsForm() {
  rangeInput.value = settings.range;
  rangeValue.textContent = `${settings.range}°`;
  autocenterInput.checked = settings.autocenter;
  hapticsInput.checked = settings.haptics;
}

rangeInput.addEventListener("input", () => {
  settings.range = Number(rangeInput.value);
  rangeValue.textContent = `${settings.range}°`;
  state.angle = clampAngle(state.angle);
  saveSettings();
});
autocenterInput.addEventListener("change", () => {
  settings.autocenter = autocenterInput.checked;
  saveSettings();
});
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
});

document.getElementById("fullscreen").addEventListener("click", async () => {
  try {
    if (!document.fullscreenElement) {
      await document.documentElement.requestFullscreen({ navigationUI: "hide" });
      await screen.orientation?.lock?.("landscape");
    } else {
      await document.exitFullscreen();
    }
  } catch {
    // Not supported (e.g. iPhone Safari): the page still works without it.
  }
  requestWakeLock();
});

async function requestWakeLock() {
  try {
    await navigator.wakeLock?.request("screen");
  } catch {
    // Wake Lock needs HTTPS on most browsers; ignore when unavailable.
  }
}

syncSettingsForm();
connect();
requestAnimationFrame(frame);
