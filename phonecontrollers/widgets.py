"""Drawn widgets for the desktop app: the live cockpit, pedal and force gauges, curve editor
and thumbnails, mode cards and the QR code."""

import copy
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo  # noqa: E402

from . import curves  # noqa: E402

TAU = 2 * math.pi

# The phone page's colors, so both screens read as one product.
PEDAL_COLORS = {"c": (0.345, 0.651, 1.0), "b": (0.973, 0.318, 0.286), "t": (0.247, 0.725, 0.314)}
WHEEL_MARKER = (0.949, 0.647, 0.255)
PEDAL_NAMES = {"c": "Clutch", "b": "Brake", "t": "Gas"}

CSS = """
.mode-card {
  padding: 12px;
  border-radius: 12px;
}
button.mode-card:checked, button.preset-card:checked {
  background-color: ACCENT_SOFT;
  box-shadow: inset 0 0 0 2px ACCENT;
}
.big-number {
  font-size: 34px;
  font-weight: 800;
  font-feature-settings: "tnum";
}
.fine-tuning > title { opacity: 0.6; font-size: 0.9em; }
.fine-tuning-body { opacity: 0.9; }
.part-button { padding: 6px 14px; border-radius: 999px; }
.preset-card { padding: 8px; border-radius: 10px; }
.chip {
  padding: 2px 10px;
  border-radius: 999px;
  background-color: alpha(currentColor, 0.1);
  font-size: 0.85em;
}
.cockpit-frame { border-radius: 16px; background-color: alpha(currentColor, 0.04); }
"""


def install_css() -> None:
    """Our styles, with the system accent color filled in (and kept current)."""
    provider = Gtk.CssProvider()

    def load(*_args):
        accent = accent_color()
        rgb = f"{round(accent.red * 255)}, {round(accent.green * 255)}, {round(accent.blue * 255)}"
        provider.load_from_string(CSS.replace("ACCENT_SOFT", f"rgba({rgb}, 0.16)")
                                  .replace("ACCENT", f"rgb({rgb})"))

    load()
    Adw.StyleManager.get_default().connect("notify::accent-color", load)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def accent_color() -> Gdk.RGBA:
    manager = Adw.StyleManager.get_default()
    if hasattr(manager, "get_accent_color_rgba"):
        return manager.get_accent_color_rgba()
    color = Gdk.RGBA()
    color.parse("#3584e4")
    return color


def rounded_rectangle(cr, x, y, width, height, radius) -> None:
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -TAU / 4, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, TAU / 4)
    cr.arc(x + radius, y + height - radius, radius, TAU / 4, TAU / 2)
    cr.arc(x + radius, y + radius, radius, TAU / 2, 3 * TAU / 4)
    cr.close_path()


def draw_text(widget, cr, text, x, y, size=11, weight="normal", alpha=1.0, align=0.5,
              color=None) -> None:
    """Text centered (align 0.5), left (0) or right (1) on x, with its top at y."""
    layout = widget.create_pango_layout(text)
    description = Pango.FontDescription.from_string(f"Sans {weight} {size}")
    layout.set_font_description(description)
    width, _height = layout.get_pixel_size()
    fg = color or widget.get_color()
    cr.set_source_rgba(fg.red, fg.green, fg.blue, alpha)
    cr.move_to(x - width * align, y)
    PangoCairo.show_layout(cr, layout)


def draw_wheel(cr, fg, cx, cy, radius, angle_degrees, force=0.0) -> None:
    """The phone page's wheel: rim, three spokes, amber marker; force as an arc above."""
    cr.save()
    cr.translate(cx, cy)
    if abs(force) > 0.02:
        r, g, b = WHEEL_MARKER
        cr.set_source_rgba(r, g, b, 0.9)
        cr.set_line_width(radius * 0.08)
        cr.set_line_cap(1)  # round
        start = -TAU / 4
        end = start + force * TAU / 4
        (cr.arc if force > 0 else cr.arc_negative)(0, 0, radius * 1.12, start, end)
        cr.stroke()
    cr.rotate(math.radians(angle_degrees))
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
    cr.set_line_width(radius * 0.16)
    cr.arc(0, 0, radius * 0.92, 0, TAU)
    cr.stroke()
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.25)
    spoke = radius * 0.1
    cr.rectangle(-radius * 0.84, -spoke / 2, radius * 1.68, spoke)
    cr.rectangle(-spoke / 2, 0, spoke, radius * 0.84)
    cr.fill()
    cr.arc(0, 0, radius * 0.26, 0, TAU)
    cr.fill()
    r, g, b = WHEEL_MARKER
    cr.set_source_rgb(r, g, b)
    rounded_rectangle(cr, -radius * 0.05, -radius * 1.02, radius * 0.1, radius * 0.24, 3)
    cr.fill()
    cr.restore()


