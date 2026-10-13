"""HydraBot - the headless orchestrator.

Reconstruction of the backend's ``run_bot_cycle`` / ``_execute_start_block``
orchestration, minus the Tk GUI. The original schedules each cycle with
``tk.after``; here the cycle runs on a background thread gated by a Start/Stop
flag, which is cleaner and behaviourally equivalent for headless use.

Cycle order (recovered):
    START (totem / potion / reconnect triggers)
      -> MISC (loadout)
      -> CAST
      -> SHAKE  -> 'fish_stage' | 'restart' | None
      -> FISH   (reel) when shake hooked a fish, else auto-scan for fish
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from . import constants as C
from . import perception as P
from .config import Config
from .win_input import InputController
from .capture import Capture, screen_size
from .discord import DiscordNotifier
from . import detection as D
from .stages import cast as cast_stage
from .stages import shake as shake_stage
from .stages import fish as fish_stage
from .stages import totem as totem_stage
from .stages import potion as potion_stage


class HydraBot:
    def __init__(self, config_path: str = "config.json"):
        self.cfg = Config(config_path)
        self.cfg.load()
        self.input = InputController()

        self._running = False
        self._quitting = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # coordinate scaling (boxes already stored in screen px -> identity)
        self.scale_x = 1.0
        self.scale_y = 1.0

        try:
            self.screen_width, self.screen_height = screen_size()
        except Exception:
            self.screen_width, self.screen_height = 1920, 1080

        # shared stage state
        self._shake_green_history: list[int] = []
        self._totem_sun_tmpl, self._totem_moon_tmpl = (None, None)
        self._totem_cycle_counter = 0
        self._totem_last_time = 0.0
        self._potion_last_time = 0.0
        self._total_cycle_count = 0
        self.loop_count = 0
        self.caught_count = 0  # fish stages that ended with the catch probe
        self.started_at: Optional[float] = None
        self._park_warned = False

        # status exposed via the HTTP API
        self.status_text = "Idle"
        self.status_color = "white"
        self.status_sub = ""
        # live state for the on-screen overlay (see overlay.py)
        self.overlay: dict = {}

        self.notifier = DiscordNotifier(self)

    # -- public control (also used by the HTTP API) -----------------------
    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self.started_at = time.time()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
        self.status("Running", color="green")
        self.notifier.on_start()

    def stop(self) -> None:
        was_running = self._running
        self._running = False
        try:
            self.input.left_up()
        except Exception:
            pass
        self.status("Stopped", color="yellow")
        if was_running:
            self.notifier.on_stop()

    def toggle(self) -> bool:
        if self._running:
            self.stop()
        else:
            self.start()
        return self._running

    def quit(self) -> None:
        if self._running:
            self.notifier.on_stop()
        self._quitting = True
        self._running = False
        # give a queued "stopped" message a moment to go out before exit
        self.notifier.close(timeout=3.0)

    # -- helpers used by stages ------------------------------------------
    def running(self) -> bool:
        return self._running and not self._quitting

    def sleep(self, duration: float) -> bool:
        """Interruptible sleep; returns False if Start/Stop flipped off."""
        if duration <= 0:
            return True
        end = time.time() + duration
        while time.time() < end:
            if not self.running():
                return False
            remaining = end - time.time()
            time.sleep(min(C.INTERRUPTIBLE_SLEEP_CHECK_INTERVAL, max(0, remaining)))
        return True

    def status(self, text: str, sub: str = "", color: str = "white") -> None:
        self.status_text = text
        self.status_sub = sub
        self.status_color = color

    def _enabled(self, name: str) -> bool:
        return self.cfg.bottom_button_states.get(name, True)

    # -- cursor parking ---------------------------------------------------
    def park_point(self) -> tuple[int, int]:
        """Where the cursor rests between actions (casts click wherever it is).

        Custom: ``cursor_park_x/y`` (screen px). Auto: centred over the shake
        area, a little below its top edge, and always well above the reel bar,
        so it can never sit on the hotbar (which is below the reel bar).
        """
        s = self.cfg
        sw, sh = self.screen_width, self.screen_height
        if s.get("cursor_park_mode", "Auto") == "Custom":
            x, y = s.get("cursor_park_x", 0), s.get("cursor_park_y", 0)
            if x or y:
                return int(min(max(x, 0), sw - 1)), int(min(max(y, 0), sh - 1))
        shake = s.shake_box
        if shake:
            x = (shake["x1"] + shake["x2"]) // 2
            y = shake["y1"] + int(0.1 * (shake["y2"] - shake["y1"]))
        else:
            x, y = sw // 2, int(0.35 * sh)
        # keep clear of the reel bar / hotbar below it and the top-bar buttons
        fish = s.fish_box
        if fish:
            y = min(y, fish["y1"] - int(0.12 * sh))
        y = max(y, int(0.1 * sh))
        return int(x), int(y)

    def park_cursor(self) -> bool:
        """Move the cursor to the park point and verify it stayed there."""
        if not self.running():
            return False  # don't grab the user's mouse after Stop
        x, y = self.park_point()
        try:
            ok = self.input.move_cursor_verified(x, y)
        except Exception as e:
            print(f"    Cursor park failed: {e}")
            return False
        if not ok and not self._park_warned:
            self._park_warned = True  # once per session; it's usually a lock mode
            print(f"    Cursor didn't stay at the park point {x, y} - is Roblox "
                  "in shift-lock or first person?")
        return ok

    # -- the cycle --------------------------------------------------------
    def _loop(self) -> None:
        # lazily load totem templates once
        if self._totem_sun_tmpl is None and self._totem_moon_tmpl is None:
            try:
                self._totem_sun_tmpl, self._totem_moon_tmpl = P._load_totem_templates()
            except Exception:
                pass
        while self.running():
            try:
                self._run_cycle()
            except Exception as e:
                print(f" CRITICAL ERROR in cycle: {e}")
                import traceback
                traceback.print_exc()
                self.notifier.on_error(f"{type(e).__name__}: {e}")
            time.sleep(C.BOT_CYCLE_CHECK_INTERVAL)
        # loop exits when stopped; thread will be recreated on next start

    def _run_cycle(self) -> None:
        self._total_cycle_count += 1
        self._execute_start_block()
        if not self.running():
            return

        if self._enabled("Misc"):
            self.status("Misc Block")
            self._execute_misc_block()
        if not self.running():
            return

        # Normal casts click wherever the cursor is, and the last stage (shake
        # clicks, a rod special, a totem) may have left it anywhere
        self.park_cursor()
        if not self.running():
            return

        if self._enabled("Cast"):
            self.status("Cast Block")
            cast_stage.execute_cast(self)
        if not self.running():
            return

        shake_result = None
        if self._enabled("Shake"):
            self.status("Shake Block")
            shake_result = shake_stage.execute_shake(self)
        if not self.running():
            return

        if shake_result == "restart":
            return
        if shake_result == "fish_stage":
            self.status("Fish Stage")
            self._record_fish(fish_stage.execute_fish_stage(self))
            return

        # Auto mode: scan the fish area for the minigame colours, then reel.
        track = self.cfg.get("fish_track_style", "Color")
        if track != "Disabled" and self._enabled("Fish"):
            if self._wait_for_fish():
                self.status("Fish Stage")
                self._record_fish(fish_stage.execute_fish_stage(self))

    def _record_fish(self, result: Optional[str]) -> None:
        self.loop_count += 1
        if result == "caught":
            self.caught_count += 1
        self.notifier.on_loop(self.loop_count)

    def _execute_start_block(self) -> None:
        s = self.cfg
        # totem trigger
        totem_style = s.get("totem_style", "Disabled")
        if totem_style != "Disabled":
            trigger = s.get("totem_trigger_mode", "Cycles")
            if trigger == "Time":
                every = float(s.get("totem_check_every_seconds", 120.0))
                now = time.time()
                if now - self._totem_last_time >= every:
                    self._totem_last_time = now
                    totem_stage.execute_totem_check(self)
            else:
                every = int(s.get("totem_check_every_n_cycles", 10))
                self._totem_cycle_counter += 1
                if self._totem_cycle_counter >= every:
                    self._totem_cycle_counter = 0
                    totem_stage.execute_totem_check(self)
        # potion trigger (time-based)
        if s.get("potion_style", False):
            every = float(s.get("potion_check_every_seconds", 600.0))
            now = time.time()
            if now - self._potion_last_time >= every:
                self._potion_last_time = now
                potion_stage.execute_potion_use(self)
        # auto-reconnect is intentionally out of scope for the core rewrite.

    def _execute_misc_block(self) -> None:
        s = self.cfg
        if not s.get("auto_select_rod_enabled", False):
            return
        bag = s.get("hotbar_equipment_bag", "2")
        rod = s.get("hotbar_fishing_rod", "1")
        d1 = s.get("auto_select_rod_delay1", 0.0)
        d2 = s.get("auto_select_rod_delay2", 1.0)
        d3 = s.get("auto_select_rod_delay3", 0.0)
        if d1 > 0 and not self.sleep(d1):
            return
        self.input.send_key_press(bag)
        if d2 > 0 and not self.sleep(d2):
            return
        self.input.send_key_press(rod)
        if d3 > 0:
            self.sleep(d3)

    def _wait_for_fish(self) -> bool:
        """Poll the fish area until the minigame colours appear or timeout."""
        s = self.cfg
        style = s.get("shake_style", "Pixel")
        if style == "Navigation":
            timeout = s.get("nav_fail_cast_timeout", 20)
        else:
            timeout = s.get("shake_fail_cast_timeout", 15)
        box = self.cfg.fish_box
        if not box:
            return False
        use_mss = s.get("shake_capture_mode", "MSS") == "MSS"
        cam = Capture(box, use_mss=use_mss)
        start = time.time()
        rod = s.current_rod()
        target_color = s.rod_color("fish_target_line_color")
        target_tol = s.rod_tolerance("fish_target_line_tolerance")
        try:
            while self.running() and (time.time() - start) < timeout:
                frame = cam.grab()
                if frame is not None and target_color:
                    if D.detect_color_count(frame, target_color, target_tol) > 0:
                        return True
                time.sleep(0.01)
            return False
        finally:
            cam.close()
