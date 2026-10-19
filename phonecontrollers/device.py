"""The virtual joystick: merges the phones' controls into one uinput device and takes force
feedback from games."""

import ctypes
import fcntl
import math
import time

from evdev import AbsInfo, UInput, ecodes, ff

from .curves import Curve

DEVICE_NAME = "Phone Controllers Wheel"
STEER_MAX = 32767
PEDAL_MAX = 1023

# Joystick-range button codes, so udev tags the device ID_INPUT_JOYSTICK (not a gamepad).
BUTTON_CODES = [
    ecodes.BTN_TRIGGER, ecodes.BTN_THUMB, ecodes.BTN_THUMB2, ecodes.BTN_TOP,
    ecodes.BTN_TOP2, ecodes.BTN_PINKIE, ecodes.BTN_BASE, ecodes.BTN_BASE2,
    ecodes.BTN_BASE3, ecodes.BTN_BASE4, ecodes.BTN_BASE5, ecodes.BTN_BASE6,
    # Turn signals, added later: codes after the first twelve keep the earlier buttons' numbers.
    ecodes.BTN_TRIGGER_HAPPY1, ecodes.BTN_TRIGGER_HAPPY2,
    # Pressed by the PC itself when the look direction is recentered (Rigs of Rods: camera reset).
    ecodes.BTN_TRIGGER_HAPPY3,
]
LOOK_RESET_BIT = 1 << 14

# Wire field -> (axis code, max value)
AXES = {
    "s": (ecodes.ABS_X, STEER_MAX),   # steering, -1..1
    "t": (ecodes.ABS_Y, PEDAL_MAX),   # throttle, 0..1
    "b": (ecodes.ABS_Z, PEDAL_MAX),   # brake, 0..1
    "c": (ecodes.ABS_RZ, PEDAL_MAX),  # clutch, 0..1
}

# Looking around: how fast the game's camera turns, from full left to full right. A code after
# the pedals' keeps their axis numbers in games that bind by number.
LOOK_AXIS = ecodes.ABS_RUDDER
LOOK_MAX = 32767
# Degrees per second the Rigs of Rods camera turns with the axis fully out.
LOOK_SPEED = 50.0
# How quickly the camera catches up with where the phone points, in seconds.
LOOK_LAG = 0.12

# Effects a game may upload. Constant force is what wheel games (Rigs of Rods included) use
# for steering force; rumble and periodic effects become phone vibration.
FF_CODES = [
    ecodes.FF_CONSTANT, ecodes.FF_RAMP, ecodes.FF_SPRING, ecodes.FF_DAMPER,
    ecodes.FF_PERIODIC, ecodes.FF_SINE, ecodes.FF_SQUARE, ecodes.FF_TRIANGLE,
    ecodes.FF_SAW_UP, ecodes.FF_SAW_DOWN, ecodes.FF_RUMBLE, ecodes.FF_GAIN,
    ecodes.FF_AUTOCENTER,
]
MAX_EFFECTS = 16


def create_device() -> UInput:
    capabilities = {
        ecodes.EV_KEY: BUTTON_CODES,
        ecodes.EV_ABS: [
            (ecodes.ABS_X, AbsInfo(0, -STEER_MAX, STEER_MAX, 0, 0, 0)),
            (ecodes.ABS_Y, AbsInfo(0, 0, PEDAL_MAX, 0, 0, 0)),
            (ecodes.ABS_Z, AbsInfo(0, 0, PEDAL_MAX, 0, 0, 0)),
            (ecodes.ABS_RZ, AbsInfo(0, 0, PEDAL_MAX, 0, 0, 0)),
            (LOOK_AXIS, AbsInfo(0, -LOOK_MAX, LOOK_MAX, 0, 0, 0)),
        ],
        ecodes.EV_FF: FF_CODES,
    }
    return UInput(
        capabilities,
        name=DEVICE_NAME,
        vendor=0x1209,
        product=0x5257,
        version=2,
        bustype=ecodes.BUS_USB,
        max_effects=MAX_EFFECTS,
    )


def parse_state(raw: dict) -> dict:
    """Clamp the fields a phone sent; a missing field means that phone doesn't have the control."""
    state = {}
    for field in AXES:
        if field in raw:
            low = -1.0 if field == "s" else 0.0
            value = float(raw[field])
            if not math.isfinite(value):
                raise ValueError(f"{field} is not a number")
            state[field] = min(max(value, low), 1.0)
    if "k" in raw:
        state["k"] = int(raw["k"])
    if "l" in raw:
        look = float(raw["l"])
        if not math.isfinite(look):
            raise ValueError("l is not a number")
        state["l"] = min(max(look, -180.0), 180.0)
    return state