class Cockpit(Gtk.DrawingArea):
    """The live rig: wheel, signal lever and pedals as the game gets them. Each part is a
    link to its settings page."""

    PARTS = ("wheel", "lever", "shifter", "c", "b", "t", "force")

    def __init__(self, on_part):
        super().__init__(content_height=280, hexpand=True)
        self.on_part = on_part
        self.monitor = None
        self.range = 900
        self.regions: dict[str, tuple] = {}
        self.hover: str | None = None
        self.set_draw_func(self._draw)
        self.set_cursor_from_name("default")
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.add_controller(click)

    def update(self, monitor, wheel_range) -> None:
        self.monitor, self.range = monitor, wheel_range
        self.queue_draw()

    def _part_at(self, x, y):
        for part, (rx, ry, rw, rh) in self.regions.items():
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return part
        return None

    def _set_hover(self, part) -> None:
        if part != self.hover:
            self.hover = part
            self.set_cursor_from_name("pointer" if part else "default")
            self.queue_draw()

    def _motion(self, _controller, x, y) -> None:
        self._set_hover(self._part_at(x, y))

    def _clicked(self, _gesture, _presses, x, y) -> None:
        part = self._part_at(x, y)
        if part:
            self.on_part(part)

    def _highlight(self, cr, part, pad=6) -> None:
        if self.hover != part:
            return
        x, y, w, h = self.regions[part]
        accent = accent_color()
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.12)
        rounded_rectangle(cr, x - pad, y - pad, w + 2 * pad, h + 2 * pad, 14)
        cr.fill()

    def _draw(self, area, cr, width, height) -> None:
        fg = area.get_color()
        monitor = self.monitor or {}
        shown = monitor.get("shown", {})
        output = monitor.get("output", {})
        forces = monitor.get("ff", {})
        buttons = monitor.get("buttons", 0)

        # Wheel on the left, lever beside it, pedals on the right; centered as a group.
        radius = min(height * 0.36, width * 0.17)
        pedal_width = max(min(width * 0.07, 64), 36)
        pedal_height = height * 0.7
        gap = pedal_width * 0.35
        lever_width = 26
        group = 2.3 * radius + 30 + 2 * lever_width + 22 + 40 + 3 * pedal_width + 2 * gap
        left = max((width - group) / 2, 10)
        cx, cy = left + radius * 1.15, height * 0.46
        self.regions["wheel"] = (cx - radius * 1.15, cy - radius * 1.15, radius * 2.3,
                                 radius * 2.3 + 34)
        self.regions["force"] = (cx - radius * 0.7, cy - radius * 1.3, radius * 1.4,
                                 radius * 0.22)
        lever_x = cx + radius * 1.15 + 30
        lever_h = pedal_height * 0.55
        lever_y = cy + radius - lever_h + 10
        self.regions["lever"] = (lever_x - 8, lever_y - 8, lever_width + 16, lever_h + 16)
        shifter_x = lever_x + lever_width + 22
        self.regions["shifter"] = (shifter_x - 8, lever_y - 8, lever_width + 16, lever_h + 16)
        pedal_left = shifter_x + lever_width + 40
        pedal_top = (height - pedal_height) / 2 - 10
        for index, key in enumerate(("c", "b", "t")):
            self.regions[key] = (pedal_left + index * (pedal_width + gap), pedal_top,
                                 pedal_width, pedal_height + 30)

        for part in ("wheel", "lever", "shifter", "c", "b", "t"):
            self._highlight(cr, part)

        steering = output.get("s", 0.0)
        angle = steering * self.range / 2
        draw_wheel(cr, fg, cx, cy, radius, angle, forces.get("force", 0.0))
        draw_text(self, cr, f"{angle:+.0f}°" if round(angle) else "0°", cx,
                  cy + radius * 1.15 + 4, size=15, weight="bold")

        # The stalks: an end lights while the game gets that button.
        for x, name, ends, lit_color in (
                (lever_x, "Signal", ((13, "▲"), (12, "▼")), "#3fb950"),
                (shifter_x, "Shift", ((1, "+"), (0, "−")), "#f2a541")):
            slot_x = x + lever_width / 2
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            rounded_rectangle(cr, slot_x - 4, lever_y + 18, 8, lever_h - 36, 4)
            cr.fill()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            rounded_rectangle(cr, x, lever_y + lever_h / 2 - 9, lever_width, 18, 5)
            cr.fill()
            for (bit, text), y in zip(ends, (lever_y, lever_y + lever_h - 16)):
                lit = buttons & (1 << bit)
                color = None
                if lit:
                    color = Gdk.RGBA()
                    color.parse(lit_color)
                draw_text(self, cr, text, slot_x, y, size=10, weight="bold" if lit else "normal",
                          alpha=1 if lit else 0.5, color=color)
            draw_text(self, cr, name, slot_x, lever_y + lever_h + 6, size=9, alpha=0.6)

        for key in ("c", "b", "t"):
            x, y, w, _h = self.regions[key]
            r, g, b = PEDAL_COLORS[key]
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.1)
            rounded_rectangle(cr, x, y, w, pedal_height, 10)
            cr.fill()
            value = shown.get(key, 0.0)
            cr.save()
            rounded_rectangle(cr, x, y, w, pedal_height, 10)
            cr.clip()
            cr.set_source_rgba(r, g, b, 0.75)
            cr.rectangle(x, y + pedal_height * (1 - value), w, pedal_height * value)
            cr.fill()
            out = output.get(key, 0.0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
            cr.rectangle(x, y + pedal_height * (1 - out) - 1.5, w, 3)
            cr.fill()
            cr.restore()
            draw_text(self, cr, PEDAL_NAMES[key], x + w / 2, y + pedal_height + 6, size=10,
                      alpha=0.7)
            draw_text(self, cr, f"{out * 100:.0f}%", x + w / 2, y + pedal_height + 21,
                      size=11, weight="bold")


class PedalGauge(Gtk.DrawingArea):
    """A big live pedal with two markers you drag: where pressing starts to count and where
    the pedal is fully pressed. Moving them rewrites the pedal's input curve."""

    def __init__(self, key, on_travel):
        super().__init__(content_width=190, content_height=300)
        self.key = key
        self.on_travel = on_travel
        self.start, self.full = 0.0, 1.0
        self.shown = self.raw = self.output = 0.0
        self.game = None
        self.dragging = None
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        self.set_cursor_from_name("ns-resize")

    def _bar(self):
        height = self.get_height()
        return 96, 12, 70, height - 24  # x, y, width, height

    def _y_of(self, value):
        _x, y, _w, h = self._bar()
        return y + h * (1 - value)

    def set_travel(self, start, full) -> None:
        self.start, self.full = start, full
        self.queue_draw()

    def set_live(self, raw, shown, output, game=None) -> None:
        self.raw, self.shown, self.output, self.game = raw, shown, output, game
        self.queue_draw()

    def _begin(self, gesture, _x, y) -> None:
        self.drag_y = y
        to_start = abs(y - self._y_of(self.start))
        to_full = abs(y - self._y_of(self.full))
        self.dragging = "start" if to_start < to_full else "full"

    def _update(self, _gesture, _dx, dy) -> None:
        _x, top, _w, h = self._bar()
        value = 1 - (self.drag_y + dy - top) / h
        value = round(min(max(value, 0.0), 1.0), 2)
        if self.dragging == "start":
            self.start = min(value, self.full - 0.05)
        else:
            self.full = max(value, self.start + 0.05)
        self.queue_draw()

    def _end(self, *_args) -> None:
        if self.dragging:
            self.dragging = None
            self.on_travel(self.start, self.full)

    def _draw(self, area, cr, width, height) -> None:
        fg = area.get_color()
        r, g, b = PEDAL_COLORS[self.key]
        x, y, w, h = self._bar()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.1)
        rounded_rectangle(cr, x, y, w, h, 14)
        cr.fill()
        # The part of the travel that does something.
        cr.set_source_rgba(r, g, b, 0.1)
        cr.rectangle(x, self._y_of(self.full), w, self._y_of(self.start) - self._y_of(self.full))
        cr.fill()
        cr.save()
        rounded_rectangle(cr, x, y, w, h, 14)
        cr.clip()
        cr.set_source_rgba(r, g, b, 0.8)
        cr.rectangle(x, self._y_of(self.shown), w, h * self.shown)
        cr.fill()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.95)
        cr.rectangle(x, self._y_of(self.output) - 1.5, w, 3)
        cr.fill()
        if self.game is not None:
            accent = accent_color()
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
            cr.set_dash([6, 4])
            cr.set_line_width(2)
            cr.move_to(x, self._y_of(self.game))
            cr.line_to(x + w, self._y_of(self.game))
            cr.stroke()
            cr.set_dash([])
        cr.restore()
        # Finger position on the phone.
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
        cr.move_to(x + w + 4, self._y_of(self.raw))
        cr.line_to(x + w + 12, self._y_of(self.raw) - 5)
        cr.line_to(x + w + 12, self._y_of(self.raw) + 5)
        cr.fill()
        # Draggable travel markers on the left.
        for value, text in ((self.start, "starts"), (self.full, "full")):
            my = self._y_of(value)
            cr.set_source_rgba(r, g, b, 1)
            cr.move_to(x - 2, my)
            cr.line_to(x - 14, my - 8)
            cr.line_to(x - 14, my + 8)
            cr.close_path()
            cr.fill()
            cr.set_source_rgba(r, g, b, 0.6)
            cr.set_line_width(1)
            cr.move_to(x, my)
            cr.line_to(x + w, my)
            cr.stroke()
            draw_text(self, cr, f"{text} {value * 100:.0f}%", x - 18, my - 8, size=9, align=1,
                      alpha=0.8)


