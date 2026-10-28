"""Раздел «Опросы» на странице «🎪 Статистика форума»: итоги опросов и анкет без ПД.

`dashboard/polls_block.py` повторяет SQL бота (контейнер дашборда не видит `database/` и
`services/`), поэтому здесь паритет «дашборд == бот»: обычный опрос сверяется с
`database.db.get_poll_results`, анкета — с `services.comms.survey_report.build_survey_results`.
Фикстура — прямые INSERT через `database.db._connect()`, чтение — read-only sqlite3, как в
`tests/test_dashboard_forum_stats_260926.py`.
"""
from __future__ import annotations

import asyncio
import json

from database import db
from database import survey_db as sdb
from database.db import _connect
from services.comms import survey_report
from tests.test_arrival_stats_260924 import _user
from tests.test_dashboard_forum_stats_260926 import SEASON, _conn, _ready, _seed_cities

from dashboard import polls_block as pb
from dashboard.queries import Scope

ADMIN_ID = 926100


def _run(coro):
    return asyncio.run(coro)


def _block(scope=None):
    conn = _conn()
    try:
        return pb.polls_block(conn, scope or Scope(season=SEASON))
    finally:
        conn.close()


async def _exec(sql, params=()):
    async with _connect() as conn:
        await conn.execute(sql, params)
        await conn.commit()


async def _poll(question, options, *, anonymous=False, city=None, status="open"):
    pid = await db.create_poll(
        question, options, is_anonymous=anonymous, allows_multiple=False,
        created_by=ADMIN_ID, city=city, audience=None, scheduled_at="2026-10-01 10:00:00",
    )
    await _exec("UPDATE polls SET status = ?, created_at = '2026-10-01 09:00:00' WHERE id = ?",
                (status, pid))
    return pid


async def _deliver(pid, chat_ids, *, status="ok", totals=None):
    for cid in chat_ids:
        await _exec(
            "INSERT INTO poll_messages (poll_id, chat_id, telegram_poll_id, message_id, status, "
            "totals_json) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, cid, f"tg{pid}_{cid}", 100 + cid if status == "ok" else None, status,
             json.dumps(totals) if totals is not None else None),
        )


async def _answer(pid, uid, option_ids):
    await db.upsert_poll_answer(pid, uid, option_ids)


async def _progress(pid, uid, state):
    await _exec(
        "INSERT INTO survey_progress (poll_id, user_id, current_qid, state, started_at) "
        "VALUES (?, ?, NULL, ?, '2026-10-01 10:00:00')",
        (pid, uid, state),
    )


async def _survey(title="Анкета после форума", city=None):
    pid = await sdb.create_survey_draft(title, ADMIN_ID, city)
    q_single = await sdb.add_survey_question(
        pid, "single", "Как добрались?",
        options=[{"t": "Поезд", "more": "Какой?"}, {"t": "Самолёт"}, {"t": "Автобус"}])
    q_multi = await sdb.add_survey_question(
        pid, "multi", "Что понравилось?",
        options=[{"t": "Лекции"}, {"t": "Кейсы"}, {"t": "Вечеринка"}])
    q_scale = await sdb.add_survey_question(pid, "scale", "Оцените форум")
    q_table = await sdb.add_survey_question(pid, "table", "Оцените блоки", rows=["Утро", "Вечер"])
    q_text = await sdb.add_survey_question(pid, "text", "Что улучшить?")
    await _exec("UPDATE polls SET status = 'open' WHERE id = ?", (pid,))
    return pid, {"single": q_single, "multi": q_multi, "scale": q_scale,
                 "table": q_table, "text": q_text}


async def _survey_answers(pid, q):
    """Три делегата: 1 и 2 закончили, 3 начал и ждёт свободный текст (awaiting_text),
    4 — только начал (active)."""
    await _progress(pid, 1, "done")
    await _progress(pid, 2, "done")
    await _progress(pid, 3, "awaiting_text")
    await _progress(pid, 4, "active")
    up = sdb.upsert_survey_answer
    await up(pid, 1, q["single"], "", 0)
    await up(pid, 1, q["single"], "m0", "Сапсан")
    await up(pid, 2, q["single"], "", 1)
    await up(pid, 3, q["single"], "", 0)
    await up(pid, 1, q["multi"], "", [0, 1])
    await up(pid, 2, q["multi"], "", [1])
    await up(pid, 1, q["scale"], "", 5)
    await up(pid, 2, q["scale"], "", 4)
    await up(pid, 3, q["scale"], "", 4)
    await up(pid, 1, q["table"], "r0", 5)
    await up(pid, 1, q["table"], "r1", 3)
    await up(pid, 2, q["table"], "r0", 4)
    await up(pid, 2, q["table"], "r1", 0)  # «затрудняюсь ответить» — не оценка
    await up(pid, 1, q["text"], "", "Больше кофе")
    await up(pid, 2, q["text"], "", "Длиннее перерывы")


