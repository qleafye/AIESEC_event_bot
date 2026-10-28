"""Стенд «день форума» для сквозных тестов: временная база, замороженные часы МСК, бот без сети,
настоящий `Dispatcher` с роутерами в порядке `main.py`, настоящий планировщик (в памяти,
на паузе — джобы не срабатывают сами, тест зовёт их по своему расписанию), Mini App через
`TestClient` с настоящей подписью initData.

Часы. Весь проект берёт «сейчас» из `services.infra.timeutil.msk_now` (и `miniapp.timeutil`);
функции импортированы по имени во многих модулях, поэтому подменяется не функция, а `datetime`
в глобалах этих двух модулей — подмена видна всем, кто зовёт `msk_now()`.

Роутеры — модульные синглтоны: `World.attached()` отцепляет их на время сценария и возвращает
прежнего родителя (тот же приём, что `tests/test_sos_dispatch_261003.py`)."""
from __future__ import annotations

import asyncio
import io
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendDocument, SendMessage, SendPhoto,
)
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, Update, User

from config import config
from database import db
from services.infra import timeutil
from tests._dbtpl import fast_init_db

BOT_ID = 123456
TOKEN = "123456:ABCDEF-testtoken"  # тот же, что подписывает initData (tests/test_miniapp_auth.py)
ADMIN_ID = 904001
MSK = "msk"
MSK_TZ = timeutil.MOSCOW_TZ


class FrozenClock:
    """`datetime` с управляемым «сейчас» (МСК). `now(tz)` отдаёт то же мгновение в нужной зоне."""

    moment: datetime = datetime(2026, 10, 28, 12, 0, tzinfo=MSK_TZ)

    @classmethod
    def set_msk(cls, *args) -> None:
        cls.moment = datetime(*args, tzinfo=MSK_TZ)

    @classmethod
    def make_class(cls):
        clock = cls

        class _DT(datetime):
            @classmethod
            def now(klass, tz=None):  # noqa: N805
                m = clock.moment
                return m.astimezone(tz).replace(tzinfo=tz) if tz else m.astimezone(MSK_TZ).replace(tzinfo=None)

        return _DT


class RecordingBot(Bot):
    """Bot API не дёргается: методы записываются, отправки возвращают правдоподобный `Message`."""

    def __init__(self):
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        super().__init__(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.calls: list = []
        self._mid = 10_000
        self.files: dict[str, bytes] = {}

    def _msg(self, chat_id, **kw) -> Message:
        self._mid += 1
        return Message(
            message_id=self._mid, date=datetime.now(),
            chat=Chat(id=int(chat_id), type="private" if int(chat_id) > 0 else "supergroup"),
            from_user=User(id=BOT_ID, is_bot=True, first_name="bot"), **kw,
        )

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return self._msg(method.chat_id, text=method.text)
        if isinstance(method, SendPhoto):
            return self._msg(method.chat_id, caption=method.caption,
                             photo=[PhotoSize(file_id="ph", file_unique_id="ph", width=1, height=1)])
        if isinstance(method, SendDocument):
            return self._msg(method.chat_id, caption=method.caption,
                             document=Document(file_id="doc", file_unique_id="doc"))
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return True
        if type(method).__name__ == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="bot", username="yl_test_bot")
        if type(method).__name__ == "GetChatMember":
            from aiogram.types import ChatMemberAdministrator
            return ChatMemberAdministrator(
                user=User(id=BOT_ID, is_bot=True, first_name="bot"), status="administrator",
                can_be_edited=False, is_anonymous=False, can_manage_chat=True, can_delete_messages=True,
                can_manage_video_chats=True, can_restrict_members=True, can_promote_members=False,
                can_change_info=True, can_invite_users=True, can_post_stories=False,
                can_edit_stories=False, can_delete_stories=False,
            )
        return True

    async def download(self, file, destination=None, **kw):
        return io.BytesIO(self.files[file if isinstance(file, str) else file.file_id])

    # ── чтение записанного ───────────────────────────────────────────────────────────────
    def texts_to(self, chat_id: int) -> list[str]:
        out = []
        for m in self.calls:
            if isinstance(m, (SendMessage, EditMessageText)) and int(m.chat_id or 0) == chat_id:
                out.append(m.text)
            elif isinstance(m, (SendPhoto, SendDocument)) and int(m.chat_id) == chat_id:
                out.append(m.caption or "")
        return out

    def photos_to(self, chat_id: int) -> list[SendPhoto]:
        return [m for m in self.calls if isinstance(m, SendPhoto) and int(m.chat_id) == chat_id]

    def photo_recipients(self) -> list[int]:
        return [int(m.chat_id) for m in self.calls if isinstance(m, SendPhoto)]

    def alerts(self) -> list[str]:
        return [m.text for m in self.calls if isinstance(m, AnswerCallbackQuery) and m.text]

    def last_markup(self, chat_id: int):
        for m in reversed(self.calls):
            if isinstance(m, (SendMessage, EditMessageText)) and int(m.chat_id or 0) == chat_id and m.reply_markup:
                return m.reply_markup
        return None