def travel_points(start: float, full: float) -> list[list[float]]:
    points = [[0.0, 0.0]]
    if start > 0:
        points.append([start, 0.0])
    if full < 1:
        points.append([full, 1.0])
    points.append([1.0, 1.0])
    return points


def travel_of(points) -> tuple[float, float, bool]:
    """Where an input curve starts and reaches full, and whether it is a plain straight
    travel (anything else was shaped in fine-tuning)."""
    curve = curves.Curve(points)
    start = 0.0
    for x, y in curve.points:
        if y > 0:
            break
        start = x
    full = 1.0
    for x, y in curve.points:
        if y >= 1:
            full = x
            break
    plain = curves.normalize(travel_points(start, full)) == curve.points
    return start, full, plain


class CurveThumb(Gtk.DrawingArea):
    def __init__(self, points, color=None):
        super().__init__(content_width=72, content_height=48)
        self.curve = curves.Curve(points)
        self.color = color
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, width, height) -> None:
        fg = area.get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
        cr.set_line_width(1)
        cr.move_to(4, height - 4)
        cr.line_to(width - 4, 4)
        cr.stroke()
        if self.color:
            cr.set_source_rgb(*self.color)
        else:
            accent = accent_color()
            cr.set_source_rgb(accent.red, accent.green, accent.blue)
        cr.set_line_width(2.5)
        for index, (x, y) in enumerate(self.curve.points):
            sx, sy = 4 + x * (width - 8), height - 4 - y * (height - 8)
            (cr.move_to if index == 0 else cr.line_to)(sx, sy)
        cr.stroke()


