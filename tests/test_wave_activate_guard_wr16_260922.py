"""Ревизия 32-FIX (фиксер 3, находка WR-16) — `handlers/admin_game_waves.py::wave_activate_confirm`:

запуск пустой волны (ни одного активного задания) или волны с уже прошедшей датой конца
отклоняется с человеческим объяснением; текст подтверждения честно называет момент рассылки
стартового сообщения (дату начала волны или «в течение минуты», если она уже наступила), а не
безусловное «сразу».

Handlers called DIRECTLY with Fake message/callback doubles (pytest-asyncio недоступен) — та же
конвенция, что у tests/test_ambassador_waves_crud_32.py.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin_game_waves as w

ADMIN_ID = 932001


def _run(coro):
    return asyncio.run(coro)


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers_sent = []
        self.edits = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append(text)


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _db_ready(tmp_path, name="wave_activate_guard.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def test_activate_confirm_rejects_empty_wave(tmp_path):
    _db_ready(tmp_path)
    wid = _run(db.create_wave("2099-10-01 00:00:00", "2099-10-15 23:59:59", created_by=ADMIN_ID))
    cb = FakeCallback(f"waveactivate:{wid}")
    _run(w.wave_activate_confirm(cb, _new_state()))
    assert cb.answers and cb.answers[-1][1] is True
    assert "добавьте задания" in cb.answers[-1][0].lower()
    assert not cb.message.edits  # экран подтверждения не показан
    wave = _run(db.get_wave(wid))
    assert wave["state"] == "draft"


def test_activate_confirm_rejects_past_end_date(tmp_path):
    _db_ready(tmp_path)
    wid = _run(db.create_wave("2020-01-01 00:00:00", "2020-01-10 23:59:59", created_by=ADMIN_ID))
    _run(db.create_task("Задание", "Light", 10, "text", "2020-01-10 23:59:59", ADMIN_ID, wave_id=wid))
    cb = FakeCallback(f"waveactivate:{wid}")
    _run(w.wave_activate_confirm(cb, _new_state()))
    assert cb.answers and cb.answers[-1][1] is True
    assert "поправьте даты" in cb.answers[-1][0].lower() or "уже прошла" in cb.answers[-1][0].lower()
    assert not cb.message.edits


def test_activate_confirm_shows_future_start_date_not_immediate(tmp_path):
    """Даты волны в будущем — текст называет дату начала, а не «сразу»/«в течение минуты»."""
    _db_ready(tmp_path)
    wid = _run(db.create_wave("2099-10-01 00:00:00", "2099-10-15 23:59:59", created_by=ADMIN_ID))
    _run(db.create_task("Задание", "Light", 10, "text", "2099-10-15 23:59:59", ADMIN_ID, wave_id=wid))
    cb = FakeCallback(f"waveactivate:{wid}")
    _run(w.wave_activate_confirm(cb, _new_state()))
    assert not cb.answers or cb.answers[-1][1] is not True
    text = cb.message.edits[-1]
    assert "01.10.2099" in text
    assert "в течение минуты" not in text


def test_activate_confirm_shows_immediate_text_when_start_already_passed(tmp_path):
    """Дата начала уже наступила (волна создана «на сегодня»), конец — в будущем: рассылка
    уйдёт «в течение минуты», а не в прошедшую дату начала."""
    _db_ready(tmp_path)
    wid = _run(db.create_wave("2020-01-01 00:00:00", "2099-10-15 23:59:59", created_by=ADMIN_ID))
    _run(db.create_task("Задание", "Light", 10, "text", "2099-10-15 23:59:59", ADMIN_ID, wave_id=wid))
    cb = FakeCallback(f"waveactivate:{wid}")
    _run(w.wave_activate_confirm(cb, _new_state()))
    assert not cb.answers or cb.answers[-1][1] is not True
    text = cb.message.edits[-1]
    assert "в течение минуты" in text


# ── ревизия 32-FIX-common-2 (остаток WR-16): те же проверки на самом нажатии «Да, запустить» ──
# Экран подтверждения перепроверял пустоту/просроченный конец, а сам клик по «Да, запустить»
# (`wave_activate_go`) — нет: между показом экрана и нажатием состав заданий или даты волны
# могли смениться (менеджер Б успел удалить последнее задание, или экран висел с вечера, пока
# дата конца не прошла). Тесты ниже зовут `wave_activate_go` НАПРЯМУЮ, минуя экран
# подтверждения, — так же, как устаревшая кнопка попала бы к обработчику в проде.

def test_activate_go_rejects_empty_wave_bypassing_confirm_screen(tmp_path):
    _db_ready(tmp_path)
    wid = _run(db.create_wave("2099-10-01 00:00:00", "2099-10-15 23:59:59", created_by=ADMIN_ID))
    cb = FakeCallback(f"waveactivate_go:{wid}")
    _run(w.wave_activate_go(cb, _new_state()))
    assert cb.answers and cb.answers[-1][1] is True
    assert "добавьте задания" in cb.answers[-1][0].lower()
    wave = _run(db.get_wave(wid))
    assert wave["state"] == "draft"  # запуск не состоялся


def test_activate_go_rejects_past_end_date_bypassing_confirm_screen(tmp_path):
    _db_ready(tmp_path)
    wid = _run(db.create_wave("2020-01-01 00:00:00", "2020-01-10 23:59:59", created_by=ADMIN_ID))
    _run(db.create_task("Задание", "Light", 10, "text", "2020-01-10 23:59:59", ADMIN_ID, wave_id=wid))
    cb = FakeCallback(f"waveactivate_go:{wid}")
    _run(w.wave_activate_go(cb, _new_state()))
    assert cb.answers and cb.answers[-1][1] is True
    assert "поправьте даты" in cb.answers[-1][0].lower() or "уже прошла" in cb.answers[-1][0].lower()
    wave = _run(db.get_wave(wid))
    assert wave["state"] == "draft"


def test_activate_go_blocks_when_last_task_removed_between_screen_and_click(tmp_path):
    """Сценарий ревью: менеджер видел экран подтверждения с заданием, но пока он думал,
    последнее задание волны удалили (или архивировали) — запуск всё равно отклоняется."""
    _db_ready(tmp_path)
    wid = _run(db.create_wave("2099-10-01 00:00:00", "2099-10-15 23:59:59", created_by=ADMIN_ID))
    tid = _run(db.create_task("Задание", "Light", 10, "text", "2099-10-15 23:59:59", ADMIN_ID, wave_id=wid))
    confirm_cb = FakeCallback(f"waveactivate:{wid}")
    _run(w.wave_activate_confirm(confirm_cb, _new_state()))
    assert not confirm_cb.answers or confirm_cb.answers[-1][1] is not True  # экран показан, не отказ

    _run(db.archive_task(tid))
    go_cb = FakeCallback(f"waveactivate_go:{wid}")
    _run(w.wave_activate_go(go_cb, _new_state()))
    assert go_cb.answers and go_cb.answers[-1][1] is True
    assert "добавьте задания" in go_cb.answers[-1][0].lower()
    wave = _run(db.get_wave(wid))
    assert wave["state"] == "draft"
