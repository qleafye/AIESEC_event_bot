"""Вкладка «Гейма»: три колонки итогов в конце (чат, вручную и прочее, итого баллов).

Матрица — чистая функция; итоги считает `db.coin_totals_by_user`, городские вкладки режут их
по `users.event_city` (как сдачи). Драйвим через asyncio.run (pytest-asyncio в окружении нет).
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod  # noqa: F401 -- seam-imports admin_gamification
from handlers.game import admin_gamification
import services.game.game_sheets as game_sheets
from tests._dbtpl import fast_init_db

ADMIN_ID = 961401
SUB_USER = 961402    # сдача + баллы
CHAT_USER = 961403   # только «+N» в чате
SPB_USER = 961404    # другой город

TAIL = ["Чат (+N)", "Вручную и прочее", "Итого баллов"]


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_sheet_totals_261014.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _seed():
    async def go():
        for uid, code in ((SUB_USER, "msk"), (CHAT_USER, "msk"), (SPB_USER, "spb")):
            await db.add_user({"telegram_id": uid, "username": f"u{uid}", "full_name": f"User {uid}",
                               "registration_date": "2026-08-01 10:00:00", "event_city": code})
        t = await db.create_task("Стенд", "Light", 10, "text", "2026-08-30 23:59:00", ADMIN_ID)
        await db.create_submission(t, SUB_USER, "text", "готово", "2026-08-14 10:00:00")
        await db.add_coins(SUB_USER, 10, reason="Задание", source="task")
        await db.add_coins(SUB_USER, 3, reason="чат", source="chat")
        await db.add_coins(SUB_USER, 5, reason="вручную", source="manual")
        await db.add_coins(SUB_USER, -2, reason="списание", source="manual")
        await db.add_coins(SUB_USER, 4, reason="старое")  # source NULL -> «прочее»
        await db.add_coins(CHAT_USER, 7, reason="чат", source="chat")
        await db.add_coins(CHAT_USER, -1, reason="чат", source="chat")
        await db.add_coins(SPB_USER, 6, reason="чат", source="chat")
    asyncio.run(go())


def _matrix(totals):
    tasks = [{"id": 1, "text": "Стенд", "title": "Стенд", "created_at": "2026-08-01 10:00:00"}]
    subs = [{"id": 1, "task_id": 1, "user_id": SUB_USER, "status": "approved", "coins_awarded": 10,
             "user_full_name": "User", "user_username": "u", "user_event_city": "msk"}]
    return admin_gamification._build_game_matrix(tasks, subs, totals)


def test_totals_columns_are_last_and_old_ones_stay(tmp_path):
    _db_ready(tmp_path)
    _seed()
    totals = asyncio.run(db.coin_totals_by_user())
    headers, rows = _matrix(totals)
    assert headers[:5] == ["telegram_id", "ФИО", "Город", "Юзернейм", "Стенд"]
    assert headers[5:] == TAIL


def test_user_with_only_chat_coins_gets_a_row_and_totals_match_balance(tmp_path):
    _db_ready(tmp_path)
    _seed()
    totals = asyncio.run(db.coin_totals_by_user())
    _headers, rows = _matrix(totals)
    by_uid = {r[0]: r for r in rows}
    assert CHAT_USER in by_uid
    chat_row = by_uid[CHAT_USER]
    assert chat_row[4] == "-"                      # задания — прочерк
    assert chat_row[5:] == [6, 0, 6]               # +7 -1 в чате, итого = баланс
    assert chat_row[1] == f"User {CHAT_USER}"      # ФИО из users

    sub_row = by_uid[SUB_USER]
    # чат 3; прочее = 5 - 2 + 4 = 7 (списание учтено, NULL тоже); итого 10 + 3 + 7 = 20
    assert sub_row[5:] == [3, 7, 20]
    for uid in (SUB_USER, CHAT_USER):
        assert by_uid[uid][-1] == asyncio.run(db.get_balance(uid))


def test_no_totals_means_zeros_not_crash():
    headers, rows = admin_gamification._build_game_matrix([], [])
    assert headers[-3:] == TAIL and rows == []


def test_city_tab_sees_only_own_people(tmp_path):
    _db_ready(tmp_path)
    _seed()
    totals = asyncio.run(db.coin_totals_by_user())
    spb = game_sheets.filter_totals_for_city(totals, "spb")
    assert set(spb) == {SPB_USER}
    assert set(game_sheets.filter_totals_for_city(totals, None)) == {SUB_USER, CHAT_USER, SPB_USER}
    _h, rows = admin_gamification._build_game_matrix([], [], spb)
    assert [r[0] for r in rows] == [SPB_USER] and rows[0][-3:] == [6, 0, 6]


def test_coins_journal_screen_points_to_sheet_totals(tmp_path):
    _db_ready(tmp_path)
    text, _kb = asyncio.run(admin_gamification._coins_journal_screen(offset=0))
    assert "во вкладке «Гейма»" in text and "«Чат (+N)»" in text and "«Итого баллов»" in text