class Wheel:
    """Merges the controls of every connected phone into one device and only emits what changed.

    Each phone sends the full state of the controls it shows, as the phone displays them.
    Steering follows whichever phone turns furthest, each pedal whichever presses furthest,
    and buttons from all phones combine. The output curves then turn that into what the game
    gets.
    """

    def __init__(self, device: UInput, post_curves: dict):
        self.device = device
        self.phones: dict[object, dict] = {}
        self.axes = {code: 0 for code, _ in AXES.values()}
        self.buttons = 0
        self.merged = {field: 0.0 for field in AXES}
        # Buttons the PC presses itself, on top of the phones'.
        self.own_buttons = 0
        self.look = 0
        self.set_curves(post_curves)

    def set_curves(self, post_curves: dict) -> None:
        self.curves = {field: Curve(post_curves[field]) for field in AXES}
        if self.phones:
            self.emit()

    def update(self, phone: object, state: dict) -> None:
        self.phones[phone] = state
        self.emit()

    def remove(self, phone: object) -> None:
        self.phones.pop(phone, None)
        self.emit()

    def output(self, field: str) -> float:
        """What the game currently gets for an axis: -1..1 for steering, 0..1 for pedals."""
        code, max_value = AXES[field]
        return self.axes[code] / max_value

    def emit(self) -> None:
        changed = False
        for field, (code, max_value) in AXES.items():
            values = [state[field] for state in self.phones.values() if field in state]
            merged = max(values, key=abs if field == "s" else None, default=0.0)
            self.merged[field] = merged
            curve = self.curves[field]
            value = round((curve.signed(merged) if field == "s" else curve(merged)) * max_value)
            if value != self.axes[code]:
                self.device.write(ecodes.EV_ABS, code, value)
                self.axes[code] = value
                changed = True
        buttons = self.own_buttons
        for state in self.phones.values():
            buttons |= state.get("k", 0)
        for index, code in enumerate(BUTTON_CODES):
            bit = 1 << index
            if (buttons ^ self.buttons) & bit:
                self.device.write(ecodes.EV_KEY, code, 1 if buttons & bit else 0)
                changed = True
        self.buttons = buttons
        if changed:
            self.device.syn()

    def look_target(self) -> float | None:
        """Degrees the phone steering points away from center, or None when no phone sends it."""
        values = [state["l"] for state in self.phones.values() if "l" in state]
        return max(values, key=abs) if values else None

    def set_look(self, rate: float) -> None:
        value = round(min(max(rate, -1.0), 1.0) * LOOK_MAX)
        if value != self.look:
            self.device.write(ecodes.EV_ABS, LOOK_AXIS, value)
            self.device.syn()
            self.look = value

    def resync(self) -> None:
        """Make every reader see the current value of every axis again.

        A game only learns an axis value from an event, and the kernel drops events that
        repeat the current value. So an axis that never moved since the game opened the
        device (a brake nobody touched) keeps whatever the game assumed: many games, Rigs of
        Rods among them, assume the middle of the range, which is half brake. Nudging each
        axis by one step and back makes the kernel deliver the real value.
        """
        axes = [(code, max_value, self.axes[code]) for code, max_value in AXES.values()]
        axes.append((LOOK_AXIS, LOOK_MAX, self.look))
        for code, max_value, value in axes:
            self.device.write(ecodes.EV_ABS, code, value - 1 if value >= max_value else value + 1)
        self.device.syn()
        for code, _, value in axes:
            self.device.write(ecodes.EV_ABS, code, value)
        self.device.syn()


class LookAround:
    """Turns where the player wants to look into how fast to turn the game's camera.

    Rigs of Rods turns its camera at a speed set by an axis, and doesn't say where it points.
    So this keeps its own estimate of the camera angle and drives it toward the target, which
    stays right as long as both start from center: recentering also presses the game's camera
    reset.
    """

    def __init__(self, speed: float = LOOK_SPEED, lag: float = LOOK_LAG):
        self.speed = speed
        self.lag = lag
        self.angle = 0.0

    def reset(self) -> None:
        self.angle = 0.0

    def step(self, target: float, dt: float) -> float:
        """The axis value (-1..1) for the next dt seconds, positive turning toward +degrees."""
        rate = (target - self.angle) / (self.speed * self.lag)
        rate = min(max(rate, -1.0), 1.0)
        # Too small to move the camera visibly: hold still rather than creep.
        if abs(target - self.angle) < 0.5:
            rate = 0.0
        self.angle += rate * self.speed * dt
        return rate


def look_target(yaw: float, config: dict) -> float:
    """Camera angle for the steering phone turned yaw degrees from center."""
    beyond = max(abs(yaw) - config["deadzone"], 0.0)
    angle = min(beyond * config["gain"], 170.0)
    if config["invert"]:
        yaw = -yaw
    return math.copysign(angle, yaw) if angle else 0.0


def _ioctl_number(direction: int, number: int, struct_type) -> int:
    return (direction << 30) | (ctypes.sizeof(struct_type) << 16) | (ord("U") << 8) | number


