"""Settings shared by every phone, kept on the PC so they can be changed without a phone in hand.

Each phone still keeps its own layout, which controls it shows, vibration and tilt neutral.
Keys are camelCase because the phone page reads the same object.
"""

import copy
import json
import math
import os
import shutil
from pathlib import Path

from . import curves

APP_DIR_NAME = "phone-controllers"
# The project's earlier name; its settings and certificate are copied over on first start.
OLD_APP_DIR_NAME = "phone-wheel"

STEER_MODES = ("touch", "tilt-speed", "gyro")
LEVER_MODES = ("pulse", "hold")
PEDAL_MODES = ("touch", "away", "toward", "left", "right", "camera")
PEDAL_KEYS = ("c", "b", "t")
AXIS_KEYS = ("s", "t", "b", "c")
BUTTON_COUNT = 14

DEFAULTS = {
    "wheel": {
        "range": 900,              # degrees lock to lock
        "autocenter": False,       # spring back to center when released
        "steerMode": "touch",      # "touch", "tilt-speed" or "gyro"
        "gyroLock": 60,            # degrees of phone tilt for full lock in gyro mode
        "tiltSteerSpeed": 540,     # degrees of wheel rotation per second at full tilt
        "tiltSteerFull": 30,       # degrees of tilt for full turning speed
        "tiltSteerDeadzone": 5,    # degrees of tilt ignored around neutral
    },
    "pedals": {
        # Per pedal: "touch", a tilt direction ("away", "toward", "left", "right"), or
        # "camera": covering the phone's back camera presses it.
        "modes": {"c": "touch", "b": "touch", "t": "touch"},
        "tiltFull": 25,            # degrees of tilt for a fully pressed pedal
        "tiltDeadzone": 3,         # degrees of tilt ignored around neutral
        "clutchReturn": 0.5,       # seconds for the clutch to come up after the finger lifts
    },
    # "pre": phone input (finger height, tilt) to what the pedal or wheel shows.
    # "post": what it shows to what the game gets. Steering curves shape each side alike;
    # the steering pre curve only bends tilt steering, since a dragged wheel is the input.
    "curves": {
        "pre": {key: curves.PRESETS["linear"] for key in AXIS_KEYS},
        "post": {
            "s": curves.PRESETS["linear"],
            "t": curves.PRESETS["linear"],
            "b": curves.PRESETS["linear"],
            "c": curves.PRESETS["clutch bite"],
        },
    },
    "ffb": {
        "enabled": True,           # accept force feedback from games and pass it to phones
        "gain": 1.0,               # scales every force the game sends
        "invert": False,           # flip the steering force direction
        "pull": True,              # a released on-screen wheel follows the game's force
        "pullSpeed": 720,          # degrees per second at full force
        "vibrate": True,           # rumble effects vibrate the phone
    },
    "buttons": [
        "Shift −", "Shift +", "Handbrake", "Horn", "Lights", "Camera",
        "Look back", "Wipers", "Ignition", "Hazard", "Map", "Pause",
        "Left signal", "Right signal",
    ],
    "lever": {
        "mode": "pulse",           # "pulse": a press per move (games that toggle), "hold"
        "autoCancel": True,        # returns to the middle when the wheel comes back
    },
    "shifter": {
        "upShifts": True,          # pushing the shift lever up shifts up (False: rally style)
    },
    "look": {
        "enabled": False,          # turning the steering phone left or right looks around
        "gain": 3.0,               # degrees the camera turns per degree the phone turns
        "deadzone": 5,             # degrees of phone turn ignored around center
        "invert": False,           # the camera turns the other way
    },
    "server": {
        "port": 8765,
        "httpsPort": 8766,
        "https": True,
        "outgauge": True,          # listen for OutGauge telemetry from games
        "outgaugePort": 4444,
        "autostart": True,         # desktop app starts the server when it opens
    },
}

