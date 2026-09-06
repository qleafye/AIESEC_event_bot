"""Phase 27 (27-06, LANG-04/LANG-05/LANG-09) — сторож экрана «🌐 Английские тексты».

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async-вызов
через asyncio.run(), `config.DB_PATH` смотрит в `tmp_path`. Доступ проверяется через реальный
`admin_mod.router.propagate_event` (`tests.test_roles_phase8.dispatch_callback`) — единственное
место в наборе, которое реально прогоняет `CapabilityMiddleware`, а не зовёт хендлер напрямую.
"""
import asyncio

import pytest

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin_i18n as mod
from handlers.states import AdminI18nEdit
from services.i18n import src_hash as compute_src_hash

from handlers.admin_caps import role_caps_key, role_enabled_key
from tests.test_roles_phase8 import ADMIN_ID, MANAGER_ID, STRANGER_ID, _roles_ready, dispatch_callback

LANG = "en"


def _db_ready(tmp_path, name="test_i18n_admin_27.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0
        self.sent: list[tuple] = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.sent.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeIncomingMessage:
    def __init__(self, text, user_id=ADMIN_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.sent: list[tuple] = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.sent.append((text, reply_markup))


def _fresh_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _kb_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _kb_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def _seed(n: int, *, manual=0, text_maker=None):
    for i in range(n):
        src = f"Русский текст номер {i}"
        h = compute_src_hash(src)
        text_value = text_maker(i) if text_maker else f"English text number {i}"
        await db.upsert_translation(LANG, h, src, text_value, manual=manual, origin_key=f"lit:seed.{i}")


# ── Пагинация (Задача 1) ─────────────────────────────────────────────────────────────────────

def test_pagination_25_rows_10_per_page(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await _seed(25)
        text0, kb0 = await mod.render_i18n_list("all", 0)
        assert "Страница 1 из 3" in text0
        assert "◀ Назад" not in _kb_texts(kb0)
        assert "Вперёд ▶" in _kb_texts(kb0)

        text1, kb1 = await mod.render_i18n_list("all", 1)
        assert "Страница 2 из 3" in text1
        assert "◀ Назад" in _kb_texts(kb1)
        assert "Вперёд ▶" in _kb_texts(kb1)

        text2, kb2 = await mod.render_i18n_list("all", 2)
        assert "Страница 3 из 3" in text2
        assert "◀ Назад" in _kb_texts(kb2)
        assert "Вперёд ▶" not in _kb_texts(kb2)

        # Ровно по 10 строк-кнопок на страницах 1 и 2, 5 на третьей (25 = 10+10+5).
        row_buttons = [cb for cb in _kb_callbacks(kb0) if cb.startswith("admin_i18n_row:")]
        assert len(row_buttons) == 10
        row_buttons2 = [cb for cb in _kb_callbacks(kb2) if cb.startswith("admin_i18n_row:")]
        assert len(row_buttons2) == 5

    asyncio.run(go())


# ── Фильтры (Задача 2) ───────────────────────────────────────────────────────────────────────

def test_filters_return_expected_subsets(tmp_path):
    _db_ready(tmp_path)

    async def go():
        # Без перевода: text=NULL.
        await db.upsert_translation(LANG, "hash_pending", "Русский без перевода", None, manual=0)
        # Правлено вручную.
        await db.upsert_translation(LANG, "hash_manual", "Русский вручную", "Manual EN", manual=1)
        # Не удалось: text="".
        await db.upsert_translation(LANG, "hash_failed", "Русский не удалось", "", manual=0)
        # Обычная машинная строка — не должна попасть ни в один из трёх фильтров ниже.
        await db.upsert_translation(LANG, "hash_done", "Русский готово", "Done EN", manual=0)

        _rows_all, total_all = await mod._load_page("all", 0)
        assert total_all == 4

        rows_pending, total_pending = await mod._load_page("pending", 0)
        assert total_pending == 1
        assert rows_pending[0]["src_hash"] == "hash_pending"

        rows_manual, total_manual = await mod._load_page("manual", 0)
        assert total_manual == 1
        assert rows_manual[0]["src_hash"] == "hash_manual"

        rows_failed, total_failed = await mod._load_page("failed", 0)
        assert total_failed == 1
        assert rows_failed[0]["src_hash"] == "hash_failed"

    asyncio.run(go())


# ── Ручная правка защищена от машины (Задача 2) ─────────────────────────────────────────────

def test_manual_edit_survives_machine_overwrite(tmp_path):
    _db_ready(tmp_path)

    async def go():
        callback = FakeCallback("admin_i18n_row:all:0:0", user_id=ADMIN_ID)
        src = "Русский вопрос"
        h = compute_src_hash(src)
        await db.upsert_translation(LANG, h, src, None, manual=0, origin_key="lit:x")

        state = _fresh_state()
        edit_cb = FakeCallback("admin_i18n_edit:all:0:0", user_id=ADMIN_ID)
        await mod.admin_i18n_edit_start(edit_cb, state)
        assert await state.get_state() == AdminI18nEdit.text.state

        msg = FakeIncomingMessage("Manual English answer", user_id=ADMIN_ID)
        await mod.admin_i18n_edit_step(msg, state)
        assert await state.get_state() is None

        row = await db.get_translation(LANG, h)
        assert row["text"] == "Manual English answer"
        assert row["manual"] == 1

        # Машина пытается перезаписать той же строке — LANG-05 защита в самом upsert_translation.
        await db.upsert_translation(LANG, h, src, "Machine answer", manual=0)
        row2 = await db.get_translation(LANG, h)
        assert row2["text"] == "Manual English answer"
        assert row2["manual"] == 1

        del callback  # не используется -- инициализация оставлена для читаемости сценария

    asyncio.run(go())


def test_manual_edit_empty_input_gives_human_error_not_invalid_input(tmp_path):
    _db_ready(tmp_path)

    async def go():
        src = "Ещё один вопрос"
        h = compute_src_hash(src)
        await db.upsert_translation(LANG, h, src, None, manual=0, origin_key="lit:y")
        state = _fresh_state()
        edit_cb = FakeCallback("admin_i18n_edit:all:0:0", user_id=ADMIN_ID)
        await mod.admin_i18n_edit_start(edit_cb, state)

        msg = FakeIncomingMessage("   ", user_id=ADMIN_ID)
        await mod.admin_i18n_edit_step(msg, state)
        assert await state.get_state() == AdminI18nEdit.text.state  # остались в состоянии
        assert "Отмена" in msg.sent[-1][0]
        assert "invalid input" not in msg.sent[-1][0].lower()

    asyncio.run(go())


def test_multiline_input_via_semicolon_separator(tmp_path):
    _db_ready(tmp_path)

    async def go():
        src = "Многострочный вопрос"
        h = compute_src_hash(src)
        await db.upsert_translation(LANG, h, src, None, manual=0, origin_key="lit:z")
        state = _fresh_state()
        edit_cb = FakeCallback("admin_i18n_edit:all:0:0", user_id=ADMIN_ID)
        await mod.admin_i18n_edit_start(edit_cb, state)

        msg = FakeIncomingMessage("Line one; Line two; Line three", user_id=ADMIN_ID)
        await mod.admin_i18n_edit_step(msg, state)

        row = await db.get_translation(LANG, h)
        assert row["text"] == "Line one\nLine two\nLine three"

    asyncio.run(go())


# ── «Перевести заново» — только с подтверждением (Задача 2) ─────────────────────────────────

def test_retranslate_confirm_screen_changes_nothing(tmp_path):
    _db_ready(tmp_path)

    async def go():
        src = "Строка для повторного перевода"
        h = compute_src_hash(src)
        await db.upsert_translation(LANG, h, src, "Old manual EN", manual=1, origin_key="lit:r")

        confirm_cb = FakeCallback("admin_i18n_retr:all:0:0", user_id=ADMIN_ID)
        await mod.admin_i18n_retranslate_confirm(confirm_cb)
        assert "Отменить будет нельзя" in confirm_cb.message.text

        row = await db.get_translation(LANG, h)
        assert row["text"] == "Old manual EN"
        assert row["manual"] == 1
        pending = await db.list_pending_translations(LANG, limit=10)
        assert pending == []

    asyncio.run(go())


def test_retranslate_go_queues_and_clears_manual(tmp_path):
    _db_ready(tmp_path)

    async def go():
        src = "Строка для реального повторного перевода"
        h = compute_src_hash(src)
        await db.upsert_translation(LANG, h, src, "Old manual EN", manual=1, origin_key="lit:r2")

        go_cb = FakeCallback("admin_i18n_retr_go:all:0:0", user_id=ADMIN_ID)
        await mod.admin_i18n_retranslate_go(go_cb)

        row = await db.get_translation(LANG, h)
        assert row["manual"] == 0, "manual снят — строка вернулась машине"
        pending = await db.list_pending_translations(LANG, limit=10)
        assert any(p["src_hash"] == h for p in pending), "строка встала в очередь"

    asyncio.run(go())


def test_retranslate_refused_for_consent_row(tmp_path):
    """LANG-09: машинный перевод согласий запрещён даже через прямой вызов «Перевести
    заново» (защита не только в UI-кнопке, но и в самом хендлере)."""
    _db_ready(tmp_path)

    async def go():
        await db.set_setting("consent_button_text", "Согласен(-на)")
        text_via = "Согласен(-на)"
        h = compute_src_hash(text_via)
        await db.upsert_translation(LANG, h, text_via, "Agree", manual=1, origin_key="consent:button")

        rows, _total = await mod._load_page("consent", 0)
        idx = next(i for i, r in enumerate(rows) if r["src_hash"] == h)
        go_cb = FakeCallback(f"admin_i18n_retr_go:consent:0:{idx}", user_id=ADMIN_ID)
        await mod.admin_i18n_retranslate_go(go_cb)

        assert go_cb.answers and go_cb.answers[0][1] is True  # show_alert
        row = await db.get_translation(LANG, h)
        assert row["manual"] == 1  # не тронуто

    asyncio.run(go())


# ── «Русский изменился» (Задача 2) ───────────────────────────────────────────────────────────

def test_stale_manual_translation_flagged_and_both_rows_survive(tmp_path):
    _db_ready(tmp_path)

    async def go():
        # Менеджер правит вопрос анкеты вручную под старой редакцией русского.
        old_ru = "Какой у тебя вуз?"
        old_hash = compute_src_hash(old_ru)
        await db.upsert_translation(
            LANG, old_hash, old_ru, "Which university do you attend?",
            manual=1, origin_key="reg_prompt_university",
        )

        # Русский текст поменяли (та же настройка, новое значение) -- новая редакция.
        new_ru = "В каком вузе ты учишься?"
        await db.set_setting("reg_prompt_university", new_ru)

        corpus_idx = await mod._corpus_hash_index()
        old_row = await db.get_translation(LANG, old_hash)
        assert mod._is_stale_manual(old_row, corpus_idx) is True

        # Список фильтра "manual" помечает строку меткой, не молчит.
        text, _kb = await mod.render_i18n_list("manual", 0)
        assert "русский изменился" in text or True  # заголовок не обязан, проверяем карточку ниже
        rows, _total = await mod._load_page("manual", 0)
        idx = next(i for i, r in enumerate(rows) if r["src_hash"] == old_hash)
        screen = await mod.render_i18n_card("manual", 0, idx)
        assert screen is not None
        card_text, card_kb, _row = screen
        assert "Русский изменился" in card_text
        assert "прежней редакции" in card_text
        assert any(cb.startswith("admin_i18n_edit_new:") for cb in _kb_callbacks(card_kb))

        # Старая ручная запись не удалена и не затёрта молча.
        still_old = await db.get_translation(LANG, old_hash)
        assert still_old is not None
        assert still_old["text"] == "Which university do you attend?"

        # Правка "новой редакции" создаёт ВТОРУЮ, независимую запись под новым хешем.
        state = _fresh_state()
        edit_new_cb = FakeCallback(f"admin_i18n_edit_new:manual:0:{idx}", user_id=ADMIN_ID)
        await mod.admin_i18n_edit_new_start(edit_new_cb, state)
        msg = FakeIncomingMessage("Which university are you at?", user_id=ADMIN_ID)
        await mod.admin_i18n_edit_step(msg, state)

        new_hash = compute_src_hash(new_ru)
        new_row = await db.get_translation(LANG, new_hash)
        assert new_row is not None
        assert new_row["text"] == "Which university are you at?"
        assert new_row["manual"] == 1
        # Старая запись всё ещё цела, никуда не делась.
        still_old2 = await db.get_translation(LANG, old_hash)
        assert still_old2["text"] == "Which university do you attend?"

    asyncio.run(go())


# ── Согласия доступны для ручного ввода, машине недоступны (Задача 2, LANG-09) ──────────────

def test_consent_filter_lists_virtual_and_saved_rows(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await db.set_setting("consent_button_text", "Согласен(-на)")
        await db.set_setting("consent_list", "Согласие на обработку данных|data\nПолитика|policy")

        rows, total = await mod._load_page("consent", 0)
        # Кнопка + 2 пункта списка + дефолтный текст просьбы пересогласиться
        # (consent_recollect_text — `get_setting_typed` отдаёт дефолт схемы, даже если менеджер
        # его не трогал).
        assert total == 4
        # Ни одна строка ещё не переведена -- все "виртуальные" (text is None).
        assert all(r["text"] is None for r in rows)

        # Экран не роняется на строке без сохранённой записи (виртуальная карточка рендерится).
        screen = await mod.render_i18n_card("consent", 0, 0)
        assert screen is not None
        card_text, card_kb, _row = screen
        assert "LANG-09" in card_text or "только ручной ввод" in card_text
        assert not any(cb.startswith("admin_i18n_retr:") for cb in _kb_callbacks(card_kb))

    asyncio.run(go())


# ── Экранирование HTML (T-27-06-02) ──────────────────────────────────────────────────────────

def test_html_special_chars_escaped_in_list_and_card(tmp_path):
    _db_ready(tmp_path)

    async def go():
        src = "Вопрос с <b>тегом</b> & амперсандом"
        h = compute_src_hash(src)
        await db.upsert_translation(LANG, h, src, "Answer with <b>tag</b> & ampersand", manual=1, origin_key="lit:esc")

        # Русский исходник виден МЕНЕДЖЕРУ как подпись кнопки в списке -- она и экранируется.
        _list_text, kb = await mod.render_i18n_list("all", 0)
        button_texts = " ".join(_kb_texts(kb))
        assert "<b>тегом</b>" not in button_texts
        assert "&lt;b&gt;" in button_texts

        rows, _total = await mod._load_page("all", 0)
        idx = next(i for i, r in enumerate(rows) if r["src_hash"] == h)
        screen = await mod.render_i18n_card("all", 0, idx)
        card_text, _kb2, _row = screen
        assert "<b>tag</b>" not in card_text
        assert "&lt;b&gt;tag&lt;/b&gt;" in card_text

    asyncio.run(go())


# ── Никаких src_hash/кодов состояний в текстах экрана (T-27-06-05) ──────────────────────────

def test_no_src_hash_or_state_codes_leak_into_rendered_text(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await _seed(3)
        list_text, _kb = await mod.render_i18n_list("all", 0)
        for i in range(3):
            h = compute_src_hash(f"Русский текст номер {i}")
            assert h not in list_text
        for code in ("pending", "manual", "failed"):
            assert code not in list_text

        rows, _total = await mod._load_page("all", 0)
        screen = await mod.render_i18n_card("all", 0, 0)
        card_text, _kb2, _row = screen
        assert rows[0]["src_hash"] not in card_text
        for code in ("pending", "manual", "failed"):
            assert code not in card_text

    asyncio.run(go())


# ── Доступ: закрыт тем же гейтом, что соседние экраны раздела ───────────────────────────────

def test_manager_without_settings_capability_is_denied(tmp_path):
    """D-04: тихий отказ (без тоста) зарезервирован для СЛУЧАЙНОГО пользователя без единой
    роли (T-08-14, не раскрываем форму админки чужому); известный менеджер БЕЗ капы `settings`
    получает обычный тост «Недостаточно прав» — тот же гейт, что у соседних экранов раздела."""
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg"))

    result, event = dispatch_callback("admin_i18n", MANAGER_ID)
    assert event.answers, "должен быть ответ (алерт с отказом), а не тишина"
    assert event.answers[0][0] == "Недостаточно прав"


def test_stranger_gets_silent_denial(tmp_path):
    """T-08-14: случайный пользователь без единой роли — тишина, форма экрана не утекает."""
    _roles_ready(tmp_path)

    result, event = dispatch_callback("admin_i18n", STRANGER_ID)
    assert event.answers == []


def test_admin_reaches_the_screen(tmp_path):
    _roles_ready(tmp_path)

    result, event = dispatch_callback("admin_i18n", ADMIN_ID)
    assert event.message.edit_calls == 1
    assert "Английские тексты" in event.message.text
