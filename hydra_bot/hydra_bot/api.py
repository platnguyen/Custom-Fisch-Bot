"""Local HTTP control API (loopback) that the Tauri UI drives.

Reconstruction of ``HydraAPIHandler`` / ``HydraHTTPServer`` /
``start_hydra_api_server``. Endpoints and the ``/command`` action set match the
original; every response is JSON with permissive CORS headers.

    GET  /health          -> {"status": "ok"}
    GET  /state           -> run state + status
    GET  /config          -> full config document
    GET  /yolo_models     -> available .pt files
    GET  /logs            -> recent log lines
    POST /config          -> replace config document, save
    POST /command {action, ...}

Supported actions: toggle_start_stop, set_setting, set_gui_setting,
reload_config, save_full_config, reset_config, quit_app, test_webhook.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import constants as C
from .perception import _resource_dir


def make_handler(bot, log_buffer):
    class HydraAPIHandler(BaseHTTPRequestHandler):
        """All endpoints return JSON."""

        def log_message(self, *args):  # silence default stderr logging
            pass

        def _send_json(self, data, status=200):
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return {}

        def do_OPTIONS(self):
            self._send_json({"status": "ok"})

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                self._send_json({"status": "ok"})
            elif path == "/state":
                self._send_json(self._handle_get_state())
            elif path == "/config":
                self._send_json(bot.cfg.document)
            elif path == "/yolo_models":
                self._send_json({"models": self._list_yolo_models()})
            elif path == "/logs":
                self._send_json({"logs": log_buffer.tail()})
            else:
                self._send_json({"error": "not_found"}, status=404)

        def do_POST(self):
            path = urlparse(self.path).path
            data = self._read_body()
            if path == "/command":
                self._send_json(self._handle_command(data))
            elif path == "/config":
                self._send_json(self._handle_post_config(data))
            else:
                self._send_json({"error": "not_found"}, status=404)

        # -- GET helpers --------------------------------------------------
        def _handle_get_state(self):
            return {
                "isRunning": bot.is_running,
                "status": bot.status_text,
                "statusColor": bot.status_color,
                "statusSub": bot.status_sub,
                "loopCount": bot.loop_count,
                "caughtCount": bot.caught_count,
                "cycleCount": bot._total_cycle_count,
                "rod": bot.cfg.current_rod(),
            }

        def _list_yolo_models(self):
            folder = _resource_dir("YOLO rods")
            try:
                return [f for f in os.listdir(folder) if f.endswith(".pt")]
            except OSError:
                return []

        # -- POST helpers -------------------------------------------------
        def _handle_post_config(self, data):
            if not isinstance(data, dict):
                return {"error": "invalid config"}
            bot.cfg.document.update(data)
            bot.cfg.save()
            return {"status": "ok"}

        def _handle_command(self, data):
            action = (data or {}).get("action", "")
            if action == "toggle_start_stop":
                new_state = bot.toggle()
                return {"status": "ok", "isRunning": new_state}
            if action == "set_setting":
                key = data.get("key")
                if not key:
                    return {"error": "missing key"}
                bot.cfg.set(key, data.get("value"))
                bot.cfg.save()
                return {"status": "ok"}
            if action == "set_gui_setting":
                key = data.get("key")
                if not key:
                    return {"error": "missing key"}
                bot.cfg.global_gui_settings[key] = data.get("value")
                bot.cfg.save()
                return {"status": "ok"}
            if action == "reload_config":
                bot.cfg.load()
                return {"status": "ok"}
            if action == "save_full_config":
                cfg = data.get("config")
                if isinstance(cfg, dict):
                    bot.cfg.document.update(cfg)
                    bot.cfg.save()
                return {"status": "ok"}
            if action == "reset_config":
                bot.cfg._doc = bot.cfg._load_defaults()
                bot.cfg.save()
                return {"status": "ok"}
            if action == "quit_app":
                bot.quit()
                threading.Timer(0.5, lambda: os._exit(0)).start()
                return {"status": "quitting"}
            if action == "test_webhook":
                try:
                    ok, detail = bot.notifier.test().result(timeout=30)
                except Exception as e:
                    ok, detail = False, f"timed out ({e})"
                return {"status": "ok" if ok else "error", "message": detail}
            return {"error": f"Unknown action: {action}"}

    return HydraAPIHandler


def start_api_server(bot, log_buffer, port: int = C.HYDRA_API_PORT):
    handler = make_handler(bot, log_buffer)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f" Hydra API server listening on http://127.0.0.1:{port}")
    return server
