"""Fish (reel) stage: hold/release the mouse to keep the bar on the target.

Reconstruction of ``_execute_fish_stage`` + ``_execute_fish_stage_color`` and
its Line/YOLO siblings. All three modes share one PD control loop and differ
only in how the *target line* and *bar centre* are perceived each frame:

* Color - colour masks for the target line and left/right bar edges
* Line  - vertical-edge detection
* YOLO  - an Ultralytics model emitting ``icon`` (target) and ``bar`` boxes

Control law (recovered):
    error         = target_x - bar_center_x
    bar_velocity  = (bar_center_x - last_bar_x) / dt
    damping       = 2.0 if (|err| shrinking and bar moving toward target) else 0.5
    control       = clamp(kp*error + kd*bar_velocity*damping, -pd_clamp, +pd_clamp)
    hold_mouse    = control > 0           # holding reels the bar one way
Edge overrides force release at the far-left / hold at the far-right.
"""
from __future__ import annotations

import time
from typing import Optional

import numpy as np

from .. import detection as D
from .. import perception as P
from ..capture import Capture, grab_box_once, screen_size
from .. import constants as C
from . import rod_specials

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


class SteadyTapper:
    """"Steady" control: feather the mouse with quick taps instead of long
    hold/release swings.

    Holding pushes the bar one way and releasing lets it fall back, so a long
    hold or release always overshoots. Players keep the bar still by tapping:
    the fraction of each short period spent holding (the duty) sets the net
    push, and a balance duty (``hover``, ~50%) keeps it still. Here the bar is
    driven by speed rather than position:

        wanted_speed = clamp(approach * error, +-max_speed)
        duty         = hover + gain * (wanted_speed - bar_speed)

    Far from the fish the duty saturates (a full hold / release, so long moves
    stay fast); as the bar closes in, the wanted speed drops and the taps brake
    it to a stop on the fish. ``hover`` is learned from any
    persistent speed shortfall (an integral term), since the balance point
    differs per rod.
    """

    def __init__(self, period=0.033, min_tap=0.017, hover=0.5, gain=0.0025,
                 approach=4.5, max_speed=600.0, adapt=0.0004):
        self.period = max(0.02, float(period))
        self.min_tap = max(0.0, float(min_tap))
        self.hover = min(0.85, max(0.15, float(hover)))
        self.gain = float(gain)
        self.approach = float(approach)
        self.max_speed = float(max_speed)
        self.adapt = float(adapt)
        self._cycle_start = None
        self._on = 0.0
        self._velocity = 0.0
        self._ref = None  # (time, x) the velocity is measured from
        self._last_t = None
        self.tapping = False

    def _measure(self, now, x):
        # the game redraws at ~60 Hz, so measure over >= 30 ms to avoid 0/spike aliasing
        if self._ref is None:
            self._ref = (now, x)
            return
        dt = now - self._ref[0]
        if dt >= 0.03:
            v = (x - self._ref[1]) / dt
            self._velocity = 0.5 * self._velocity + 0.5 * v
            self._ref = (now, x)

    def update(self, now, control, error, bar_center, bar_half_width=None) -> bool:
        self._measure(now, bar_center)
        dt = 0.0 if self._last_t is None else now - self._last_t
        self._last_t = now

        if self._cycle_start is None or now - self._cycle_start >= self.period:
            self._cycle_start = now
            wanted = max(-self.max_speed, min(self.max_speed, self.approach * error))
            duty = self.hover + self.gain * (wanted - self._velocity)
            # learn the balance duty: a persistent speed shortfall means the
            # bar needs more (or less) hold than ``hover`` assumes
            if 0 < dt < 0.2:
                shortfall = max(-300.0, min(300.0, wanted - self._velocity))
                self.hover = min(0.85, max(0.15, self.hover + self.adapt * shortfall * self.period))
            duty = min(1.0, max(0.0, duty))
            self.tapping = 0.0 < duty < 1.0
            on = duty * self.period
            # taps shorter than a game frame get lost; snap to nothing / one frame
            if on < self.min_tap:
                on = 0.0 if on < self.min_tap / 2 else self.min_tap
            elif self.period - on < self.min_tap:
                on = self.period if self.period - on < self.min_tap / 2 else self.period - self.min_tap
            self._on = on
        return (now - self._cycle_start) < self._on


