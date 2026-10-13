"""On-screen indicators drawn over Roblox while the bot runs.

The original app had three overlay switches (``global_gui_settings``):

* Show Status Overlay       - a small pill: stage, hold/release, error, counts
* Show Fishing Overlay      - fish box brackets, fish line / bar / steering
                              markers, plus rod-special markers (Pinions note,
                              Halibut hidden-line hold, Stellarwave sign aim)
* Show Perfect Cast Overlay - fill gauge next to the cast bobber: current fill,
                              predicted fill and the release threshold

The bot threads publish plain dicts into ``bot.overlay`` (screen coordinates,
with a timestamp ``t``); this window just redraws them ~30x a second.

Windows only: the window is transparent, click-through, never takes focus and
is excluded from screen capture (``WDA_EXCLUDEFROMCAPTURE``) so the bot's own
MSS/DXcam grabs never see the drawings. If capture exclusion isn't available
(Windows < 10 2004), only the status pill is drawn, placed outside every area
the bot scans.
"""
from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk

IS_WINDOWS = sys.platform == "win32"

KEY = "#010203"  # transparent colour key
FONT = "Segoe UI" if IS_WINDOWS else "DejaVu Sans"
MONO = "Consolas" if IS_WINDOWS else "DejaVu Sans Mono"

# palette borrowed from the minigame visuals (pastel lilac/cyan glow on dark)
PANEL = "#1b1f2a"
PANEL_EDGE = "#4a4f7a"
TEXT = "#e9e6ff"
MUTED = "#a9a3cf"
LILAC = "#b9a7ff"
CYAN = "#9fe3ff"
FISH = "#ff6b8a"
BAR = "#7cf0b0"
AIM = "#6fa8ff"
WARN = "#ffd36b"
HOLD = "#7cf0b0"
RELEASE = "#ffb36b"

STALE = 0.6  # seconds before a published state is considered gone


def _fresh(state, now, max_age=STALE):
    return bool(state) and now - state.get("t", 0) <= max_age


def draw(c: tk.Canvas, bot, prefs, now, capture_safe=True, origin=(0, 0)):
    """Draw every enabled indicator for ``bot.overlay`` onto canvas ``c``.

    ``origin`` is the screen position of the canvas' (0, 0).
    """
    ox, oy = origin
    c.delete("hud")
    data = getattr(bot, "overlay", {}) or {}

    def X(x):
        return x - ox

    def Y(y):
        return y - oy

    if prefs.get("Show Fishing Overlay", True) and capture_safe:
        fish = data.get("fish")
        if _fresh(fish, now):
            _draw_fish(c, fish, data, now, X, Y)
    if prefs.get("Show Perfect Cast Overlay", True) and capture_safe:
        cast = data.get("cast")
        if _fresh(cast, now, 1.0):
            _draw_cast(c, cast, X, Y)
    if prefs.get("Show Status Overlay", True):
        _draw_status(c, bot, data, now, X, Y)


def _text(c, x, y, s, color=TEXT, size=9, weight="bold", anchor="w", font=FONT):
    c.create_text(x + 1, y + 1, text=s, fill="#000001", font=(font, size, weight),
                  anchor=anchor, tags="hud")
    c.create_text(x, y, text=s, fill=color, font=(font, size, weight), anchor=anchor,
                  tags="hud")


