"""Cast stage: Normal and Perfect casting + the Perfect-Cast release solver.

Reconstruction of ``_execute_cast_block`` / ``_execute_cast_normal_flow`` /
``_execute_cast_perfect_flow`` and the default release style
``_perform_perfect_cast_release_idiotproof``. The cosmetic Tk overlays that the
original draws during the release are intentionally omitted (see README).
"""
from __future__ import annotations

import math
import time

import numpy as np

from .. import constants as C
from .. import detection as D
from ..capture import region_to_monitor

try:
    import mss as _mss
    import cv2
except Exception:  # pragma: no cover
    _mss = None
    cv2 = None


def execute_cast(bot) -> None:
    mode = bot.cfg.get("cast_mode", "Normal")
    if mode == "Disabled":
        return
    if mode == "Normal":
        _cast_normal(bot)
    elif mode == "Perfect":
        _cast_perfect(bot)
    else:
        print(f"  Unknown cast mode: {mode}")


def _cast_normal(bot) -> None:
    s = bot.cfg
    delay1 = s.get("cast_delay1", 1.25)
    delay2 = s.get("cast_delay2", 0.2)
    delay3 = s.get("cast_delay3", 1.0)
    try:
        if delay1 > 0 and not bot.sleep(delay1):
            return
        bot.input.left_down()
        if not bot.sleep(delay2):
            bot.input.left_up()
            return
        bot.input.left_up()
        if delay3 > 0 and not bot.sleep(delay3):
            return
    except Exception as e:
        print(f"    Error in Normal cast flow: {e}")


def _cast_perfect(bot) -> None:
    s = bot.cfg
    delay1 = s.get("perfect_delay1", 0.0)
    zoom_out = s.get("zoom_out", 13)
    delay2 = s.get("perfect_delay2", 0.0)
    zoom_in = s.get("zoom_in", 6)
    delay3 = s.get("perfect_delay3", 1.0)
    look_down = s.get("look_down", 2000)
    delay4 = s.get("perfect_delay4", 0.2)
    look_up = s.get("look_up", 900)
    delay5 = s.get("perfect_delay5", 0.2)
    delay6 = s.get("perfect_delay6", 0.0)

    shake = bot.cfg.shake_box or {}
    cx = (shake.get("x1", 0) + shake.get("x2", 0)) // 2
    cy = (shake.get("y1", 0) + shake.get("y2", 0)) // 2

    try:
        if delay1 > 0 and not bot.sleep(delay1):
            return
        if zoom_out > 0:
            bot.input.scroll_zoom(zoom_out, "down", bot.running)
        if delay2 > 0 and not bot.sleep(delay2):
            return
        if zoom_in > 0:
            bot.input.scroll_zoom(zoom_in, "up", bot.running)
        if delay3 > 0 and not bot.sleep(delay3):
            return
        if look_down > 0:
            bot.input.look_action(cx, cy, look_down, "down")
        if delay4 > 0 and not bot.sleep(delay4):
            return
        if look_up > 0:
            bot.input.look_action(cx, cy, look_up, "up")
        if delay5 > 0 and not bot.sleep(delay5):
            return

        # loadout: equipment bag then fishing rod
        bot.input.send_key_press(s.get("hotbar_equipment_bag", "2"))
        time.sleep(0.05)
        bot.input.send_key_press(s.get("hotbar_fishing_rod", "1"))

        # the look drags can leave the cursor at the screen edge (on the
        # hotbar after a look-down); the cast press must land on the game view
        bot.park_cursor()
        bot.input.left_down()
        if delay6 > 0 and not bot.sleep(delay6):
            bot.input.left_up()
            return

        _perfect_cast_release(bot)

        delay8 = s.get("perfect_delay8", 0.0)
        if delay8 > 0 and not bot.sleep(delay8):
            return
        final_zoom_in = s.get("final_zoom_in", 5)
        if final_zoom_in > 0:
            bot.input.scroll_zoom(final_zoom_in, "up", bot.running)
        delay9 = s.get("perfect_delay9", 0.0)
        if delay9 > 0 and not bot.sleep(delay9):
            return
        final_look_up = s.get("final_look_up", 2000)
        if final_look_up > 0:
            bot.input.look_action(cx, cy, final_look_up, "up")
        bot.park_cursor()
        delay10 = s.get("perfect_delay10", 0.0)
        if delay10 > 0 and not bot.sleep(delay10):
            return
    except Exception as e:
        print(f"    Error in Perfect cast flow: {e}")
        try:
            bot.input.left_up()
        except Exception:
            pass


def _perfect_cast_release(bot) -> None:
    style = bot.cfg.get("perfect_cast_release_style", "Idiotproof")
    # Only Idiotproof is reconstructed here (the configured default). The other
    # styles (Prediction / GigaSigmaUltraMode / Velocity-Percentage) share this
    # tracking loop and differ only in the release-threshold formula.
    _release_idiotproof(bot)


