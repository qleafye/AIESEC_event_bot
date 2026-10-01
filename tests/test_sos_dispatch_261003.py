"""Ответ орга на карточку SOS и кнопки «🙋 Беру»/«✅ Решено» — через НАСТОЯЩИЙ `Dispatcher`.

`tests/test_sos_260924.py` зовёт хендлеры напрямую, мимо роутеров и `CapabilityMiddleware`, —
поэтому был зелёным, пока в проде реплай на карточку не доходил до делегата нигде: в чате SOS
его съедал catch-all группового роутера (подключён раньше `admin.router`), в личке —
deny-by-default middleware (форма «🆘»-реплая ей была неизвестна). Здесь — порядок роутеров
`main.py` (group_chat.router -> group_chat.private_router -> admin.router), апдейты через
`feed_update`, исходящие вызовы Bot API перехватываются на `Bot.__call__`.

Роутеры — модульные синглтоны, и `include_router` второй раз в процессе бросает
«Router is already attached» (их уже кэшируют `tests/test_refac_snapshot_260816.py` и
`tests/test_chat_binding_260914.py`). `_attached` отцепляет их на время теста и возвращает
прежнего родителя после — чужие кэшированные диспетчеры не ломаются при общем воркере xdist.
"""
import asyncio
import time
from contextlib import contextmanager
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from config import config
from database import db
from handlers import admin, group_chat
from services import sos as sos_service
from tests._dbtpl import fast_init_db

BOT_ID = 123456
TOKEN = f"{BOT_ID}:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ADMIN_ID = 903001
MANAGER_ID = 903002
STRANGER_ID = 903003
DELEGATE_ID = 903010
SOS_CHAT_ID = -1009031001
OTHER_CHAT_ID = -1009031002


class RecordingBot(Bot):
    """Bot API не дёргается: каждый метод записывается, SendMessage возвращает правдоподобное
    `Message` (из него хендлеры берут message_id), остальное — True."""

    def __init__(self):
        super().__init__(token=TOKEN)
        self.calls: list = []
        self._next_mid = 7000

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            self._next_mid += 1
            return Message(
                message_id=self._next_mid, date=datetime.now(),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=User(id=BOT_ID, is_bot=True, first_name="bot"), text=method.text,
            )
        return True

    def sent_to(self, chat_id: int) -> list[str]:
        return [m.text for m in self.calls if isinstance(m, SendMessage) and int(m.chat_id) == chat_id]

    def alerts(self) -> list[str]:
        return [m.text for m in self.calls if isinstance(m, AnswerCallbackQuery)]


@contextmanager
def _attached():
    routers = (group_chat.router, group_chat.private_router, admin.router)
    saved = [(r, r._parent_router) for r in routers]
    for r in routers:
        r._parent_router = None
    dp = Dispatcher(storage=MemoryStorage())
    for r in routers:
        dp.include_router(r)
    try:
        yield dp
    finally:
        for r, parent in saved:
            r._parent_router = parent


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "sos_dispatch.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _seed_report(*, city=None, bind_chat=True) -> dict:
    await db.add_user({
        "telegram_id": DELEGATE_ID, "full_name": "Тест Делегатов", "username": "testdel",
        "university": "ВШЭ", "phone": "+79990000000", "event_city": city,
        "registration_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": "approved",
    })
    if bind_chat:
        await sos_service.bind_sos_chat(ADMIN_ID, SOS_CHAT_ID, "Чат SOS", city)
    rid = await db.create_sos_report(DELEGATE_ID, city)
    if bind_chat:
        await db.set_sos_card(rid, SOS_CHAT_ID, 500)
    return await db.get_sos_report(rid)


def _card(chat: Chat, rid: int, mid: int = 500) -> Message:
    # Текст реплая у Telegram плоский (без <b>/<code>), как в tests/test_sos_260924.py.
    return Message(
        message_id=mid, date=int(time.time()), chat=chat,
        from_user=User(id=BOT_ID, is_bot=True, first_name="bot"),
        text=f"🆘 SOS #{rid}\n🆔 {DELEGATE_ID} Тест Делегатов\n🆘 СРОЧНО — подробности ещё не прислали",
    )


