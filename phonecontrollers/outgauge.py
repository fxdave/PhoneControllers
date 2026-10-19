"""OutGauge telemetry: the UDP dashboard packets Live for Speed, BeamNG.drive and others send.

Shows what the game itself sees (its throttle, brake and clutch) next to the phone's pedals,
plus gear, speed and RPM. Rigs of Rods only sends it on Windows.
"""

import asyncio
import struct

# Time, Car[4], Flags, Gear, PLID, Speed, RPM, Turbo, EngTemp, Fuel, OilPressure, OilTemp,
# DashLights, ShowLights, Throttle, Brake, Clutch, Display1[16], Display2[16] (+ optional ID).
PACKET = struct.Struct("<I4sHBB7f2I3f16s16s")

DL_SHIFT, DL_FULLBEAM, DL_HANDBRAKE = 1 << 0, 1 << 1, 1 << 2
DL_SIGNAL_L, DL_SIGNAL_R = 1 << 5, 1 << 6


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def parse(data: bytes) -> dict | None:
    if len(data) < PACKET.size:
        return None
    (_time, car, _flags, gear, _plid, speed, rpm, _turbo, _engine_temp, fuel, _oil_pressure,
     _oil_temp, _available, lights, throttle, brake, clutch, display1,
     _display2) = PACKET.unpack_from(data)
    # OutGauge gears: 0 reverse, 1 neutral, 2 first...
    gear_name = "R" if gear == 0 else "N" if gear == 1 else str(gear - 1)

    def unit(value: float) -> float:
        return round(min(max(value, 0.0), 1.0), 3)

    return {
        "car": _text(car),
        "name": _text(display1),
        "gear": gear_name,
        "speed": round(speed * 3.6, 1),  # km/h
        "rpm": round(rpm),
        "fuel": unit(fuel),
        "t": unit(throttle),
        "b": unit(brake),
        "c": unit(clutch),
        "lights": {
            "shift": bool(lights & DL_SHIFT),
            "beam": bool(lights & DL_FULLBEAM),
            "handbrake": bool(lights & DL_HANDBRAKE),
            "left": bool(lights & DL_SIGNAL_L),
            "right": bool(lights & DL_SIGNAL_R),
        },
    }


class OutGaugeProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet

    def datagram_received(self, data: bytes, _address) -> None:
        packet = parse(data)
        if packet:
            self.on_packet(packet)
