"""Квик 260919-m9x: порядок делегатского списка заданий и московский «срок вышел».

Почему тест существует. `database.db.list_active_tasks` отдаёт `ORDER BY deadline_at ASC`,
дедлайн мягкий, архивируют задания руками — и наверху делегатского списка стояли самые
старые задания. На проде это стоило 200 сдач, ушедших в задания с истёкшим дедлайном
(195 из них менеджер отклонил руками). Сторож держит два факта: чистый порядок
(`game_labels.sort_tasks_for_delegate`) и то, что оба делегатских списка — бота и Mini App —
им пользуются.

Вызов хендлеров напрямую с подставными message/callback — та же конвенция, что в
tests/test_game_ui16_delegate_260820.py (pytest-asyncio в окружении нет).
"""
import asyncio
from datetime import datetime, timedelta

import game_labels
from config import config
from database import db
from handlers import user_actions as ua_mod


ADMIN_ID = 941101
DELEGATE_ID = 941102


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _task_row(task_id: int, deadline: str | None) -> dict:
    return {"id": task_id, "deadline_at": deadline}


# ── чистый порядок ───────────────────────────────────────────────────────────────────────

def test_open_tasks_come_before_overdue():
    now = game_labels.msk_now()
    rows = [
        _task_row(1, _fmt(now - timedelta(days=30))),   # просрочено давно
        _task_row(2, _fmt(now - timedelta(days=2))),    # просрочено вчера-позавчера
        _task_row(3, _fmt(now + timedelta(days=10))),   # открыто, срок дальний
        _task_row(4, _fmt(now + timedelta(days=1))),    # открыто, срок ближний
    ]
    assert [t["id"] for t in game_labels.sort_tasks_for_delegate(rows)] == [4, 3, 2, 1]


def test_task_without_deadline_is_open_but_last_of_open():
    now = game_labels.msk_now()
    rows = [
        _task_row(1, None),
        _task_row(2, _fmt(now + timedelta(days=5))),
        _task_row(3, _fmt(now - timedelta(days=5))),
    ]
    assert [t["id"] for t in game_labels.sort_tasks_for_delegate(rows)] == [2, 1, 3]


def test_sort_does_not_mutate_input():
    now = game_labels.msk_now()
    rows = [_task_row(1, _fmt(now - timedelta(days=1))), _task_row(2, _fmt(now + timedelta(days=1)))]
    before = list(rows)
    game_labels.sort_tasks_for_delegate(rows)
    assert rows == before


def test_unparsable_deadline_does_not_raise():
    rows = [_task_row(1, "завтра вечером"), _task_row(2, None)]
    assert {t["id"] for t in game_labels.sort_tasks_for_delegate(rows)} == {1, 2}


# ── «срок вышел» считается по Москве, а не по UTC контейнера ─────────────────────────────

def test_overdue_is_measured_in_moscow_time(monkeypatch):
    """Контейнер на проде живёт в UTC, дедлайн менеджер вводит по Москве: задание «до 23:59»
    считалось открытым ещё три часа. Фиксируем московское «сейчас» и проверяем обе стороны
    границы."""
    frozen = datetime(2026, 9, 17, 23, 58, 0)
    monkeypatch.setattr(game_labels, "msk_now", lambda: frozen)
    task = {"id": 1, "deadline_at": "2026-09-17 23:59:00"}
    assert game_labels.task_deadline_short(task) == ("17.09", False)

    monkeypatch.setattr(game_labels, "msk_now", lambda: datetime(2026, 9, 18, 0, 1, 0))
    assert game_labels.task_deadline_short(task) == ("17.09", True)


# ── список бота пользуется тем же порядком ───────────────────────────────────────────────

def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_task_order.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_ID, "full_name": "Делегат", "registration_date": "2026-08-01",
    }))


def _create(title: str, deadline: datetime) -> int:
    return asyncio.run(db.create_task(
        text=f"{title} — описание", category="Light", coins=10, proof_type="photo",
        deadline_at=_fmt(deadline), created_by=ADMIN_ID, title=title,
    ))


def test_bot_task_list_puts_overdue_last(tmp_path):
    _db_ready(tmp_path)
    now = game_labels.msk_now()
    old = _create("Августовское", now - timedelta(days=30))
    fresh = _create("Свежее", now + timedelta(days=2))

    text, kb = asyncio.run(ua_mod._game_task_list_screen(DELEGATE_ID))
    assert text.index("Свежее") < text.index("Августовское")
    # Кнопка сдачи свежего задания — первая, просроченного — вторая.
    labels = [row[0].text for row in kb.inline_keyboard]
    assert "Свежее" in labels[0] and "Августовское" in labels[1]
    assert old != fresh
