"""Уведомление менеджеру «🎮 Новая сдача по заданию «…»» называет задание его названием
(`db.task_title`), а не обрезанным текстом задания — и из бота, и из приложения (строка outbox).
Строка outbox от приложения прежней версии несёт только текст — название выводится из него.
Дайджест сдач заданий не называет вовсе: в нём только люди и счётчики."""
import asyncio

from database import db
from handlers import user_actions as ua_mod
from services import game_digest as gd
from services import scheduler as sched
from services.infra import miniapp_outbox
from tests.test_game_submit_digest_260822 import _capture_notify, _FakeScheduler
from tests.test_gamification_delegate_phase9 import (
    ADMIN_ID, FakeBot, FakeCallback, FakeMessage, _db_ready, _seed_delegate, _start_submission,
)
from tests.test_miniapp_outbox_job import _enqueue

TITLE = "Пост про форум"
TEXT = "Выложи пост про форум у себя на странице, отметь нас и пришли скрин поста целиком"


def _titled_task(title=TITLE):
    return asyncio.run(db.create_task(
        TEXT, "Light", 10, "text", "2026-12-31 23:59:00", ADMIN_ID, title=title,
    ))


def test_bot_submission_names_task_by_title(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state, _ = _start_submission(_titled_task())
    asyncio.run(ua_mod.receive_proof(FakeMessage(text="вот скрин"), FakeBot(), state))
    bot = FakeBot()
    asyncio.run(ua_mod.finalize_game_submission(FakeCallback("gs_done"), bot, state))

    (chat_id, text), = bot.sent
    assert chat_id == ADMIN_ID
    assert f"Новая сдача по заданию «{TITLE}»" in text
    assert "Выложи пост" not in text


def test_miniapp_outbox_row_names_task_by_title(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    _enqueue("submission_created", {
        "submission_id": 1, "user_id": 2, "task_id": 3,
        "task_text": TEXT, "task_title": TITLE, "submitter_name": "Ира",
    })
    assert asyncio.run(miniapp_outbox.drain(FakeBot())) == 1
    assert f"«{TITLE}»" in calls[0]["text"]
    assert "Выложи пост" not in calls[0]["text"]


def test_old_miniapp_outbox_row_without_title_gets_one_from_text(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    _enqueue("submission_created", {
        "submission_id": 1, "user_id": 2, "task_id": 3,
        "task_text": "Знакомство\nрасскажи о себе в чате", "submitter_name": "Ира",
    })
    assert asyncio.run(miniapp_outbox.drain(FakeBot())) == 1
    assert "«Знакомство»" in calls[0]["text"]
    assert "None" not in calls[0]["text"]


def test_digest_mode_lists_people_not_task_texts(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_submit_notify_mode", "digest"))
    _seed_delegate()
    calls = _capture_notify(monkeypatch)
    monkeypatch.setattr(sched, "_scheduler", _FakeScheduler())
    asyncio.run(gd.notify_submission(
        FakeBot(), submission_id=1, user_id=930902, task_id=3, task_title=TITLE, submitter_name="Ира",
    ))
    assert calls == []  # в режиме дайджеста — в очередь, без сообщения
    assert asyncio.run(gd.send_game_digest(None)) == 1
    text = calls[0]["text"]
    assert "Новые сдачи: 1" in text and "Delegate 930902" in text
    assert TITLE not in text and "Выложи пост" not in text