def _routers():
    from handlers import admin, payment, registration, user_actions
    from handlers.access import admin_no_access, uat_seed
    from handlers.chat import group_chat
    from handlers.comms import polls
    from handlers.reg import onsite_reg, reg_silence_fallback
    return (
        group_chat.router, group_chat.private_router, uat_seed.router, admin.router,
        admin_no_access.router, payment.router, onsite_reg.router, registration.router,
        user_actions.router, polls.router, reg_silence_fallback.router,
    )


@contextmanager
def attached_dispatcher():
    routers = _routers()
    saved = [(r, r._parent_router) for r in routers]
    for r in routers:
        r._parent_router = None
    dp = Dispatcher(storage=MemoryStorage())
    from handlers import payment
    payment.init_payment_module(dp.storage)
    for r in routers:
        dp.include_router(r)
    # Слушатели первого входа — как в main.py.
    from services.forum import checkin as checkin_mod
    from services.forum.forum_welcome import register as register_forum_welcome
    import services.delegations.delegations as delegations
    checkin_mod.clear_first_entry_listeners()
    register_forum_welcome()
    checkin_mod.register_first_entry_listener(delegations.on_first_entry)
    try:
        yield dp
    finally:
        checkin_mod.clear_first_entry_listeners()
        for r, parent in saved:
            r._parent_router = parent


class World:
    """Один сценарий: бот, диспетчер, планировщик, Mini App. Живёт внутри одного event loop."""

    def __init__(self, dp: Dispatcher, bot: RecordingBot, scheduler, client):
        self.dp, self.bot, self.scheduler, self.client = dp, bot, scheduler, client
        self._uid = 1000

    # ── апдейты ────────────────────────────────────────────────────────────────────────────
    def _next(self) -> int:
        self._uid += 1
        return self._uid

    async def send(self, user_id: int, text: str | None = None, *, document: Document | None = None,
                   chat_id: int | None = None, chat_type: str = "private", username: str | None = None):
        n = self._next()
        user = User(id=user_id, is_bot=False, first_name="Тест", username=username)
        chat = Chat(id=chat_id or user_id, type=chat_type)
        msg = Message(message_id=n, date=int(time.time()), chat=chat, from_user=user, text=text, document=document)
        return await self.dp.feed_update(self.bot, Update(update_id=n, message=msg))

    async def tap(self, user_id: int, data: str, *, username: str | None = None):
        n = self._next()
        user = User(id=user_id, is_bot=False, first_name="Тест", username=username)
        chat = Chat(id=user_id, type="private")
        msg = Message(message_id=n, date=int(time.time()), chat=chat,
                      from_user=User(id=BOT_ID, is_bot=True, first_name="bot"), text="screen")
        cb = CallbackQuery(id=str(n), from_user=user, chat_instance="t", data=data, message=msg)
        return await self.dp.feed_update(self.bot, Update(update_id=n, callback_query=cb))

    async def upload(self, user_id: int, name: str, content: bytes):
        fid = f"file_{self._next()}"
        self.bot.files[fid] = content
        doc = Document(file_id=fid, file_unique_id=fid, file_name=name, file_size=len(content))
        return await self.send(user_id, None, document=doc)

    # ── Mini App ───────────────────────────────────────────────────────────────────────────
    def app_post(self, user_id: int, path: str, payload: dict):
        from tests.test_miniapp_routes import _hdr
        return self.client.post(path, json=payload, headers=_hdr(user_id))

    def app_get(self, user_id: int, path: str, **params):
        from tests.test_miniapp_routes import _hdr
        return self.client.get(path, params=params, headers=_hdr(user_id))

    # ── планировщик ────────────────────────────────────────────────────────────────────────
    def job_time_msk(self, job_id: str) -> str | None:
        job = self.scheduler.get_job(job_id)
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M")


