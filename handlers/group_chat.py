"""Квик 260914-rgr (RGR-01..07): групповой роутер — единственное место, где бот отвечает
на апдейты ИЗ ГРУППЫ/СУПЕРГРУППЫ (личные роутеры — `admin.router`/`registration.router`/
`user_actions.router`/`payment.router` — на групповые чаты не рассчитаны вовсе).

Форма — СВОЙ `Router()`, мимо `CapabilityMiddleware` (та висит на `admin.router`, не на
`dp`), тот же приём, что `handlers/uat_seed.py`: право на привязку/отвязку чата проверяется
ВНУТРИ каждого хендлера (`chat_tracking.is_bot_admin_user`), а не через капу — человек,
добавляющий бота в группу, ещё может быть неизвестен `staff`/`ADMIN_IDS` вовсе, и middleware
на несуществующем праве просто не сработала бы.

Роутер подключается в `main.py` ПЕРВЫМ (перед `uat_seed.router`/`admin.router`/...) — не
потому что группа важнее личных апдейтов, а потому что регистрация `my_chat_member`/
`chat_member` observer'ов здесь — единственный способ вообще ПОЛУЧИТЬ эти апдейты от
Telegram (aiogram собирает `allowed_updates` из зарегистрированных наблюдателей, прецедент
`tests/test_polls_260822.py`). Фильтры уровня роутера (`chat.type in {group, supergroup}`)
гарантируют, что личные апдейты («privet», анкета, /start в личке) через этот роутер
проходят НАСКВОЗЬ, не приставая ни к одному хендлеру ниже — а групповые сообщения, наоборот,
СЪЕДАЮТСЯ catch-all хендлером `on_group_message` в самом конце файла и дальше, к
`registration.router`, не идут (D-4): личные хендлеры в группе больше не отвечают.

D-9 — железное правило всего модуля: текст сообщения из группы НИГДЕ не читается и не
логируется. Каждый хендлер ниже работает только с id/статусами/типами вложений, никогда с
`message.text`/`message.caption`.

Правка 15.09 (владелец, «привязка через личку админа»): бот БОЛЬШЕ НИКОГДА не пишет В ГРУППУ —
ни подтверждение привязки, ни вопрос о городе, ни `/chat_stats` (команда снесена целиком).
Вместо этого `on_bot_membership_changed` пишет ЛИЧНО тому, кто повысил бота до администратора
(`event.from_user`); если личка не доставилась (человек не открывал бота ни разу) — тот же
текст/клавиатура уходят веером в `config.ADMIN_IDS`, и привязывает первый ответивший. Сам
выбор города (`chatbind:{code}:{chat_id}`) — коллбэк из ЛИЧНОГО чата, поэтому живёт в отдельном
`private_router` этого же модуля (не на `router` выше: тот целиком отфильтрован по
`chat.type in {group, supergroup}`), но так же мимо `admin.router`/`CapabilityMiddleware` — то
же обоснование, что у группового `router`: получатель личного сообщения с кнопками уже
перепроверен `is_bot_admin_user` до отправки, а капа привязывала бы ту же проверку под другим
именем, не независимый гейт.
"""
import logging

from aiogram import F, Router, types, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import city_codes, city_label, cities_module_on, enabled_cities
from config import config
from database.db import (
    CHAT_PRESENT_STATUSES,
    bump_chat_activity,
    log_chat_event,
    upsert_chat_member,
)
from services import chat_tracking

logger = logging.getLogger(__name__)

router = Router()

router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.my_chat_member.filter(F.chat.type.in_({"group", "supergroup"}))
router.chat_member.filter(F.chat.type.in_({"group", "supergroup"}))

_MEDIA_ATTRS = ("photo", "video", "document", "voice", "video_note", "animation", "sticker", "audio")


async def _dm(bot: Bot, user_id: int, text: str, reply_markup=None) -> bool:
    """Личное сообщение с fail-soft: неудача (человек не открывал бота, заблокировал его и
    т.п.) не рвёт вызывающий хендлер, только сигналит `False` — вызывающий сам решает, звать
    ли фолбэк. Лог — только id получателя и класс исключения, без текста сообщения (тот
    всегда наш собственный, не делегатский, но дисциплина «личка — не для чужих глаз в логе»
    держится одинаково для всех личных сообщений этого модуля)."""
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
        return True
    except Exception as e:
        logger.info("group_chat._dm: не удалось написать id=%s: %s: %s", user_id, type(e).__name__, e)
        return False