class PresetPicker(Gtk.FlowBox):
    """Curve presets as little pictures. The one matching the current curve is selected."""

    def __init__(self, presets: dict, on_pick, color=None):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8,
                         row_spacing=8, homogeneous=True, max_children_per_line=6,
                         min_children_per_line=3)
        self.on_pick = on_pick
        self.buttons = {}
        group = None
        for name, points in presets.items():
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.append(CurveThumb(points, color))
            label = Gtk.Label(label=name, wrap=True, justify=Gtk.Justification.CENTER)
            label.add_css_class("caption")
            box.append(label)
            button = Gtk.ToggleButton(child=box)
            button.add_css_class("flat")
            button.add_css_class("preset-card")
            if group:
                button.set_group(group)
            group = group or button
            button.connect("toggled", self._toggled, name)
            self.buttons[name] = button
            self.append(button)
        self.updating = False

    def _toggled(self, button, name) -> None:
        if button.get_active() and not self.updating:
            self.on_pick(name)

    def show_current(self, points, presets: dict) -> None:
        self.updating = True
        current = curves.normalize(points)
        matched = False
        for name, button in self.buttons.items():
            match = curves.normalize(presets[name]) == current
            button.set_active(match)
            matched |= match
        if not matched:
            # No preset matches: clear the group's selection.
            for button in self.buttons.values():
                button.set_active(False)
        self.updating = False


