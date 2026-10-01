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

D-9 — железное правило всего модуля: текст сообщения из группы НИГДЕ не хранится и не
логируется. Каждый хендлер ниже работает только с id/статусами/типами вложений. Квик 260927
(живой рейтинг, решение владельца 20.09 «таблица message_id -> автор без текста»): единственное
касание текста — `_own_text_len`, длина текста считается и сразу забывается; сам текст не
сохраняется и не логируется. Форум-ночь п.8 (SOS) — ОДНО узкое, явное исключение:
`on_sos_id_command` матчит фиксированную команду `/sos_id` (не содержимое) и не читает
`message.text` за пределами этого совпадения — см. комментарий у самого хендлера.

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
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import city_codes, city_label, cities_module_on, enabled_cities
from config import config
from database.db import (
    CHAT_PRESENT_STATUSES,
    bump_chat_activity,
    log_chat_event,
    log_chat_message,
    set_chat_bot_state,
    set_chat_reactions,
    update_chat_message_len,
    upsert_chat_member,
    upsert_chat_username,
)
from services import chat_cleanup, chat_tracking
from services.timeutil import aware_to_msk
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

router = Router()

router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.my_chat_member.filter(F.chat.type.in_({"group", "supergroup"}))
router.chat_member.filter(F.chat.type.in_({"group", "supergroup"}))
# Квик 260927: правки (длина сообщения в журнале рейтинга) и реакции. Регистрация observer'ов —
# это и есть подписка: aiogram собирает allowed_updates из них (resolve_used_update_types).
router.edited_message.filter(F.chat.type.in_({"group", "supergroup"}))
router.message_reaction.filter(F.chat.type.in_({"group", "supergroup"}))

_MEDIA_ATTRS = ("photo", "video", "document", "voice", "video_note", "animation", "sticker", "audio")


def _is_real_reply(message: types.Message) -> bool:
    """Настоящий ответ пользователя, а не автопривязка Telegram к корню топика.

    В форум-группах (супергруппа с топиками, `is_topic_message=True`) `reply_to_message`
    указывает на корневое сообщение топика у КАЖДОГО сообщения в нём, даже если человек ничего
    руками не отвечал — иначе `chat_activity.replies` в топиках равнялся `messages`. Корень
    топика узнаётся по `forum_topic_created` на нём самом либо по совпадению его
    `message_id` с `message.message_thread_id` (оба поля добавлены в aiogram под форумы, есть
    в установленной версии). В обычных группах (не топики) `is_topic_message` не выставлен —
    поведение не меняется, любой `reply_to_message` — реальный ответ."""
    replied = message.reply_to_message
    if not replied:
        return False
    if getattr(message, "is_topic_message", False):
        if getattr(replied, "forum_topic_created", None) is not None:
            return False
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None and getattr(replied, "message_id", None) == thread_id:
            return False
    return True


# Квик 260927: что пишется в журнал рейтинга (chat_messages). Остальные типы (служебные
# уведомления, создание темы и т.п.) журналом не учитываются.
LOGGED_CONTENT_TYPES = frozenset({
    "text", "photo", "video", "document", "voice", "video_note", "animation", "sticker",
    "audio", "poll", "location", "venue", "contact", "dice", "story",
})
_MEDIA_KINDS = {"photo", "video", "document", "voice", "video_note", "audio"}


def _message_kind(message: types.Message) -> str:
    content_type = message.content_type
    if content_type == "text":
        return "text"
    if content_type in ("sticker", "animation"):
        return "sticker"
    if content_type in _MEDIA_KINDS:
        return "media"
    return "other"


def _own_text_len(message: types.Message) -> int:
    """ЕДИНСТВЕННОЕ касание текста в модуле (D-9): длина считается и сразу забывается.
    Пересланный чужой текст — не собственный вклад автора, длина 0 (автопересылка из
    связанного канала — не «чужой» текст, это сам пост канала)."""
    if getattr(message, "forward_origin", None) is not None and not getattr(message, "is_automatic_forward", False):
        return 0
    return len(message.text or message.caption or "")


def _reply_fields(message: types.Message) -> tuple[int | None, int | None]:
    """(id сообщения-цели, автор-человек цели). Автопривязка к корню темы — не ответ; ответ
    боту, анонимному админу или посту канала — ответ без автора-человека."""
    if not _is_real_reply(message):
        return None, None
    replied = message.reply_to_message
    author = None
    sender = getattr(replied, "from_user", None)
    if getattr(replied, "sender_chat", None) is None and sender is not None and not sender.is_bot:
        author = sender.id
    return replied.message_id, author