def _reply_update(uid: int, chat: Chat, card: Message, text: str, update_id: int = 1) -> Update:
    msg = Message(
        message_id=card.message_id + 1, date=int(time.time()), chat=chat,
        from_user=User(id=uid, is_bot=False, first_name="Орг"), text=text, reply_to_message=card,
    )
    return Update(update_id=update_id, message=msg)


def _button_update(uid: int, card: Message, data: str, update_id: int = 2) -> Update:
    cb = CallbackQuery(
        id=str(update_id), from_user=User(id=uid, is_bot=False, first_name="Дежурный"),
        chat_instance="x", data=data, message=card,
    )
    return Update(update_id=update_id, callback_query=cb)


def _feed(dp, bot, *updates):
    async def go():
        try:
            for u in updates:
                await dp.feed_update(bot, u)
        finally:
            await bot.session.close()
    asyncio.run(go())


GROUP = Chat(id=SOS_CHAT_ID, type="supergroup", title="Чат SOS")
OTHER_GROUP = Chat(id=OTHER_CHAT_ID, type="supergroup", title="Случайный чат")


# ── Чат SOS ──────────────────────────────────────────────────────────────────────────────────

def test_group_reply_from_moderator_without_claim_is_not_delivered(tmp_path):
    """Реплаем на невзятую карточку команда переговаривается («кто ближе?») — это не должно
    долететь делегату и молча отдать заявку спросившему, даже если он держит «📋 Модерация»."""
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, GROUP, _card(GROUP, report["id"]), "кто ближе?"))
    assert bot.sent_to(DELEGATE_ID) == []
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] is None
    hint = bot.sent_to(SOS_CHAT_ID)
    assert hint and "не отправлен" in hint[0] and "🙋 Беру" in hint[0]


def test_group_reply_from_moderator_after_claim_reaches_delegate(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    card = _card(GROUP, report["id"])
    bot = RecordingBot()
    with _attached() as dp:
        _feed(
            dp, bot,
            _button_update(ADMIN_ID, card, f"sos_claim:{report['id']}", update_id=1),
            _reply_update(ADMIN_ID, GROUP, card, "Иду, где ты?", update_id=2),
        )
    delivered = bot.sent_to(DELEGATE_ID)
    assert delivered and "Иду, где ты?" in delivered[0]
    assert any("Ответ отправлен" in t for t in bot.sent_to(SOS_CHAT_ID))


def test_group_reply_on_card_claimed_by_colleague_is_not_delivered(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    card = _card(GROUP, report["id"])
    bot = RecordingBot()
    with _attached() as dp:
        _feed(
            dp, bot,
            _button_update(STRANGER_ID, card, f"sos_claim:{report['id']}", update_id=1),
            _reply_update(ADMIN_ID, GROUP, card, "звоню в скорую", update_id=2),
        )
    assert bot.sent_to(DELEGATE_ID) == []
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] == STRANGER_ID
    assert any("ведёт" in t for t in bot.sent_to(SOS_CHAT_ID))


def test_group_reply_from_member_without_rights_is_not_delivered(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(STRANGER_ID, GROUP, _card(GROUP, report["id"]), "привет"))
    assert bot.sent_to(DELEGATE_ID) == []
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] is None
    hint = bot.sent_to(SOS_CHAT_ID)
    assert hint and "🙋 Беру" in hint[0]  # объясняет, что сделать, а не молчит


def test_group_reply_on_card_forwarded_to_other_chat_is_ignored(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, OTHER_GROUP, _card(OTHER_GROUP, report["id"]), "Иду"))
    assert bot.sent_to(DELEGATE_ID) == []
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] is None


