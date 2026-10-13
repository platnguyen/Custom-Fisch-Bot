"""hydra_bot - a clean-room Python rewrite of the core Hydra fishing-macro.

Reconstructed from the decompiled ``HydraBackend.exe`` (PyInstaller / Python
3.14). This package implements the *core* bot only: the capture -> detect ->
cast -> shake -> reel -> totem/potion loop and the localhost HTTP control API.
The cosmetic Tk GUI (starfield menu, shop/rebirth meta-game, overlays) is
deliberately omitted.
"""
from .bot import HydraBot
from .config import Config

__all__ = ["HydraBot", "Config"]
__version__ = "0.1.0"
