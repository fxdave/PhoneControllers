"""Keeps the PC from going idle (screen saver, screen off, suspend) while phones drive it.

A phone isn't keyboard or mouse input, so without this the desktop thinks nobody is there.
Every way used here ends by itself when this process dies, killed or crashed, so the screen
saver never stays blocked:

- the desktop portal's Inhibit (works inside Flatpak; GNOME, KDE and others),
- org.freedesktop.ScreenSaver.Inhibit (KDE, hypridle and other idle daemons), released when
  this process's D-Bus connection closes,
- a logind "idle" inhibitor (hypridle, swayidle and others honour it), held as an open file
  descriptor that the kernel closes when the process goes.

Without PyGObject (the command line server on a minimal system) it falls back to running
systemd-inhibit, which is set to die together with this process, around a command that ends
when this process does.
"""

import ctypes
import os
import signal
import subprocess

APP_NAME = "Phone Controllers"
PORTAL_IDLE = 8
SCREENSAVER_PATHS = ("/org/freedesktop/ScreenSaver", "/ScreenSaver")
PR_SET_PDEATHSIG = 1

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError):
    Gio = GLib = None


class IdleInhibitor:
    """inhibit(reason) and release() may be called repeatedly; they only act on a change.
    Both block briefly on D-Bus, so call them off the event loop."""

    def __init__(self, log=lambda message: None):
        self.log = log
        self.active = False
        self._portal_handle = None
        self._screensaver = None   # (path, cookie)
        self._logind_fd = None
        self._process = None

    def inhibit(self, reason: str) -> None:
        if self.active:
            return
        self.active = True
        if Gio is None:
            self._start_systemd_inhibit(reason)
        else:
            self._portal(reason)
            self._screensaver_inhibit(reason)
            self._logind(reason)
        ways = [name for name, held in (
            ("portal", self._portal_handle), ("screen saver", self._screensaver),
            ("logind", self._logind_fd is not None), ("systemd-inhibit", self._process))
            if held]
        if ways:
            self.log(f"keeping the screen awake ({', '.join(ways)})")
        else:
            self.log("can't keep the screen awake: no idle inhibitor available")

    def release(self) -> None:
        if not self.active:
            return
        self.active = False
        if self._portal_handle:
            self._call(Gio.BusType.SESSION, "org.freedesktop.portal.Desktop",
                       self._portal_handle, "org.freedesktop.portal.Request", "Close", None)
            self._portal_handle = None
        if self._screensaver:
            path, cookie = self._screensaver
            self._call(Gio.BusType.SESSION, "org.freedesktop.ScreenSaver", path,
                       "org.freedesktop.ScreenSaver", "UnInhibit", GLib.Variant("(u)", (cookie,)))
            self._screensaver = None
        if self._logind_fd is not None:
            os.close(self._logind_fd)
            self._logind_fd = None
        if self._process:
            self._process.terminate()
            self._process.wait()
            self._process = None
        self.log("the screen may go idle again")

    # ---------- the ways ----------

    def _portal(self, reason: str) -> None:
        options = {"reason": GLib.Variant("s", reason)}
        result = self._call(Gio.BusType.SESSION, "org.freedesktop.portal.Desktop",
                            "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Inhibit",
                            "Inhibit", GLib.Variant("(sua{sv})", ("", PORTAL_IDLE, options)))
        if result:
            self._portal_handle = result.unpack()[0]

    def _screensaver_inhibit(self, reason: str) -> None:
        for path in SCREENSAVER_PATHS:
            result = self._call(Gio.BusType.SESSION, "org.freedesktop.ScreenSaver", path,
                                "org.freedesktop.ScreenSaver", "Inhibit",
                                GLib.Variant("(ss)", (APP_NAME, reason)))
            if result:
                self._screensaver = (path, result.unpack()[0])
                return

    def _logind(self, reason: str) -> None:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            result, fds = bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager", "Inhibit",
                GLib.Variant("(ssss)", ("idle", APP_NAME, reason, "block")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 2000, None, None)
            self._logind_fd = fds.get(result.unpack()[0])
        except GLib.Error:
            pass

    def _start_systemd_inhibit(self, reason: str) -> None:
        def die_with_parent() -> None:
            ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)

        try:
            self._process = subprocess.Popen(
                ["systemd-inhibit", "--what=idle", f"--who={APP_NAME}", f"--why={reason}",
                 "--mode=block", "tail", f"--pid={os.getpid()}", "-f", "/dev/null"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=die_with_parent)
        except OSError:
            self._process = None

    @staticmethod
    def _call(bus_type, name, path, interface, method, parameters):
        try:
            bus = Gio.bus_get_sync(bus_type, None)
            return bus.call_sync(name, path, interface, method, parameters, None,
                                 Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error:
            return None