def _release_idiotproof(bot) -> None:
    """Fill-percentage release: track the green bobber + white fill bar and
    release the mouse as the predicted fill reaches the bottom threshold."""
    s = bot.cfg
    green_tol = s.get("perfect_cast_green_color_tolerance", 15)
    white_tol = s.get("perfect_cast_white_color_tolerance", 15)
    fail_timeout = s.get("perfect_cast_fail_scan_timeout", 3.0)
    scan_fps = s.get("perfect_cast_scan_fps", 150)
    frame_delay = (1.0 / scan_fps) if scan_fps > 0 else 0.0
    release_timing = s.get("perfect_cast_release_timing", 0)
    minimum_distance = s.get("perfect_cast_minimum_distance", 25)

    shake = dict(bot.cfg.shake_box or {})
    if not shake:
        bot.input.left_up()
        return
    orig_h = shake["y2"] - shake["y1"]
    extension = int(0.5 * orig_h)
    shake["y2"] = min(shake["y2"] + extension, bot.screen_height)
    shake_width = shake["x2"] - shake["x1"]
    box_size = max(1, int(shake_width / 1500 * 20))
    box_width_multiplier = 2.0

    monitor = region_to_monitor(shake)

    is_tracking = False
    last_mid_x = None
    last_green_y = None
    frames_since_lost = 0
    last_fill = None
    last_frame_time = None
    speed_samples: list[float] = []
    max_speed_samples = 5

    # release threshold (recovered): base 95.5%, nudged by release_timing.
    if release_timing <= 0:
        release_threshold = 95.5
    else:
        release_threshold = 95.5 + (release_timing / 50.0) * 4.5

    start = time.time()
    with _mss.mss() as sct:
        while True:
            now = time.time()
            if now - start >= fail_timeout:
                print(f"    TIMEOUT ({fail_timeout}s) - releasing")
                bot.input.left_up()
                return
            if not bot.running():
                bot.input.left_up()
                return
            try:
                shot = sct.grab(monitor)
                frame = cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)
            except Exception:
                continue

            if is_tracking and last_mid_x is not None:
                green = D.find_green_tracking_box(
                    frame, last_mid_x, last_green_y, C.GREEN_BGR, green_tol,
                    box_size, box_width_multiplier,
                )
            else:
                green = D.find_green_full_scan(frame, C.GREEN_BGR, green_tol)

            if green is None:
                frames_since_lost += 1
                if frames_since_lost > 5:
                    is_tracking = False
                    last_mid_x = None
                    last_green_y = None
                if frame_delay > 0:
                    time.sleep(frame_delay)
                continue

            is_tracking = True
            frames_since_lost = 0
            mid_x, green_y, left_x, right_x = green
            last_mid_x, last_green_y = mid_x, green_y

            edges = D.find_white_below_green_both_edges(
                frame, green_y, left_x, right_x, white_tol, C.WHITE_BGR,
            )
            if edges is None:
                if frame_delay > 0:
                    time.sleep(frame_delay)
                continue
            white_top, white_bottom = edges
            total_distance = white_bottom - green_y
            current_distance = white_top - green_y
            if total_distance <= 0:
                if frame_delay > 0:
                    time.sleep(frame_delay)
                continue
            actual_fill = (1 - current_distance / total_distance) * 100.0

            # velocity estimate (%/s) over recent samples
            fill_speed = 0.0
            if last_fill is not None and last_frame_time is not None:
                dt = now - last_frame_time
                if dt > 0:
                    fill_change = actual_fill - last_fill
                    if fill_change < -50:  # bar reset; drop history
                        speed_samples.clear()
                        last_fill = None
                        last_frame_time = None
                    else:
                        speed_samples.append(fill_change / dt)
                        if len(speed_samples) > max_speed_samples:
                            speed_samples.pop(0)
            if speed_samples:
                fill_speed = sum(speed_samples) / len(speed_samples)
            last_fill = actual_fill
            last_frame_time = now

            # look-ahead offset from velocity (reconstructed, in spirit)
            base_offset = (1.5 * math.log(fill_speed / 25.0 + 1)) if fill_speed > 0 else 0.0
            predicted_fill = actual_fill + max(0.0, base_offset)

            # emergency release if white marker is right on top of green
            if current_distance <= minimum_distance:
                bot.input.left_up()
                return

            bot.overlay["cast"] = {
                "t": now, "x": shake["x1"] + mid_x, "green_y": shake["y1"] + green_y,
                "white_bottom": shake["y1"] + white_bottom, "fill": actual_fill,
                "predicted": predicted_fill, "threshold": release_threshold,
            }
            if predicted_fill >= release_threshold:
                bot.overlay["cast"]["released"] = True
                bot.input.left_up()
                return

            if frame_delay > 0:
                time.sleep(frame_delay)