def _msk_ts(dt) -> str:
    return aware_to_msk(dt).strftime("%Y-%m-%d %H:%M:%S")


async def _is_bound(chat_id: int) -> bool:
    return any(b["chat_id"] == chat_id for b in await chat_tracking.bound_chats())


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
    # Квик 260927: статус бота и право «Удаление сообщений» — для ЛЮБОГО чата (привязанного
    # или нет), до всех веток ниже. Права вернули повышением — очистка служебных уведомлений
    # (services/chat_cleanup.py) продолжится сама. Fail-soft: сбой БД здесь не должен сорвать
    # привязку чата и личное сообщение админу ниже.
    try:
        await set_chat_bot_state(event.chat.id, new_status, chat_tracking.can_delete_from(event.new_chat_member))
    except Exception as e:
        logger.warning("group_chat: chat_bot_state для чата id=%s не записан: %s: %s",
                       event.chat.id, type(e).__name__, e)
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
            # Квик 260915-twr (D2): сверка сразу после привязки, отчёт — личным сообщением,
            # отдельным от подтверждения выше (два разных события во времени).
            reconcile_start = await get_setting_typed(chat_tracking.CHAT_BIND_RECONCILE_START_KEY)
            await _dm(bot, event.from_user.id, reconcile_start)
            await chat_tracking.schedule_bind_reconcile(event.chat.id, None, event.from_user.id)
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
async def on_new_chat_members(message: types.Message, bot: Bot | None = None):
    """Фолбэк, когда апдейт `chat_member` не пришёл (не у всех прав бота он включается
    одинаково надёжно) — тот же учёт, `source="message"`. Квик 260927: учёт — СНАЧАЛА,
    удаление уведомления «вступил(а)» (если менеджер отметил этот тип) — последней строкой."""
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        await upsert_chat_member(message.chat.id, user.id, "member", source="message")
        await log_chat_event(message.chat.id, user.id, "join")
    if bot is not None:
        await chat_cleanup.handle_service_message(bot, message.chat.id, message.message_id, "join")


@router.message(F.left_chat_member)
async def on_left_chat_member(message: types.Message, bot: Bot | None = None):
    user = message.left_chat_member
    if not user.is_bot:
        await upsert_chat_member(message.chat.id, user.id, "left", source="message")
        await log_chat_event(message.chat.id, user.id, "leave")
    if bot is not None:
        await chat_cleanup.handle_service_message(bot, message.chat.id, message.message_id, "leave")


# Форум-ночь п.8 (идея №19, SOS): `/sos_id` — узкое, ЯВНОЕ исключение из D-9 («текст сообщения
# из группы нигде не читается») для ЭТОГО модуля. D-9 запрещает читать СОДЕРЖИМОЕ (что человек
# написал), а не адресную команду того же класса, что my_chat_member/chat_member апдейты выше —
# `Command("sos_id")` матчит фиксированный префикс, не текст. Бот по-прежнему НИКОГДА не пишет
# В ГРУППУ (правка 15.09) — ответ (подтверждение привязки) идёт личным сообщением отправителю
# команды, ровно как у остальной привязки чата (`services.chat_tracking`/`services.sos`), и
# только если для него есть незакрытая заявка `sos_chat_bind_pending` (иначе — тишина, тот же
# D-9-приём, что у `on_bot_membership_changed`: без явной заявки — ни ответа, ни намёка).
# Отдельной перепроверки капы `settings` здесь нет — сама заявка уже АВТОРИЗАЦИЯ: она могла
# появиться только через `handlers/admin_sos.py::asos_bind_start`, который сам сидит под
# `ADMIN_CAPS["asos_bind"] = "settings"` (CapabilityMiddleware на `admin.router`); человек без
# этого права не мог поставить заявку, на которую отвечает эта команда.
@router.message(Command("sos_id"))
async def on_sos_id_command(message: types.Message, bot: Bot):
    from services import sos as sos_service

    await sos_service.complete_chat_bind(
        bot, message.from_user.id, message.chat.id, message.chat.title or "",
    )


