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


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 2: список/архив с тумблером, карточка правки, точечные правки, превью, пункт меню
# ═══════════════════════════════════════════════════════════════════════════════════════════

from handlers import admin as admin_mod  # noqa: E402,F401  (ядро роутера — первым, иначе цикл импорта)
from handlers import admin_gamification  # noqa: E402
from handlers import admin_game_tasks  # noqa: E402  (новый шов-модуль этого плана)
from handlers import admin_core  # noqa: E402
from handlers import user_actions as ua_mod  # noqa: E402
from handlers import game_labels  # noqa: E402
import settings_schema  # noqa: E402


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None


class FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID, photo=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.photo = photo
        self.answers_sent = []
        self.answer_markups = []
        self.answer_parse_modes = []
        self.answer_photo_calls = []
        self.text_edited = None
        self.edit_markup = None
        self.edit_calls = 0
        self.markup_edits = []
        self.deleted = False

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.answer_parse_modes.append(parse_mode)

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.answer_photo_calls.append((photo, caption, parse_mode, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup
        self.edit_calls += 1

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _seed_delegate(uid=DELEGATE_ID):
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": f"Delegate {uid}", "registration_date": "2026-08-01",
    }))


# ── список: нумерация, RU-категория, тумблер, кнопки «№N» ──────────────────────────────────

def test_tasks_screen_numbered_lines_with_ru_category_and_header(tmp_path):
    _db_ready(tmp_path)
    _mk_task(title="Первое", category="Light", coins=30, deadline_at="2099-08-25 23:59:00")
    text, kb = asyncio.run(admin_gamification._game_tasks_screen())
    assert text.startswith("🎯 <b>Задания</b>")
    assert "1. <b>Первое</b>" in text
    assert "Лёгкое · 30🪙 · до 25.08 23:59" in text
    assert "«Light»" not in text  # код категории человеку не показываем


def test_tasks_screen_toggle_row_and_numbered_action_buttons(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    t_new = _mk_task(title="Новое")
    t_arch = _mk_task(title="Архивное")
    asyncio.run(db.archive_task(t_arch))
    text, kb = asyncio.run(admin_gamification._game_tasks_screen())
    rows = kb.inline_keyboard
    # тумблер — первая строка, оба callback'а уже зарегистрированы (ничего нового в ADMIN_CAPS)
    assert [b.text for b in rows[0]] == ["Активные (1)", "Архив (1)"]
    assert [b.callback_data for b in rows[0]] == ["admin_game_tasks", "admin_game_archive"]
    # действия — нумерованные, а не по имени задания
    texts = _flat_texts(kb)
    assert "🗄 №1 В архив" in texts
    assert "✏️ №1" in texts
    assert "🗑 №1" in texts
    data = _flat_callback_data(kb)
    assert f"gtarchive:{t_new}" in data and f"gtedit:{t_new}" in data and f"gtdelete:{t_new}" in data
    assert "gtnew" in data
    assert not any(t.startswith("🗄 В архив:") or t.startswith("✏️ Правка:") for t in texts)


def test_tasks_screen_hides_numbered_delete_when_task_has_submissions(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Со сдачей", proof_type="text")
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "42", "2026-08-01 10:00:00"))
    text, kb = asyncio.run(admin_gamification._game_tasks_screen())
    assert "🗑 №1" not in _flat_texts(kb)
    assert "можно только убрать в архив" in text


def test_archive_screen_numbered_return_buttons_and_toggle(tmp_path):
    _db_ready(tmp_path)
    t = _mk_task(title="Старое")
    asyncio.run(db.archive_task(t))
    _mk_task(title="Живое")
    text, kb = asyncio.run(admin_gamification._game_archive_screen())
    assert "1. <b>Старое</b>" in text
    assert "в архиве с" in text
    rows = kb.inline_keyboard
    assert [b.text for b in rows[0]] == ["Активные (1)", "Архив (1)"]
    assert "↩️ №1 Вернуть" in _flat_texts(kb)
    assert f"gtunarchive:{t}" in _flat_callback_data(kb)


def test_show_game_tasks_edits_in_place(tmp_path):
    """Тумблер «Активные» и «← К заданиям» редактируют ТО ЖЕ сообщение (одно живое сообщение
    на экран) — show_game_tasks симметричен show_game_archive."""
    _db_ready(tmp_path)
    _mk_task(title="Задание")
    callback = FakeCallback("admin_game_tasks")
    asyncio.run(admin_gamification.show_game_tasks(callback, _new_state()))
    assert callback.message.edit_calls == 1
    assert "Задание" in callback.message.text_edited
    assert callback.message.answers_sent == []


# ── карточка правки: все действия ────────────────────────────────────────────────────────────

def test_edit_card_offers_every_point_edit_and_actions(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Карточка", category="Medium", coins=25)
    callback = FakeCallback(f"gtedit:{task_id}")
    asyncio.run(admin_gamification.game_task_edit_screen(callback, _new_state()))
    assert "Карточка" in callback.message.text_edited
    assert "Среднее" in callback.message.text_edited
    texts = _flat_texts(callback.message.edit_markup)
    for label in ("✏️ Название", "✏️ Описание", "💰 Монеты", "📅 Дедлайн", "📷 Добавить фото",
                  "🗄 В архив", "🗑 Удалить", "👁 Как видит делегат", "← К заданиям"):
        assert label in texts, label
    assert "↩️ Вернуть" not in texts
    data = _flat_callback_data(callback.message.edit_markup)
    for cb in (f"gteditdesc:{task_id}", f"gteditcoins:{task_id}", f"gteditdeadline:{task_id}",
               f"gtarchive:{task_id}", f"gtdelete:{task_id}", f"gtpreview:{task_id}"):
        assert cb in data, cb


def test_edit_card_archived_task_offers_return_and_hides_delete_with_submissions(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Архивная", proof_type="text")
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "42", "2026-08-01 10:00:00"))
    asyncio.run(db.archive_task(task_id))
    callback = FakeCallback(f"gtedit:{task_id}")
    asyncio.run(admin_gamification.game_task_edit_screen(callback, _new_state()))
    texts = _flat_texts(callback.message.edit_markup)
    assert "↩️ Вернуть" in texts
    assert "🗄 В архив" not in texts
    assert "🗑 Удалить" not in texts
    assert f"gtunarchive:{task_id}" in _flat_callback_data(callback.message.edit_markup)


