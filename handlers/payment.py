"""Phase 4 (PAY-02/03/05): user-facing payment flow.

Triggered from approve_user() in registration.py when payment_enabled=on. Flow:
option selection (if >1 paid option) → payment details (requisites/deadline/penalties)
→ receipt upload (PDF document OR photo). Receipt is stored as a Telegram file_id only
(never downloaded — D-11). All modules fail-safe OFF; when payment_enabled is off this
file's handlers never fire.
"""
import html
import logging

from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.db import get_setting, get_user, update_payment_status
from handlers.states import Registration
from keyboards.builders import get_main_menu_kb

router = Router()
logger = logging.getLogger(__name__)

# Set once at startup (main.py → init_payment_module(dp.storage)). Lets start_payment_step
# build an out-of-handler FSMContext for the free/single-option path, where approve_user()
# has no FSMContext of its own.
_storage = None


def init_payment_module(storage):
    global _storage
    _storage = storage


def _parse_options(raw: str) -> list[tuple[str, int]]:
    """Parse the payment_options setting ('label|price' per line) → [(label, price)]."""
    options: list[tuple[str, int]] = []
    for line in (raw or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            label, price_raw = line.split("|", 1)
            label = label.strip() or "Участие"
            try:
                price = int(price_raw.strip())
            except ValueError:
                price = 0
        else:
            label, price = line, 0
        options.append((label, price))
    return options


async def start_payment_step(bot: Bot, telegram_id: int):
    """Entry point called from approve_user() when payment_enabled=on. Fail-soft."""
    try:
        options = _parse_options(await get_setting("payment_options") or "")
        paid = [o for o in options if o[1] > 0]
        if len(options) > 1 and paid:
            # Multi-option path: let the user pick. State is set later by _show_payment_details.
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{label} — {price} ₽", callback_data=f"pay_option:{i}")]
                for i, (label, price) in enumerate(options)
            ])
            await bot.send_message(telegram_id, "💳 Выбери вариант участия:", reply_markup=kb)
            return
        # Single / free path: skip selection, go straight to details.
        label, price = options[0] if options else ("Участие", 0)
        key = StorageKey(bot_id=bot.id, chat_id=telegram_id, user_id=telegram_id)
        ctx = FSMContext(storage=_storage, key=key)
        await _show_payment_details(bot, telegram_id, ctx, label, price)
    except Exception as e:
        logger.error(f"Failed to start payment step for {telegram_id}: {e}")
        # CR-01: never strand an approved user. If details failed to send (e.g. a
        # transient Telegram error), clear any half-set receipt_upload state and deliver
        # the completion text + menu so they land on the main menu, not a dead end.
        try:
            key = StorageKey(bot_id=bot.id, chat_id=telegram_id, user_id=telegram_id)
            await FSMContext(storage=_storage, key=key).clear()
            from handlers.registration import send_completion_and_bonus
            await send_completion_and_bonus(bot, telegram_id)
        except Exception as e2:
            logger.error(f"Failed fallback completion for {telegram_id}: {e2}")


@router.callback_query(F.data.startswith("pay_option:"))
async def process_payment_option(callback: types.CallbackQuery, state: FSMContext):
    # NO Registration.payment_option state filter — start_payment_step runs without an
    # FSMContext, so that state is never set; a filter here would silently swallow the tap.
    options = _parse_options(await get_setting("payment_options") or "")
    try:
        idx = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный вариант.", show_alert=True)
        return
    if not (0 <= idx < len(options)):
        await callback.answer("Вариант больше не доступен.", show_alert=True)
        return
    label, price = options[idx]
    await update_payment_status(callback.from_user.id, "not_paid", payment_option=label)
    await _show_payment_details(callback.bot, callback.from_user.id, state, label, price)
    await callback.answer()


async def _show_payment_details(bot: Bot, telegram_id: int, state: FSMContext, option_label: str, option_price: int):
    requisites = await get_setting("payment_requisites")
    deadline = await get_setting("payment_deadline")
    penalties = await get_setting("penalty_schedule")

    parts = [
        "💰 <b>Оплата участия</b>\n",
        f"Вариант: {html.escape(option_label)}",
        f"Сумма: {option_price} ₽\n",
    ]
    if requisites:
        # CR-01: admin-entered requisites often contain & or < (e.g. "Сбербанк & Тинькофф");
        # without escaping, parse_mode=HTML rejects the whole message.
        parts.append(f"📋 Реквизиты:\n{html.escape(requisites)}\n")
    if deadline:
        parts.append(f"📅 Дедлайн: {html.escape(deadline)}\n")
    if penalties and penalties.strip():
        lines = []
        for line in penalties.strip().splitlines():
            if "|" in line:
                date_part, amount = line.split("|", 1)
                # CR-01: escape the admin-entered penalty fields too.
                lines.append(f"• до {html.escape(date_part.strip())} — остаток {html.escape(amount.strip())} ₽")
        if lines:
            parts.append("⚠️ Штрафы за отмену:\n" + "\n".join(lines) + "\n")
    parts.append("📎 Загрузи чек оплаты (PDF-документ или скриншот).")

    # CR-01: set state BEFORE the send so a transient send failure does not leave the
    # user approved-but-stateless. start_payment_step's except still delivers a fallback
    # menu for the single/free path; the multi-option path re-raises to the callback.
    await state.set_state(Registration.receipt_upload)
    await bot.send_message(telegram_id, "\n".join(parts), parse_mode="HTML")


@router.message(Registration.receipt_upload, F.document)
async def process_receipt_document(message: types.Message, state: FSMContext, bot: Bot):
    if message.document.mime_type != "application/pdf":
        await message.answer(
            "❌ Принимается только PDF-документ. Для скриншота используй функцию отправки фото."
        )
        return
    await _finalize_receipt(message, state, message.document.file_id)


@router.message(Registration.receipt_upload, F.photo)
async def process_receipt_photo(message: types.Message, state: FSMContext, bot: Bot):
    await _finalize_receipt(message, state, message.photo[-1].file_id)  # highest-res


@router.message(Registration.receipt_upload)
async def process_receipt_invalid(message: types.Message, state: FSMContext):
    await message.answer("❌ Отправь PDF-документ или скриншот оплаты (фото).")


async def _finalize_receipt(message: types.Message, state: FSMContext, file_id: str):
    telegram_id = message.from_user.id
    await update_payment_status(telegram_id, "receipt_sent", receipt_file_id=file_id)

    # PAY-06: schedule deadline reminders. send_payment_reminder self-guards on status,
    # so even though we're already in 'receipt_sent' here, scheduling is harmless. A
    # failure here must never break the receipt confirmation.
    deadline_str = await get_setting("payment_deadline")
    if deadline_str:
        try:
            from datetime import datetime, timedelta
            from services.scheduler import schedule_payment_reminder
            deadline = datetime.strptime(deadline_str.strip(), "%d.%m.%Y %H:%M")
            now = datetime.now()
            minus3d = deadline - timedelta(days=3)
            minus1d = deadline - timedelta(days=1)
            if minus3d > now:
                schedule_payment_reminder(telegram_id, minus3d, "minus3d")
            if minus1d > now:
                schedule_payment_reminder(telegram_id, minus1d, "minus1d")
        except Exception as e:
            logger.error(f"Failed to schedule payment reminders for {telegram_id}: {e}")

    await state.clear()
    await message.answer(
        "✅ Чек получен! Менеджер проверит его в ближайшее время.", parse_mode="HTML"
    )
