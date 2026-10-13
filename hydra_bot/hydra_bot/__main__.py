"""Entry point: ``python -m hydra_bot [--headless] [--config PATH] [--port N]``.

Mirrors the original backend's ``--headless`` mode: start the HTTP API server
and run the bot loop, driven entirely by the API (or by --autostart).
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import deque

from . import constants as C
from .win_input import set_dpi_aware
from .bot import HydraBot
from .api import start_api_server


class LogBuffer:
    """Ring buffer of recent log lines, tee'd from stdout for the /logs API."""

    def __init__(self, maxlen=500):
        self._buf = deque(maxlen=maxlen)
        self._stdout = sys.stdout

    def write(self, text):
        self._stdout.write(text)
        for line in text.splitlines():
            if line:
                self._buf.append(line)

    def flush(self):
        self._stdout.flush()

    def tail(self, n=200):
        return list(self._buf)[-n:]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hydra_bot")
    parser.add_argument("--headless", action="store_true",
                        help="run API server + bot loop with no GUI (default)")
    parser.add_argument("--config", default="config.json", help="config.json path")
    parser.add_argument("--port", type=int, default=C.HYDRA_API_PORT)
    parser.add_argument("--autostart", action="store_true",
                        help="begin the fishing loop immediately")
    args = parser.parse_args(argv)
    set_dpi_aware()  # before any capture/cursor call, like the GUI

    log_buffer = LogBuffer()
    sys.stdout = log_buffer

    print(" Starting Hydra (core rewrite)...")
    print(f"Python version: {sys.version.split()[0]}")

    bot = HydraBot(args.config)
    if bot.cfg.loaded_from:
        print(f" Config loaded from {bot.cfg.loaded_from}")
    print(f" Loaded {len(bot.cfg.settings)} settings")

    start_api_server(bot, log_buffer, port=args.port)

    if args.autostart:
        bot.start()

    print(" Headless backend running. Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        bot.quit()
        print(" Hydra closed")


if __name__ == "__main__":
    main()
