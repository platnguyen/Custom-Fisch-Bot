"""Pixel-level detection primitives.

Faithful reconstructions of the backend's module-level detection helpers
(``find_green_full_scan``, ``find_green_tracking_box``,
``find_white_below_green``, ``find_vertical_lines_in_frame``,
``calculate_speed_and_predict``) plus the colour utilities used by the fish
stages. All operate on BGR ``numpy`` frames.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore


# ---------------------------------------------------------------------------
# Colour utilities
# ---------------------------------------------------------------------------
def hex_to_bgr(hex_color: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """'#RRGGBB' -> (B, G, R); None/'none'/'' -> None (recovered)."""
    if hex_color is None:
        return None
    if hex_color.lower() in ("none", "#none", ""):
        return None
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return None
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return (b, g, r)


def detect_color_count(frame: np.ndarray, hex_color: str, tolerance: int) -> int:
    """Number of pixels within ``tolerance`` of ``hex_color`` (BGR inRange)."""
    if frame is None or frame.size == 0:
        return 0
    bgr = hex_to_bgr(hex_color)
    if bgr is None:
        return 0
    target = np.array(bgr, dtype=np.int16)
    lower = np.clip(target - tolerance, 0, 255).astype(np.uint8)
    upper = np.clip(target + tolerance, 0, 255).astype(np.uint8)
    mask = cv2.inRange(frame, lower, upper)
    return int(cv2.countNonZero(mask))


def find_white_pixel(frame: np.ndarray, tolerance: int) -> Optional[Tuple[int, int]]:
    """First near-white pixel (x, y) or None. Reconstruction of ``_find_white_pixel``."""
    if frame is None or frame.size == 0:
        return None
    lower = np.array([255 - tolerance, 255 - tolerance, 255 - tolerance])
    upper = np.array([255, 255, 255])
    mask = np.all((frame >= lower) & (frame <= upper), axis=-1)
    white = np.argwhere(mask)
    if len(white) > 0:
        y, x = white[0]
        return int(x), int(y)
    return None


# ---------------------------------------------------------------------------
# Perfect-cast bobber tracking (green marker + white fill bar)
# ---------------------------------------------------------------------------
def find_green_full_scan(frame, target_rgb, tolerance):
    """Locate the green bobber across the whole frame.

    Returns (midpoint_x, green_y, leftmost_x, rightmost_x) or None.
    """
    if frame is None or frame.size == 0:
        return None
    target = target_rgb.astype(np.int16)
    diff = np.abs(frame.astype(np.int16) - target)
    max_diff = np.max(diff, axis=2)
    mask = max_diff <= tolerance
    if not np.any(mask):
        return None
    ys, xs = np.where(mask)
    leftmost_x = int(np.min(xs))
    rightmost_x = int(np.max(xs))
    green_y = int(np.min(ys))
    midpoint_x = (leftmost_x + rightmost_x) // 2
    return midpoint_x, green_y, leftmost_x, rightmost_x


def find_green_tracking_box(
    frame, last_midpoint_x, last_green_y, target_rgb, tolerance,
    box_size, box_width_multiplier,
):
    """Locate the green bobber inside a tracking box around its last position."""
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[0], frame.shape[1]
    box_width = int(box_size * box_width_multiplier)
    box_x1 = max(0, last_midpoint_x - box_width)
    box_x2 = min(w, last_midpoint_x + box_width)
    box_y1 = max(0, last_green_y - box_size)
    box_y2 = min(h, last_green_y + box_size)
    if box_x1 >= box_x2 or box_y1 >= box_y2:
        return None
    tracking = frame[box_y1:box_y2, box_x1:box_x2]
    if tracking.size == 0:
        return None
    target = target_rgb.astype(np.int16)
    diff = np.abs(tracking.astype(np.int16) - target)
    max_diff = np.max(diff, axis=2)
    mask = max_diff <= tolerance
    if not np.any(mask):
        return None
    ys, xs = np.where(mask)
    leftmost = int(np.min(xs))
    rightmost = int(np.max(xs))
    green_y = int(np.min(ys))
    midpoint = (leftmost + rightmost) // 2
    return (
        midpoint + box_x1,
        green_y + box_y1,
        leftmost + box_x1,
        rightmost + box_x1,
    )


def find_white_below_green(frame, local_green_y, local_left_x, local_right_x,
                           white_tolerance, white_bgr):
    """Closest near-white pixel below the green marker, or None."""
    if frame is None or frame.size == 0:
        return None
    scan_start_y = max(0, local_green_y)
    scan_end_y = frame.shape[0]
    if scan_start_y >= scan_end_y:
        return None
    scan_left_x = max(0, local_left_x)
    scan_right_x = min(frame.shape[1], local_right_x + 1)
    if scan_left_x >= scan_right_x:
        return None
    region = frame[scan_start_y:scan_end_y, scan_left_x:scan_right_x]
    if region.size == 0:
        return None
    target = white_bgr.astype(np.int16)
    diff = np.abs(region.astype(np.int16) - target)
    max_diff = np.max(diff, axis=2)
    white_mask = max_diff <= white_tolerance
    if not np.any(white_mask):
        return None
    ys, xs = np.where(white_mask)
    closest = int(np.argmin(ys))
    return (scan_left_x + int(xs[closest]), scan_start_y + int(ys[closest]))


# ---------------------------------------------------------------------------
# Vertical-line detection (Line fish-track mode)
# ---------------------------------------------------------------------------
def find_white_below_green_both_edges(frame, local_green_y, local_left_x,
                                      local_right_x, white_tolerance, white_bgr):
    """Top and bottom near-white y below the green marker, or None.

    Reconstruction of ``find_white_below_green_both_edges``; returns
    ``(white_y_top, white_y_bottom)`` in frame coordinates.
    """
    if frame is None or frame.size == 0:
        return None
    scan_start_y = max(0, local_green_y)
    scan_end_y = frame.shape[0]
    if scan_start_y >= scan_end_y:
        return None
    scan_left_x = max(0, local_left_x)
    scan_right_x = min(frame.shape[1], local_right_x + 1)
    if scan_left_x >= scan_right_x:
        return None
    region = frame[scan_start_y:scan_end_y, scan_left_x:scan_right_x]
    if region.size == 0:
        return None
    target = white_bgr.astype(np.int16)
    diff = np.abs(region.astype(np.int16) - target)
    max_diff = np.max(diff, axis=2)
    white_mask = max_diff <= white_tolerance
    if not np.any(white_mask):
        return None
    ys = np.where(white_mask)[0]
    white_y_top = int(np.min(ys)) + scan_start_y
    white_y_bottom = int(np.max(ys)) + scan_start_y
    return white_y_top, white_y_bottom


def find_vertical_lines_in_frame(frame, expected_count=3, min_sep=10) -> List[int]:
    """Detect strong vertical edges (target line + bar edges).

    Column-difference energy -> percentile thresholding -> non-maximum
    suppression with a minimum separation. Reconstruction of
    ``find_vertical_lines_in_frame``; returns sorted x positions.
    """
    if frame is None or frame.size == 0:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    w = gray.shape[1]
    diffs = np.zeros(w, dtype=np.float32)
    for x in range(1, w):
        diffs[x] = np.abs(
            gray[:, x].astype(np.int16) - gray[:, x - 1].astype(np.int16)
        ).sum()
    if w == 0:
        return []

    best_lines: List[int] = []
    best_score = -999.0
    for percentile in range(95, 50, -5):
        threshold = np.percentile(diffs, percentile)
        candidates = [
            x for x in range(1, w - 1)
            if diffs[x] > threshold
            and diffs[x] >= diffs[x - 1]
            and diffs[x] >= diffs[x + 1]
        ]
        candidates.sort(key=lambda x: diffs[x], reverse=True)
        lines: List[int] = []
        for x in candidates:
            if all(abs(x - c) >= min_sep for c in lines):
                lines.append(x)
            if len(lines) >= expected_count + 1:
                break
        if len(lines) == expected_count:
            return sorted(lines)
        score = -abs(len(lines) - expected_count) * 10 + len(lines)
        if score > best_score and len(lines) >= expected_count - 1:
            best_score = score
            best_lines = lines
    return sorted(best_lines[:expected_count]) if best_lines else []


# ---------------------------------------------------------------------------
# Velocity prediction (least-squares slope of white marker position)
# ---------------------------------------------------------------------------
def calculate_speed_and_predict(white_positions, timestamps) -> Optional[float]:
    """Linear-regression velocity (px/s) of the marker; None if degenerate."""
    if len(white_positions) < 2:
        return None
    n = len(white_positions)
    y_values = [pos for pos in white_positions]
    t0 = timestamps[0]
    time_values = [t - t0 for t in timestamps]
    mean_t = sum(time_values) / n
    mean_y = sum(y_values) / n
    numerator = sum((t - mean_t) * (y - mean_y) for t, y in zip(time_values, y_values))
    denominator = sum((t - mean_t) ** 2 for t in time_values)
    if abs(denominator) < 0.0001:
        return None
    return numerator / denominator
