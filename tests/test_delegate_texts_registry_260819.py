"""Phase 17.1 (17.1-01, 17.1-02, 17.1-03) — делегатские тексты гейма/баланса/рейтинга/рефералки,
затем recall/возвращения и платёжного потока, затем empty-state'ы медиа/контактов и «❓ Задать
вопрос» переехали из литералов в SETTINGS_SCHEMA. Два вида проверок на каждую поверхность:

1) «дефолт байт-в-байт» — `SETTINGS_SCHEMA[key]["default"]` равен ровно тому литералу, который
   стоял в коде до миграции (эмодзи, переносы, HTML — как было; изменений поведения нет);
2) «ключ переопределяет дефолт» — записали значение в bot_settings, экран показал его.

Хендлеры зовутся напрямую с Fake message/callback, как в
tests/test_game_ui16_delegate_260820.py (pytest-asyncio в этом окружении нет).
"""
import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
# handlers.admin импортируется ПЕРВЫМ намеренно: admin_settings в одиночку не импортируется
# (цикл admin <-> admin_settings, та же идиома, что в tests/test_settings_groups_c0x.py).
from handlers import admin as _admin_mod  # noqa: F401
from handlers import admin_settings
from handlers import game_labels
from handlers import payment as pay_mod
from handlers import registration as reg_mod
from handlers import user_actions as ua_mod
from handlers.states import Registration
from settings_schema import SETTINGS_SCHEMA
from tests.test_registration_phase5 import _CapturingMessage as _RegCapturingMessage


ADMIN_ID = 941101
DELEGATE_ID = 941102


def _db_ready(tmp_path, name="test_delegate_texts_registry.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_delegate(uid=DELEGATE_ID, **extra):
    data = {
        "telegram_id": uid, "full_name": f"Delegate {uid}", "registration_date": "2026-08-01",
    }
    data.update(extra)
    asyncio.run(db.add_user(data))


def _mk_task(**kwargs):
    defaults = dict(text="Текст задания", category="Light", coins=20,
                    proof_type="photo", deadline_at="2099-01-01 00:00:00", created_by=ADMIN_ID)
    defaults.update(kwargs)
    return asyncio.run(db.create_task(**defaults))


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None
        self.username = None


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers_sent = []
        self.text_edited = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.answers_sent.append(caption)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text


class FakeCallback:
    def __init__(self, data, user_id=DELEGATE_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBotUser:
    username = "AIESEC_test_bot"


class FakeBot:
    async def get_me(self):
        return FakeBotUser()


def _default(key):
    return SETTINGS_SCHEMA[key]["default"]


# ── Дефолты байт-в-байт равны до-миграционным литералам ─────────────────────────────────
#
# Это единственное место, где литералы остаются зашитыми НАМЕРЕННО: тест сторожит контракт
# «менеджер ничего не настраивал -> делегат видит ровно тот же текст, что и до 17.1-01».
# Все остальные тесты сравнивают с `SETTINGS_SCHEMA[...]["default"]`, а не с литералом.

_PRE_MIGRATION_LITERALS = {
    "pending_gate_text": "⏳ Твоя заявка на рассмотрении. Доступ откроется после одобрения.",
    "game_proof_type_label_photo": "📷 Скриншот/фото",
    "game_proof_type_label_pdf": "📄 PDF",
    "game_proof_type_label_text": "✍️ Текст",
    "game_proof_type_label_link": "🔗 Ссылка",
    "game_proof_type_unspecified_text": "не важно",
    "game_task_overdue_hint_text": "⏰ Срок вышел — отправить можно, монеты решит менеджер",
    "leaderboard_header_text": "🏆 <b>Рейтинг по монетам</b>",
    "leaderboard_empty_text": "Пока ни у кого нет монет.",
    "leaderboard_rank_line_text": "Твоё место: <b>{rank}</b> · баланс: <b>{balance}</b>",
    "balance_history_header_text": "📜 <b>История монет</b>",
    "referral_link_prompt_text": (
        "Отправь эту ссылку друзьям, чтобы пригласить их на форум!\n\n{link}"
    ),
    "referral_list_header_text": "👥 <b>Твои приглашённые ({count}):</b>",
    "referral_list_empty_text": (
        "Пока никто не зарегистрировался по твоей ссылке.\n\nПоделись ей с друзьями:\n{link}"
    ),
    # 17.1-02: recall/возвращение (registration.py) + платёжный поток (payment.py).
    "start_returning_cta_text": (
        "Хочешь участвовать снова? Обновим анкету — прошлые ответы предложу оставить."
    ),
    "recall_resume_prompt_text": (
        "У нас есть твоё резюме с прошлой регистрации. Оставить его или прислать новое?"
    ),
    "recall_generic_prompt_text": (
        "<b>{label}</b>\n\nПрошлый ответ: <b>{display}</b>\n\nОставить или изменить?"
    ),
    "payment_option_picker_header_text": "💳 Выбери вариант участия:",
    # Прежний экран собирался как "\n".join(parts) с условными блоками; шаблон воспроизводит
    # тот же рендер: условные блоки приходят УЖЕ с завершающим "\n\n" (или пустые). Полная
    # байт-идентичность рендера — test_payment_details_default_template_renders_byte_identical_to_legacy.
    "payment_details_template_text": (
        "💰 <b>Оплата участия</b>\n\nВариант: {option}\nСумма: {amount} ₽\n\n"
        "{requisites}{deadline}{penalties}📎 Загрузи чек оплаты (PDF-документ или скриншот)."
    ),
    "payment_pay_later_text": "Ок! Оплатишь позже.",
    "payment_pay_later_menu_hint_text": "Кнопка «💳 Оплата» будет в меню, пока чек не отправлен.",
    "payment_receipt_received_text": "✅ Чек получен! Менеджер проверит его в ближайшее время.",
    # 17.1-03: empty-state'ы информационных кнопок меню + «❓ Задать вопрос» (user_actions.py).
    "program_empty_text": "Программа форума ещё не загружена.",
    "speakers_empty_text": "Список спикеров формируется и скоро появится здесь.",
    "contacts_empty_text": "Контакты пока не указаны. Обратитесь к организаторам.",
    "ask_question_prompt_text": "Напиши свой вопрос, и мы передадим его организаторам.",
    "ask_question_sent_text": "Твой вопрос отправлен!",
    # 17.1-03 (schema-completeness): читались из bot_settings, но не были объявлены в реестре.
    "city_fork_text": "Выбери город мероприятия:",
    "party_fork_text": "Выбери формат участия:",
    "preselect_no_username_text": (
        "Чтобы продолжить, задайте @username в настройках Telegram и снова отправьте /start."
    ),
    "preselect_fail_text": "Отбор не пройден.",
}


@pytest.mark.parametrize("key,literal", sorted(_PRE_MIGRATION_LITERALS.items()))
def test_registry_default_is_byte_identical_to_pre_migration_literal(key, literal):
    assert SETTINGS_SCHEMA[key]["default"] == literal


@pytest.mark.parametrize("key", sorted(_PRE_MIGRATION_LITERALS))
def test_every_new_key_is_a_text_entry_in_a_declared_group(key):
    entry = SETTINGS_SCHEMA[key]
    assert entry["type"] == "text"
    assert entry["prompt"]  # менеджер видит человеческое объяснение, а не пустоту
    grouped = [k for _, __, keys in admin_settings.SETTINGS_GROUPS for k in keys]
    assert key in grouped, f"{key} не попал ни в одну группу настроек"


# ── ensure_registered: гейт «заявка на рассмотрении» ────────────────────────────────────

def test_pending_gate_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.set_user_status(DELEGATE_ID, "pending"))

    message = FakeMessage()
    assert asyncio.run(ua_mod.ensure_registered(message)) is False
    assert message.answers_sent == [_default("pending_gate_text")]


def test_pending_gate_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.set_user_status(DELEGATE_ID, "pending"))
    asyncio.run(db.set_setting("pending_gate_text", "Ждём решения оргкомитета 🙌"))

    message = FakeMessage()
    assert asyncio.run(ua_mod.ensure_registered(message)) is False
    assert message.answers_sent == ["Ждём решения оргкомитета 🙌"]


