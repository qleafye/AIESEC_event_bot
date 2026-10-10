"""Квик 260919-u7e (находка #3, аудит прода 19.09,
`.planning/review-260919-sections/01-reg-chat.md`): бот молчит, если рестарт контейнера снёс
FSM (MemoryStorage) посреди анкеты делегата. `reg_drafts` персистентен и переживает рестарт —
ответы не потеряны, но следующее сообщение делегата (или тап по старой inline-кнопке анкеты)
не ловит НИ ОДИН хендлер: единственный `@router.message()` без фильтра состояния —
`handlers/group_chat.py::group_message` — отфильтрован на уровне роутера по
`chat.type in {group, supergroup}`, приватные апдейты проходят сквозь него нетронутыми.
14 из 38 делегатов, оказавшихся в анкете за 20 минут до рестарта (05–16.09), не вернулись ни
разу.

ВАЖНО (не разводить второй catch-all-роутер для ТЕКСТА): `handlers/user_actions.py::
reg_handoff_idle_fallback` (`StateFilter(None), F.text`) уже стоит ПОСЛЕДНИМ message-хендлером
в `user_actions.router` и БЕЗУСЛОВНО забирает любое безсостояние текстовое сообщение первым
(аiogram останавливает propagation на первом совпавшем фильтре в цепочке роутеров) — отдельный
роутер этого модуля, включённый в main.py ПОСЛЕ `user_actions.router`, физически никогда не
увидел бы ни одного текстового апдейта. Поэтому текстовая ветка живёт прямо в
`reg_handoff_idle_fallback` (вызывает `offer_if_resumable` отсюда), а этот модуль/роутер несёт
только то, что `reg_handoff_idle_fallback` не ловит: НЕтекстовые сообщения (документ/фото —
например, повторная присылка резюме файлом после рестарта) и tap по УСТАРЕВШЕЙ inline-кнопке
анкеты (`callback_query` — ни один роутер до этого не держит безусловного catch-all по
callback_query, так что этот хендлер реально достижим, включён в main.py САМЫМ ПОСЛЕДНИМ).

Кнопка ведёт в ТОТ ЖЕ путь возобновления, что и `/start` — `handlers.reg_resume.offer_resume`
+ `reg_resume:continue`/`reg_resume:restart` (переиспользованы байт-в-байт, не задублированы).
"""
import logging
import time

from aiogram import F, Router, types
from aiogram.filters import StateFilter

from config import config
from database.db import get_user
from handlers.admin_caps import resolve_capabilities
from handlers.reg_resume import offer_resume
from handlers.registration import _resumable_draft_for
from handlers import reg_i18n
from keyboards.builders import get_main_menu_kb
from domain.regform.engine import has_submitted_anketa
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

router = Router(name="reg_silence_fallback")

# «Меню обновилось» — не чаще раза в 10 минут на человека: делегат, который жмёт мёртвую
# кнопку подряд, получает одну подсказку, а не ответ на каждое нажатие. В памяти процесса —
# после рестарта подсказка придёт ещё раз, это безвредно.
MENU_HINT_COOLDOWN_SECONDS = 600
_menu_hint_sent_at: dict[int, float] = {}


async def _is_staff_or_admin(telegram_id: int) -> bool:
    """Менеджер/модератор никогда не должен увидеть делегатский экран «анкета сохранена» —
    ни ADMIN_IDS (полные права), ни держатель произвольной роли из `staff` (D-13 capability
    bootstrap, `handlers/admin_caps.py::resolve_capabilities`)."""
    if telegram_id in config.ADMIN_IDS:
        return True
    try:
        return bool(await resolve_capabilities(telegram_id))
    except Exception as e:
        logger.error(f"reg_silence_fallback: resolve_capabilities failed for {telegram_id}: {e}")
        # Fail-soft В СТОРОНУ «это стафф» -- сбой чтения ролей не должен подсунуть менеджеру
        # делегатский экран; в худшем случае этот безобидно недостающий ответ выглядит как
        # прежняя тишина, а не как новая протечка в админку.
        return True


