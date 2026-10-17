#!/usr/bin/env python3
"""Phone racing wheel: serves the touch controller page and feeds a virtual uinput joystick."""

import argparse
import asyncio
import json
import socket
from pathlib import Path

from aiohttp import WSMsgType, web
from evdev import AbsInfo, UInput, ecodes

STATIC_DIR = Path(__file__).parent / "static"

STEER_MAX = 32767
PEDAL_MAX = 1023

# Joystick-range button codes, so udev tags the device ID_INPUT_JOYSTICK (not a gamepad).
BUTTON_CODES = [
    ecodes.BTN_TRIGGER, ecodes.BTN_THUMB, ecodes.BTN_THUMB2, ecodes.BTN_TOP,
    ecodes.BTN_TOP2, ecodes.BTN_PINKIE, ecodes.BTN_BASE, ecodes.BTN_BASE2,
    ecodes.BTN_BASE3, ecodes.BTN_BASE4, ecodes.BTN_BASE5, ecodes.BTN_BASE6,
]

# Wire field -> (axis code, max value)
AXES = {
    "s": (ecodes.ABS_X, STEER_MAX),   # steering, -1..1
    "t": (ecodes.ABS_Y, PEDAL_MAX),   # throttle, 0..1
    "b": (ecodes.ABS_Z, PEDAL_MAX),   # brake, 0..1
    "c": (ecodes.ABS_RZ, PEDAL_MAX),  # clutch, 0..1
}


def create_device() -> UInput:
    capabilities = {
        ecodes.EV_KEY: BUTTON_CODES,
        ecodes.EV_ABS: [
            (ecodes.ABS_X, AbsInfo(0, -STEER_MAX, STEER_MAX, 0, 0, 0)),
            (ecodes.ABS_Y, AbsInfo(0, 0, PEDAL_MAX, 0, 0, 0)),
            (ecodes.ABS_Z, AbsInfo(0, 0, PEDAL_MAX, 0, 0, 0)),
            (ecodes.ABS_RZ, AbsInfo(0, 0, PEDAL_MAX, 0, 0, 0)),
        ],
    }
    return UInput(
        capabilities,
        name="Phone Racing Wheel",
        vendor=0x1209,
        product=0x5257,
        version=1,
        bustype=ecodes.BUS_USB,
    )


class Wheel:
    """Holds the last state sent to the virtual device and only emits what changed."""

    def __init__(self, device: UInput):
        self.device = device
        self.axes = {code: 0 for code, _ in AXES.values()}
        self.buttons = 0

    def apply(self, state: dict) -> None:
        changed = False
        for field, (code, max_value) in AXES.items():
            if field not in state:
                continue
            low = -1.0 if field == "s" else 0.0
            value = round(min(max(float(state[field]), low), 1.0) * max_value)
            if value != self.axes[code]:
                self.device.write(ecodes.EV_ABS, code, value)
                self.axes[code] = value
                changed = True
        if "k" in state:
            buttons = int(state["k"])
            for index, code in enumerate(BUTTON_CODES):
                bit = 1 << index
                if (buttons ^ self.buttons) & bit:
                    self.device.write(ecodes.EV_KEY, code, 1 if buttons & bit else 0)
                    changed = True
            self.buttons = buttons
        if changed:
            self.device.syn()

    def release_all(self) -> None:
        self.apply({"s": 0, "t": 0, "b": 0, "c": 0, "k": 0})


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    wheel: Wheel = request.app["wheel"]
    ws = web.WebSocketResponse(heartbeat=2.0)
    await ws.prepare(request)
    peer = request.remote
    print(f"phone connected: {peer}")
    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                wheel.apply(json.loads(message.data))
            except (ValueError, TypeError) as error:
                print(f"bad message from {peer}: {error}")
    finally:
        # Never leave the throttle stuck down when the phone drops off.
        wheel.release_all()
        print(f"phone disconnected: {peer}")
    return ws


async def index_handler(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


def lan_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def print_connect_info(url: str) -> None:
    print(f"\nOpen on your phone: {url}\n")
    try:
        import qrcode

        code = qrcode.QRCode(border=1)
        code.add_data(url)
        code.print_ascii(invert=True)
    except ImportError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    device = create_device()
    print(f"virtual device: {device.device.path} ({device.name})")

    app = web.Application()
    app["wheel"] = Wheel(device)
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/static", STATIC_DIR)

    async def disable_caching(_: web.Request, response: web.StreamResponse) -> None:
        # The phone should always pick up the latest page after an edit.
        response.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(disable_caching)

    async def close_device(_: web.Application) -> None:
        device.close()

    app.on_cleanup.append(close_device)

    print_connect_info(f"http://{lan_address()}:{args.port}/")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