class ModeCard(Gtk.ToggleButton):
    """A big choice: a little drawing, a title and one line saying what it does."""

    def __init__(self, title: str, description: str, draw=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        if draw:
            picture = Gtk.DrawingArea(content_width=96, content_height=56,
                                      halign=Gtk.Align.CENTER)
            picture.set_draw_func(draw)
            box.append(picture)
        heading = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER)
        heading.add_css_class("heading")
        box.append(heading)
        text = Gtk.Label(label=description, wrap=True, justify=Gtk.Justification.CENTER,
                         max_width_chars=22)
        text.add_css_class("caption")
        text.add_css_class("dim-label")
        box.append(text)
        super().__init__(child=box, hexpand=True)
        self.add_css_class("flat")
        self.add_css_class("mode-card")


class CardRow(Gtk.Box):
    """Mutually exclusive mode cards bound to one setting."""

    def __init__(self, choices, on_pick):
        super().__init__(spacing=12, homogeneous=True)
        self.cards = {}
        self.updating = False
        group = None
        for value, card in choices:
            if group:
                card.set_group(group)
            group = group or card
            card.connect("toggled", self._toggled, value, on_pick)
            self.cards[value] = card
            self.append(card)

    def _toggled(self, card, value, on_pick) -> None:
        if card.get_active() and not self.updating:
            on_pick(value)

    def select(self, value) -> None:
        self.updating = True
        if value in self.cards:
            self.cards[value].set_active(True)
        self.updating = False


class RotationDial(Gtk.DrawingArea):
    """Rotation lock to lock as turns around the wheel: each side spirals out per turn."""

    def __init__(self):
        super().__init__(content_width=170, content_height=170)
        self.range = 900
        self.set_draw_func(self._draw)

    def set_range(self, degrees) -> None:
        self.range = degrees
        self.queue_draw()

    def _draw(self, area, cr, width, height) -> None:
        fg = area.get_color()
        cx, cy = width / 2, height / 2
        radius = min(width, height) * 0.3
        draw_wheel(cr, fg, cx, cy, radius * 0.85, 0)
        r, g, b = WHEEL_MARKER
        cr.set_line_width(4)
        cr.set_line_cap(1)
        half = math.radians(self.range / 2)
        for sign in (1, -1):
            cr.set_source_rgba(r, g, b, 0.9 if sign > 0 else 0.55)
            steps = max(int(abs(half) / 0.05), 2)
            for step in range(steps + 1):
                t = half * step / steps
                spiral = radius * 1.12 + t / TAU * 9
                angle = -TAU / 4 + sign * t
                x, y = cx + spiral * math.cos(angle), cy + spiral * math.sin(angle)
                (cr.move_to if step == 0 else cr.line_to)(x, y)
            cr.stroke()


class ForceGauge(Gtk.DrawingArea):
    """Steering force from the game, pushing left or right from the middle, with rumble."""

    def __init__(self):
        super().__init__(content_width=320, content_height=86, hexpand=True)
        self.force = self.rumble = 0.0
        self.set_draw_func(self._draw)

    def update(self, force, rumble) -> None:
        if (force, rumble) != (self.force, self.rumble):
            self.force, self.rumble = force, rumble
            self.queue_draw()

    def _draw(self, area, cr, width, height) -> None:
        fg = area.get_color()
        r, g, b = WHEEL_MARKER
        middle = width / 2
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.1)
        rounded_rectangle(cr, 0, 14, width, 22, 11)
        cr.fill()
        cr.set_source_rgba(r, g, b, 0.9)
        end = middle + self.force * middle
        rounded_rectangle(cr, min(middle, end), 14, abs(end - middle), 22, 6)
        cr.fill()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
        cr.rectangle(middle - 1, 10, 2, 30)
        cr.fill()
        draw_text(self, cr, "left", 0, 40, size=9, align=0, alpha=0.6)
        draw_text(self, cr, "right", width, 40, size=9, align=1, alpha=0.6)
        draw_text(self, cr, f"{abs(self.force) * 100:.0f}%", middle, 40, size=10, alpha=0.8)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.1)
        rounded_rectangle(cr, 0, 64, width, 8, 4)
        cr.fill()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
        rounded_rectangle(cr, 0, 64, width * self.rumble, 8, 4)
        cr.fill()
        draw_text(self, cr, "rumble", 0, 74, size=8, align=0, alpha=0.5)