def execute_fish_stage(bot) -> Optional[str]:
    """Returns "caught", "lost", "restart" (no fish area) or None (stopped)."""
    style = bot.cfg.get("fish_track_style", "Color")
    if style == "Disabled":
        return None
    print(f" === FISH STAGE ({style}) ===")
    try:
        if style == "YOLO":
            return _reel_loop(bot, mode="YOLO")
        if style == "Line":
            return _reel_loop(bot, mode="Line")
        return _reel_loop(bot, mode="Color")
    finally:
        try:
            bot.input.left_up()
        except Exception:
            pass
        # a rod special may have left the cursor on a sign in the reel bar,
        # right above the hotbar; the next cast clicks wherever it is
        bot.park_cursor()


def _reel_loop(bot, mode: str) -> Optional[str]:
    s = bot.cfg
    rod = s.current_rod()

    # per-rod control gains
    if mode == "Line":
        kp = s.get("fish_line_kp", 0.9)
        kd = s.get("fish_line_kd", 0.3)
        pd_clamp = s.get("fish_line_pd_clamp", 1.0)
        scan_fps = s.get("fish_line_scan_fps", 120)
        lost_timeout = s.get("fish_line_lost_timeout", 0.5)
    elif mode == "YOLO":
        kp = s.get("fish_yolo_kp", 0.9)
        kd = s.get("fish_yolo_kd", 0.3)
        pd_clamp = s.get("fish_pd_clamp", 1.0)
        scan_fps = s.rod_setting("fish_scan_fps", 120)
        lost_timeout = s.rod_setting("fish_lost_timeout", 0.5)
    else:  # Color
        kp = s.rod_setting("fish_kp", 0.9)
        kd = s.rod_setting("fish_kd", 0.3)
        pd_clamp = s.rod_setting("fish_pd_clamp", 1.0)
        scan_fps = s.rod_setting("fish_scan_fps", 120)
        lost_timeout = s.rod_setting("fish_lost_timeout", 0.5)

    scan_delay = (1.0 / scan_fps) if scan_fps > 0 else 0.001

    box = bot.cfg.fish_box
    if not box:
        print(" No fish area set - cannot proceed")
        return "restart"
    width = box["x2"] - box["x1"]

    # colour config (Color/Line perception)
    target_color = s.rod_color("fish_target_line_color")
    left_color = s.rod_color("fish_left_bar_color")
    right_color = s.rod_color("fish_right_bar_color")
    arrow_color = s.rod_color("fish_arrow_color")
    target_tol = s.rod_tolerance("fish_target_line_tolerance")
    left_tol = s.rod_tolerance("fish_left_bar_tolerance")
    right_tol = s.rod_tolerance("fish_right_bar_tolerance")
    arrow_tol = s.rod_tolerance("fish_arrow_tolerance")
    use_line_detection = s.get("use_vertical_line_detection", True)

    yolo = None
    if mode == "YOLO":
        try:
            yolo = P.YoloFishDetector(s.get("fish_yolo_rod", "Tryhard Rod.pt"))
        except Exception as e:
            print(f" YOLO unavailable ({e}); aborting fish stage")
            return "restart"
    yolo_conf = s.get("fish_yolo_confidence", 0.25)

    use_mss = bot.cfg.get("shake_capture_mode", "MSS") == "MSS"
    cam = Capture(box, use_mss=use_mss)
    special = rod_specials.create(bot, box) if s.get("rod_specials_enabled", True) else None

    # holds register anywhere on the game view; keep the cursor off the hotbar
    bot.input.set_cursor(*bot.park_point())

    control_mode = (s.get("fish_line_control_mode", "Steady") if mode == "Line"
                    else s.rod_setting("fish_control_mode", "Steady"))
    tapper = None
    if control_mode == "Steady":
        tapper = SteadyTapper(
            period=s.get("fish_steady_tap_period", 0.033),
            min_tap=s.get("fish_steady_min_tap", 0.017),
            hover=getattr(bot, "_steady_hover", {}).get(rod, s.get("fish_steady_hover", 0.5)),
            gain=s.get("fish_steady_gain", 0.0025),
            approach=s.get("fish_steady_approach", 4.5),
            max_speed=s.get("fish_steady_max_speed", 600),
        )
    print(f" Control mode: {control_mode}")

    last_bar_x = None
    last_error = None
    last_bar_time = time.time()
    is_holding = False
    target_lost_timer = 0.0
    last_scan = time.time()
    last_exit_check = time.time()
    exit_interval = 0.5

    try:
        while bot.running():
            now = time.time()
            try:
                frame = cam.grab()
            except Exception:
                time.sleep(0.001)
                continue
            if frame is None:
                time.sleep(0.001)
                continue

            target_x, bar_center_x, bar_left, bar_right = _perceive(
                bot, frame, mode, yolo, yolo_conf, width,
                target_color, left_color, right_color, arrow_color,
                target_tol, left_tol, right_tol, arrow_tol, use_line_detection,
            )
            if special is not None:
                if mode != "YOLO":
                    target_x = special.filter_target(frame, target_x, now)
                special.tick(now, is_holding)

            # periodic hooked-fish-ended probe (green bar reappears -> caught)
            if now - last_exit_check >= exit_interval:
                last_exit_check = now
                if _check_fish_exit_condition(bot) == "fish_stage":
                    print(" Fish caught (exit condition met)")
                    return "caught"

            signs_only = special is not None and getattr(special, "signs_only", False)
            if signs_only and special.minigame_visible(now):
                # the bar isn't steered here, so losing it doesn't mean the fish is gone
                if is_holding:
                    bot.input.left_up()
                    is_holding = False
                bot.status("Signs Only", f"{special.name}: clicking signs", "green")
                target_lost_timer = 0.0
                last_scan = now
                time.sleep(scan_delay)
                continue

            if target_x is None or bar_center_x is None:
                target_lost_timer += now - last_scan
                if target_lost_timer >= lost_timeout:
                    print(f" Target lost for {lost_timeout}s - exiting fish stage")
                    return "lost"
                last_scan = now
                time.sleep(scan_delay)
                continue
            target_lost_timer = 0.0
            raw_target = target_x
            if special is not None:
                target_x = special.adjust_target(target_x, bar_left, bar_right, now)

            # --- PD control -------------------------------------------
            error = target_x - bar_center_x
            p_term = kp * error
            d_term = 0.0
            dt = now - last_bar_time
            if dt > 0.001 and last_bar_x is not None:
                bar_velocity = (bar_center_x - last_bar_x) / dt
                err_shrinking = (last_error is not None
                                 and abs(error) < abs(last_error))
                moving_toward = ((error > 0 and bar_velocity > 0)
                                 or (error < 0 and bar_velocity < 0))
                damping = 2.0 if (err_shrinking and moving_toward) else 0.5
                d_term = kd * bar_velocity * damping
            control = max(-pd_clamp, min(pd_clamp, p_term + d_term))
            if tapper is not None:
                half = (abs(bar_right - bar_left) / 2.0
                        if bar_left is not None and bar_right is not None else None)
                should_hold = tapper.update(now, control / (pd_clamp or 1.0), error,
                                            bar_center_x, half)
            else:
                should_hold = control > 0

            # edge overrides
            edge = max(1.0, width * 0.08)
            if target_x < edge:
                should_hold = False
            elif target_x > width - edge:
                should_hold = True

            # apply only on state change
            if should_hold and not is_holding:
                bot.input.left_down()
                is_holding = True
            elif not should_hold and is_holding:
                bot.input.left_up()
                is_holding = False
            if tapper is not None and tapper.tapping:
                bot.status("Steady", f"Err {error:.0f}px  duty~{tapper.hover:.0%}", "green")
            elif is_holding:
                bot.status("Holding", f"Err {error:.0f}px", "green")
            else:
                bot.status("Releasing", f"Err {error:.0f}px", "yellow")

            if special is not None:
                note = getattr(special, "note_x", None)
                hidden = getattr(special, "line_hidden", False)
                special_name = special.name
            else:
                note, hidden, special_name = None, False, None
            bot.overlay["fish"] = {
                "t": now, "box": box, "target": raw_target, "aim": target_x,
                "bar": (bar_left, bar_right), "holding": is_holding, "err": error,
                "note_x": note, "hidden": hidden, "special": special_name,
                "tapping": bool(tapper and tapper.tapping),
            }

            last_bar_x = bar_center_x
            last_bar_time = now
            last_error = error
            last_scan = now
            time.sleep(scan_delay)
        return None
    finally:
        cam.close()
        if tapper is not None:
            # remember the learned balance point for this rod's next fish
            if not hasattr(bot, "_steady_hover"):
                bot._steady_hover = {}
            bot._steady_hover[rod] = tapper.hover
        if special is not None:
            special.close()


