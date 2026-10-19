"""End to end: a fake phone over the websocket, a fake game on the real uinput device."""

import asyncio
import os
import sys

import aiohttp
import pytest
from evdev import InputDevice, ecodes

from phonecontrollers import config
from phonecontrollers.server import Server

pytestmark = pytest.mark.skipif(not os.access("/dev/uinput", os.W_OK),
                                reason="needs write access to /dev/uinput")


# Uploads a rightward constant force at half strength, plays it, then stops and erases it.
GAME_SCRIPT = """
import sys, time
from evdev import InputDevice, ecodes, ff
game = InputDevice(sys.argv[1])
effect = ff.Effect(ecodes.FF_CONSTANT, -1, 0xC000, ff.Trigger(0, 0), ff.Replay(0, 0),
                   ff.EffectType(ff_constant_effect=ff.Constant(0x4000, ff.Envelope())))
effect_id = game.upload_effect(effect)
game.write(ecodes.EV_FF, effect_id, 1)
time.sleep(0.5)
game.write(ecodes.EV_FF, effect_id, 0)
game.erase_effect(effect_id)
print("done")
"""


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def started_server(events):
    settings = config.sanitize({"server": {"port": free_port(), "https": False,
                                           "outgauge": False}})
    started = asyncio.Event()

    def on_event(kind, data):
        events.append((kind, data))
        if kind == "status" and data.get("running"):
            started.set()

    server = Server(settings, on_event)
    stop = asyncio.Event()
    task = asyncio.create_task(server.run(stop, host="127.0.0.1"))
    await asyncio.wait_for(started.wait(), 5)
    return server, stop, task


async def read_until(ws, key, timeout=3.0):
    async with asyncio.timeout(timeout):
        while True:
            message = await ws.receive_json()
            if key in message:
                return message


def drain(device):
    events = []
    while (event := device.read_one()) is not None:
        events.append(event)
    return events


def test_phone_to_game_round_trip():
    async def scenario():
        events = []
        server, stop, task = await started_server(events)
        url = f"http://127.0.0.1:{server.config['server']['port']}/ws"
        try:
            async with aiohttp.ClientSession() as session, session.ws_connect(url) as ws:
                first = await read_until(ws, "config")
                assert first["config"]["curves"]["post"]["c"][0] == [0, 0]
                await ws.send_json({"hello": {"id": "abc", "controls": ["b", "t"]}})
                await ws.send_json({"t": 0.5, "b": 0, "c": 0.5, "r": {"t": 0.5}})
                await asyncio.sleep(0.2)
                assert server.wheel.output("t") == pytest.approx(0.5, abs=0.002)
                # The clutch output curve is applied on the PC.
                assert server.wheel.output("c") == pytest.approx(1 - 0.5**4, abs=0.01)

                # A game opening the device gets every axis resent, the untouched brake too.
                game = InputDevice(server.status["device"])
                try:
                    await asyncio.sleep(0.6)
                    seen = drain(game)
                    brake = [e.value for e in seen if e.type == ecodes.EV_ABS
                             and e.code == ecodes.ABS_Z]
                    assert brake[-2:] == [1, 0]

                    # Force feedback: a constant force a game plays reaches the phone. The game
                    # runs as its own process: an upload blocks until the server answers, and
                    # python-evdev holds the interpreter lock while it waits.
                    player = await asyncio.create_subprocess_exec(
                        sys.executable, "-c", GAME_SCRIPT, server.status["device"],
                        stdout=asyncio.subprocess.PIPE)
                    message = await read_until(ws, "ff", timeout=10)
                    while message["ff"]["force"] == 0:
                        message = await read_until(ws, "ff", timeout=10)
                    assert message["ff"]["force"] == pytest.approx(0.5, abs=0.01)
                    output, _ = await asyncio.wait_for(player.communicate(), 10)
                    assert player.returncode == 0 and output.strip() == b"done"
                finally:
                    game.close()

                # The hub's quick switch: handed to whoever owns the settings.
                await ws.send_json({"set": {"path": ["wheel", "steerMode"], "value": "gyro"}})
                await ws.send_json({"set": {"path": ["server", "port"], "value": 1}})
                await asyncio.sleep(0.2)
                sets = [data for kind, data in events if kind == "set"]
                assert sets == [{"path": ("wheel", "steerMode"), "value": "gyro"}]

                phones = [data for kind, data in events if kind == "phones"][-1]
                assert phones[0]["id"] == "abc"
                assert phones[0]["controls"] == ["b", "t"]

                # The desktop app picks what a phone shows.
                await ws.send_json({"hello": {"id": "abc", "controls": ["b", "t"],
                                              "buttons": [2, 3, "x", 99]}})
                await asyncio.sleep(0.1)
                phones = [data for kind, data in events if kind == "phones"][-1]
                assert phones[0]["buttons"] == [2, 3]
                server.show_on_phone(phones[0]["key"], "button-4", True)
                assert (await read_until(ws, "show"))["show"] == {"widget": "button-4",
                                                                  "shown": True}

                # Looking around: the steering phone turned right turns the camera right
                # until it gets there, and recentering presses the camera reset.
                server.config["look"]["enabled"] = True
                await ws.send_json({"l": 20.0})
                await asyncio.sleep(0.1)
                assert server.wheel.look > 0
                await asyncio.sleep(1.5)
                assert server.look.angle == pytest.approx(45.0, abs=1.0)
                assert server.wheel.look == 0
                await ws.send_json({"lookCenter": True})
                await asyncio.sleep(0.05)
                assert server.wheel.buttons & (1 << 14)
                assert server.look.angle == 0
                await asyncio.sleep(0.3)
                assert not server.wheel.buttons & (1 << 14)
            await asyncio.sleep(0.2)
            # The phone left: nothing stays pressed.
            assert server.wheel.output("t") == 0
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
