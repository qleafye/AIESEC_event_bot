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


# 🆘 SOS — кнопка главного меню
@router.message(F.text.in_(MENU_TEXTS["menu_sos"]))
async def sos_start(message: types.Message, state: FSMContext):
    if not await ensure_registered(message):
        return
    if await get_open_sos_report(message.from_user.id) is not None:
        await reg_i18n.say(message, await get_setting_typed("sos_already_open_text"))
        return
    logger.info(f"User {message.from_user.id} opened SOS")
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
    # Повторный гейт анти-спама — см. докстринг модуля (гонка «кнопка -> категория»).
    if await get_open_sos_report(callback.from_user.id) is not None:
        await callback.answer()
        lang, tr_map = await reg_i18n.ctx_for(callback)
        text = reg_i18n.tr_text(await get_setting_typed("sos_already_open_text"), lang, tr_map)
        try:
            await callback.message.edit_text(text)
        except Exception:
            pass
        return

    city = await _resolve_city(callback.from_user.id)
    await state.update_data(sos_category=category, sos_city=city)
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
    )
    logger.info(f"User {message.from_user.id} created SOS #{report_id} (category={category})")

    try:
        await sos_service.post_card(message.bot, report_id)
    except Exception as e:
        logger.error(f"sos._finalize_sos: post_card({report_id}) failed: {e}")

    try:
        minutes_raw = await get_setting_typed_for_city("sos_escalation_minutes", city)
        minutes = int(minutes_raw) if minutes_raw else sos_service.DEFAULT_ESCALATION_MINUTES
    except (TypeError, ValueError):
        minutes = sos_service.DEFAULT_ESCALATION_MINUTES
    sos_service.schedule_escalation(report_id, minutes)

    await reg_i18n.say(
        message, await get_setting_typed("sos_sent_text"),
        reply_markup=await get_main_menu_kb(message.from_user.id),
    )


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

    if report.get("chat_id") and report.get("card_message_id"):
        try:
            await message.copy_to(
                report["chat_id"], reply_to_message_id=report["card_message_id"],
            )
            return
        except Exception as e:
            logger.warning(
                f"sos_delegate_followup: не удалось отправить в чат id={report['chat_id']}: {e}",
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
            logger.info(f"sos_delegate_followup: не удалось написать id={uid}: {e}")