async def offer_if_resumable(chat_message: types.Message) -> bool:
    """Общее тело — вызывается и отсюда (документ/фото, callback), и из
    `handlers/user_actions.py::reg_handoff_idle_fallback` (текст, см. докстринг модуля выше).
    `chat_message` — любой объект с `.chat`/`.answer` для приватной переписки (`reg_i18n.say`/
    `ctx_for` работают через `chat.id`, тот же приём, что и `callback.message` во всём модуле
    `reg_resume.py`). True — ответили (показали «анкета сохранена» + экран возобновления),
    False — тишина (как и раньше: нет черновика, или это staff/admin)."""
    uid = chat_message.chat.id
    if await _is_staff_or_admin(uid):
        return False
    draft = await _resumable_draft_for(uid)
    if not draft:
        return False
    await reg_i18n.say(chat_message, await get_setting_typed("reg_resume_after_restart_text"))
    await offer_resume(chat_message, draft)
    return True


async def reply_if_submitted(chat_message: types.Message) -> bool:
    """Приёмка 09.10: делегат подал анкету в чате и пишет «когда ответ» — бот молчал.
    `reg_already_submitted_text` отвечал только из `RegHandoffGuard` (состояние
    `Registration:*`), а после подачи из чата FSM уже очищен. Отвечаем тем же текстом, если
    анкета этого сезона подана и ещё ждёт решения. Одобренным/отклонённым — по-прежнему
    молчание здесь: «анкета на проверке» им неверно (одобренному отвечает
    `reply_menu_refreshed`), а «❓ Задать вопрос» ожидающему закрыт."""
    if getattr(chat_message.chat, "type", "private") != "private":
        return False
    uid = chat_message.chat.id
    if await _is_staff_or_admin(uid):
        return False
    try:
        user_row = await get_user(uid)
        season = await get_setting_typed("event_season") or None
    except Exception as e:
        logger.error(f"reg_silence_fallback: submitted check failed for {uid}: {e}")
        return False
    if not has_submitted_anketa(user_row, season) or user_row.get("status") != "pending":
        return False
    await reg_i18n.say(chat_message, await get_setting_typed("reg_already_submitted_text"))
    return True


async def reply_menu_refreshed(chat_message: types.Message) -> bool:
    """10.10: делегат с поданной анкетой жмёт кнопку старой клавиатуры (подпись переименовали)
    или пишет то, чего бот не понимает, — раньше тишина. Отвечаем коротким текстом и
    присылаем актуальное меню. Только текст в личке, не персоналу (у него свои экраны), не
    чаще `MENU_HINT_COOLDOWN_SECONDS`; повтор в окне — тишина."""
    if getattr(chat_message.chat, "type", "private") != "private":
        return False
    if not getattr(chat_message, "text", None):
        return False
    uid = chat_message.chat.id
    now = time.monotonic()
    last = _menu_hint_sent_at.get(uid)
    if last is not None and now - last < MENU_HINT_COOLDOWN_SECONDS:
        return False
    if await _is_staff_or_admin(uid):
        return False
    try:
        user_row = await get_user(uid)
        season = await get_setting_typed("event_season") or None
    except Exception as e:
        logger.error(f"reg_silence_fallback: menu hint check failed for {uid}: {e}")
        return False
    if not has_submitted_anketa(user_row, season):
        return False
    _menu_hint_sent_at[uid] = now
    await reg_i18n.say(
        chat_message, await get_setting_typed("menu_refreshed_text"),
        reply_markup=await get_main_menu_kb(uid),
    )
    return True


async def reply_idle(chat_message: types.Message) -> None:
    """Последний ответ на сообщение без состояния: сначала незаконченная анкета (рестарт
    посреди заполнения), потом «анкета уже отправлена», последним — «меню обновилось»."""
    if await offer_if_resumable(chat_message):
        return
    if await reply_if_submitted(chat_message):
        return
    await reply_menu_refreshed(chat_message)


@router.message(StateFilter(None), F.chat.type == "private")
async def catch_silent_nontext_message(message: types.Message) -> None:
    """Текстовые сообщения сюда не доходят вовсе — забирает `reg_handoff_idle_fallback`
    (см. докстринг модуля). Остаётся нетекст: документ/фото и т.п."""
    await reply_idle(message)


@router.callback_query(StateFilter(None), F.message.chat.type == "private")
async def catch_stale_callback(callback: types.CallbackQuery) -> None:
    if callback.message is not None:
        await offer_if_resumable(callback.message)
    # Гасим "часики" на кнопке в любом случае -- иначе тап по мёртвой inline-кнопке анкеты
    # (даже когда живого черновика уже нет) навсегда виснет в состоянии загрузки у делегата.
    await callback.answer()
