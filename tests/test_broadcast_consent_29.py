"""Согласие, которое анкета не спрашивает: кнопка только в рассылке.

Пустой список оставляет шаги анкеты прежними. Нажатие пишет то согласие, которое
менеджер прикрепил к строке журнала. Ключ из текста кнопки не читается.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from config import config
from database import db
from domain.regform.engine import (
    broadcast_consent_entries,
    get_consent_steps,
    registration_consent_entries,
)
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.comms import admin_broadcast_consent as seam
from handlers.comms import admin_broadcasts
from handlers.comms import consent_tap
from handlers.states import Broadcast
from tests._enroll38 import ready, run

_FORBIDDEN = ("отбор", "не прошёл", "не прошел", "отказ")
_ROOT = Path(__file__).resolve().parents[1]


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, uid=1):
        self.chat = SimpleNamespace(id=uid)
        self.from_user = _User(uid)
        self.edits = []
        self.deleted = False

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))
        self.text = text
        self.markup = reply_markup

    async def answer(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))

    async def delete(self):
        self.deleted = True


class _Cb:
    def __init__(self, data, uid=7, message=None):
        self.data = data
        self.from_user = _User(uid)
        self.message = message or _Msg(uid)
        self.alerts = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append((text, show_alert))


class _State:
    def __init__(self, data=None, current=None):
        self.data = dict(data or {})
        self.current = current

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_state(self):
        return self.current

    async def set_state(self, value):
        self.current = value

    async def clear(self):
        self.data = {}
        self.current = None


class _Bot:
    def __init__(self):
        self.sent = []
        self.albums = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_media_group(self, chat_id, media):
        self.albums.append((chat_id, media))
        return [SimpleNamespace(message_id=50 + len(self.albums))]


def _buttons(markup):
    if markup is None:
        return []
    return [btn.text for row in markup.inline_keyboard for btn in row]


def test_empty_broadcast_list_keeps_steps(tmp_path):
    ready(tmp_path)

    async def go():
        await db.set_setting("consent_enabled", "on")
        await db.set_setting("consent_list", "A|data\nB|policy\nbad-no-pipe\nNoKey|")
        assert await get_consent_steps() == ["consent:data", "consent:policy"]
        await db.set_setting("consent_list", "")
        assert await get_consent_steps() == ["consent:personal_data"]
        assert await broadcast_consent_entries() == []

    run(go())


def test_broadcast_key_is_not_a_registration_step(tmp_path):
    ready(tmp_path)

    async def go():
        await db.set_setting("consent_enabled", "on")
        await db.set_setting("consent_list", "Данные|data\nСъёмка|photo")
        await db.set_setting("consent_broadcast_list", "Согласие на съёмку|photo")
        steps = await get_consent_steps()
        assert steps == ["consent:data"]
        keys = {key for _label, key in await registration_consent_entries()}
        assert "photo" not in keys
        assert "data" in keys

    run(go())


def test_no_seeded_photo_line():
    entry = SETTINGS_SCHEMA["consent_broadcast_list"]
    assert not entry["default"]
    assert "photo" not in str(entry["default"] or "")
    assert "Согласие на съёмку" in entry["prompt"]
    assert entry["default"] is None


def test_tap_table_and_column(tmp_path):
    ready(tmp_path)

    async def fresh():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'broadcast_consent_taps'"
            ) as cursor:
                assert await cursor.fetchone()
            for table in ("broadcasts", "scheduled_broadcasts"):
                async with conn.execute(f"PRAGMA table_info({table})") as cursor:
                    info = await cursor.fetchall()
                col = next(row for row in info if row[1] == "consent_key")
                assert col[3] == 0  # nullable
        plain = await db.create_broadcast(1, "без кнопки", 1)
        assert (await db.get_broadcast(plain))["consent_key"] is None
        marked = await db.create_broadcast(1, "с кнопкой", 1, consent_key="photo")
        assert (await db.get_broadcast(marked))["consent_key"] == "photo"
        scheduled = await db.create_scheduled_broadcast(
            "позже", None, None, "2026-12-01 10:00:00", 1,
        )
        assert (await db.get_scheduled_broadcast(scheduled))["consent_key"] is None

    run(fresh())

    old_path = tmp_path / "old_broadcasts.db"
    conn = sqlite3.connect(old_path)
    conn.execute(
        "CREATE TABLE broadcasts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, text_preview TEXT, "
        "started_at TEXT, finished_at TEXT, status TEXT, total INTEGER, "
        "delivered INTEGER, blocked INTEGER)"
    )
    conn.execute(
        "INSERT INTO broadcasts (admin_id, text_preview, status, total, delivered, blocked) "
        "VALUES (7, 'старая', 'done', 3, 2, 1)"
    )
    conn.execute(
        "CREATE TABLE scheduled_broadcasts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, photo_file_id TEXT, "
        "filter_spec TEXT, scheduled_at TEXT NOT NULL, status TEXT, "
        "created_by INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO scheduled_broadcasts (text, scheduled_at, status) "
        "VALUES ('старая', '2026-01-01 10:00:00', 'pending')"
    )
    conn.commit()
    conn.close()
    config.DB_PATH = str(old_path)
    run(db.init_db())
    assert run(db.get_broadcast(1))["consent_key"] is None
    assert run(db.get_scheduled_broadcast(1))["consent_key"] is None


def test_delegate_defaults_have_no_forbidden_words():
    for key in ("consent_broadcast_thanks_text", "consent_broadcast_stale_text"):
        default = SETTINGS_SCHEMA[key]["default"].lower()
        for word in _FORBIDDEN:
            assert word not in default


def test_picker_lists_only_broadcast_consents(tmp_path):
    ready(tmp_path)

    async def go():
        await db.set_setting("consent_list", "Политика|policy")
        await db.set_setting("consent_broadcast_list", "Согласие на съёмку|photo")
        state = _State(current=Broadcast.confirm.state)
        row = await seam.confirm_row(state)
        assert _buttons(SimpleNamespace(inline_keyboard=row)) == ["📸 Кнопка согласия"]
        message = _Msg()
        callback = _Cb("bc_consent_open", message=message)
        await seam.bc_consent_open(callback, state)
        labels = _buttons(message.markup)
        assert labels == ["Согласие на съёмку"]
        assert "Политика" not in labels
        assert "policy" not in labels
        pick = _Cb("bc_consent:photo", message=_Msg())
        await seam.bc_consent_pick(pick, state, _Bot())
        assert state.data["bc_consent_key"] == "photo"
        chosen = await seam.confirm_row(state)
        assert _buttons(SimpleNamespace(inline_keyboard=chosen))[0] == "✅ Кнопка: Согласие на съёмку"
        button = await seam.consent_button(15, "photo")
        assert button.inline_keyboard[0][0].callback_data == "cns:15"
        assert "photo" not in button.inline_keyboard[0][0].callback_data

    run(go())


def test_empty_list_explains(tmp_path):
    ready(tmp_path)
    state = _State(current=Broadcast.confirm.state)
    callback = _Cb("bc_consent_open")
    run(seam.bc_consent_open(callback, state))
    text = callback.alerts[0][0]
    assert "рассылк" in text.lower()
    assert "анкет" in text.lower()
    assert "consent_" not in text
    assert "настройк" not in text.lower()


def test_album_refuses_button(tmp_path, monkeypatch):
    ready(tmp_path)

    async def go():
        await db.set_setting("consent_broadcast_list", "Согласие на съёмку|photo")
        state = _State({
            "bc_users": [42],
            "bc_preview": "письмо",
            "bc_album": [{"type": "photo", "file_id": "fid", "caption": "кадр"}],
            "bc_consent_key": "photo",
            "bc_important": True,
            "bc_kind": "media",
        })
        bot = _Bot()
        message = _Msg(1)
        callback = _Cb("bc_go", uid=1, message=message)
        spawned = []

        def capture(coro):
            spawned.append(coro)

        monkeypatch.setattr(admin_broadcasts, "_spawn", capture)

        async def _noop(_seconds):
            return None

        monkeypatch.setattr(admin_broadcasts.asyncio, "sleep", _noop)
        from services.comms import broadcast_run as br
        monkeypatch.setattr(br.asyncio, "sleep", _noop)
        await admin_broadcasts.bc_go(callback, state, bot)
        assert spawned
        await spawned[0]
        assert bot.albums
        assert bot.sent == []
        assert all(item[1] is None or "cns:" not in str(item[1]) for item in bot.sent)
        row = await db.get_broadcast(1)
        assert row["consent_key"] is None
        assert any("К альбому кнопка не крепится" in text for text, _kb in message.edits)

    run(go())


def test_tap_records_consent_and_is_idempotent(tmp_path):
    ready(tmp_path)

    async def go():
        await db.set_setting("consent_broadcast_list", "Согласие на съёмку|photo")
        bid = await db.create_broadcast(1, "письмо", 2, consent_key="photo")
        other = await db.create_broadcast(1, "другое", 1, consent_key="other")
        first = _Cb(f"cns:{bid}", uid=50)
        await consent_tap.consent_tap(first)
        await consent_tap.consent_tap(first)
        junk = _Cb(f"cns:{bid}:notphoto", uid=50)
        await consent_tap.consent_tap(junk)
        assert await db.get_user_consents(50) == ["photo"]
        assert junk.alerts[-1][0] == "Эта кнопка больше не действует."
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM broadcast_consent_taps WHERE broadcast_id = ? AND user_id = ?",
                (bid, 50),
            ) as cursor:
                assert (await cursor.fetchone())[0] == 1
        forged = _Cb(f"cns:{other}", uid=50)
        await consent_tap.consent_tap(forged)
        assert "other" in await db.get_user_consents(50)
        assert "photo" in await db.get_user_consents(50)
        assert first.alerts[-1][0] == "Принято. Спасибо."

    run(go())


def test_stale_broadcast_answers_registry_text(tmp_path):
    ready(tmp_path)

    async def go():
        bid = await db.create_broadcast(1, "без кнопки", 1)
        callback = _Cb(f"cns:{bid}", uid=51)
        await consent_tap.consent_tap(callback)
        assert callback.alerts[-1][0] == "Эта кнопка больше не действует."
        assert await db.get_user_consents(51) == []
        missing = _Cb("cns:99999", uid=51)
        await consent_tap.consent_tap(missing)
        assert missing.alerts[-1][0] == "Эта кнопка больше не действует."
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM broadcast_consent_taps") as cursor:
                assert (await cursor.fetchone())[0] == 0

    run(go())


def test_journal_line_only_when_attached(tmp_path):
    ready(tmp_path)
    base = {
        "id": 3, "status": "done", "delivered": 4, "blocked": 0, "total": 4,
        "text_preview": "привет", "started_at": "2026-11-03 12:00:00", "admin_id": 1,
        "important": 0, "mute_skipped": 0,
    }
    plain, _kb = admin_broadcasts._broadcast_card(dict(base))
    marked = dict(base, consent_line="Нажали кнопку: 2 из 4")
    text, _kb2 = admin_broadcasts._broadcast_card(marked)
    assert "Нажали кнопку" not in plain
    assert "Нажали кнопку: 2 из 4" in text
    assert text.startswith(plain)

    async def counts():
        bid = await db.create_broadcast(1, "с кнопкой", 4, consent_key="photo")
        await db.finish_broadcast(bid, "done", 4, 0, 0)
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO broadcast_consent_taps (broadcast_id, user_id, accepted_at) "
                "VALUES (?, 1, 't'), (?, 2, 't')",
                (bid, bid),
            )
            await conn.commit()
        rows = await db.list_recent_broadcasts(10)
        await seam.attach_consent_lines(rows)
        row = next(item for item in rows if item["id"] == bid)
        assert row["consent_line"] == "Нажали кнопку: 2 из 4"
        card, _kb3 = admin_broadcasts._broadcast_card(row)
        assert "Нажали кнопку: 2 из 4" in card

    run(counts())


def test_admin_broadcasts_growth_is_calls_only():
    import subprocess

    old = subprocess.check_output(
        ["git", "show", "HEAD:handlers/comms/admin_broadcasts.py"], text=True,
    )
    new = (_ROOT / "handlers" / "comms" / "admin_broadcasts.py").read_text(encoding="utf-8")

    def defs(text):
        return [
            line.strip() for line in text.splitlines()
            if line.startswith("def ") or line.startswith("async def ")
        ]

    assert defs(new) == defs(old)
    assert "def confirm_row" not in new
    assert "def consent_button" not in new
