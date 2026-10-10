"""Бэклог чек-ина №7: тренировочный режим сканера и лист учебных QR
(`services/checkin_training.py`, `miniapp/routers/checkin.py`, `handlers/forum/admin_checkin.py`,
`handlers/forum/admin_checkin_training.py`).

Главное свойство: тренировка НИЧЕГО не пишет — ни `checkins`, ни `venue_log`, — но плашка та
же, что дал бы вход. Учебный токен узнаётся через реестр резолверов (`kind="training"`)."""
from __future__ import annotations

import asyncio
import io

import pytest

from config import config as bot_config
from database import db as bot_db
from handlers.forum import admin_checkin, admin_checkin_training
from handlers.states import CheckinImport
from services import checkin as checkin_mod
from services import checkin_training as training
from services.checkin import build_payload

from tests.test_admin_checkin_260924 import (
    _FakeBot,
    _FakeCallback,
    _FakeDocument,
    _FakeMessage,
    _flat_text,
    _new_state,
)
from tests.test_miniapp_checkin_260924 import (
    BASE,
    GAME_MANAGER_ID,
    _grant_checkin_to_game_manager,
    _hdr,
    _insert_user,
    _qr,
    _run,
    client_with,
)


async def _count(table: str) -> int:
    async with bot_db._connect() as conn:
        async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            return (await cur.fetchone())[0]


def _nothing_written():
    assert _run(_count("checkins")) == 0
    assert _run(_count("venue_log")) == 0


def _payloads() -> dict[str, str]:
    return dict(_run(training.training_payloads()))


# ── резолвер и формат ──────────────────────────────────────────────────────────────────────

def test_training_token_resolves_as_training_kind_even_after_registry_reset(tmp_path):
    client_with(tmp_path)
    checkin_mod.clear_token_resolvers()
    resolved = _run(training.resolve_training("TRAIN-PENDING", point="entry", source="miniapp"))
    assert resolved["kind"] == "training"
    assert resolved["id"] == "pending"
    assert resolved["denial"] == "not_approved"
    assert _run(training.resolve_training("real-token")) is None


def test_payloads_five_kinds_foreign_has_other_tag(tmp_path):
    client_with(tmp_path)
    payloads = _payloads()
    assert list(payloads) == ["ok", "dup", "pending", "past", "foreign"]
    tag = _run(checkin_mod.current_event_tag())
    for kind, payload in payloads.items():
        parsed = checkin_mod.parse_qr_payload(payload)
        assert parsed["token"] == training.TRAINING_TOKENS[kind]
        assert (parsed["tag"] == tag) == (kind != "foreign")


