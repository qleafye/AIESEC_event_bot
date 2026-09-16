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

from database.db import get_user, update_user_answers
from cities import get_setting_typed_for_city
from settings_schema import get_setting_typed
from handlers.registration import router
from handlers import reg_i18n

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


@router.callback_query(F.data == "regamb:want")
async def regamb_want(callback: types.CallbackQuery):
    """«Хочу свою ссылку» — ставит `is_ambassador=1` узким UPDATE (`is_ambassador_candidate`
    НЕ трогается, OQ-2) и шлёт ГОЛУЮ ссылку третьим сообщением, `parse_mode=None`, без i18n-
    обёртки `reg_i18n.say` (переводить в URL нечего, а обёртка рискует что-то к нему
    приклеить).

    Приёмка 17.09 (п.1): следом — ЧЕТВЁРТОЕ сообщение, пояснение, где эту ссылку найти
    потом (делегат тапнул один раз и увидел голый URL без контекста). Это обычный текст, не
    сырой URL из OQ-1 — через `reg_i18n.say` (перевод, тот же приём, что у `offer_ref_link`)."""
    uid = callback.from_user.id
    await update_user_answers(uid, {"is_ambassador": 1}, allowed_columns=["is_ambassador"])
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    bot_username = await _bot_username(callback.bot)
    if not bot_username:
        return
    link = f"https://t.me/{bot_username}?start=amb_{uid}"
    await callback.message.answer(link, parse_mode=None)
    user = await get_user(uid)
    event_city = user.get("event_city") if user else None
    note = await get_setting_typed_for_city("miniapp_form_ambassador_link_note_text", event_city)
    if note:
        await reg_i18n.say(callback.message, note)


@router.callback_query(F.data == "regamb:later")
async def regamb_later(callback: types.CallbackQuery):
    """«Позже» — просто гасит инлайн-клавиатуру, никаких записей в БД и новых сообщений."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
