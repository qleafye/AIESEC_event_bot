"""Phase 17.1 (17.1-01) — делегатские тексты гейма/баланса/рейтинга/рефералки переехали из
литералов в SETTINGS_SCHEMA. Два вида проверок на каждую поверхность:

1) «дефолт байт-в-байт» — `SETTINGS_SCHEMA[key]["default"]` равен ровно тому литералу, который
   стоял в коде до миграции (эмодзи, переносы, HTML — как было; изменений поведения нет);
2) «ключ переопределяет дефолт» — записали значение в bot_settings, экран показал его.

Хендлеры зовутся напрямую с Fake message/callback, как в
tests/test_game_ui16_delegate_260820.py (pytest-asyncio в этом окружении нет).
"""
import asyncio

import pytest

from config import config
from database import db
# handlers.admin импортируется ПЕРВЫМ намеренно: admin_settings в одиночку не импортируется
# (цикл admin <-> admin_settings, та же идиома, что в tests/test_settings_groups_c0x.py).
from handlers import admin as _admin_mod  # noqa: F401
from handlers import admin_settings
from handlers import game_labels
from handlers import user_actions as ua_mod
from settings_schema import SETTINGS_SCHEMA


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