# ── «🏆 Рейтинг»: заголовок / пустой экран / строка «твоё место» ─────────────────────────

def test_leaderboard_uses_registry_defaults(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 7, source="manual"))
    rows = asyncio.run(db.get_leaderboard(10))

    out = asyncio.run(ua_mod.render_leaderboard(rows, DELEGATE_ID, 1, 7))
    assert out.startswith(_default("leaderboard_header_text"))
    assert out.endswith(
        _default("leaderboard_rank_line_text").format(rank=1, balance=7, total=1)
    )


def test_leaderboard_settings_override_defaults(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 7, source="manual"))
    asyncio.run(db.set_setting("leaderboard_header_text", "🥇 Топ монетоносцев"))
    asyncio.run(db.set_setting(
        "leaderboard_rank_line_text", "Ты {rank}-й из {total}, монет — {balance}",
    ))
    rows = asyncio.run(db.get_leaderboard(10))

    out = asyncio.run(ua_mod.render_leaderboard(rows, DELEGATE_ID, 1, 7))
    assert out.startswith("🥇 Топ монетоносцев")
    assert out.endswith("Ты 1-й из 1, монет — 7")


def test_leaderboard_empty_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()

    out = asyncio.run(ua_mod.render_leaderboard([], DELEGATE_ID, None, 0))
    assert _default("leaderboard_empty_text") in out

    asyncio.run(db.set_setting("leaderboard_empty_text", "Монет пока нет ни у кого 🙂"))
    out = asyncio.run(ua_mod.render_leaderboard([], DELEGATE_ID, None, 0))
    assert "Монет пока нет ни у кого 🙂" in out
    assert _default("leaderboard_empty_text") not in out


# ── «📜 История монет»: заголовок ────────────────────────────────────────────────────────

def test_balance_history_header_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 3, reason="за задание", source="task"))

    text, _kb = asyncio.run(ua_mod._balance_history_screen(DELEGATE_ID))
    assert text.startswith(_default("balance_history_header_text"))


def test_balance_history_header_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.set_setting("balance_history_header_text", "📖 <b>Что происходило</b>"))

    text, _kb = asyncio.run(ua_mod._balance_history_screen(DELEGATE_ID))
    assert text.startswith("📖 <b>Что происходило</b>")


