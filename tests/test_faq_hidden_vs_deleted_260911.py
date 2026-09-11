"""Quick 260911-805 (W4-02): FAQ — «скрыт» не путается с «удалён».

УАТ-находка ночи 10-11.09: «Не удаляется вопрос FAQ, только скрывается» — бага удаления не
было (`delete_faq_item` жёстко удаляет строку, `database/db.py` этой волной не тронут),
путались кнопки карточки (скрытие соседствовало с удалением) и список менеджера не отличал
«скрыт» от «удалён» ничем, кроме одной иконки, а нажатие «скрыть» отвечало голой тишиной
(`callback.answer()` без текста) — менеджер решал, что кнопка не сработала вовсе.

Стиль и фейки — существующий `tests/test_faq_260906.py` (`_FakeCallback`/`_FakeMessage`,
`asyncio.run`, tmp_path, `config.ADMIN_IDS`).
"""
import asyncio

from config import config
from database import db
from handlers import admin_faq


ADMIN_ID = 8901301


def _run(coro):
    return asyncio.run(coro)


def _admin_ready(tmp_path, name="faq_hidden_vs_deleted.db"):
    config.DB_PATH = str(tmp_path / name)
    config.GOOGLE_SHEET_ID = ""
    _run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.text_edited = None
        self.edit_markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = message if message is not None else _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat(kb):
    return [(btn.callback_data, btn.text) for row in kb.inline_keyboard for btn in row]


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ── Алерт на переключении видимости ────────────────────────────────────────────────────────

def test_hide_visible_item_gives_alert_naming_state_and_delete_button(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    cb = _FakeCallback(f"afaq_t:{item_id}")

    _run(admin_faq.afaq_toggle_enabled(cb))

    row = _run(db.get_faq_item(item_id))
    assert row["enabled"] == 0
    assert cb.answers, "нажатие скрытия обязано ответить алертом, а не тишиной"
    text, show_alert = cb.answers[0]
    assert show_alert is True
    assert "скрыт" in text.lower()
    assert "удал" in text.lower()  # называет, чем удаляют
    assert "Удалить навсегда" in text


def test_show_hidden_item_gives_its_own_alert(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    _run(db.update_faq_item(item_id, enabled=0))
    cb = _FakeCallback(f"afaq_t:{item_id}")

    _run(admin_faq.afaq_toggle_enabled(cb))

    row = _run(db.get_faq_item(item_id))
    assert row["enabled"] == 1
    text, show_alert = cb.answers[0]
    assert show_alert is True
    assert "виден делегатам" in text.lower()


# ── Кнопки скрытия и удаления никогда не соседние ──────────────────────────────────────────

def test_hide_and_delete_buttons_not_adjacent_without_cities_module(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))

    text, kb = _run(admin_faq.render_faq_card(ADMIN_ID, item_id))
    rows = kb.inline_keyboard
    toggle_row = next(i for i, r in enumerate(rows) if any(b.callback_data == f"afaq_t:{item_id}" for b in r))
    delete_row = next(i for i, r in enumerate(rows) if any(b.callback_data == f"afaq_d:{item_id}" for b in r))
    assert abs(toggle_row - delete_row) > 1


def test_hide_and_delete_buttons_not_adjacent_with_cities_module_bound_header(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    import cities as cities_mod
    _run(db.insert_city("msk", "Москва", "", 0))
    _run(db.set_setting("event_city_enabled", "on"))
    _run(cities_mod.reload_cities())
    _run(cities_mod.set_admin_city(ADMIN_ID, "msk"))
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))

    text, kb = _run(admin_faq.render_faq_card(ADMIN_ID, item_id))
    rows = kb.inline_keyboard
    toggle_row = next(i for i, r in enumerate(rows) if any(b.callback_data == f"afaq_t:{item_id}" for b in r))
    delete_row = next(i for i, r in enumerate(rows) if any(b.callback_data == f"afaq_d:{item_id}" for b in r))
    assert abs(toggle_row - delete_row) > 1

    cities_mod.CITIES.clear()


# ── Подписи ──────────────────────────────────────────────────────────────────────────────