# ── пусто ────────────────────────────────────────────────────────────────────────────────

def test_empty_db_returns_none(tmp_path):
    _ready(tmp_path)
    assert _block() is None


def test_only_draft_and_scheduled_return_none(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    pid = _run(_poll("Черновик", ["А", "Б"], status="scheduled"))
    _run(_deliver(pid, [1]))
    _run(sdb.create_survey_draft("Черновик анкеты", ADMIN_ID))
    assert _block() is None


def test_no_polls_table_returns_none(tmp_path):
    import sqlite3

    path = tmp_path / "bare.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT)")
    try:
        assert pb.polls_block(conn, Scope()) is None
    finally:
        conn.close()


# ── обычный опрос ──────────────────────────────────────────────────────────────────────────

def test_regular_poll_answered_of_sent_and_options_match_bot(tmp_path):
    _ready(tmp_path)
    for uid in (1, 2, 3, 4):
        _run(_user(uid))
    pid = _run(_poll("Придёте на вечеринку?", ["Да", "Нет", "Не знаю"]))
    _run(_deliver(pid, [1, 2, 3]))
    _run(_deliver(pid, [4], status="failed"))
    _run(_answer(pid, 1, [0]))
    _run(_answer(pid, 2, [0]))

    block = _block()
    assert block["total"] == 1 and block["shown"] == 1
    item = block["items"][0]
    assert item["title"] == "Придёте на вечеринку?"
    assert item["kind_label"] == "Опрос"
    assert item["status_label"] == db.POLL_STATUS_LABELS["open"]
    assert item["created"] == "01.10.2026"
    assert item["sent"] == 3 and item["answered"] == 2
    assert item["anonymous"] is False

    bot = _run(db.get_poll_results(pid))
    assert [o["count"] for o in item["options"]] == bot["counts"]
    assert item["answered"] == bot["respondents"] and item["sent"] == bot["delivered"]
    assert [o["label"] for o in item["options"]] == ["Да", "Нет", "Не знаю"]
    assert [o["pct"] for o in item["options"]] == [100, 0, 0]


def test_anonymous_poll_sums_telegram_totals(tmp_path):
    _ready(tmp_path)
    for uid in (1, 2):
        _run(_user(uid))
    pid = _run(_poll("Анонимно", ["А", "Б"], anonymous=True))
    _run(_deliver(pid, [1], totals={"total": 1, "options": [1, 0]}))
    _run(_deliver(pid, [2], totals={"total": 1, "options": [0, 1]}))

    item = _block()["items"][0]
    bot = _run(db.get_poll_results(pid))
    assert item["anonymous"] is True
    assert item["answered"] == bot["respondents"] == 2
    assert [o["count"] for o in item["options"]] == bot["counts"] == [1, 1]
    assert [o["pct"] for o in item["options"]] == [50, 50]


