"""Serves the phone page and feeds the virtual joystick. Used by the desktop app and the
command line alike; everything runs on one asyncio loop."""

import asyncio
import contextlib
import hashlib
import itertools
import json
import os
import shutil
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import WSMsgType, web
from evdev import UInputError

from . import config as settings
from .inhibit import IdleInhibitor
from .device import (
    AXES, LOOK_RESET_BIT, ForceFeedback, LookAround, Wheel, create_device, look_target,
    parse_state,
)
from .outgauge import OutGaugeProtocol
from .watch import OpenWatcher, device_nodes, programs_using

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
# Certificates from before the settings moved out of the repo; reused so phones that already
# accepted one don't see the warning again.
OLD_CERT_DIR = ROOT / "certs"

# How often to check the page files for edits, in seconds.
WATCH_INTERVAL = 1.0
# Force feedback and the live monitor are sent at most this often, in seconds.
FORCE_INTERVAL = 1 / 30
MONITOR_INTERVAL = 1 / 20
# The look-around camera is steered this often, in seconds.
LOOK_INTERVAL = 1 / 60
# How long the game's camera reset button is held when the look direction is recentered.
LOOK_RESET_PRESS = 0.15
# After a game opens the device, resync the axes after these delays: the game may still be
# setting up when it opens it, and some open, close and reopen while probing.
RESYNC_DELAYS = (0.3, 1.5)

FIELD_NAMES = {"s": "steering", "t": "gas", "b": "brake", "c": "clutch", "k": "buttons",
               "l": "look around"}


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return settings.app_dir(Path(base))


def lan_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def ensure_certificate(address: str) -> tuple[Path, Path] | str:
    """Create a self-signed certificate once; browsers only expose tilt sensors over HTTPS.
    Returns the paths, or why there is no certificate."""
    cert_dir = data_dir() / "certs"
    cert, key = cert_dir / "cert.pem", cert_dir / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    cert_dir.mkdir(parents=True, exist_ok=True)
    old_cert, old_key = OLD_CERT_DIR / "cert.pem", OLD_CERT_DIR / "key.pem"
    if old_cert.exists() and old_key.exists():
        shutil.copy(old_cert, cert)
        shutil.copy(old_key, key)
        return cert, key
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "3650",
                "-subj", "/CN=Phone Controllers Wheel",
                "-addext", f"subjectAltName=IP:{address},IP:127.0.0.1,DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        return f"could not create a certificate with openssl: {error}"
    return cert, key


def page_version() -> str:
    """Changes whenever a file of the phone page is edited."""
    stamps = []
    for path in STATIC_DIR.rglob("*"):
        try:
            if path.is_file():
                stamps.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            continue  # deleted while scanning, like an editor's swap file
    return hashlib.sha1(repr(sorted(stamps)).encode()).hexdigest()[:12]


def describe_browser(user_agent: str) -> str:
    """A short name for a phone from its user agent, like "Chrome on Android 15"."""
    system = "Phone"
    for marker, name in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                         ("Linux", "Linux"), ("Windows", "Windows"), ("Mac OS", "Mac")):
        if marker in user_agent:
            system = name
            if marker == "Android":
                after = user_agent.split("Android", 1)[1].split(";")[0].strip()
                if after:
                    system = f"Android {after}"
            break
    browser = ""
    for marker, name in (("SamsungBrowser", "Samsung Internet"), ("Firefox", "Firefox"),
                         ("EdgA", "Edge"), ("Chrome", "Chrome"), ("Safari", "Safari")):
        if marker in user_agent:
            browser = name
            break
    return f"{browser} on {system}" if browser else system


@dataclass
class Phone:
    ws: web.WebSocketResponse
    address: str
    connected: float = field(default_factory=time.time)
    key: int = 0
    id: str = ""
    name: str = ""
    controls: list = field(default_factory=list)
    # Buttons (by number) this phone shows.
    buttons: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    rtt: float | None = None

    def describe(self) -> dict:
        return {
            "key": self.key,
            "id": self.id,
            "name": self.name or self.address,
            "address": self.address,
            "controls": self.controls,
            "buttons": self.buttons,
            "sends": [FIELD_NAMES[key] for key in self.state],
            "rtt": self.rtt,
            "connected": self.connected,
        }


