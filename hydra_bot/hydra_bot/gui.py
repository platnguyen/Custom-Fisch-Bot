"""Desktop GUI for the core rewrite: ``python -m hydra_bot.gui`` (or HydraGUI.exe).

Runs the bot in-process, so no PowerShell / curl / hand-editing of config.json
is needed. Every change is written straight back to config.json. The HTTP API
is still started (if the port is free) so existing tooling keeps working.

Dark UI modelled on the original Hydra front-end. Tabs:
    Main       capture mode, hotkeys + hotbar slots (Rebind), window prefs,
               detection areas (drag-to-select), cursor park point, stage
               toggles, loadout
    Cast       cast style and its step-by-step sequence (Normal / Perfect)
    Shake      shake style and pixel-shake tuning
    Fish       track style, rods (add/delete), per-rod colours with a screen
               eyedropper, per-rod reel tuning (``<key>_<Rod>``)
    Totem      totem style/trigger/timing and potions
    Reconnect  auto-reconnect settings (stored; not run by the core rewrite)
    Discord    webhook notifications (start/stop, every N loops, status,
               errors; text or screenshot) + a test button
    Extra      session info, config reload/reset, searchable editor for every
               key in config.json, live log

Global hotkeys (work while Roblox is focused) come from ``hotkeys`` in the
config: Start/Stop (F3), Change Area (F1), Exit (F4) by default.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque

IS_WINDOWS = sys.platform == "win32"

# Must run before Tk is created so Tk, MSS and the stored boxes all agree on
# physical pixel coordinates (otherwise 125%/150% display scaling skews boxes).
if IS_WINDOWS:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

from . import __version__
from . import constants as C
from .api import start_api_server
from .bot import HydraBot
from .config import Config
from .discord import valid_webhook_url
from .overlay import Overlay
from .stages.rod_specials import (HalibutHarpoon, PinionsAria, StellarwaveMelody,
                                  special_for_rod)

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BG = "#1b1f2a"
HEADER = "#212635"
CARD = "#232838"
CARD_BORDER = "#30374a"
STEP = "#1e2230"
ROW_LINE = "#2b3244"
ROW_HOVER = "#2a3043"
INPUT = "#161a23"
INPUT_BORDER = "#363e52"
TEXT = "#dfe3ea"
MUTED = "#8b93a5"
DIM = "#5a6276"
ACCENT = "#5b8def"
ACCENT_HOVER = "#7aa3f5"
BTN = "#363d4f"
BTN_HOVER = "#444c61"
DANGER = "#ec6672"
DANGER_BG = "#3b2a33"
DANGER_HOVER = "#4b3240"
STATUS_COLORS = {"green": "#3fcf7f", "yellow": "#e7b844", "red": "#ef4b55"}

FAMILY = "Segoe UI" if IS_WINDOWS else "DejaVu Sans"
FAMILY_SEMI = "Segoe UI Semibold" if IS_WINDOWS else "DejaVu Sans"
MONO = "Consolas" if IS_WINDOWS else "DejaVu Sans Mono"


def font(size=10, weight="normal", family=None):
    return (family or FAMILY, size, weight)


LABEL_FONT = (FAMILY_SEMI, 10)

# ---------------------------------------------------------------------------
# Field tables
# ---------------------------------------------------------------------------
BOX_NAMES = ("fish_box", "shake_box", "totem_box", "exit_box")
BOX_HELP = {
    "fish_box": "The reel minigame bar",
    "shake_box": "Where shake buttons appear",
    "totem_box": "Sun/moon icon (day/night check)",
    "exit_box": "Bottom-left 'fish hooked' indicator",
}
STAGES = (
    ("Misc", "Loadout / auto-select rod"),
    ("Cast", "Cast the line"),
    ("Shake", "Click shake prompts"),
    ("Fish", "Play the reel minigame"),
)
HOTKEY_ACTIONS = ("Start/Stop", "Change Area", "Exit")
HOTBAR_FIELDS = [
    ("Fishing Rod", "hotbar_fishing_rod"),
    ("Equipment Bag", "hotbar_equipment_bag"),
    ("Mystic Mirror", "hotbar_mystic_mirror"),
    ("Special Totem", "hotbar_aurora_totem"),
    ("Sundial", "hotbar_sundial"),
    ("Potion", "hotbar_potion"),
]
GUI_TOGGLES = [
    ("Always On Top", "Always On Top"),
    ("Auto Minimize", "Auto Minimize GUI"),
    ("Auto Focus", "Auto Focus Roblox"),
    ("Auto Maximize", "Auto Maximize Roblox"),
    ("Perfect Cast Overlay", "Show Perfect Cast Overlay"),
    ("Fishing Overlay", "Show Fishing Overlay"),
    ("Status Overlay", "Show Status Overlay"),
]
ROD_COLOR_FIELDS = [
    ("Target Line", "fish_target_line_color", "fish_target_line_tolerance"),
    ("Arrow", "fish_arrow_color", "fish_arrow_tolerance"),
    ("Left Bar", "fish_left_bar_color", "fish_left_bar_tolerance"),
    ("Right Bar", "fish_right_bar_color", "fish_right_bar_tolerance"),
]
# Every key that can carry a "<key>_<Rod>" override (copied / removed with a rod).
ROD_KEYS = [k for _l, ck, tk_ in ROD_COLOR_FIELDS for k in (ck, tk_)] + [
    "fish_scan_fps", "fish_lost_timeout", "fish_bar_ratio_from_side",
    "fish_control_mode", "fish_kp", "fish_kd", "fish_pd_clamp",
]
TOTEM_STYLES = ["Disabled", "Day", "Night", "Aurora", "Rainbow", "Shiny", "Sparkling", "Mutation"]
# style -> [(label, key)] of the delays that style actually uses
TOTEM_TIMING = {
    "Day": [("Bag Delay", "totem_day_bag_delay"), ("Equip Delay", "totem_day_equip_delay")],
    "Night": [("Bag Delay", "totem_night_bag_delay"), ("Equip Delay", "totem_night_equip_delay")],
    "Aurora": [("Bag Delay", "totem_aurora_bag_delay"), ("Equip Delay", "totem_aurora_equip_delay")],
    "Rainbow": [("Bag Delay", "totem_rainbow_bag_delay"),
                ("Equip Delay", "totem_rainbow_equip_delay")],
}
for _style in ("Shiny", "Sparkling", "Mutation"):
    TOTEM_TIMING[_style] = [("Bag Delay", "totem_direct_bag_delay"),
                            ("Equip Delay", "totem_direct_equip_delay")]

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


def _coerce(text: str, like):
    """Parse ``text`` into the same type as ``like`` (raises ValueError)."""
    text = text.strip()
    if isinstance(like, bool):
        low = text.lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError("expected true/false")
    if isinstance(like, int):
        return int(float(text)) if "." in text else int(text)
    if isinstance(like, float):
        return float(text)
    if isinstance(like, (list, dict)):
        value = json.loads(text)
        if not isinstance(value, type(like)):
            raise ValueError(f"expected JSON {type(like).__name__}")
        return value
    return text


def _parse_num(text: str, kind: str):
    """``kind`` is "int" or "float"; ints must be whole numbers."""
    value = float(text.strip())
    if kind == "int":
        if not value.is_integer():
            raise ValueError("expected a whole number")
        return int(value)
    return value


def _parse_webhook(text: str) -> str:
    text = text.strip()
    if text and not valid_webhook_url(text):
        raise ValueError("not a Discord webhook URL")
    return text


def _parse_user_id(text: str) -> str:
    text = text.strip().lstrip("<@!").rstrip(">")
    if text and not text.isdigit():
        raise ValueError("expected a numeric Discord user ID")
    return text


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


class GuiLog:
    """stdout/stderr tee: keeps a ring buffer for /logs and a queue for the GUI.

    Windowed (no-console) exes have ``sys.stdout = None``, so the original
    stream is optional.
    """

    def __init__(self, stream, maxlen=500):
        self._stream = stream
        self._buf = deque(maxlen=maxlen)
        self.queue: "queue.Queue[str]" = queue.Queue()

    def write(self, text):
        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:
                pass
        for line in text.splitlines():
            if line.strip():
                self._buf.append(line)
                self.queue.put(line)

    def flush(self):
        if self._stream is not None:
            try:
                self._stream.flush()
            except Exception:
                pass

    def tail(self, n=200):
        return list(self._buf)[-n:]


# ---------------------------------------------------------------------------
# Global hotkeys (polled so they work while Roblox has focus)
# ---------------------------------------------------------------------------
def _vk_for(name: str):
    if not name:
        return None
    if len(name) >= 2 and name[0] in "Ff" and name[1:].isdigit():
        n = int(name[1:])
        return 0x6F + n if 1 <= n <= 24 else None
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    return None


class HotkeyWatcher(threading.Thread):
    def __init__(self, get_bindings, events: "queue.Queue[str]"):
        super().__init__(daemon=True)
        self._get_bindings = get_bindings
        self._events = events
        self._down: set[int] = set()

    def run(self):
        if not IS_WINDOWS:
            return
        get_state = ctypes.windll.user32.GetAsyncKeyState
        while True:
            for action, key in list(self._get_bindings().items()):
                vk = _vk_for(key)
                if vk is None:
                    continue
                pressed = bool(get_state(vk) & 0x8000)
                if pressed and vk not in self._down:
                    self._down.add(vk)
                    self._events.put(action)
                elif not pressed:
                    self._down.discard(vk)
            time.sleep(0.03)


# ---------------------------------------------------------------------------
# Screen overlays: region picker + colour eyedropper
# ---------------------------------------------------------------------------
def _virtual_screen():
    if IS_WINDOWS:
        m = ctypes.windll.user32.GetSystemMetrics
        return m(76), m(77), m(78), m(79)  # x, y, w, h across all monitors
    return None


class RegionPicker(tk.Toplevel):
    """Translucent full-screen overlay; drag a rectangle, Esc to cancel."""

    def __init__(self, master, title, current, on_done):
        super().__init__(master)
        self._on_done = on_done
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.35)
        vs = _virtual_screen()
        if vs:
            self.ox, self.oy, w, h = vs
        else:
            self.ox, self.oy = 0, 0
            w, h = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{self.ox}+{self.oy}")
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            w // 2, 60, fill="white",
            font=font(22, "bold"),
            text=f"Drag to select: {title}    (Esc = cancel)")
        if current:
            self.canvas.create_rectangle(
                current["x1"] - self.ox, current["y1"] - self.oy,
                current["x2"] - self.ox, current["y2"] - self.oy,
                outline="yellow", width=2, dash=(6, 4))
        self._start = None
        self._rect = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda e: self._finish(None))
        self.focus_force()
        self.grab_set()

    def _press(self, e):
        self._start = (e.x_root, e.y_root)
        if self._rect:
            self.canvas.delete(self._rect)
        self._rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#00ff66", width=3, fill="#00ff66", stipple="gray25")

    def _drag(self, e):
        if self._start and self._rect:
            sx, sy = self._start
            self.canvas.coords(self._rect, sx - self.ox, sy - self.oy,
                               e.x_root - self.ox, e.y_root - self.oy)

    def _release(self, e):
        if not self._start:
            return
        sx, sy = self._start
        x1, x2 = sorted((sx, e.x_root))
        y1, y2 = sorted((sy, e.y_root))
        if x2 - x1 < 3 or y2 - y1 < 3:
            return  # treat as a stray click
        self._finish({"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    def _finish(self, box):
        self.grab_release()
        self.destroy()
        self._on_done(box)


class PointPicker(tk.Toplevel):
    """Translucent full-screen overlay; click a point, Esc to cancel.

    Shades everything below the reel bar (where the hotbar is) as a no-go zone.
    """

    def __init__(self, master, title, current, fish_box, on_done):
        super().__init__(master)
        self._on_done = on_done
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.35)
        vs = _virtual_screen()
        if vs:
            self.ox, self.oy, w, h = vs
        else:
            self.ox, self.oy = 0, 0
            w, h = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{self.ox}+{self.oy}")
        c = self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="crosshair")
        c.pack(fill="both", expand=True)
        if fish_box:
            top = fish_box["y1"] - self.oy
            c.create_rectangle(0, top, w, h, fill="#ef4b55", outline="", stipple="gray50")
            c.create_text(w // 2, top + 20, fill="white", font=font(14, "bold"),
                          text="Reel bar / hotbar - don't park here")
        c.create_text(w // 2, 60, fill="white", font=font(22, "bold"),
                      text=f"Click to set: {title}    (Esc = cancel)")
        if current:
            x, y = current[0] - self.ox, current[1] - self.oy
            c.create_oval(x - 9, y - 9, x + 9, y + 9, outline="yellow", width=2)
            c.create_line(x - 16, y, x + 16, y, fill="yellow")
            c.create_line(x, y - 16, x, y + 16, fill="yellow")
        c.bind("<ButtonRelease-1>", lambda e: self._finish((e.x_root, e.y_root)))
        c.bind("<ButtonPress-3>", lambda e: self._finish(None))
        self.bind("<Escape>", lambda e: self._finish(None))
        self.focus_force()
        self.grab_set()

    def _finish(self, point):
        self.grab_release()
        self.destroy()
        self._on_done(point)


class ScreenColorPicker(tk.Toplevel):
    """Freezes the screen and shows a pixel magnifier with the hex under the
    cursor. Click to pick, Esc / right-click to cancel.

    Raises if the screen can't be captured (caller falls back to a dialog).
    """

    CELLS = 11  # odd, so there's a centre pixel
    CELL = 12

    def __init__(self, master, on_done):
        import mss
        import numpy as np

        with mss.mss() as sct:
            mon = sct.monitors[0]  # the whole virtual screen
            shot = np.asarray(sct.grab(mon))
        self.rgb = np.ascontiguousarray(shot[:, :, 2::-1])  # BGRA -> RGB
        self.h, self.w = self.rgb.shape[:2]
        ppm = f"P6\n{self.w} {self.h}\n255\n".encode() + self.rgb.tobytes()
        # build the image before the window so a failure leaves nothing on screen
        img = tk.PhotoImage(master=master, data=ppm, format="ppm")

        super().__init__(master)
        self._img = img
        self._on_done = on_done
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{self.w}x{self.h}+{mon['left']}+{mon['top']}")
        c = self.canvas = tk.Canvas(self, highlightthickness=0, cursor="crosshair",
                                    width=self.w, height=self.h)
        c.pack(fill="both", expand=True)
        c.create_image(0, 0, image=self._img, anchor="nw")

        tip = "Click to pick a colour   ·   Esc to cancel"
        c.create_rectangle(self.w // 2 - 190, 24, self.w // 2 + 190, 62,
                           fill=BG, outline=CARD_BORDER)
        c.create_text(self.w // 2, 43, text=tip, fill=TEXT, font=font(11, "bold"))

        # magnifier, built at the origin and moved with the cursor
        n, s = self.CELLS, self.CELL
        size = n * s
        self._cells = []
        for j in range(n):
            for i in range(n):
                self._cells.append(c.create_rectangle(
                    i * s, j * s, i * s + s, j * s + s, width=0, fill="#000", tags="mag"))
        mid = n // 2 * s
        c.create_line(mid + s / 2, 0, mid + s / 2, size, fill="#ffffff", stipple="gray50",
                      tags="mag")
        c.create_line(0, mid + s / 2, size, mid + s / 2, fill="#ffffff", stipple="gray50",
                      tags="mag")
        c.create_rectangle(mid, mid, mid + s, mid + s, outline="white", width=2, tags="mag")
        c.create_rectangle(0, 0, size, size, outline=ACCENT, width=2, tags="mag")
        c.create_rectangle(0, size + 6, 100, size + 30, fill=BG, outline=CARD_BORDER, tags="mag")
        self._swatch = c.create_rectangle(7, size + 12, 19, size + 24, outline=MUTED, tags="mag")
        self._hex = c.create_text(26, size + 18, anchor="w", fill=TEXT,
                                  font=font(10, "bold", MONO), tags="mag")
        self._mag_pos = (0, 0)

        c.bind("<Motion>", self._motion)
        c.bind("<ButtonPress-1>", self._click)
        c.bind("<ButtonPress-3>", lambda e: self._finish(None))
        self.bind("<Escape>", lambda e: self._finish(None))
        self.focus_force()
        self.grab_set()
        px, py = self.winfo_pointerxy()
        self._update(px - mon["left"], py - mon["top"])

    def _hex_at(self, x, y):
        x = min(max(x, 0), self.w - 1)
        y = min(max(y, 0), self.h - 1)
        r, g, b = (int(v) for v in self.rgb[y, x])
        return f"#{r:02x}{g:02x}{b:02x}"

    def _update(self, x, y):
        n, s = self.CELLS, self.CELL
        half = n // 2
        k = 0
        for j in range(n):
            for i in range(n):
                self.canvas.itemconfigure(self._cells[k], fill=self._hex_at(x + i - half, y + j - half))
                k += 1
        hexcode = self._hex_at(x, y)
        self.canvas.itemconfigure(self._swatch, fill=hexcode)
        self.canvas.itemconfigure(self._hex, text=hexcode)
        size = n * s
        mx = x + 24 if x + 24 + size < self.w else x - 24 - size
        my = y + 24 if y + 24 + size + 32 < self.h else y - 24 - size - 32
        ox, oy = self._mag_pos
        self.canvas.move("mag", mx - ox, my - oy)
        self._mag_pos = (mx, my)

    def _motion(self, e):
        self._update(e.x, e.y)

    def _click(self, e):
        self._finish(self._hex_at(e.x, e.y))

    def _finish(self, hexcode):
        self.grab_release()
        self.destroy()
        self._on_done(hexcode)


# ---------------------------------------------------------------------------
# Themed widgets
# ---------------------------------------------------------------------------
class FlatButton(tk.Label):
    KINDS = {
        "normal": (BTN, BTN_HOVER, TEXT),
        "accent": (ACCENT, ACCENT_HOVER, "#ffffff"),
        "danger": (DANGER_BG, DANGER_HOVER, DANGER),
    }

    def __init__(self, master, text, command, kind="normal", padx=10, pady=3, size=9):
        bg, _hover, fg = self.KINDS[kind]
        super().__init__(master, text=text, bg=bg, fg=fg, font=font(size, "bold"),
                         padx=padx, pady=pady, cursor="hand2")
        self._command = command
        self.set_kind(kind)
        self.bind("<Enter>", lambda e: self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg))
        self.bind("<ButtonRelease-1>", self._release)

    def set_kind(self, kind):
        self._bg, self._hover, fg = self.KINDS[kind]
        self.config(bg=self._bg, fg=fg)

    def _release(self, e):
        # only fire if released over the button (lets the user drag off to cancel)
        if 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            self._command()


class Toggle(tk.Canvas):
    W, H = 40, 22

    def __init__(self, master, value, command):
        super().__init__(master, width=self.W, height=self.H, bg=master.cget("bg"),
                         highlightthickness=0, cursor="hand2")
        self.value = bool(value)
        self._command = command
        self.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.W, self.H
        r = h / 2
        fill = ACCENT if self.value else "#3a4154"
        self.create_oval(1, 1, h - 1, h - 1, fill=fill, outline=fill)
        self.create_oval(w - h + 1, 1, w - 1, h - 1, fill=fill, outline=fill)
        self.create_rectangle(r, 1, w - r, h - 1, fill=fill, outline=fill)
        x = w - r if self.value else r
        knob = "#ffffff" if self.value else "#9aa2b4"
        self.create_oval(x - 7, r - 7, x + 7, r + 7, fill=knob, outline=knob)

    def _click(self, _e):
        self.value = not self.value
        self._draw()
        self._command(self.value)


class Slider(tk.Canvas):
    """Thin track + round knob. Calls ``command(value)`` while dragging."""

    def __init__(self, master, value, lo, hi, step, command, length=170):
        super().__init__(master, width=length + 16, height=22, bg=master.cget("bg"),
                         highlightthickness=0, cursor="hand2")
        self.lo, self.hi, self.step = lo, hi, step
        self.length = length
        self._command = command
        self._is_int = all(isinstance(v, int) for v in (lo, hi, step))
        try:
            self.value = min(max(float(value), lo), hi)
        except (TypeError, ValueError):
            self.value = lo
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self._draw()

    def _x(self):
        span = (self.hi - self.lo) or 1
        return 8 + (self.value - self.lo) / span * self.length

    def _draw(self):
        self.delete("all")
        y = 11
        self.create_line(8, y, 8 + self.length, y, fill="#3a4154", width=3, capstyle="round")
        x = self._x()
        self.create_line(8, y, x, y, fill="#40598f", width=3, capstyle="round")
        self.create_oval(x - 7, y - 7, x + 7, y + 7, fill=ACCENT, outline="#9cbcff")

    def _drag(self, e):
        frac = min(max((e.x - 8) / self.length, 0.0), 1.0)
        raw = self.lo + frac * (self.hi - self.lo)
        value = self.lo + round((raw - self.lo) / self.step) * self.step
        value = min(max(value, self.lo), self.hi)
        value = int(round(value)) if self._is_int else round(value, 4)
        if value != self.value:
            self.value = value
            self._draw()
            self._command(value)


class Card(tk.Frame):
    """Bordered section with an uppercase title and divided rows."""

    def __init__(self, master, title, icon=None):
        super().__init__(master, bg=CARD, highlightthickness=1, highlightbackground=CARD_BORDER)
        self.pack(fill="x", padx=20, pady=(0, 14))
        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x", padx=18, pady=(13, 10))
        if icon:
            tk.Label(head, text=icon, bg=CARD, fg=MUTED, font=font(11)).pack(
                side="left", padx=(0, 8))
        tk.Label(head, text=title.upper(), bg=CARD, fg=TEXT, font=font(10, "bold")).pack(
            side="left")
        tk.Frame(self, bg=ROW_LINE, height=1).pack(fill="x", padx=18)
        self.body = tk.Frame(self, bg=CARD)
        self.body.pack(fill="x", padx=8, pady=(4, 10))
        self._rows = 0

    def _separator(self):
        if self._rows:
            tk.Frame(self.body, bg=ROW_LINE, height=1).pack(fill="x", padx=10)
        self._rows += 1

    def row(self, label, sub=None, label_width=None):
        """Add a ``label ........ [controls]`` row; returns the right-hand frame."""
        self._separator()
        row = tk.Frame(self.body, bg=CARD)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=CARD)
        inner.pack(fill="x", padx=10, pady=8)
        tk.Label(inner, text=label, bg=CARD, fg=TEXT, font=LABEL_FONT,
                 width=label_width, anchor="w").pack(side="left")
        if sub:
            tk.Label(inner, text=sub, bg=CARD, fg=DIM, font=font(9)).pack(side="left", padx=10)
        right = tk.Frame(inner, bg=CARD)
        right.pack(side="right")
        _hoverable(row)
        return right

    def left_row(self, label, label_width=14):
        """Row whose controls flow left-to-right after the label."""
        self._separator()
        row = tk.Frame(self.body, bg=CARD)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=CARD)
        inner.pack(fill="x", padx=10, pady=7)
        tk.Label(inner, text=label, bg=CARD, fg=TEXT, font=LABEL_FONT,
                 width=label_width, anchor="w").pack(side="left")
        _hoverable(row)
        return inner

    def subheader(self, text):
        self._separator()
        tk.Label(self.body, text=text.upper(), bg=CARD, fg=TEXT,
                 font=font(10, "bold")).pack(pady=(10, 4))
        self._rows = 0  # no divider directly under the subheader

    def note(self, text):
        tk.Label(self.body, text=text, bg=CARD, fg=MUTED, font=font(9), justify="left",
                 anchor="w", wraplength=900).pack(fill="x", padx=10, pady=(6, 4))


def _recolor(widget, old, new):
    for child in widget.winfo_children():
        if isinstance(child, (tk.Frame, tk.Label, tk.Canvas)) and not isinstance(
                child, FlatButton):
            try:
                if child.cget("bg") == old:
                    child.config(bg=new)
            except tk.TclError:
                pass
        _recolor(child, old, new)


def _hoverable(row):
    """Subtle row highlight like the original (children share the row colour)."""
    def enter(_e):
        if row.cget("bg") == CARD:
            row.config(bg=ROW_HOVER)
            _recolor(row, CARD, ROW_HOVER)

    def leave(_e):
        # <Leave> also fires when moving onto a child; only reset when truly outside
        def check():
            if not row.winfo_exists():
                return
            px, py = row.winfo_pointerxy()
            x, y = row.winfo_rootx(), row.winfo_rooty()
            if not (x <= px < x + row.winfo_width() and y <= py < y + row.winfo_height()):
                row.config(bg=CARD)
                _recolor(row, ROW_HOVER, CARD)
        row.after(10, check)

    row.bind("<Enter>", enter, add="+")
    row.bind("<Leave>", leave, add="+")


class ScrollPage(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                style="Dark.Vertical.TScrollbar")
        self.body = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._win, width=e.width))
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.clear()

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()
        tk.Frame(self.body, bg=BG, height=18).pack()

    def scroll(self, units):
        if self.body.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(units, "units")

    def scroll_to(self, widget):
        self.update_idletasks()
        total = max(1, self.body.winfo_height())
        self.canvas.yview_moveto(max(0, widget.winfo_y() - 10) / total)


def _setup_styles(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        "Dark.TCombobox", fieldbackground=INPUT, background=INPUT, foreground=TEXT,
        arrowcolor=TEXT, bordercolor=INPUT_BORDER, lightcolor=INPUT, darkcolor=INPUT,
        selectbackground=INPUT, selectforeground=TEXT, padding=(8, 4), arrowsize=12)
    style.map(
        "Dark.TCombobox",
        fieldbackground=[("readonly", INPUT)], foreground=[("readonly", TEXT)],
        selectbackground=[("readonly", INPUT)], selectforeground=[("readonly", TEXT)],
        background=[("active", BTN), ("readonly", INPUT)],
        bordercolor=[("focus", ACCENT), ("active", ACCENT)],
        arrowcolor=[("disabled", DIM)])
    root.option_add("*TCombobox*Listbox.background", INPUT)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", font(10))
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    style.configure(
        "Dark.Vertical.TScrollbar", background=BTN, troughcolor=BG, bordercolor=BG,
        arrowcolor=MUTED, lightcolor=BTN, darkcolor=BTN, gripcount=0)
    style.map("Dark.Vertical.TScrollbar", background=[("active", BTN_HOVER)])

    style.configure(
        "Dark.Treeview", background=INPUT, fieldbackground=INPUT, foreground=TEXT,
        bordercolor=CARD_BORDER, lightcolor=INPUT, darkcolor=INPUT, rowheight=24,
        font=font(9))
    style.map("Dark.Treeview", background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])
    style.configure(
        "Dark.Treeview.Heading", background=CARD, foreground=MUTED, bordercolor=CARD_BORDER,
        lightcolor=CARD, darkcolor=CARD, relief="flat", font=font(9, "bold"))
    style.map("Dark.Treeview.Heading", background=[("active", ROW_HOVER)])

    # Stop the mouse wheel from silently changing dropdown values while scrolling.
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.unbind_class("TCombobox", seq)


def _dark_titlebar(root):
    if not IS_WINDOWS:
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / pre-20H1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def _ask_string(root, title, prompt, initial=""):
    """Small dark modal text prompt; returns the text or None."""
    win = tk.Toplevel(root, bg=CARD, padx=20, pady=16)
    win.title(title)
    win.transient(root)
    win.resizable(False, False)
    tk.Label(win, text=prompt, bg=CARD, fg=TEXT, font=LABEL_FONT).pack(anchor="w")
    var = tk.StringVar(value=initial)
    entry = tk.Entry(win, textvariable=var, width=32, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                     relief="flat", font=font(10), highlightthickness=1,
                     highlightbackground=INPUT_BORDER, highlightcolor=ACCENT)
    entry.pack(fill="x", pady=(8, 14), ipady=4)
    result = {"value": None}

    def ok(_e=None):
        result["value"] = var.get().strip() or None
        win.destroy()

    buttons = tk.Frame(win, bg=CARD)
    buttons.pack(fill="x")
    FlatButton(buttons, "OK", ok, kind="accent", padx=16).pack(side="right")
    FlatButton(buttons, "Cancel", win.destroy, padx=12).pack(side="right", padx=8)
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda e: win.destroy())
    _dark_titlebar(win)
    entry.focus_set()
    win.grab_set()
    root.wait_window(win)
    return result["value"]


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
TABS = [
    ("Main", "⌂"), ("Cast", "➚"), ("Shake", "◯"), ("Fish", "≫"),
    ("Totem", "✦"), ("Reconnect", "⟳"), ("Discord", "✉"), ("Extra", "⚙"),
]
DIM_TABS = ("Reconnect",)  # not run by the core rewrite


class HydraGUI:
    def __init__(self, root: tk.Tk, bot: HydraBot, log: GuiLog):
        self.root = root
        self.bot = bot
        self.cfg = bot.cfg
        self.log = log
        self._defaults = Config._load_defaults()["settings"]
        self._save_job = None
        self._hotkey_events: "queue.Queue[str]" = queue.Queue()
        self._rebind = None
        self._log_lines: deque = deque(maxlen=1000)
        self.log_text = None
        self._box_labels: dict = {}
        self._park_label = None
        self._areas_card = None
        self.current = None

        root.title("Hydra")
        root.geometry("1120x780")
        root.minsize(860, 560)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        _setup_styles(root)

        self._build_header()
        self._build_tabbar()
        holder = tk.Frame(root, bg=BG)
        holder.pack(fill="both", expand=True)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.builders = {
            "Main": self._page_main, "Cast": self._page_cast, "Shake": self._page_shake,
            "Fish": self._page_fish, "Totem": self._page_totem,
            "Reconnect": self._page_reconnect, "Discord": self._page_discord,
            "Extra": self._page_extra,
        }
        self.pages = {}
        for name, _icon in TABS:
            page = ScrollPage(holder)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = page
            self.builders[name](page.body)
        self.show_page("Main")

        root.bind_all("<MouseWheel>", self._wheel)
        root.bind_all("<Button-4>", self._wheel)
        root.bind_all("<Button-5>", self._wheel)

        _dark_titlebar(root)
        self._apply_window_prefs()
        HotkeyWatcher(lambda: self.cfg.hotkeys, self._hotkey_events).start()
        # on-screen indicators over Roblox (Windows: click-through + hidden from capture)
        self.overlay = None
        if IS_WINDOWS or os.environ.get("HYDRA_OVERLAY_FORCE"):
            self.overlay = Overlay(root, bot, lambda: self.cfg.global_gui_settings)
        self._tick()

    # -- persistence -----------------------------------------------------
    def set_setting(self, key, value):
        self.cfg.set(key, value)
        self.schedule_save()

    def schedule_save(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(400, self._save_now)

    def _save_now(self):
        self._save_job = None
        try:
            self.cfg.save()
            self.saved_label.config(text="✓ Saved")
            self.root.after(1800, lambda: self.saved_label.config(text=""))
        except OSError as e:
            messagebox.showerror("Save failed", f"Could not write {self.cfg.path}:\n{e}")

    def refresh(self, *names):
        for name in names or self.pages:
            page = self.pages[name]
            page.clear()
            self.builders[name](page.body)

    # -- header / tabs -----------------------------------------------------
    def _build_header(self):
        hdr = tk.Frame(self.root, bg=HEADER)
        hdr.pack(fill="x")
        tk.Label(hdr, text="♆", bg=HEADER, fg=TEXT, font=font(20)).pack(
            side="left", padx=(20, 8), pady=8)
        tk.Label(hdr, text="Hydra", bg=HEADER, fg=TEXT, font=font(15, "bold")).pack(side="left")
        tk.Frame(hdr, bg=DIM, width=1, height=22).pack(side="left", padx=10)
        tk.Label(hdr, text="The Ultimate Fishing Macro", bg=HEADER, fg=MUTED,
                 font=font(11)).pack(side="left")

        # right side (packed right-to-left)
        pill = tk.Frame(hdr, bg="#2a3042", highlightthickness=1,
                        highlightbackground=CARD_BORDER, cursor="hand2")
        pill.pack(side="right", padx=(8, 20))
        self.status_dot = tk.Canvas(pill, width=10, height=10, bg="#2a3042",
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(12, 6), pady=8)
        self._dot = self.status_dot.create_oval(1, 1, 9, 9, fill=STATUS_COLORS["red"], width=0)
        self.status_label = tk.Label(pill, text="Idle", bg="#2a3042", fg=TEXT,
                                     font=font(10, "bold"))
        self.status_label.pack(side="left", padx=(0, 12))
        for w in (pill, self.status_dot, self.status_label):
            w.bind("<Button-1>", lambda e: self.toggle())

        self.start_btn = FlatButton(hdr, "▶  Start", self.toggle, kind="accent", padx=14, pady=5,
                                    size=10)
        self.start_btn.pack(side="right", padx=(8, 0))
        tk.Label(hdr, text=f"V{__version__}", bg="#3a4152", fg=TEXT, font=font(9, "bold"),
                 padx=8, pady=3).pack(side="right", padx=(8, 4))
        self.count_label = tk.Label(hdr, text="", bg=HEADER, fg=MUTED, font=font(9))
        self.count_label.pack(side="right", padx=8)
        self.saved_label = tk.Label(hdr, text="", bg=HEADER, fg=STATUS_COLORS["green"],
                                    font=font(9, "bold"))
        self.saved_label.pack(side="right", padx=4)
        self.sub_label = tk.Label(hdr, text="", bg=HEADER, fg=MUTED, font=font(9))
        self.sub_label.pack(side="right", padx=4)
        tk.Frame(self.root, bg=ACCENT, height=1).pack(fill="x")

    def _build_tabbar(self):
        bar = tk.Frame(self.root, bg=HEADER)
        bar.pack(fill="x")
        self._tab_widgets = {}
        for i, (name, icon) in enumerate(TABS):
            bar.columnconfigure(i, weight=1, uniform="tabs")
            cell = tk.Frame(bar, bg=HEADER)
            cell.grid(row=0, column=i, sticky="nsew")
            lbl = tk.Label(cell, text=f"{icon}  {name}", bg=HEADER, font=font(10),
                           pady=10, cursor="hand2")
            lbl.pack(fill="x")
            line = tk.Frame(cell, bg=HEADER, height=2)
            line.pack(fill="x")
            for w in (cell, lbl):
                w.bind("<Button-1>", lambda e, n=name: self.show_page(n))
                w.bind("<Enter>", lambda e, n=name: self._tab_hover(n, True))
                w.bind("<Leave>", lambda e, n=name: self._tab_hover(n, False))
            self._tab_widgets[name] = (lbl, line)
        tk.Frame(self.root, bg=CARD_BORDER, height=1).pack(fill="x")

    def _tab_colors(self, name, hover=False):
        if name == self.current:
            return TEXT
        if hover:
            return TEXT if name not in DIM_TABS else MUTED
        return DIM if name in DIM_TABS else MUTED

    def _tab_hover(self, name, hover):
        lbl, _line = self._tab_widgets[name]
        lbl.config(fg=self._tab_colors(name, hover))

    def show_page(self, name):
        self.current = name
        self.pages[name].tkraise()
        for n, (lbl, line) in self._tab_widgets.items():
            active = n == name
            lbl.config(fg=self._tab_colors(n), font=font(10, "bold" if active else "normal"))
            line.config(bg=ACCENT if active else HEADER)
        if name == "Extra":
            self._fill_all()

    def _wheel(self, e):
        w = e.widget
        if isinstance(w, (tk.Text, ttk.Treeview, tk.Listbox)) or not isinstance(w, tk.Misc):
            return  # these scroll themselves
        try:
            if w.winfo_toplevel() is not self.root:
                return
        except (tk.TclError, AttributeError):
            return
        if getattr(e, "num", None) == 4:
            units = -3
        elif getattr(e, "num", None) == 5:
            units = 3
        else:
            units = int(-e.delta / 40) or (-1 if e.delta > 0 else 1)
        self.pages[self.current].scroll(units)

    # -- generic controls --------------------------------------------------
    def _combo(self, parent, value, options, on_select, width=16):
        values = list(options)
        if value is not None and str(value) not in values:
            values.append(str(value))
        var = tk.StringVar(value="" if value is None else str(value))
        box = ttk.Combobox(parent, textvariable=var, values=values, state="readonly",
                           width=width, style="Dark.TCombobox", font=font(10, "bold"),
                           height=min(max(len(values), 1), 20))
        box.bind("<<ComboboxSelected>>", lambda e: (box.selection_clear(), on_select(var.get())))
        box.pack(side="right")
        return box

    def _entry(self, parent, value, on_commit, parse=None, width=9, justify="center", **kw):
        """Dark entry; ``parse(text)`` validates (raise ValueError -> red border)."""
        var = tk.StringVar(value="" if value is None else _fmt(value))
        e = tk.Entry(parent, textvariable=var, width=width, justify=justify, bg=INPUT, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=font(9, "bold"),
                     highlightthickness=1, highlightbackground=INPUT_BORDER,
                     highlightcolor=ACCENT, **kw)
        state = {"last": var.get()}

        def commit(_e=None):
            text = var.get()
            try:
                value = parse(text) if parse else text
            except ValueError:
                e.config(highlightbackground=DANGER, highlightcolor=DANGER)
                return
            e.config(highlightbackground=INPUT_BORDER, highlightcolor=ACCENT)
            if text != state["last"]:
                state["last"] = text
                on_commit(value)

        e.bind("<Return>", commit)
        e.bind("<FocusOut>", commit)
        var.trace_add("write", lambda *a: e.after_idle(commit))
        e.var = var
        return e

    def _num(self, card, label, key, kind="float", rod=False, sub=None):
        getter, setter = self._accessors(key, rod)
        right = card.row(label, sub)
        self._entry(right, getter(), setter, parse=lambda t: _parse_num(t, kind)).pack(
            side="right", ipady=3)

    def _toggle(self, card, label, key, sub=None, on_change=None):
        right = card.row(label, sub)

        def set_(v):
            self.set_setting(key, v)
            if on_change:
                on_change(v)
        Toggle(right, self.cfg.get(key, False), set_).pack(side="right")

    def _choice(self, card, label, key, options, rod=False, on_change=None, sub=None, width=16):
        getter, setter = self._accessors(key, rod)
        right = card.row(label, sub)

        def select(v):
            setter(v)
            if on_change:
                on_change(v)
        self._combo(right, getter(), options, select, width=width)

    def _slider(self, card, label, key, lo, hi, step, fmt=_fmt, rod=False, kind="float"):
        getter, setter = self._accessors(key, rod)
        right = card.row(label)
        value_lbl = tk.Label(right, bg=CARD, fg=MUTED, font=font(9), width=11, anchor="e")
        value_lbl.pack(side="right")

        def changed(v):
            value_lbl.config(text=fmt(v))
            setter(int(v) if kind == "int" else float(v))
        slider = Slider(right, getter(), lo, hi, step, changed)
        slider.pack(side="right", padx=(0, 6))
        value_lbl.config(text=fmt(getter() if getter() is not None else slider.value))

    def _accessors(self, key, rod=False):
        if rod:
            r = self.cfg.current_rod()
            return (lambda: self.cfg.rod_setting(key)), (
                lambda v: self.set_setting(f"{key}_{r}", v))
        return (lambda: self.cfg.get(key)), (lambda v: self.set_setting(key, v))

    # -- Main ----------------------------------------------------------------
    def _page_main(self, body):
        c = Card(body, "Display Capture", "▭")
        self._choice(c, "Capture Mode", "shake_capture_mode", ["MSS", "DXCAM"])

        c = Card(body, "Hotkey Configuration", "⌨")
        for action in HOTKEY_ACTIONS:
            self._bind_row(c, action, self.cfg.hotkeys.get(action, ""),
                           lambda v, a=action: self._set_hotkey(a, v), hotkey=True)
        for label, key in HOTBAR_FIELDS:
            self._bind_row(c, label, str(self.cfg.get(key, "")),
                           lambda v, k=key: self.set_setting(k, v), hotkey=False)

        c = Card(body, "GUI Settings", "⊞")
        for label, name in GUI_TOGGLES:
            right = c.row(label)
            Toggle(right, self.cfg.global_gui_settings.get(name, False),
                   lambda v, n=name: self._set_gui_pref(n, v)).pack(side="right")

        c = self._areas_card = Card(body, "Detection Areas", "⛶")
        self._box_labels = {}
        for name in BOX_NAMES:
            right = c.row(name.replace("_", " ").title(), BOX_HELP[name])
            FlatButton(right, "Select", lambda n=name: self.pick_box(n)).pack(side="right")
            lbl = tk.Label(right, bg=CARD, fg=MUTED, font=font(9, family=MONO))
            lbl.pack(side="right", padx=12)
            self._box_labels[name] = lbl
        self._refresh_box_labels()

        c = Card(body, "Cursor Park", "⌖")
        self._choice(c, "Park Point", "cursor_park_mode", ["Auto", "Custom"],
                     sub="where the cursor rests between cycles (casts click here)",
                     on_change=lambda v: self._refresh_park_label())
        right = c.row("Position")
        if IS_WINDOWS:
            FlatButton(right, "Show", self._show_park_point).pack(side="right", padx=(8, 0))
        FlatButton(right, "Pick", self.pick_park_point).pack(side="right")
        self._park_label = tk.Label(right, bg=CARD, fg=MUTED, font=font(9, family=MONO))
        self._park_label.pack(side="right", padx=12)
        self._refresh_park_label()
        c.note("Auto keeps the cursor centred over the shake area and above the reel bar, "
               "so it never rests on the hotbar. The bot moves it back here before every "
               "cast, after each fish and after camera moves. Use Custom if your game "
               "view needs a different spot.")

        c = Card(body, "Stages", "☰")
        states = self.cfg.bottom_button_states
        for name, sub in STAGES:
            right = c.row(name, sub)

            def toggle(v, n=name):
                self.cfg.bottom_button_states[n] = v
                self.schedule_save()
            Toggle(right, states.get(name, True), toggle).pack(side="right")

        c = Card(body, "Loadout", "⚲")
        self._toggle(c, "Auto Select Rod", "auto_select_rod_enabled",
                     sub="Misc stage: equipment bag, then rod")
        self._num(c, "Delay Before", "auto_select_rod_delay1")
        self._num(c, "Delay Between", "auto_select_rod_delay2")
        self._num(c, "Delay After", "auto_select_rod_delay3")

    def _bind_row(self, card, label, current, on_set, hotkey):
        right = card.row(label)
        btn = FlatButton(right, "Rebind", lambda: None)
        btn.pack(side="right")
        badge = tk.Label(right, text=current.upper(), bg=INPUT, fg=TEXT, font=font(9, "bold"),
                         width=4, pady=3, highlightthickness=1, highlightbackground=INPUT_BORDER)
        badge.pack(side="right", padx=(0, 8))
        btn._command = lambda: self._start_rebind(badge, btn, on_set, hotkey)

    def _start_rebind(self, badge, btn, on_set, hotkey):
        self._cancel_rebind()
        self._rebind = (badge, btn, on_set, hotkey, badge.cget("text"))
        badge.config(text="…", highlightbackground=ACCENT)
        btn.config(text="Press a key")
        btn.set_kind("accent")
        self.root.focus_set()
        self.root.bind_all("<KeyPress>", self._on_rebind_key)

    def _cancel_rebind(self, new_text=None):
        if not self._rebind:
            return
        badge, btn, _on_set, _hotkey, old = self._rebind
        self._rebind = None
        self.root.unbind_all("<KeyPress>")
        if badge.winfo_exists():
            badge.config(text=new_text or old, highlightbackground=INPUT_BORDER)
            btn.config(text="Rebind")
            btn.set_kind("normal")

    def _on_rebind_key(self, e):
        badge, _btn, on_set, hotkey, _old = self._rebind
        if e.keysym == "Escape":
            self._cancel_rebind()
            return "break"
        name = None
        if hotkey and re.fullmatch(r"F\d{1,2}", e.keysym):
            name = e.keysym
        elif e.char and len(e.char) == 1 and e.char.isalnum():
            name = e.char.lower()
        if not name or (hotkey and name not in C.AVAILABLE_HOTKEYS):
            badge.config(highlightbackground=DANGER)  # unsupported key; keep waiting
            return "break"
        on_set(name)
        self._cancel_rebind(new_text=name.upper())
        return "break"

    def _set_hotkey(self, action, key):
        self.cfg.hotkeys[action] = key
        self.schedule_save()
        self._refresh_start_btn(force=True)

    def _set_gui_pref(self, name, value):
        self.cfg.global_gui_settings[name] = value
        self.schedule_save()
        self._apply_window_prefs()

    def _apply_window_prefs(self):
        self.root.attributes(
            "-topmost", bool(self.cfg.global_gui_settings.get("Always On Top", False)))

    def _refresh_box_labels(self):
        for name, lbl in self._box_labels.items():
            if not lbl.winfo_exists():
                continue
            b = self.cfg.box(name)
            lbl.config(text=(f"({b['x1']}, {b['y1']}) → ({b['x2']}, {b['y2']})"
                             if b else "not set"))

    def _refresh_park_label(self):
        if not (self._park_label and self._park_label.winfo_exists()):
            return
        x, y = self.bot.park_point()
        custom = self.cfg.get("cursor_park_mode", "Auto") == "Custom"
        unset = custom and not (self.cfg.get("cursor_park_x", 0) or
                                self.cfg.get("cursor_park_y", 0))
        self._park_label.config(text=f"({x}, {y})" + ("  · not picked, using auto"
                                                      if unset else ""))

    def pick_park_point(self):
        if self.bot.is_running:
            self.bot.stop()
        self.root.withdraw()

        def done(point):
            self.root.deiconify()
            if point:
                self.cfg.set("cursor_park_x", int(point[0]))
                self.cfg.set("cursor_park_y", int(point[1]))
                self.cfg.set("cursor_park_mode", "Custom")
                self.schedule_save()
                fish = self.cfg.fish_box
                if fish and point[1] >= fish["y1"]:
                    messagebox.showwarning(
                        "Cursor Park", "That point is level with or below the reel bar, "
                        "close to the hotbar. Casts click wherever the cursor rests, so this "
                        "can select hotbar slots.", parent=self.root)
                self.refresh("Main")
        self.root.after(250, lambda: PointPicker(
            self.root, "cursor park point", self.bot.park_point(), self.cfg.fish_box, done))

    def _show_park_point(self):
        try:
            self.bot.input.set_cursor(*self.bot.park_point())
        except Exception as e:
            print(f" Couldn't move the cursor: {e}")

    def pick_box(self, name):
        if self.bot.is_running:
            self.bot.stop()
        self.root.withdraw()

        def done(box):
            self.root.deiconify()
            if box:
                self.cfg.document[name] = box
                self.schedule_save()
                self._refresh_box_labels()
                self._refresh_park_label()  # auto park follows the shake/fish areas
        # Give the window time to disappear so Roblox is visible underneath.
        self.root.after(250, lambda: RegionPicker(
            self.root, name.replace("_", " "), self.cfg.box(name), done))

    # -- Cast ------------------------------------------------------------------
    def _page_cast(self, body):
        c = Card(body, "Cast Configuration", "◎")
        self._choice(c, "Cast Style", "cast_mode", ["Disabled", "Normal", "Perfect"],
                     on_change=lambda v: self.refresh("Cast"))
        mode = self.cfg.get("cast_mode", "Normal")

        if mode == "Normal":
            seq = Card(body, "Normal Cast Sequence")
            self._steps(seq.body, [
                ("⧗", "Delay", "cast_delay1"),
                ("●", "Hold Left Click", None),
                ("⧗", "Delay", "cast_delay2"),
                ("○", "Release Left Click", None),
                ("⧗", "Delay", "cast_delay3"),
            ])
        elif mode == "Perfect":
            seq = Card(body, "Perfect Cast Sequence")
            self._steps(seq.body, [
                ("⧗", "Delay", "perfect_delay1"),
                ("⊖", "Zoom Out (scroll ticks)", "zoom_out", "int"),
                ("⧗", "Delay", "perfect_delay2"),
                ("⊕", "Zoom In (scroll ticks)", "zoom_in", "int"),
                ("⧗", "Delay", "perfect_delay3"),
                ("↓", "Look Down (px)", "look_down", "int"),
                ("⧗", "Delay", "perfect_delay4"),
                ("↑", "Look Up (px)", "look_up", "int"),
                ("⧗", "Delay", "perfect_delay5"),
                ("⇄", "Equipment Bag → Fishing Rod", None),
                ("●", "Hold Left Click", None),
                ("⧗", "Delay", "perfect_delay6"),
                ("◎", "Release on Perfect (auto)", None),
                ("⧗", "Delay", "perfect_delay8"),
                ("⊕", "Final Zoom In (scroll ticks)", "final_zoom_in", "int"),
                ("⧗", "Delay", "perfect_delay9"),
                ("↑", "Final Look Up (px)", "final_look_up", "int"),
                ("⧗", "Delay", "perfect_delay10"),
            ])
            c = Card(body, "Perfect Cast Release", "✧")
            self._choice(c, "Release Style", "perfect_cast_release_style", ["Idiotproof"])
            self._slider(c, "Green Tolerance", "perfect_cast_green_color_tolerance",
                         0, 100, 1, kind="int")
            self._slider(c, "White Tolerance", "perfect_cast_white_color_tolerance",
                         0, 100, 1, kind="int")
            self._slider(c, "Scan FPS", "perfect_cast_scan_fps", 10, 1000, 10, kind="int",
                         fmt=lambda v: f"{v} ({1000 / max(v, 1):.1f}ms)")
            self._num(c, "Fail Scan Timeout", "perfect_cast_fail_scan_timeout")
            self._num(c, "Release Timing (ms)", "perfect_cast_release_timing")
            self._num(c, "Minimum Distance (px)", "perfect_cast_minimum_distance", "int")
        else:
            c.note("Casting is disabled — each cycle goes straight to the Shake stage.")

    def _steps(self, parent, steps):
        for i, step in enumerate(steps):
            icon, text, key = step[:3]
            kind = step[3] if len(step) > 3 else "float"
            if i:
                conn = tk.Frame(parent, bg=CARD, height=9)
                conn.pack(fill="x")
                tk.Frame(conn, bg=CARD_BORDER, width=1).place(relx=0.5, rely=0, relheight=1)
            box = tk.Frame(parent, bg=STEP, highlightthickness=1, highlightbackground=CARD_BORDER)
            box.pack(fill="x", padx=10)
            tk.Label(box, text=icon, bg="#3a4154", fg=TEXT, font=font(11), width=2).pack(
                side="left", padx=14, pady=9)
            tk.Label(box, text=text, bg=STEP, fg=TEXT, font=LABEL_FONT).place(
                relx=0.5, rely=0.5, anchor="center")
            if key:
                self._entry(box, self.cfg.get(key, 0), lambda v, k=key: self.set_setting(k, v),
                            parse=lambda t, kd=kind: _parse_num(t, kd)).pack(
                    side="right", padx=14, ipady=3)

    # -- Shake -----------------------------------------------------------------
    def _page_shake(self, body):
        c = Card(body, "Shake Configuration", "◫")
        self._choice(c, "Shake Style", "shake_style", ["Disabled", "Pixel"],
                     on_change=lambda v: self.refresh("Shake"))
        if self.cfg.get("shake_style") != "Pixel":
            c.note("Shake is disabled — after casting the bot waits for the reel "
                   "minigame to appear in the fish area.")
            return
        c = Card(body, "Pixel Shake Settings", "⌖")
        self._num(c, "Click Count", "shake_click_count", "int")
        self._slider(c, "Color Tolerance", "shake_color_tolerance", 0, 100, 1, kind="int")
        self._num(c, "Pixel Distance Tolerance", "shake_pixel_distance_tolerance", "int")
        self._slider(c, "Scan FPS", "shake_scan_fps", 10, 1000, 10, kind="int",
                     fmt=lambda v: f"{v} ({1000 / max(v, 1):.1f}ms)")
        self._num(c, "Duplicate Timeout (s)", "shake_duplicate_timeout")
        self._num(c, "Fail Cast Timeout (s)", "shake_fail_cast_timeout")

    # -- Fish ------------------------------------------------------------------
    def _page_fish(self, body):
        style = self.cfg.get("fish_track_style", "Color")
        c = Card(body, "Fish Configuration", "≫")
        self._choice(c, "Fish Track Style", "fish_track_style",
                     ["Disabled", "Color", "Line", "YOLO"],
                     on_change=lambda v: self.refresh("Fish"))
        if style == "YOLO":
            self._choice(c, "YOLO Model", "fish_yolo_rod", self._yolo_models(), width=20)
        rods = self._rod_list()
        self._choice(c, "Rod Type", "fish_rod_type", rods, width=20,
                     on_change=lambda v: self.refresh("Fish"))
        c._separator()
        actions = tk.Frame(c.body, bg=CARD)
        actions.pack(fill="x", padx=10, pady=8)
        FlatButton(actions, "Add Rod", self._add_rod).pack(side="left")
        FlatButton(actions, "Delete Rod", self._delete_rod, kind="danger").pack(side="right")

        if style == "Disabled":
            c.note("Fishing is disabled — the reel minigame won't be played.")
            return

        rod = self.cfg.current_rod()
        self._rod_special_card(body, rod)
        c = Card(body, "Color Options")
        for label, ckey, tkey in ROD_COLOR_FIELDS:
            self._color_row(c, label, ckey, tkey, rod)

        if style == "Color":
            c.subheader("Fish Settings")
            fps_fmt = lambda v: f"{v} ({1000 / max(v, 1):.1f}ms)"  # noqa: E731
            self._slider(c, "Scan FPS", "fish_scan_fps", 10, 1000, 10, fmt=fps_fmt,
                         rod=True, kind="int")
            self._slider(c, "Fish Lost Timeout", "fish_lost_timeout", 0, 10, 0.5,
                         fmt=lambda v: f"{v:g}s", rod=True)
            self._slider(c, "Bar Ratio From Side", "fish_bar_ratio_from_side", 0, 1, 0.05,
                         rod=True)
            self._choice(c, "Control Mode", "fish_control_mode", ["Steady", "Normal"], rod=True,
                         on_change=lambda v: self.refresh("Fish"))
            if self.cfg.rod_setting("fish_control_mode", "Steady") == "Steady":
                self._steady_rows(c)
            else:
                self._num(c, "KP", "fish_kp", rod=True)
                self._num(c, "KD", "fish_kd", rod=True)
                self._num(c, "PD Clamp", "fish_pd_clamp", rod=True)
            self._toggle(c, "Bag Spam", "fish_bag_spam_enabled")
            c.note("Bar Ratio and Bag Spam are saved for the original app; the core "
                   "rewrite's reel loop doesn't use them.")
        elif style == "Line":
            c.subheader("Line Settings")
            self._slider(c, "Scan FPS", "fish_line_scan_fps", 10, 1000, 10, kind="int",
                         fmt=lambda v: f"{v} ({1000 / max(v, 1):.1f}ms)")
            self._slider(c, "Fish Lost Timeout", "fish_line_lost_timeout", 0, 10, 0.5,
                         fmt=lambda v: f"{v:g}s")
            self._slider(c, "Bar Ratio", "fish_line_bar_ratio", 0, 1, 0.05)
            self._slider(c, "Min Density", "fish_line_min_density", 0, 1, 0.05)
            self._choice(c, "Control Mode", "fish_line_control_mode", ["Steady", "Normal"],
                         on_change=lambda v: self.refresh("Fish"))
            if self.cfg.get("fish_line_control_mode", "Steady") == "Steady":
                self._steady_rows(c)
            else:
                self._num(c, "KP", "fish_line_kp")
                self._num(c, "KD", "fish_line_kd")
                self._num(c, "PD Clamp", "fish_line_pd_clamp")
            c.note("Bar Ratio and Min Density are saved for the original app; the core "
                   "rewrite's reel loop doesn't use them.")
        elif style == "YOLO":
            c.subheader("YOLO Settings")
            self._slider(c, "Confidence", "fish_yolo_confidence", 0, 1, 0.05)
            self._slider(c, "Scan FPS", "fish_scan_fps", 10, 1000, 10, rod=True, kind="int",
                         fmt=lambda v: f"{v} ({1000 / max(v, 1):.1f}ms)")
            self._slider(c, "Fish Lost Timeout", "fish_lost_timeout", 0, 10, 0.5,
                         fmt=lambda v: f"{v:g}s", rod=True)
            self._num(c, "KP", "fish_yolo_kp")
            self._num(c, "KD", "fish_yolo_kd")
            self._num(c, "PD Clamp", "fish_pd_clamp")

    def _steady_rows(self, c):
        """Settings for Steady (quick-tap) control; see fish.SteadyTapper."""
        right = c.row("Tap Rate", "quick clicks per second while holding the bar steady")
        lbl = tk.Label(right, bg=CARD, fg=MUTED, font=font(9), width=11, anchor="e")
        lbl.pack(side="right")
        rate = round(1.0 / max(0.01, float(self.cfg.get("fish_steady_tap_period", 0.033))))

        def set_rate(v):
            lbl.config(text=f"{v}/s")
            self.set_setting("fish_steady_tap_period", round(1.0 / v, 4))
        Slider(right, rate, 10, 60, 1, set_rate).pack(side="right", padx=(0, 6))
        lbl.config(text=f"{rate}/s")
        self._slider(c, "Start Balance", "fish_steady_hover", 0.15, 0.85, 0.01,
                     fmt=lambda v: f"{v:.0%} hold")
        self._num(c, "Approach (1/s)", "fish_steady_approach",
                  sub="higher = moves to the fish faster, more overshoot")
        self._num(c, "Brake Gain", "fish_steady_gain")
        c.note("Steady taps the mouse quickly so the bar sits still on the fish, like "
               "a player feathering the click; far from the fish it still uses a full hold "
               "or release. The balance point is learned per rod while fishing. "
               "Normal = plain hold/release with KP/KD.")

    def _color_row(self, card, label, ckey, tkey, rod=None):
        """Swatch (eyedropper) + hex + None/Default + tolerance; per-rod unless rod=None."""
        row = card.left_row(label)
        cget, cset = self._accessors(ckey, rod is not None)
        tget, tset = self._accessors(tkey, rod is not None)

        def btn(text, cmd):
            FlatButton(row, text, cmd, padx=8, pady=2).pack(side="left", padx=(0, 6))

        swatch = tk.Label(row, width=2, cursor="hand2", relief="flat", highlightthickness=1,
                          highlightbackground=INPUT_BORDER, text="", font=font(9))
        swatch.pack(side="left", padx=(0, 8), ipady=1)
        entry = self._entry(row, cget() or "None", cset, parse=self._parse_color, width=9)
        entry.pack(side="left", padx=(0, 8), ipady=3)

        def paint(*_a):
            value = entry.var.get().strip()
            if HEX_RE.match(value):
                swatch.config(bg=value, text="")
            else:
                swatch.config(bg=INPUT, fg=DIM, text="∅")
        entry.var.trace_add("write", paint)
        paint()
        swatch.bind("<Button-1>", lambda e: self._pick_color(entry.var))

        btn("None", lambda: entry.var.set("None"))
        btn("Default", lambda: entry.var.set(self._rod_default(ckey, rod) or "None"))
        tol = self._entry(row, tget() if tget() is not None else 0, tset,
                          parse=lambda t: _parse_num(t, "int"), width=4)
        tol.pack(side="left", padx=(6, 8), ipady=3)
        btn("Default", lambda: tol.var.set(_fmt(self._rod_default(tkey, rod) or 0)))
        tk.Label(row, text="Tol", bg=CARD, fg=MUTED, font=font(9)).pack(side="left")

    def _rod_special_card(self, body, rod):
        cls = special_for_rod(rod)
        if cls is None:
            return
        c = Card(body, f"{cls.name} Special", "✧")
        if cls is PinionsAria:
            c.note("Music notes fall straight onto the bar; catching one with your bar raises "
                   "progress speed. While a note falls, the bar is steered under it without "
                   "letting the fish leave the bar.")
            self._toggle(c, "Chase Falling Notes", cls.enable_key)
            self._color_row(c, "Note Color", "pinions_note_color_1", "pinions_note_tolerance")
            self._num(c, "Max Chase Distance (px)", "pinions_note_threshold",
                      sub="ignore notes further than this from the fish")
            self._num(c, "Min Note Size (px²)", "pinions_note_min_area", "int")
        elif cls is HalibutHarpoon:
            c.note("Harpoon volleys and slash effects cross the bar and look like the fish line. "
                   "Only a tall vertical line counts as the fish, and its last position is held "
                   "while a slash hides it.")
            self._toggle(c, "Harpoon Filter", cls.enable_key)
            self._slider(c, "Min Line Height", "halibut_line_min_height", 0.1, 1, 0.05,
                         fmt=lambda v: f"{v:.0%}")
            self._num(c, "Hold Through Gaps (s)", "halibut_target_hold")
            self._num(c, "Max Jump (px)", "halibut_max_jump")
        elif cls is StellarwaveMelody:
            c.note("Touch the zodiac signs in order (♈ ♉ ♊ ♋ …) to light the stars above the "
                   "bar. The next sign is found in the bar and the cursor is moved onto it while "
                   "reeling. Pick this rod's bar/target colours below with the eyedropper.")
            self._toggle(c, "Collect Zodiac Signs", cls.enable_key)
            self._toggle(c, "Click Signs", "stellarwave_click_signs",
                         sub="off = only hover over them")
            self._toggle(c, "Signs Only", "stellarwave_signs_only",
                         sub="don't steer the reel bar, only click signs")
            self._toggle(c, "YOLO Sign Detector", "stellarwave_use_yolo",
                         sub="needs ultralytics + 'YOLO rods/Stellarwave Signs.pt'")
            self._slider(c, "YOLO Confidence", "stellarwave_yolo_confidence", 0.1, 0.9, 0.05)
            self._slider(c, "Match Threshold", "stellarwave_match_threshold", 0.3, 0.9, 0.05)
        if not self.cfg.get("rod_specials_enabled", True):
            c.note("Rod specials are turned off globally (rod_specials_enabled in Extra → "
                   "All Settings).")

    @staticmethod
    def _parse_color(text):
        text = text.strip()
        if text == "None":
            return "None"
        if HEX_RE.match(text):
            return text.lower()
        raise ValueError("expected #rrggbb or None")

    def _rod_default(self, key, rod=None):
        d = self._defaults
        return d.get(f"{key}_{rod}", d.get(key)) if rod else d.get(key)

    def _pick_color(self, var):
        """Screen eyedropper (falls back to the system colour dialog)."""
        self.root.withdraw()

        def done(hexcode):
            self.root.deiconify()
            self.root.lift()
            if hexcode:
                var.set(hexcode)

        def open_picker():
            try:
                ScreenColorPicker(self.root, done)
            except Exception as e:
                print(f" Screen colour picker unavailable ({e}); using dialog")
                self.root.deiconify()
                initial = var.get() if HEX_RE.match(var.get()) else None
                _rgb, hexcode = colorchooser.askcolor(color=initial, parent=self.root)
                done(hexcode)
        self.root.after(250, open_picker)

    def _yolo_models(self):
        from .perception import _resource_dir
        try:
            return sorted(f for f in os.listdir(_resource_dir("YOLO rods")) if f.endswith(".pt"))
        except OSError:
            return []

    def _rod_list(self):
        """rod_types from the loaded config, plus built-in rods an older config lacks."""
        rods = list(self.cfg.get("rod_types") or ["Default"])
        for rod in self._defaults.get("rod_types", []):
            if rod not in rods:
                rods.append(rod)
        if rods != self.cfg.get("rod_types"):
            self.set_setting("rod_types", rods)
        return rods

    def _add_rod(self):
        name = _ask_string(self.root, "Add Rod", "New rod name:")
        if not name:
            return
        rods = list(self.cfg.get("rod_types") or ["Default"])
        if name in rods:
            messagebox.showinfo("Add Rod", f"'{name}' already exists.", parent=self.root)
            return
        # start the new rod from the current rod's tuning
        for key in ROD_KEYS:
            value = self.cfg.rod_setting(key)
            if value is not None:
                self.cfg.set(f"{key}_{name}", value)
        rods.append(name)
        self.cfg.set("rod_types", rods)
        self.set_setting("fish_rod_type", name)
        self.refresh("Fish")

    def _delete_rod(self):
        rod = self.cfg.current_rod()
        if rod == "Default":
            messagebox.showinfo("Delete Rod", "The Default rod can't be deleted.",
                                parent=self.root)
            return
        if not messagebox.askyesno("Delete Rod", f"Delete '{rod}' and its settings?",
                                   parent=self.root):
            return
        for key in ROD_KEYS:
            self.cfg.settings.pop(f"{key}_{rod}", None)
        rods = [r for r in (self.cfg.get("rod_types") or []) if r != rod] or ["Default"]
        self.cfg.set("rod_types", rods)
        self.set_setting("fish_rod_type", "Default" if "Default" in rods else rods[0])
        self.refresh("Fish")

    # -- Totem -----------------------------------------------------------------
    def _page_totem(self, body):
        style = self.cfg.get("totem_style", "Disabled")
        c = Card(body, "Totem Configuration", "✦")
        self._choice(c, "Totem Style", "totem_style", TOTEM_STYLES,
                     on_change=lambda v: self.refresh("Totem"))
        if style != "Disabled":
            self._choice(c, "Trigger Mode", "totem_trigger_mode", ["Cycles", "Time"],
                         on_change=lambda v: self.refresh("Totem"))
            if self.cfg.get("totem_trigger_mode", "Cycles") == "Time":
                self._num(c, "Check Every (s)", "totem_check_every_seconds")
            else:
                self._num(c, "Check Every N Cycles", "totem_check_every_n_cycles", "int")
            t = Card(body, f"{style} Timing", "⧗")
            for label, key in TOTEM_TIMING[style]:
                self._num(t, label, key)
            t.note({
                "Day": "Uses the Mystic Mirror when it's night.",
                "Night": "Uses the Sundial when it's day.",
            }.get(style, "Uses the Special Totem hotbar slot."))

        c = Card(body, "Potion", "⚗")
        self._toggle(c, "Use Potions", "potion_style")
        self._num(c, "Use Every (s)", "potion_check_every_seconds")
        self._num(c, "Bag Delay", "potion_bag_delay")
        self._num(c, "Equip Delay", "potion_equip_delay")

    # -- Reconnect (stored only) / Discord ---------------------------------------
    def _page_reconnect(self, body):
        c = Card(body, "Auto Reconnect", "⟳")
        c.note("Auto-reconnect isn't part of the core rewrite — these settings are saved "
               "for the original app but not used here.")
        self._toggle(c, "Enabled", "auto_reconnect_enabled")
        self._num(c, "Check Every N Cycles", "auto_reconnect_check_every_n_cycles", "int")
        self._num(c, "Reconnect Delay (s)", "auto_reconnect_delay")
        self._num(c, "Post-Rejoin Delay (s)", "auto_reconnect_post_rejoin_delay")

    def _page_discord(self, body):
        c = Card(body, "Discord Webhook", "✉")
        self._toggle(c, "Enabled", "discord_enabled", sub="send notifications while fishing")
        right = c.row("Webhook URL")
        entry = self._entry(right, self.cfg.get("webhook_url", ""),
                            lambda v: self.set_setting("webhook_url", v),
                            parse=_parse_webhook, width=48, justify="left", show="•")
        reveal = FlatButton(right, "Show", lambda: None)
        reveal.pack(side="right", padx=(8, 0))
        entry.pack(side="right", ipady=3)

        def flip():
            hidden = entry.cget("show") == "•"
            entry.config(show="" if hidden else "•")
            reveal.config(text="Hide" if hidden else "Show")
        reveal._command = flip

        right = c.row("Test", "sends one message using the style below")
        result = tk.Label(right, bg=CARD, fg=MUTED, font=font(9, "bold"))
        test_btn = FlatButton(right, "Send Test", lambda: self._test_webhook(entry, result),
                              kind="accent")
        test_btn.pack(side="right")
        result.pack(side="right", padx=12)

        c = Card(body, "Messages", "☷")
        style = self.cfg.get("discord_message_style", "Screenshot")
        self._choice(c, "Message Style", "discord_message_style", ["Screenshot", "Text"],
                     sub="Screenshot attaches a capture of your screen",
                     on_change=lambda v: self.refresh("Discord"))
        loops_key = "loops_screenshot" if style == "Screenshot" else "loops_text"
        self._num(c, "Report Every N Loops", loops_key, "int",
                  sub="stats after every N fish (0 = off)")
        self._num(c, "Status Every (min)", "discord_status_interval_minutes",
                  sub="periodic update while running (0 = off)")
        self._toggle(c, "Start / Stop", "discord_notify_start_stop",
                     sub="message when the macro starts or stops")
        self._toggle(c, "Errors", "discord_notify_errors",
                     sub="message when a cycle crashes (max once a minute)")
        right = c.row("Ping User ID", "@mention this user on errors (blank = nobody)")
        self._entry(right, self.cfg.get("discord_ping_user_id", ""),
                    lambda v: self.set_setting("discord_ping_user_id", v),
                    parse=_parse_user_id, width=22).pack(side="right", ipady=3)
        c.note("Messages include the rod, loops, fish caught, cycles, runtime, fish/hour and "
               "the current status. They're sent in the background, so a slow connection "
               "never delays the macro. Keep the webhook URL private — anyone with it can "
               "post to your channel.")

    def _test_webhook(self, entry, result):
        text = entry.var.get().strip()
        if not text:
            result.config(text="Enter a webhook URL first", fg=DANGER)
            return
        if not valid_webhook_url(text):
            result.config(text="Not a Discord webhook URL", fg=DANGER)
            return
        if text != self.cfg.get("webhook_url", ""):
            self.set_setting("webhook_url", text)
        result.config(text="Sending…", fg=MUTED)
        fut = self.bot.notifier.test()

        def poll():
            if not result.winfo_exists():
                return
            if not fut.done():
                self.root.after(150, poll)
                return
            ok, detail = fut.result()
            result.config(text="✓ Sent" if ok else f"✗ {detail}",
                          fg=STATUS_COLORS["green"] if ok else DANGER)
        poll()

    # -- Extra -------------------------------------------------------------------
    def _page_extra(self, body):
        c = Card(body, "Session", "◷")
        right = c.row("Config File")
        tk.Label(right, text=self.cfg.path, bg=CARD, fg=MUTED, font=font(9, family=MONO)).pack(
            side="right")
        right = c.row("Configuration")
        FlatButton(right, "Reset Defaults", self._reset, kind="danger").pack(side="right")
        FlatButton(right, "Reload File", self._reload).pack(side="right", padx=8)
        if IS_WINDOWS:
            FlatButton(right, "Open Folder", lambda: os.startfile(
                os.path.dirname(os.path.abspath(self.cfg.path)))).pack(side="right")

        c = Card(body, "All Settings", "≡")
        bar = tk.Frame(c.body, bg=CARD)
        bar.pack(fill="x", padx=10, pady=(6, 8))
        tk.Label(bar, text="Search", bg=CARD, fg=TEXT, font=LABEL_FONT).pack(side="left")
        self.search_var = tk.StringVar()
        tk.Entry(bar, textvariable=self.search_var, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=font(10), highlightthickness=1,
                 highlightbackground=INPUT_BORDER, highlightcolor=ACCENT).pack(
            side="left", fill="x", expand=True, padx=10, ipady=4)
        self.search_var.trace_add("write", lambda *a: self._fill_all())
        frame = tk.Frame(c.body, bg=CARD)
        frame.pack(fill="x", padx=10)
        self.tree = ttk.Treeview(frame, columns=("value",), show="tree headings", height=14,
                                 style="Dark.Treeview")
        self.tree.heading("#0", text="Key")
        self.tree.heading("value", text="Value")
        self.tree.column("#0", width=360)
        self.tree.column("value", width=420)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview,
                           style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._edit_row)
        c.note("Double-click a row to edit it.")
        self._fill_all()

        c = Card(body, "Log", "▤")
        frame = tk.Frame(c.body, bg=CARD)
        frame.pack(fill="x", padx=10, pady=(6, 4))
        self.log_text = tk.Text(frame, height=16, wrap="word", font=font(9, family=MONO),
                                bg=INPUT, fg="#c9cfda", insertbackground=TEXT, relief="flat",
                                highlightthickness=1, highlightbackground=INPUT_BORDER,
                                padx=8, pady=6)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview,
                           style="Dark.Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        if self._log_lines:
            self.log_text.insert("end", "\n".join(self._log_lines) + "\n")
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _fill_all(self):
        if not self.tree.winfo_exists():
            return
        self.tree.delete(*self.tree.get_children())
        needle = self.search_var.get().lower()
        for key, value in self.cfg.settings.items():
            if needle and needle not in key.lower():
                continue
            shown = json.dumps(value) if isinstance(value, (list, dict, bool)) else str(value)
            self.tree.insert("", "end", iid=key, text=key, values=(shown,))

    def _edit_row(self, event):
        key = self.tree.identify_row(event.y)
        if not key:
            return
        like = self.cfg.get(key)
        x, y, w, h = self.tree.bbox(key, "value")
        initial = json.dumps(like) if isinstance(like, (list, dict, bool)) else str(like)
        var = tk.StringVar(value=initial)
        entry = tk.Entry(self.tree, textvariable=var, bg=CARD, fg=TEXT, insertbackground=TEXT,
                         relief="flat", font=font(9), highlightthickness=1,
                         highlightcolor=ACCENT)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")

        def commit(_e=None):
            if not entry.winfo_exists():
                return
            try:
                value = _coerce(var.get(), like)
            except ValueError as err:
                messagebox.showerror("Invalid value", f"{key}: {err}", parent=self.root)
                entry.focus_set()
                return
            entry.destroy()
            self.set_setting(key, value)
            # keep the other tabs in sync with the edited value
            self.refresh(*[n for n in self.pages if n != "Extra"])
            self._fill_all()
            if self.tree.exists(key):
                self.tree.see(key)
                self.tree.selection_set(key)
        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", lambda e: entry.destroy())

    def _reload(self):
        self.cfg.load()
        self.refresh()
        self._apply_window_prefs()
        self._refresh_start_btn(force=True)

    def _reset(self):
        if not messagebox.askyesno(
                "Reset config", "Replace ALL settings and areas with the defaults?",
                parent=self.root):
            return
        self.cfg._doc = self.cfg._load_defaults()
        self.cfg.save()
        self.refresh()
        self._apply_window_prefs()
        self._refresh_start_btn(force=True)

    def _drain_log(self):
        lines = []
        try:
            while len(lines) < 200:
                lines.append(self.log.queue.get_nowait())
        except queue.Empty:
            pass
        if not lines:
            return
        self._log_lines.extend(lines)
        if not (self.log_text and self.log_text.winfo_exists()):
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\n".join(lines) + "\n")
        excess = int(self.log_text.index("end-1c").split(".")[0]) - 1000
        if excess > 0:
            self.log_text.delete("1.0", f"{excess}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- control -------------------------------------------------------------------
    def toggle(self):
        if self.bot.is_running:
            self.bot.stop()
            return
        self._save_now()
        prefs = self.cfg.global_gui_settings
        if prefs.get("Auto Focus Roblox", False) or prefs.get("Auto Maximize Roblox", False):
            self._focus_roblox(prefs.get("Auto Focus Roblox", False),
                               prefs.get("Auto Maximize Roblox", False))
        if prefs.get("Auto Minimize GUI", False):
            self.root.iconify()
        self.bot.start()

    @staticmethod
    def _focus_roblox(focus, maximize):
        try:
            import win32gui
            hwnd = win32gui.FindWindow(None, "Roblox")
            if hwnd:
                win32gui.ShowWindow(hwnd, 3 if maximize else 9)  # SW_MAXIMIZE / SW_RESTORE
                if focus:
                    win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _handle_hotkey(self, action):
        if action == "Start/Stop":
            self.toggle()
        elif action == "Change Area":
            self.bot.stop()
            self.root.deiconify()
            self.root.lift()
            self.show_page("Main")
            if self._areas_card and self._areas_card.winfo_exists():
                self.pages["Main"].scroll_to(self._areas_card)
        elif action == "Exit":
            self.quit()

    def _refresh_start_btn(self, force=False):
        running = self.bot.is_running
        if not force and getattr(self, "_shown_running", None) == running:
            return
        self._shown_running = running
        key = self.cfg.hotkeys.get("Start/Stop", "")
        hint = f"  ({key})" if key else ""
        if running:
            self.start_btn.config(text=f"■  Stop{hint}")
            self.start_btn.set_kind("danger")
        else:
            self.start_btn.config(text=f"▶  Start{hint}")
            self.start_btn.set_kind("accent")

    def _tick(self):
        try:
            while True:
                action = self._hotkey_events.get_nowait()
                if not self._rebind:  # don't fire hotkeys while choosing a new key
                    self._handle_hotkey(action)
        except queue.Empty:
            pass
        self._refresh_start_btn()
        color = self.bot.status_color
        dot = STATUS_COLORS.get(color)
        if dot is None:  # bot uses "white" for neutral states
            dot = STATUS_COLORS["green"] if self.bot.is_running else STATUS_COLORS["red"]
        self.status_dot.itemconfigure(self._dot, fill=dot)
        self.status_label.config(text=self.bot.status_text)
        self.sub_label.config(text=self.bot.status_sub)
        self.count_label.config(
            text=f"Fish {self.bot.loop_count}  ·  Caught {self.bot.caught_count}  ·  "
                 f"Cycles {self.bot._total_cycle_count}")
        self._drain_log()
        self.root.after(150, self._tick)

    def quit(self):
        self.bot.quit()
        try:
            self.bot.input.left_up()
        except Exception:
            pass
        if self._save_job:
            self._save_now()
        if self.overlay:
            self.overlay.destroy()
        self.root.destroy()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="hydra_bot.gui")
    parser.add_argument("--config", default=os.path.join(_app_dir(), "config.json"))
    parser.add_argument("--port", type=int, default=C.HYDRA_API_PORT)
    parser.add_argument("--no-api", action="store_true", help="don't start the HTTP API")
    args = parser.parse_args(argv)

    log = GuiLog(sys.stdout)
    sys.stdout = log
    sys.stderr = GuiLog(sys.stderr)
    sys.stderr.queue = log.queue  # tracebacks show up in the Log tab too

    print(" Starting Hydra (GUI)...")
    bot = HydraBot(args.config)
    print(f" Config: {bot.cfg.loaded_from or bot.cfg.path + ' (new, using defaults)'}")
    if not args.no_api:
        try:
            start_api_server(bot, log, port=args.port)
        except OSError as e:
            print(f" HTTP API not started (port {args.port} busy?): {e}")

    root = tk.Tk()
    HydraGUI(root, bot, log)
    root.mainloop()
    os._exit(0)  # don't wait on a bot thread stuck in a capture call


if __name__ == "__main__":
    main()