# Квик 260927: остальные служебные уведомления из закрытого набора автоочистки (закреп, смена
# названия/фото, темы, бусты, видеочаты). Уведомление съедается здесь, ДО catch-all ниже —
# поэтому закреп или смена названия больше не засчитываются автору как активность в чате.
# Вступления/выходы — в своих хендлерах выше (там сначала учёт); создание темы
# (forum_topic_created) в набор не входит и идёт в catch-all, как раньше.
_SERVICE_CONTENT_TYPES = set(chat_cleanup.CONTENT_TYPE_TO_CODE) - {"new_chat_members", "left_chat_member"}


@router.message(F.content_type.in_(_SERVICE_CONTENT_TYPES))
async def on_group_service_message(message: types.Message, bot: Bot):
    code = chat_cleanup.CONTENT_TYPE_TO_CODE.get(message.content_type)
    if code is not None:
        await chat_cleanup.handle_service_message(bot, message.chat.id, message.message_id, code)


async def _sos_card_reply_report(message: types.Message, bot: Bot | None) -> dict | None:
    """Заявка SOS, на карточку которой ответили В ЕЁ ЖЕ чате SOS, иначе `None`. Карточка —
    сообщение САМОГО бота с номером заявки (`services.sos.card_report_id`), а чат реплая —
    тот, куда эта карточка ушла (`sos_reports.chat_id`): пересланная в другую группу копия
    ответа делегату не даёт."""
    from database.db import get_sos_report
    from services import sos as sos_service

    replied = message.reply_to_message
    bot = bot or getattr(message, "bot", None)
    author = getattr(replied, "from_user", None) if replied is not None else None
    if author is None or bot is None or author.id != bot.id:
        return None
    report_id = sos_service.card_report_id(replied)
    if report_id is None:
        return None
    report = await get_sos_report(report_id)
    if report is None or report.get("chat_id") != message.chat.id:
        return None
    return report


async def _answer_sos_card_reply(message: types.Message, bot: Bot, report: dict) -> None:
    """Ответ делегату реплаем из чата SOS. Сразу (с неявным захватом) — держателю «📋 Модерация
    заявок»; остальным участникам чата — после «🙋 Беру» (кнопку в привязанном чате SOS жмёт
    любой его участник, `on_sos_card_button`): так у ответа всегда есть видимый на карточке
    ответственный, а случайное сообщение в треде не уходит человеку в беде. Без захвата —
    подсказка в чат, а не тишина: иначе орг уверен, что ответил."""
    from handlers.admin_caps import has_capability
    from services import sos as sos_service

    uid = message.from_user.id
    if report.get("claimed_by") != uid and not await has_capability(uid, "moderate_reg"):
        await message.reply(
            f"Чтобы ответить делегату, сначала нажмите «🙋 Беру» под карточкой SOS "
            f"#{report['id']} — так команда увидит, кто ведёт этот SOS. Потом ответьте реплаем ещё раз."
        )
        return
    await sos_service.deliver_org_reply(bot, message, report)


# «🙋 Беру»/«✅ Решено» под карточкой в чате SOS — здесь, мимо `CapabilityMiddleware`: в
# привязанном чате SOS кнопки жмёт любой его участник. Чат SOS — это чат команды города, его
# привязывает менеджер с правом «⚙️ Настройки»; в Тюмени и Москве это общий чат команды, где
# дежурят волонтёры без ролей в боте, и выдавать каждому роль утром форума никто не будет.
# Что карточка именно из чата этой заявки (а не пересланная в другую группу), проверяет сам
# хендлер (`handlers/admin_sos.py::_card_origin_ok`) — с понятным алертом, не тишиной. Копии
# карточки в личке (фоллбэк) идут мимо этого хендлера в admin.router под капой, как раньше.
@router.callback_query(
    F.data.regexp(r"^sos_(claim|resolve):"), F.message.chat.type.in_({"group", "supergroup"}),
)
async def on_sos_card_button(callback: types.CallbackQuery, bot: Bot, fsm_storage=None):
    from handlers import admin_sos  # ленивый: домен кнопок живёт там, модуль висит на admin.router

    if callback.data.startswith("sos_claim:"):
        await admin_sos.sos_claim(callback, bot)
    else:
        await admin_sos.sos_resolve(callback, bot, fsm_storage=fsm_storage)


