"""Phase 16 (16-03, GAME-UI-03) — менеджерские экраны заданий: нумерованный список с тумблером
«Активные | Архив», карточка точечной правки (описание/монеты/дедлайн/архив/удаление/превью),
визард с RU-категориями и дедлайн-пресетами, финальный шаг «👁 Так увидит делегат» +
«✅ Опубликовать» с точечным возвратом на шаг.

Хендлеры зовутся НАПРЯМУЮ с Fake-дублёрами (pytest-asyncio в окружении нет) — тот же стиль,
что tests/test_game_task_title_photo_260819.py.
"""
import asyncio
from datetime import datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers.admin_caps import required_capability
from handlers.states import GameTaskCreate, GameTaskEdit


ADMIN_ID = 941001
DELEGATE_ID = 941002


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_ui16_manager_tasks.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _mk_task(**kwargs):
    defaults = dict(text="Пост со скрином #знакомство", category="Light", coins=20,
                    proof_type="photo", deadline_at="2099-01-01 00:00:00", created_by=ADMIN_ID)
    defaults.update(kwargs)
    return asyncio.run(db.create_task(**defaults))


# ── Task 1: DB-аксессоры точечной правки ────────────────────────────────────────────────────

def test_update_task_text_round_trip(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="старое описание")
    assert asyncio.run(db.update_task_text(task_id, "новое описание")) is True
    assert asyncio.run(db.get_task(task_id))["text"] == "новое описание"


def test_update_task_coins_round_trip(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(coins=20)
    assert asyncio.run(db.update_task_coins(task_id, 45)) is True
    assert asyncio.run(db.get_task(task_id))["coins"] == 45


def test_update_task_deadline_round_trip(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(deadline_at="2099-01-01 00:00:00")
    assert asyncio.run(db.update_task_deadline(task_id, "2099-02-02 23:59:00")) is True
    assert asyncio.run(db.get_task(task_id))["deadline_at"] == "2099-02-02 23:59:00"


def test_update_task_point_edit_accessors_missing_task_return_false(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(db.update_task_text(99999, "x")) is False
    assert asyncio.run(db.update_task_coins(99999, 1)) is False
    assert asyncio.run(db.update_task_deadline(99999, "2099-01-01 00:00:00")) is False


# ── Task 1: GameTaskEdit — новые стейты ─────────────────────────────────────────────────────

def test_game_task_edit_states_gained_text_coins_deadline():
    for name in ("text", "coins", "deadline"):
        assert hasattr(GameTaskEdit, name), f"GameTaskEdit.{name} missing"
    assert str(GameTaskEdit.text.state) == "GameTaskEdit:text"


# ── Task 1: ADMIN_CAPS — все новые литералы этого плана -> moderate_game ────────────────────

def test_new_manager_task_callbacks_require_moderate_game():
    for cb in (
        "gteditdesc:1", "gteditcoins:1", "gteditdeadline:1",
        "gtdeadline_preset:today", "gtdeadline_custom",
        "gteditdeadline_preset:plus3", "gteditdeadline_custom",
        "gtpreview:1", "gtpreview_close",
        "gtwiz_edit_menu", "gtwiz_edit:title", "gtwiz_back",
    ):
        assert required_capability(callback_data=cb) == "moderate_game", cb
    for raw_state in ("GameTaskEdit:text", "GameTaskEdit:coins", "GameTaskEdit:deadline"):
        assert required_capability(raw_state=raw_state) == "moderate_game", raw_state


def test_deadline_preset_key_does_not_shadow_point_edit_key():
    """`gteditdeadline:*` и `gteditdeadline_preset:*` — разные ключи (префикс «gteditdeadline:»
    не покрывает «gteditdeadline_preset:», как gtdelete:* не покрывает gtdelete_go:*)."""
    from handlers.admin_caps import ADMIN_CAPS
    assert "gteditdeadline:*" in ADMIN_CAPS
    assert "gteditdeadline_preset:*" in ADMIN_CAPS