def test_group_reply_to_non_bot_message_with_markers_is_ignored(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    fake_card = _card(GROUP, report["id"]).model_copy(
        update={"from_user": User(id=STRANGER_ID, is_bot=False, first_name="Шутник")},
    )
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, GROUP, fake_card, "Иду"))
    assert bot.sent_to(DELEGATE_ID) == []


# ── «🙋 Беру» / «✅ Решено» в чате SOS ───────────────────────────────────────────────────────

def test_claim_button_works_for_sos_chat_member_without_rights(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _button_update(STRANGER_ID, _card(GROUP, report["id"]), f"sos_claim:{report['id']}"))
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] == STRANGER_ID
    assert "Взято." in bot.alerts()


def test_member_who_claimed_can_then_reply(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    card = _card(GROUP, report["id"])
    bot = RecordingBot()
    with _attached() as dp:
        _feed(
            dp, bot,
            _button_update(STRANGER_ID, card, f"sos_claim:{report['id']}", update_id=1),
            _reply_update(STRANGER_ID, GROUP, card, "Бегу к тебе", update_id=2),
        )
    delivered = bot.sent_to(DELEGATE_ID)
    assert delivered and "Бегу к тебе" in delivered[0]


def test_resolve_button_works_for_sos_chat_member_without_rights(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _button_update(STRANGER_ID, _card(GROUP, report["id"]), f"sos_resolve:{report['id']}"))
    assert asyncio.run(db.get_sos_report(report["id"]))["resolved_by"] == STRANGER_ID


def test_claim_button_in_foreign_chat_is_refused_with_explanation(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _button_update(STRANGER_ID, _card(OTHER_GROUP, report["id"]), f"sos_claim:{report['id']}"))
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] is None
    assert any(a and "не из чата SOS" in a for a in bot.alerts())


# ── Личная копия карточки (фоллбэк без чата SOS) ────────────────────────────────────────────

def test_dm_reply_from_superadmin_reaches_delegate(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report(bind_chat=False))
    dm = Chat(id=ADMIN_ID, type="private")
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, dm, _card(dm, report["id"]), "Иду к тебе"))
    delivered = bot.sent_to(DELEGATE_ID)
    assert delivered and "Иду к тебе" in delivered[0]
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] == ADMIN_ID


def test_dm_reply_from_user_without_rights_is_not_delivered(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report(bind_chat=False))
    dm = Chat(id=STRANGER_ID, type="private")
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(STRANGER_ID, dm, _card(dm, report["id"]), "Иду"))
    assert bot.sent_to(DELEGATE_ID) == []
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] is None


def test_dm_reply_from_moderator_of_other_city_is_refused(tmp_path):
    _ready(tmp_path)

    async def seed():
        await db.set_setting("event_city_enabled", "on")
        await db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(MANAGER_ID, "msk")
        # В Тюмени свой держатель — иначе фолбэк «в городе никого» отдал бы SOS всем.
        await db.add_staff(STRANGER_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(STRANGER_ID, "tyumen")
        return await _seed_report(city="tyumen", bind_chat=False)

    report = asyncio.run(seed())
    dm = Chat(id=MANAGER_ID, type="private")
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(MANAGER_ID, dm, _card(dm, report["id"]), "Иду"))
    assert bot.sent_to(DELEGATE_ID) == []
    refusal = bot.sent_to(MANAGER_ID)
    assert refusal and "команда этого города" in refusal[0]


# ── Реплай на дописку делегата в треде карточки ──────────────────────────────────────────────

class CopyingBot(RecordingBot):
    """Плюс CopyMessage: бот копирует дописку делегата в тред и получает id копии."""

    async def __call__(self, method, request_timeout=None):
        from aiogram.methods import CopyMessage
        from aiogram.types import MessageId

        if isinstance(method, CopyMessage):
            self.calls.append(method)
            self._next_mid += 1
            return MessageId(message_id=self._next_mid)
        return await super().__call__(method, request_timeout)


