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
логируется (кроме `/chat_stats`, который сам ничего пользовательского не печатает). Каждый
хендлер ниже работает только с id/статусами/типами вложений, никогда с `message.text`/
`message.caption`.
"""
import logging

from aiogram import F, Router, types, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import city_codes, city_label, cities_module_on, enabled_cities, city_scope
from config import config
from database.db import (
    CHAT_PRESENT_STATUSES,
    bump_chat_activity,
    chat_activity_totals,
    chat_counts,
    chat_member_ids,
    log_chat_event,
    upsert_chat_member,
)
from services import chat_tracking
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

router = Router()

router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.my_chat_member.filter(F.chat.type.in_({"group", "supergroup"}))
router.chat_member.filter(F.chat.type.in_({"group", "supergroup"}))
router.callback_query.filter(F.message.chat.type.in_({"group", "supergroup"}))

_MEDIA_ATTRS = ("photo", "video", "document", "voice", "video_note", "animation", "sticker", "audio")


async def _event_name() -> str:
    return await get_setting_typed("event_name") or "мероприятие"


@router.my_chat_member()
async def on_bot_membership_changed(event: types.ChatMemberUpdated, bot: Bot):
    """Единственная точка, где привязывается чат: срабатывает на ЛЮБОЕ изменение статуса
    самого бота в группе, интересуют только переходы в «administrator» (привязка) и
    в «left»/«kicked» (авто-снятие, данные не трогаем — см. докстринг `chat_tracking.
    unbind_chat`)."""
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
            return
        event_name = await _event_name()
        if not await cities_module_on():
            await chat_tracking.bind_chat(event.from_user.id, event.chat.id, event.chat.title or "", None)
            try:
                await bot.send_message(event.chat.id, f"Чат подключён к «{event_name}».")
            except Exception as e:
                logger.warning("group_chat: не удалось отправить подтверждение привязки в чат id=%s: %s", event.chat.id, e)
            return

        codes = await enabled_cities()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"chatbind:{c['code']}")]
            for c in codes
        ])
        try:
            await bot.send_message(
                event.chat.id,
                f"Чат подключён к «{event_name}». К какому городу относится?",
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning("group_chat: не удалось спросить город в чате id=%s: %s", event.chat.id, e)
        return

    if new_status in ("left", "kicked"):
        bound = await chat_tracking.bound_chats()
        entry = next((b for b in bound if b["chat_id"] == event.chat.id), None)
        if entry is None:
            return  # бота выгнали из непривязанного чата — учёту и так нечего было делать
        await chat_tracking.unbind_chat(None, entry["city"])
        title = entry["title"] or event.chat.title or "чат"
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ Бота убрали из чата «{title}» — привязка снята, учёт остановлен.",
                )
            except Exception as e:
                logger.warning("group_chat: не удалось уведомить admin_id=%s об отвязке: %s", admin_id, e)


@router.callback_query(F.data.startswith("chatbind:"))
async def on_chatbind_pick(callback: types.CallbackQuery):
    """Инлайн-кнопки не истекают (WR-04, тот же довод, что у `event_city`/`season`/`resume`
    в мастере рассылки) — право перепроверяется здесь, а не только в момент отрисовки
    клавиатуры выше."""
    if not await chat_tracking.is_bot_admin_user(callback.from_user.id):
        await callback.answer("Привязать чат может только администратор бота.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1]
    if code not in city_codes():
        await callback.answer("Этот город больше не существует в реестре.", show_alert=True)
        return
    chat = callback.message.chat
    await chat_tracking.bind_chat(callback.from_user.id, chat.id, chat.title or "", code)
    label = await city_label(code)
    try:
        await callback.message.edit_text(
            f"Чат привязан к городу «{label}». Настройки — в админке бота, раздел "
            "«📢 Общение» → «💬 Чат»."
        )
    except Exception as e:
        logger.warning("group_chat: не удалось отредактировать сообщение после привязки: %s", e)
    await callback.answer()


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


@router.message(Command("chat_stats"))
async def on_chat_stats(message: types.Message):
    """Только для администратора бота — остальным молчит (никакого «недостаточно прав» в
    общей группе, чтобы не подсказывать посторонним, что здесь вообще что-то настроено)."""
    if not await chat_tracking.is_bot_admin_user(message.from_user.id):
        return
    chat_id = message.chat.id
    bound = await chat_tracking.bound_chats()
    entry = next((b for b in bound if b["chat_id"] == chat_id), None)
    if entry is None:
        await message.answer("Этот чат ещё не привязан к городу/событию — сводки нет.")
        return
    scope = city_scope(entry["city"]) if entry["city"] is not None else None
    counts = await chat_counts(chat_id, scope)
    member_ids = await chat_member_ids(chat_id)
    totals = await chat_activity_totals(chat_id)
    lines = [
        f"👥 Участников в базе бота: {len(member_ids)}",
        f"✅ Одобрено: {counts['approved']} · в чате {counts['in_chat']} · "
        f"не в чате {counts['not_in_chat']}",
        f"❓ В чате, но не зарегистрированы: {counts['unknown_members']}",
        f"💬 Сообщений сегодня: {totals['today']} · за 7 дней: {totals['week']}",
    ]
    await message.answer("\n".join(lines))


@router.message()
async def on_group_message(message: types.Message):
    """ПОСЛЕДНИЙ хендлер роутера — catch-all. Сматчился здесь -> дальше, к личным роутерам,
    апдейт не идёт (D-4). Текст/подпись сообщения нигде не читаются (D-9) — только факт
    наличия ответа/вложения по ИМЕНАМ полей, не по содержимому."""
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