# ── Рефералка: текст ссылки, заголовок списка, пустой список ─────────────────────────────

def _referral_link(uid=DELEGATE_ID):
    return f"https://t.me/{FakeBotUser.username}?start={uid}"


def test_referral_link_prompt_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()

    message = FakeMessage()
    asyncio.run(ua_mod.my_referral_link(message, FakeBot()))
    assert message.answers_sent == [
        _default("referral_link_prompt_text").format(link=_referral_link())
    ]


def test_referral_link_prompt_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.set_setting("referral_link_prompt_text", "Зови друзей: {link}"))

    message = FakeMessage()
    asyncio.run(ua_mod.my_referral_link(message, FakeBot()))
    assert message.answers_sent == [f"Зови друзей: {_referral_link()}"]


def test_referral_list_empty_uses_registry_default_then_setting(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()

    message = FakeMessage()
    asyncio.run(ua_mod.my_referrals(message, FakeBot()))
    assert message.answers_sent == [
        _default("referral_list_empty_text").format(link=_referral_link())
    ]

    asyncio.run(db.set_setting("referral_list_empty_text", "Пока пусто. Твоя ссылка: {link}"))
    message = FakeMessage()
    asyncio.run(ua_mod.my_referrals(message, FakeBot()))
    assert message.answers_sent == [f"Пока пусто. Твоя ссылка: {_referral_link()}"]


def test_referral_list_header_uses_registry_default_then_setting(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    invited_id = DELEGATE_ID + 1
    _seed_delegate(invited_id, referrer_id=DELEGATE_ID)

    message = FakeMessage()
    asyncio.run(ua_mod.my_referrals(message, FakeBot()))
    sent = message.answers_sent[0]
    assert sent.startswith(_default("referral_list_header_text").format(count=1))
    assert f"• Delegate {invited_id}" in sent  # список имён по-прежнему строит бот

    asyncio.run(db.set_setting("referral_list_header_text", "Ты позвал {count} чел.:"))
    message = FakeMessage()
    asyncio.run(ua_mod.my_referrals(message, FakeBot()))
    assert message.answers_sent[0].startswith("Ты позвал 1 чел.:")


# ── RU-подписи типов подтверждения (game_labels.proof_types_label) ───────────────────────

def test_proof_type_labels_use_registry_defaults(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(game_labels.proof_types_label("photo")) == _default(
        "game_proof_type_label_photo")
    assert asyncio.run(game_labels.proof_types_label("pdf")) == _default(
        "game_proof_type_label_pdf")
    assert asyncio.run(game_labels.proof_types_label("text")) == _default(
        "game_proof_type_label_text")
    assert asyncio.run(game_labels.proof_types_label("link")) == _default(
        "game_proof_type_label_link")
    assert asyncio.run(game_labels.proof_types_label("")) == _default(
        "game_proof_type_unspecified_text")


def test_proof_type_label_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_proof_type_label_photo", "📸 Фотка"))
    assert asyncio.run(game_labels.proof_types_label("photo")) == "📸 Фотка"


def test_proof_type_unspecified_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_proof_type_unspecified_text", "что угодно"))
    assert asyncio.run(game_labels.proof_types_label(None)) == "что угодно"


def test_proof_types_multi_keeps_canonical_order_with_overrides(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_proof_type_label_photo", "Фото"))
    asyncio.run(db.set_setting("game_proof_type_label_text", "Текст"))
    # Порядок — GAME_PROOF_TYPES, не порядок ввода (контракт 16-01 не ослаблен).
    assert asyncio.run(game_labels.proof_types_label("text,photo")) == "Фото + Текст"


# ── Карточка задания: подсказка «срок вышел» ─────────────────────────────────────────────

def test_task_card_overdue_hint_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Просрочено", deadline_at="2020-01-01 00:00:00")
    task = asyncio.run(db.get_task(task_id))

    text = asyncio.run(ua_mod._render_task_card_text(task, "новое", None))
    assert _default("game_task_overdue_hint_text") in text


def test_task_card_overdue_hint_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Просрочено", deadline_at="2020-01-01 00:00:00")
    task = asyncio.run(db.get_task(task_id))
    asyncio.run(db.set_setting("game_task_overdue_hint_text", "Дедлайн прошёл, но сдать можно"))

    text = asyncio.run(ua_mod._render_task_card_text(task, "новое", None))
    assert "Дедлайн прошёл, но сдать можно" in text
    assert _default("game_task_overdue_hint_text") not in text


def test_task_card_not_overdue_has_no_hint(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Ещё в сроке")
    task = asyncio.run(db.get_task(task_id))

    text = asyncio.run(ua_mod._render_task_card_text(task, "новое", None))
    assert _default("game_task_overdue_hint_text") not in text


# ═══════════════════════════════════════════════════════════════════════════════════════════
# 17.1-02: recall/возвращение (handlers/registration.py) + платёжный поток (handlers/payment.py)
# ═══════════════════════════════════════════════════════════════════════════════════════════

RETURNING_ID = 941201


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _KBCapturingMessage:
    """(text, reply_markup, parse_mode) от answer/answer_photo — как в
    tests/test_returning_delegate_073.py (cmd_start шлёт баннер через _send_welcome)."""

    def __init__(self, uid, username=None):
        self.from_user = FakeUser(uid)
        self.from_user.username = username
        self.chat = type("Chat", (), {"id": uid})()
        self.sent = []
        self.bot = None

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))

    async def answer_photo(self, *a, caption=None, reply_markup=None, parse_mode=None, **k):
        self.sent.append((caption, reply_markup, parse_mode))

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.from_user.id, self.from_user.username)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new

    def texts(self):
        return [t for (t, _, _) in self.sent]


class _SendCapturingBot:
    id = 1

    def __init__(self):
        self.sent = []  # (chat_id, text)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text))


class _FakeState:
    def __init__(self):
        self.state = None
        self.cleared = False

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.cleared = True


# ── _ask_step_or_recall: развилка «прошлое резюме» ───────────────────────────────────────

def _recall_resume(msg_uid=RETURNING_ID):
    msg = _KBCapturingMessage(msg_uid)
    state = _new_state(msg_uid)
    asyncio.run(state.update_data(participant_type="full",
                                  _prior_answers={"resume_url": "https://cloud/x.pdf"}))
    asyncio.run(reg_mod._ask_step_or_recall("resume", msg, state, 4, 9))
    return msg, state


def test_recall_resume_prompt_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    msg, state = _recall_resume()
    assert msg.texts() == [_default("recall_resume_prompt_text")]
    assert asyncio.run(state.get_state()) == Registration.recall_pending.state


def test_recall_resume_prompt_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("recall_resume_prompt_text", "Резюме уже у нас. Оставим?"))
    msg, _state = _recall_resume()
    assert msg.texts() == ["Резюме уже у нас. Оставим?"]


def test_recall_resume_prompt_keeps_progress_prefix(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reg_show_progress", "on"))
    msg, _state = _recall_resume()
    assert msg.texts() == [f"(4/9) {_default('recall_resume_prompt_text')}"]


# ── _ask_step_or_recall: общий экран «Прошлый ответ» ────────────────────────────────────

def _recall_generic(value="МГУ", msg_uid=RETURNING_ID):
    msg = _KBCapturingMessage(msg_uid)
    state = _new_state(msg_uid)
    asyncio.run(state.update_data(participant_type="full", _prior_answers={"university": value}))
    asyncio.run(reg_mod._ask_step_or_recall("university", msg, state, 3, 9))
    return msg, state


def test_recall_generic_prompt_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    msg, state = _recall_generic()
    label = reg_mod.dropout_step_label("university")
    expected = (_default("recall_generic_prompt_text")
                .replace("{label}", label).replace("{display}", "МГУ"))
    assert msg.texts() == [expected]
    assert asyncio.run(state.get_state()) == Registration.recall_pending.state


def test_recall_generic_prompt_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting(
        "recall_generic_prompt_text", "{label} — раньше ты отвечал(а): {display}. Оставим?"))
    msg, _state = _recall_generic()
    label = reg_mod.dropout_step_label("university")
    assert msg.texts() == [f"{label} — раньше ты отвечал(а): МГУ. Оставим?"]


def test_recall_generic_prompt_still_escapes_prior_value(tmp_path):
    # T-073-04-02 не ослаблен: значение из строки users экранируется до подстановки.
    _db_ready(tmp_path)
    msg, _state = _recall_generic(value="<b>x</b>")
    assert "<b>x</b>" not in msg.texts()[0]
    assert "&lt;b&gt;x&lt;/b&gt;" in msg.texts()[0]


def test_recall_generic_prompt_tolerates_stray_braces(tmp_path):
    # .replace, не .format: посторонние {} в тексте менеджера не роняют экран.
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("recall_generic_prompt_text", "{label}: {display} {не плейсхолдер}"))
    msg, _state = _recall_generic()
    assert msg.texts()[0].endswith("МГУ {не плейсхолдер}")