class Server:
    """on_event(kind, data) reports what happens, for the desktop app: "status", "log",
    "error", "phones", "programs", "monitor", "game", and "set" (a phone changed a setting;
    the receiver stores it and calls set_config). It is called on the server's loop."""

    def __init__(self, config: dict, on_event=None):
        self.config = config
        self.on_event = on_event or (lambda kind, data: None)
        self.phones: dict[web.WebSocketResponse, Phone] = {}
        self.wheel: Wheel | None = None
        self.ff: ForceFeedback | None = None
        self.programs: list[dict] = []
        self.game: dict | None = None
        self.game_time = 0.0
        self.page = {"version": page_version()}
        self.status: dict = {"running": False}
        self._resync_handles: list[asyncio.TimerHandle] = []
        self._last_forces: dict | None = None
        self._phone_keys = itertools.count(1)
        self.inhibitor = IdleInhibitor(self.log)
        self._awake_lock = asyncio.Lock()
        self.look = LookAround()
        self._programs_task: asyncio.Future | None = None
        self._programs_dirty = False

    # ---------- reporting ----------

    def log(self, message: str) -> None:
        print(message, flush=True)
        self.on_event("log", message)

    def emit_phones(self) -> None:
        self.on_event("phones", [phone.describe() for phone in self.phones.values()])
        asyncio.ensure_future(self.keep_awake())

    async def keep_awake(self) -> None:
        """Phones aren't keyboard or mouse input: without this the screen saver kicks in, or
        the PC suspends, in the middle of a drive."""
        async with self._awake_lock:
            want = bool(self.phones) and self.config["server"]["keepAwake"] and \
                self.status.get("running", False)
            if want != self.inhibitor.active:
                await asyncio.to_thread(
                    self.inhibitor.inhibit if want else self.inhibitor.release,
                    *(["A phone is driving"] if want else []))

    async def send_all(self, payload: dict) -> None:
        for ws in list(self.phones):
            with contextlib.suppress(ConnectionError, RuntimeError):
                await ws.send_json(payload)

    # ---------- settings ----------

    def set_config(self, config: dict) -> None:
        """Apply new settings. Port and HTTPS changes only take effect on restart."""
        self.config = config
        if self.wheel:
            self.wheel.set_curves(config["curves"]["post"])
        asyncio.ensure_future(self.send_all({"config": settings.for_phones(config)}))
        asyncio.ensure_future(self.keep_awake())

    # ---------- the device ----------

    def resync(self, reason: str = "") -> None:
        if self.wheel:
            self.wheel.resync()
            if reason:
                self.log(f"resent all axes ({reason})")

    def schedule_resync(self) -> None:
        for handle in self._resync_handles:
            handle.cancel()
        loop = asyncio.get_running_loop()
        self._resync_handles = [loop.call_later(delay, self.resync) for delay in RESYNC_DELAYS]

    def device_opened(self) -> None:
        self.schedule_resync()

    def programs_changed(self, nodes: list[str]) -> None:
        """Rescan which programs have the device open; scans never overlap, so an older scan
        can't overwrite a newer one."""
        self._programs_dirty = True
        if self._programs_task is None or self._programs_task.done():
            self._programs_task = asyncio.ensure_future(self._scan_programs(nodes))

    async def _scan_programs(self, nodes: list[str]) -> None:
        while self._programs_dirty:
            self._programs_dirty = False
            await self.refresh_programs(nodes)

    async def refresh_programs(self, nodes: list[str]) -> None:
        programs = await asyncio.to_thread(programs_using, nodes)
        if programs == self.programs:
            return
        names = {program["name"] for program in programs}
        for program in programs:
            if program["pid"] not in {known["pid"] for known in self.programs}:
                self.log(f"{program['name']} (pid {program['pid']}) opened the device")
        for program in self.programs:
            if program["pid"] not in {known["pid"] for known in programs}:
                self.log(f"{program['name']} (pid {program['pid']}) closed the device")
        self.programs = programs
        self.on_event("programs", programs)
        await self.send_all({"programs": sorted(names)})

    def forces(self) -> dict:
        forces = self.ff.forces() if self.ff else {"force": 0.0, "spring": 0.0, "rumble": 0.0}
        ffb = self.config["ffb"]
        if not ffb["enabled"]:
            return {"force": 0.0, "spring": 0.0, "rumble": 0.0}
        gain = ffb["gain"]
        force = forces["force"] * gain * (-1 if ffb["invert"] else 1)
        return {
            "force": round(min(max(force, -1.0), 1.0), 3),
            "spring": round(min(forces["spring"] * gain, 1.0), 3),
            "rumble": round(min(forces["rumble"] * gain, 1.0), 3),
        }

    async def read_force_feedback(self, device) -> None:
        # A plain reader rather than evdev's async_read_loop, which raises from a callback
        # when its task is cancelled at shutdown.
        def ready() -> None:
            try:
                events = list(device.read())
            except BlockingIOError:
                return
            for event in events:
                try:
                    self.ff.handle(event)
                except OSError as error:
                    self.log(f"force feedback request failed: {error}")

        loop = asyncio.get_running_loop()
        loop.add_reader(device.fd, ready)
        try:
            await asyncio.Future()
        finally:
            loop.remove_reader(device.fd)

    async def send_forces(self) -> None:
        while True:
            await asyncio.sleep(FORCE_INTERVAL)
            forces = self.forces()
            if forces != self._last_forces:
                self._last_forces = forces
                await self.send_all({"ff": forces})

    async def send_monitor(self) -> None:
        last = None
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            raw = {}
            for phone in self.phones.values():
                for key, value in phone.raw.items():
                    if abs(value) >= abs(raw.get(key, 0.0)):
                        raw[key] = value
            monitor = {
                "raw": raw,
                "shown": dict(self.wheel.merged),
                "output": {key: round(self.wheel.output(key), 4) for key in AXES},
                "buttons": self.wheel.buttons,
                "look": round(self.look.angle) if self.config["look"]["enabled"] else None,
                "ff": self._last_forces or self.forces(),
            }
            if monitor != last:
                last = monitor
                self.on_event("monitor", monitor)

    def game_packet(self, packet: dict) -> None:
        now = time.monotonic()
        first = self.game is None
        self.game = packet
        if first:
            self.log(f"receiving OutGauge telemetry ({packet['car'] or 'game'})")
        if now - self.game_time >= MONITOR_INTERVAL:
            self.game_time = now
            self.on_event("game", packet)
            asyncio.ensure_future(self.send_all({"game": packet}))

    # ---------- phones ----------

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=2.0)
        await ws.prepare(request)
        phone = Phone(ws=ws, address=request.remote or "?", key=next(self._phone_keys))
        phone.name = describe_browser(request.headers.get("User-Agent", ""))
        self.phones[ws] = phone
        self.log(f"phone connected: {phone.address}")
        self.emit_phones()
        sends = None
        try:
            await ws.send_json({"version": self.page["version"],
                                "config": settings.for_phones(self.config),
                                "programs": sorted({p["name"] for p in self.programs})})
            if self.game:
                await ws.send_json({"game": self.game})
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                    if not isinstance(data, dict):
                        raise ValueError("not an object")
                    if "hello" in data or "ping" in data:
                        await self.phone_info(phone, data)
                        continue
                    if "set" in data:
                        self.phone_setting(phone, data["set"])
                        continue
                    if "lookCenter" in data:
                        self.look_center()
                        continue
                    state = parse_state(data)
                    raw = data.get("r")
                    if isinstance(raw, dict):
                        phone.raw = {key: float(raw[key]) for key in AXES if key in raw}
                except (ValueError, TypeError, OverflowError) as error:
                    self.log(f"bad message from {phone.address}: {error}")
                    continue
                first = not phone.state and state
                phone.state = state
                self.wheel.update(ws, state)
                if set(state) != sends:
                    sends = set(state)
                    names = ", ".join(FIELD_NAMES[key] for key in state) or "nothing"
                    self.log(f"phone {phone.address} controls: {names}")
                    self.emit_phones()
                if first:
                    # The game may have opened the device before this phone came along.
                    self.resync()
        finally:
            self.phones.pop(ws, None)
            # Never leave the throttle stuck down when the phone drops off.
            self.wheel.remove(ws)
            self.log(f"phone disconnected: {phone.address}")
            self.emit_phones()
        return ws

    def phone_setting(self, phone: Phone, request) -> None:
        """A phone changed a setting (the hub's touch/gyro switch). Whoever owns the settings
        (the desktop app, or the command line's settings file) stores it and sends it back to
        every phone; on_event "set" is that hand-off."""
        change = settings.phone_change(request)
        if not change:
            self.log(f"phone {phone.address} asked to change a setting it can't: {request}")
            return
        path, value = change
        self.log(f"phone {phone.address} set {'.'.join(path)} to {value}")
        self.on_event("set", {"path": path, "value": value})

    async def phone_info(self, phone: Phone, data: dict) -> None:
        hello = data.get("hello")
        if isinstance(hello, dict):
            phone.id = str(hello.get("id", ""))[:40]
            controls = hello.get("controls")
            if isinstance(controls, list):
                phone.controls = [str(control) for control in controls][:16]
            buttons = hello.get("buttons")
            if isinstance(buttons, list):
                phone.buttons = sorted({b for b in buttons if isinstance(b, int)
                                        and not isinstance(b, bool) and 0 <= b < 32})
            self.emit_phones()
        if "ping" in data:
            await phone.ws.send_json({"pong": data["ping"]})
            rtt = data.get("rtt")
            if isinstance(rtt, (int, float)) and rtt != phone.rtt:
                phone.rtt = round(float(rtt))
                self.emit_phones()

    def show_on_phone(self, key: int, widget: str, shown: bool) -> None:
        """Shows or hides one control on one phone, chosen from the desktop app."""
        for phone in self.phones.values():
            if phone.key == key:
                asyncio.ensure_future(
                    phone.ws.send_json({"show": {"widget": widget, "shown": bool(shown)}}))

    # ---------- looking around ----------

    async def drive_look(self) -> None:
        """Turns the game's camera toward where the steering phone points."""
        last = time.monotonic()
        while True:
            await asyncio.sleep(LOOK_INTERVAL)
            now = time.monotonic()
            options = self.config["look"]
            yaw = self.wheel.look_target()
            target = look_target(yaw, options) if options["enabled"] and yaw is not None else 0.0
            self.wheel.set_look(self.look.step(target, now - last))
            last = now

    def look_center(self) -> None:
        """The phone's straight-ahead was set again: put the game's camera back too."""
        self.look.reset()
        # Until the phone's next state arrives, its last look angle is from before the reset.
        for state in self.wheel.phones.values():
            state.pop("l", None)
        self.wheel.set_look(0.0)
        if not self.config["look"]["enabled"]:
            return
        self.wheel.own_buttons |= LOOK_RESET_BIT
        self.wheel.emit()

        def release() -> None:
            self.wheel.own_buttons &= ~LOOK_RESET_BIT
            self.wheel.emit()

        asyncio.get_running_loop().call_later(LOOK_RESET_PRESS, release)

    # ---------- running ----------

    async def watch_page(self) -> None:
        """Tells every phone when the page changes, so it reloads itself."""
        while True:
            await asyncio.sleep(WATCH_INTERVAL)
            version = await asyncio.to_thread(page_version)
            if version == self.page["version"]:
                continue
            self.page["version"] = version
            self.log("page changed, reloading phones")
            await self.send_all({"version": version})

    def make_app(self, https_port: int | None) -> web.Application:
        app = web.Application()

        async def index_handler(_: web.Request) -> web.FileResponse:
            return web.FileResponse(STATIC_DIR / "index.html")

        async def info_handler(_: web.Request) -> web.Response:
            return web.json_response({"https_port": https_port})

        async def disable_caching(_: web.Request, response: web.StreamResponse) -> None:
            # The phone should always pick up the latest page after an edit.
            response.headers["Cache-Control"] = "no-cache"

        app.router.add_get("/", index_handler)
        app.router.add_get("/ws", self.websocket_handler)
        app.router.add_get("/info", info_handler)
        app.router.add_static("/static", STATIC_DIR)
        app.on_response_prepare.append(disable_caching)
        return app

    async def run(self, stop: asyncio.Event, host: str = "0.0.0.0") -> None:
        options = self.config["server"]
        address = lan_address()
        ssl_context = None
        if options["https"]:
            certificate = ensure_certificate(address)
            if isinstance(certificate, str):
                self.log(f"HTTPS disabled, {certificate}")
            else:
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ssl_context.load_cert_chain(*certificate)

        try:
            device = create_device()
        except (OSError, UInputError) as error:
            self.on_event("error", f"Can't create the virtual device: {error}. "
                                   "See \"If the app can't create the wheel\" in the README.")
            self.log(f"can't create the virtual device: {error}")
            return
        self.wheel = Wheel(device, self.config["curves"]["post"])
        self.ff = ForceFeedback(device)
        self.log(f"virtual device: {device.device.path} ({device.name})")

        https_port = options["httpsPort"] if ssl_context else None
        runner = web.AppRunner(self.make_app(https_port))
        await runner.setup()
        tasks: list[asyncio.Task] = []
        watcher = None
        transport = None
        try:
            try:
                await web.TCPSite(runner, host, options["port"]).start()
                if ssl_context:
                    await web.TCPSite(runner, host, https_port, ssl_context=ssl_context).start()
            except OSError as error:
                self.on_event("error", f"Can't listen on the port: {error}")
                self.log(f"can't listen: {error}")
                return

            loop = asyncio.get_running_loop()
            if options["outgauge"]:
                try:
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda: OutGaugeProtocol(self.game_packet),
                        local_addr=("0.0.0.0", options["outgaugePort"]))
                except OSError as error:
                    self.log(f"OutGauge disabled, can't listen on UDP {options['outgaugePort']}: "
                             f"{error}")

            nodes = device_nodes(device.device.path)
            try:
                watcher = OpenWatcher(
                    nodes, on_open=self.device_opened,
                    on_change=lambda: self.programs_changed(nodes))
            except OSError as error:
                self.log(f"can't watch the device for games opening it: {error}")
            await self.refresh_programs(nodes)

            tasks = [asyncio.create_task(coroutine) for coroutine in (
                self.watch_page(), self.read_force_feedback(device), self.send_forces(),
                self.send_monitor(), self.drive_look())]

            http_url = f"http://{address}:{options['port']}/"
            https_url = f"https://{address}:{https_port}/" if ssl_context else None
            self.status = {"running": True, "http": http_url, "https": https_url,
                           "device": device.device.path, "address": address}
            self.on_event("status", self.status)
            await stop.wait()
        finally:
            if self._programs_task:
                tasks.append(self._programs_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for handle in self._resync_handles:
                handle.cancel()
            if watcher:
                watcher.close()
            if transport:
                transport.close()
            for ws in list(self.phones):
                with contextlib.suppress(Exception):
                    await ws.close()
            await runner.cleanup()
            async with self._awake_lock:
                await asyncio.to_thread(self.inhibitor.release)
            device.close()
            self.status = {"running": False}
            self.on_event("status", self.status)
