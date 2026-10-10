"""Рейтинг чата: кого показывать и как подписывать.

Старый рейтинг показывал только делегатов сезона и города; живой стал показывать всех, кто
писал в чат, — людей без анкеты, прошлый сезон, посторонних, иногда голым telegram_id.
Теперь на странице есть переключатель «Только с анкетой» (анкета сезона страницы и города
чата): по формуле он включён по умолчанию, по правилам города — выключен (СПб считает всех в
чате). Подпись — @ник, иначе имя из Telegram, иначе «без ника»; голого id нет никогда.
"""
from __future__ import annotations

import asyncio

from config import config
from dashboard import chat_rating
from dashboard import db as dash_db
from database import db
from tests._dbtpl import fast_init_db
from tests.test_dashboard_chat_rating_260927 import (  # noqa: F401 — фикстура db_path
    CHAT,
    NOW,
    _by_id,
    _exec,
    _msg,
    _setting,
    db_path,
)

SPB_CHAT_DICT = {"chat_id": -1006666666661, "city": "spb", "title": "СПб", "label": "СПб"}


def _user(path, tid, *, season="YL 26/2", city=None):
    _exec(path, "INSERT INTO users (telegram_id, full_name, username, status, season, event_city) "
                "VALUES (?, ?, '-', 'approved', ?, ?)", (tid, f"ФИО {tid}", season, city))


def _formula(path, chat=CHAT, **kw):
    with dash_db.read_conn(path) as conn:
        return chat_rating.chat_rating(conn, chat, period="all", admin_ids=set(), now=NOW, **kw)


def test_formula_default_shows_only_people_with_application(db_path):  # noqa: F811
    _setting(db_path, "event_season", "YL 26/2")
    _user(db_path, 11)
    _user(db_path, 12, season="YL 26/1")  # прошлый сезон
    for mid, author in ((1, 11), (2, 12), (3, 13)):  # 13 — без анкеты
        _msg(db_path, mid, author, "2026-09-20 10:00:00")
    result = _formula(db_path)
    assert result["registered_only"] is True
    assert set(_by_id(result)) == {11}
    everyone = _formula(db_path, registered_only=False)
    assert everyone["registered_only"] is False
    assert set(_by_id(everyone)) == {11, 12, 13}


def test_page_season_applies_to_filter(db_path):  # noqa: F811
    _setting(db_path, "event_season", "YL 26/2")
    _user(db_path, 12, season="YL 26/1")
    _msg(db_path, 1, 12, "2026-09-20 10:00:00")
    assert set(_by_id(_formula(db_path, season="YL 26/1"))) == {12}
    assert _formula(db_path)["rows"] == []


def test_filter_uses_chat_city(db_path):  # noqa: F811
    _exec(db_path, "INSERT INTO cities (code, label, enabled, sort_order) VALUES "
                   "('msk', 'Москва', 1, 0), ('spb', 'СПб', 1, 1)")
    chat_id = SPB_CHAT_DICT["chat_id"]
    _user(db_path, 11, city="spb")
    _user(db_path, 12, city="msk")
    _msg(db_path, 1, 11, "2026-09-20 10:00:00", chat_id=chat_id)
    _msg(db_path, 2, 12, "2026-09-20 10:00:00", chat_id=chat_id)
    assert set(_by_id(_formula(db_path, chat=SPB_CHAT_DICT))) == {11}


def test_rules_mode_default_counts_everyone_in_chat(db_path):  # noqa: F811
    _exec(db_path, "INSERT INTO staff (telegram_id, role, added_at) VALUES (500, 'reg_manager', '2026-01-01')")
    chat_id = SPB_CHAT_DICT["chat_id"]
    _msg(db_path, 1, 500, "2026-09-20 10:00:00", chat_id=chat_id)
    _msg(db_path, 2, 13, "2026-09-20 10:05:00", reply_mid=1, reply_author=500, chat_id=chat_id)
    with dash_db.read_conn(db_path) as conn:
        result = chat_rating.rules_rating(conn, SPB_CHAT_DICT, period="all", admin_ids=set(), now=NOW)
        only_reg = chat_rating.rules_rating(conn, SPB_CHAT_DICT, period="all", admin_ids=set(),
                                            now=NOW, registered_only=True)
    assert result["registered_only"] is False
    assert {r["telegram_id"] for r in result["rows"]} == {13}
    assert only_reg["rows"] == []


