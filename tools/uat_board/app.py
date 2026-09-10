"""Доска приёмки с общей базой отметок (10.09.2026).

Одна страница-чек-лист + крошечный JSON-API на stdlib, чтобы трое тестировщиков на своих
телефонах видели отметки друг друга. Состояние — один файл ``/data/state.json``:
``{"v": <версия>, "steps": {"<вкладка>:<шаг>": {"s": "ok|bad|skip|", "note": "...",
"who": "...", "at": "ЧЧ:ММ"}}}``. Доступ — по коду из ссылки (``?k=...``), код задаётся
переменной ``UAT_CODE``; пустой код = доступ открыт.

Запуск: ``python app.py`` (порт 8005), в проде — контейнер ``uat-board`` на leafye,
наружу через tunnel-relay → is-hosting → Cloudflare как ``uat.alekseev.info``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DATA = os.environ.get("UAT_DATA", "/data/state.json")
CODE = os.environ.get("UAT_CODE", "")
PORT = int(os.environ.get("UAT_PORT", "8005"))
HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(HERE, "index.html"), "rb").read()

_lock = threading.Lock()
_ALLOWED = {"", "ok", "bad", "skip"}


def _load() -> dict:
    try:
        with open(DATA, encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and isinstance(state.get("steps"), dict):
            state.setdefault("v", 0)
            return state
    except (OSError, ValueError):
        pass
    return {"v": 0, "steps": {}}


def _store(state: dict) -> None:
    os.makedirs(os.path.dirname(DATA) or ".", exist_ok=True)
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, DATA)


def _msk_hhmm() -> str:
    # МСК = UTC+3 круглый год; контейнер живёт в UTC.
    return time.strftime("%d.%m %H:%M", time.gmtime(time.time() + 3 * 3600))


class Handler(BaseHTTPRequestHandler):
    server_version = "uat-board/1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _authed(self, query: dict) -> bool:
        if not CODE:
            return True
        return self.headers.get("X-Code") == CODE or query.get("k", [""])[0] == CODE

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif parts.path == "/health":
            self._send(200, b"ok", "text/plain")
        elif parts.path == "/api/state":
            if not self._authed(query):
                self._json(403, {"error": "no_access"})
                return
            with _lock:
                self._json(200, _load())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if not self._authed(query):
            self._json(403, {"error": "no_access"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16_000:
            self._json(413, {"error": "too_big"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "bad_json"})
            return
        if parts.path == "/api/step":
            key = str(body.get("key", ""))[:80]
            s = str(body.get("s", ""))
            note = str(body.get("note", ""))[:2000]
            who = str(body.get("who", ""))[:60].strip()
            if not key or s not in _ALLOWED:
                self._json(400, {"error": "bad_step"})
                return
            with _lock:
                state = _load()
                if not s and not note.strip():
                    state["steps"].pop(key, None)
                else:
                    state["steps"][key] = {"s": s, "note": note, "who": who, "at": _msk_hhmm()}
                state["v"] = int(state.get("v", 0)) + 1
                _store(state)
                self._json(200, state)
        elif parts.path == "/api/reset":
            if body.get("confirm") != "да":
                self._json(400, {"error": "confirm"})
                return
            with _lock:
                state = _load()
                state = {"v": int(state.get("v", 0)) + 1, "steps": {}}
                _store(state)
                self._json(200, state)
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # noqa: D102
        if "/api/state" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
