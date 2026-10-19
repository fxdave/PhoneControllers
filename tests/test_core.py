import json
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from evdev import ecodes

from phonecontrollers import config, curves, outgauge
from phonecontrollers.device import (
    LOOK_AXIS, PEDAL_MAX, ForceFeedback, LookAround, Wheel, look_target, parse_state,
)

ROOT = Path(__file__).resolve().parent.parent


class FakeDevice:
    def __init__(self):
        self.events = []

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append("syn")


# ---------- curves ----------

def test_linear_curve_is_identity():
    curve = curves.Curve(curves.PRESETS["linear"])
    for x in (0, 0.1, 0.5, 0.93, 1):
        assert curve(x) == pytest.approx(x)


def test_curve_passes_through_points_and_stays_monotone():
    points = curves.PRESETS["clutch bite"]
    curve = curves.Curve(points)
    for x, y in points:
        assert curve(x) == pytest.approx(y, abs=1e-9)
    samples = [curve(i / 200) for i in range(201)]
    assert all(b >= a - 1e-12 for a, b in zip(samples, samples[1:]))
    # Close to the formula it was sampled from.
    for x in (0.1, 0.3, 0.6):
        assert curve(x) == pytest.approx(1 - (1 - x) ** 4, abs=0.02)


def test_flat_section_stays_flat():
    curve = curves.Curve(curves.PRESETS["dead zone 10%"])
    assert curve(0.05) == 0
    assert curve(0.55) == pytest.approx(0.5)


def test_normalize_pins_ends_and_sorts():
    assert curves.normalize([[0.5, 0.2], [0.2, 2], [0.9, -1]]) == [[0, 1], [0.5, 0.2], [1, 0]]
    assert curves.normalize([]) == [[0, 0], [1, 1]]
    assert curves.normalize([[0, 0], [0.995, 1], [1, 1]]) == [[0, 0], [1, 1]]


