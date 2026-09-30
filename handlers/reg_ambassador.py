"""Phase 28 (28-06, SU-07, СкиллАп 5): финальный экран — предложение получить свою реф-ссылку
после успешной анкеты.

Своего `Router` НЕТ — импортирует и декорирует напрямую `router`, определённый в
`handlers/registration.py` (13-02 приём). Импортируется В ХВОСТЕ `registration.py`, ПОСЛЕ
`reg_resume_fork` — обработчики этого шва (`regamb:want`/`regamb:later`) регистрируются
последними, золотой снимок порядка (`tests/test_refac_snapshot_260816.py`) только дополняется.

OQ-1 (CONTEXT, не пересматривается): предложение — ВТОРОЕ сообщение после «поздравляем»
(reply-клавиатура и инлайн не совместимы на одном сообщении), ссылка после тапа «Хочу свою
ссылку» — ТРЕТЬЕ сообщение, `parse_mode=None`, ТОЛЬКО URL, без единого слова вокруг —
единственный «сырой текст без обёртки» в проекте, новый ключ реестра для него сознательно не
заводится (28-UI-SPEC.md Copywriting Contract).

OQ-2/OQ-3 (не пересматриваются): `is_ambassador` — отдельная колонка от
`is_ambassador_candidate` («стал амбассадором кнопкой после анкеты» vs «ответил "да" на
вопрос-шаг анкеты внутри неё» — разная семантика, разное время простановки, ни одна не
перетирает другую). `ref_code` = `telegram_id`, отдельной колонки в БД нет — ссылка строится
как `https://t.me/<bot>?start=amb_<telegram_id>`.

Имя бота резолвится тем же фолбэк-приёмом, что `services/scheduler.py::_nudge_keyboard` —
`get_me()` не смог -> предложение/ссылка молча не показываются (мёртвой кнопки быть не
должно), исключение наружу не улетает."""
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_user
from cities import get_setting_typed_for_city
from settings_schema import get_setting_typed
from handlers.registration import router
from handlers import reg_i18n
from reg_engine import build_referral_link  # решение владельца 17.09: один формат amb_<id> везде
from services import amb_status

_ALERT_MAX = 200  # потолок текста всплывающего алерта Telegram

logger = logging.getLogger(__name__)


async def _bot_username(bot) -> str | None:
    try:
        me = await bot.get_me()
        return me.username
    except Exception as e:
        logger.warning(f"reg_ambassador: get_me failed, offer/link omitted: {e}")
        return None


async def offer_ref_link(message: types.Message, telegram_id: int, event_city: str | None = None) -> None:
    """Второе сообщение после «поздравляем» (OQ-1) — только при включённом тумблере
    `reg_offer_ref_link` (дефолт off, D-06) и резолвящемся имени бота. Вызывающая сторона
    (`finalize_registration`) оборачивает вызов в try/except — сбой предложения не должен
    ронять уже сохранённую заявку; здесь дополнительно свой собственный fail-soft на get_me(),
    чтобы функция не поднимала исключение вообще ни при каких обстоятельствах."""
    try:
        # Правила входа — services.amb_status. Кандидату (режим отбора, ответил «да» в анкете)
        # подтверждение приходит независимо от тумблера предложения ссылки; при набранном
        # лимите и отказанному предлагать нечего. Сбой чтения — прежнее предложение.
        try:
            state = await amb_status.delegate_state(telegram_id)
        except Exception as e:
            logger.error(f"offer_ref_link: delegate_state failed for {telegram_id}: {e}")
            state = "open"
        if state == "candidate":
            ack = await get_setting_typed("amb_candidate_ack_text")
            if ack:
                await reg_i18n.say(message, ack)
            return
        if state in ("full", "declined"):
            return
        if await get_setting_typed("reg_offer_ref_link") != "on":
            return
        bot_username = await _bot_username(message.bot)
        if not bot_username:
            return
        heading = await get_setting_typed_for_city(
            "miniapp_form_ambassador_offer_heading_text", event_city,
        )
        body = await get_setting_typed_for_city(
            "miniapp_form_ambassador_offer_body_text", event_city,
        )
        cta = await get_setting_typed("miniapp_form_ambassador_cta_text")
        later = await get_setting_typed("miniapp_form_ambassador_later_text")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=cta, callback_data="regamb:want"),
            InlineKeyboardButton(text=later, callback_data="regamb:later"),
        ]])
        text = f"<b>{heading}</b>\n{body}"
        await reg_i18n.say(message, text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"offer_ref_link failed for {telegram_id}: {e}")


def _alert_text(text: str) -> str:
    """Алерт колбэка длиннее 200 символов Telegram не покажет — обрезаем с многоточием."""
    text = text or ""
    return text if len(text) <= _ALERT_MAX else text[:_ALERT_MAX - 1].rstrip() + "…"


async def _hide_keyboard(callback: types.CallbackQuery) -> None:
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _send_link_and_note(message, bot, uid: int) -> None:
    """Голая ссылка (`parse_mode=None`, без i18n-обёртки — переводить в URL нечего) и следом
    пояснение, где её найти потом (приёмка 17.09, п.2); `{section}` — подпись постоянного места
    реф-ссылки в приложении (`miniapp_hub_referral_label_text`)."""
    bot_username = await _bot_username(bot)
    if not bot_username:
        return
    link = build_referral_link(bot_username, uid)
    await message.answer(link, parse_mode=None)
    user = await get_user(uid)
    event_city = user.get("event_city") if user else None
    note_tpl = await get_setting_typed_for_city("miniapp_form_ambassador_link_note_text", event_city)
    if note_tpl:
        section_label = await get_setting_typed("miniapp_hub_referral_label_text")
        note = note_tpl.replace("{section}", section_label or "")
        await reg_i18n.say(message, note)


@router.callback_query(F.data == "regamb:want")
async def regamb_want(callback: types.CallbackQuery):
    """«Хочу свою ссылку». Вход в команду идёт через `services.amb_status.request_join` (там же
    лимит мест, режим отбора, отказанные и ступени): вступил — ссылка и пояснение; стал
    кандидатом — подтверждение, ссылка и пояснение; мест нет или отказано — короткий алерт,
    в БД ничего не пишется. `uid` — только из `callback.from_user`, устаревшая кнопка
    перепроверяется в `request_join`."""
    uid = callback.from_user.id
    result = await amb_status.request_join(uid, source="button_bot")
    outcome = result.outcome
    if outcome in ("full", "declined"):
        text = await reg_i18n.tr_for(callback, await get_setting_typed("amb_slots_full_text") or "")
        await callback.answer(_alert_text(text), show_alert=True)
        await _hide_keyboard(callback)
        if len(text) > _ALERT_MAX:
            await reg_i18n.say(callback.message, text)
        return
    await callback.answer()
    await _hide_keyboard(callback)
    if outcome == "no_user":
        return
    if outcome in ("candidate", "already_candidate"):
        ack = await get_setting_typed("amb_candidate_ack_text")
        if ack:
            await reg_i18n.say(callback.message, ack)
    await _send_link_and_note(callback.message, callback.bot, uid)


@router.callback_query(F.data == "regamb:later")
async def regamb_later(callback: types.CallbackQuery):
    """«Позже» — просто гасит инлайн-клавиатуру, никаких записей в БД и новых сообщений."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