@router.message()
async def on_group_message(message: types.Message, bot: Bot | None = None):
    """ПОСЛЕДНИЙ хендлер роутера — catch-all. Сматчился здесь -> дальше, к личным роутерам,
    апдейт не идёт (D-4). Текст/подпись сообщения нигде не читаются (D-9) — только факт
    наличия ответа/вложения по ИМЕНАМ полей, не по содержимому. Бот НИЧЕГО не отвечает в
    группу — только считает (правка 15.09: снесён `/chat_stats`, единственный хендлер,
    который отвечал прямо в группу).

    Квик 260927: плюс строка журнала рейтинга (chat_messages) — без текста, только длина.
    Автопересылка поста из связанного канала приходит от служебного 777000 (не бот) и раньше
    засчитывалась как делегат — теперь это пост канала: в журнал с is_channel_post, в
    активность людей — нет."""
    if getattr(message, "is_automatic_forward", False) and message.sender_chat is not None:
        if await chat_tracking.tracking_on() and await _is_bound(message.chat.id):
            await log_chat_message(
                message.chat.id, message.message_id, message.sender_chat.id, _msk_ts(message.date),
                kind=_message_kind(message), text_len=_own_text_len(message),
                reply_to_message_id=None, reply_to_author_id=None, is_channel_post=True,
            )
        return
    if message.from_user is None or message.from_user.is_bot:
        return
    # Ответ орга реплаем на карточку SOS — до учёта активности и независимо от того, ведётся
    # ли рейтинг этого чата: чат SOS привязан отдельно (`services.sos`), не через chat_tracking.
    # Единственное исключение из D-9: текст ответа читается, чтобы переслать его делегату, и
    # нигде не хранится. Кто вправе ответить — `_answer_sos_card_reply`.
    sos_report = await _sos_card_reply_report(message, bot)
    if sos_report is not None:
        await _answer_sos_card_reply(message, bot or message.bot, sos_report)
    if not await chat_tracking.tracking_on():
        return
    if not await _is_bound(message.chat.id):
        return
    reply = _is_real_reply(message)
    media = any(getattr(message, attr, None) for attr in _MEDIA_ATTRS)
    await bump_chat_activity(message.chat.id, message.from_user.id, reply=reply, media=media)
    if getattr(message, "content_type", None) in LOGGED_CONTENT_TYPES:
        reply_mid, reply_author = _reply_fields(message)
        await log_chat_message(
            message.chat.id, message.message_id, message.from_user.id, _msk_ts(message.date),
            kind=_message_kind(message), text_len=_own_text_len(message),
            reply_to_message_id=reply_mid, reply_to_author_id=reply_author,
        )
        await upsert_chat_username(message.from_user.id, message.from_user.username,
                                   message.from_user.first_name)


@router.edited_message()
async def on_group_edited_message(message: types.Message):
    """Квик 260927: правка меняет только длину в журнале рейтинга (текст не читается дальше
    `_own_text_len`)."""
    if not await chat_tracking.tracking_on() or not await _is_bound(message.chat.id):
        return
    await update_chat_message_len(message.chat.id, message.message_id, _own_text_len(message))


def _reaction_key(reaction) -> str | None:
    kind = getattr(reaction, "type", None)
    if kind == "emoji":
        return reaction.emoji
    if kind == "custom_emoji":
        return f"custom:{reaction.custom_emoji_id}"
    if kind == "paid":
        return "paid"
    return None


@router.message_reaction()
async def on_group_reaction(event: types.MessageReactionUpdated):
    """Квик 260927: текущие реакции человека на сообщение (приходят, только если бот —
    администратор чата). Реакции от имени чата/канала (actor_chat без user) и ботов не
    учитываются."""
    user = event.user
    if user is None or user.is_bot:
        return
    if not await chat_tracking.tracking_on() or not await _is_bound(event.chat.id):
        return
    keys = [k for k in (_reaction_key(r) for r in event.new_reaction) if k]
    await set_chat_reactions(event.chat.id, event.message_id, user.id, keys, _msk_ts(event.date))
    # Тот, кто только ставит реакции, тоже попадает в рейтинг («отдача») — нужна подпись.
    await upsert_chat_username(user.id, getattr(user, "username", None), getattr(user, "first_name", None))


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

    # Квик 260915-twr (D2): сверка сразу после привязки, отчёт — личным сообщением, отдельным
    # от подтверждения выше (два разных события во времени).
    reconcile_start = await get_setting_typed(chat_tracking.CHAT_BIND_RECONCILE_START_KEY)
    await _dm(bot, callback.from_user.id, reconcile_start)
    await chat_tracking.schedule_bind_reconcile(chat_id, code, callback.from_user.id)