def test_poll_with_zero_votes_has_zero_percent(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    pid = _run(_poll("Никто не ответил", ["А", "Б"], status="closed"))
    _run(_deliver(pid, [1]))
    item = _block()["items"][0]
    assert item["answered"] == 0 and item["sent"] == 1
    assert [o["pct"] for o in item["options"]] == [0, 0]
    assert item["status_label"] == db.POLL_STATUS_LABELS["closed"]


# ── город ──────────────────────────────────────────────────────────────────────────────────

def test_city_scope_narrows_recipients_and_hides_other_city_polls(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_user(1, city="spb"))
    _run(_user(2, city="spb"))
    _run(_user(3, city="msk"))
    common = _run(_poll("Всем городам", ["А", "Б"]))
    _run(_deliver(common, [1, 2, 3]))
    _run(_answer(common, 1, [0]))
    _run(_answer(common, 3, [1]))
    msk_only = _run(_poll("Только Москва", ["А", "Б"], city="msk"))
    _run(_deliver(msk_only, [3]))
    msk_recipients = _run(_poll("Всем, но ушёл только в Москву", ["А", "Б"]))
    _run(_deliver(msk_recipients, [3]))

    block = _block(Scope(city="spb", season=SEASON))
    titles = [i["title"] for i in block["items"]]
    assert titles == ["Всем городам"]
    item = block["items"][0]
    assert item["sent"] == 2 and item["answered"] == 1
    assert [o["count"] for o in item["options"]] == [1, 0]

    whole = _block(Scope(season=SEASON))
    assert {i["title"] for i in whole["items"]} == {
        "Всем городам", "Только Москва", "Всем, но ушёл только в Москву"}
    common_all = next(i for i in whole["items"] if i["title"] == "Всем городам")
    assert common_all["sent"] == 3 and common_all["answered"] == 2


def test_city_scope_without_any_recipients_returns_none(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_user(3, city="msk"))
    pid = _run(_poll("Москва", ["А", "Б"]))
    _run(_deliver(pid, [3]))
    assert _block(Scope(city="spb", season=SEASON)) is None


# ── сезон ──────────────────────────────────────────────────────────────────────────────────

def test_past_season_is_flagged(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    pid = _run(_poll("Опрос", ["А", "Б"]))
    _run(_deliver(pid, [1]))
    assert _block(Scope(season="YL'25")) == {"past_season": True}
    assert _block(Scope(season=None))["total"] == 1
    assert _block(Scope(season=SEASON))["total"] == 1


# ── анкета ─────────────────────────────────────────────────────────────────────────────────

def test_survey_numbers_match_bot_build_survey_results(tmp_path):
    _ready(tmp_path)
    for uid in (1, 2, 3, 4):
        _run(_user(uid))
    pid, q = _run(_survey())
    _run(_deliver(pid, [1, 2, 3, 4]))
    _run(_survey_answers(pid, q))

    item = _block()["items"][0]
    assert item["kind_label"] == "Анкета"
    assert item["sent"] == 4
    assert item["answered"] == 2
    # «Начали и не закончили» — все, кто не 'done': и 'active', и 'awaiting_text'.
    assert item["started_not_done"] == 2

    bot = _run(survey_report.build_survey_results(pid))
    assert len(item["questions"]) == len(bot)
    for mine, ref in zip(item["questions"], bot):
        assert mine["text"] == ref["q"]["text"]
        assert mine["type"] == ref["type"]
        assert mine["respondents"] == ref["respondents"]
        assert mine["total_texts"] == ref["texts"]
        if ref["type"] in ("single", "multi"):
            assert [o["count"] for o in mine["options"]] == ref["counts"]
            assert [o["label"] for o in mine["options"]] == [o["t"] for o in ref["q"]["options"]]
        elif ref["type"] == "scale":
            assert [d["count"] for d in mine["dist"]] == ref["dist"]
            assert mine["avg_label"] == survey_report._fmt_avg(ref["avg"])
        elif ref["type"] == "table":
            assert [r["label"] for r in mine["rows"]] == [r["name"] for r in ref["rows"]]
            assert [r["n"] for r in mine["rows"]] == [r["n"] for r in ref["rows"]]
            assert [r["avg_label"] for r in mine["rows"]] == [
                survey_report._fmt_avg(r["avg"]) for r in ref["rows"]]

    scale = next(x for x in item["questions"] if x["type"] == "scale")
    assert scale["avg_label"] == "4,3"
    single = next(x for x in item["questions"] if x["type"] == "single")
    assert single["texts"] == [{"text": "Сапсан", "note": "уточнение к «Поезд»"}]
    text_q = next(x for x in item["questions"] if x["type"] == "text")
    assert {t["text"] for t in text_q["texts"]} == {"Больше кофе", "Длиннее перерывы"}
    assert all(t["note"] == "" for t in text_q["texts"])


def test_survey_awaiting_text_counts_as_started_not_done(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    _run(_user(2))
    pid, _q = _run(_survey())
    _run(_deliver(pid, [1, 2]))
    _run(_progress(pid, 1, "awaiting_text"))
    _run(_progress(pid, 2, "done"))
    item = _block()["items"][0]
    assert item["answered"] == 1
    assert item["started_not_done"] == 1


def test_survey_city_scope_counts_only_city_people(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_user(1, city="spb"))
    _run(_user(2, city="msk"))
    _run(_user(3, city="spb"))
    _run(_user(4, city="msk"))
    pid, q = _run(_survey())
    _run(_deliver(pid, [1, 2, 3, 4]))
    _run(_survey_answers(pid, q))

    item = _block(Scope(city="spb", season=SEASON))["items"][0]
    assert item["sent"] == 2
    assert item["answered"] == 1  # 1 закончил, 3 ждёт текст
    assert item["started_not_done"] == 1
    single = next(x for x in item["questions"] if x["type"] == "single")
    assert [o["count"] for o in single["options"]] == [2, 0, 0]
    text_q = next(x for x in item["questions"] if x["type"] == "text")
    assert [t["text"] for t in text_q["texts"]] == ["Больше кофе"]


# ── свободные ответы ───────────────────────────────────────────────────────────────────────

def test_free_texts_last_five_truncated_and_no_personal_data(tmp_path):
    _ready(tmp_path)
    for uid in range(1, 8):
        _run(_user(uid))
    _run(_exec("UPDATE users SET username = 'secret_nick_' || telegram_id"))
    pid = _run(sdb.create_survey_draft("Свободные ответы", ADMIN_ID))
    qid = _run(sdb.add_survey_question(pid, "text", "Что улучшить?"))
    _run(_exec("UPDATE polls SET status = 'closed' WHERE id = ?", (pid,)))
    _run(_deliver(pid, list(range(1, 8))))
    long_text = "Д" * 400
    for uid in range(1, 8):
        _run(_progress(pid, uid, "done"))
        _run(sdb.upsert_survey_answer(pid, uid, qid, "", long_text if uid == 7 else f"Ответ {uid}"))
        _run(_exec(
            "UPDATE survey_answers SET answered_at = ? WHERE poll_id = ? AND user_id = ?",
            (f"2026-10-02 10:0{uid}:00", pid, uid),
        ))

    block = _block()
    q = block["items"][0]["questions"][0]
    assert q["total_texts"] == 7
    assert len(q["texts"]) == 5
    assert q["texts"][0]["text"] == "Д" * 299 + "…"
    assert [t["text"] for t in q["texts"][1:]] == ["Ответ 6", "Ответ 5", "Ответ 4", "Ответ 3"]
    dump = repr(block)
    for uid in range(1, 8):
        assert f"Тест {uid}" not in dump
        assert f"secret_nick_{uid}" not in dump
    assert "2026-10-02" not in dump


def test_free_text_keys_have_no_person_fields(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    pid, q = _run(_survey())
    _run(_deliver(pid, [1]))
    _run(_progress(pid, 1, "done"))
    _run(sdb.upsert_survey_answer(pid, 1, q["text"], "", "Текст"))
    item = _block()["items"][0]
    forbidden = {"user_id", "telegram_id", "full_name", "username", "event_city", "answered_at"}
    for question in item["questions"]:
        for t in question["texts"]:
            assert set(t) == {"text", "note"}
    assert not forbidden & set(item)


# ── лимит ──────────────────────────────────────────────────────────────────────────────────

def test_more_than_ten_polls_shows_ten_newest(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    ids = []
    for n in range(12):
        pid = _run(_poll(f"Опрос {n}", ["А", "Б"]))
        _run(_deliver(pid, [1]))
        ids.append(pid)
    block = _block()
    assert block["total"] == 12 and block["shown"] == 10
    assert [i["title"] for i in block["items"]] == [f"Опрос {n}" for n in range(11, 1, -1)]


# ── сторож дрейфа подписей ─────────────────────────────────────────────────────────────────

def test_status_labels_match_bot():
    for status in ("open", "sending", "closed"):
        assert pb._STATUS_LABELS[status] == db.POLL_STATUS_LABELS[status]
    assert set(pb._STATUS_LABELS) == {"open", "sending", "closed"}


def test_dashboard_default_matches_registry():
    from dashboard.queries import _SETTING_DEFAULTS
    from domain.settings.schema import SETTINGS_SCHEMA

    assert _SETTING_DEFAULTS["dashboard_block_polls"] == SETTINGS_SCHEMA["dashboard_block_polls"]["default"] == "on"