def test_delete_button_label_says_forever_and_callback_unchanged(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    text, kb = _run(admin_faq.render_faq_card(ADMIN_ID, item_id))
    labels = dict(_flat(kb))
    assert labels[f"afaq_d:{item_id}"] == "🗑 Удалить навсегда"


def test_toggle_labels_name_the_audience(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    text, kb = _run(admin_faq.render_faq_card(ADMIN_ID, item_id))
    labels = dict(_flat(kb))
    assert "делегат" in labels[f"afaq_t:{item_id}"].lower()

    _run(db.update_faq_item(item_id, enabled=0))
    text2, kb2 = _run(admin_faq.render_faq_card(ADMIN_ID, item_id))
    labels2 = dict(_flat(kb2))
    assert "делегат" in labels2[f"afaq_t:{item_id}"].lower()
    assert labels[f"afaq_t:{item_id}"] != labels2[f"afaq_t:{item_id}"]


# ── Список менеджера словами отличает скрытый пункт ────────────────────────────────────────

def test_list_marks_hidden_item_with_word_not_only_icon(tmp_path):
    _admin_ready(tmp_path)
    a = _run(db.create_faq_item(city=None, question="Видимый?", answer="a", created_by=ADMIN_ID))
    b = _run(db.create_faq_item(city=None, question="Скрытый?", answer="b", created_by=ADMIN_ID))
    _run(db.update_faq_item(b, enabled=0))

    text, kb = _run(admin_faq.render_faq_screen(ADMIN_ID))
    row_labels = {btn.callback_data: btn.text for row in kb.inline_keyboard for btn in row}
    assert "скрыт" in row_labels[f"afaq_v:{b}"].lower()
    assert "скрыт" not in row_labels[f"afaq_v:{a}"].lower()


def test_list_no_hidden_items_shows_no_extra_lines(tmp_path):
    _admin_ready(tmp_path)
    _run(db.create_faq_item(city=None, question="Видимый?", answer="a", created_by=ADMIN_ID))

    text, kb = _run(admin_faq.render_faq_screen(ADMIN_ID))
    assert "скрыт" not in text.lower()


def test_list_with_hidden_items_explains_difference_and_counts(tmp_path):
    _admin_ready(tmp_path)
    a = _run(db.create_faq_item(city=None, question="Видимый?", answer="a", created_by=ADMIN_ID))
    b = _run(db.create_faq_item(city=None, question="Скрытый?", answer="b", created_by=ADMIN_ID))
    _run(db.update_faq_item(b, enabled=0))

    text, kb = _run(admin_faq.render_faq_screen(ADMIN_ID))
    assert "1" in text  # счётчик скрытых
    assert "скрыт" in text.lower() and "удал" in text.lower()


# ── Экран подтверждения удаления предлагает скрыть вместо удаления ─────────────────────────

def test_delete_confirm_offers_hide_instead_for_visible_item(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    cb = _FakeCallback(f"afaq_d:{item_id}")

    _run(admin_faq.afaq_delete_confirm(cb))

    cbs = _cbs(cb.message.edit_markup)
    assert f"afaq_dgo:{item_id}" in cbs  # прежний
    assert f"afaq_v:{item_id}" in cbs  # прежний
    assert f"afaq_t:{item_id}" in cbs  # выход «скрыть вместо удаления» — существующий callback
    labels = dict(_flat(cb.message.edit_markup))
    assert "скрыть" in labels[f"afaq_t:{item_id}"].lower()
    assert "навсегда" in cb.message.text_edited.lower()


def test_delete_confirm_hidden_item_has_no_redundant_hide_button(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    _run(db.update_faq_item(item_id, enabled=0))
    cb = _FakeCallback(f"afaq_d:{item_id}")

    _run(admin_faq.afaq_delete_confirm(cb))

    cbs = _cbs(cb.message.edit_markup)
    assert f"afaq_t:{item_id}" not in cbs


def test_delete_go_removes_item_everywhere_and_alert_says_forever(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    go_cb = _FakeCallback(f"afaq_dgo:{item_id}")

    _run(admin_faq.afaq_delete_go(go_cb))

    assert _run(db.get_faq_item(item_id)) is None
    text, show_alert = go_cb.answers[0]
    assert "навсегда" in text.lower()


def test_list_admin_still_sees_hidden_items_enabled_only_false(tmp_path):
    """Регресс: `list_faq_items` в админском списке остаётся БЕЗ `enabled_only` — менеджер
    видит скрытые пункты (делегатская видимость `services/faq.py` не тронута отдельно)."""
    _admin_ready(tmp_path)
    hidden_id = _run(db.create_faq_item(city=None, question="Скрыт?", answer="b", created_by=ADMIN_ID))
    _run(db.update_faq_item(hidden_id, enabled=0))

    text, kb = _run(admin_faq.render_faq_screen(ADMIN_ID))
    cbs = _cbs(kb)
    assert f"afaq_v:{hidden_id}" in cbs
