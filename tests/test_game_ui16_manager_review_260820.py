"""Phase 16 Plan 04 (GAME-UI-03, менеджерская половина): карточка модерации (Экран 5),
«🪙 Монеты вручную» — quick-pick сумм + карточка подтверждения «было → станет» (Экран 8),
«📊 Статистика геймы» — unicode-полосы по RU-категориям (Экран 9).

Хендлеры зовутся напрямую с Fake-дублями (та же конвенция, что
tests/test_gamification_review_phase9.py и tests/test_coins_manual_260818.py — дубли скопированы
и расширены, не импортированы). pytest-asyncio недоступен — всё через asyncio.run(),
config.DB_PATH -> tmp_path.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin_gamification
from handlers import admin_settings
from handlers.admin_caps import required_capability
from handlers.states import CoinsManual
from settings_schema import SETTINGS_SCHEMA


ADMIN_ID = 941601
DELEGATE_ID = 941602
DELEGATE2_ID = 941603


def _db_ready(tmp_path, name="test_game_ui16_manager_review_260820.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))

    async def send_media_group(self, chat_id, media):
        self.sent.append((chat_id, media))


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID, bot=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.deleted = False
        self.answers_sent = []
        self.answer_markups = []
        self.answer_parse_modes = []
        self.photos_sent = []
        self.documents_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.answer_parse_modes.append(parse_mode)
        self.text = text

    async def answer_photo(self, photo):
        self.photos_sent.append(photo)

    async def answer_document(self, document):
        self.documents_sent.append(document)

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, bot=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.message = FakeMessage(user_id=user_id, bot=self.bot)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_buttons(kb):
    return [(btn.text, btn.callback_data) for row in kb.inline_keyboard for btn in row]


def _seed_delegate(user_id=DELEGATE_ID, username="@delegate1", full_name="Пётр Сидоров",
                   event_city=None):
    asyncio.run(db.add_user({
        "telegram_id": user_id, "username": username, "full_name": full_name,
        "registration_date": "2026-08-01", "event_city": event_city,
    }))


def _card_row(**over):
    row = {
        "task_text": "Ребус-квест, реши и пришли ответ", "task_category": "Medium", "task_coins": 25,
        "user_full_name": "Пётр Сидоров", "user_username": "@petr", "task_proof_type": "text",
        "content_type": "text", "content": "42, потому что",
    }
    row.update(over)
    return row


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 1a: карточка модерации — RU-категория, «Осталось: N», «✅ Принять»
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_render_submission_card_ru_category_and_remaining():
    text = admin_gamification._render_submission_card(
        _card_row(), 2, 5, category_label_text="Среднее", remaining=3,
    )
    assert "Категория: Среднее" in text
    assert "Medium" not in text
    assert "Осталось: 3" in text
    # «Осталось» — в шапке, сразу после строки «Сдача 2/5», не в хвосте карточки.
    lines = text.split("\n")
    assert lines[0] == "🎮 <b>Сдача 2/5</b>"
    assert lines[1] == "Осталось: 3"


def test_render_submission_card_defaults_keep_raw_category_and_no_remaining():
    """Регресс-гард: старая форма вызова (без новых kwargs) рендерит как раньше — сырая
    категория, никакого «Осталось»."""
    text = admin_gamification._render_submission_card(_card_row(), 1, 1)
    assert "Категория: Medium" in text
    assert "Осталось" not in text
    # remaining=0 (последняя карточка) — строка не появляется, чтобы не писать «Осталось: 0».
    last = admin_gamification._render_submission_card(_card_row(), 5, 5, category_label_text="Среднее", remaining=0)
    assert "Осталось" not in last
    assert "Категория: Среднее" in last


def test_submission_card_kb_approve_button_has_no_amount():
    kb = admin_gamification._submission_card_kb(1, 25)
    flat = _flat_buttons(kb)
    assert ("✅ Принять", "grev_approve:1") in flat
    assert not any("Одобрить" in text for text, _ in flat)
    assert not any("25" in text for text, _ in flat)
    assert ("✏️ Другая сумма", "grev_approve_custom:1") in flat
    assert ("❌ Отклонить", "grev_reject:1") in flat
    assert ("⏭ Пропустить", "grev_skip:1") in flat


def test_show_current_submission_wires_ru_category_and_remaining(tmp_path):
    """Очередь из 5 сдач, первая пропущена -> карточка 2/5, «Осталось: 3», категория по-русски
    (реестр `game_category_label_light`, дефолт «Лёгкое»)."""
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID))
    sids = []
    for i in range(5):
        sids.append(asyncio.run(db.create_submission(task_id, 700 + i, "text", f"c{i}", f"2026-08-14 10:0{i}:00")))
    state = _new_state()
    asyncio.run(state.update_data(grev_skipped=[sids[0]]))
    target = FakeMessage()
    asyncio.run(admin_gamification._show_current_submission(target, state))
    card = target.answers_sent[-1]
    assert "Сдача 2/5" in card
    assert "Осталось: 3" in card
    assert "Категория: Лёгкое" in card
    assert "Light" not in card
    kb = target.answer_markups[-1]
    assert ("✅ Принять", f"grev_approve:{sids[1]}") in _flat_buttons(kb)


def test_show_current_submission_last_card_has_no_remaining_line(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Hard", 10, "text", "2099-01-01 00:00:00", ADMIN_ID))
    asyncio.run(db.create_submission(task_id, 700, "text", "c", "2026-08-14 10:00:00"))
    state = _new_state()
    target = FakeMessage()
    asyncio.run(admin_gamification._show_current_submission(target, state))
    card = target.answers_sent[-1]
    assert "Сдача 1/1" in card
    assert "Осталось" not in card
    assert "Категория: Сложное" in card


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 1b: карточка подтверждения ручных монет — «было → станет» + город
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_render_coinsman_confirm_card_balance_transition():
    text = admin_gamification._render_coinsman_confirm_card("Пётр Сидоров", 5, "за помощь", balance_before=120)
    assert "120" in text and "125" in text
    assert "Баланс сейчас: 120🪙 → станет: 125🪙" in text
    assert "Сумма:" not in text
    assert "Кому: Пётр Сидоров" in text
    assert "Причина: за помощь" in text


def test_render_coinsman_confirm_card_negative_delta_transition():
    text = admin_gamification._render_coinsman_confirm_card("Пётр Сидоров", -3, "за нарушение", balance_before=10)
    assert "Баланс сейчас: 10🪙 → станет: 7🪙" in text
    assert "-3" in text  # дельта остаётся видимой рядом с переходом


def test_render_coinsman_confirm_card_default_is_legacy_sum_line():
    """Регресс-гард (tests/test_coins_manual_260818.py): без balance_before — прежняя строка
    «Сумма: +5 монет(ы)», никакого «станет», никакого города."""
    text = admin_gamification._render_coinsman_confirm_card("Пётр Сидоров", 5, "за помощь")
    assert text == (
        "🪙 <b>Подтвердите операцию</b>\n\n"
        "Кому: Пётр Сидоров\n"
        "Сумма: +5 монет(ы)\n"
        "Причина: за помощь\n\n"
        "Делегат получит сообщение с суммой, причиной и новым балансом."
    )


def test_render_coinsman_confirm_card_city_on_recipient_line():
    text = admin_gamification._render_coinsman_confirm_card(
        "Пётр Сидоров", 5, "за помощь", balance_before=120, city_label_text="Москва",
    )
    recipient_line = next(l for l in text.split("\n") if l.startswith("Кому:"))
    assert recipient_line == "Кому: Пётр Сидоров · 🏙 Москва"
    without = admin_gamification._render_coinsman_confirm_card("Пётр Сидоров", 5, "за помощь", balance_before=120)
    assert "🏙" not in without


def test_render_coinsman_confirm_card_escapes_city_and_reason():
    text = admin_gamification._render_coinsman_confirm_card(
        "Имя", 1, "<b>x</b>", balance_before=0, city_label_text="<i>Город</i>",
    )
    assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt;" in text
    assert "<i>Город</i>" not in text and "&lt;i&gt;Город&lt;/i&gt;" in text


def test_coinsman_reason_step_confirm_card_shows_transition_module_off(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 120, reason="seed", changed_by=ADMIN_ID, source="manual"))
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus", cm_delta=5))
    asyncio.run(state.set_state(CoinsManual.reason))
    msg = FakeMessage(text="за помощь на стенде")
    asyncio.run(admin_gamification.coinsman_reason_step(msg, state))
    card = msg.answers_sent[-1]
    assert "Баланс сейчас: 120🪙 → станет: 125🪙" in card
    assert "🏙" not in card  # модуль городов выключен — города нет
    assert "Причина: за помощь на стенде" in card


def test_coinsman_reason_step_confirm_card_shows_city_when_module_on(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_delegate(event_city="spb")
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="minus", cm_delta=-2))
    asyncio.run(state.set_state(CoinsManual.reason))
    msg = FakeMessage(text="за опоздание")
    asyncio.run(admin_gamification.coinsman_reason_step(msg, state))
    card = msg.answers_sent[-1]
    assert "Баланс сейчас: 0🪙 → станет: -2🪙" in card
    assert "🏙 Санкт-Петербург" in card


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 1c: quick-pick сумм из реестра coins_manual_amount_presets
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_coins_manual_amount_presets_registry_key():
    entry = SETTINGS_SCHEMA["coins_manual_amount_presets"]
    assert entry["group"] == "game"
    assert entry["type"] == "text"
    assert entry["default"] == "5,10,20"
    assert "coins_manual_amount_presets" in admin_settings._GAME_FIELD_ORDER


def test_coinsman_amount_kb_plus_and_minus_from_default_presets(tmp_path):
    _db_ready(tmp_path)
    plus = asyncio.run(admin_gamification._coinsman_amount_kb("plus"))
    assert _flat_buttons(plus) == [("+5", "coinsman_amount:5"), ("+10", "coinsman_amount:10"), ("+20", "coinsman_amount:20")]
    minus = asyncio.run(admin_gamification._coinsman_amount_kb("minus"))
    assert [t for t, _ in _flat_buttons(minus)] == ["-5", "-10", "-20"]
    assert len(plus.inline_keyboard) == 1  # одна строка кнопок


def test_coinsman_amount_kb_skips_non_numeric_entries_fail_soft(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("coins_manual_amount_presets", " 5 , abc, 15,, -3, 0"))
    kb = asyncio.run(admin_gamification._coinsman_amount_kb("plus"))
    assert _flat_buttons(kb) == [("+5", "coinsman_amount:5"), ("+15", "coinsman_amount:15")]


def test_coinsman_amount_kb_all_garbage_gives_no_buttons(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("coins_manual_amount_presets", "abc; def"))
    kb = asyncio.run(admin_gamification._coinsman_amount_kb("plus"))
    assert kb.inline_keyboard == []


def test_coinsman_sign_step_sends_prompt_then_quick_pick(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID))
    asyncio.run(state.set_state(CoinsManual.person))
    callback = FakeCallback("coinsman_sign:minus")
    asyncio.run(admin_gamification.coinsman_sign_step(callback, state))
    assert asyncio.run(state.get_state()) == CoinsManual.amount
    msgs = callback.message.answers_sent
    assert any("Сколько монет" in m for m in msgs)
    inline = [kb for kb in callback.message.answer_markups if kb is not None and hasattr(kb, "inline_keyboard")]
    assert inline, "второе сообщение с inline quick-pick не отправлено"
    assert [t for t, _ in _flat_buttons(inline[-1])] == ["-5", "-10", "-20"]


def test_coinsman_amount_step_pick_plus_moves_to_reason(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus"))
    asyncio.run(state.set_state(CoinsManual.amount))
    callback = FakeCallback("coinsman_amount:10")
    asyncio.run(admin_gamification.coinsman_amount_step_pick(callback, state))
    assert asyncio.run(state.get_state()) == CoinsManual.reason
    assert asyncio.run(state.get_data()).get("cm_delta") == 10
    assert any("За что?" in m for m in callback.message.answers_sent)
    assert callback.answers


def test_coinsman_amount_step_pick_minus_applies_sign(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="minus"))
    asyncio.run(state.set_state(CoinsManual.amount))
    callback = FakeCallback("coinsman_amount:20")
    asyncio.run(admin_gamification.coinsman_amount_step_pick(callback, state))
    assert asyncio.run(state.get_data()).get("cm_delta") == -20
    assert asyncio.run(state.get_state()) == CoinsManual.reason


def test_coinsman_amount_step_pick_stale_without_sign_alerts(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(CoinsManual.amount))
    callback = FakeCallback("coinsman_amount:5")
    asyncio.run(admin_gamification.coinsman_amount_step_pick(callback, state))
    assert asyncio.run(state.get_state()) == CoinsManual.amount
    assert asyncio.run(state.get_data()).get("cm_delta") is None
    assert callback.answers and callback.answers[-1][1] is True  # show_alert


def test_coinsman_amount_step_pick_garbage_value_alerts(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus"))
    asyncio.run(state.set_state(CoinsManual.amount))
    for bad in ("coinsman_amount:abc", "coinsman_amount:0", "coinsman_amount:-4"):
        callback = FakeCallback(bad)
        asyncio.run(admin_gamification.coinsman_amount_step_pick(callback, state))
        assert asyncio.run(state.get_state()) == CoinsManual.amount
        assert asyncio.run(state.get_data()).get("cm_delta") is None
        assert callback.answers[-1][1] is True


def test_coinsman_typed_amount_still_works_alongside_quick_pick(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus"))
    asyncio.run(state.set_state(CoinsManual.amount))
    msg = FakeMessage(text="7")
    asyncio.run(admin_gamification.coinsman_amount_step(msg, state))
    assert asyncio.run(state.get_data()).get("cm_delta") == 7
    assert asyncio.run(state.get_state()) == CoinsManual.reason


def test_coinsman_amount_callback_resolves_to_moderate_game(tmp_path):
    _db_ready(tmp_path)
    assert required_capability(callback_data="coinsman_amount:5") == "moderate_game"


class _KbMessage(FakeMessage):
    """FakeMessage + edit_reply_markup (учёт снятия inline-клавиатуры)."""
    def __init__(self, *a, fail=False, **kw):
        super().__init__(*a, **kw)
        self.markup_edits = []
        self._fail = fail

    async def edit_reply_markup(self, reply_markup=None):
        if self._fail:
            raise RuntimeError("message is not modified")
        self.markup_edits.append(reply_markup)


def test_coinsman_amount_stale_answers_and_drops_keyboard(tmp_path):
    """Quick 260819: тап по «Или выберите сумму:» ВНЕ CoinsManual.amount (сумма уже введена
    текстом / визард закрыт) -- catch-all отвечает без alert, убирает клавиатуру, стейт не
    трогает, монеты не пишет."""
    _db_ready(tmp_path)
    callback = FakeCallback("coinsman_amount:10")
    callback.message = _KbMessage()
    asyncio.run(admin_gamification.coinsman_amount_stale(callback))
    assert callback.answers == [("Выбор суммы уже закрыт", False)]
    assert callback.message.markup_edits == [None]
    assert callback.message.answers_sent == []
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 0


def test_coinsman_amount_stale_is_fail_soft_when_markup_edit_fails(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("coinsman_amount:10")
    callback.message = _KbMessage(fail=True)
    asyncio.run(admin_gamification.coinsman_amount_stale(callback))  # не падает
    assert callback.answers == [("Выбор суммы уже закрыт", False)]


def test_coinsman_amount_stale_registered_after_state_gated_pick():
    """Порядок регистрации = first-match: state-гейтнутый pick ДО catch-all, иначе catch-all
    перехватит легитимный тап в CoinsManual.amount."""
    names = [h.callback.__name__ for h in admin_gamification.router.callback_query.handlers]
    assert names.index("coinsman_amount_step_pick") < names.index("coinsman_amount_stale")


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 2: «📊 Статистика геймы» — unicode-полосы по RU-категориям
# ═══════════════════════════════════════════════════════════════════════════════════════════

def _run_stats_with(monkeypatch, stats: dict) -> str:
    async def fake_stats():
        return stats

    monkeypatch.setattr(admin_gamification, "get_game_stats", fake_stats)
    callback = FakeCallback("admin_game_stats")
    asyncio.run(admin_gamification.show_game_stats(callback))
    assert callback.answers
    return callback.message.answers_sent[-1]


def test_show_game_stats_empty_state_unchanged(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    text = _run_stats_with(monkeypatch, {"participants": 0, "pending": 0, "approved": 0, "rejected": 0, "by_category": {}})
    assert text == "📊 <b>Статистика геймификации</b>\n\nПока никто ничего не сдавал."


def test_show_game_stats_bars_scaled_to_max_and_ru_labels(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    text = _run_stats_with(monkeypatch, {
        "participants": 84, "pending": 5, "approved": 61, "rejected": 9,
        "by_category": {"Light": 30, "Medium": 22, "Hard": 9},
    })
    assert "👥 Участников: 84" in text
    assert "⏳ На проверке: 5" in text
    assert "✅ Одобрено: 61" in text
    assert "❌ Отклонено: 9" in text
    assert text.count("<pre>") == 1 and text.count("</pre>") == 1
    pre = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
    rows = pre.split("\n")
    assert len(rows) == 3
    assert rows[0].startswith("Лёгкое") and rows[0].endswith(" 30")
    assert rows[1].startswith("Среднее") and rows[1].endswith(" 22")
    assert rows[2].startswith("Сложное") and rows[2].endswith(" 9")
    bars = [r.split()[1] for r in rows]  # «Лёгкое   ▇▇▇▇▇▇▇▇▇▇ 30» -> второй токен — полоса
    assert all(len(b) == 10 for b in bars)
    assert bars[0] == "▇" * 10  # максимум -> полная полоса
    assert bars[1] == "▇" * 7 + "░" * 3  # round(10*22/30) = 7
    assert bars[2] == "▇" * 3 + "░" * 7  # round(10*9/30) = 3
    assert "Light" not in text and "Medium" not in text and "Hard" not in text
    assert "•" not in text  # старый маркированный список ушёл


def test_show_game_stats_nonzero_small_category_gets_at_least_one_block(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    text = _run_stats_with(monkeypatch, {
        "participants": 3, "pending": 0, "approved": 101, "rejected": 0,
        "by_category": {"Light": 100, "Special": 1},
    })
    pre = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
    rows = pre.split("\n")
    assert len(rows) == 2  # нулевые категории (Medium/Hard/Referral) пропущены
    assert rows[1].startswith("Особое")
    assert rows[1].split()[1] == "▇" + "░" * 9


def test_show_game_stats_no_approvals_yet_keeps_placeholder(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    text = _run_stats_with(monkeypatch, {"participants": 1, "pending": 1, "approved": 0, "rejected": 0, "by_category": {}})
    assert "пока нет одобренных сдач" in text
    assert "<pre>" not in text