def _relay_followup(bot, report_id: int, text: str) -> int:
    """Дописка делегата уходит в тред (как из режима «дописываю SOS»); возвращает id копии."""
    dm = Chat(id=DELEGATE_ID, type="private")
    msg = Message(
        message_id=42, date=int(time.time()), chat=dm,
        from_user=User(id=DELEGATE_ID, is_bot=False, first_name="Тест"), text=text,
    ).as_(bot)
    asyncio.run(sos_service.relay_delegate_message(msg, report_id))
    return bot._next_mid


def _bot_copy(chat: Chat, mid: int, text: str) -> Message:
    return Message(
        message_id=mid, date=int(time.time()), chat=chat,
        from_user=User(id=BOT_ID, is_bot=True, first_name="bot"), text=text,
    )


def test_group_reply_to_delegate_followup_reaches_delegate(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    card = _card(GROUP, report["id"])
    bot = CopyingBot()
    copy_mid = _relay_followup(bot, report["id"], "мне плохо, 2 этаж")
    with _attached() as dp:
        _feed(
            dp, bot,
            _button_update(STRANGER_ID, card, f"sos_claim:{report['id']}", update_id=1),
            _reply_update(STRANGER_ID, GROUP, _bot_copy(GROUP, copy_mid, "мне плохо, 2 этаж"),
                          "Иду, жди у лифта", update_id=2),
        )
    delivered = bot.sent_to(DELEGATE_ID)
    assert delivered and "Иду, жди у лифта" in delivered[0]


def test_group_reply_to_delegate_followup_without_claim_explains(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = CopyingBot()
    copy_mid = _relay_followup(bot, report["id"], "мне плохо")
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, GROUP, _bot_copy(GROUP, copy_mid, "мне плохо"), "кто ближе?"))
    assert bot.sent_to(DELEGATE_ID) == []
    assert any("🙋 Беру" in t for t in bot.sent_to(SOS_CHAT_ID))


def test_group_reply_to_unrelated_bot_message_is_ignored(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = CopyingBot()
    _relay_followup(bot, report["id"], "мне плохо")
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, GROUP, _bot_copy(GROUP, 99999, "Всем привет"), "ок"))
    assert bot.sent_to(DELEGATE_ID) == []
    assert bot.sent_to(SOS_CHAT_ID) == []


# ── Реплай на уже решённую карточку ─────────────────────────────────────────────────────────

def test_group_reply_after_resolve_by_who_led_it_is_delivered_and_marked(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    card = _card(GROUP, report["id"])
    bot = RecordingBot()
    with _attached() as dp:
        _feed(
            dp, bot,
            _button_update(STRANGER_ID, card, f"sos_claim:{report['id']}", update_id=1),
            _button_update(STRANGER_ID, card, f"sos_resolve:{report['id']}", update_id=2),
            _reply_update(STRANGER_ID, GROUP, card, "и забери бейдж на стойке Б", update_id=3),
        )
    delivered = [t for t in bot.sent_to(DELEGATE_ID) if "стойке Б" in t]
    assert delivered and "SOS #" in delivered[0]
    row = asyncio.run(db.get_sos_report(report["id"]))
    assert row["post_resolve_reply_at"] and row["post_resolve_reply_by_name"]
    assert any("после решения" in t for t in bot.sent_to(SOS_CHAT_ID))
    text = sos_service.render_card_text(row, None)
    assert "💬 Ответ после решения:" in text


def test_group_reply_after_resolve_by_someone_else_is_not_delivered(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    card = _card(GROUP, report["id"])
    bot = RecordingBot()
    with _attached() as dp:
        _feed(
            dp, bot,
            _button_update(STRANGER_ID, card, f"sos_resolve:{report['id']}", update_id=1),
            _reply_update(ADMIN_ID, GROUP, card, "молодцы!", update_id=2),
        )
    assert not any("молодцы" in t for t in bot.sent_to(DELEGATE_ID))
    assert asyncio.run(db.get_sos_report(report["id"]))["post_resolve_reply_at"] is None
    assert any("уже решён" in t for t in bot.sent_to(SOS_CHAT_ID))


def test_dm_reply_after_resolve_is_delivered(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report(bind_chat=False))
    asyncio.run(db.resolve_sos_report(report["id"], MANAGER_ID, "Коллега"))
    dm = Chat(id=ADMIN_ID, type="private")
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, dm, _card(dm, report["id"]), "забери бейдж"))
    assert any("забери бейдж" in t for t in bot.sent_to(DELEGATE_ID))
    assert asyncio.run(db.get_sos_report(report["id"]))["post_resolve_reply_by_name"]