@router.my_chat_member()
async def on_bot_membership_changed(event: types.ChatMemberUpdated, bot: Bot):
    """Единственная точка, где привязывается чат: срабатывает на ЛЮБОЕ изменение статуса
    самого бота в группе, интересуют только переходы в «administrator» (привязка) и
    в «left»/«kicked» (авто-снятие, данные не трогаем — см. докстринг `chat_tracking.
    unbind_chat`). Ни один из веток НИКОГДА не пишет В САМ ЧАТ (`event.chat.id`) — только в
    личку промоутеру/`config.ADMIN_IDS` (правка 15.09)."""
    if event.new_chat_member.user.id != bot.id:
        return  # изменился статус не бота, а другого участника — это дело chat_member ниже

    new_status = event.new_chat_member.status
    if new_status == "administrator":
        admin_ok = await chat_tracking.is_bot_admin_user(event.from_user.id)
        if not admin_ok:
            logger.info(
                "group_chat: администратором бота сделал id=%s (без права settings) в чате id=%s — привязка не выполнена",
                event.from_user.id, event.chat.id,
            )
            return  # D-1: ни личка, ни группа не получают ни единого сообщения

        title = event.chat.title or "чат"
        if not await cities_module_on():
            await chat_tracking.bind_chat(event.from_user.id, event.chat.id, title, None)
            text = f"Бот добавлен в чат «{title}» и подключён к событию."
            if not await _dm(bot, event.from_user.id, text):
                for admin_id in config.ADMIN_IDS:
                    await _dm(bot, admin_id, text)
                logger.info(
                    "group_chat: личка промоутеру id=%s не доставлена, подтверждение привязки "
                    "чата id=%s разослано ADMIN_IDS", event.from_user.id, event.chat.id,
                )
            return

        codes = await enabled_cities()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=await city_label(c["code"]),
                callback_data=f"chatbind:{c['code']}:{event.chat.id}",
            )]
            for c in codes
        ])
        question = f"Бот добавлен в чат «{title}». К какому городу относится?"
        if not await _dm(bot, event.from_user.id, question, reply_markup=kb):
            for admin_id in config.ADMIN_IDS:
                await _dm(bot, admin_id, question, reply_markup=kb)
            logger.info(
                "group_chat: личка промоутеру id=%s не доставлена, вопрос о городе для чата "
                "id=%s разослан ADMIN_IDS", event.from_user.id, event.chat.id,
            )
        return

    if new_status in ("left", "kicked"):
        bound = await chat_tracking.bound_chats()
        entry = next((b for b in bound if b["chat_id"] == event.chat.id), None)
        if entry is None:
            return  # бота выгнали из непривязанного чата — учёту и так нечего было делать
        await chat_tracking.unbind_chat(None, entry["city"])
        title = entry["title"] or event.chat.title or "чат"
        text = f"⚠️ Бота убрали из чата «{title}» — привязка снята, учёт остановлен."
        for admin_id in config.ADMIN_IDS:
            await _dm(bot, admin_id, text)


@router.chat_member()
async def on_chat_member_changed(event: types.ChatMemberUpdated):
    """Любое изменение статуса УЧАСТНИКА (не самого бота — тот выше, my_chat_member).
    Ботов пропускаем: gamification/сервисные боты в группе не в счёт."""
    user = event.new_chat_member.user
    if user.is_bot:
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    await upsert_chat_member(event.chat.id, user.id, new_status, source="chat_member")
    was_present = old_status in CHAT_PRESENT_STATUSES
    now_present = new_status in CHAT_PRESENT_STATUSES
    if now_present and not was_present:
        await log_chat_event(event.chat.id, user.id, "join")
    elif was_present and not now_present:
        await log_chat_event(event.chat.id, user.id, "kick" if new_status == "kicked" else "leave")


