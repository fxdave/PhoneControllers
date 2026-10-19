"""Desktop app: runs the server and shows the rig live. The home screen is the cockpit; each
part of it opens a focused page with the main choices up top and fine-tuning tucked away."""

import asyncio
import copy
import os
import sys
import threading
import traceback

import gi

# Libadwaita brings its own stylesheet; a GTK_THEME override swaps it for GTK's plain theme and
# breaks the layout. Keep only the dark preference it may carry.
GTK_THEME = os.environ.pop("GTK_THEME", "")

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import config as settings  # noqa: E402
from . import curves, games  # noqa: E402
from .server import Server  # noqa: E402
from .widgets import (  # noqa: E402
    PEDAL_COLORS, PEDAL_NAMES, CardRow, Cockpit, CurveEditor, ForceGauge, ModeCard,
    PedalGauge, PresetPicker, QrCode, RotationDial, draw_camera, draw_gyro, draw_hold,
    draw_pulse, draw_shift_rally, draw_shift_up, draw_tilt_pedal, draw_tilt_speed,
    draw_touch_slider, draw_touch_wheel, install_css, travel_of, travel_points,
)

APP_ID = "io.github.fxdave.PhoneControllers"
SAVE_DELAY_MS = 400
LOG_LINES = 300

CONTROL_NAMES = {"wheel": "Wheel", "paddles": "Paddles", "lever": "Signal lever",
                 "shifter": "Shift lever", "buttons": "Buttons", "c": "Clutch", "b": "Brake",
                 "t": "Gas"}
TILT_DIRECTIONS = [("away", "Top edge away from you"), ("toward", "Top edge toward you"),
                   ("left", "Lean left"), ("right", "Lean right")]
ROTATION_PRESETS = [("Kart", 270), ("Sports car", 540), ("Road car", 900), ("Truck", 1440)]
FEEL_PRESETS = {
    "Direct": curves.PRESETS["linear"],
    "Progressive": curves.PRESETS["gentle start"],
    "Sharp": curves.PRESETS["quick start"],
    "Smooth": curves.PRESETS["s-curve"],
}
CLUTCH_FEEL_PRESETS = {"Late bite": curves.PRESETS["clutch bite"], **FEEL_PRESETS}
# Which page each part of the cockpit opens.
PART_PAGES = {"wheel": "steering", "force": "feedback", "lever": "buttons",
              "shifter": "buttons", "c": "pedal-c", "b": "pedal-b", "t": "pedal-t"}


class ServerRunner:
    """Runs the server on its own thread and asyncio loop. Events come back on the GTK thread."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: Server | None = None
        self.stop_event: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _emit(self, kind, data) -> None:
        GLib.idle_add(self.on_event, kind, data)

    def start(self, config: dict) -> None:
        if self.running:
            return
        ready = threading.Event()

        async def main() -> None:
            self.loop = asyncio.get_running_loop()
            self.stop_event = asyncio.Event()
            self.server = Server(copy.deepcopy(config), self._emit)
            ready.set()
            await self.server.run(self.stop_event)

        def run() -> None:
            try:
                asyncio.run(main())
            except Exception as error:  # keep the app alive whatever the server hits
                traceback.print_exc()
                self._emit("error", f"The server stopped: {error}")
            finally:
                self.loop = self.server = self.stop_event = None
                ready.set()
                self._emit("stopped", None)

        self.thread = threading.Thread(target=run, name="phone-controllers-server", daemon=True)
        self.thread.start()
        ready.wait(2)

    def stop(self) -> None:
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def join(self, timeout: float) -> None:
        if self.thread:
            self.thread.join(timeout)

    def call(self, function) -> None:
        """Run function(server) on the server's loop."""
        if self.loop and self.server:
            server = self.server
            self.loop.call_soon_threadsafe(lambda: function(server))


def heading(text: str, level: str = "title-3") -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.add_css_class(level)
    return label


def caption(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True)
    label.add_css_class("dim-label")
    return label


def section(*children, spacing=12) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    for child in children:
        box.append(child)
    return box


def margins(widget, size: int):
    for side in ("top", "bottom", "start", "end"):
        getattr(widget, f"set_margin_{side}")(size)
    return widget


def pill(label: str, on_click, suggested=False) -> Gtk.Button:
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    button.add_css_class("pill" if suggested else "part-button")
    if suggested:
        button.add_css_class("suggested-action")
    button.connect("clicked", lambda *_: on_click())
    return button


def fine_tuning(*children) -> Gtk.Expander:
    """The less important settings: folded away, quieter type."""
    body = section(*children, spacing=18)
    body.add_css_class("fine-tuning-body")
    body.set_margin_top(12)
    expander = Gtk.Expander(label="Fine-tuning", child=body)
    expander.add_css_class("fine-tuning")
    return expander


