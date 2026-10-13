"""Higher-level perception for the fish/totem stages.

* :func:`detect_fish_colors_in_frame` finds the target line and bar edges in
  the fishing minigame frame, by vertical-line detection (Line mode) or colour
  masking (Color mode). Reconstruction of ``_detect_fish_colors_in_frame``.
* :class:`YoloFishDetector` wraps an Ultralytics model that emits ``icon`` and
  ``bar`` boxes. Reconstruction of ``_load_yolo_fish_model`` / ``_yolo_detect``.
* :func:`detect_day_or_night` template-matches the sun/moon icons for the totem
  stage. Reconstruction of ``_detect_day_or_night`` / ``_load_totem_templates``.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, Optional

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore

from . import detection as D


def _resource_dir(subfolder: str) -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        # one level up from the package so "YOLO rods" / "Detection" sit
        # beside the original backend layout.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, subfolder)


def detect_fish_colors_in_frame(
    frame,
    *,
    fish_track_style: str,
    use_line_detection: bool,
    target_line_color: Optional[str],
    left_bar_color: Optional[str],
    right_bar_color: Optional[str],
    arrow_color: Optional[str],
    target_line_tolerance: int,
    left_bar_tolerance: int,
    right_bar_tolerance: int,
    arrow_tolerance: int,
    skip_arrow_scan: bool = False,
) -> Dict[str, Optional[float]]:
    """Locate target line / bar edges / arrow in the minigame frame.

    Returns a dict with keys ``target_line_x``, ``target_left_x``,
    ``target_right_x``, ``target_middle_x``, ``bar_left_x``, ``bar_right_x``,
    ``arrow_center_x`` (any may be None).
    """
    result: Dict[str, Optional[float]] = {
        "target_line_x": None,
        "target_left_x": None,
        "target_right_x": None,
        "target_middle_x": None,
        "bar_left_x": None,
        "bar_right_x": None,
        "arrow_center_x": None,
    }
    if frame is None or frame.size == 0:
        return result

    # --- Line mode: detect vertical edges and infer positions ----------
    if use_line_detection and fish_track_style != "Color":
        expected_lines = 4
        lines = D.find_vertical_lines_in_frame(
            frame, expected_count=expected_lines, min_sep=10
        )
        if len(lines) >= 2:
            if len(lines) == 2:
                result["target_left_x"] = lines[0]
                result["target_right_x"] = lines[1]
                result["target_middle_x"] = (lines[0] + lines[1]) // 2
                result["target_line_x"] = result["target_middle_x"]
                result["bar_left_x"] = lines[0]
                result["bar_right_x"] = lines[1]
                return result
            if len(lines) == 3:
                result["target_left_x"] = lines[0]
                result["target_right_x"] = lines[1]
                result["target_middle_x"] = (lines[0] + lines[1]) // 2
                result["target_line_x"] = result["target_middle_x"]
                bar_x = lines[2]
                if bar_x < result["target_middle_x"]:
                    result["bar_left_x"] = bar_x
                else:
                    result["bar_right_x"] = bar_x
                return result
            # 4+ lines: outer two are the target band.
            result["target_left_x"] = lines[0]
            result["target_right_x"] = lines[-1]
            result["target_middle_x"] = (lines[0] + lines[-1]) // 2
            result["target_line_x"] = result["target_middle_x"]

    # --- Color mode: mask each configured colour -----------------------
    def find_color_x_coords(hex_color, tolerance):
        bgr = D.hex_to_bgr(hex_color)
        if bgr is None:
            return None
        target = np.array(bgr, dtype=np.int16)
        lower = np.clip(target - tolerance, 0, 255).astype(np.uint8)
        upper = np.clip(target + tolerance, 0, 255).astype(np.uint8)
        mask = cv2.inRange(frame, lower, upper)
        xs = np.where(mask > 0)[1]
        return xs if xs.size else None

    if result["target_line_x"] is None:
        xs = find_color_x_coords(target_line_color, target_line_tolerance)
        if xs is not None:
            result["target_left_x"] = int(np.min(xs))
            result["target_right_x"] = int(np.max(xs))
            result["target_middle_x"] = int(
                (result["target_left_x"] + result["target_right_x"]) / 2
            )
            result["target_line_x"] = result["target_middle_x"]

    if result["bar_left_x"] is None and result["bar_right_x"] is None:
        bar_xs = None
        left_xs = find_color_x_coords(left_bar_color, left_bar_tolerance)
        if left_xs is not None:
            bar_xs = left_xs
        right_xs = find_color_x_coords(right_bar_color, right_bar_tolerance)
        if right_xs is not None:
            bar_xs = right_xs if bar_xs is None else np.concatenate([bar_xs, right_xs])
        if bar_xs is not None:
            result["bar_left_x"] = int(np.min(bar_xs))
            result["bar_right_x"] = int(np.max(bar_xs))

    if not skip_arrow_scan and arrow_color:
        arrow_xs = find_color_x_coords(arrow_color, arrow_tolerance)
        if arrow_xs is not None:
            result["arrow_center_x"] = float(np.mean(arrow_xs))

    return result


class YoloFishDetector:
    """Ultralytics YOLO detector emitting icon/bar x-coordinates."""

    def __init__(self, model_filename: str):
        from ultralytics import YOLO  # raises if not installed
        import warnings

        model_path = os.path.join(_resource_dir("YOLO rods"), model_filename)
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"YOLO model not found: {model_path} "
                "(place .pt files in the 'YOLO rods' folder)"
            )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.model = YOLO(model_path, verbose=False)
        self.name = model_filename

    def detect(self, frame, confidence: float):
        """Return (icon_center_x, bar_left_x, bar_right_x, bar_center_x)."""
        icon_center_x = bar_left_x = bar_right_x = bar_center_x = None
        results = self.model.predict(frame, conf=confidence, verbose=False)
        if results and len(results[0].boxes) > 0:
            names = results[0].names
            for box in results[0].boxes:
                cls_id = int(box.cls[0])
                class_name = names[cls_id].lower()
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                if class_name == "icon":
                    icon_center_x = int((x1 + x2) / 2)
                elif class_name == "bar":
                    bar_left_x = int(x1)
                    bar_right_x = int(x2)
                    bar_center_x = (bar_left_x + bar_right_x) // 2
        return icon_center_x, bar_left_x, bar_right_x, bar_center_x


def _load_totem_templates():
    base = _resource_dir("Detection")
    sun = moon = None
    sun_path = os.path.join(base, "sun.png")
    moon_path = os.path.join(base, "moon.png")
    if os.path.exists(sun_path):
        sun = cv2.imread(sun_path, cv2.IMREAD_COLOR)
    if os.path.exists(moon_path):
        moon = cv2.imread(moon_path, cv2.IMREAD_COLOR)
    return sun, moon


def detect_day_or_night(totem_box: Dict[str, int], sun_tmpl, moon_tmpl) -> str:
    """'day' | 'night' | 'unknown' via template matching (threshold 0.65)."""
    from .capture import grab_box_once

    if not totem_box:
        return "unknown"
    frame = grab_box_once(totem_box)
    if frame is None:
        return "unknown"
    THRESHOLD = 0.65
    sun_score = moon_score = -1.0
    bh, bw = frame.shape[0], frame.shape[1]
    if sun_tmpl is not None:
        tmpl = cv2.resize(sun_tmpl, (bw, bh), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
        sun_score = float(res.max())
    if moon_tmpl is not None:
        tmpl = cv2.resize(moon_tmpl, (bw, bh), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
        moon_score = float(res.max())
    if sun_score >= THRESHOLD and sun_score > moon_score:
        return "day"
    if moon_score >= THRESHOLD and moon_score > sun_score:
        return "night"
    return "unknown"
