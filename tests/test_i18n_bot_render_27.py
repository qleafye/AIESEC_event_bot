"""Phase 27 (27-05, LANG-02) — перевод чата бота НА ОТПРАВКЕ: `handlers/reg_i18n.py`
(`tr_text`/`tr_kb`) и врезка в `handlers/registration.py::_safe_answer`/`_build_summary`.

Как и соседние тесты фазы (`tests/test_i18n_lang_27.py`) — Fake message, `asyncio.run()`,
pytest-asyncio в этом окружении нет. `reg_i18n.ctx_for` монкипатчится напрямую там, где нужен
конкретный `(lang, tr_map)` — тот же приём, что `tests/test_i18n_miniapp_27.py` использует для
`services.i18n.context`, только на один уровень ниже (сам `ctx_for`, не его зависимость),
потому что `_safe_answer`/`say()` зовут именно его, а не `services.i18n.context` напрямую.
"""
import asyncio

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config import config
from database import db
from handlers import reg_i18n
from handlers import registration as reg


def _db_ready(tmp_path, name="test_i18n_bot_render_27.db"):
    """DB для option_pairs/summary_value_maps (option_list_for читает city_options/goal_options
    через get_setting даже на дефолтном списке) — тот же приём, что
    tests/test_i18n_options_roundtrip_27.py::_db_ready."""
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())

UID = 820001


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    """Минимальный message — тот же контракт, что у _FakeMessage в
    tests/test_registration_send_guard_260816.py: _safe_answer трогает только .chat.id и
    .answer. Намеренно БЕЗ .from_user — reg_i18n.ctx_for обязан резолвить личность делегата по
    chat.id (см. докстринг handlers/reg_i18n.py), не падать на отсутствии from_user."""

    def __init__(self, chat_id=UID):
        self.chat = _FakeChat(chat_id)
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return "sent:%d" % len(self.calls)


def _patch_ctx(monkeypatch, lang, tr_map):
    """Подменяет reg_i18n.ctx_for на фиксированный (lang, tr_map) — без похода в БД. Патчим
    ИМЕННО reg_i18n.ctx_for (не services.i18n.context) — и _safe_answer, и say() зовут его
    напрямую."""
    async def _ctx(_target):
        return lang, dict(tr_map)

    monkeypatch.setattr(reg_i18n, "ctx_for", _ctx)


# ── Юнит: tr_text ────────────────────────────────────────────────────────────────────────

def test_tr_text_identity_at_ru():
    text = "Сколько тебе лет?"
    assert reg_i18n.tr_text(text, "ru", {"whatever": "x"}) is text


def test_tr_text_translates_via_tr_map_at_en():
    text = "Сколько тебе лет?"
    tr_map = {reg_i18n.i18n_service.src_hash(text): "How old are you?"}
    assert reg_i18n.tr_text(text, "en", tr_map) == "How old are you?"


def test_tr_text_progress_prefix_split_and_rejoined():
    text = "Сколько тебе лет?"
    tr_map = {reg_i18n.i18n_service.src_hash(text): "How old are you?"}
    prefixed = f"(3/9) {text}"
    assert reg_i18n.tr_text(prefixed, "en", tr_map) == "(3/9) How old are you?"


def test_tr_text_missing_translation_falls_back_to_russian_no_exception():
    text = "Совсем новый текст без перевода"
    assert reg_i18n.tr_text(text, "en", {}) == text


def test_tr_text_layer_a_wins_even_with_empty_map():
    # UI_EN не зависит от БД — "Пропустить" переводится даже с пустой картой tr_map.
    assert reg_i18n.tr_text("Пропустить", "en", {}) == "Skip"


# ── Юнит: tr_kb ──────────────────────────────────────────────────────────────────────────

def test_tr_kb_identity_at_ru():
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]])
    assert reg_i18n.tr_kb(kb, "ru", {}) is kb


def test_tr_kb_none_passthrough():
    assert reg_i18n.tr_kb(None, "en", {"a": "b"}) is None


