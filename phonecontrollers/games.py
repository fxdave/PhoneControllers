"""Per-game setup the desktop app can do with one click."""

import re
from pathlib import Path

from .server import ROOT

ROR_CONFIG_DIR = Path.home() / ".rigsofrods" / "config"
ROR_MAP = ROOT / "games" / "rigs-of-rods" / "Phone_Controllers_Wheel.map"


def ror_found() -> bool:
    return ROR_CONFIG_DIR.is_dir()


def ror_map_state() -> str:
    """"installed", "outdated" (an older copy of the map) or "missing"."""
    target = ROR_CONFIG_DIR / ROR_MAP.name
    try:
        return "installed" if target.read_text() == ROR_MAP.read_text() else "outdated"
    except OSError:
        return "missing"


def ror_force_feedback_on() -> bool:
    try:
        text = (ROR_CONFIG_DIR / "RoR.cfg").read_text()
    except OSError:
        return False
    return re.search(r"^Force Feedback=Yes\s*$", text, re.MULTILINE) is not None


def install_ror_map() -> Path:
    """Rigs of Rods ignores a joystick until a mapping file named after it exists."""
    target = ROR_CONFIG_DIR / ROR_MAP.name
    target.write_text(ROR_MAP.read_text())
    return target


def enable_ror_force_feedback() -> None:
    """Turn force feedback on in RoR.cfg. Rigs of Rods leaves every gain at 0, which sends no
    force at all, so gains still at 0 get a starting value (tune them in the game's settings).
    The game rewrites RoR.cfg when it exits, so it should be closed first."""
    path = ROR_CONFIG_DIR / "RoR.cfg"
    text = path.read_text() if path.exists() else ""
    wanted = {"Force Feedback": "Yes"}
    starting_gains = {"Force Feedback Gain": "1.000000", "Force Feedback Stress": "1.000000",
                      "Force Feedback Centering": "0.500000"}
    for key, value in starting_gains.items():
        match = re.search(rf"^{re.escape(key)}=(.*)$", text, re.MULTILINE)
        try:
            current = float(match.group(1)) if match else 0.0
        except ValueError:
            current = 0.0
        if current == 0:
            wanted[key] = value
    for key, value in wanted.items():
        line = f"{key}={value}"
        text, count = re.subn(rf"^{re.escape(key)}=.*$", line, text, flags=re.MULTILINE)
        if not count:
            text = text + ("" if not text or text.endswith("\n") else "\n") + line + "\n"
    path.write_text(text)