# ── cmd_start: CTA «Хочешь участвовать снова?» под баннером прошлого сезона ─────────────

def _returning_start(uid=RETURNING_ID):
    asyncio.run(db.set_setting("event_season", "YL'26"))
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": "Прошлый Делегат", "username": "old_delegate",
        "registration_date": "2025-08-01 10:00:00", "season": "YL'25",
    }))
    asyncio.run(db.set_user_status(uid, "approved"))
    msg = _KBCapturingMessage(uid, "old_delegate")
    asyncio.run(reg_mod.cmd_start(msg, _new_state(uid), bot=object(), command=None))
    return msg


def _cta_message(msg):
    hits = [(t, rm) for (t, rm, _) in msg.sent
            if rm is not None and hasattr(rm, "inline_keyboard")
            and any(b.callback_data == "rereg_start" for row in rm.inline_keyboard for b in row)]
    assert len(hits) == 1
    return hits[0][0]


def test_start_returning_cta_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    msg = _returning_start()
    assert _cta_message(msg) == _default("start_returning_cta_text")


def test_start_returning_cta_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("start_returning_cta_text", "Снова с нами? Жми кнопку 👇"))
    msg = _returning_start()
    assert _cta_message(msg) == "Снова с нами? Жми кнопку 👇"


# ── payment.start_payment_step: заголовок выбора варианта ───────────────────────────────

