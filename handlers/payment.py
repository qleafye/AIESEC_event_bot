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

from config import config
from database.db import get_setting, get_user, update_payment_status, set_payment_due
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


def _parse_lc_requisites(raw: str) -> dict[str, str]:
    """Parse per-LC requisites ('ЛК | реквизиты' per line) → {lc_lower: requisites}.
    LC name is matched case-insensitively against the user's local_committee."""
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        lc, req = line.split("|", 1)
        lc = lc.strip().lower()
        req = req.strip()
        if lc and req:
            out[lc] = req
    return out


async def _resolve_requisites(telegram_id: int) -> str | None:
    """Per-LC requisites for this user's local_committee, else the global payment_requisites.
    Each ЛК collects to its own card (payment_requisites_by_lc); missing/unmatched LC falls
    back to the single shared card."""
    by_lc = _parse_lc_requisites(await get_setting("payment_requisites_by_lc") or "")
    if by_lc:
        user = await get_user(telegram_id)
        lc = (user or {}).get("local_committee")
        if lc:
            hit = by_lc.get(str(lc).strip().lower())
            if hit:
                return hit
    return await get_setting("payment_requisites")


def _format_requisites_block(requisites: str | None) -> str:
    """WR-05: single source of truth for the requisites block — resolve/escape/format was
    duplicated across 3 call sites (picker, defer, details), risking CR-01-class drift.
    admin-entered requisites often contain & or < (e.g. "Сбербанк & Тинькофф"); without
    escaping, parse_mode=HTML rejects the whole message. Returns "" when there's nothing to pay."""
    if not requisites or not requisites.strip():
        return ""
    return f"📋 Реквизиты:\n{html.escape(requisites)}"


# Inline "pay later" escape shown on the option picker and the requisites message.
_PAY_LATER_BTN = InlineKeyboardButton(text="⏭ Оплачу позже", callback_data="pay_later")


async def should_offer_receipt_upload(telegram_id: int) -> bool:
    """True when the user still owes a receipt: payment module on, status in
    {not_paid, overdue}, and there are real requisites to pay to. Drives the
    persistent '💳 Оплата' menu button so upload works any time — gated on
    the DB status, not the MemoryStorage FSM, so it survives a bot restart. Free /
    no-requisites participants (nothing to pay) never trip it."""
    if (await get_setting("payment_enabled") or "off") != "on":
        return False
    user = await get_user(telegram_id)
    if not user:
        return False
    if (user.get("payment_status") or "not_paid") not in ("not_paid", "overdue"):
        return False
    requisites = await _resolve_requisites(telegram_id)
    return bool(requisites and requisites.strip())


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


async def _schedule_deadline_reminders(telegram_id: int):
    """Schedule the T-3 / T-1 payment-deadline reminders for a user who OWES payment.
    Called at payment entry (option picked / defer), NOT at receipt upload — a user who
    tapped «Оплачу позже» (status not_paid) is exactly who must be reminded. Idempotent:
    scheduler.schedule_payment_reminder uses replace_existing, and send_payment_reminder
    self-guards on paid/receipt_sent so a user who pays before it fires is never pinged."""
    deadline_str = await get_setting("payment_deadline")
    if not deadline_str:
        return
    try:
        from datetime import datetime, timedelta
        from services.scheduler import schedule_payment_reminder
        deadline = datetime.strptime(deadline_str.strip(), "%d.%m.%Y %H:%M")
        # WR-03: persist payment_due so a user who defers straight from the multi-option picker
        # (never picking an option → payment_option stays NULL) is still caught by the overdue
        # sweep, which gates on (payment_option IS NOT NULL OR payment_due IS NOT NULL).
        try:
            await set_payment_due(telegram_id, deadline_str.strip())
        except Exception as e:
            logger.error(f"Failed to persist payment_due for {telegram_id}: {e}")
        now = datetime.now()
        minus3d = deadline - timedelta(days=3)
        minus1d = deadline - timedelta(days=1)
        if minus3d > now:
            schedule_payment_reminder(telegram_id, minus3d, "minus3d")
        if minus1d > now:
            schedule_payment_reminder(telegram_id, minus1d, "minus1d")
    except Exception as e:
        logger.error(f"Failed to schedule payment reminders for {telegram_id}: {e}")


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
            ] + [[_PAY_LATER_BTN]])
            text = "💳 Выбери вариант участия:"
            block = _format_requisites_block(await _resolve_requisites(telegram_id))
            if block:
                text += f"\n\n{block}"
            await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=kb)
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
    # WR-01: fail-soft like start_payment_step, and guarantee callback.answer() via finally so
    # a mid-flow error never leaves the tapped button spinning.
    try:
        await update_payment_status(callback.from_user.id, "not_paid", payment_option=label)
        await _show_payment_details(callback.bot, callback.from_user.id, state, label, price)
    except Exception as e:
        logger.error(f"process_payment_option failed for {callback.from_user.id}: {e}")
    finally:
        await callback.answer()


