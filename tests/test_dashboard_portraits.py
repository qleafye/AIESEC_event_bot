"""Сводки «ФОРУМ: ИТОГИ»: одобренные и дошедшие одного среза, без ПД и без NPS."""
from __future__ import annotations

import asyncio
import json

from config import config
from database import db
from database.db import _connect
from tests._dbtpl import fast_init_db

from dashboard.portraits import forum_portraits
from dashboard.queries import Scope

SEASON = "YL 26/2"
OTHER = "YL 26/1"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "portraits.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("event_city_enabled", "on"))
    return config.DB_PATH


def _conn():
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


async def _city():
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO cities (code, label, enabled, sort_order, created_at) VALUES "
            "('spb', 'Санкт-Петербург', 1, 1, '2026-01-01 00:00:00'), "
            "('msk', 'Москва', 1, 2, '2026-01-01 00:00:00')"
        )
        await conn.commit()


async def _user(tid, city, season=SEASON, status="approved", **fields):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season, event_city) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, f"Секретная Фамилия {tid}", status, season, city),
        )
        if fields:
            cols = ", ".join(f"{name} = ?" for name in fields)
            await conn.execute(
                f"UPDATE users SET {cols} WHERE telegram_id = ?",
                (*fields.values(), tid),
            )
        await conn.commit()


async def _entry(tid, day):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
            "VALUES (?, 'entry', ?, 'scan', ?, ?)",
            (tid, f"{day} 10:00:00", f"{day} 10:00:00", day),
        )
        await conn.commit()


def test_portraits_merge_aliases_and_keep_pii_out(tmp_path):
    _ready(tmp_path)
    _run(_city())
    goal = (
        "Найти возможность трудоустройства, "
        "Пообщаться с людьми из моей сферы, нетворкинг"
    )
    _run(_user(
        1, "msk", course="3", study_field="Бизнес и управление",
        university="ВШЭ", goal=goal, source="website_1", source_from_tag=1,
        education_status="Да, в ВУЗе или колледже", age=19, participant_type="full",
    ))
    _run(_user(
        2, "msk", course="1", study_field="Экономика",
        university="НИУ ВШЭ", goal="Хочу котиков", source="Узнал от блогера",
        education_status="Нет, завершил(а) обучение", age=22, participant_type="short",
    ))
    _run(_user(3, "spb", university="СПбГУ", course="2", participant_type="full"))
    _run(_user(4, "msk", season=OTHER, university="МГУ", course="4"))
    _run(_user(5, "msk", status="rejected", university="ВШЭ", course="3"))
    _run(_entry(1, "2026-10-30"))
    _run(_entry(1, "2026-10-31"))
    _run(_entry(5, "2026-10-30"))

    with _conn() as conn:
        block = forum_portraits(conn, Scope(city="msk", season=SEASON))

    raw = json.dumps(block, ensure_ascii=False)
    assert "Секретная Фамилия" not in raw
    assert "Хочу котиков" not in raw
    assert block["approved"] == 2
    assert block["arrived"] == 1
    assert block["outside"] == 1
    assert "30.10.2026" in block["days_text"]
    assert "Оба дня: 1" in block["days_text"]

    cuts = {cut["id"]: cut for cut in block["cuts"]}
    uni = {row["label"]: row for row in cuts["uni"]["rows"]}
    assert uni["НИУ ВШЭ"]["reg"] == 2
    assert uni["НИУ ВШЭ"]["arr"] == 1
    assert "СПбГУ" not in uni
    assert "МГУ" not in uni

    goals = {row["label"]: row for row in cuts["goal"]["rows"]}
    assert goals["Найти возможность трудоустройства"]["reg"] == 1
    assert goals["Пообщаться с людьми из моей сферы, нетворкинг"]["reg"] == 1
    assert goals["Прокачать свои hard и soft skills"]["reg"] == 0
    assert any("1 одобренный" in line for line in cuts["goal"]["footer"])

    fields = {row["label"]: row for row in cuts["field"]["rows"]}
    assert fields["Бизнес и управление"]["reg"] == 1
    assert any(row["label"].startswith("остальные свои формулировки") for row in cuts["field"]["rows"])
    assert "Экономика" not in fields

    sources = {row["label"]: row for row in cuts["source"]["rows"]}
    assert sources["по ссылке: website_1"]["arr"] == 1
    assert sources["Узнал от блогера"]["reg"] == 1

    tracks = {row["label"]: row for row in cuts["track"]["rows"]}
    assert tracks["полная регистрация"]["arr"] == 1
    assert tracks["краткая форма"]["reg"] == 1
    assert block["nps"]["asked"] is False