def test_tr_kb_reply_markup_translates_button_text_layer_a():
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить")]])
    out = reg_i18n.tr_kb(kb, "en", {})
    assert out.keyboard[0][0].text == "Skip"
    assert out is not kb  # en -> пересборка, не тот же объект


def test_tr_kb_preserves_request_contact_and_translates_sibling_button():
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="\U0001f4f1 Поделиться контактом", request_contact=True)],
        [KeyboardButton(text="Пропустить")],
    ])
    out = reg_i18n.tr_kb(kb, "en", {})
    assert out.keyboard[0][0].request_contact is True
    assert out.keyboard[1][0].text == "Skip"


def test_tr_kb_inline_callback_data_untouched():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Пропустить", callback_data="regmulti:goal:0")],
        [InlineKeyboardButton(text="Готово", callback_data="regmulti_done:goal")],
    ])
    out = reg_i18n.tr_kb(kb, "en", {})
    assert out.inline_keyboard[0][0].callback_data == "regmulti:goal:0"
    assert out.inline_keyboard[1][0].callback_data == "regmulti_done:goal"
    assert out.inline_keyboard[1][0].text == "Done"


def test_tr_kb_non_keyboard_markup_passthrough():
    remove = ReplyKeyboardRemove()
    assert reg_i18n.tr_kb(remove, "en", {}) is remove


def test_multi_kb_labels_translate_callback_data_stays():
    kb = reg._multi_kb("goal", ["Пропустить", "Другое"], {0})
    out = reg_i18n.tr_kb(kb, "en", {})
    assert out.inline_keyboard[0][0].callback_data == "regmulti:goal:0"
    assert out.inline_keyboard[1][0].callback_data == "regmulti:goal:1"
    assert out.inline_keyboard[2][0].callback_data == "regmulti_done:goal"
    assert "Skip" in out.inline_keyboard[0][0].text  # выбран -> префикс "✅ "
    assert "Other" in out.inline_keyboard[1][0].text  # не выбран -> префикс "▫️ "
    assert out.inline_keyboard[2][0].text == "Done"


# ── Интеграция: _safe_answer ────────────────────────────────────────────────────────────

def test_safe_answer_module_off_returns_same_text_and_markup_objects(monkeypatch):
    _patch_ctx(monkeypatch, "ru", {})
    msg = _FakeMessage()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]])
    text = "Сколько тебе лет?"

    asyncio.run(reg._safe_answer(msg, text, reply_markup=kb))

    sent_text, kwargs = msg.calls[0]
    assert sent_text is text
    assert kwargs["reply_markup"] is kb


def test_safe_answer_translates_text_and_reply_kb_at_en(monkeypatch):
    q = "Сколько тебе лет?"
    tr_map = {reg_i18n.i18n_service.src_hash(q): "How old are you?"}
    _patch_ctx(monkeypatch, "en", tr_map)
    msg = _FakeMessage()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]])

    asyncio.run(reg._safe_answer(msg, q, reply_markup=kb))

    sent_text, kwargs = msg.calls[0]
    assert sent_text == "How old are you?"
    assert kwargs["reply_markup"].keyboard[0][0].text == "Cancel"


def test_safe_answer_inline_callback_data_unchanged_at_en(monkeypatch):
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage()
    kb = reg._multi_kb("goal", ["Пропустить"], set())

    asyncio.run(reg._safe_answer(msg, "Текст", reply_markup=kb))

    _, kwargs = msg.calls[0]
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "regmulti:goal:0"


def test_safe_answer_no_translation_available_sends_russian_no_exception(monkeypatch):
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage()

    asyncio.run(reg._safe_answer(msg, "Совсем новый текст без перевода", reply_markup=None))

    sent_text, _ = msg.calls[0]
    assert sent_text == "Совсем новый текст без перевода"


def test_safe_answer_translates_progress_prefix(monkeypatch):
    q = "Сколько тебе лет?"
    tr_map = {reg_i18n.i18n_service.src_hash(q): "How old are you?"}
    _patch_ctx(monkeypatch, "en", tr_map)
    msg = _FakeMessage()

    asyncio.run(reg._safe_answer(msg, f"(3/9) {q}", reply_markup=None))

    sent_text, _ = msg.calls[0]
    assert sent_text == "(3/9) How old are you?"