def _perceive(bot, frame, mode, yolo, yolo_conf, width,
              target_color, left_color, right_color, arrow_color,
              target_tol, left_tol, right_tol, arrow_tol, use_line_detection):
    """Return (target_x, bar_center_x, bar_left_x, bar_right_x); any may be None."""
    if mode == "YOLO" and yolo is not None:
        icon_cx, bar_lx, bar_rx, bar_cx = yolo.detect(frame, yolo_conf)
        return icon_cx, bar_cx, bar_lx, bar_rx

    det = P.detect_fish_colors_in_frame(
        frame,
        fish_track_style=("Line" if mode == "Line" else "Color"),
        use_line_detection=(mode == "Line" or use_line_detection),
        target_line_color=target_color,
        left_bar_color=left_color,
        right_bar_color=right_color,
        arrow_color=arrow_color,
        target_line_tolerance=target_tol,
        left_bar_tolerance=left_tol,
        right_bar_tolerance=right_tol,
        arrow_tolerance=arrow_tol,
    )
    target_x = det["target_middle_x"]
    bar_left = det["bar_left_x"]
    bar_right = det["bar_right_x"]
    arrow = det["arrow_center_x"]

    bar_center = None
    if bar_left is not None and bar_right is not None:
        if bar_left > bar_right:
            bar_left, bar_right = bar_right, bar_left
        bar_center = (bar_left + bar_right) / 2.0
    elif arrow is not None:
        bar_center = arrow
        bar_left = bar_right = None  # width unknown
    return target_x, bar_center, bar_left, bar_right


def _check_fish_exit_condition(bot) -> Optional[str]:
    """Green reel bar visible at bottom-left again -> minigame finished."""
    try:
        sw, sh = screen_size()
    except Exception:
        return None
    probe = {
        "x1": 0,
        "y1": int(0.8333333333333334 * sh),
        "x2": int(0.1953125 * sw),
        "y2": sh,
    }
    frame = grab_box_once(probe)
    if frame is None:
        return None
    target = C.SHAKE_EXIT_GREEN_BGR
    tolerance = 60
    lower = np.clip(target - tolerance, 0, 255).astype(np.uint8)
    upper = np.clip(target + tolerance, 0, 255).astype(np.uint8)
    mask = cv2.inRange(frame, lower, upper)
    if cv2.countNonZero(mask) > 0:
        return "fish_stage"
    return None
