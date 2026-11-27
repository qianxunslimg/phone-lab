#!/usr/bin/env python3
"""HTTP server entrypoint for Phone Mini-Server Control Center with on-demand idle sleep."""
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from metrics import collect_snapshot
import scrcpy_bridge
import sms_forwarder
from services import (
    control_service,
    drop_memory_caches,
    get_all_services,
    read_acc_config,
    set_hardware_bypass,
    tail_log,
    update_acc_config,
    verify_basic_auth,
)

PORT = int(os.environ.get("DASHBOARD_PORT", "8888"))
COLLECT_INTERVAL = float(os.environ.get("COLLECT_INTERVAL", "3.0"))
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT", "15.0"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon.png": ("icon.png", "image/png"),
    "/favicon.ico": ("icon.png", "image/png"),
    "/vendor/jmuxer.min.js": ("vendor/jmuxer.min.js", "application/javascript; charset=utf-8"),
}

_cache_lock = threading.Lock()
_cached_snapshot = None
_cached_at = 0.0
_last_viewer_at = 0.0
_wake_event = threading.Event()


def refresh_cache_once():
    global _cached_snapshot, _cached_at
    snap = collect_snapshot(get_all_services())
    with _cache_lock:
        _cached_snapshot = snap
        _cached_at = time.monotonic()


def collector_loop():
    """Collect telemetry only while a browser is actively viewing; sleep completely when idle."""
    while True:
        idle_for = time.monotonic() - _last_viewer_at
        if idle_for > IDLE_TIMEOUT:
            _wake_event.clear()
            _wake_event.wait()
            continue
        try:
            refresh_cache_once()
        except Exception as exc:
            print(f"[collector] error: {exc}", flush=True)
        time.sleep(COLLECT_INTERVAL)


def get_cached_metrics(sort_by: str = "cpu") -> dict:
    global _last_viewer_at
    now = time.monotonic()
    _last_viewer_at = now
    _wake_event.set()

    with _cache_lock:
        snap = _cached_snapshot
        age = now - _cached_at

    # Only block synchronously on very first request or after a long sleep
    if snap is None or age > IDLE_TIMEOUT:
        refresh_cache_once()
        with _cache_lock:
            snap = _cached_snapshot

    payload = dict(snap)
    if sort_by == "mem":
        payload["top_procs"] = snap.get("top_procs_mem", snap.get("top_procs", []))
    payload.pop("top_procs_mem", None)
    return payload


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _safe_write(self, body: bytes):
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._safe_write(body)

    def require_auth(self) -> bool:
        if not verify_basic_auth(self.headers):
            self.send_json(401, {"ok": False, "error": "Unauthorized"})
            return False
        return True

    def serve_static(self, filename: str, content_type: str):
        target = STATIC_DIR / filename
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        ctype = content_type or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._safe_write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path in STATIC_ROUTES:
            fname, ctype = STATIC_ROUTES[parsed.path]
            self.serve_static(fname, ctype)
            return

        if parsed.path == "/api/metrics":
            sort_by = qs.get("sort", ["cpu"])[0]
            self.send_json(200, get_cached_metrics(sort_by))
            return

        if parsed.path == "/api/acc":
            if not self.require_auth():
                return
            self.send_json(200, read_acc_config())
            return

        if parsed.path == "/api/logs":
            if not self.require_auth():
                return
            name = qs.get("name", ["filebrowser"])[0]
            ok, content = tail_log(name)
            self.send_json(200 if ok else 400, {"ok": ok, "name": name, "content": content})
            return

        if parsed.path == "/api/sms/status":
            if not self.require_auth():
                return
            self.send_json(200, sms_forwarder.get_status())
            return

        if parsed.path == "/api/scrcpy/ws":
            scrcpy_bridge.handle_websocket_upgrade(self, qs)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if not self.require_auth():
            return

        if parsed.path == "/api/auth/verify":
            self.send_json(200, {"ok": True, "message": "验证通过"})
            return

        if parsed.path == "/api/service":
            data = self.read_json_body()
            name = data.get("service") or qs.get("service", [""])[0]
            action = data.get("action") or qs.get("action", ["restart"])[0]
            ok, msg = control_service(name, action)
            threading.Thread(target=refresh_cache_once, daemon=True).start()
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/acc":
            ok, msg = update_acc_config(self.read_json_body())
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg, "config": read_acc_config()})
            return

        if parsed.path == "/api/bypass":
            data = self.read_json_body()
            ok, msg = set_hardware_bypass(bool(data.get("enable", False)))
            threading.Thread(target=refresh_cache_once, daemon=True).start()
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/drop-caches":
            ok, msg = drop_memory_caches()
            threading.Thread(target=refresh_cache_once, daemon=True).start()
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/sms/config":
            ok, msg = sms_forwarder.update_config(self.read_json_body())
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg, "status": sms_forwarder.get_status()})
            return

        if parsed.path == "/api/sms/test":
            ok, msg = sms_forwarder.send_test_email(self.read_json_body())
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg, "status": sms_forwarder.get_status()})
            return

        if parsed.path == "/api/sms/resend":
            data = self.read_json_body()
            try:
                sms_id = int(data.get("id", 0))
            except (TypeError, ValueError):
                self.send_json(400, {"ok": False, "message": "无效的短信 ID"})
                return
            ok, msg = sms_forwarder.resend_sms_by_id(sms_id)
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg, "status": sms_forwarder.get_status()})
            return

        if parsed.path == "/api/scrcpy/ticket":
            if not scrcpy_bridge.is_available():
                self.send_json(503, {"ok": False, "message": "手机端未安装 scrcpy-server.jar"})
                return
            self.send_json(200, {"ok": True, "ticket": scrcpy_bridge.issue_ticket()})
            return

        self.send_error(404)


def main():
    threading.Thread(target=collector_loop, daemon=True).start()
    sms_forwarder.start_watcher()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Phone Mini-Server Control Center listening on 0.0.0.0:{PORT} (idle sleep + SMS watcher enabled)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
