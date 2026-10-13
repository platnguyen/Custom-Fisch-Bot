"""Low-level Windows input, reconstructed from the backend's input helpers.

The original drives Roblox through raw ``user32`` calls (``SetCursorPos`` +
``mouse_event``) and ``win32api.keybd_event`` with scancodes. This module keeps
that behaviour but isolates the Win32 surface so the rest of the package stays
import-clean on non-Windows hosts (the calls simply raise if invoked there).
"""
from __future__ import annotations

import ctypes
import time

from . import constants as C

try:  # Windows-only dependencies, imported lazily.
    import win32api
    import win32con
    import win32gui

    _WINDLL = ctypes.windll  # type: ignore[attr-defined]
    _HAVE_WIN32 = True
except Exception:  # pragma: no cover - non-Windows import guard
    win32api = win32con = win32gui = None  # type: ignore
    _WINDLL = None
    _HAVE_WIN32 = False


def _user32():
    if not _HAVE_WIN32:
        raise RuntimeError("Win32 input is only available on Windows")
    return _WINDLL.user32


def set_dpi_aware() -> None:
    """Use physical pixels so SetCursorPos agrees with MSS and the stored boxes.

    Without this, 125%/150% display scaling scales every cursor move, which
    pushes positions down/right (e.g. off the game and onto the hotbar).
    """
    if not _HAVE_WIN32:
        return
    try:
        _WINDLL.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            _WINDLL.user32.SetProcessDPIAware()
        except Exception:
            pass


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def is_roblox_window_active() -> bool:
    """True when the focused window looks like Roblox/Fisch (recovered)."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd).lower()
        return "roblox" in title or "fisch" in title
    except Exception:
        return True  # original fails open


class InputController:
    """Mouse + keyboard primitives used by every stage."""

    # -- mouse buttons ----------------------------------------------------
    def left_down(self) -> None:
        _user32().mouse_event(C.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

    def left_up(self) -> None:
        _user32().mouse_event(C.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def set_cursor(self, x: int, y: int) -> None:
        u = _user32()
        u.SetCursorPos(int(x), int(y))
        # The original nudges 1px relative so Roblox registers the move.
        u.mouse_event(C.MOUSEEVENTF_MOVE, 0, 1, 0, 0)

    def get_cursor(self) -> tuple[int, int]:
        pt = _POINT()
        _user32().GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def move_cursor_verified(self, x: int, y: int, settle: float = 0.035,
                             tries: int = 4, tolerance: int = 3) -> bool:
        """Move the cursor and make sure it *stays* there.

        Roblox handles input once a frame, so after a right-drag it can warp
        the cursor back to where the drag lock started a frame *after* our
        own SetCursorPos. Wait a frame, check, and repeat until it holds.
        Returns False if the cursor kept being moved elsewhere.
        """
        x, y = int(x), int(y)
        for _ in range(max(1, tries)):
            self.set_cursor(x, y)
            time.sleep(settle)
            cx, cy = self.get_cursor()
            # set_cursor nudges 1px down so Roblox registers the move
            if abs(cx - x) <= tolerance and abs(cy - y) <= tolerance + 1:
                return True
        return False

    def click_at(self, x: int, y: int, click_count: int = 1) -> None:
        """Reconstruction of ``_click_at_position``."""
        u = _user32()
        u.SetCursorPos(int(x), int(y))
        u.mouse_event(C.MOUSEEVENTF_MOVE, 0, 0, 0, 0)
        for i in range(click_count):
            u.mouse_event(C.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            u.mouse_event(C.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if i < click_count - 1:
                time.sleep(0.03)

    # -- scroll zoom ------------------------------------------------------
    def scroll_zoom(self, amount: int, direction: str, is_running=lambda: True) -> bool:
        """Scroll the wheel ``amount`` steps. direction 'down' zooms out.

        Reconstruction of ``_perform_scroll_zoom`` (interruptible).
        """
        u = _user32()
        wheel = -C.WHEEL_DELTA if direction == "down" else C.WHEEL_DELTA
        for i in range(int(amount)):
            if not is_running():
                return False
            u.mouse_event(C.MOUSEEVENTF_WHEEL, 0, 0, wheel, 0)
            time.sleep(0.025)
        return True

    # -- camera look ------------------------------------------------------
    def look_action(self, center_x: int, center_y: int, pixels: int, direction: str) -> None:
        """Right-click-drag the camera up/down (``_perform_look_action``)."""
        u = _user32()
        try:
            u.SetCursorPos(int(center_x), int(center_y))
            u.mouse_event(C.MOUSEEVENTF_MOVE, 0, 0, 0, 0)
            u.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            time.sleep(0.025)
            steps = 10
            pixels_per_step = int(pixels) // steps
            remainder = int(pixels) % steps
            dy = pixels_per_step if direction == "down" else -pixels_per_step
            for _ in range(steps):
                u.mouse_event(C.MOUSEEVENTF_MOVE, 0, dy, 0, 0)
                time.sleep(C.MOUSE_SMOOTH_DELAY)
            if remainder > 0:
                rdy = remainder if direction == "down" else -remainder
                u.mouse_event(C.MOUSEEVENTF_MOVE, 0, rdy, 0, 0)
            u.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            # If Roblox hadn't locked the cursor yet, the drag moved the real
            # cursor up to ``pixels`` px (clamped at the screen edge - for a
            # look-down that's the hotbar), and Roblox may put it back there
            # when it handles the release. Restore it only once that settles.
            self.move_cursor_verified(center_x, center_y)
        except Exception as e:  # always release right button
            print(f"    Error in look action: {e}")
            try:
                u.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            except Exception:
                pass

    # -- keyboard ---------------------------------------------------------
    def send_key_press(self, key_char: str) -> None:
        """Press a single key or 'enter' (reconstruction of ``_send_key_press``)."""
        if key_char.lower() == "enter":
            vk_code = win32con.VK_RETURN
            scan_code = 0
        elif len(key_char) == 1:
            result = win32api.VkKeyScan(key_char)
            if result == -1:
                print(f"     Cannot map key: {key_char}")
                return
            vk_code = result & 0xFF
            shift_state = (result >> 8) & 0xFF
            need_shift = (shift_state & 1) != 0
            need_ctrl = (shift_state & 2) != 0
            need_alt = (shift_state & 4) != 0
            if need_shift:
                win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0)
            if need_ctrl:
                win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
            if need_alt:
                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            scan_code = win32api.MapVirtualKey(vk_code, 0)
            win32api.keybd_event(vk_code, scan_code, 0, 0)
            win32api.keybd_event(vk_code, scan_code, win32con.KEYEVENTF_KEYUP, 0)
            if need_alt:
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            if need_ctrl:
                win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
            if need_shift:
                win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0)
            return
        else:
            print(f"     Unsupported key format: {key_char}")
            return
        scan_code = win32api.MapVirtualKey(vk_code, 0)
        win32api.keybd_event(vk_code, scan_code, 0, 0)
        time.sleep(0.01)
        win32api.keybd_event(vk_code, scan_code, win32con.KEYEVENTF_KEYUP, 0)
