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

def test_group_reply_from_moderator_reaches_delegate(tmp_path):
    _ready(tmp_path)
    report = asyncio.run(_seed_report())
    bot = RecordingBot()
    with _attached() as dp:
        _feed(dp, bot, _reply_update(ADMIN_ID, GROUP, _card(GROUP, report["id"]), "Иду, где ты?"))
    delivered = bot.sent_to(DELEGATE_ID)
    assert delivered and "Иду, где ты?" in delivered[0]
    assert asyncio.run(db.get_sos_report(report["id"]))["claimed_by"] == ADMIN_ID
    assert any("Ответ отправлен" in t for t in bot.sent_to(SOS_CHAT_ID))


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