@router.message(F.new_chat_members)
async def on_new_chat_members(message: types.Message):
    """Фолбэк, когда апдейт `chat_member` не пришёл (не у всех прав бота он включается
    одинаково надёжно) — тот же учёт, `source="message"`."""
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        await upsert_chat_member(message.chat.id, user.id, "member", source="message")
        await log_chat_event(message.chat.id, user.id, "join")


@router.message(F.left_chat_member)
async def on_left_chat_member(message: types.Message):
    user = message.left_chat_member
    if user.is_bot:
        return
    await upsert_chat_member(message.chat.id, user.id, "left", source="message")
    await log_chat_event(message.chat.id, user.id, "leave")


@router.message()
async def on_group_message(message: types.Message):
    """ПОСЛЕДНИЙ хендлер роутера — catch-all. Сматчился здесь -> дальше, к личным роутерам,
    апдейт не идёт (D-4). Текст/подпись сообщения нигде не читаются (D-9) — только факт
    наличия ответа/вложения по ИМЕНАМ полей, не по содержимому. Бот НИЧЕГО не отвечает в
    группу — только считает (правка 15.09: снесён `/chat_stats`, единственный хендлер,
    который отвечал прямо в группу)."""
    if message.from_user is None or message.from_user.is_bot:
        return
    if not await chat_tracking.tracking_on():
        return
    bound = await chat_tracking.bound_chats()
    if not any(b["chat_id"] == message.chat.id for b in bound):
        return
    reply = bool(message.reply_to_message)
    media = any(getattr(message, attr, None) for attr in _MEDIA_ATTRS)
    await bump_chat_activity(message.chat.id, message.from_user.id, reply=reply, media=media)


# ── Личка: выбор города после сообщения от бота (правка 15.09) ──────────────────────────
#
# Отдельный роутер, НЕ `router` выше (тот целиком отфильтрован по `chat.type in {group,
# supergroup}`) и НЕ `admin.router` (тот несёт `CapabilityMiddleware`, deny-by-default —
# `handlers/admin_caps.py`): право привязать чат здесь уже перепроверено `is_bot_admin_user`
# ДО отправки личного сообщения с кнопками, а капа поверх была бы той же самой проверкой под
# другим именем, не независимым гейтом (тот же довод, что у группового `router` в докстринге
# модуля).
private_router = Router()
private_router.callback_query.filter(F.message.chat.type == "private")


@private_router.callback_query(F.data.startswith("chatbind:"))
async def on_chatbind_pick(callback: types.CallbackQuery, bot: Bot):
    """Инлайн-кнопки не истекают (WR-04, тот же довод, что у `event_city`/`season`/`resume`
    в мастере рассылки) — право перепроверяется здесь, а не только в момент отправки личного
    сообщения выше. `callback_data` несёт ЦЕЛЕВОЙ `chat_id` (личное сообщение может прийти
    не только исходному промоутеру, но и веером в `config.ADMIN_IDS` — «первый ответивший
    привязывает», сам чат-получатель коллбэка тут ни при чём)."""
    if not await chat_tracking.is_bot_admin_user(callback.from_user.id):
        await callback.answer("Привязать чат может только администратор бота.", show_alert=True)
        return
    _, code, chat_id_raw = callback.data.split(":", 2)
    if code not in city_codes():
        await callback.answer("Этот город больше не существует в реестре.", show_alert=True)
        return
    try:
        chat_id = int(chat_id_raw)
    except (TypeError, ValueError):
        await callback.answer("Не удалось определить чат — добавьте бота в группу заново.", show_alert=True)
        return

    title = ""
    try:
        chat = await bot.get_chat(chat_id)
        title = chat.title or ""
    except Exception as e:
        logger.warning("group_chat.on_chatbind_pick: не удалось получить чат id=%s: %s", chat_id, e)

    await chat_tracking.bind_chat(callback.from_user.id, chat_id, title, code)
    label = await city_label(code)
    confirm = (
        f"Чат «{title}» привязан к городу «{label}»."
        if title else f"Чат привязан к городу «{label}»."
    )
    confirm += " Учёт можно включить в разделе «🔧 Управление» бота."
    try:
        await callback.message.edit_text(confirm)
    except Exception as e:
        logger.warning("group_chat: не удалось отредактировать сообщение после привязки: %s", e)
    await callback.answer()