# ── Интеграция: _build_summary ──────────────────────────────────────────────────────────

def test_build_summary_default_lang_ru_unchanged():
    data = {"full_name": "Иванова Мария", "age": 19}
    out = reg._build_summary(data)
    assert "Проверь свои ответы:" in out
    assert "Иванова Мария" in out


def test_build_summary_translates_labels_keeps_values_verbatim():
    header = "Проверь свои ответы:"
    label = "ФИО"
    tr_map = {
        reg_i18n.i18n_service.src_hash(header): "Check your answers:",
        reg_i18n.i18n_service.src_hash(label): "Full name",
    }
    data = {"full_name": "Иванова Мария"}
    out = reg._build_summary(data, "en", tr_map)
    assert "Check your answers:" in out
    assert "Full name" in out
    assert "Иванова Мария" in out  # значение делегата НЕ переводится
    assert "ФИО" not in out


def test_build_summary_translates_closed_option_values_choice_and_multi(tmp_path):
    """UAT-фикс (стенд, lang=en, 27-05): значения closed-option полей сводки — choice (city) и
    multi (goal, ", ".join канонов) — переводятся канон -> подпись; свободный ввод (comments)
    остаётся как есть. Byte-identical output at lang="ru" — тот же вызов без lang/tr_map/
    value_maps (parity guard, тот же приём, что test_build_summary_default_lang_ru_unchanged)."""
    _db_ready(tmp_path)
    header = "Проверь свои ответы:"
    label_city, label_goal, label_comments = "Город", "Цель участия", "Комментарии"
    goal_a = "Найти возможность трудоустройства"
    goal_b = "Прокачать свои hard и soft skills"
    data = {
        "full_name": "Иванова Мария",
        "city": "Казань",
        "goal": f"{goal_a}, {goal_b}",
        "comments": "Свободный текст делегата",
    }

    async def go():
        tr_map = {
            reg_i18n.i18n_service.src_hash(header): "Check your answers:",
            reg_i18n.i18n_service.src_hash(label_city): "City",
            reg_i18n.i18n_service.src_hash(label_goal): "Goal",
            reg_i18n.i18n_service.src_hash(label_comments): "Comments",
            reg_i18n.i18n_service.src_hash("Казань"): "Kazan",
            reg_i18n.i18n_service.src_hash(goal_a): "Find a job opportunity",
            reg_i18n.i18n_service.src_hash(goal_b): "Improve hard and soft skills",
        }
        value_maps = await reg_i18n.summary_value_maps("en", tr_map)
        return reg._build_summary(data, "en", tr_map, value_maps)

    out_en = asyncio.run(go())

    assert "Check your answers:" in out_en
    assert "<b>City:</b> Kazan" in out_en
    assert "<b>Goal:</b> Find a job opportunity, Improve hard and soft skills" in out_en
    assert "<b>Comments:</b> Свободный текст делегата" in out_en  # свободный ввод не тронут
    assert "Казань" not in out_en
    assert goal_a not in out_en and goal_b not in out_en

    # lang="ru" (дефолт, без value_maps) — байт-в-байт то же, что и до этого фикса.
    out_ru = reg._build_summary(data)
    assert "<b>Город:</b> Казань" in out_ru
    assert f"<b>Цель участия:</b> {goal_a}, {goal_b}" in out_ru
    assert "<b>Комментарии:</b> Свободный текст делегата" in out_ru


# ── Гейт: составная строка summary не ломается повторной попыткой перевода в _safe_answer ──

def test_safe_answer_does_not_mangle_already_translated_summary(monkeypatch):
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage()
    summary_en = "<b>Check your answers:</b>\n\n<b>Full name:</b> Иванова Мария"

    asyncio.run(reg._safe_answer(msg, summary_en, parse_mode="HTML"))

    sent_text, _ = msg.calls[0]
    assert sent_text == summary_en