def _brackets(c, x1, y1, x2, y2, color, arm=14, width=2):
    for (x, y, dx, dy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
        c.create_line(x, y, x + dx * arm, y, fill=color, width=width, tags="hud")
        c.create_line(x, y, x, y + dy * arm, fill=color, width=width, tags="hud")


def _draw_fish(c, fish, data, now, X, Y):
    box = fish["box"]
    x1, y1, x2, y2 = X(box["x1"]), Y(box["y1"]), X(box["x2"]), Y(box["y2"])
    holding = fish.get("holding")
    _brackets(c, x1 - 6, y1 - 6, x2 + 6, y2 + 6, HOLD if holding else LILAC)

    # rail under the bar: bar extent (green), fish (pink), steering aim (blue)
    ry = y2 + 14
    c.create_line(x1, ry, x2, ry, fill=PANEL_EDGE, width=4, capstyle="round", tags="hud")
    bl, br = fish.get("bar") or (None, None)
    if bl is not None and br is not None:
        c.create_line(x1 + bl, ry, x1 + br, ry, fill=BAR, width=6, capstyle="round", tags="hud")
    target = fish.get("target")
    if target is not None:
        tx = x1 + target
        hidden = fish.get("hidden")
        color = WARN if hidden else FISH
        # pointer above the bar, outside the box so it never covers the fish line
        c.create_polygon(tx - 7, y1 - 16, tx + 7, y1 - 16, tx, y1 - 7,
                         fill="" if hidden else color, outline=color, width=2, tags="hud")
        c.create_polygon(tx - 6, ry + 11, tx + 6, ry + 11, tx, ry + 3,
                         fill=color, outline=color, tags="hud")
        if hidden:
            _text(c, tx + 10, y1 - 14, "line hidden – holding", WARN, 8)
    aim = fish.get("aim")
    if aim is not None and target is not None and abs(aim - target) > 3:
        ax = x1 + aim
        c.create_polygon(ax, ry - 7, ax + 7, ry, ax, ry + 7, ax - 7, ry,
                         fill=AIM, outline="#ffffff", tags="hud")
        c.create_line(x1 + target, ry, ax, ry, fill=AIM, width=2, dash=(4, 3), tags="hud")

    state = "HOLD" if holding else "RELEASE"
    err = fish.get("err")
    _text(c, x2 + 14, (y1 + y2) / 2 - 7, state, HOLD if holding else RELEASE, 10)
    if err is not None:
        _text(c, x2 + 14, (y1 + y2) / 2 + 9, f"err {err:+.0f}px", MUTED, 8, "normal", font=MONO)

    special = fish.get("special")
    if special:
        _text(c, x1, y1 - 30, f"✧ {special}", LILAC, 9)

    note = fish.get("note_x")
    if note is not None:
        nx = x1 + note
        c.create_line(nx, y1 - 70, nx, y1 - 20, fill=CYAN, width=2, dash=(5, 4),
                      arrow="last", arrowshape=(10, 12, 5), tags="hud")
        _text(c, nx + 8, y1 - 64, "♪ catch", CYAN, 9)

    zod = data.get("zodiac")
    if _fresh(zod, now, 0.9):
        zx, zy = X(zod["x"]), Y(zod["y"])
        r = 26
        c.create_oval(zx - r, zy - r, zx + r, zy + r, outline=CYAN, width=3, tags="hud")
        c.create_oval(zx - r - 5, zy - r - 5, zx + r + 5, zy + r + 5, outline=LILAC, width=1,
                      tags="hud")
        _text(c, zx, zy + r + 14, f"{zod['sign']}  {zod['n']}/12", CYAN, 9, anchor="center")


def _draw_cast(c, cast, X, Y):
    # vertical gauge beside the bobber, scaled to the tracked fill bar
    gx = X(cast["x"]) + 46
    top, bottom = Y(cast["green_y"]), Y(cast["white_bottom"])
    if bottom - top < 30:
        top, bottom = bottom - 120, bottom
    h = bottom - top
    c.create_rectangle(gx - 8, top - 2, gx + 8, bottom + 2, fill=PANEL, outline=PANEL_EDGE,
                       width=2, tags="hud")

    def at(pct):
        return bottom - h * max(0.0, min(100.0, pct)) / 100.0

    fill = cast.get("fill", 0.0)
    pred = cast.get("predicted", fill)
    thr = cast.get("threshold", 95.5)
    c.create_rectangle(gx - 5, at(fill), gx + 5, bottom, fill=CYAN, outline="", tags="hud")
    if pred > fill:
        c.create_rectangle(gx - 5, at(pred), gx + 5, at(fill), fill=LILAC, outline="",
                           stipple="gray50", tags="hud")
    ty = at(thr)
    c.create_line(gx - 14, ty, gx + 14, ty, fill=WARN, width=2, tags="hud")
    released = cast.get("released")
    label = "RELEASE!" if released else f"{fill:4.1f}%"
    _text(c, gx + 18, ty, f"{thr:g}%", WARN, 8, "normal", font=MONO)
    _text(c, gx + 18, at(fill), label, HOLD if released else TEXT, 10)
    _text(c, gx - 10, top - 14, "PERFECT CAST", LILAC, 8)


def _draw_status(c, bot, data, now, X, Y):
    fish = data.get("fish")
    running = bot.is_running
    dot = HOLD if running else RELEASE
    parts = [bot.status_text or ("Running" if running else "Idle")]
    if bot.status_sub:
        parts.append(bot.status_sub)
    line2 = f"Fish {bot.loop_count}  ·  Cycles {bot._total_cycle_count}"
    if _fresh(fish, now) and fish.get("special"):
        line2 += f"  ·  {fish['special']}"
    x, y = X(16), Y(96)  # under Roblox's top-left buttons, outside the scan areas
    w = max(230, 8 * max(len(" · ".join(parts)), len(line2)) + 50)
    c.create_rectangle(x, y, x + w, y + 46, fill=PANEL, outline=PANEL_EDGE, width=1, tags="hud")
    c.create_rectangle(x, y, x + 3, y + 46, fill=LILAC, outline="", tags="hud")
    c.create_oval(x + 14, y + 11, x + 24, y + 21, fill=dot, outline="", tags="hud")
    _text(c, x + 32, y + 16, "♆ Hydra  " + "  ·  ".join(parts), TEXT, 9)
    _text(c, x + 32, y + 33, line2, MUTED, 8, "normal")


class Overlay:
    """Click-through, capture-excluded, always-on-top drawing surface."""

    FPS = 30

    def __init__(self, root: tk.Tk, bot, prefs):
        self.root = root
        self.bot = bot
        self.prefs = prefs  # callable -> dict of global_gui_settings
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=KEY)
        if IS_WINDOWS:
            self.win.attributes("-transparentcolor", KEY)
        x, y, w, h = self._screen()
        self.origin = (x, y)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.canvas = tk.Canvas(self.win, bg=KEY, highlightthickness=0, width=w, height=h)
        self.canvas.pack(fill="both", expand=True)
        self.capture_safe = False
        self._shown = False
        self._configured = False
        self._loop()

    @staticmethod
    def _screen():
        if IS_WINDOWS:
            m = ctypes.windll.user32.GetSystemMetrics
            return m(76), m(77), m(78), m(79)
        return 0, 0, 1920, 1080

    def _make_click_through(self):
        """Layered + transparent + no-activate, and hidden from screen capture."""
        if not IS_WINDOWS:
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            GWL_EXSTYLE = -20
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style |= 0x80000 | 0x20 | 0x80 | 0x08000000  # LAYERED|TRANSPARENT|TOOLWINDOW|NOACTIVATE
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            WDA_EXCLUDEFROMCAPTURE = 0x11
            self.capture_safe = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
            if not self.capture_safe:
                print(" Overlay: capture exclusion unavailable - showing status pill only")
        except Exception as e:
            print(f" Overlay setup failed: {e}")

    def _wanted(self):
        p = self.prefs()
        any_on = any(p.get(k, True) for k in (
            "Show Status Overlay", "Show Fishing Overlay", "Show Perfect Cast Overlay"))
        return any_on and self.bot.is_running

    def _loop(self):
        try:
            if self._wanted():
                if not self._shown:
                    self.win.deiconify()
                    self.win.lift()
                    self._shown = True
                    if not self._configured:
                        self.win.update_idletasks()
                        self._make_click_through()
                        self._configured = True
                draw(self.canvas, self.bot, self.prefs(), time.time(),
                     self.capture_safe, self.origin)
            elif self._shown:
                self.canvas.delete("hud")
                self.win.withdraw()
                self._shown = False
        except tk.TclError:
            return  # window destroyed on exit
        self.root.after(int(1000 / self.FPS), self._loop)

    def destroy(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
