"""Форум-ночь п.8 (идея №19 бэклога чек-ина): делегатская сторона «🆘 SOS» — категория
(кнопки) -> текст/фото (можно пропустить) -> геопозиция (можно пропустить) -> карточка в чат
оргов + подтверждение делегату.

Форма шва — та же, что у соседних делегатских экранов (FAQ/программа/чек-ин): своего
`Router()` нет, `from handlers.user_actions import router`; импортирован ХВОСТОМ
`handlers/user_actions.py`. Домен (категории, карточка, привязка чата, эскалация) целиком в
`services/sos.py` — здесь только FSM-шаги и точки отправки. Тексты — через `reg_i18n.say`
(тот же перевод делегатского чата, что остальные экраны); литералы этого модуля
зарегистрированы в `services/i18n_sources.py::code_literals()` (сторож
`tests/test_i18n_literal_corpus_guard_260906.py`, SCANNED_FILES дополнен этим модулем).

Анти-спам (пункт 1 плана «не чаще одного открытого SOS на делегата») — `get_open_sos_report`
проверяется ДВАЖДЫ: на входе (кнопка «🆘 SOS») и повторно при выборе категории (гонка: два тапа
подряд, пока первая заявка ещё не создана строкой). Полной атомарности здесь нет (в отличие от
`claim_sos_report`) — цена гонки мала (одна лишняя открытая заявка на очень редком стечении
таймингов), а неявная сериализация каждого делегатского сообщения через одно FSM-состояние уже
снимает подавляющее большинство случаев."""
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from cities import default_city_code, get_setting_typed_for_city
from database.db import create_sos_report, get_open_sos_report
from handlers import reg_i18n
from handlers.states import SosReport
from handlers.user_actions import _delegate_city, ensure_registered, router
from keyboards.builders import MENU_TEXTS, get_main_menu_kb
from services import sos as sos_service
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_SKIP_TEXT = "Пропустить"
_CANCEL_TEXT = "Отмена"
_LOCATION_BUTTON_TEXT = "📍 Отправить геопозицию"


async def _resolve_city(telegram_id: int) -> str | None:
    """SOS привязывается к чату КОНКРЕТНОГО города — «нет города» не бывает (в отличие от
    вопроса делегата, у которого фан-аут глобальный), тот же приём, что
    `handlers.program._resolve_delegate_city`."""
    code = await _delegate_city(telegram_id)
    return code or default_city_code()


def _category_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=sos_service.CATEGORY_LABELS[code], callback_data=f"sos_cat:{code}")]
        for code in sos_service.CATEGORY_ORDER
    ])