class CurveEditor(Gtk.Box):
    """Drag points to shape a curve. Double-click adds a point, right-click removes one."""

    PICK_RADIUS = 14
    MARGIN = 14

    def __init__(self, title: str, x_label: str, y_label: str, on_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.on_change = on_change
        self.curve = curves.Curve(curves.PRESETS["linear"])
        self.points = self.curve.points
        self.live: float | None = None
        self.dragging: int | None = None
        self.drag_start = (0.0, 0.0)

        header = Gtk.Box(spacing=6)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        label.add_css_class("heading")
        header.append(label)
        names = list(curves.PRESETS)
        self.presets = Gtk.DropDown.new_from_strings(["Preset…"] + [n.capitalize() for n in names])
        self.presets.set_tooltip_text("Replace the curve with a preset")
        self.presets.connect("notify::selected", self._preset_chosen, names)
        header.append(self.presets)
        self.append(header)

        self.area = Gtk.DrawingArea(content_width=240, content_height=190, hexpand=True)
        self.area.set_draw_func(self._draw)
        self.area.set_tooltip_text(f"{x_label} → {y_label}. Drag points; double-click adds one, "
                                   "right-click removes one.")
        self.append(Gtk.Frame(child=self.area))
        axes = Gtk.Label(label=f"→ {x_label}   ↑ {y_label}", xalign=0)
        axes.add_css_class("dim-label")
        axes.add_css_class("caption")
        self.append(axes)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.area.add_controller(drag)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._pressed)
        self.area.add_controller(click)

    def _plot(self):
        width, height = self.area.get_width(), self.area.get_height()
        m = self.MARGIN
        return m, m, max(width - 2 * m, 1), max(height - 2 * m, 1)

    def _to_screen(self, x: float, y: float):
        left, top, width, height = self._plot()
        return left + x * width, top + (1 - y) * height

    def _to_curve(self, sx: float, sy: float):
        left, top, width, height = self._plot()
        return (sx - left) / width, 1 - (sy - top) / height

    def _nearest(self, sx: float, sy: float) -> int | None:
        best, best_distance = None, self.PICK_RADIUS
        for index, (x, y) in enumerate(self.points):
            px, py = self._to_screen(x, y)
            distance = math.hypot(px - sx, py - sy)
            if distance <= best_distance:
                best, best_distance = index, distance
        return best

    def set_points(self, points) -> None:
        if self.dragging is not None:
            return
        self.curve = curves.Curve(points)
        self.points = [list(point) for point in self.curve.points]
        self.area.queue_draw()

    def set_live(self, x: float | None) -> None:
        x = None if x is None else min(max(abs(x), 0.0), 1.0)
        if x != self.live:
            self.live = x
            self.area.queue_draw()

    def _commit(self) -> None:
        self.curve = curves.Curve(self.points)
        self.points = [list(point) for point in self.curve.points]
        self.area.queue_draw()
        self.on_change([list(point) for point in self.points])

    def _preset_chosen(self, dropdown, _param, names) -> None:
        index = dropdown.get_selected()
        if index == 0:
            return
        self.points = copy.deepcopy(curves.PRESETS[names[index - 1]])
        self._commit()
        dropdown.set_selected(0)

    def _drag_begin(self, gesture, sx, sy) -> None:
        self.dragging = self._nearest(sx, sy)
        if self.dragging is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self.drag_start = (sx, sy)

    def _drag_update(self, _gesture, dx, dy) -> None:
        if self.dragging is None:
            return
        index = self.dragging
        x, y = self._to_curve(self.drag_start[0] + dx, self.drag_start[1] + dy)
        y = round(min(max(y, 0.0), 1.0), 3)
        if index == 0:
            x = 0.0
        elif index == len(self.points) - 1:
            x = 1.0
        else:
            # A little over the minimum gap, so cleaning up the points never merges this one.
            low = self.points[index - 1][0] + curves.MIN_GAP * 1.05
            high = self.points[index + 1][0] - curves.MIN_GAP * 1.05
            # Neighbours too close to fit between: keep x, let the height still move.
            x = self.points[index][0] if low > high else min(max(x, low), high)
        self.points[index] = [x, y]
        self.curve = curves.Curve(self.points)
        self.area.queue_draw()

    def _drag_end(self, _gesture, _dx, _dy) -> None:
        if self.dragging is not None:
            self.dragging = None
            self._commit()

    def _pressed(self, gesture, presses, sx, sy) -> None:
        button = gesture.get_current_button()
        index = self._nearest(sx, sy)
        if button == Gdk.BUTTON_SECONDARY:
            if index is not None and 0 < index < len(self.points) - 1:
                del self.points[index]
                self._commit()
        elif presses == 2 and index is None:
            x, y = self._to_curve(sx, sy)
            if 0 < x < 1:
                self.points.append([round(x, 3), round(min(max(y, 0.0), 1.0), 3)])
                self._commit()

    def _draw(self, area, cr, width, height) -> None:
        fg = area.get_color()
        accent = accent_color()
        left, top, plot_width, plot_height = self._plot()
        cr.set_line_width(1)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        for step in range(5):
            fraction = step / 4
            cr.move_to(left + fraction * plot_width, top)
            cr.line_to(left + fraction * plot_width, top + plot_height)
            cr.move_to(left, top + fraction * plot_height)
            cr.line_to(left + plot_width, top + fraction * plot_height)
        cr.stroke()
        cr.set_dash([4, 4])
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.25)
        cr.move_to(*self._to_screen(0, 0))
        cr.line_to(*self._to_screen(1, 1))
        cr.stroke()
        cr.set_dash([])
        cr.set_line_width(2.5)
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
        for index, (x, y) in enumerate(self.points):
            (cr.move_to if index == 0 else cr.line_to)(*self._to_screen(x, y))
        cr.stroke()
        for index, (x, y) in enumerate(self.points):
            px, py = self._to_screen(x, y)
            cr.arc(px, py, 6 if index == self.dragging else 4.5, 0, TAU)
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
            cr.fill_preserve()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
            cr.set_line_width(1)
            cr.stroke()
        if self.live is not None:
            px, py = self._to_screen(self.live, self.curve(self.live))
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
            cr.set_line_width(1)
            cr.move_to(px, top + plot_height)
            cr.line_to(px, py)
            cr.line_to(left, py)
            cr.stroke()
            cr.arc(px, py, 5, 0, TAU)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 1)
            cr.fill()