def _picker(uid=RETURNING_ID):
    asyncio.run(db.set_setting("payment_options", "Стандарт|3000\nВИП|5000"))
    bot = _SendCapturingBot()
    asyncio.run(pay_mod.start_payment_step(bot, uid, "full"))
    assert len(bot.sent) == 1
    return bot.sent[0][1]


def test_payment_picker_header_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    assert _picker() == _default("payment_option_picker_header_text")


def test_payment_picker_header_setting_overrides_default_and_keeps_requisites(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("payment_option_picker_header_text", "Какой билет берём?"))
    asyncio.run(db.set_setting("payment_requisites", "Сбер 1234"))
    assert _picker() == "Какой билет берём?\n\n📋 Реквизиты:\nСбер 1234"


# ── payment._show_payment_details: шаблон экрана оплаты ─────────────────────────────────

def _details(label="Стандарт", price=3000, uid=RETURNING_ID):
    bot = _SendCapturingBot()
    state = _FakeState()
    asyncio.run(pay_mod._show_payment_details(bot, uid, state, label, price))
    assert len(bot.sent) == 1
    assert state.state == Registration.receipt_upload
    return bot.sent[0][1]


def _legacy_details_render(option_label, option_price, requisites, deadline, penalties):
    """Дословный прежний рендер _show_payment_details ("\\n".join(parts) с условными блоками)
    — эталон байт-идентичности для дефолтного шаблона."""
    import html
    parts = [
        "💰 <b>Оплата участия</b>\n",
        f"Вариант: {html.escape(option_label)}",
        f"Сумма: {option_price} ₽\n",
    ]
    block = pay_mod._format_requisites_block(requisites)
    if block:
        parts.append(block + "\n")
    if deadline:
        parts.append(f"📅 Дедлайн: {html.escape(deadline)}\n")
    if penalties and penalties.strip():
        lines = []
        for line in penalties.strip().splitlines():
            if "|" in line:
                date_part, amount = line.split("|", 1)
                lines.append(f"• до {html.escape(date_part.strip())} — остаток {html.escape(amount.strip())} ₽")
        if lines:
            parts.append("⚠️ Штрафы за отмену:\n" + "\n".join(lines) + "\n")
    parts.append("📎 Загрузи чек оплаты (PDF-документ или скриншот).")
    return "\n".join(parts)


@pytest.mark.parametrize("requisites,deadline,penalties", [
    ("Сбер 1234 & Тинькофф", None, None),
    ("Сбер 1234", "15.08.2099 23:59", None),
    ("Сбер 1234", "15.08.2099 23:59", "01.08.2099|3000\n10.08.2099|0"),
    ("Сбер 1234", None, "01.08.2099|3000"),
    ("Сбер 1234", "15.08.2099 23:59", "строка без разделителя"),
])
def test_payment_details_default_template_renders_byte_identical_to_legacy(
        tmp_path, requisites, deadline, penalties):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("payment_requisites", requisites))
    if deadline:
        asyncio.run(db.set_setting("payment_deadline", deadline))
    if penalties:
        asyncio.run(db.set_setting("penalty_schedule", penalties))
    text = _details("Стандарт <VIP>", 3000)
    assert text == _legacy_details_render("Стандарт <VIP>", 3000, requisites, deadline, penalties)


def test_payment_details_template_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("payment_requisites", "Сбер 1234"))
    asyncio.run(db.set_setting(
        "payment_details_template_text",
        "К оплате {amount} ₽ за «{option}».\n{requisites}Жду чек {и не плейсхолдер}!"))
    text = _details("Стандарт", 3000)
    assert text == "К оплате 3000 ₽ за «Стандарт».\n📋 Реквизиты:\nСбер 1234\n\nЖду чек {и не плейсхолдер}!"


def test_payment_details_missing_blocks_render_empty_in_custom_template(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("payment_requisites", "Сбер 1234"))
    asyncio.run(db.set_setting(
        "payment_details_template_text", "[{deadline}][{penalties}]{option}"))
    assert _details("Стандарт", 3000) == "[][]Стандарт"


# ── payment.process_pay_later: «Ок! Оплатишь позже.» + подсказка про меню ───────────────

def _pay_later(uid=RETURNING_ID):
    _seed_delegate(uid)
    cb = FakeCallback("pay_later", user_id=uid)
    asyncio.run(pay_mod.process_pay_later(cb, _FakeState()))
    assert len(cb.message.answers_sent) == 1
    return cb.message.answers_sent[0]


def test_pay_later_uses_registry_defaults(tmp_path):
    _db_ready(tmp_path)
    assert _pay_later() == (
        f"{_default('payment_pay_later_text')}\n\n{_default('payment_pay_later_menu_hint_text')}"
    )


def test_pay_later_settings_override_defaults_and_keep_requisites_between(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("payment_requisites", "Сбер 1234"))
    asyncio.run(db.set_setting("payment_pay_later_text", "Хорошо, без спешки."))
    asyncio.run(db.set_setting("payment_pay_later_menu_hint_text", "Оплата — в меню внизу."))
    assert _pay_later() == "Хорошо, без спешки.\n\n📋 Реквизиты:\nСбер 1234\n\nОплата — в меню внизу."


# ── payment._finalize_receipt: «✅ Чек получен!» ─────────────────────────────────────────

