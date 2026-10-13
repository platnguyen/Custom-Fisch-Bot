"""Shake stage (Pixel mode) + the "fish hooked" exit check.

Reconstruction of ``_execute_shake_pixel_mode`` and
``_check_shake_exit_condition``. Returns one of:

* ``"fish_stage"`` - fish detected, proceed to reeling
* ``"restart"``    - timed out with no shake prompt, restart the cycle
* ``None``         - stopped / inconclusive

The Circle and Navigation shake styles from the original are not reconstructed
here; Pixel is the default and the most used.
"""
from __future__ import annotations

import time
from typing import Optional

import numpy as np

from .. import constants as C
from .. import detection as D
from ..capture import Capture, grab_box_once, screen_size

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def execute_shake(bot) -> Optional[str]:
    style = bot.cfg.get("shake_style", "Pixel")
    if style == "Disabled":
        return None
    if style == "Pixel":
        return _shake_pixel(bot)
    # Circle / Navigation omitted in the core rewrite.
    print(f"[shake] style {style!r} not implemented in core rewrite; using Pixel")
    return _shake_pixel(bot)


def _shake_pixel(bot) -> Optional[str]:
    s = bot.cfg
    click_count = s.get("shake_click_count", 1)
    color_tolerance = s.get("shake_color_tolerance", 0)
    pixel_tol_base = s.get("shake_pixel_distance_tolerance", 0)
    scan_fps = s.get("shake_scan_fps", 60)
    duplicate_timeout = s.get("shake_duplicate_timeout", 0.3)
    fail_cast_timeout = s.get("shake_fail_cast_timeout", 15)
    scan_delay = (1.0 / scan_fps) if scan_fps > 0 else 0.005

    box = bot.cfg.shake_box
    if not box:
        return "restart"
    shake_width = box["x2"] - box["x1"]
    pixel_distance_tolerance = pixel_tol_base / 1500.0 * shake_width

    use_mss = bot.cfg.get("shake_capture_mode", "MSS") == "MSS"
    cam = Capture(box, use_mss=use_mss)

    last_pixel = None
    duplicate_timer = 0.0
    no_pixel_timer = 0.0
    last_scan = time.time()
    start = time.time()
    GRACE = 2.0

    try:
        while bot.running():
            now = time.time()
            frame = None
            try:
                frame = cam.grab()
            except Exception as e:
                print(f"    Frame capture error: {e}")
                time.sleep(0.05)
                continue
            if frame is None:
                time.sleep(scan_delay)
                continue

            white = D.find_white_pixel(frame, color_tolerance)
            if white is not None:
                px, py = white
                screen_x = box["x1"] + px
                screen_y = box["y1"] + py

                is_duplicate = False
                if last_pixel is not None:
                    dist = ((screen_x - last_pixel[0]) ** 2
                            + (screen_y - last_pixel[1]) ** 2) ** 0.5
                    is_duplicate = dist <= pixel_distance_tolerance

                if is_duplicate:
                    duplicate_timer += now - last_scan
                    if duplicate_timer >= duplicate_timeout:
                        bot.input.click_at(screen_x, screen_y, click_count)
                        duplicate_timer = 0.0
                        last_pixel = None
                else:
                    bot.input.click_at(screen_x, screen_y, click_count)
                    last_pixel = (screen_x, screen_y)
                    duplicate_timer = 0.0
                no_pixel_timer = 0.0
            else:
                no_pixel_timer += now - last_scan
                duplicate_timer = 0.0
                last_pixel = None

                # after the grace period, poll the hooked-fish exit probe
                if now - start >= GRACE:
                    if _check_shake_exit_condition(bot) == "fish_stage":
                        return "fish_stage"

                if no_pixel_timer >= fail_cast_timeout:
                    print(f"     No shake prompt for {fail_cast_timeout}s - restarting")
                    return "restart"

            last_scan = now
            time.sleep(scan_delay)
        return None
    finally:
        cam.close()


def _check_shake_exit_condition(bot) -> Optional[str]:
    """Detect the bottom-left green 'reeling' bar; its disappearance (or
    presence per the original's history logic) signals the fish is hooked."""
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
    matching = cv2.countNonZero(mask)

    hist = bot._shake_green_history
    hist.append(matching)
    if len(hist) > 5:
        hist.pop(0)
    # Original: once the green reel bar has been seen and then reads 0, the
    # fishing minigame has begun.
    if matching == 0 and any(h > 0 for h in hist[:-1]):
        return "fish_stage"
    return None
