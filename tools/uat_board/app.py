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
# Фирменный узор плиты Юлид (копия miniapp/static/pattern/youlead.webp) — фон шапки страницы.
try:
    PATTERN = open(os.path.join(HERE, "pattern.webp"), "rb").read()
except OSError:
    PATTERN = b""

# Презентация «Бот на форуме» (23.09.2026) на отдельном поддомене deck.alekseev.info: тот же
# контейнер, маршрут выбирается по заголовку Host. Заметки к слайдам — свой файл, чтобы
# /api/reset доски приёмки их не стирал.
DECK_DIR = os.path.join(HERE, "deck")
DECK_NOTES = os.environ.get("DECK_NOTES", "/data/deck_notes.json")
DECK_HOST = os.environ.get("DECK_HOST", "deck.")
# Презентация бота для внешних команд (28.09.2026, /bot): свои комментарии в отдельном файле,
# чтобы гости не видели заметок ОК к деке форума.
BOT_NOTES = os.environ.get("BOT_NOTES", os.path.join(os.path.dirname(DECK_NOTES) or ".", "bot_notes.json"))


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""

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


def _deck_load(path: str = DECK_NOTES) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and isinstance(state.get("notes"), dict):
            state.setdefault("v", 0)
            return state
    except (OSError, ValueError):
        pass
    return {"v": 0, "notes": {}}


def _deck_store(state: dict, path: str = DECK_NOTES) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def _deck_public(state: dict, cid: str) -> dict:
    """Клиентский id автора наружу не отдаём — только признак «моя заметка»."""
    notes = {
        slide: [
            {"id": n["id"], "who": n["who"], "text": n["text"], "at": n["at"], "mine": bool(cid) and n.get("cid") == cid}
            for n in items
        ]
        for slide, items in state["notes"].items()
    }
    return {"v": state["v"], "notes": notes}


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

    def _is_deck(self) -> bool:
        return (self.headers.get("Host") or "").startswith(DECK_HOST)

    def _deck_get(self, path: str) -> None:
        if path in ("/", "/index.html"):
            self._send(200, _read(os.path.join(DECK_DIR, "deck.html")), "text/html; charset=utf-8")
        elif path == "/deck.pdf":
            body = _read(os.path.join(DECK_DIR, "deck.pdf"))
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''bot-na-forume.pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/bot", "/bot/"):
            self._send(200, _read(os.path.join(DECK_DIR, "bot.html")), "text/html; charset=utf-8")
        elif path == "/bot.pdf":
            body = _read(os.path.join(DECK_DIR, "bot.pdf"))
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''aiesec-event-bot.pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/bot/api/notes":
            with _lock:
                self._json(200, _deck_public(_deck_load(BOT_NOTES), self.headers.get("X-Client", "")))
        elif path in ("/guide", "/guide/"):
            # Гайд «Бот на форуме: что сделано и как работать» (26.09) — отдельная страница,
            # дека для ОК на «/» не меняется.
            self._send(200, _read(os.path.join(DECK_DIR, "guide.html")), "text/html; charset=utf-8")
        elif path in ("/uat", "/uat/"):
            # Чеклист приёмки форумного функционала (26.09): отметки хранятся в браузере
            # тестировщика, отчёт копируется кнопкой — серверного состояния нет.
            self._send(200, _read(os.path.join(DECK_DIR, "uat.html")), "text/html; charset=utf-8")
        elif "/shots/" in path and path.endswith(".png"):
            name = os.path.basename(path)
            body = _read(os.path.join(DECK_DIR, "shots", name))
            if body:
                self._send(200, body, "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        elif path == "/api/notes":
            with _lock:
                self._json(200, _deck_public(_deck_load(), self.headers.get("X-Client", "")))
        elif path == "/health":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def _deck_post(self, path: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16_000:
            self._json(413, {"error": "too_big"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "bad_json"})
            return
        cid = self.headers.get("X-Client", "")[:64]
        notes_file = DECK_NOTES
        if path.startswith("/bot/"):
            notes_file, path = BOT_NOTES, path[len("/bot"):]
        with _lock:
            state = _deck_load(notes_file)
            if path == "/api/notes":
                slide = str(body.get("slide", ""))[:64]
                text = str(body.get("text", "")).strip()[:3000]
                who = str(body.get("who", "")).strip()[:60]
                if not slide or not text:
                    self._json(400, {"error": "empty"})
                    return
                state["notes"].setdefault(slide, []).append({
                    "id": "%x%s" % (int(time.time() * 1000), os.urandom(3).hex()),
                    "who": who, "text": text, "at": _msk_hhmm(), "cid": cid,
                })
            elif path == "/api/notes/delete":
                nid = str(body.get("id", ""))
                for slide, items in state["notes"].items():
                    state["notes"][slide] = [n for n in items if not (n["id"] == nid and cid and n.get("cid") == cid)]
            else:
                self._send(404, b"not found", "text/plain")
                return
            state["v"] = int(state.get("v", 0)) + 1
            _deck_store(state, notes_file)
            self._json(200, _deck_public(state, cid))

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if self._is_deck():
            self._deck_get(parts.path)
            return
        if parts.path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif parts.path == "/health":
            self._send(200, b"ok", "text/plain")
        elif parts.path == "/pattern.webp" and PATTERN:
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(PATTERN)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(PATTERN)
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
        if self._is_deck():
            self._deck_post(parts.path)
            return
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
