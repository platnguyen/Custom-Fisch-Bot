"""Shared constants, recovered 1:1 from the original backend.

All Win32 event flags and timing constants here match the values decompiled
from ``HydraBackend.exe`` (PyInstaller / Python 3.14).
"""
from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# Win32 mouse_event flags (winuser.h)
# ---------------------------------------------------------------------------
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800  # original passes the literal 2048
WHEEL_DELTA = 120

# ---------------------------------------------------------------------------
# Timing (seconds) — recovered module-level constants
# ---------------------------------------------------------------------------
INTERRUPTIBLE_SLEEP_CHECK_INTERVAL = 0.05
SCROLL_ZOOM_STEP_DELAY = 0.05
CAMERA_CLEANUP_DELAY = 0.025
MOUSE_SMOOTH_DELAY = 0.0005
LOOK_ACTION_DELAY = 0.01
BOT_CYCLE_CHECK_INTERVAL = 0.05  # original schedules 50 ms via tk.after

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
HYDRA_API_PORT = 9760

# ---------------------------------------------------------------------------
# Reference colours (BGR), as used by the perfect-cast solver
# ---------------------------------------------------------------------------
GREEN_BGR = np.array([76, 160, 100], dtype=np.float32)
WHITE_BGR = np.array([223, 234, 234], dtype=np.float32)

# Bottom-left "fish hooked" probe colour used by the shake exit check.
SHAKE_EXIT_GREEN_BGR = np.array([155, 255, 155], dtype=np.int16)

# Keys that can be bound as hotkeys (a-z, 0-9, F1-F12).
AVAILABLE_HOTKEYS = (
    [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [str(d) for d in range(10)]
    + [f"F{i}" for i in range(1, 13)]
)

YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@repetitives1?sub_confirmation=1"