class QrCode(Gtk.DrawingArea):
    def __init__(self, size=180):
        super().__init__(content_width=size, content_height=size, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER)
        self.matrix = None
        self.set_draw_func(self._draw)

    def set_url(self, url: str | None) -> None:
        self.matrix = None
        if url:
            try:
                import qrcode

                code = qrcode.QRCode(border=2)
                code.add_data(url)
                code.make()
                self.matrix = code.get_matrix()
            except ImportError:
                pass
        self.queue_draw()

    def _draw(self, _area, cr, width, height) -> None:
        if not self.matrix:
            return
        count = len(self.matrix)
        cell = min(width, height) / count
        cr.set_source_rgb(1, 1, 1)
        rounded_rectangle(cr, 0, 0, cell * count, cell * count, 8)
        cr.fill()
        cr.set_source_rgb(0, 0, 0)
        for row, cells in enumerate(self.matrix):
            for column, dark in enumerate(cells):
                if dark:
                    cr.rectangle(column * cell, row * cell, cell + 0.5, cell + 0.5)
        cr.fill()


# ---------- little drawings for the mode cards ----------

def _fg(area):
    return area.get_color()


def draw_touch_wheel(area, cr, width, height) -> None:
    fg = _fg(area)
    draw_wheel(cr, fg, width / 2, height / 2, height * 0.38, -25)
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
    cr.arc(width / 2 + height * 0.3, height * 0.3, 6, 0, TAU)
    cr.fill()


