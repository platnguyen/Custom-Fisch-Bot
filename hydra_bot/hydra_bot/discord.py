"""Discord webhook notifications.

Everything is sent from one background worker so the bot loop never waits on
the network. Settings are read when each message is sent, so GUI changes apply
straight away:

* ``discord_enabled`` / ``webhook_url``
* ``discord_message_style``  "Screenshot" attaches a screen grab, "Text" doesn't
* ``loops_screenshot`` / ``loops_text``  report every N loops (for the chosen
  style; 0 = never)
* ``discord_notify_start_stop``, ``discord_notify_errors``
* ``discord_status_interval_minutes``  periodic status while running (0 = off)
* ``discord_ping_user_id``  user to @mention on errors (blank = nobody)

Only the standard library (+ the mss/OpenCV already used for capture) is
needed.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future
from typing import Optional

try:
    import mss as _mss
    import numpy as np
    import cv2
except Exception:  # pragma: no cover
    _mss = np = cv2 = None

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?$")

COLOR_INFO = 0x5B8DEF   # GUI accent
COLOR_GOOD = 0x3FCF7F
COLOR_WARN = 0xE7B844
COLOR_BAD = 0xEF4B55

MAX_QUEUE = 20
MAX_RETRIES = 3
SCREENSHOT_MAX_WIDTH = 1920
ERROR_COOLDOWN = 60.0  # a crash that repeats every cycle is reported once a minute


def valid_webhook_url(url: str) -> bool:
    return bool(url) and WEBHOOK_RE.match(url.strip()) is not None


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _grab_screenshot() -> Optional[bytes]:
    """JPEG of the primary monitor (small enough for any webhook limit)."""
    if _mss is None:
        return None
    with _mss.mss() as sct:
        frame = np.asarray(sct.grab(sct.monitors[2]))
    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    h, w = frame.shape[:2]
    if w > SCREENSHOT_MAX_WIDTH:
        frame = cv2.resize(frame, (SCREENSHOT_MAX_WIDTH, int(h * SCREENSHOT_MAX_WIDTH / w)),
                           interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def _multipart(payload: dict, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="payload_json"\r\n'
        "Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload).encode() + b"\r\n",
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
        "Content-Type: image/jpeg\r\n\r\n".encode() + data + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def post_webhook(url: str, payload: dict, screenshot: Optional[bytes] = None,
                 timeout: float = 15.0) -> tuple[bool, str]:
    """Send one message, retrying on rate limits. Returns (ok, detail)."""
    if screenshot:
        payload = dict(payload)
        embeds = [dict(e) for e in payload.get("embeds", [])]
        if embeds:
            embeds[0]["image"] = {"url": "attachment://screenshot.jpg"}
            payload["embeds"] = embeds
        body, ctype = _multipart(payload, "screenshot.jpg", screenshot)
    else:
        body, ctype = json.dumps(payload).encode(), "application/json"
    headers = {
        "Content-Type": ctype,
        # Discord's edge rejects the default Python-urllib agent
        "User-Agent": "hydra_bot/1.0 (Discord webhook)",
    }
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url.strip(), data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                try:
                    wait = float(json.loads(e.read() or b"{}").get("retry_after", 1.0))
                except (ValueError, AttributeError):
                    wait = 1.0
                time.sleep(min(max(wait, 0.5), 30.0))
                continue
            if e.code in (401, 404):
                return False, f"HTTP {e.code}: webhook deleted or URL wrong"
            detail = ""
            try:
                detail = json.loads(e.read() or b"{}").get("message", "")
            except (ValueError, AttributeError):
                pass
            return False, f"HTTP {e.code}{': ' + detail if detail else ''}"
        except (urllib.error.URLError, OSError) as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            return False, f"network error: {getattr(e, 'reason', e)}"
    return False, "rate limited"


class DiscordNotifier:
    """Queues bot events and posts them to the configured webhook."""

    def __init__(self, bot):
        self.bot = bot
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=MAX_QUEUE)
        self._closed = False
        self._last_error_sent = 0.0
        self._last_status = 0.0
        self._pending = 0  # queued + in-flight messages
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._run, name="discord", daemon=True)
        self._thread.start()

    # -- settings ----------------------------------------------------------
    def _get(self, key, default=None):
        return self.bot.cfg.get(key, default)

    def _url(self) -> str:
        return str(self._get("webhook_url", "") or "").strip()

    def enabled(self) -> bool:
        return bool(self._get("discord_enabled", False)) and valid_webhook_url(self._url())

    def _wants_screenshot(self) -> bool:
        return self._get("discord_message_style", "Screenshot") == "Screenshot"

    def _every_n_loops(self) -> int:
        key = "loops_screenshot" if self._wants_screenshot() else "loops_text"
        try:
            return max(0, int(self._get(key, 10)))
        except (TypeError, ValueError):
            return 0

    # -- events (called from the bot / GUI threads; never block) -----------
    def on_start(self) -> None:
        self._last_status = time.time()
        if self._get("discord_notify_start_stop", True):
            self._enqueue("Macro started", COLOR_GOOD)

    def on_stop(self) -> None:
        if self._get("discord_notify_start_stop", True):
            self._enqueue("Macro stopped", COLOR_WARN)

    def on_loop(self, loop_count: int) -> None:
        n = self._every_n_loops()
        if n and loop_count % n == 0:
            self._enqueue(f"{loop_count} loops completed", COLOR_INFO)

    def on_error(self, message: str) -> None:
        if not self._get("discord_notify_errors", True):
            return
        now = time.time()
        if now - self._last_error_sent < ERROR_COOLDOWN:
            return
        self._last_error_sent = now
        self._enqueue("Error", COLOR_BAD, description=f"```{message[:1500]}```", ping=True)

    def test(self) -> Future:
        """Send a test message now (even if disabled). Resolves to (ok, detail)."""
        fut: Future = Future()
        if not valid_webhook_url(self._url()):
            fut.set_result((False, "Not a Discord webhook URL"))
            return fut
        self._enqueue("Test message", COLOR_INFO, description="Webhook is set up correctly.",
                      future=fut, force=True)
        return fut

    def close(self, timeout: float = 3.0) -> None:
        """Wait (briefly) for queued messages, then stop the worker."""
        self._closed = True
        with self._cv:
            self._cv.wait_for(lambda: self._pending == 0, timeout)

    # -- internals -----------------------------------------------------------
    def _enqueue(self, title, color, description="", ping=False, future=None,
                 force=False) -> None:
        if self._closed or not (force or self.enabled()):
            if future is not None and not future.done():
                future.set_result((False, "Notifier closed"))
            return
        with self._cv:
            self._pending += 1
        try:
            self._queue.put_nowait((title, color, description, ping, future, time.time()))
        except queue.Full:
            print("[Discord] queue full - dropping message")
            self._done()
            if future is not None:
                future.set_result((False, "Too many queued messages"))

    def _done(self) -> None:
        with self._cv:
            self._pending -= 1
            self._cv.notify_all()

    def _stats_fields(self) -> list[dict]:
        bot = self.bot
        fields = [
            {"name": "Rod", "value": str(bot.cfg.current_rod()), "inline": True},
            {"name": "Loops", "value": str(bot.loop_count), "inline": True},
            {"name": "Caught", "value": str(getattr(bot, "caught_count", 0)), "inline": True},
            {"name": "Cycles", "value": str(bot._total_cycle_count), "inline": True},
        ]
        started = getattr(bot, "started_at", None)
        if started:
            elapsed = time.time() - started
            fields.append({"name": "Runtime", "value": _fmt_duration(elapsed), "inline": True})
            if elapsed >= 60:
                rate = getattr(bot, "caught_count", 0) / (elapsed / 3600.0)
                fields.append({"name": "Fish / hour", "value": f"{rate:.0f}", "inline": True})
        status = bot.status_text + (f" — {bot.status_sub}" if bot.status_sub else "")
        fields.append({"name": "Status", "value": status[:1000] or "-", "inline": False})
        return fields

    def _build(self, title, color, description, ping, queued_at) -> dict:
        embed = {
            "title": title,
            "color": color,
            "fields": self._stats_fields(),
            "footer": {"text": "Hydra"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(queued_at)),
        }
        if description:
            embed["description"] = description
        payload = {"username": "Hydra", "embeds": [embed],
                   "allowed_mentions": {"parse": []}}
        user_id = str(self._get("discord_ping_user_id", "") or "").strip()
        if ping and user_id.isdigit():
            payload["content"] = f"<@{user_id}>"
            payload["allowed_mentions"] = {"users": [user_id]}
        return payload

    def _maybe_status(self) -> None:
        try:
            minutes = float(self._get("discord_status_interval_minutes", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0 or not self.bot.is_running:
            return
        if time.time() - self._last_status >= minutes * 60:
            self._last_status = time.time()
            self._enqueue("Status update", COLOR_INFO)

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                if not self._closed:
                    self._maybe_status()
                continue
            title, color, description, ping, future, queued_at = item
            shot = None
            if self._wants_screenshot():
                try:
                    shot = _grab_screenshot()
                except Exception as e:
                    print(f"[Discord] screenshot failed ({e}); sending text only")
            try:
                ok, detail = post_webhook(self._url(),
                                          self._build(title, color, description, ping,
                                                      queued_at),
                                          shot)
            except Exception as e:  # never let the worker die
                ok, detail = False, f"{type(e).__name__}: {e}"
            if not ok:
                print(f"[Discord] '{title}' not sent: {detail}")
            if future is not None and not future.done():
                future.set_result((ok, detail))
            self._done()