def _skip_cancel_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=_SKIP_TEXT)
    kb.button(text=_CANCEL_TEXT)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def _location_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=_LOCATION_BUTTON_TEXT, request_location=True)
    kb.button(text=_SKIP_TEXT)
    kb.button(text=_CANCEL_TEXT)
    kb.adjust(1, 2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


async def _cancel(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await reg_i18n.say(
        message, "Действие отменено.",
        reply_markup=await get_main_menu_kb(message.from_user.id),
    )


# ── Ревью 24.09 (находка 3): «свежий» открытый SOS того же делегата (младше
# `sos_reopen_window_minutes`) больше НЕ блокирует наглухо — предлагает дополнить существующую
# заявку (следующее сообщение уйдёт в её тред, см. `SosReport.followup` ниже). «Старый» —
# новый SOS разрешён (прежний остаётся открытым, в карточке нового — честная ссылка на него,
# `services.sos.render_card_text`/`database.db.create_sos_report(prior_open_report_id=...)`).

async def _reopen_window_minutes(city: str | None) -> float:
    raw = await get_setting_typed_for_city("sos_reopen_window_minutes", city)
    try:
        return float(raw) if raw else sos_service.DEFAULT_REOPEN_WINDOW_MINUTES
    except (TypeError, ValueError):
        return sos_service.DEFAULT_REOPEN_WINDOW_MINUTES


async def _is_recent_open_report(report: dict, city: str | None) -> bool:
    age = sos_service.report_age_minutes(report)
    if age is None:
        return False  # штамп не распарсился -> не блокируем повторно, ведём себя как «старый»
    return age < await _reopen_window_minutes(city)


async def _recent_followup_text(report: dict) -> str:
    # Плейсхолдер {claim_status} подставляется ДО перевода (шаблон с плейсхолдером — тот же
    # известный неполный перевод, что `recall_generic_prompt_text`, handlers/registration.py:836)
    # — .format здесь, не reg_i18n.tr_text поверх готового текста.
    raw = await get_setting_typed("sos_recent_followup_text")
    return raw.format(claim_status=sos_service.claim_status_label(report))


async def _offer_followup(message: types.Message, state: FSMContext, report: dict) -> None:
    await state.set_state(SosReport.followup)
    await state.update_data(sos_followup_report_id=report["id"])
    await reg_i18n.say(message, await _recent_followup_text(report))


# 🆘 SOS — кнопка главного меню
@router.message(F.text.in_(MENU_TEXTS["menu_sos"]))
async def sos_start(message: types.Message, state: FSMContext):
    if not await ensure_registered(message):
        return
    open_report = await get_open_sos_report(message.from_user.id)
    prior_open_id = None
    if open_report is not None:
        city_for_window = await _resolve_city(message.from_user.id)
        if await _is_recent_open_report(open_report, city_for_window):
            await _offer_followup(message, state, open_report)
            return
        prior_open_id = open_report["id"]
    logger.info(f"User {message.from_user.id} opened SOS")
    await state.update_data(sos_prior_open_id=prior_open_id)
    await reg_i18n.say(
        message, await get_setting_typed("sos_category_prompt_text"),
        reply_markup=_category_kb(),
    )


@router.callback_query(F.data.startswith("sos_cat:"))
async def sos_pick_category(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split(":", 1)[1]
    if category not in sos_service.CATEGORY_ORDER:
        await callback.answer()
        return
    city = await _resolve_city(callback.from_user.id)
    # Повторный гейт анти-спама — см. докстринг модуля (гонка «кнопка -> категория»).
    open_report = await get_open_sos_report(callback.from_user.id)
    prior_open_id = None
    if open_report is not None:
        if await _is_recent_open_report(open_report, city):
            await callback.answer()
            await state.set_state(SosReport.followup)
            await state.update_data(sos_followup_report_id=open_report["id"])
            try:
                await callback.message.edit_text(await _recent_followup_text(open_report))
            except Exception:
                pass
            return
        prior_open_id = open_report["id"]

    await state.update_data(sos_category=category, sos_city=city, sos_prior_open_id=prior_open_id)
    await state.set_state(SosReport.details)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await reg_i18n.say(
        callback.message, await get_setting_typed("sos_details_prompt_text"),
        reply_markup=_skip_cancel_kb(),
    )


@router.message(SosReport.details, F.text == _CANCEL_TEXT)
async def sos_details_cancel(message: types.Message, state: FSMContext):
    await _cancel(message, state)


@router.message(SosReport.details, F.text == _SKIP_TEXT)
async def sos_details_skip(message: types.Message, state: FSMContext):
    await state.update_data(sos_details_text=None, sos_details_photo=None)
    await _advance_to_location(message, state)


@router.message(SosReport.details)
async def sos_details_step(message: types.Message, state: FSMContext):
    text = message.text or message.caption
    photo_id = message.photo[-1].file_id if message.photo else None
    if not text and not photo_id:
        await reg_i18n.say(
            message, f"Не понял, пришли текст, фото или нажми «{_SKIP_TEXT}».",
        )
        return
    await state.update_data(sos_details_text=text, sos_details_photo=photo_id)
    await _advance_to_location(message, state)


async def _advance_to_location(message: types.Message, state: FSMContext) -> None:
    await state.set_state(SosReport.location)
    await reg_i18n.say(
        message, await get_setting_typed("sos_location_prompt_text"),
        reply_markup=_location_kb(),
    )


@router.message(SosReport.location, F.text == _CANCEL_TEXT)
async def sos_location_cancel(message: types.Message, state: FSMContext):
    await _cancel(message, state)


@router.message(SosReport.location, F.text == _SKIP_TEXT)
async def sos_location_skip(message: types.Message, state: FSMContext):
    await _finalize_sos(message, state, latitude=None, longitude=None)


@router.message(SosReport.location, F.location)
async def sos_location_step(message: types.Message, state: FSMContext):
    await _finalize_sos(
        message, state,
        latitude=message.location.latitude, longitude=message.location.longitude,
    )


@router.message(SosReport.location)
async def sos_location_invalid(message: types.Message, state: FSMContext):
    await reg_i18n.say(
        message, f"Не понял, пришли геопозицию (кнопкой) или нажми «{_SKIP_TEXT}».",
    )


async def _finalize_sos(message: types.Message, state: FSMContext, *,
                         latitude: float | None, longitude: float | None) -> None:
    data = await state.get_data()
    await state.clear()
    category = data.get("sos_category")
    city = data.get("sos_city")

    report_id = await create_sos_report(
        message.from_user.id, city, category,
        data.get("sos_details_text"), data.get("sos_details_photo"),
        latitude, longitude,
        prior_open_report_id=data.get("sos_prior_open_id"),
    )
    logger.info(f"User {message.from_user.id} created SOS #{report_id} (category={category})")

    # Ревью 24.09 (находка 1): раньше результат `post_card` игнорировался целиком — делегат
    # слышал «Оргкомитет получил» даже когда карточка не дошла НИКУДА (чат упал, фоллбэк-веер
    # разошёлся нулю получателей). `PostCardResult.delivered_total` — реальное число доставок;
    # `record_delivery_outcome` штампует/снимает `sos_reports.delivery_failed_at` по факту.
    try:
        result = await sos_service.post_card(message.bot, report_id)
    except Exception as e:
        logger.error(f"sos._finalize_sos: post_card({report_id}) failed: {e}")
        result = sos_service.PostCardResult()
    await sos_service.record_delivery_outcome(report_id, result)

    try:
        minutes_raw = await get_setting_typed_for_city("sos_escalation_minutes", city)
        minutes = int(minutes_raw) if minutes_raw else sos_service.DEFAULT_ESCALATION_MINUTES
    except (TypeError, ValueError):
        minutes = sos_service.DEFAULT_ESCALATION_MINUTES
    sos_service.schedule_escalation(report_id, minutes)

    if result.delivered_total > 0:
        await reg_i18n.say(
            message, await get_setting_typed("sos_sent_text"),
            reply_markup=await get_main_menu_kb(message.from_user.id),
        )
        return

    # Ни в чат, ни фоллбэком в личку — делегат не должен уйти с пустым «получили». Одна
    # повторная попытка доставки через минуту (джоба перечитывает статус).
    logger.error(f"sos._finalize_sos: SOS #{report_id} не доставлен НИКОМУ, ставлю повтор")
    sos_service.schedule_delivery_retry(report_id)
    await reg_i18n.say(
        message, await get_setting_typed("sos_delivery_failed_text"),
        reply_markup=await get_main_menu_kb(message.from_user.id),
    )
    # Контакт (телефон и т.п.) — сырое значение, НЕ переводится (тот же приём, что
    # contact_person/contact_vk/contact_tg в handlers/user_actions.py::show_contacts),
    # отдельным сообщением, чтобы не портить сопоставление корпуса перевода выше.
    contact = await get_setting_typed_for_city("sos_fallback_contact_text", city)
    if contact:
        await message.answer(contact)


# ── Ответ делегата на «Ответ по SOS» — снова в тред карточки (пункт 3 плана) ────────────────
#
# Маркер — ТОЛЬКО "🆘" (без "🆔"): это личное сообщение делегату (handlers/admin_sos.py::
# admin_reply_to_sos шлёт «🆘 Ответ по SOS #<id>:»), его telegram_id тут ни при чём — в отличие
# от карточки В ЧАТЕ ОРГОВ (services/sos.py::render_card_text), где "🆔"+"🆘" вместе отличают
# реплай орга от произвольного ответа в группе.

def _is_sos_followup(message: types.Message) -> bool:
    replied = message.reply_to_message
    if not replied or not replied.text:
        return False
    return "🆘" in replied.text and "SOS #" in replied.text


async def _relay_report_followup(message: types.Message, report: dict) -> None:
    """Хвост обеих веток дополнения открытого SOS: реплай на карточку (`sos_delegate_followup`
    ниже) И повторное «🆘 SOS» на СВЕЖУЮ заявку (`SosReport.followup`, ревью 24.09 находка 3) —
    один и тот же приём доставки, разный триггер."""
    if report.get("chat_id") and report.get("card_message_id"):
        try:
            await message.copy_to(
                report["chat_id"], reply_to_message_id=report["card_message_id"],
            )
            return
        except Exception as e:
            logger.warning(
                f"_relay_report_followup: не удалось отправить в чат id={report['chat_id']}: {e}",
            )
    # Фоллбэк без треда (чат не привязан или доставка в него упала) — известное ограничение
    # (services/sos.py::post_card docstring): личный веер держателям moderate_reg города, без
    # общего треда карточки. Копия сообщения делегата уходит КАЖДОМУ получателю отдельным
    # вызовом (copy_message не умеет broadcast) — тот же приём, что services.sos._fallback_fanout.
    from config import config
    from handlers.admin_caps import capability_holders

    recipients = await capability_holders("moderate_reg", city=report.get("city"))
    if not recipients:
        recipients = list(config.ADMIN_IDS)
    prefix = f"💬 Делегат дополнил SOS #{report['id']}:"
    for uid in recipients:
        try:
            await message.bot.send_message(uid, prefix)
            await message.copy_to(uid)
        except Exception as e:
            logger.info(f"_relay_report_followup: не удалось написать id={uid}: {e}")


@router.message(_is_sos_followup)
async def sos_delegate_followup(message: types.Message):
    import re

    from database.db import get_sos_report

    match = re.search(r"SOS #([0-9]+)", message.reply_to_message.text)
    if not match:
        return
    report = await get_sos_report(int(match.group(1)))
    if report is None or report.get("telegram_id") != message.from_user.id:
        return  # чужая карточка/устаревшая ссылка — тихо, ничего не пересылаем не по адресу
    await _relay_report_followup(message, report)


# Ревью 24.09 (находка 3): «свежий» открытый SOS — повторное «🆘 SOS» ставит это состояние
# (`_offer_followup` выше), следующее ЛЮБОЕ сообщение делегата (не обязательно реплай) уходит
# дополнением к прежней заявке, ОДИН раз — состояние снимается сразу после.
@router.message(SosReport.followup)
async def sos_followup_step(message: types.Message, state: FSMContext):
    from database.db import get_sos_report

    data = await state.get_data()
    await state.clear()
    report_id = data.get("sos_followup_report_id")
    report = await get_sos_report(report_id) if report_id else None
    if report is None:
        return
    await _relay_report_followup(message, report)