def test_scores_skip_zero_and_bot_survey(tmp_path):
    _ready(tmp_path)
    _run(_city())
    _run(_user(1, "msk"))
    _run(_user(2, "spb"))
    _run(_user(6, "msk"))

    async def _seed():
        async with _connect() as conn:
            await conn.execute(
                "INSERT INTO session_feedback (telegram_id, session_id, rating, prompted_at) "
                "VALUES (1, 10, 5, '2026-10-31 12:00:00'), "
                "(1, 11, 0, '2026-10-31 12:00:00')"
            )
            cur = await conn.execute(
                "INSERT INTO polls (question, options_json, is_anonymous, allows_multiple, "
                "created_at, status, kind) VALUES "
                "('День 1', '[]', 0, 0, '2026-10-30 09:00:00', 'closed', 'survey')"
            )
            day = cur.lastrowid
            bot = (await conn.execute(
                "INSERT INTO polls (question, options_json, is_anonymous, allows_multiple, "
                "created_at, status, kind) VALUES "
                "('Как тебе бот', '[]', 0, 0, '2026-10-30 09:00:00', 'closed', 'survey')"
            )).lastrowid
            venue = (await conn.execute(
                "INSERT INTO survey_questions (poll_id, position, qtype, text, required, "
                "options_json, rows_json) VALUES (?, 1, 'table', "
                "'Что на площадке работало', 1, '[]', ?)",
                (day, json.dumps(["Регистрация и вход", "Перекусы и напитки"], ensure_ascii=False)),
            )).lastrowid
            mood = (await conn.execute(
                "INSERT INTO survey_questions (poll_id, position, qtype, text, required, "
                "options_json, rows_json) VALUES (?, 2, 'single', 'Как после первого дня', 1, ?, '[]')",
                (day, json.dumps([{"t": "🔥 Хочу ещё"}, {"t": "Устал"}], ensure_ascii=False)),
            )).lastrowid
            bot_q = (await conn.execute(
                "INSERT INTO survey_questions (poll_id, position, qtype, text, required, "
                "options_json, rows_json) VALUES (?, 1, 'table', "
                "'Насколько удобно было пользоваться ботом', 1, '[]', ?)",
                (bot, json.dumps(["QR-код на вход"], ensure_ascii=False)),
            )).lastrowid
            await conn.execute(
                "INSERT INTO survey_answers (poll_id, user_id, question_id, sub, value_json) VALUES "
                "(?, 1, ?, 'r0', '5'), (?, 6, ?, 'r0', '0'), (?, 1, ?, 'r1', '3'), "
                "(?, 1, ?, '', '0'), (?, 2, ?, 'r0', '1'), (?, 1, ?, 'r0', '5')",
                (day, venue, day, venue, day, venue, day, mood, day, venue, bot, bot_q),
            )
            await conn.execute(
                "INSERT INTO survey_progress (poll_id, user_id, state, started_at, finished_at) "
                "VALUES (?, 1, 'done', '2026-10-30 18:00:00', '2026-10-30 18:10:00')",
                (day,),
            )
            await conn.commit()

    _run(_seed())
    with _conn() as conn:
        block = forum_portraits(conn, Scope(city="msk", season=SEASON))

    sessions = block["nps"]["sessions"]
    assert sessions["prompted"] == 2
    assert sessions["rated"] == 1
    assert sessions["avg"] == "5,00"
    titles = " ".join(group["title"] for group in block["nps"]["groups"])
    assert "площадке" in titles
    assert "ботом" not in titles
    venue = block["nps"]["groups"][0]
    by_label = {row["label"]: row for row in venue["rows"]}
    assert by_label["Регистрация и вход"]["n"] == 1
    assert by_label["Регистрация и вход"]["avg"] == "5,00"
    assert by_label["Перекусы и напитки"]["n"] == 1
    assert by_label["Перекусы и напитки"]["avg"] == "3,00"
    assert "1 из 1" in venue["note"]
    choice = block["nps"]["choices"][0]
    assert choice["total"] == 1
    assert choice["rows"][0]["label"].endswith("Хочу ещё")
    assert choice["rows"][0]["n"] == 1
    assert choice["rows"][1]["n"] == 0