def test_names_never_show_bare_id(db_path):  # noqa: F811
    _exec(db_path, "INSERT INTO chat_usernames (telegram_id, username, first_name) VALUES "
                   "(11, 'nick', 'Оля'), (12, NULL, 'Петя'), (13, NULL, NULL)")
    for mid, author in ((1, 11), (2, 12), (3, 13), (4, 14)):
        _msg(db_path, mid, author, "2026-09-20 10:00:00")
    rows = _by_id(_formula(db_path, registered_only=False))
    assert rows[11]["display_name"] == "@nick"
    assert rows[12]["display_name"] == "Петя"
    assert rows[13]["display_name"] == "без ника"
    assert rows[14]["display_name"] == "без ника"


def test_upsert_keeps_first_name_and_username_independently(tmp_path):
    config.DB_PATH = str(tmp_path / "names.db")
    fast_init_db()

    async def go():
        await db.upsert_chat_username(11, None, "Оля")
        await db.upsert_chat_username(11, "olya", None)
        await db.upsert_chat_username(11, None, None)  # пустое не стирает
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT username, first_name FROM chat_usernames WHERE telegram_id = 11"
            ) as cur:
                return await cur.fetchone()

    assert tuple(asyncio.run(go())) == ("olya", "Оля")


def test_group_message_without_username_stores_first_name(tmp_path):
    from datetime import datetime

    from aiogram.types import Chat, Message, User

    from handlers.chat import group_chat

    config.DB_PATH = str(tmp_path / "capture_names.db")
    config.ADMIN_IDS = [1]
    fast_init_db()
    chat_id = -1009285001

    async def go():
        await db.set_setting("chat_tracking_enabled", "on")
        await db.set_setting("delegate_chat_id", str(chat_id))
        msg = Message(message_id=5, date=datetime.now(), text="привет",
                      chat=Chat(id=chat_id, type="supergroup"),
                      from_user=User(id=11, is_bot=False, first_name="Оля"))
        await group_chat.on_group_message(msg)
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT username, first_name FROM chat_usernames WHERE telegram_id = 11"
            ) as cur:
                return await cur.fetchone()

    assert tuple(asyncio.run(go())) == (None, "Оля")


def test_chat_page_chip_toggles_and_keeps_period(tmp_path):
    from tests.test_dashboard_chat_260914 import (
        CHAT_ID, STATS_MANAGER_ID, _cfg, _client, _login, _seed, _use_tmp_db,
    )

    path = _use_tmp_db(tmp_path)
    _seed(
        staff=[(STATS_MANAGER_ID, "reg_manager", None)],
        settings={"role_caps_reg_manager": "moderate_reg;stats",
                  "delegate_chat_id": str(CHAT_ID), "delegate_chat_title": "Общий чат"},
    )
    _exec(path, "INSERT INTO chat_usernames (telegram_id, username) VALUES (1, 'outsider')")
    from dashboard import timeutil as dash_timeutil
    today = dash_timeutil.msk_now().strftime("%Y-%m-%d")
    _msg(path, 1, 1, f"{today} 10:00:00", chat_id=CHAT_ID)
    client = _client(_cfg(path))
    _login(client, STATS_MANAGER_ID)

    html = client.get("/chat", params={"period": "7d"}).text
    assert "Только с анкетой" in html and 'aria-pressed="true"' in html
    assert "@outsider" not in html
    assert 'href="/chat?period=7d&amp;reg=0"' in html

    html = client.get("/chat", params={"period": "7d", "reg": "0"}).text
    assert 'aria-pressed="false"' in html
    assert "@outsider" in html
    assert 'href="/chat?period=week&amp;reg=0"' in html  # выбор переживает смену периода
