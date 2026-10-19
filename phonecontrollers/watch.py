"""Notices when a game opens the virtual device, and which programs have it open."""

import asyncio
import ctypes
import os
from pathlib import Path

IN_OPEN = 0x20
IN_CLOSE_WRITE = 0x08
IN_CLOSE_NOWRITE = 0x10
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

_libc = ctypes.CDLL(None, use_errno=True)


def device_nodes(event_path: str) -> list[str]:
    """The evdev node plus the legacy joystick node (/dev/input/jsN) of the same device."""
    nodes = [event_path]
    sysfs = Path("/sys/class/input") / Path(event_path).name / "device"
    for js in sorted(sysfs.glob("js*")):
        nodes.append(f"/dev/input/{js.name}")
    return nodes


class OpenWatcher:
    """Calls on_open whenever another program opens one of the nodes, and on_change on any
    open or close. Uses inotify, which reports opens of device files too."""

    def __init__(self, paths: list[str], on_open, on_change):
        self.on_open = on_open
        self.on_change = on_change
        self.fd = _libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.paths = []
        for path in paths:
            mask = IN_OPEN | IN_CLOSE_WRITE | IN_CLOSE_NOWRITE
            if _libc.inotify_add_watch(self.fd, os.fsencode(path), mask) >= 0:
                self.paths.append(path)
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.fd, self._read)

    def _read(self) -> None:
        opened = False
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        offset = 0
        while offset + 16 <= len(data):
            # struct inotify_event { int wd; uint32_t mask, cookie, len; char name[]; }
            mask = int.from_bytes(data[offset + 4:offset + 8], "little")
            length = int.from_bytes(data[offset + 12:offset + 16], "little")
            opened |= bool(mask & IN_OPEN)
            offset += 16 + length
        if opened:
            self.on_open()
        self.on_change()

    def close(self) -> None:
        self.loop.remove_reader(self.fd)
        os.close(self.fd)


def programs_using(paths: list[str]) -> list[dict]:
    """Programs (other than this one) that have any of the nodes open, by scanning /proc."""
    targets = set(paths)
    own = os.getpid()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        try:
            for fd in (entry / "fd").iterdir():
                if os.readlink(fd) in targets:
                    name = (entry / "comm").read_text().strip()
                    found.append({"pid": int(entry.name), "name": name})
                    break
        except OSError:
            continue
    return sorted(found, key=lambda program: program["pid"])
