"""Вкладка «Журнал баллов»: запрос, построитель строк, план вкладок и пересборка.

Драйвим через asyncio.run (pytest-asyncio в окружении нет).
"""
import asyncio

from config import config
from database import chat_coins_db, db
from handlers import admin as admin_mod  # noqa: F401 -- seam-imports admin_gamification
from services.game import coins_log_sheet as cls
from services.game import game_sheets
from tests._dbtpl import fast_init_db

ADMIN_ID = 971401
MSK_USER = 971402
SPB_USER = 971403
GHOST = 971404   # нет строки users, только chat_usernames
CHAT_ID = -1001234567890


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_coins_log_261014.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _seed():
    async def go():
        for uid, code in ((MSK_USER, "msk"), (SPB_USER, "spb")):
            await db.add_user({"telegram_id": uid, "username": f"u{uid}", "full_name": f"User {uid}",
                               "registration_date": "2026-08-01 10:00:00", "event_city": code})
        await db.add_user({"telegram_id": ADMIN_ID, "username": "boss", "full_name": "Boss",
                           "registration_date": "2026-08-01 10:00:00", "event_city": "msk"})
        async with db._connect() as c:
            await c.execute(
                "INSERT INTO chat_usernames (telegram_id, username, first_name, updated_at) "
                "VALUES (?, ?, ?, ?)", (GHOST, "ghost", "Призрак", "2026-08-01 10:00:00"))
            await c.commit()
        await db.add_coins(MSK_USER, 5, reason="вручную", changed_by=ADMIN_ID, source="manual")
        await db.add_coins(MSK_USER, -3, reason="списание", changed_by=ADMIN_ID, source="manual")
        await db.add_coins(SPB_USER, 10, reason="Задание", source="task")
        await db.add_coins(SPB_USER, 2, reason="приглашение", source="referral")
        await db.add_coins(MSK_USER, 1, reason="старое")
        await chat_coins_db.record_transfer([(MSK_USER, 7, "перенос")], ADMIN_ID)
        await chat_coins_db.claim_chat_award(
            chat_id=CHAT_ID, message_id=42, user_id=GHOST, amount=4, reason="+4 в чате",
            awarded_by=ADMIN_ID, award_message_id=43)
    asyncio.run(go())


def test_empty_log_has_exact_headers():
    headers, rows = cls.build_coins_log([], {})
    assert headers == ["Когда", "Кому", "@ник", "telegram_id", "Город", "Баллы", "Откуда",
                       "Кто начислил", "За что", "Ссылка на сообщение"]
    assert rows == []


def test_source_labels_and_unknown():
    for src, label in (("chat", "Чат"), ("manual", "Вручную"), ("transfer", "Перенос из таблицы"),
                       ("task", "За задание"), ("referral", "За приглашённого"),
                       ("referral_reversal", "Отмена за приглашённого"),
                       (None, "Старая запись"), ("weird", "Прочее")):
        _h, rows = cls.build_coins_log([{"source": src, "delta": 1}], {})
        assert rows[0][6] == label


def test_row_cells():
    entry = {"timestamp": "2026-10-14 18:54:07", "user_id": 5, "delta": -3, "source": "manual",
             "user_full_name": None, "user_chat_first_name": "Имя", "user_username": "u",
             "user_event_city": "spb", "changed_by": 9, "changer_full_name": "Ф И",
             "changer_username": "@m", "reason": "причина"}
    _h, rows = cls.build_coins_log([entry], {"spb": "СПб"})
    r = rows[0]
    assert r[0] == "14.10 18:54"
    assert r[1] == "Имя" and r[2] == "@u" and r[3] == 5 and r[4] == "СПб"
    assert r[5] == -3 and isinstance(r[5], int)
    assert r[7] == "Ф И (@m)" and r[8] == "причина" and r[9] == ""
    _h, rows = cls.build_coins_log([{"timestamp": "мусор", "user_event_city": "xx", "changed_by": 7},
                                    {"timestamp": None, "changed_by": None}], {})
    assert rows[0][0] == "мусор" and rows[0][4] == "xx" and rows[0][7] == "7"
    assert rows[1][0] == "-" and rows[1][4] == "-" and rows[1][7] == "-" and rows[1][1] == "-"


