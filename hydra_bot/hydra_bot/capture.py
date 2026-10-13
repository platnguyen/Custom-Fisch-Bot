"""Screen capture abstraction (MSS and DXcam backends).

The original selects the backend from ``shake_capture_mode`` ("MSS" vs
"DXCAM"). Both ultimately yield a BGR ``numpy`` frame of a screen region
``{x1,y1,x2,y2}``. This wrapper hides the per-backend plumbing (BGRA->BGR
conversion for MSS, camera lifecycle for DXcam).
"""
from __future__ import annotations

import time
from typing import Dict, Optional

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore

try:
    import mss as _mss
except Exception:  # pragma: no cover
    _mss = None  # type: ignore

try:
    import dxcam as _dxcam
    DXCAM_AVAILABLE = True
except Exception:  # pragma: no cover
    _dxcam = None
    DXCAM_AVAILABLE = False


def region_to_monitor(box: Dict[str, int]) -> Dict[str, int]:
    """Convert a {x1,y1,x2,y2} box to an MSS monitor dict."""
    return {
        "top": int(box["y1"]),
        "left": int(box["x1"]),
        "width": int(box["x2"]) - int(box["x1"]),
        "height": int(box["y2"]) - int(box["y1"]),
    }


class Capture:
    """One capture session over a fixed region."""

    def __init__(self, box: Dict[str, int], use_mss: bool = True):
        self.box = box
        self.use_mss = use_mss or not DXCAM_AVAILABLE
        self.monitor = region_to_monitor(box)
        self._sct = None
        self._camera = None
        if self.use_mss:
            self._sct = _mss.mss()
        else:
            self._camera = self._create_dxcam()

    # -- dxcam lifecycle (mirrors retry logic in the original) -----------
    def _create_dxcam(self, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                cam = _dxcam.create(output_idx=0, output_color="BGR")
                if cam is not None:
                    return cam
            except Exception as e:
                print(f"    DXCam error (attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(0.1)
        print("     ERROR: Failed to create dxcam camera after retries")
        self.use_mss = True
        self._sct = _mss.mss()
        return None

    def grab(self) -> Optional[np.ndarray]:
        """Return a BGR frame of the region, or None on transient failure."""
        if self.use_mss:
            shot = self._sct.grab(self.monitor)
            return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)
        region = (
            self.box["x1"],
            self.box["y1"],
            self.box["x2"],
            self.box["y2"],
        )
        return self._camera.grab(region=region)

    def close(self) -> None:
        try:
            if self._camera is not None:
                self._camera.release()
        except Exception:
            pass
        try:
            if self._sct is not None:
                self._sct.close()
        except Exception:
            pass


def grab_box_once(box: Dict[str, int]) -> Optional[np.ndarray]:
    """One-shot MSS grab of a region (used by exit/day-night checks)."""
    with _mss.mss() as sct:
        shot = sct.grab(region_to_monitor(box))
        return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)


def screen_size() -> tuple[int, int]:
    with _mss.mss() as sct:
        mon = sct.monitors[1]
        return mon["width"], mon["height"]