def test_signed_curve_mirrors_steering():
    curve = curves.Curve(curves.PRESETS["gentle start"])
    assert curve.signed(-0.5) == pytest.approx(-curve(0.5))
    # A curve starting above 0 lifts small turns but leaves center at center.
    lifted = curves.Curve([[0, 0.2], [1, 1]])
    assert lifted.signed(0.0) == 0 and lifted.signed(-0.0) == 0
    assert lifted.signed(-0.5) == pytest.approx(-0.6)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_phone_curves_match():
    """static/curves.js must give the same values as curves.py."""
    script = """
    const fs = require("fs"); const vm = require("vm");
    vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
    const presets = JSON.parse(process.argv[2]);
    const out = {};
    for (const [name, points] of Object.entries(presets)) {
      const curve = PhoneControllersCurves.build(points);
      out[name] = Array.from({length: 41}, (_, i) => curve(i / 40));
    }
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "static" / "curves.js"), json.dumps(curves.PRESETS)],
        check=True, capture_output=True, text=True)
    phone = json.loads(result.stdout)
    for name, points in curves.PRESETS.items():
        curve = curves.Curve(points)
        assert phone[name] == pytest.approx([curve(i / 40) for i in range(41)], abs=1e-9), name


# ---------- settings ----------

def test_sanitize_fills_defaults_and_clamps():
    stored = {
        "wheel": {"range": 99999, "steerMode": "bogus", "autocenter": 1},
        "pedals": {"modes": {"b": "away"}, "tiltFull": 10, "tiltDeadzone": 20},
        "curves": {"post": {"b": [[0, 0], [0.5, 1], [1, 1]], "t": "junk"}},
        "buttons": ["A", "B"],
    }
    result = config.sanitize(stored)
    assert result["wheel"]["range"] == 2520
    assert result["wheel"]["steerMode"] == "touch"
    assert result["wheel"]["autocenter"] is False
    assert result["pedals"]["modes"] == {"c": "touch", "b": "away", "t": "touch"}
    assert result["pedals"]["tiltDeadzone"] == 9
    assert result["curves"]["post"]["b"] == [[0, 0], [0.5, 1], [1, 1]]
    assert result["curves"]["post"]["t"] == curves.PRESETS["linear"]
    assert result["buttons"][:3] == ["A", "B", "Handbrake"]
    assert len(result["buttons"]) == config.BUTTON_COUNT


def test_sanitize_rejects_non_finite_numbers():
    result = config.sanitize({"wheel": {"range": float("nan")}, "ffb": {"gain": float("inf")}})
    assert result["wheel"]["range"] == 900 and result["ffb"]["gain"] == 1.0


def test_phones_may_only_switch_steering_mode():
    assert config.phone_change({"path": ["wheel", "steerMode"], "value": "gyro"}) == (
        ("wheel", "steerMode"), "gyro")
    assert config.phone_change({"path": ["wheel", "steerMode"], "value": "bogus"}) is None
    assert config.phone_change({"path": ["server", "port"], "value": 1}) is None
    assert config.phone_change("junk") is None
    assert config.phone_change({"path": ["look", "enabled"], "value": True}) == (
        ("look", "enabled"), True)
    assert config.phone_change({"path": ["look", "enabled"], "value": 1}) is None
    changed = config.apply_change(config.sanitize({}), ("wheel", "steerMode"), "gyro")
    assert changed["wheel"]["steerMode"] == "gyro"


def test_camera_pedal_mode_is_kept():
    assert config.sanitize({"pedals": {"modes": {"b": "camera"}}})["pedals"]["modes"]["b"] == "camera"


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "config.json"
    stored = config.sanitize({"wheel": {"range": 540}})
    config.save(stored, path)
    assert config.load(path) == stored
    assert "server" not in config.for_phones(stored)


# ---------- the device ----------

def make_wheel(post=None):
    device = FakeDevice()
    post_curves = dict(config.DEFAULTS["curves"]["post"], **(post or {}))
    return Wheel(device, post_curves), device


def test_wheel_merges_phones_and_applies_output_curve():
    wheel, device = make_wheel({"c": curves.PRESETS["linear"]})
    wheel.update("a", parse_state({"s": -0.5, "t": 0.2}))
    wheel.update("b", parse_state({"s": 0.25, "t": 0.6, "k": 4}))
    assert wheel.output("s") == pytest.approx(-0.5, abs=1e-4)
    assert wheel.output("t") == pytest.approx(0.6, abs=1e-3)
    assert (ecodes.EV_KEY, ecodes.BTN_THUMB2, 1) in device.events
    wheel.remove("b")
    assert wheel.output("t") == pytest.approx(0.2, abs=1e-3)


def test_clutch_bite_is_applied_on_the_pc():
    wheel, _ = make_wheel()
    wheel.update("a", parse_state({"c": 0.5}))
    assert wheel.output("c") == pytest.approx(1 - 0.5**4, abs=0.01)


def test_resync_nudges_every_axis_and_restores_it():
    wheel, device = make_wheel()
    wheel.update("a", parse_state({"t": 1}))
    device.events.clear()
    wheel.resync()
    nudged = {code: value for etype, code, value in device.events[:5]}
    assert nudged[ecodes.ABS_Z] == 1          # brake at 0 goes up a step
    assert nudged[ecodes.ABS_Y] == PEDAL_MAX - 1  # gas at full goes down a step
    assert nudged[ecodes.ABS_X] == 1
    assert nudged[LOOK_AXIS] == 1
    assert device.events[5] == "syn"
    restored = {code: value for etype, code, value in device.events[6:11]}
    assert restored == {ecodes.ABS_X: 0, ecodes.ABS_Y: PEDAL_MAX, ecodes.ABS_Z: 0,
                        ecodes.ABS_RZ: 0, LOOK_AXIS: 0}
    assert device.events[-1] == "syn"


def test_look_follows_the_phone_and_settles():
    look = LookAround()
    rates = [look.step(90.0, 1 / 60) for _ in range(240)]
    assert rates[0] == 1.0                    # far off: full speed
    assert look.angle == pytest.approx(90.0, abs=0.5)
    assert rates[-1] == 0.0                   # arrived: the camera holds still
    look.reset()
    assert look.step(0.0, 1 / 60) == 0.0


def test_look_target_dead_zone_gain_and_invert():
    options = {"gain": 3.0, "deadzone": 5, "invert": False}
    assert look_target(4.0, options) == 0.0
    assert look_target(15.0, options) == pytest.approx(30.0)
    assert look_target(-15.0, options) == pytest.approx(-30.0)
    assert look_target(200.0, options) == 170.0
    assert look_target(15.0, {**options, "invert": True}) == pytest.approx(-30.0)


def test_parse_state_reads_look():
    assert parse_state({"l": 400})["l"] == 180.0
    with pytest.raises(ValueError):
        parse_state({"l": float("inf")})


def test_parse_state_rejects_nan():
    with pytest.raises(ValueError):
        parse_state({"t": float("nan")})
    assert parse_state({"s": -3, "b": 2})["s"] == -1


def effect(effect_type, direction=0x8000, length=0, **union):
    u = SimpleNamespace(
        ff_constant_effect=SimpleNamespace(level=union.get("level", 0)),
        ff_ramp_effect=SimpleNamespace(start_level=0, end_level=0),
        ff_condition_effect=[SimpleNamespace(right_coeff=union.get("coeff", 0),
                                             left_coeff=union.get("coeff", 0))],
        ff_periodic_effect=SimpleNamespace(magnitude=union.get("magnitude", 0)),
        ff_rumble_effect=SimpleNamespace(strong_magnitude=union.get("strong", 0),
                                         weak_magnitude=0),
    )
    return SimpleNamespace(type=effect_type, direction=direction,
                           ff_replay=SimpleNamespace(length=length), u=u)


def play(force_feedback, effect_id, summary_effect):
    force_feedback.effects[effect_id] = ForceFeedback._summarize(summary_effect)
    force_feedback.handle(SimpleNamespace(type=ecodes.EV_FF, code=effect_id, value=1))


def test_force_feedback_constant_spring_rumble():
    force_feedback = ForceFeedback(device=None)
    # Straight up (what Rigs of Rods sends for its one-axis force): the level is the force.
    play(force_feedback, 0, effect(ecodes.FF_CONSTANT, level=0x7FFF // 2))
    assert force_feedback.forces()["force"] == pytest.approx(0.5, abs=0.01)
    # Pointing left counts as a leftward (negative) force.
    play(force_feedback, 1, effect(ecodes.FF_CONSTANT, direction=0x4000, level=0x7FFF // 4))
    assert force_feedback.forces()["force"] == pytest.approx(0.25, abs=0.01)
    play(force_feedback, 2, effect(ecodes.FF_SPRING, coeff=0x7FFF))
    play(force_feedback, 3, effect(ecodes.FF_RUMBLE, strong=0xFFFF))
    forces = force_feedback.forces()
    assert forces["spring"] == 1 and forces["rumble"] == 1
    force_feedback.handle(SimpleNamespace(type=ecodes.EV_FF, code=ecodes.FF_GAIN, value=0x7FFF))
    assert force_feedback.forces()["rumble"] == pytest.approx(0.5, abs=0.01)
    force_feedback.handle(SimpleNamespace(type=ecodes.EV_FF, code=3, value=0))
    assert force_feedback.forces()["rumble"] == 0


def test_force_feedback_effect_with_length_ends(monkeypatch):
    force_feedback = ForceFeedback(device=None)
    now = [100.0]
    monkeypatch.setattr("phonecontrollers.device.time.monotonic", lambda: now[0])
    play(force_feedback, 0, effect(ecodes.FF_RUMBLE, length=200, strong=0xFFFF))
    assert force_feedback.forces()["rumble"] == 1
    now[0] += 0.3
    assert force_feedback.forces()["rumble"] == 0


# ---------- OutGauge ----------

def test_outgauge_parse():
    packet = outgauge.PACKET.pack(
        1, b"XRT\0", 0, 3, 0, 20.0, 4500.0, 0, 0, 0.5, 0, 0, 0, 1 << 2, 0.8, 0.1, 1.5,
        b"Car name\0".ljust(16, b"\0"), b"\0" * 16)
    parsed = outgauge.parse(packet + struct.pack("<i", 0))
    assert parsed["gear"] == "2"
    assert parsed["speed"] == 72.0
    assert parsed["rpm"] == 4500
    assert (parsed["t"], parsed["b"], parsed["c"]) == (0.8, 0.1, 1.0)
    assert parsed["name"] == "Car name"
    assert parsed["lights"]["handbrake"]
    assert outgauge.parse(b"short") is None