# ── сканер Mini App ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,status", [
    ("ok", "new"), ("dup", "duplicate"), ("pending", "denied"), ("past", "denied"),
    ("foreign", "foreign_event"),
])
def test_training_qr_at_entry_shows_plaque_writes_nothing(tmp_path, kind, status):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    resp = client.post(f"{BASE}/scan", json={"payload": _payloads()[kind], "point": "entry"},
                       headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["status"] == status
    assert body["training"] is True
    assert body["training_note"] == "Учебный QR — не пропуск на вход, ничего не записывается."
    assert body["hint"]
    if kind == "ok":
        assert body["undo"]["demo"] is True
        assert body["undo"]["label"] == "↩️ Отменить"
    if kind == "pending":
        assert body["reason_text"] == "Заявка ещё на рассмотрении"
    _nothing_written()


def test_real_qr_at_training_point_previews_without_writing(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951001
    _run(_insert_user(uid, full_name="Петров Пётр"))
    payload = _qr(uid, full_name="Петров Пётр")

    r1 = client.post(f"{BASE}/scan", json={"payload": payload, "point": "training"},
                     headers=_hdr(GAME_MANAGER_ID)).json()
    assert r1["status"] == "new"
    assert r1["training"] is True
    assert r1["full_name"] == "Петров Пётр"
    assert r1["undo"]["demo"] is True
    _nothing_written()

    real = client.post(f"{BASE}/scan", json={"payload": payload, "point": "entry"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert real["status"] == "new"
    log_rows = _run(_count("venue_log"))

    r2 = client.post(f"{BASE}/scan", json={"payload": payload, "point": "training"},
                     headers=_hdr(GAME_MANAGER_ID)).json()
    assert r2["status"] == "duplicate"
    assert r2["scanned_at"] == real["scanned_at"]
    assert r2["training"] is True
    assert _run(_count("checkins")) == 1
    assert _run(_count("venue_log")) == log_rows


def test_denied_real_qr_at_training_point_not_journaled(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951002
    _run(_insert_user(uid, status="pending"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid), "point": "training"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "denied"
    assert body["training"] is True
    foreign = client.post(f"{BASE}/scan", json={"payload": _qr(uid, tag="OTHER"), "point": "training"},
                          headers=_hdr(GAME_MANAGER_ID)).json()
    assert foreign["status"] == "foreign_event"
    _nothing_written()


def test_manual_at_training_point_writes_nothing(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951003
    _run(_insert_user(uid))
    body = client.post(f"{BASE}/manual", json={"telegram_id": uid, "point": "training"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new"
    assert body["training"] is True
    _nothing_written()


# ── загрузка CSV в боте ────────────────────────────────────────────────────────────────────

def _csv(*payloads: str) -> bytes:
    rows = "".join(f"2026-10-03T09:0{i}:00,{p}\n" for i, p in enumerate(payloads))
    return ("ts,qr\n" + rows).encode("utf-8")


def test_csv_only_training_codes_reports_and_marks_nothing(tmp_path, monkeypatch):
    client_with(tmp_path)
    monkeypatch.setattr(bot_config, "ADMIN_IDS", [GAME_MANAGER_ID])
    state = _new_state(GAME_MANAGER_ID)
    _run(state.set_state(CheckinImport.waiting_file))
    message = _FakeMessage(GAME_MANAGER_ID, document=_FakeDocument())
    _run(admin_checkin.checkin_import_file_step(message, state, _FakeBot(_csv(*_payloads().values()))))
    assert any("только учебные QR: 5" in (t or "") for t in _flat_text(message))
    assert _run(state.get_state()) is None
    _nothing_written()


def test_csv_mixed_file_records_real_and_reports_training(tmp_path, monkeypatch):
    client_with(tmp_path)
    monkeypatch.setattr(bot_config, "ADMIN_IDS", [GAME_MANAGER_ID])
    uid = 951004
    _run(_insert_user(uid))
    tag = _run(checkin_mod.current_event_tag())
    real = build_payload(tag, "Иванов Иван", "Казань", _run(bot_db.get_or_create_checkin_token(uid)))
    p = _payloads()
    state = _new_state(GAME_MANAGER_ID)
    _run(state.set_state(CheckinImport.waiting_file))
    message = _FakeMessage(GAME_MANAGER_ID, document=_FakeDocument())
    _run(admin_checkin.checkin_import_file_step(message, state, _FakeBot(_csv(real, p["ok"], p["foreign"]))))
    assert any("Нашёл кодов: 1" in (t or "") for t in _flat_text(message))

    cb = _FakeCallback("checkin_point:entry", GAME_MANAGER_ID)
    _run(admin_checkin.checkin_point_pick(cb, state))
    report = _flat_text(cb.message)[0]
    assert "Отмечено новых: 1" in report
    assert "не найдено: 0" in report
    assert "Учебных QR: 2 (не записаны)" in report
    assert _run(_count("checkins")) == 1


def test_count_and_drop_training_codes():
    text = "a,YL26·Учебный делегат·—·TRAIN-OK\nb,YL26·Учебный делегат·—·TRAIN-OK\nc,X·y·z·TRAIN-OKAY\n"
    assert training.count_training_codes(text) == 1
    recs = [{"qr": "YL26·a·b·TRAIN-DUP", "scanned_at": None}, {"qr": "YL26·a·b·real", "scanned_at": None}]
    assert training.drop_training_records(recs) == [recs[1]]


# ── лист учебных QR ────────────────────────────────────────────────────────────────────────

def test_sheet_is_a4_png(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    client_with(tmp_path)
    png = training.render_training_sheet(_run(training.training_sheet_inputs("ru", {})))
    assert Image.open(io.BytesIO(png)).size == (1240, 1754)


@pytest.mark.parametrize("name", ["lato-400.woff2", "lato-700.woff2", "raleway-800.woff2"])
def test_sheet_font_opens_with_cyrillic_not_bitmap_fallback(name):
    """Шрифт листа реально открыт FreeType (не битмап load_default без кириллицы)."""
    pytest.importorskip("PIL")
    from PIL import ImageFont

    font = training.sheet_font(name, 30)
    assert font.getname()[0].startswith(("Lato", "Raleway"))
    assert font.getlength("Ж") != ImageFont.load_default(30).getlength("Ж")
    assert font.getbbox("Привет")[2] > 60


def test_sheet_handler_renders_in_thread(tmp_path, monkeypatch):
    """Рендер Pillow идёт через asyncio.to_thread — event loop бота не блокируется."""
    client_with(tmp_path)
    _grant_checkin_to_game_manager()
    calls = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        calls.append(fn)
        return await real(fn, *a, **k)

    monkeypatch.setattr(admin_checkin_training.asyncio, "to_thread", spy)
    cb = _FakeCallback("checkin_training_sheet", GAME_MANAGER_ID)
    cb.bot = _SheetBot()
    _run(admin_checkin_training.checkin_training_sheet(cb))
    assert calls == [training.render_training_sheet]


class _SheetBot:
    def __init__(self):
        self.documents = []
        self.media = []

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.documents.append((chat_id, caption))

    async def send_media_group(self, chat_id, media, **kw):
        self.media.append((chat_id, media))


def test_sheet_button_requires_checkin_or_moderate_reg(tmp_path):
    client_with(tmp_path)
    # GAME_MANAGER_ID без checkin -> алерт, ничего не отправлено.
    cb = _FakeCallback("checkin_training_sheet", GAME_MANAGER_ID)
    cb.bot = _SheetBot()
    _run(admin_checkin_training.checkin_training_sheet(cb))
    assert cb.answers and cb.answers[0][1] is True
    assert cb.bot.documents == [] and cb.bot.media == []

    _grant_checkin_to_game_manager()
    cb2 = _FakeCallback("checkin_training_sheet", GAME_MANAGER_ID)
    cb2.bot = _SheetBot()
    _run(admin_checkin_training.checkin_training_sheet(cb2))
    assert cb2.bot.documents or cb2.bot.media
    if cb2.bot.documents:
        assert cb2.bot.documents[0][1].startswith("Лист учебных QR.")


def test_sheet_callback_key_is_mapped():
    from handlers.access.admin_caps import ANY_CAPABILITY, required_capability

    assert required_capability(callback_data="checkin_training_sheet") == ANY_CAPABILITY


def _staff_with(caps: str):
    from handlers.access.admin_caps import role_caps_key

    _run(bot_db.set_setting(role_caps_key("stats_manager"), caps))
    _run(bot_db.add_staff(951099, "stats_manager", 1))


@pytest.mark.parametrize("caps,allowed", [
    ("moderate_reg", True), ("checkin", True), ("stats", False),
])
def test_sheet_rights_checkin_or_moderate_reg(tmp_path, caps, allowed):
    client_with(tmp_path)
    _staff_with(caps)
    cb = _FakeCallback("checkin_training_sheet", 951099)
    cb.bot = _SheetBot()
    _run(admin_checkin_training.checkin_training_sheet(cb))
    assert bool(cb.bot.documents or cb.bot.media) is allowed