def base_db(tmp_path, monkeypatch, *, delegates: int = 50, city: str = "msk") -> list[int]:
    """База как на проде перед форумом: модуль городов включён, сезон YL'26, N одобренных."""
    import asyncio

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "forum_day.db"))
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID])
    fast_init_db()
    dt = FrozenClock.make_class()
    monkeypatch.setattr(timeutil, "datetime", dt)
    import miniapp.timeutil as mt
    monkeypatch.setattr(mt, "datetime", dt)

    ids = [700_000 + i for i in range(delegates)]

    async def seed():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("event_season", "YL'26")
        await db.set_setting("miniapp_enabled", "on")
        async with db._connect() as conn:
            for i, tid in enumerate(ids):
                await conn.execute(
                    "INSERT INTO users (telegram_id, full_name, username, status, season, event_city, "
                    "registration_date, participant_type) VALUES (?, ?, ?, 'approved', ?, ?, ?, 'full')",
                    (tid, f"Делегатов Делегат{i:02d}", f"deleg{i:02d}", "YL'26", city, "2026-10-01 10:00:00"),
                )
            await conn.commit()
        from domain.cities import reload_cities
        await reload_cities()

    asyncio.run(seed())
    return ids


def make_scheduler():
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    return AsyncIOScheduler(
        jobstores={"default": MemoryJobStore()}, timezone=MSK_TZ,
        job_defaults={"misfire_grace_time": 86400, "coalesce": True},
    )


def miniapp_client(db_path: str):
    from tests.test_miniapp_routes import _cfg, _client
    return _client(_cfg(db_path, admin_ids=(ADMIN_ID,)))


def at(*args) -> None:
    FrozenClock.set_msk(*args)


def plus(**kw) -> None:
    FrozenClock.moment = FrozenClock.moment + timedelta(**kw)


@pytest.fixture
def world_factory(tmp_path, monkeypatch):
    """`run(scenario)` — сценарий `async def scenario(world, delegate_ids)` в одном event loop."""
    from services import scheduler as sched_mod

    ids = base_db(tmp_path, monkeypatch)

    def run(scenario):
        async def go():
            bot = RecordingBot()
            scheduler = make_scheduler()
            scheduler.start(paused=True)
            monkeypatch.setattr(sched_mod, "_scheduler", scheduler)
            monkeypatch.setattr(sched_mod, "_bot", bot)
            client = miniapp_client(config.DB_PATH)
            try:
                with attached_dispatcher() as dp:
                    return await scenario(World(dp, bot, scheduler, client), ids)
            finally:
                scheduler.shutdown(wait=False)
                await bot.session.close()
        return asyncio.run(go())

    return run


async def manager_sets_up_moscow(w: World) -> None:
    """Шапка — Москва; дата 30.10.2026, 2 дня, вход по QR вкл, рассылка 18:00 / 07:30."""
    from domain.cities import set_admin_city
    assert await set_admin_city(ADMIN_ID, MSK)
    await w.tap(ADMIN_ID, "settings_edit:forum_date")
    await w.tap(ADMIN_ID, "settings_edit_city:forum_date")
    await w.send(ADMIN_ID, "30.10.2026")
    await w.tap(ADMIN_ID, "settings_edit_city:sos_active_days")
    await w.send(ADMIN_ID, "2")
    await w.tap(ADMIN_ID, "toggle_checkin_qr_enabled")
    await w.tap(ADMIN_ID, f"checkinqr_time:evening:{MSK}")
    await w.send(ADMIN_ID, "18:00")
    await w.tap(ADMIN_ID, f"checkinqr_time:morning:{MSK}")
    await w.send(ADMIN_ID, "07:30")


VOLUNTEER = 905001


async def manager_invites_volunteer(w: World) -> str:
    """«🔗 Пригласить волонтёров» → ссылка «до конца форума», права «до конца форума», 30 человек."""
    await w.tap(ADMIN_ID, f"volinvite_cfg:{MSK}")
    await w.tap(ADMIN_ID, f"volinvite_toggle:{MSK}")
    await w.tap(ADMIN_ID, f"volinvite_new:{MSK}")
    await w.tap(ADMIN_ID, "volinv_le:forum")
    await w.tap(ADMIN_ID, "volinv_re:forum")
    await w.tap(ADMIN_ID, "volinv_lim:30")
    link = next(t for t in reversed(w.bot.texts_to(ADMIN_ID)) if "start=vol_" in (t or ""))
    import re
    code = re.search(r"start=vol_([\w-]+)", link).group(1)
    await w.send(VOLUNTEER, f"/start vol_{code}", username="vol_one")
    return code


def ready_lines(w: World) -> list[str]:
    """Строки светофора «🚦 Готовность к форуму» из последнего экрана админа."""
    text = w.bot.texts_to(ADMIN_ID)[-1]
    return [ln for ln in text.splitlines() if ln and ln[0] in "🟢🟡🔴⚪"]