class Window(Adw.ApplicationWindow):
    def __init__(self, app: "App"):
        super().__init__(application=app, title="Phone Controllers", default_width=940,
                         default_height=820)
        install_css()
        self.app = app
        self.config = app.config
        self.refreshers: list = []    # callables that show the current settings
        self.updating = False         # true while widgets are being set from the settings
        self.editors: dict = {}
        self.gauges: dict = {}
        self.save_source = 0
        self.status: dict = {"running": False}
        self.phones: list = []
        self.programs: list = []
        self.game: dict | None = None

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        self.nav.add(self.build_home())
        for page in (self.build_steering(), *(self.build_pedal(key) for key in "tbc"),
                     self.build_feedback(), self.build_buttons(), self.build_connection(),
                     self.build_log()):
            self.nav.add(page)

        action = Gio.SimpleAction.new("open", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, tag: self.open_page(tag.get_string()))
        self.add_action(action)

        self.refresh()
        self.show_status(self.status)

    # ---------- settings plumbing ----------

    def get(self, path):
        value = self.config
        for key in path:
            value = value[key]
        return value

    def change(self, path, value) -> None:
        if self.updating:
            return
        target = self.config
        for key in path[:-1]:
            target = target[key]
        if target[path[-1]] == value:
            return
        target[path[-1]] = value
        cleaned = settings.sanitize(self.config)
        self.config.clear()
        self.config.update(cleaned)
        self.refresh()
        self.app.config_changed()
        if self.save_source:
            GLib.source_remove(self.save_source)
        self.save_source = GLib.timeout_add(SAVE_DELAY_MS, self.save)

    def save(self) -> bool:
        self.save_source = 0
        try:
            settings.save(self.config)
        except OSError as error:
            self.toast(f"Couldn't save the settings: {error}")
        return False

    def refresh(self) -> None:
        self.updating = True
        try:
            for refresher in self.refreshers:
                refresher()
        finally:
            self.updating = False
        self.restart_banner.set_revealed(self.app.needs_restart())

    def bind(self, refresher) -> None:
        self.refreshers.append(refresher)

    def spin(self, path, title, subtitle, lower, upper, step, digits=0) -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(lower, upper, step)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_digits(digits)
        row.connect("notify::value", lambda r, _p: self.change(
            path, round(r.get_value(), digits) if digits else int(r.get_value())))
        self.bind(lambda: row.set_value(self.get(path)))
        return row

    def switch(self, path, title, subtitle="") -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.connect("notify::active", lambda r, _p: self.change(path, r.get_active()))
        self.bind(lambda: row.set_active(self.get(path)))
        return row

    def cards(self, path, choices) -> CardRow:
        row = CardRow(choices, lambda value: self.change(path, value))
        self.bind(lambda: row.select(self.get(path)))
        return row

    def curve_editor(self, kind, key, title, x_label, y_label) -> CurveEditor:
        editor = CurveEditor(title, x_label, y_label,
                             lambda points: self.change(("curves", kind, key), points))
        self.editors[(kind, key)] = editor
        self.bind(lambda: editor.set_points(self.config["curves"][kind][key]))
        return editor

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def open_page(self, tag: str) -> None:
        if tag == "home":
            self.nav.pop_to_tag("home")
            return
        if self.nav.get_visible_page().get_tag() != tag:
            self.nav.push_by_tag(tag)

    def page(self, tag: str, title: str, *children) -> Adw.NavigationPage:
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        content = margins(section(*children, spacing=28), 24)
        clamp = Adw.Clamp(maximum_size=860, child=content)
        view.set_content(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER))
        return Adw.NavigationPage(tag=tag, title=title, child=view)

    # ---------- home ----------

    def build_home(self) -> Adw.NavigationPage:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.power = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Server on or off")
        self.power.connect("notify::active", self.power_toggled)
        power_box = Gtk.Box(spacing=8)
        self.power_label = Gtk.Label(label="Off")
        power_box.append(self.power)
        power_box.append(self.power_label)
        header.pack_start(power_box)
        menu = Gio.Menu()
        menu.append("Buttons & levers", "win.open::buttons")
        menu.append("Connection", "win.open::connection")
        menu.append("Log", "win.open::log")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="More"))
        view.add_top_bar(header)

        self.restart_banner = Adw.Banner(title="Restart the server to use the new connection "
                                               "settings", button_label="Restart")
        self.restart_banner.connect("button-clicked", lambda *_: self.app.restart_server())
        view.add_top_bar(self.restart_banner)
        self.error_banner = Adw.Banner(button_label="Dismiss")
        self.error_banner.connect("button-clicked", lambda b: b.set_revealed(False))
        view.add_top_bar(self.error_banner)

        # Server off: one clear action.
        self.stopped = Adw.StatusPage(
            icon_name="input-gaming-symbolic", title="The wheel is off",
            description="Start the server to let phones connect and games see the wheel.",
            vexpand=True)
        start = pill("Start server", self.app.start_server, suggested=True)
        start.set_halign(Gtk.Align.CENTER)
        self.stopped.set_child(start)

        # Server on: the live cockpit, then phones and the game.
        self.cockpit = Cockpit(lambda part: self.open_page(PART_PAGES[part]))
        parts = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                            row_spacing=6, max_children_per_line=6, halign=Gtk.Align.CENTER)
        for label, tag in (("Steering", "steering"), ("Gas", "pedal-t"), ("Brake", "pedal-b"),
                           ("Clutch", "pedal-c"), ("Force feedback", "feedback"),
                           ("Buttons & levers", "buttons")):
            parts.append(pill(label, lambda tag=tag: self.open_page(tag)))
        parts.set_margin_bottom(12)
        cockpit_box = section(self.cockpit, parts, spacing=6)
        cockpit_box.add_css_class("cockpit-frame")
        cockpit_hint = caption("Click a part to set it up. The line across each pedal is "
                               "what the game gets.")
        cockpit_hint.set_xalign(0.5)
        cockpit_hint.add_css_class("caption")

        self.connect_box = self.build_connect()

        phones_header = Gtk.Box(spacing=12)
        phones_header.append(heading("Phones"))
        phones_header.append(Gtk.Box(hexpand=True))
        self.add_phone = Gtk.Button(label="Connect another phone", valign=Gtk.Align.CENTER)
        self.add_phone.add_css_class("flat")
        self.add_phone.connect("clicked", lambda *_: self.connect_box.set_visible(
            not self.connect_box.get_visible()))
        phones_header.append(self.add_phone)
        self.phone_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.phone_list.add_css_class("boxed-list")
        self.phone_list.set_placeholder(self.placeholder("No phone yet. Scan the code above."))

        game_header = Gtk.Box(spacing=12)
        game_header.append(heading("Game"))
        game_header.append(Gtk.Box(hexpand=True))
        resend = Gtk.Button(label="Resend all axes", valign=Gtk.Align.CENTER,
                            tooltip_text="Makes the game read every axis again, in case it "
                                         "kept an old value")
        resend.add_css_class("flat")
        resend.connect("clicked", lambda *_: self.app.runner.call(
            lambda server: server.resync("asked from the app")))
        game_header.append(resend)
        self.game_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.game_list.add_css_class("boxed-list")

        running = margins(section(
            section(cockpit_box, cockpit_hint, spacing=8),
            self.connect_box,
            section(phones_header, self.phone_list, spacing=8),
            section(game_header, self.game_list, spacing=8),
            spacing=28), 24)

        self.home_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.home_stack.add_named(self.stopped, "stopped")
        self.home_stack.add_named(
            Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=860, child=running),
                               hscrollbar_policy=Gtk.PolicyType.NEVER), "running")
        view.set_content(self.home_stack)
        return Adw.NavigationPage(tag="home", title="Phone Controllers", child=view)

    def build_connect(self) -> Gtk.Box:
        self.qr = QrCode(150)
        self.address = Gtk.Label(xalign=0, selectable=True, wrap=True)
        self.address.add_css_class("title-2")
        copy_button = Gtk.Button(label="Copy address", halign=Gtk.Align.START)
        copy_button.connect("clicked", self.copy_address)
        self.address_note = caption("")
        self.address_note.add_css_class("caption")
        text = section(heading("Connect a phone"),
                       caption("Scan the code with the phone's camera, or type this address "
                               "in its browser. The phone must be on the same network."),
                       self.address, copy_button, self.address_note, spacing=8)
        text.set_valign(Gtk.Align.CENTER)
        text.set_hexpand(True)
        box = Gtk.Box(spacing=24)
        box.append(self.qr)
        box.append(text)
        return box

    @staticmethod
    def placeholder(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, margin_top=14, margin_bottom=14, wrap=True)
        label.add_css_class("dim-label")
        return label

    def copy_address(self, _button) -> None:
        address = self.address.get_label()
        if address:
            self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(address))
            self.toast("Address copied")

    def power_toggled(self, switch, _param) -> None:
        if self.updating:
            return
        if switch.get_active() and not self.app.runner.running:
            self.error_banner.set_revealed(False)
            self.app.start_server()
        elif not switch.get_active() and self.app.runner.running:
            self.app.runner.stop()

    def show_status(self, status: dict) -> None:
        self.status = status
        running = bool(status.get("running"))
        self.updating = True
        self.power.set_active(running or self.app.runner.running)
        self.updating = False
        self.power_label.set_label("On" if running else "Off")
        self.home_stack.set_visible_child_name("running" if running else "stopped")
        url = status.get("https") or status.get("http")
        self.address.set_label(url or "")
        self.qr.set_url(url)
        if status.get("https"):
            self.address_note.set_label("The first time, the phone warns about the certificate: "
                                        "choose Advanced, then Proceed. Tilt controls need this "
                                        f"secure address. Touch only works at {status['http']}")
        else:
            self.address_note.set_label("")
        if not running:
            self.show_phones([])
            self.show_programs([])
        self.show_game_setup()

    def show_phones(self, phones: list) -> None:
        self.phones = phones
        self.phone_list.remove_all()
        for phone in phones:
            row = Adw.ActionRow(title=phone["name"])
            details = phone["address"]
            if phone["rtt"] is not None:
                details += f", {phone['rtt']} ms delay"
            row.set_subtitle(details)
            row.add_prefix(Gtk.Image(icon_name="phone-symbolic", pixel_size=24))
            # Wraps onto more lines in a narrow window instead of widening it.
            chips = Adw.WrapBox(child_spacing=4, line_spacing=4, align=1.0, hexpand=True,
                                valign=Gtk.Align.CENTER)
            for control in phone["controls"]:
                chip = Gtk.Label(label=CONTROL_NAMES.get(control, control))
                chip.add_css_class("chip")
                chips.append(chip)
            row.add_suffix(chips)
            edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Choose what this phone shows")
            edit.add_css_class("flat")
            edit.connect("clicked", lambda _b, p=phone: self.edit_phone(p))
            row.add_suffix(edit)
            self.phone_list.append(row)
        self.connect_box.set_visible(not phones)
        self.add_phone.set_visible(bool(phones))

    def edit_phone(self, phone: dict) -> None:
        """Pick the controls and buttons one phone shows, without picking the phone up."""
        key = phone["key"]

        def toggle(widget: str, active: bool) -> None:
            self.app.runner.call(lambda server: server.show_on_phone(key, widget, active))

        def row(widget: str, title: str, active: bool) -> Adw.SwitchRow:
            switch = Adw.SwitchRow(title=title, active=active)
            switch.connect("notify::active", lambda r, _p: toggle(widget, r.get_active()))
            return switch

        controls = Adw.PreferencesGroup(
            title="Controls",
            description="Turn off what another phone handles. Steering and each pedal "
                        "follow whichever phone turns or presses furthest.")
        for widget, control, title in (
                ("wheel", "wheel", "Steering wheel"), ("lever", "lever", "Signal lever"),
                ("shifter", "shifter", "Shift lever"), ("pedal-c", "c", "Clutch"),
                ("pedal-b", "b", "Brake"), ("pedal-t", "t", "Gas")):
            controls.add(row(widget, title, control in phone["controls"]))
        buttons = Adw.PreferencesGroup(title="Buttons")
        labels = self.config["buttons"]
        for index in range(12):
            buttons.add(row(f"button-{index}", labels[index], index in phone["buttons"]))
        page = Adw.PreferencesPage()
        page.add(controls)
        page.add(buttons)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        dialog = Adw.Dialog(title=f"{phone['name']}, {phone['address']}", child=view,
                            content_width=460, content_height=640)
        dialog.present(self)

    def show_programs(self, programs: list) -> None:
        self.programs = programs
        self.show_game_setup()

    def show_game_setup(self) -> None:
        """What's using the wheel, and a checklist for the games set up from here."""
        self.game_list.remove_all()
        if self.programs:
            for program in self.programs:
                row = Adw.ActionRow(title=f"{program['name']} is using the wheel",
                                    subtitle="Every axis was sent again when it opened the "
                                             "wheel, so no pedal is stuck.")
                row.add_prefix(Gtk.Image(icon_name="object-select-symbolic"))
                self.game_list.append(row)
        else:
            row = Adw.ActionRow(title="No game has opened the wheel yet",
                                subtitle="Start your game; it shows up here.")
            row.add_prefix(Gtk.Image(icon_name="input-gaming-symbolic"))
            self.game_list.append(row)
        if self.game:
            packet = self.game
            row = Adw.ActionRow(title=packet["name"] or packet["car"] or "Telemetry",
                                subtitle=f"Gear {packet['gear']}, {packet['speed']:.0f} km/h, "
                                         f"{packet['rpm']} rpm")
            row.add_prefix(Gtk.Image(icon_name="power-profile-performance-symbolic"))
            self.game_list.append(row)
        if games.ror_found():
            state = games.ror_map_state()
            row = Adw.ActionRow(title="Rigs of Rods: wheel mapping",
                                subtitle={"installed": "Installed",
                                          "outdated": "An older version is installed",
                                          "missing": "Rigs of Rods ignores the wheel without "
                                                     "it"}[state])
            row.add_prefix(Gtk.Image(icon_name="object-select-symbolic" if state == "installed"
                                     else "dialog-warning-symbolic"))
            if state != "installed":
                row.add_suffix(pill("Install" if state == "missing" else "Update",
                                    self.install_ror_map))
            self.game_list.append(row)
            on = games.ror_force_feedback_on()
            row = Adw.ActionRow(title="Rigs of Rods: force feedback",
                                subtitle="On" if on else "Off in the game's settings")
            row.add_prefix(Gtk.Image(icon_name="object-select-symbolic" if on
                                     else "dialog-information-symbolic"))
            if not on:
                row.add_suffix(pill("Turn on", self.enable_ror_force_feedback))
            self.game_list.append(row)

    def install_ror_map(self) -> None:
        try:
            games.install_ror_map()
        except OSError as error:
            self.toast(f"Couldn't install the mapping: {error}")
            return
        self.toast("Mapping installed. Restart Rigs of Rods to use it.")
        self.show_game_setup()

    def enable_ror_force_feedback(self) -> None:
        if any("ror" in program["name"].lower() or "rigs" in program["name"].lower()
               for program in self.programs):
            self.toast("Close Rigs of Rods first: it rewrites its settings when it exits.")
            return
        try:
            games.enable_ror_force_feedback()
        except OSError as error:
            self.toast(f"Couldn't change RoR.cfg: {error}")
            return
        self.toast("Force feedback turned on. Tune its strength in the game's settings.")
        self.show_game_setup()

    def show_monitor(self, monitor: dict) -> None:
        self.cockpit.update(monitor, self.config["wheel"]["range"])
        raw, shown, output = monitor["raw"], monitor["shown"], monitor["output"]
        game = self.game
        for key, gauge in self.gauges.items():
            gauge.set_live(raw.get(key, 0.0), shown[key], output[key],
                           game[key] if game else None)
        for (kind, key), editor in self.editors.items():
            editor.set_live(raw.get(key) if kind == "pre" else shown[key])
        forces = monitor["ff"]
        self.force_gauge.update(forces["force"], forces["rumble"])
        angle = monitor.get("look")
        if angle is not None:
            side = "right" if angle > 0 else "left"
            text = f"{abs(angle)}° to the {side}" if angle else "Straight ahead"
            if self.look_row.get_subtitle() != text:
                self.look_row.set_subtitle(text)

    def show_game(self, packet: dict) -> None:
        first = self.game is None
        self.game = packet
        if first:
            self.show_game_setup()

    def add_log(self, line: str) -> None:
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(),
                      GLib.DateTime.new_now_local().format("%H:%M:%S ") + line + "\n")
        extra = buffer.get_line_count() - LOG_LINES
        if extra > 0:
            buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_line(extra)[1])

        def scroll_to_end():
            adjustment = self.log_scroller.get_vadjustment()
            adjustment.set_value(adjustment.get_upper())

        GLib.idle_add(scroll_to_end)

    def show_error(self, message: str) -> None:
        self.error_banner.set_title(GLib.markup_escape_text(message))
        self.error_banner.set_revealed(True)

    # ---------- steering ----------

    def build_steering(self) -> Adw.NavigationPage:
        dial = RotationDial()
        number = Gtk.Label(xalign=0)
        number.add_css_class("big-number")
        turns = caption("")
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 90, 2520, 10)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        for _name, degrees in ROTATION_PRESETS:
            scale.add_mark(degrees, Gtk.PositionType.BOTTOM, None)
        scale.connect("value-changed", lambda s: self.change(
            ("wheel", "range"), int(round(s.get_value() / 10) * 10)))
        presets = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                              row_spacing=6, max_children_per_line=4)
        for name, degrees in ROTATION_PRESETS:
            presets.append(pill(f"{name} {degrees}°", lambda d=degrees: self.change(
                ("wheel", "range"), d)))

        def show_range():
            degrees = self.config["wheel"]["range"]
            dial.set_range(degrees)
            number.set_label(f"{degrees}°")
            turns.set_label(f"Lock to lock: {degrees / 360:g} turns of the wheel")
            if abs(scale.get_value() - degrees) >= 10:
                scale.set_value(degrees)

        self.bind(show_range)
        rotation_text = section(heading("Rotation"), number, turns, scale, presets, spacing=6)
        rotation_text.set_valign(Gtk.Align.CENTER)
        rotation_text.set_hexpand(True)
        rotation = Gtk.Box(spacing=24)
        rotation.append(dial)
        rotation.append(rotation_text)

        modes = self.cards(("wheel", "steerMode"), [
            ("touch", ModeCard("Drag the wheel", "Turn it with your thumb, like a real wheel",
                               draw_touch_wheel)),
            ("tilt-speed", ModeCard("Drag or tilt", "Tilting the phone also turns the wheel, "
                                    "faster the further", draw_tilt_speed)),
            ("gyro", ModeCard("Phone is the wheel", "Hold the phone like a wheel and steer "
                              "with it", draw_gyro)),
        ])
        spring = Adw.PreferencesGroup()
        spring.add(self.switch(("wheel", "autocenter"), "Spring back to center",
                               "When your thumb lets go of the wheel"))

        gyro_rows = Adw.PreferencesGroup(title="Phone is the wheel")
        gyro_rows.add(self.spin(("wheel", "gyroLock"), "Full lock at",
                                "Degrees of phone tilt", 5, 90, 1))
        tilt_rows = Adw.PreferencesGroup(title="Drag or tilt")
        tilt_rows.add(self.spin(("wheel", "tiltSteerSpeed"), "Turn speed at full tilt",
                                "Degrees per second", 30, 2880, 30))
        tilt_rows.add(self.spin(("wheel", "tiltSteerFull"), "Full speed at",
                                "Degrees of phone tilt", 2, 90, 1))
        tilt_rows.add(self.spin(("wheel", "tiltSteerDeadzone"), "Ignore the first",
                                "Degrees of phone tilt", 0, 45, 1))

        def show_mode_rows():
            mode = self.config["wheel"]["steerMode"]
            gyro_rows.set_visible(mode == "gyro")
            tilt_rows.set_visible(mode == "tilt-speed")

        self.bind(show_mode_rows)

        look = Adw.PreferencesGroup(
            title="Look around",
            description="Turn the phone you steer with left or right and the game's camera "
                        "turns with it, using the phone's compass. Center on the phone sets "
                        "straight ahead. Rigs of Rods needs the updated wheel mapping from the "
                        "home screen.")
        look.add(self.switch(("look", "enabled"), "Look by turning the phone"))
        self.look_row = Adw.ActionRow(title="Camera now", subtitle="Straight ahead")
        look.add(self.look_row)
        self.bind(lambda: self.look_row.set_visible(self.config["look"]["enabled"]))
        look_rows = Adw.PreferencesGroup(title="Look around")
        look_rows.add(self.spin(("look", "gain"), "Camera turn per phone degree",
                                "At 3, turning the phone 30° looks 90° to the side",
                                0.5, 8, 0.5, digits=1))
        look_rows.add(self.spin(("look", "deadzone"), "Ignore the first",
                                "Degrees of phone turn, so small wobbles don't move the view",
                                0, 45, 1))
        look_rows.add(self.switch(("look", "invert"), "Turn the camera the other way"))

        editors = Gtk.Box(spacing=18, homogeneous=True)
        editors.append(self.curve_editor("pre", "s", "Tilt to wheel", "phone tilt",
                                         "wheel angle"))
        editors.append(self.curve_editor("post", "s", "Wheel to game", "wheel angle",
                                         "game input"))
        return self.page(
            "steering", "Steering",
            rotation,
            section(heading("How you steer"), modes),
            spring,
            look,
            fine_tuning(gyro_rows, tilt_rows, look_rows,
                        caption("Curves shape both sides of center alike. The first one only "
                                "bends tilt steering: a dragged wheel is its own input."),
                        editors))

    # ---------- pedals ----------

    def build_pedal(self, key: str) -> Adw.NavigationPage:
        color = PEDAL_COLORS[key]
        gauge = PedalGauge(key, lambda start, full: self.change(
            ("curves", "pre", key), travel_points(start, full)))
        self.gauges[key] = gauge

        def pick_mode(value):
            current = self.config["pedals"]["modes"][key]
            if value == "tilt":
                # Keep a direction chosen before, otherwise top edge away.
                value = current if current != "touch" else "away"
            self.change(("pedals", "modes", key), value)

        modes = CardRow([
            ("touch", ModeCard("Touch slider", "Slide your finger up the pedal",
                               draw_touch_slider)),
            ("tilt", ModeCard("Tilt the phone", "Lay the phone down and tip it",
                              draw_tilt_pedal)),
            ("camera", ModeCard("Cover the camera", "The more your finger covers the back "
                                "camera, the further it presses", draw_camera)),
        ], pick_mode)
        direction = Gtk.DropDown.new_from_strings([label for _, label in TILT_DIRECTIONS])
        direction.connect("notify::selected", lambda d, _p: self.change(
            ("pedals", "modes", key), TILT_DIRECTIONS[d.get_selected()][0]))
        direction_row = Gtk.Box(spacing=12)
        direction_row.append(Gtk.Label(label="Tip it"))
        direction_row.append(direction)
        camera_note = caption("Calibrate on the phone: Settings, Camera pedal. It needs the "
                              "phone's secure (https) address.")
        camera_note.add_css_class("caption")

        travel_text = caption("")
        reset_travel = Gtk.Button(label="Use the whole travel", halign=Gtk.Align.START)
        reset_travel.add_css_class("flat")
        reset_travel.connect("clicked", lambda *_: self.change(
            ("curves", "pre", key), travel_points(0.0, 1.0)))

        presets = CLUTCH_FEEL_PRESETS if key == "c" else FEEL_PRESETS
        feel = PresetPicker(presets, lambda preset: self.change(
            ("curves", "post", key), copy.deepcopy(presets[preset])), color)
        feel_text = caption("")

        def show_pedal():
            mode = self.config["pedals"]["modes"][key]
            tilt = mode not in ("touch", "camera")
            modes.select("tilt" if tilt else mode)
            direction_row.set_visible(tilt)
            camera_note.set_visible(mode == "camera")
            if tilt:
                direction.set_selected([v for v, _ in TILT_DIRECTIONS].index(mode))
            start, full, plain = travel_of(self.config["curves"]["pre"][key])
            gauge.set_travel(start, full)
            text = (f"Pressing counts from {start * 100:.0f}% of the travel and is full at "
                    f"{full * 100:.0f}%. Drag the markers beside the pedal to change it.")
            if not plain:
                text += " The travel was shaped in fine-tuning; dragging replaces that shape."
            travel_text.set_label(text)
            reset_travel.set_visible((start, full) != (0.0, 1.0) or not plain)
            current = self.config["curves"]["post"][key]
            feel.show_current(current, presets)
            matched = any(curves.normalize(points) == current for points in presets.values())
            feel_text.set_label("How the pedal's travel turns into what the game gets."
                                + ("" if matched else " Now: a custom curve from fine-tuning."))

        self.bind(show_pedal)

        main = section(
            section(heading("How you press it"), modes, direction_row, camera_note, spacing=10),
            section(heading("Travel"), travel_text, reset_travel, spacing=6),
            section(heading("Feel"), feel_text, feel, spacing=8),
            spacing=24)
        main.set_hexpand(True)
        top = Gtk.Box(spacing=24)
        top.append(gauge)
        top.append(main)
        legend = caption("On the pedal: the fill is what the phone shows, the line across it "
                         "is what the game gets, the arrow is your finger.")
        legend.add_css_class("caption")

        tilt = Adw.PreferencesGroup(title="Tilt pedals",
                                    description="Shared by every pedal that follows tilt.")
        tilt.add(self.spin(("pedals", "tiltFull"), "Fully pressed at", "Degrees of phone tilt",
                           2, 90, 1))
        tilt.add(self.spin(("pedals", "tiltDeadzone"), "Ignore the first",
                           "Degrees of phone tilt", 0, 45, 1))
        extra = [tilt]
        if key == "c":
            clutch = Adw.PreferencesGroup()
            clutch.add(self.spin(("pedals", "clutchReturn"), "Clutch comes up in",
                                 "Seconds after your finger lifts, 0 for at once",
                                 0, 5, 0.1, digits=1))
            extra.insert(0, clutch)
        editors = Gtk.Box(spacing=18, homogeneous=True)
        editors.append(self.curve_editor("pre", key, "Input to pedal", "finger or tilt",
                                         "pedal shows"))
        editors.append(self.curve_editor("post", key, "Pedal to game", "pedal shows",
                                         "game input"))
        return self.page(f"pedal-{key}", PEDAL_NAMES[key], section(top, legend, spacing=10),
                         fine_tuning(*extra, editors))

    # ---------- force feedback ----------

    def build_feedback(self) -> Adw.NavigationPage:
        self.force_gauge = ForceGauge()
        enabled = Adw.PreferencesGroup()
        enabled.add(self.switch(("ffb", "enabled"), "Use force feedback from games",
                                "The phone shows the game's steering force as an amber arc "
                                "on the wheel, and rumble vibrates it"))
        strength_number = Gtk.Label(xalign=0)
        strength_number.add_css_class("big-number")
        strength = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 5)
        strength.set_draw_value(False)
        strength.add_mark(100, Gtk.PositionType.BOTTOM, None)
        strength.connect("value-changed", lambda s: self.change(
            ("ffb", "gain"), round(s.get_value() / 100, 2)))

        def show_strength():
            percent = round(self.config["ffb"]["gain"] * 100)
            strength_number.set_label(f"{percent}%")
            if abs(strength.get_value() - percent) >= 1:
                strength.set_value(percent)

        self.bind(show_strength)
        advanced = Adw.PreferencesGroup()
        advanced.add(self.switch(("ffb", "invert"), "Reverse the steering force",
                                 "If the wheel is pushed the wrong way"))
        advanced.add(self.switch(("ffb", "pull"), "Let go and the wheel follows the force",
                                 "Touch steering only"))
        advanced.add(self.spin(("ffb", "pullSpeed"), "Follow speed at full force",
                               "Degrees per second", 0, 3600, 30))
        advanced.add(self.switch(("ffb", "vibrate"), "Vibrate on rumble"))
        return self.page(
            "feedback", "Force feedback",
            section(heading("From the game now"), self.force_gauge, spacing=10),
            enabled,
            section(heading("Strength"), strength_number, strength, spacing=6),
            fine_tuning(advanced))

    # ---------- buttons & lever ----------

    def build_buttons(self) -> Adw.NavigationPage:
        lever = self.cards(("lever", "mode"), [
            ("pulse", ModeCard("A press per move", "For games that switch the signal on each "
                               "press, like Rigs of Rods", draw_pulse)),
            ("hold", ModeCard("Held while on", "For games that expect the button held down",
                              draw_hold)),
        ])
        cancel = Adw.PreferencesGroup()
        cancel.add(self.switch(("lever", "autoCancel"), "Cancel after the turn",
                               "The lever comes back when the wheel returns to center"))
        shifter = self.cards(("shifter", "upShifts"), [
            (True, ModeCard("Push up to shift up", "Like the paddles: up is a higher gear",
                            draw_shift_up)),
            (False, ModeCard("Pull down to shift up", "Rally style: push away for a lower "
                             "gear", draw_shift_rally)),
        ])
        labels = Adw.PreferencesGroup(
            title="Button names",
            description="Shown on the phones. Bind each button to anything in the game.")
        for index in range(12):
            row = Adw.EntryRow(title="Left paddle" if index == 0 else
                               "Right paddle" if index == 1 else f"Button {index + 1}")
            row.connect("changed", lambda r, i=index: self.set_label(i, r.get_text()))

            def show_label(row=row, index=index):
                if row.get_text() != self.config["buttons"][index]:
                    row.set_text(self.config["buttons"][index])

            self.bind(show_label)
            labels.add(row)
        return self.page(
            "buttons", "Buttons & levers",
            section(heading("Signal lever"),
                    caption("Push it up for right, down for left, back to the middle to "
                            "cancel."), lever, spacing=10),
            cancel,
            section(heading("Shift lever"),
                    caption("Springs back to the middle. Holding it out holds the shift "
                            "button; a tap on its top or bottom half is a quick shift."),
                    shifter, spacing=10),
            labels)

    def set_label(self, index: int, text: str) -> None:
        if self.updating:
            return
        labels = list(self.config["buttons"])
        labels[index] = text
        self.change(("buttons",), labels)

    # ---------- connection & log ----------

    def build_connection(self) -> Adw.NavigationPage:
        group = Adw.PreferencesGroup(
            description="Changes here apply when the server restarts.")
        group.add(self.spin(("server", "port"), "Address port", "For touch-only phones",
                            1, 65535, 1))
        group.add(self.switch(("server", "https"), "Secure address",
                              "Browsers only give tilt sensors to secure (https) pages"))
        group.add(self.spin(("server", "httpsPort"), "Secure address port", "", 1, 65535, 1))
        group.add(self.switch(("server", "autostart"), "Start the server when the app opens"))
        telemetry = Adw.PreferencesGroup(
            title="Game telemetry",
            description="Live for Speed, BeamNG.drive and others can send OutGauge data. Then "
                        "phones show the game's own pedals, gear and speed.")
        telemetry.add(self.switch(("server", "outgauge"), "Receive OutGauge telemetry"))
        telemetry.add(self.spin(("server", "outgaugePort"), "Telemetry port (UDP)", "",
                                1, 65535, 1))
        files = Adw.PreferencesGroup(title="Settings file")
        files.add(Adw.ActionRow(title=str(settings.config_path()), title_selectable=True))
        return self.page("connection", "Connection", group, telemetry, files)

    def build_log(self) -> Adw.NavigationPage:
        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8,
                                     left_margin=8, bottom_margin=8)
        self.log_scroller = Gtk.ScrolledWindow(child=self.log_view, vexpand=True)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.log_scroller)
        return Adw.NavigationPage(tag="log", title="Log", child=view)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.config = settings.load()
        self.runner = ServerRunner(self.on_server_event)
        self.window: Window | None = None
        # The connection settings the running server was started with.
        self.started_with: dict | None = None
        self.restart_pending = False
        self.connect("shutdown", self.on_shutdown)

    def do_activate(self) -> None:
        if not self.window:
            if GTK_THEME.endswith(":dark"):
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
            self.window = Window(self)
            if self.config["server"]["autostart"]:
                self.start_server()
        self.window.present()

    def start_server(self) -> None:
        self.started_with = copy.deepcopy(self.config["server"])
        self.runner.start(self.config)
        self.window.refresh()

    def restart_server(self) -> None:
        if self.runner.running:
            self.restart_pending = True
            self.runner.stop()
        else:
            self.start_server()

    def needs_restart(self) -> bool:
        return self.runner.running and self.started_with != self.config["server"]

    def config_changed(self) -> None:
        config = copy.deepcopy(self.config)
        self.runner.call(lambda server: server.set_config(config))

    def on_server_event(self, kind: str, data) -> bool:
        window = self.window
        if not window:
            return False
        if kind == "status":
            window.show_status(data)
        elif kind == "stopped":
            self.started_with = None
            window.show_status({"running": False})
            window.refresh()
            if self.restart_pending:
                self.restart_pending = False
                self.start_server()
        elif kind == "log":
            window.add_log(data)
        elif kind == "error":
            window.show_error(data)
            window.add_log(data)
        elif kind == "phones":
            window.show_phones(data)
        elif kind == "programs":
            window.show_programs(data)
        elif kind == "monitor":
            window.show_monitor(data)
        elif kind == "game":
            window.show_game(data)
        elif kind == "set":
            # A phone's quick switch: store it like any change made here.
            window.change(tuple(data["path"]), data["value"])
        return False

    def on_shutdown(self, _app) -> None:
        if self.window and self.window.save_source:
            self.window.save()
        # Let the server close the virtual device so no axis is left pressed.
        self.runner.stop()
        self.runner.join(3)


def main() -> int:
    app = App()
    # Ctrl+C in a terminal quits cleanly, releasing the virtual device.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, lambda: app.quit() or False)
    return app.run(sys.argv)