# ── Личка: реплай на копию дописки делегата (веер без чата SOS) ─────────────────────────────

def _dm_followup_setup(tmp_path, text: str):
    _ready(tmp_path)
    report = asyncio.run(_seed_report(bind_chat=False))
    bot = CopyingBot()

    async def post():
        await sos_service.post_card(bot, report["id"])

    asyncio.run(post())
    assert ADMIN_ID in dict(asyncio.run(db.list_sos_card_copies(report["id"])))
    return report, _relay_followup(bot, report["id"], text)


def test_dm_reply_to_relayed_followup_reaches_delegate(tmp_path):
    """Без чата SOS дописка делегата приходит админу реплаем на копию карточки — выглядит как
    тред. Реплай админа на неё раньше не доходил ни до кого, и бот молчал."""
    report, copy_mid = _dm_followup_setup(tmp_path, "мне плохо, 2 этаж")
    dm = Chat(id=ADMIN_ID, type="private")
    bot = CopyingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, dm, _bot_copy(dm, copy_mid, "мне плохо, 2 этаж"), "Иду"))
    delivered = bot.sent_to(DELEGATE_ID)
    assert delivered and "Иду" in delivered[0]
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] == ADMIN_ID


def test_dm_reply_to_relayed_followup_from_stranger_is_ignored(tmp_path):
    """Запись копии привязана к личке того, кому она ушла: чужой с тем же message_id в своей
    личке заявку не находит."""
    _report, copy_mid = _dm_followup_setup(tmp_path, "мне плохо")
    dm = Chat(id=STRANGER_ID, type="private")
    bot = CopyingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(STRANGER_ID, dm, _bot_copy(dm, copy_mid, "мне плохо"), "Иду"))
    assert bot.sent_to(DELEGATE_ID) == []


def test_group_reply_to_followup_with_card_markers_stays_in_its_report(tmp_path):
    """Дописка делегата с «🆔», «🆘» и «SOS #N» в тексте: реплай на её копию идёт в ЕГО заявку,
    а не в заявку #N из текста."""
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    other_rid = asyncio.run(db.create_sos_report(DELEGATE_ID + 1, None))
    asyncio.run(db.set_sos_card(other_rid, SOS_CHAT_ID, 600))
    card = _card(GROUP, report["id"])
    bot = CopyingBot()
    tricky = f"🆘 как в SOS #{other_rid}, 🆔 не помню"
    copy_mid = _relay_followup(bot, report["id"], tricky)
    with _attached() as dp:
        _feed(
            dp, bot,
            _button_update(STRANGER_ID, card, f"sos_claim:{report['id']}", update_id=1),
            _reply_update(STRANGER_ID, GROUP, _bot_copy(GROUP, copy_mid, tricky), "Иду", update_id=2),
        )
    assert any("Иду" in t for t in bot.sent_to(DELEGATE_ID))
    assert asyncio.run(db.get_sos_report(other_rid))["claimed_by"] is None


def test_group_reply_after_resolve_refusal_names_claimer_and_resolver(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    asyncio.run(db.claim_sos_report(report["id"], STRANGER_ID, "Вера Взявшая"))
    asyncio.run(db.resolve_sos_report(report["id"], MANAGER_ID, "Рома Закрывший"))
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, GROUP, _card(GROUP, report["id"]), "молодцы!"))
    refusal = [t for t in bot.sent_to(SOS_CHAT_ID) if "уже решён" in t]
    assert refusal and "Вера Взявшая" in refusal[0] and "Рома Закрывший" in refusal[0]