def _phone(cr, fg, cx, cy, width, height, tilt) -> None:
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(math.radians(tilt))
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
    cr.set_line_width(2)
    rounded_rectangle(cr, -width / 2, -height / 2, width, height, 4)
    cr.stroke()
    cr.restore()


def draw_tilt_speed(area, cr, width, height) -> None:
    fg = _fg(area)
    draw_wheel(cr, fg, width * 0.32, height / 2, height * 0.34, 40)
    _phone(cr, fg, width * 0.75, height / 2, 18, 32, 20)
    cr.set_source_rgba(*WHEEL_MARKER, 1)
    cr.set_line_width(2)
    cr.arc(width * 0.75, height / 2, 22, -TAU * 0.4, -TAU * 0.15)
    cr.stroke()


def draw_gyro(area, cr, width, height) -> None:
    fg = _fg(area)
    _phone(cr, fg, width / 2, height / 2, 56, 30, -18)
    cr.set_source_rgba(*WHEEL_MARKER, 1)
    cr.set_line_width(2)
    cr.arc(width / 2, height / 2, 26, -TAU * 0.42, -TAU * 0.08)
    cr.stroke()


def draw_touch_slider(area, cr, width, height) -> None:
    fg = _fg(area)
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.2)
    rounded_rectangle(cr, width / 2 - 12, 4, 24, height - 8, 6)
    cr.fill()
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
    rounded_rectangle(cr, width / 2 - 12, height * 0.45, 24, height * 0.55 - 4, 6)
    cr.fill()
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.95)
    cr.arc(width / 2, height * 0.45, 6, 0, TAU)
    cr.fill()


def draw_tilt_pedal(area, cr, width, height) -> None:
    fg = _fg(area)
    _phone(cr, fg, width / 2, height / 2, 20, 36, 25)
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
    cr.set_line_width(2)
    cr.arc(width / 2, height / 2, 24, -TAU * 0.35, -TAU * 0.15)
    cr.stroke()


def draw_pulse(area, cr, width, height) -> None:
    fg = _fg(area)
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
    cr.set_line_width(2)
    base = height * 0.7
    cr.move_to(6, base)
    for start in (0.2, 0.6):
        cr.line_to(width * start, base)
        cr.line_to(width * start, height * 0.25)
        cr.line_to(width * (start + 0.1), height * 0.25)
        cr.line_to(width * (start + 0.1), base)
    cr.line_to(width - 6, base)
    cr.stroke()


def draw_hold(area, cr, width, height) -> None:
    fg = _fg(area)
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
    cr.set_line_width(2)
    base = height * 0.7
    cr.move_to(6, base)
    cr.line_to(width * 0.2, base)
    cr.line_to(width * 0.2, height * 0.25)
    cr.line_to(width * 0.75, height * 0.25)
    cr.line_to(width * 0.75, base)
    cr.line_to(width - 6, base)
    cr.stroke()


def draw_camera(area, cr, width, height) -> None:
    """The back of a phone with its camera, a fingertip half over the lens."""
    fg = _fg(area)
    cx, cy = width / 2, height / 2
    _phone(cr, fg, cx, cy, 30, 50, 0)
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
    cr.set_line_width(2)
    cr.arc(cx - 6, cy - 14, 5, 0, TAU)
    cr.stroke()
    cr.set_source_rgba(*WHEEL_MARKER, 0.85)
    cr.arc(cx + 4, cy - 10, 9, 0, TAU)
    cr.fill()


def _stalk(cr, fg, width, height, up_label, down_label, highlight_up, area) -> None:
    cx = width / 2
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.2)
    rounded_rectangle(cr, cx - 4, 10, 8, height - 20, 4)
    cr.fill()
    cr.set_source_rgba(*WHEEL_MARKER, 1)
    rounded_rectangle(cr, cx - 12, 12 if highlight_up else height - 24, 24, 12, 4)
    cr.fill()
    draw_text(area, cr, up_label, cx + 24, 4, size=10, weight="bold")
    draw_text(area, cr, down_label, cx + 24, height - 18, size=10, weight="bold")


def draw_shift_up(area, cr, width, height) -> None:
    _stalk(cr, _fg(area), width, height, "+", "−", True, area)


def draw_shift_rally(area, cr, width, height) -> None:
    _stalk(cr, _fg(area), width, height, "−", "+", False, area)