# Ranges for numbers; anything outside is clamped when loading or changing settings.
LIMITS = {
    ("wheel", "range"): (90, 2520),
    ("wheel", "gyroLock"): (5, 90),
    ("wheel", "tiltSteerSpeed"): (30, 2880),
    ("wheel", "tiltSteerFull"): (2, 90),
    ("wheel", "tiltSteerDeadzone"): (0, 45),
    ("pedals", "tiltFull"): (2, 90),
    ("pedals", "tiltDeadzone"): (0, 45),
    ("pedals", "clutchReturn"): (0, 5),
    ("ffb", "gain"): (0, 4),
    ("ffb", "pullSpeed"): (0, 3600),
    ("look", "gain"): (0.5, 8),
    ("look", "deadzone"): (0, 45),
    ("server", "port"): (1, 65535),
    ("server", "httpsPort"): (1, 65535),
    ("server", "outgaugePort"): (1, 65535),
}


def app_dir(base: Path) -> Path:
    """This app's folder under an XDG base folder, carried over from the old name."""
    path, old = base / APP_DIR_NAME, base / OLD_APP_DIR_NAME
    if not path.exists() and old.is_dir():
        try:
            shutil.copytree(old, path)
        except OSError:
            pass
    return path


def config_dir() -> Path:
    return app_dir(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"))


def config_path() -> Path:
    return config_dir() / "config.json"


def _merge(default, stored, path=()):
    """Keep stored values of the right kind, fall back to defaults for the rest."""
    if isinstance(default, dict):
        stored = stored if isinstance(stored, dict) else {}
        return {key: _merge(value, stored.get(key), path + (key,)) for key, value in default.items()}
    if path[:1] == ("curves",):
        try:
            return curves.normalize(stored) if stored is not None else copy.deepcopy(default)
        except (TypeError, ValueError, IndexError):
            return copy.deepcopy(default)
    if path == ("buttons",):
        labels = stored if isinstance(stored, list) else []
        labels = [str(label)[:24] for label in labels[:BUTTON_COUNT]]
        return labels + default[len(labels):]
    if isinstance(default, bool):
        return stored if isinstance(stored, bool) else default
    if isinstance(default, (int, float)):
        if isinstance(stored, bool) or not isinstance(stored, (int, float)):
            return default
        if not math.isfinite(stored):
            return default
        low, high = LIMITS.get(path, (float("-inf"), float("inf")))
        value = min(max(stored, low), high)
        return int(value) if isinstance(default, int) and float(value).is_integer() else value
    if isinstance(default, str):
        allowed = {("wheel", "steerMode"): STEER_MODES, ("lever", "mode"): LEVER_MODES}.get(path)
        if path[:2] == ("pedals", "modes"):
            allowed = PEDAL_MODES
        if not isinstance(stored, str) or (allowed and stored not in allowed):
            return default
        return stored
    return default


def sanitize(stored) -> dict:
    config = _merge(DEFAULTS, stored)
    # Full press must stay beyond the dead zone.
    for section, full, dead in (("wheel", "tiltSteerFull", "tiltSteerDeadzone"),
                                ("pedals", "tiltFull", "tiltDeadzone")):
        if config[section][dead] >= config[section][full]:
            config[section][dead] = max(config[section][full] - 1, 0)
    return config


def load(path: Path | None = None) -> dict:
    path = path or config_path()
    try:
        return sanitize(json.loads(path.read_text()))
    except (OSError, ValueError):
        return sanitize({})


def save(config: dict, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


# Settings a phone may change itself (the quick switch in the wheel's hub), with their values.
PHONE_SETTABLE = {("wheel", "steerMode"): STEER_MODES}


def phone_change(request) -> tuple[tuple, object] | None:
    """A phone's {"path": [...], "value": ...} request, if it is one phones may make."""
    if not isinstance(request, dict) or not isinstance(request.get("path"), list):
        return None
    path = tuple(request["path"])
    allowed = PHONE_SETTABLE.get(path)
    value = request.get("value")
    return (path, value) if allowed and value in allowed else None


def apply_change(config: dict, path: tuple, value) -> dict:
    changed = copy.deepcopy(config)
    target = changed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return sanitize(changed)


def for_phones(config: dict) -> dict:
    """The part of the settings the phone page uses."""
    return {key: value for key, value in config.items() if key != "server"}
