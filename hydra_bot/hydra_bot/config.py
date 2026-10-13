"""Configuration loading / saving.

Mirrors the original backend's config handling:

* The on-disk file is ``config.json`` with top-level keys ``settings``,
  ``global_gui_settings``, ``hotkeys``, ``fish_box``, ``shake_box``,
  ``totem_box``, ``exit_box``, ``bottom_button_states`` plus a couple of flags.
* ``settings`` holds ~490 flat keys. Many support *per-rod overrides* of the
  form ``<base_key>_<RodName>`` (e.g. ``fish_kp_Tryhard``); lookups fall back
  to the base key, then to a caller default.
* On load the original searches several well-known locations. We keep the same
  search order but default the primary location to the current directory.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.json")

# Search order recovered from ``_load_config``.
def _search_locations(primary: str) -> list[str]:
    home = os.path.expanduser("~")
    return [
        primary,
        os.path.join(os.getenv("APPDATA", home), "Hydra", "config.json"),
        os.path.join(home, "Documents", "Hydra", "config.json"),
        os.path.join(os.getenv("TEMP", "/tmp"), "Hydra", "config.json"),
        os.path.join(home, "Desktop", "Hydra_config.json"),
    ]


class Config:
    """Thread-safe view over the Hydra configuration document."""

    def __init__(self, path: str = "config.json"):
        self.path = path
        self._lock = threading.RLock()
        self._doc: Dict[str, Any] = self._load_defaults()
        self.loaded_from: Optional[str] = None

    # -- loading ----------------------------------------------------------
    @staticmethod
    def _load_defaults() -> Dict[str, Any]:
        with open(_DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def load(self) -> bool:
        """Load the first existing config file; defaults fill any gaps."""
        with self._lock:
            doc = self._load_defaults()
            for location in _search_locations(self.path):
                if os.path.exists(location):
                    try:
                        with open(location, "r", encoding="utf-8") as fh:
                            on_disk = json.load(fh)
                    except (OSError, ValueError):
                        continue
                    if not isinstance(on_disk, dict):
                        continue
                    # Merge: settings and dict sections update defaults.
                    if isinstance(on_disk.get("settings"), dict):
                        doc["settings"].update(on_disk["settings"])
                    for key in (
                        "global_gui_settings",
                        "hotkeys",
                        "bottom_button_states",
                    ):
                        if isinstance(on_disk.get(key), dict):
                            doc.setdefault(key, {}).update(on_disk[key])
                    for key in ("fish_box", "shake_box", "totem_box", "exit_box"):
                        if key in on_disk:
                            doc[key] = on_disk[key]
                    for key in ("tos_accepted", "subscribed", "webhook_url"):
                        if key in on_disk:
                            doc[key] = on_disk[key]
                    self.path = location
                    self.loaded_from = location
                    self._doc = doc
                    return True
            self._doc = doc
            return False

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._doc, fh, indent=2)
            os.replace(tmp, self.path)

    # -- access -----------------------------------------------------------
    @property
    def settings(self) -> Dict[str, Any]:
        return self._doc["settings"]

    @property
    def document(self) -> Dict[str, Any]:
        return self._doc

    def get(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.settings[key] = value

    # -- rod-specific overrides ------------------------------------------
    def current_rod(self) -> str:
        return self.settings.get("fish_rod_type", "Default")

    def rod_setting(self, base_key: str, default: Any = None) -> Any:
        """``settings[base_key_<rod>]`` -> ``settings[base_key]`` -> default.

        Reconstruction of ``_get_rod_specific_setting``.
        """
        rod = self.current_rod()
        s = self.settings
        if f"{base_key}_{rod}" in s:
            return s[f"{base_key}_{rod}"]
        return s.get(base_key, default)

    def rod_color(self, base_key: str) -> Optional[str]:
        return self.rod_setting(base_key)

    def rod_tolerance(self, base_key: str, default: int = 10) -> int:
        return int(self.rod_setting(base_key, default))

    # -- detection boxes --------------------------------------------------
    def box(self, name: str) -> Optional[Dict[str, int]]:
        return self._doc.get(name)

    @property
    def fish_box(self):
        return self._doc.get("fish_box")

    @property
    def shake_box(self):
        return self._doc.get("shake_box")

    @property
    def totem_box(self):
        return self._doc.get("totem_box")

    @property
    def exit_box(self):
        return self._doc.get("exit_box")

    @property
    def bottom_button_states(self) -> Dict[str, bool]:
        return self._doc.setdefault("bottom_button_states", {})

    @property
    def global_gui_settings(self) -> Dict[str, Any]:
        return self._doc.setdefault("global_gui_settings", {})

    @property
    def hotkeys(self) -> Dict[str, str]:
        return self._doc.setdefault("hotkeys", {})