# From linux/uinput.h. Called directly: python-evdev's begin_upload/begin_erase don't pass the
# request id to the kernel, so the kernel refuses them.
_WRITE, _READ_WRITE = 1, 3
UI_BEGIN_FF_UPLOAD = _ioctl_number(_READ_WRITE, 200, ff.UInputUpload)
UI_END_FF_UPLOAD = _ioctl_number(_WRITE, 201, ff.UInputUpload)
UI_BEGIN_FF_ERASE = _ioctl_number(_READ_WRITE, 202, ff.UInputErase)
UI_END_FF_ERASE = _ioctl_number(_WRITE, 203, ff.UInputErase)


def _request(device: UInput, number: int, struct) -> None:
    fcntl.ioctl(device.fd, number, struct, True)


class ForceFeedback:
    """Tracks the effects a game uploads and plays, and boils them down to what a phone can
    show: a steering force (-1..1, positive turns right), a centering spring (0..1) and a
    rumble strength (0..1)."""

    def __init__(self, device: UInput):
        self.device = device
        self.effects: dict[int, dict] = {}
        # effect id -> monotonic time it stops (math.inf while it plays until stopped)
        self.playing: dict[int, float] = {}
        self.gain = 1.0
        self.autocenter = 0.0

    def handle(self, event) -> bool:
        """Process one event read from the uinput device; True when the forces may change."""
        if event.type == ecodes.EV_UINPUT:
            if event.code == ecodes.UI_FF_UPLOAD:
                upload = ff.UInputUpload()
                upload.request_id = event.value
                _request(self.device, UI_BEGIN_FF_UPLOAD, upload)
                self.effects[upload.effect.id] = self._summarize(upload.effect)
                upload.retval = 0
                _request(self.device, UI_END_FF_UPLOAD, upload)
                return True
            if event.code == ecodes.UI_FF_ERASE:
                erase = ff.UInputErase()
                erase.request_id = event.value
                _request(self.device, UI_BEGIN_FF_ERASE, erase)
                self.effects.pop(erase.effect_id, None)
                self.playing.pop(erase.effect_id, None)
                erase.retval = 0
                _request(self.device, UI_END_FF_ERASE, erase)
                return True
            return False
        if event.type != ecodes.EV_FF:
            return False
        if event.code == ecodes.FF_GAIN:
            self.gain = event.value / 0xFFFF
        elif event.code == ecodes.FF_AUTOCENTER:
            self.autocenter = event.value / 0xFFFF
        elif event.value:
            effect = self.effects.get(event.code)
            length = effect["length"] if effect else 0
            self.playing[event.code] = time.monotonic() + length if length else math.inf
        else:
            self.playing.pop(event.code, None)
        return True

    @staticmethod
    def _summarize(effect) -> dict:
        # Direction is a full turn in 0..0xFFFF: 0x4000 points left, 0xC000 right. A wheel only
        # has one axis, so take the left/right part; straight up or down (which some games,
        # Rigs of Rods among them, use for a one-axis effect) counts as the level itself.
        angle = effect.direction / 0x10000 * 2 * math.pi
        sideways = -math.sin(angle)
        side = sideways if abs(sideways) > 0.2 else 1.0
        summary = {"type": effect.type, "length": effect.ff_replay.length / 1000, "force": 0.0,
                   "spring": 0.0, "rumble": 0.0}
        if effect.type == ecodes.FF_CONSTANT:
            summary["force"] = effect.u.ff_constant_effect.level / 0x7FFF * side
        elif effect.type == ecodes.FF_RAMP:
            ramp = effect.u.ff_ramp_effect
            summary["force"] = (ramp.start_level + ramp.end_level) / 2 / 0x7FFF * side
        elif effect.type in (ecodes.FF_SPRING, ecodes.FF_DAMPER):
            condition = effect.u.ff_condition_effect[0]
            coefficient = max(abs(condition.right_coeff), abs(condition.left_coeff)) / 0x7FFF
            if effect.type == ecodes.FF_SPRING:
                summary["spring"] = coefficient
        elif effect.type == ecodes.FF_PERIODIC:
            summary["rumble"] = abs(effect.u.ff_periodic_effect.magnitude) / 0x7FFF
        elif effect.type == ecodes.FF_RUMBLE:
            rumble = effect.u.ff_rumble_effect
            summary["rumble"] = max(rumble.strong_magnitude, rumble.weak_magnitude) / 0xFFFF
        return summary

    def forces(self) -> dict:
        now = time.monotonic()
        for effect_id, until in list(self.playing.items()):
            if until <= now:
                del self.playing[effect_id]
        force = spring = rumble = 0.0
        for effect_id in self.playing:
            effect = self.effects.get(effect_id)
            if not effect:
                continue
            force += effect["force"]
            spring = max(spring, effect["spring"])
            rumble = max(rumble, effect["rumble"])
        spring = max(spring, self.autocenter)

        def scaled(value: float) -> float:
            return round(value * self.gain, 3)

        return {
            "force": min(max(scaled(force), -1.0), 1.0),
            "spring": min(scaled(spring), 1.0),
            "rumble": min(scaled(rumble), 1.0),
        }

    def reset(self) -> None:
        self.effects.clear()
        self.playing.clear()
        self.gain = 1.0
        self.autocenter = 0.0
