"""Вкладка «Магазин»: имя кнопкой и снимок выдач. Токен брони на лист не попадает."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config import config
from database import db, shop_db

UID, MISSING, STAFF = 261121201, 261121299, 1
HEADERS = [
    "Когда", "Имя", "Username", "Telegram ID", "Товар", "Баллов",
    "Выдал (ID)", "Отменена", "Статус", "Код",
]


def _run(coro):
    return asyncio.run(coro)


def _env(tmp_path):
    config.DB_PATH = str(tmp_path / "shop_sheet.db")
    _run(db.init_db())
    _run(db.add_user({
        "telegram_id": UID, "full_name": "Иванова Анна", "username": "=cmd",
        "registration_date": "2026-01-01",
    }))
    _run(db.add_coins(UID, 500, reason="задания", changed_by=STAFF, source="manual"))
    _run(_set_nick(UID, "=cmd"))
    _run(db.add_coins(MISSING, 40, reason="без анкеты", changed_by=STAFF, source="manual"))
    _run(shop_db.set_open(True))
    _run(shop_db.set_reserve(True))


async def _set_nick(uid: int, nick: str) -> None:
    import aiosqlite

    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute("UPDATE users SET username = ? WHERE telegram_id = ?", (nick, uid))
        await conn.commit()


def _silence_sheet(monkeypatch):
    import services.sheets.shop_sheet as ss

    monkeypatch.setattr(ss, "request_shop_sheet_sync", lambda: None)


def test_admin_shop_import_enables_sync(monkeypatch):
    import services.sheets.shop_sheet as ss

    seen = []
    real = ss.enable_shop_sheet_sync

    def wrapped():
        seen.append(1)
        real()

    monkeypatch.setattr(ss, "enable_shop_sheet_sync", wrapped)
    if "handlers.game.admin_shop" not in sys.modules:
        import handlers.game.admin_shop  # noqa: F401
        assert seen == [1]
    assert ss._enabled is True
    assert ss._job.delay == 5
    assert ss._job._job is ss.rebuild_shop_sheet


def test_shop_sheet_tab_is_bot_only_rewrite():
    from domain.settings.ops import SHEET_TAB_NAME_KEYS, SHEET_TAB_WRITE_MODE, editable_keys
    from domain.settings.schema import SETTINGS_SCHEMA
    from domain.settings.synonyms import BOT_ONLY_SYNONYMS, SETTINGS_SYNONYMS
    from handlers.settings.admin_settings import _SHEETS_FIELD_ORDER

    spec = SETTINGS_SCHEMA["shop_sheet_tab"]
    assert spec["type"] == "text" and spec["group"] == "sheets"
    assert spec["default"] == "Магазин" and not spec.get("per_city")
    assert "Оставьте пустым — будет «Магазин»." in spec["prompt"]
    assert "пересобирает её целиком" in spec["prompt"]
    assert "shop_sheet_tab" in SHEET_TAB_NAME_KEYS
    assert SHEET_TAB_WRITE_MODE["shop_sheet_tab"] == "rewrite"
    assert "shop_sheet_tab" not in editable_keys()
    assert "shop_sheet_tab" not in SETTINGS_SYNONYMS
    coins = _SHEETS_FIELD_ORDER.index("game_coins_log_tab")
    assert _SHEETS_FIELD_ORDER[coins + 1] == "shop_sheet_tab"
    phrases = BOT_ONLY_SYNONYMS["shop_sheet_tab"]
    assert len(phrases) >= 2
    for phrase in phrases:
        assert phrase == phrase.lower() and "_" not in phrase and phrase != spec["label"]


def test_empty_name_is_default_tab_and_reconcile_keeps_it(tmp_path):
    from services.sheets.sheet_reconcile import _known_non_delegate_tab_titles
    from services.sheets.shop_sheet import tab_name

    _env(tmp_path)
    assert _run(tab_name()) == "Магазин"
    _run(db.set_setting("shop_sheet_tab", "   "))
    _run(db.set_setting("questions_sheet_tab", ""))
    assert _run(tab_name()) == "Магазин"
    titles = _run(_known_non_delegate_tab_titles())
    assert "Магазин" in titles and "Вопросы" not in titles
    _run(db.set_setting("shop_sheet_tab", "Витрина"))
    assert _run(tab_name()) == "Витрина"
    assert "Витрина" in _run(_known_non_delegate_tab_titles())


def test_request_without_enable_does_not_spawn():
    import services.sheets.shop_sheet as ss
    from services.infra.background import pending_count

    was = ss._enabled
    ss._enabled = False
    try:
        before = pending_count()
        ss.request_shop_sheet_sync()
        assert pending_count() == before
    finally:
        if was:
            ss.enable_shop_sheet_sync()


def test_request_when_sheets_off_does_not_spawn(monkeypatch):
    import services.sheets.shop_sheet as ss
    from services.infra.background import pending_count
    from services.sheets import sheet_target

    ss.enable_shop_sheet_sync()
    monkeypatch.setattr(sheet_target, "sheets_enabled", lambda: False)
    before = pending_count()
    ss.request_shop_sheet_sync()
    assert pending_count() == before


def test_rows_match_csv_and_hide_token(tmp_path, monkeypatch):
    from services.sheets.shop_sheet import HEADERS as sheet_headers
    from services.sheets.shop_sheet import rebuild_shop_sheet

    _env(tmp_path)
    _silence_sheet(monkeypatch)
    first = _run(shop_db.add_item("Блокнот", 10, None, None))
    formula = _run(shop_db.add_item("=Шоппер", 20, None, None))
    _run(shop_db.sell(first, UID, STAFF))
    issued = _run(shop_db.sell(formula, UID, STAFF))
    reserved = _run(shop_db.reserve(formula, UID))
    _run(shop_db.cancel_reservation(reserved["id"], STAFF))
    kept = _run(shop_db.reserve(formula, UID))
    _run(shop_db.issue_reservation(kept["id"], STAFF))
    _run(shop_db.sell(first, MISSING, STAFF))

    captured = {}

    async def fake(title, headers, rows):
        captured["title"] = title
        captured["headers"] = headers
        captured["rows"] = rows
        return len(rows)

    monkeypatch.setattr("services.sheets.sheets.sync_named_worksheet", fake)
    assert _run(rebuild_shop_sheet()) == 5
    assert captured["title"] == "Магазин"
    assert captured["headers"] == HEADERS == sheet_headers
    rows = captured["rows"]
    assert rows[0][4] == "Блокнот" and rows[0][2] == "" and rows[0][1] == ""
    assert rows[0][3] == MISSING and rows[0][8] == "выдано"
    issued_row = next(r for r in rows if r[4] == "=Шоппер" and r[6] == STAFF and r[9] == "")
    assert issued_row[2] == "=cmd" and not str(issued_row[2]).startswith("'")
    assert issued_row[4] == "=Шоппер" and not str(issued_row[4]).startswith("'")
    assert issued_row[8] == "выдано"
    cancelled = next(r for r in rows if r[7])
    assert cancelled[8] == "бронь" and cancelled[9]
    flat = [str(cell) for row in rows for cell in row]
    assert kept["token"] not in flat and "pickup_token" not in captured["headers"]
    assert rows[0][3] == MISSING
    assert issued["id"]
    assert rows[1][9] == kept["code"] or rows[2][9] == kept["code"]


def test_sell_returns_when_sheet_fails(tmp_path, monkeypatch):
    import services.sheets.shop_sheet as ss
    from services.sheets import sheet_target

    _env(tmp_path)
    item = _run(shop_db.add_item("Стикер", 10, None, None))

    def boom():
        raise RuntimeError("планировщик")

    monkeypatch.setattr(ss, "request_shop_sheet_sync", boom)
    sale = _run(shop_db.sell(item, UID, STAFF))
    assert sale["id"] and sale["title"] == "Стикер"

    monkeypatch.setattr(sheet_target, "sheets_enabled", lambda: False)
    assert _run(ss.rebuild_shop_sheet()) == -1

    async def explode(title, headers, rows):
        raise RuntimeError("google")

    monkeypatch.setattr("services.sheets.sheets.sync_named_worksheet", explode)
    monkeypatch.setattr(sheet_target, "sheets_enabled", lambda: True)
    assert _run(ss.rebuild_shop_sheet()) == -1


def test_miss_does_not_schedule_and_success_does(tmp_path, monkeypatch):
    import services.sheets.shop_sheet as ss

    _env(tmp_path)
    calls = []
    monkeypatch.setattr(ss, "request_shop_sheet_sync", lambda: calls.append(1))
    item = _run(shop_db.add_item("Стикер", 10, 5, None))
    assert _run(shop_db.cancel_reservation(999_999, STAFF)) is None
    assert _run(shop_db.issue_reservation(999_999, STAFF)) is None
    assert _run(shop_db.undo_sale(999_999, STAFF)) is None
    assert calls == []
    sale = _run(shop_db.sell(item, UID, STAFF))
    reserved = _run(shop_db.reserve(item, UID))
    assert len(calls) == 2
    assert _run(shop_db.cancel_reservation(reserved["id"], STAFF)) is not None
    assert _run(shop_db.undo_sale(sale["id"], STAFF)) is not None
    fresh = _run(shop_db.reserve(item, UID))
    assert _run(shop_db.issue_reservation(fresh["id"], STAFF)) is not None
    assert len(calls) == 6


def test_journal_button_rebuilds_in_human_words(tmp_path, monkeypatch):
    from handlers.access.admin_caps import required_capability
    from handlers.game import admin_shop as adm

    _env(tmp_path)
    _silence_sheet(monkeypatch)
    item = _run(shop_db.add_item("Стикер", 10, None, None))
    _run(shop_db.sell(item, UID, STAFF))
    assert required_capability(callback_data="shp_salessheet") == "moderate_game"

    async def fake(title, headers, rows):
        return len(rows)

    monkeypatch.setattr("services.sheets.sheets.sync_named_worksheet", fake)
    cb = SimpleNamespace(
        data="shp_sales:0", from_user=SimpleNamespace(id=STAFF),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock(), chat=None),
        answer=AsyncMock(),
    )
    _run(adm.shop_sales(cb))
    kb = cb.message.edit_text.call_args.kwargs["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "shp_salescsv" in datas and "shp_salessheet" in datas
    sheet_cb = SimpleNamespace(data="shp_salessheet", answer=AsyncMock())
    _run(adm.shop_sales_sheet(sheet_cb))
    assert sheet_cb.answer.await_args.args[0] == "Вкладка обновлена. Строк: 1"
    assert sheet_cb.answer.await_args.kwargs["show_alert"] is True

    async def off(title, headers, rows):
        return -1

    monkeypatch.setattr("services.sheets.sheets.sync_named_worksheet", off)
    _run(adm.shop_sales_sheet(sheet_cb))
    assert sheet_cb.answer.await_args.args[0] == "Таблица не подключена. Выдача в боте на месте."


def test_miniapp_build_enables_shop_sheet(tmp_path, monkeypatch):
    import services.sheets.shop_sheet as ss
    from dashboard.config import DashboardConfig
    from miniapp.main import _build_asgi_app

    seen = []
    real = ss.enable_shop_sheet_sync

    def wrapped():
        seen.append(1)
        real()

    monkeypatch.setattr(ss, "enable_shop_sheet_sync", wrapped)
    _build_asgi_app(DashboardConfig(
        db_path=str(tmp_path / "mini.db"),
        public_url="https://yl26.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token="123:ABC",
        admin_ids=(STAFF,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
    ))
    assert seen == [1]
