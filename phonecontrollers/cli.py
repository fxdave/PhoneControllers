"""Phone Controllers without the desktop app: serves the phone page and feeds a virtual
uinput joystick. Settings come from the same file the desktop app writes, and edits to that
file apply while running."""

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from . import config as settings
from .server import Server

CONFIG_CHECK_INTERVAL = 1.0


def print_qr(url: str) -> None:
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
    parser.add_argument("--port", type=int, help="HTTP port (default from the settings, 8765)")
    parser.add_argument("--https-port", type=int, help="HTTPS port (default 8766)")
    parser.add_argument("--no-https", action="store_true", help="serve plain HTTP only")
    parser.add_argument("--config", help=f"settings file (default {settings.config_path()})")
    args = parser.parse_args()
    # Show connect/disconnect lines immediately even when output goes to a file or pipe.
    sys.stdout.reconfigure(line_buffering=True)

    path = Path(args.config) if args.config else settings.config_path()

    def load() -> dict:
        config = settings.load(path)
        if args.port:
            config["server"]["port"] = args.port
        if args.https_port:
            config["server"]["httpsPort"] = args.https_port
        if args.no_https:
            config["server"]["https"] = False
        return config

    def on_event(kind: str, data) -> None:
        if kind == "status" and data.get("running"):
            print(f"\nOpen on your phone: {data['http']}")
            if data.get("https"):
                print(f"With tilt sensors:  {data['https']}  (accept the certificate warning once)\n")
                print_qr(data["https"])
            else:
                print()
                print_qr(data["http"])
        elif kind == "error":
            print(data, file=sys.stderr)
        elif kind == "set":
            # Store it in the settings file; the file watcher then applies it everywhere.
            stored = settings.load(path)
            settings.save(settings.apply_change(stored, data["path"], data["value"]), path)

    async def run() -> None:
        server = Server(load(), on_event)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)

        async def watch_config() -> None:
            def stamp():
                try:
                    return path.stat().st_mtime_ns
                except OSError:
                    return None

            last = stamp()
            while True:
                await asyncio.sleep(CONFIG_CHECK_INTERVAL)
                current = stamp()
                if current != last:
                    last = current
                    previous = server.config["server"]
                    server.set_config(load())
                    print(f"settings reloaded from {path}")
                    if server.config["server"] != previous:
                        print("server settings changed: restart to apply them")

        watcher = asyncio.create_task(watch_config())
        try:
            await server.run(stop, host=args.host)
        finally:
            watcher.cancel()

    asyncio.run(run())