# ── точечные правки: описание / монеты (без подтверждения) ──────────────────────────────────

def test_gteditdesc_flow_updates_text_and_rerenders_card(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = []
    monkeypatch.setattr(admin_game_tasks, "_request_game_resync", lambda *a, **k: calls.append(1))
    task_id = _mk_task(title="Задание", text="старый текст")
    state = _new_state()
    asyncio.run(admin_game_tasks.game_task_editdesc_start(FakeCallback(f"gteditdesc:{task_id}"), state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.text
    assert asyncio.run(state.get_data())["gte_task_id"] == task_id

    empty = FakeMessage(text="   ")
    asyncio.run(admin_game_tasks.game_task_editdesc_step(empty, state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.text
    assert asyncio.run(db.get_task(task_id))["text"] == "старый текст"

    ok = FakeMessage(text="новый текст")
    asyncio.run(admin_game_tasks.game_task_editdesc_step(ok, state))
    assert asyncio.run(state.get_state()) is None
    assert asyncio.run(db.get_task(task_id))["text"] == "новый текст"
    assert calls == [1]
    # карточка правки перерисована (не список) — последний ответ несёт клавиатуру карточки
    assert "✏️ Описание" in _flat_texts(ok.answer_markups[-1])


def test_gteditcoins_flow_validates_and_updates(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Задание", coins=20)
    state = _new_state()
    asyncio.run(admin_game_tasks.game_task_editcoins_start(FakeCallback(f"gteditcoins:{task_id}"), state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.coins

    bad = FakeMessage(text="много")
    asyncio.run(admin_game_tasks.game_task_editcoins_step(bad, state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.coins
    assert asyncio.run(db.get_task(task_id))["coins"] == 20

    ok = FakeMessage(text="45")
    asyncio.run(admin_game_tasks.game_task_editcoins_step(ok, state))
    assert asyncio.run(state.get_state()) is None
    assert asyncio.run(db.get_task(task_id))["coins"] == 45


def test_gteditdesc_missing_task_is_alert_not_crash(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("gteditdesc:99999")
    asyncio.run(admin_game_tasks.game_task_editdesc_start(callback, _new_state()))
    assert callback.answers and callback.answers[-1][1] is True


# ── «👁 Как видит делегат» ──────────────────────────────────────────────────────────────────

def test_gtpreview_without_photo_edits_card_in_place_with_delegate_render(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Превью", text="Полное описание задания", category="Hard")
    callback = FakeCallback(f"gtpreview:{task_id}")
    asyncio.run(admin_game_tasks.game_task_preview(callback))
    assert callback.message.edit_calls == 1
    shown = callback.message.text_edited
    assert shown.startswith(settings_schema.SETTINGS_SCHEMA["game_task_preview_intro"]["default"])
    task = asyncio.run(db.get_task(task_id))
    expected = asyncio.run(game_labels.render_task_card_text(task, "новое", None))
    assert expected in shown
    assert "<blockquote expandable>Полное описание задания</blockquote>" in shown
    data = _flat_callback_data(callback.message.edit_markup)
    assert data == [f"gtedit:{task_id}"]  # только «← Назад», никаких «📤 Сдать»


def test_gtpreview_with_photo_sends_photo_with_close_button(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="С обложкой", photo_file_id="cover7")
    callback = FakeCallback(f"gtpreview:{task_id}")
    asyncio.run(admin_game_tasks.game_task_preview(callback))
    assert callback.message.edit_calls == 0
    photo, caption, parse_mode, kb = callback.message.answer_photo_calls[0]
    assert photo == "cover7" and parse_mode == "HTML"
    assert "С обложкой" in caption
    assert _flat_callback_data(kb) == ["gtpreview_close"]


def test_gtpreview_close_deletes_message(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("gtpreview_close")
    asyncio.run(admin_game_tasks.game_task_preview_close(callback))
    assert callback.message.deleted is True


def test_delegate_card_renderer_lives_in_game_labels_and_is_shared():
    """Менеджер и делегат рисуют карточку ОДНОЙ функцией — user_actions импортирует её из
    game_labels под прежним именем (существующие тесты `ua_mod._render_task_card_text`)."""
    assert ua_mod._render_task_card_text is game_labels.render_task_card_text


# ── пункт меню + ключ реестра ────────────────────────────────────────────────────────────────

def test_admin_menu_row_renamed_to_target_emoji():
    rows = admin_core._visible_menu_rows({"moderate_game"})
    assert ("🎯 Задания", "admin_game_tasks") in rows
    assert not any(label == "📋 Задания" for label, _ in admin_core._ADMIN_MENU_ROWS)


def test_preview_intro_registry_key_in_game_group():
    entry = settings_schema.SETTINGS_SCHEMA["game_task_preview_intro"]
    assert entry["group"] == "game"
    assert entry["default"].startswith("👁")
    from handlers import admin_settings
    assert "game_task_preview_intro" in admin_settings._GAME_FIELD_ORDER
