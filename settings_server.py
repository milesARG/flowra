"""Local settings UI: a tiny HTTP server on 127.0.0.1 opened as an Edge app window.

Every request needs the per-run random token and a matching Host header, so other web
pages in your browser can't read or change Flowra's settings.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE = Path(__file__).parent / "settings.html"
EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]

MODELS = [
    {"id": "auto", "label": "Automatic", "note": "Large v3 Turbo on NVIDIA GPUs, Small on CPU"},
    {"id": "large-v3-turbo", "label": "Large v3 Turbo", "note": "Best balance - ~1.6 GB VRAM"},
    {"id": "large-v3", "label": "Large v3", "note": "Most accurate, slower - ~3 GB VRAM"},
    {"id": "distil-large-v3", "label": "Distil Large v3", "note": "English only, very fast - ~1.5 GB"},
    {"id": "medium", "label": "Medium", "note": "~1.5 GB VRAM"},
    {"id": "small", "label": "Small", "note": "Light - ~0.6 GB VRAM"},
    {"id": "base", "label": "Base", "note": "Very light, less accurate"},
]


def stats(history: list[dict]) -> dict:
    words = sum(h.get("words", 0) for h in history)
    secs = sum(h.get("secs", 0) for h in history)
    today = time.strftime("%Y-%m-%d")
    today_words = sum(h.get("words", 0) for h in history if h.get("ts", "").startswith(today))
    timed_words = sum(h.get("words", 0) for h in history if h.get("secs"))
    wpm = round(timed_words / (secs / 60)) if secs > 5 else 0
    saved_min = max(0, words / 40 - secs / 60)   # vs typing at 40 wpm
    apps: dict[str, int] = {}
    for h in history:
        if h.get("app"):
            apps[h["app"]] = apps.get(h["app"], 0) + 1
    return {"dictations": len(history), "words": words, "today_words": today_words, "wpm": wpm,
            "saved_min": round(saved_min), "apps": sorted(apps, key=apps.get, reverse=True)[:12]}


class Server:
    def __init__(self, app):
        import flowra
        self.app, self.flowra = app, flowra
        self.token = secrets.token_urlsafe(18)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def open(self, page: str = ""):
        url = self.url + (f"#{page}" if page else "")
        for exe in EDGE:
            if os.path.exists(exe):
                subprocess.Popen([exe, f"--app={url}", "--window-size=980,760"],
                                 creationflags=0x08000000)
                return
        webbrowser.open(url)

    def _handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _ok(self) -> bool:
                q = parse_qs(urlparse(self.path).query)
                host_ok = self.headers.get("Host") == f"127.0.0.1:{srv.port}"
                tok_ok = secrets.compare_digest((q.get("t") or [""])[0], srv.token)
                if not (host_ok and tok_ok):
                    self.send_error(403)
                    return False
                return True

            def _send(self, body, ctype="application/json", code=200):
                data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/logo.png":   # public, harmless static asset
                    return self._send((srv.flowra.HERE / "assets" / "logo.png").read_bytes(), "image/png")
                if not self._ok():
                    return
                q = parse_qs(urlparse(self.path).query)
                fl, app = srv.flowra, srv.app
                if path == "/":
                    self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    hist = fl.read_history()
                    self._send({"config": app.cfg, "defaults": fl.DEFAULT_CONFIG,
                                "mics": fl.list_microphones(), "models": MODELS,
                                "ready": app.tx.ready.is_set(), "status": app.tx.status,
                                "active": app.tx.active, "stats": stats(hist)})
                elif path == "/api/history":
                    term = (q.get("q") or [""])[0].lower()
                    hist = [h for h in fl.read_history() if term in h.get("text", "").lower()]
                    self._send(list(reversed(hist))[:400])
                else:
                    self.send_error(404)

            def do_POST(self):
                if not self._ok():
                    return
                path = urlparse(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    return self._send({"error": "bad json"}, code=400)
                app = srv.app
                if path == "/api/config":
                    allowed = {k: v for k, v in body.items() if k in srv.flowra.DEFAULT_CONFIG}
                    app.save_config(allowed)
                    self._send({"ok": True})
                elif path == "/api/preview":
                    app.preview_until = time.time() + 2.0
                    self._send({"ok": True})
                elif path == "/api/move":
                    if app.overlay:
                        app.overlay.begin_edit() if body.get("on", True) else app.overlay.end_edit(True)
                    self._send({"ok": True})
                elif path == "/api/suspend":
                    app.suspended = bool(body.get("on"))
                    self._send({"ok": True})
                else:
                    self.send_error(404)

        return H


def start(app) -> Server:
    return Server(app)
