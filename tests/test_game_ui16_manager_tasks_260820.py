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


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 3: визард — RU-категории на кнопках, дедлайн-пресеты (создание + точечная правка)
# ═══════════════════════════════════════════════════════════════════════════════════════════

from database.db import GAME_CATEGORIES  # noqa: E402
from handlers import game_task_wizard  # noqa: E402
from services.scheduler import _now_moscow_naive  # noqa: E402


def _drive_to_deadline(state, title="Задание", text="Текст задания", photo=None, coins="30"):
    """gtnew -> title -> text -> photo/skip -> category -> coins -> proof done (module off) ->
    deadline prompt. Returns the callback whose message got the deadline prompt."""
    asyncio.run(admin_gamification.game_task_new(FakeCallback("gtnew"), state))
    asyncio.run(admin_gamification.game_task_title_step(FakeMessage(text=title), state))
    asyncio.run(admin_gamification.game_task_text_step(FakeMessage(text=text), state))
    if photo:
        asyncio.run(admin_gamification.game_task_photo_step(FakeMessage(photo=[FakePhotoSize(photo)]), state))
    else:
        asyncio.run(admin_gamification.game_task_photo_skip(FakeCallback("gtphoto_skip"), state))
    asyncio.run(admin_gamification.game_task_category_step(FakeCallback("gtcat:Light"), state))
    asyncio.run(admin_gamification.game_task_coins_step(FakeMessage(text=coins), state))
    asyncio.run(admin_gamification.game_task_proof_step(FakeCallback("gtproof:photo"), state))
    cb = FakeCallback("gtproof_done")
    asyncio.run(admin_gamification.game_task_proof_done(cb, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline
    return cb


def test_category_kb_shows_ru_labels_keeps_raw_codes(tmp_path):
    _db_ready(tmp_path)
    kb = asyncio.run(admin_gamification._game_task_category_kb())
    assert _flat_callback_data(kb) == [f"gtcat:{c}" for c in GAME_CATEGORIES]
    texts = _flat_texts(kb)
    assert "Лёгкое" in texts and "Среднее" in texts and "Сложное" in texts
    assert not any(t in GAME_CATEGORIES for t in texts)  # ни одного сырого кода на кнопке


def test_photo_skip_sends_ru_category_keyboard(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.photo))
    cb = FakeCallback("gtphoto_skip")
    asyncio.run(admin_gamification.game_task_photo_skip(cb, state))
    assert "Лёгкое" in _flat_texts(cb.message.answer_markups[-1])


def test_deadline_prompt_carries_preset_keyboard(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    cb = _drive_to_deadline(state)
    kb = cb.message.answer_markups[-1]
    data = _flat_callback_data(kb)
    assert data[:3] == ["gtdeadline_preset:today", "gtdeadline_preset:plus3", "gtdeadline_preset:plus7"]
    assert "gtdeadline_custom" in data
    assert "gtcancel" in data  # «❌ Отмена» — уже зарегистрированный callback
    texts = _flat_texts(kb)
    assert "Сегодня 23:59" in texts and "+3 дня" in texts and "+7 дней" in texts


def test_resolve_deadline_preset_values():
    now = _now_moscow_naive()
    today = game_task_wizard._resolve_deadline_preset("today")
    assert (today.hour, today.minute, today.second) == (23, 59, 0)
    assert today.date() == now.date()
    assert game_task_wizard._resolve_deadline_preset("plus3") == today + timedelta(days=3)
    assert game_task_wizard._resolve_deadline_preset("plus7") == today + timedelta(days=7)
    assert game_task_wizard._resolve_deadline_preset("bogus") is None


def test_wizard_preset_plus3_stores_deadline_and_shows_preview(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    cb = FakeCallback("gtdeadline_preset:plus3")
    asyncio.run(admin_game_tasks.game_task_deadline_preset(cb, state))
    expected = game_task_wizard._resolve_deadline_preset("plus3")
    assert asyncio.run(state.get_data())["gt_deadline"] == expected.strftime("%Y-%m-%d %H:%M:%S")
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    assert "Так увидит делегат" in cb.message.answers_sent[-1]


def test_wizard_preset_past_time_is_rejected_like_manual(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    # «сегодня 23:59» уже прошло -- эмулируем поздний вечер: резолвер вернёт прошедшее время
    monkeypatch.setattr(admin_game_tasks, "_resolve_deadline_preset",
                        lambda code: _now_moscow_naive() - timedelta(minutes=1))
    cb = FakeCallback("gtdeadline_preset:today")
    asyncio.run(admin_game_tasks.game_task_deadline_preset(cb, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline
    assert cb.answers[-1][1] is True and "прошло" in cb.answers[-1][0]
    assert "gt_deadline" not in asyncio.run(state.get_data())


def test_wizard_preset_unknown_code_is_alert(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    cb = FakeCallback("gtdeadline_preset:bogus")
    asyncio.run(admin_game_tasks.game_task_deadline_preset(cb, state))
    assert cb.answers[-1][1] is True
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline


def test_wizard_preset_outside_deadline_step_is_ignored_with_alert(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()  # no state at all -- stale button after a restart
    cb = FakeCallback("gtdeadline_preset:plus7")
    asyncio.run(admin_game_tasks.game_task_deadline_preset(cb, state))
    assert cb.answers[-1][1] is True
    assert cb.message.answers_sent == []


def test_wizard_custom_date_button_is_informational_only(tmp_path):
    _db_ready(tmp_path)
    cb = FakeCallback("gtdeadline_custom")
    asyncio.run(admin_game_tasks.game_task_deadline_custom(cb))
    assert cb.answers == [("Введите дату текстом ниже", False)]


def test_typed_deadline_still_works_after_refactor(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    msg = FakeMessage(text="25.08.2099 23:59")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    assert asyncio.run(state.get_data())["gt_deadline"] == "2099-08-25 23:59:00"
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm


# ── точечная правка дедлайна: пресет и текст, без карточки подтверждения ────────────────────

def test_gteditdeadline_start_sends_edit_prefixed_preset_kb(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Дедлайн")
    state = _new_state()
    cb = FakeCallback(f"gteditdeadline:{task_id}")
    asyncio.run(admin_game_tasks.game_task_editdeadline_start(cb, state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.deadline
    # карточка правки сама становится промптом (одно живое сообщение), не новое сообщение
    assert cb.message.edit_calls == 1 and cb.message.answers_sent == []
    assert "Дедлайн" in cb.message.text_edited
    data = _flat_callback_data(cb.message.edit_markup)
    assert "gteditdeadline_preset:today" in data and "gteditdeadline_custom" in data
    assert f"gtedit:{task_id}" in data  # «❌ Отмена» возвращает в карточку правки


def test_gteditdeadline_preset_writes_directly_and_rerenders_card(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = []
    monkeypatch.setattr(admin_game_tasks, "_request_game_resync", lambda *a, **k: calls.append(1))
    task_id = _mk_task(title="Дедлайн", deadline_at="2099-01-01 00:00:00")
    state = _new_state()
    asyncio.run(admin_game_tasks.game_task_editdeadline_start(FakeCallback(f"gteditdeadline:{task_id}"), state))
    cb = FakeCallback("gteditdeadline_preset:plus7")
    asyncio.run(admin_game_tasks.game_task_editdeadline_preset(cb, state))
    expected = game_task_wizard._resolve_deadline_preset("plus7").strftime("%Y-%m-%d %H:%M:%S")
    assert asyncio.run(db.get_task(task_id))["deadline_at"] == expected
    assert asyncio.run(state.get_state()) is None
    assert calls == [1]
    # NO confirm card: the prompt message itself becomes the edit card again
    assert cb.message.edit_calls == 1
    assert "📅 Дедлайн" in _flat_texts(cb.message.edit_markup)


def test_gteditdeadline_typed_validates_and_writes(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Дедлайн", deadline_at="2099-01-01 00:00:00")
    state = _new_state()
    asyncio.run(admin_game_tasks.game_task_editdeadline_start(FakeCallback(f"gteditdeadline:{task_id}"), state))

    bad = FakeMessage(text="вчера")
    asyncio.run(admin_game_tasks.game_task_editdeadline_step(bad, state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.deadline

    past = FakeMessage(text="01.01.2020 10:00")
    asyncio.run(admin_game_tasks.game_task_editdeadline_step(past, state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.deadline
    assert asyncio.run(db.get_task(task_id))["deadline_at"] == "2099-01-01 00:00:00"

    ok = FakeMessage(text="02.02.2099 12:00")
    asyncio.run(admin_game_tasks.game_task_editdeadline_step(ok, state))
    assert asyncio.run(state.get_state()) is None
    assert asyncio.run(db.get_task(task_id))["deadline_at"] == "2099-02-02 12:00:00"
    assert "📅 Дедлайн" in _flat_texts(ok.answer_markups[-1])


def test_gteditdeadline_preset_without_open_edit_is_alert(tmp_path):
    _db_ready(tmp_path)
    cb = FakeCallback("gteditdeadline_preset:today")
    asyncio.run(admin_game_tasks.game_task_editdeadline_preset(cb, _new_state()))
    assert cb.answers[-1][1] is True
    assert cb.message.edit_calls == 0


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 4: финальный шаг визарда — превью «как увидит делегат» + «✅ Опубликовать» + «✏️ Изменить»
# ═══════════════════════════════════════════════════════════════════════════════════════════

def _drive_to_preview(state, **kw):
    _drive_to_deadline(state, **kw)
    msg = FakeMessage(text="25.08.2099 23:59")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    return msg


def test_final_step_is_delegate_preview_with_publish_button(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    msg = _drive_to_preview(state, title="Знакомство", text="Пост со скрином #знакомство")
    card = msg.answers_sent[-1]
    assert card.startswith(settings_schema.SETTINGS_SCHEMA["game_wizard_preview_title"]["default"])
    assert "Так увидит делегат" in card
    assert "Проверьте задание" not in card
    # ровно тот же рендер, что у делегата
    data = asyncio.run(state.get_data())
    task_like = game_task_wizard._wizard_task_like(data)
    assert asyncio.run(game_labels.render_task_card_text(task_like, "новое", None)) in card
    assert "<b>Знакомство</b>" in card
    assert "Лёгкое · 30🪙 · до 25.08" in card
    assert "<blockquote expandable>Пост со скрином #знакомство</blockquote>" in card
    kb = msg.answer_markups[-1]
    texts = _flat_texts(kb)
    assert texts[0] == "✅ Опубликовать"
    assert "✅ Создать" not in texts
    assert "✏️ Изменить" in texts and "❌ Отмена" in texts
    assert _flat_callback_data(kb) == ["gtconfirm", "gtwiz_edit_menu", "gtcancel"]


def test_final_step_with_photo_sends_photo_with_caption(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    msg = _drive_to_preview(state, title="С обложкой", photo="cover1")
    assert msg.answer_photo_calls, "превью с обложкой уходит фото с подписью"
    photo, caption, parse_mode, kb = msg.answer_photo_calls[-1]
    assert photo == "cover1" and parse_mode == "HTML"
    assert "Так увидит делегат" in caption and "С обложкой" in caption
    assert "gtconfirm" in _flat_callback_data(kb)


def test_final_step_kому_line_when_city_step_shown(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    asyncio.run(state.update_data(gt_city_step_shown=True, gt_event_city_label=None))
    msg = FakeMessage(text="25.08.2099 23:59")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    assert "Кому: 🌍 Все города" in msg.answers_sent[-1]


def test_publish_creates_task_exactly_as_before(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_preview(state, title="Публикуем", text="Описание")
    asyncio.run(admin_gamification.game_task_confirm(FakeCallback("gtconfirm"), state))
    tasks = asyncio.run(db.list_all_tasks())
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Публикуем"
    assert tasks[0]["text"] == "Описание"
    assert tasks[0]["category"] == "Light"
    assert tasks[0]["coins"] == 30
    assert tasks[0]["deadline_at"] == "2099-08-25 23:59:00"
    assert asyncio.run(state.get_state()) is None


def test_edit_menu_swaps_keyboard_and_back_restores(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_preview(state)
    cb = FakeCallback("gtwiz_edit_menu")
    asyncio.run(admin_game_tasks.game_task_wizard_edit_menu(cb, state))
    menu = cb.message.markup_edits[-1]
    data = _flat_callback_data(menu)
    for field in ("title", "text", "category", "coins", "deadline", "photo"):
        assert f"gtwiz_edit:{field}" in data
    assert "gtwiz_back" in data
    texts = _flat_texts(menu)
    assert "📝 Название" in texts and "📄 Описание" in texts and "💰 Монеты" in texts
    assert "📅 Дедлайн" in texts and "📷 Фото" in texts
    # state stays parked at confirm -- nothing typed yet
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm

    back = FakeCallback("gtwiz_back")
    asyncio.run(admin_game_tasks.game_task_wizard_back(back, state))
    assert _flat_callback_data(back.message.markup_edits[-1]) == ["gtconfirm", "gtwiz_edit_menu", "gtcancel"]


def test_edit_title_from_preview_returns_to_updated_preview(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_preview(state, title="Старое имя", text="Описание")
    cb = FakeCallback("gtwiz_edit:title")
    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(cb, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.title
    assert cb.message.answers_sent[-1] == settings_schema.SETTINGS_SCHEMA["game_task_title_prompt"]["default"]

    msg = FakeMessage(text="Новое имя")
    asyncio.run(admin_gamification.game_task_title_step(msg, state))
    # НЕ шаг «текст», а сразу превью с новым названием и прежними данными
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    card = msg.answers_sent[-1]
    assert "<b>Новое имя</b>" in card and "Так увидит делегат" in card
    data = asyncio.run(state.get_data())
    assert data["gt_title"] == "Новое имя"
    assert data["gt_text"] == "Описание"
    assert data["gt_deadline"] == "2099-08-25 23:59:00"
    assert data.get("gt_wiz_edit") is False


def test_edit_coins_from_preview_keeps_proof_types(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_preview(state)
    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(FakeCallback("gtwiz_edit:coins"), state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.coins
    msg = FakeMessage(text="55")
    asyncio.run(admin_gamification.game_task_coins_step(msg, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    data = asyncio.run(state.get_data())
    assert data["gt_coins"] == 55
    assert data["gt_proof_type"] == "photo"  # выбор типов подтверждения не сброшен
    assert "55🪙" in msg.answers_sent[-1]


def test_edit_category_and_text_and_photo_from_preview(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_preview(state, text="Старый текст")

    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(FakeCallback("gtwiz_edit:category"), state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.category
    cb = FakeCallback("gtcat:Hard")
    asyncio.run(admin_gamification.game_task_category_step(cb, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    assert "Сложное" in cb.message.answers_sent[-1]

    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(FakeCallback("gtwiz_edit:text"), state))
    msg = FakeMessage(text="Новый текст")
    asyncio.run(admin_gamification.game_task_text_step(msg, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    assert "<blockquote expandable>Новый текст</blockquote>" in msg.answers_sent[-1]

    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(FakeCallback("gtwiz_edit:photo"), state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.photo
    pm = FakeMessage(photo=[FakePhotoSize("cover2")])
    asyncio.run(admin_gamification.game_task_photo_step(pm, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    assert pm.answer_photo_calls and pm.answer_photo_calls[-1][0] == "cover2"


def test_edit_deadline_from_preview_uses_preset_and_returns(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_preview(state)
    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(FakeCallback("gtwiz_edit:deadline"), state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline
    cb = FakeCallback("gtdeadline_preset:plus7")
    asyncio.run(admin_game_tasks.game_task_deadline_preset(cb, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    expected = game_task_wizard._resolve_deadline_preset("plus7").strftime("%Y-%m-%d %H:%M:%S")
    assert asyncio.run(state.get_data())["gt_deadline"] == expected


def test_edit_menu_without_draft_is_alert(tmp_path):
    _db_ready(tmp_path)
    cb = FakeCallback("gtwiz_edit_menu")
    asyncio.run(admin_game_tasks.game_task_wizard_edit_menu(cb, _new_state()))
    assert cb.answers[-1][1] is True
    assert "начните" in cb.answers[-1][0]
    assert cb.message.markup_edits == []


def test_edit_menu_mid_step_tells_to_finish_current_step(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_preview(state)
    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(FakeCallback("gtwiz_edit:title"), state))
    cb = FakeCallback("gtwiz_edit:coins")  # второй тап по старому меню, пока ждём название
    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(cb, state))
    assert cb.answers[-1][1] is True and "текущий шаг" in cb.answers[-1][0]
    assert asyncio.run(state.get_state()) == GameTaskCreate.title


def test_toggle_retap_not_modified_is_silent(tmp_path):
    """Тап по стороне тумблера, на которой уже стоишь: Telegram отвечает «message is not
    modified» — это не ошибка и не повод слать дубликат экрана."""
    _db_ready(tmp_path)
    _mk_task(title="Задание")

    class NotModifiedMessage(FakeMessage):
        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            self.edit_calls += 1
            raise RuntimeError("Bad Request: message is not modified")

    cb = FakeCallback("admin_game_tasks", message=NotModifiedMessage())
    asyncio.run(admin_gamification.show_game_tasks(cb, _new_state()))
    assert cb.message.edit_calls == 1
    assert cb.message.answers_sent == []
    assert cb.answers  # callback closed, no spinner


def test_screen_edit_other_failure_falls_back_to_new_message(tmp_path):
    _db_ready(tmp_path)
    _mk_task(title="Задание")

    class PhotoMessage(FakeMessage):
        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            raise RuntimeError("Bad Request: there is no text in the message to edit")

    cb = FakeCallback("admin_game_archive", message=PhotoMessage())
    asyncio.run(admin_gamification.show_game_archive(cb))
    assert len(cb.message.answers_sent) == 1


def test_wizard_registry_keys_and_html_policy():
    for key in ("game_wizard_preview_title", "game_wizard_publish_btn"):
        assert settings_schema.SETTINGS_SCHEMA[key]["group"] == "game"
    from handlers import admin_settings
    assert "game_wizard_preview_title" in admin_settings.HTML_SETTINGS
    assert "game_wizard_publish_btn" not in admin_settings.HTML_SETTINGS
    assert "game_wizard_preview_title" in admin_settings._GAME_FIELD_ORDER
    assert "game_wizard_publish_btn" in admin_settings._GAME_FIELD_ORDER


def test_no_dead_confirm_card_left_behind():
    import inspect
    src = inspect.getsource(admin_gamification) + inspect.getsource(game_task_wizard)
    assert "Проверьте задание" not in src
    assert "✅ Создать" not in src