@router.callback_query(F.data == "pay_later")
async def process_pay_later(callback: types.CallbackQuery, state: FSMContext):
    """Defer payment: clear the receipt-upload state, land on the main menu. The
    '💳 Оплата' menu button (gated on DB status) lets the user upload later."""
    # WR-01: fail-soft + guaranteed callback.answer() (finally) — any DB/Telegram hiccup here
    # must not leave the button spinning with the user stranded off the menu.
    try:
        await state.clear()
        # Deferring is exactly when reminders matter — schedule T-3/T-1 so a forgetful
        # not_paid user still gets pinged (the whole point of this change).
        await _schedule_deadline_reminders(callback.from_user.id)
        parts = ["Ок! Оплатишь позже."]
        block = _format_requisites_block(await _resolve_requisites(callback.from_user.id))
        if block:
            parts.append(block)
        parts.append("Кнопка «💳 Оплата» будет в меню, пока чек не отправлен.")
        await callback.message.answer(
            "\n\n".join(parts),
            parse_mode="HTML",
            reply_markup=await get_main_menu_kb(callback.from_user.id),
        )
    except Exception as e:
        logger.error(f"process_pay_later failed for {callback.from_user.id}: {e}")
    finally:
        await callback.answer()


async def _show_payment_details(bot: Bot, telegram_id: int, state: FSMContext, option_label: str, option_price: int):
    requisites = await _resolve_requisites(telegram_id)  # per-LC card, else shared
    deadline = await get_setting("payment_deadline")
    penalties = await get_setting("penalty_schedule")

    # WR-05: free participation (price 0 and no bank requisites) must not be forced
    # through a receipt-upload gate. Land the user on the completion text + menu instead.
    if option_price == 0 and not (requisites and requisites.strip()):
        from handlers.registration import send_completion_and_bonus
        await send_completion_and_bonus(bot, telegram_id)
        await state.clear()
        return

    parts = [
        "💰 <b>Оплата участия</b>\n",
        f"Вариант: {html.escape(option_label)}",
        f"Сумма: {option_price} ₽\n",
    ]
    block = _format_requisites_block(requisites)  # WR-05: shared resolve/escape/format
    if block:
        parts.append(block + "\n")
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
    await bot.send_message(
        telegram_id, "\n".join(parts), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_PAY_LATER_BTN]]),
    )
    # User now owes payment — schedule deadline reminders up front (not at receipt upload).
    await _schedule_deadline_reminders(telegram_id)


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


# ~F.text.startswith("/") lets commands (/start, /cancel) fall THROUGH this catch-all.
# payment.router is included before registration.router, so without this exclusion the
# catch-all swallowed /start and stranded the user in the payment window with no escape.
@router.message(Registration.receipt_upload, ~F.text.startswith("/"))
async def process_receipt_invalid(message: types.Message, state: FSMContext):
    await message.answer(
        "❌ Отправь чек оплаты (PDF-документ или фото).\n"
        "Или /start — вернуться в меню (загрузить чек можно будет позже)."
    )


async def _finalize_receipt(message: types.Message, state: FSMContext, file_id: str):
    telegram_id = message.from_user.id
    await update_payment_status(telegram_id, "receipt_sent", receipt_file_id=file_id)
    logger.info(f"user={telegram_id} action=receipt_uploaded")

    # Ping admins so the receipt doesn't sit unseen in the «🧾 Чеки» queue. Fail-soft:
    # a notify error must never break the user's receipt confirmation below.
    try:
        user = await get_user(telegram_id)
        name = html.escape(str((user or {}).get("full_name") or telegram_id))
        note = f"🧾 <b>Новый чек оплаты</b> от {name}.\nПроверь: /admin → 🧾 Чеки"
        for admin_id in config.ADMIN_IDS:
            try:
                await message.bot.send_message(admin_id, note, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id} of receipt from {telegram_id}: {e}")
    except Exception as e:
        logger.error(f"Receipt admin-notify failed for {telegram_id}: {e}")

    # PAY-06: deadline reminders (T-3/T-1) are scheduled at payment ENTRY now
    # (_show_payment_details / process_pay_later), not here — by the time a receipt
    # is uploaded the status is 'receipt_sent' and send_payment_reminder self-guards
    # would suppress them anyway. cancel_payment_reminders runs when admin marks paid.

    await state.clear()
    await message.answer(
        "✅ Чек получен! Менеджер проверит его в ближайшее время.", parse_mode="HTML"
    )