def test_chat_message_link():
    assert cls.chat_message_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"
    assert cls.chat_message_link(-12345, 1) == ""
    assert cls.chat_message_link(12345, 1) == ""
    assert cls.chat_message_link(None, 1) == ""
    assert cls.chat_message_link(-1001234567890, None) == ""


def test_list_coins_log_and_balances(tmp_path):
    _db_ready(tmp_path)
    _seed()
    entries = asyncio.run(chat_coins_db.list_coins_log())
    assert len(entries) == 7
    assert [e["id"] for e in entries] == sorted((e["id"] for e in entries), reverse=True)
    chat = entries[0]
    assert chat["source"] == "chat" and chat["award_chat_id"] == CHAT_ID and chat["award_message_id"] == 42
    assert chat["user_chat_first_name"] == "Призрак"
    assert all(e["award_chat_id"] is None for e in entries[1:])
    headers, rows = cls.build_coins_log(entries, {})
    assert rows[0][9] == "https://t.me/c/1234567890/42"
    assert rows[0][1] == "Призрак" and rows[0][2] == "@ghost"
    for uid in (MSK_USER, SPB_USER, GHOST):
        total = sum(r[5] for r in rows if r[3] == uid)
        assert total == asyncio.run(db.get_balance(uid))


def test_filter_for_city():
    rows = [{"user_event_city": "spb"}, {"user_event_city": None}, {"user_event_city": "msk"}]
    assert game_sheets.filter_coins_log_for_city(rows, None) == rows
    assert game_sheets.filter_coins_log_for_city(rows, "spb") == [rows[0]]
    default = asyncio.run(_default_city())
    got = game_sheets.filter_coins_log_for_city(rows, default)
    assert rows[1] in got


async def _default_city():
    from domain.cities import normalize_city
    return normalize_city(None)



# ── пересборка и экраны ─────────────────────────────────────────────────────────────────────

def _fake_writer(monkeypatch, written):
    from handlers.game import admin_gamification

    async def _fake_sync(title, headers, rows):
        written[title] = (headers, rows)
        return len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)
    return admin_gamification


def test_describe_plan_mentions_journal(tmp_path):
    _db_ready(tmp_path)
    plan = asyncio.run(game_sheets.game_tab_plan())
    lines = game_sheets.describe_plan(plan)
    assert "«Журнал баллов» — каждое начисление и списание баллов" in lines
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    plan = asyncio.run(game_sheets.game_tab_plan())
    lines = game_sheets.describe_plan(plan)
    assert any(l.startswith("«Журнал баллов»") and l.endswith(", все города") for l in lines)
    assert any("«СПб Журнал баллов»" in l for l in lines)


def test_rebuild_writes_journal_tab(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _seed()
    written = {}
    ag = _fake_writer(monkeypatch, written)
    result = asyncio.run(ag.rebuild_game_sheets())
    assert len(result) == 3
    headers, rows = written["Журнал баллов"]
    assert headers == cls.COINS_LOG_HEADERS
    assert len(rows) == 7
    assert rows[0][9] == "https://t.me/c/1234567890/42"    # последняя запись сверху


def test_rebuild_city_journal_only_that_city(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed()
    written = {}
    ag = _fake_writer(monkeypatch, written)
    asyncio.run(ag.rebuild_game_sheets())
    _h, spb_rows = written["СПб Журнал баллов"]
    assert {r[3] for r in spb_rows} == {SPB_USER}
    assert len(spb_rows) == 2
    assert len(written["Журнал баллов"][1]) == 7


def test_coins_journal_screen_points_to_journal_tab(tmp_path):
    from handlers.game import admin_gamification
    _db_ready(tmp_path)
    text, _kb = asyncio.run(admin_gamification._coins_journal_screen())
    assert "во вкладке «Журнал баллов»" in text
    asyncio.run(db.set_setting("game_coins_log_tab", "🤖 Журнал баллов"))
    text, _kb = asyncio.run(admin_gamification._coins_journal_screen())
    assert "во вкладке «🤖 Журнал баллов»" in text