def _finalize(uid=RETURNING_ID):
    _seed_delegate(uid)
    msg = FakeMessage(user_id=uid)
    msg.bot = None  # notify_by_capability fail-soft: ошибка нотификации не ломает ответ делегату
    asyncio.run(pay_mod._finalize_receipt(msg, _FakeState(), "file123"))
    return msg.answers_sent


def test_receipt_received_uses_registry_default(tmp_path):
    _db_ready(tmp_path)
    assert _finalize() == [_default("payment_receipt_received_text")]
    user = asyncio.run(db.get_user(RETURNING_ID))
    assert user["payment_status"] == "receipt_sent"


def test_receipt_received_setting_overrides_default(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("payment_receipt_received_text", "Чек у нас, спасибо! 🙏"))
    assert _finalize() == ["Чек у нас, спасибо! 🙏"]


# ── 17.1-03: empty-state'ы «📅 Программа»/«🗣 Спикеры»/«📞 Контакты» ────────────────────

class _PhotoFailMessage(FakeMessage):
    """answer_photo падает — как у живого бота, когда ни file_id в настройках, ни
    resources/program.jpg на диске нет (именно тогда делегат видит empty-state)."""

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        raise RuntimeError("no photo")


def test_program_empty_state_uses_registry_default_then_setting(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()

    message = _PhotoFailMessage(text="📅 Программа форума")
    asyncio.run(ua_mod.show_program(message))
    assert message.answers_sent == [_default("program_empty_text")]

    asyncio.run(db.set_setting("program_empty_text", "Программу выложим за неделю до форума 📅"))
    message = _PhotoFailMessage(text="📅 Программа форума")
    asyncio.run(ua_mod.show_program(message))
    assert message.answers_sent == ["Программу выложим за неделю до форума 📅"]


def test_program_photo_present_never_shows_empty_state(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.set_setting("program_photo_file_id", "AgACAgIAAxkBAAI"))
    asyncio.run(db.set_setting("program_caption", "Программа дня 1"))

    message = FakeMessage(text="📅 Программа форума")
    asyncio.run(ua_mod.show_program(message))
    assert message.answers_sent == ["Программа дня 1"]


def test_speakers_empty_state_uses_registry_default_then_setting(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()

    message = FakeMessage(text="🗣 Спикеры")
    asyncio.run(ua_mod.show_speakers(message))
    assert message.answers_sent == [_default("speakers_empty_text")]

    asyncio.run(db.set_setting("speakers_empty_text", "Спикеров объявим в канале 🎤"))
    message = FakeMessage(text="🗣 Спикеры")
    asyncio.run(ua_mod.show_speakers(message))
    assert message.answers_sent == ["Спикеров объявим в канале 🎤"]


def test_contacts_empty_state_uses_registry_default_then_setting(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()

    message = FakeMessage(text="📞 Контакты")
    asyncio.run(ua_mod.show_contacts(message))
    assert message.answers_sent == [_default("contacts_empty_text")]

    asyncio.run(db.set_setting("contacts_empty_text", "Пиши в @aiesec_help — ответим 🙌"))
    message = FakeMessage(text="📞 Контакты")
    asyncio.run(ua_mod.show_contacts(message))
    assert message.answers_sent == ["Пиши в @aiesec_help — ответим 🙌"]


def test_contacts_present_never_shows_empty_state(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.set_setting("contact_person", "@org_lead"))

    message = FakeMessage(text="📞 Контакты")
    asyncio.run(ua_mod.show_contacts(message))
    assert message.answers_sent == ["По всем вопросам пиши сюда: @org_lead"]


# ── 17.1-03: «❓ Задать вопрос» — приглашение и подтверждение ────────────────────────────

def test_ask_question_prompt_uses_registry_default_then_setting(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()

    message = FakeMessage(text="❓ Задать вопрос")
    state = _new_state(DELEGATE_ID)
    asyncio.run(ua_mod.ask_organizer_start(message, state))
    assert message.answers_sent == [_default("ask_question_prompt_text")]
    assert asyncio.run(state.get_state()) == "Question:waiting_for_question"

    asyncio.run(db.set_setting("ask_question_prompt_text", "Спрашивай — оргкомитет на связи ✍️"))
    message = FakeMessage(text="❓ Задать вопрос")
    asyncio.run(ua_mod.ask_organizer_start(message, _new_state(DELEGATE_ID)))
    assert message.answers_sent == ["Спрашивай — оргкомитет на связи ✍️"]


def _send_question(monkeypatch, text="Где парковка?"):
    """process_question с подменённым fan-out менеджерам (1 получатель) — проверяем только
    ответ делегату; сам fan-out покрыт tests/test_roles_phase8.py."""
    async def _fake_notify(bot, cap, admin_text, parse_mode=None, city=None):
        return 1

    monkeypatch.setattr(ua_mod, "notify_by_capability", _fake_notify)
    message = FakeMessage(text=text)
    state = _new_state(DELEGATE_ID)
    asyncio.run(state.set_state("Question:waiting_for_question"))
    asyncio.run(ua_mod.process_question(message, state, FakeBot()))
    assert asyncio.run(state.get_state()) is None
    return message.answers_sent


def test_ask_question_sent_uses_registry_default(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _seed_delegate()
    assert _send_question(monkeypatch) == [_default("ask_question_sent_text")]


def test_ask_question_sent_setting_overrides_default(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.set_setting("ask_question_sent_text", "Принято! Ответим в течение дня 💬"))
    assert _send_question(monkeypatch) == ["Принято! Ответим в течение дня 💬"]


# ── 17.1-03: HTML_SETTINGS <=> «Поддерживается HTML» в prompt ─────────────────────────────

def test_html_promise_in_prompt_matches_html_settings():
    """Единая политика 17.1: если prompt обещает менеджеру HTML, ввод из админки должен
    браться из message.html_text (жирный/курсив Telegram сохраняются, «<»/«&» экранируются
    сами) — т.е. ключ обязан быть в HTML_SETTINGS. И наоборот: ключ в HTML_SETTINGS без
    обещания в prompt — менеджер не знает, что разметка поддерживается."""
    promised = {
        key for key, entry in SETTINGS_SCHEMA.items()
        if entry.get("prompt") and "html" in entry["prompt"].lower()
    }
    assert promised == admin_settings.HTML_SETTINGS, (
        f"расхождение: только в prompt {sorted(promised - admin_settings.HTML_SETTINGS)}, "
        f"только в HTML_SETTINGS {sorted(admin_settings.HTML_SETTINGS - promised)}"
    )


# ── 17.1-03 (schema-completeness): city_fork / party_fork / preselect_* ─────────────────

def _start(uid, username=None):
    """cmd_start новичка без deep-link (bot=object(): до отправки медиа/меню дело не доходит
    на развилках и гейте предотбора; та же идиома, что в tests/test_registration_phase5.py)."""
    msg = _RegCapturingMessage(uid, username)
    asyncio.run(reg_mod.cmd_start(msg, _new_state(uid), bot=object(), command=None))
    return msg.texts


def test_preselect_link_and_enabled_are_declared_with_neighbours_semantics():
    link = SETTINGS_SCHEMA["preselect_link"]
    assert link["type"] == "text" and link["group"] == "reg" and link["default"] is None
    enabled = SETTINGS_SCHEMA["preselect_enabled"]
    # enum on/off, НЕ toggle — как event_city_enabled (toggle-тип зарезервирован за reg_q_*).
    assert enabled["type"] == "enum" and enabled["group"] == "toggles"
    assert enabled["options"] == ["on", "off"] and enabled["default"] == "off"


def test_fork_default_constants_point_at_registry():
    assert reg_mod.DEFAULT_CITY_FORK_TEXT == _default("city_fork_text")
    assert reg_mod.DEFAULT_PARTY_FORK_TEXT == _default("party_fork_text")


def test_city_fork_uses_registry_default_then_setting(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    assert _default("city_fork_text") in _start(DELEGATE_ID + 10, "newbie")

    asyncio.run(db.set_setting("city_fork_text", "В каком городе едешь на форум? 🏙"))
    texts = _start(DELEGATE_ID + 11, "newbie2")
    assert "В каком городе едешь на форум? 🏙" in texts
    assert _default("city_fork_text") not in texts


def test_party_fork_uses_registry_default_then_setting(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("party_enabled", "on"))
    asyncio.run(db.set_setting("party_fork_question", "on"))

    assert _default("party_fork_text") in _start(DELEGATE_ID + 20, "newbie")

    asyncio.run(db.set_setting("party_fork_text", "Как участвуешь — весь форум или только вечеринка? 🎉"))
    texts = _start(DELEGATE_ID + 21, "newbie2")
    assert "Как участвуешь — весь форум или только вечеринка? 🎉" in texts
    assert _default("party_fork_text") not in texts


def _preselect_on(monkeypatch):
    from services import allowlist
    monkeypatch.setattr(allowlist, "_allowlist", {"chosen_one"})  # непустой, «нас» там нет
    asyncio.run(db.set_setting("preselect_enabled", "on"))


def test_preselect_fail_uses_registry_default_then_setting_and_link(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _preselect_on(monkeypatch)

    assert _start(DELEGATE_ID + 30, "not_chosen") == [_default("preselect_fail_text")]

    asyncio.run(db.set_setting("preselect_fail_text", "Тебя нет в списке отобранных 😔"))
    asyncio.run(db.set_setting("preselect_link", "https://t.me/aiesec_ru"))
    assert _start(DELEGATE_ID + 31, "not_chosen") == [
        "Тебя нет в списке отобранных 😔\nhttps://t.me/aiesec_ru"
    ]


def test_preselect_no_username_uses_registry_default_then_setting(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _preselect_on(monkeypatch)

    assert _start(DELEGATE_ID + 40, None) == [_default("preselect_no_username_text")]

    asyncio.run(db.set_setting("preselect_no_username_text", "Поставь @username и жми /start ещё раз"))
    assert _start(DELEGATE_ID + 41, None) == ["Поставь @username и жми /start ещё раз"]


def test_preselect_texts_stay_escaped_not_html(tmp_path, monkeypatch):
    """Консьюмер html.escape'ит тексты предотбора (как и до реестра) — поэтому они НЕ в
    HTML_SETTINGS и prompt не обещает HTML."""
    _db_ready(tmp_path)
    _preselect_on(monkeypatch)
    asyncio.run(db.set_setting("preselect_fail_text", "<b>Нет</b> & точка"))
    assert _start(DELEGATE_ID + 50, "not_chosen") == ["&lt;b&gt;Нет&lt;/b&gt; &amp; точка"]
    for key in ("preselect_no_username_text", "preselect_fail_text", "preselect_link"):
        assert key not in admin_settings.HTML_SETTINGS


def test_preselect_enabled_default_off_keeps_gate_closed(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    from services import allowlist
    monkeypatch.setattr(allowlist, "_allowlist", {"chosen_one"})
    # preselect_enabled не задан -> enum-дефолт "off" -> гейт не срабатывает, новичок идёт
    # дальше (получает приветствие, а не «Отбор не пройден.»).
    texts = _start(DELEGATE_ID + 60, "not_chosen")
    assert _default("preselect_fail_text") not in texts


# ── 17.1-03: полнота реестра — каждый литеральный ключ bot_settings в коде объявлен ──────

# Служебные значения bot_settings, которые НАМЕРЕННО не в SETTINGS_SCHEMA (менеджеру их не
# редактировать) — см. комментарий у блока предотбора в settings_schema.py.
_SERVICE_KEYS = {
    "sheet_header_schema",        # снимок заголовков вкладки регистраций (JSON, пишет reg_schema)
    "preselect_manual_ids",       # ручные исключения предотбора по telegram_id
    "start_photo_file_id",        # file_id фото-визарда (префикс "start" типа photo в реестре)
    "game_sheet_last_synced_at",  # метка времени последнего синка матрицы геймы (состояние)
}

# 17.1-03: вне скоупа, см. журнал — читаются в services/scheduler.py и services/reminders.py,
# в реестре не объявлены (менеджеру сейчас недоступны, в справке /settings_guide помечены
# «меняет разработчик»). Заводить в реестр — отдельным решением, не в этом плане.
_OUT_OF_SCOPE_KEYS = {
    "allowlist_refresh_minutes", "incomplete_sync_hours",
    "nudge_enabled", "nudge_after_minutes", "nudge_scan_minutes", "nudge_text",
    "pending_reminder_enabled",
}

_MEDIA_SUFFIXES = ("_photo_file_id", "_doc_file_id", "_caption")


def _is_registry_media_derivative(key: str) -> bool:
    """`{prefix}_photo_file_id` / `{prefix}_doc_file_id` / `{prefix}_caption` — реальные строки
    bot_settings, которые пишет фото/файл-визард; в реестре зарегистрирован сам префикс
    (тип photo/file, D-10), а не производные ключи."""
    for suffix in _MEDIA_SUFFIXES:
        if key.endswith(suffix):
            prefix = key[: -len(suffix)]
            entry = SETTINGS_SCHEMA.get(prefix)
            return bool(entry) and entry["type"] in ("photo", "file")
    return False


def _literal_setting_keys_in_source() -> dict[str, set[str]]:
    """Все литеральные ключи `get_setting("…")` / `get_setting_typed("…")` /
    `get_setting_for_city("…")` / `get_setting_typed_for_city("…")` в handlers/*.py и
    services/*.py. Динамические/составные ключи (переменная, f-string) регуляркой не
    матчатся — у них после скобки нет кавычки — и намеренно не проверяются."""
    import glob
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pattern = re.compile(
        r"""\bget_setting(?:_typed)?(?:_for_city)?\(\s*(["'])([A-Za-z0-9_]+)\1"""
    )
    found: dict[str, set[str]] = {}
    files = glob.glob(os.path.join(root, "handlers", "*.py")) + glob.glob(
        os.path.join(root, "services", "*.py")
    )
    assert files, "handlers/*.py и services/*.py не найдены — тест смотрит не туда"
    for path in files:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for m in pattern.finditer(src):
            found.setdefault(m.group(2), set()).add(os.path.relpath(path, root))
    return found


def test_every_literal_setting_key_in_code_is_declared_or_allowlisted():
    found = _literal_setting_keys_in_source()
    assert len(found) >= 100, f"нашли всего {len(found)} ключей — регулярка сломалась?"
    undeclared = {
        key: sorted(files) for key, files in found.items()
        if key not in SETTINGS_SCHEMA
        and key not in _SERVICE_KEYS
        and key not in _OUT_OF_SCOPE_KEYS
        and not _is_registry_media_derivative(key)
    }
    assert not undeclared, (
        "ключи bot_settings читаются в коде, но не объявлены в SETTINGS_SCHEMA "
        f"(менеджер их не увидит): {undeclared}"
    )
    # Ключи 17.1-03 действительно читаются кодом (иначе объявление в реестре — мёртвый груз).
    for key in ("city_fork_text", "party_fork_text", "preselect_enabled",
                "preselect_no_username_text", "preselect_fail_text", "preselect_link"):
        assert key in found, f"{key} больше нигде не читается"


def test_allowlists_do_not_hide_keys_that_are_now_declared():
    """Allow-list'ы — не мусорка: если ключ доехал до реестра, его надо оттуда убрать."""
    stale = (_SERVICE_KEYS | _OUT_OF_SCOPE_KEYS) & set(SETTINGS_SCHEMA)
    assert not stale, f"уже в реестре, убрать из allow-list: {sorted(stale)}"
