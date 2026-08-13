"""Phase 4 (PAY-02/03/05): user-facing payment flow.

Triggered from approve_user() in registration.py when payment_enabled=on. Flow:
option selection (if >1 paid option) → payment details (requisites/deadline/penalties)
→ receipt upload (PDF document OR photo). Receipt is stored as a Telegram file_id only
(never downloaded — D-11). All modules fail-safe OFF; when payment_enabled is off this
file's handlers never fire.
"""
import html
import logging
import time

from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.db import get_setting, get_user, update_payment_status, set_payment_due
from settings_schema import get_setting_typed  # REG-02 (06-06): payment_enabled gate
from handlers.states import Registration
from keyboards.builders import get_main_menu_kb
from handlers.admin_caps import notify_by_capability  # D-13: fan out by capability, not bare ADMIN_IDS

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
    if await get_setting_typed("payment_enabled") != "on":  # REG-02: registry-backed
        return False
    user = await get_user(telegram_id)
    if not user:
        return False
    if (user.get("payment_status") or "not_paid") not in ("not_paid", "overdue"):
        return False
    requisites = await _resolve_requisites(telegram_id)
    return bool(requisites and requisites.strip())


def _parse_options(raw: str) -> list[tuple[str, int, set[str] | None]]:
    """Parse the payment_options setting → [(label, price, tracks)].

    Two accepted shapes:
    - 'label|price' (unchanged since Phase 4) — tracks is None, meaning "offered to ALL
      tracks" (D-16's backward-compat guarantee: existing RusCo config keeps working
      byte-identical).
    - 'label|price|track1,track2' (Phase 5, D-16) — an optional third field, comma-separated
      track values (each stripped). An empty/blank third field ("label|price|") ALSO yields
      tracks None, not an empty set — an empty set would mean "matches nobody", which is not
      what a trailing empty field means.

    A pipe-less line still yields (line, 0, None), and a non-integer price still falls back
    to 0, exactly as before Phase 5.
    """
    options: list[tuple[str, int, set[str] | None]] = []
    for line in (raw or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            parts = line.split("|")
            label = parts[0].strip() or "Участие"
            try:
                price = int(parts[1].strip())
                if price < 0:  # LOW: a negative fee is meaningless — clamp to 0 like a bad parse
                    price = 0
            except ValueError:
                price = 0
            tracks: set[str] | None = None
            if len(parts) >= 3:
                raw_tracks = parts[2].strip()
                if raw_tracks:
                    tracks = {t.strip() for t in raw_tracks.split(",") if t.strip()} or None
        else:
            label, price, tracks = line, 0, None
        options.append((label, price, tracks))
    return options


def _visible_options(
    options: list[tuple[str, int, set[str] | None]], participant_type: str
) -> list[tuple[int, str, int]]:
    """D-17 safeguard: a pure, directly-unit-testable index-preservation helper.

    Enumerates the FULL, unfiltered `options` list and emits (i, label, price) for every
    entry whose track set is None (offered to all tracks) or contains `participant_type`.
    The emitted `i` is the enumerate() index into the ORIGINAL list — filtering never
    renumbers. A tariff sitting at index 1 of the full list keeps callback_data
    "pay_option:1" whether or not it is visible to a given track.
    """
    return [
        (i, label, price)
        for i, (label, price, tracks) in enumerate(options)
        if tracks is None or participant_type in tracks
    ]


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


async def start_payment_step(bot: Bot, telegram_id: int, participant_type: str = "full"):
    """Entry point called from approve_user() when payment_enabled=on. Fail-soft.

    Phase 5 (D-17): only the RENDERED keyboard is filtered by track — pay_option:{i}
    callback_data always indexes the FULL unfiltered `options` list (see
    process_payment_option), so a keyboard already delivered before a later settings edit
    can never resolve to a shifted tariff.
    """
    try:
        options = _parse_options(await get_setting("payment_options") or "")
        # D-17: visible is built by enumerating the FULL options list once — never
        # re-enumerated. i is the index into `options`, preserved under filtering.
        visible = _visible_options(options, participant_type)
        paid = [v for v in visible if v[2] > 0]
        if len(visible) > 1 and paid:
            # Multi-option path: let the user pick. State is set later by _show_payment_details.
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{label} — {price} ₽", callback_data=f"pay_option:{i}")]
                for i, label, price in visible
            ] + [[_PAY_LATER_BTN]])
            text = "💳 Выбери вариант участия:"
            block = _format_requisites_block(await _resolve_requisites(telegram_id))
            if block:
                text += f"\n\n{block}"
            await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=kb)
            return
        if not visible:
            # D-18: no tariff matches this track — treat as free, same outcome as
            # payment_enabled=off. Never strand an approved user on a screen with no
            # actionable button (mirrors the free-path branch in _show_payment_details).
            from handlers.registration import send_completion_and_bonus
            await send_completion_and_bonus(bot, telegram_id, participant_type=participant_type)
            return
        # Single / free path: skip selection, go straight to details. Read from the
        # VISIBLE list — never the unfiltered list's first entry (T-05-05-07): a party-only
        # tariff sitting at a non-zero index of the full list must be the one shown and charged.
        _idx0, label, price = visible[0]
        key = StorageKey(bot_id=bot.id, chat_id=telegram_id, user_id=telegram_id)
        ctx = FSMContext(storage=_storage, key=key)
        # WR-01: thread the already-resolved track through so a free/single-tariff party
        # delegate gets approve_text__party, not the global approve_text.
        await _show_payment_details(bot, telegram_id, ctx, label, price, participant_type=participant_type)
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
    label, price, tracks = options[idx]
    # T-05-05-03: re-check eligibility server-side. Filtering only the rendered keyboard
    # (D-17) is not enough on its own — a stale keyboard from another track, or one built
    # before a settings edit, could still tap a `pay_option:{i}` that is not currently
    # offered to the caller's CURRENT track. Resolve the track fresh via get_user (never
    # trust anything client-supplied) and reject before any charge/side-effect runs.
    # WR-01: resolved unconditionally (not only when `tracks is not None`) so the value is
    # always available to pass into _show_payment_details below — a party delegate picking an
    # UNTRACKED ("offered to all") free option must still get approve_text__party.
    try:
        user = await get_user(callback.from_user.id)
    except Exception as e:
        logger.error(f"process_payment_option: get_user failed for {callback.from_user.id}: {e}")
        user = None
    current_track = (user or {}).get("participant_type") or "full"
    if tracks is not None and current_track not in tracks:
        await callback.answer("Этот вариант недоступен для твоего трека.", show_alert=True)
        return
    # WR-01: fail-soft like start_payment_step, and guarantee callback.answer() via finally so
    # a mid-flow error never leaves the tapped button spinning.
    try:
        await update_payment_status(callback.from_user.id, "not_paid", payment_option=label)
        await _show_payment_details(
            callback.bot, callback.from_user.id, state, label, price, participant_type=current_track
        )
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


async def _show_payment_details(
    bot: Bot, telegram_id: int, state: FSMContext, option_label: str, option_price: int,
    participant_type: str | None = None,
):
    requisites = await _resolve_requisites(telegram_id)  # per-LC card, else shared
    deadline = await get_setting("payment_deadline")
    penalties = await get_setting("penalty_schedule")

    # WR-05: free participation (price 0 and no bank requisites) must not be forced
    # through a receipt-upload gate. Land the user on the completion text + menu instead.
    if option_price == 0 and not (requisites and requisites.strip()):
        from handlers.registration import send_completion_and_bonus
        # WR-01: thread participant_type so a free-tariff or free-option-among-many party
        # delegate gets approve_text__party instead of always falling back to the global text.
        await send_completion_and_bonus(bot, telegram_id, participant_type=participant_type)
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


# LOW (receipt hardening). The bot never downloads or parses a receipt — it stores the
# Telegram file_id and forwards it to the admin queue — so a spoofed MIME is bounded. Still,
# accept only an explicit allowlist for documents, cap the size, and rate-limit uploads so a
# user can't flood the «🧾 Чеки» queue. Telegram's own bot download cap is ~20 MB; a receipt
# is a scan/screenshot, so 10 MB is comfortably generous.
_RECEIPT_DOC_MIME_ALLOWLIST = ("application/pdf",)
_RECEIPT_MAX_BYTES = 10 * 1024 * 1024
_RECEIPT_MIN_INTERVAL_SEC = 3
_last_receipt_upload: dict[int, float] = {}


def _receipt_too_large(file_size) -> bool:
    return bool(file_size) and file_size > _RECEIPT_MAX_BYTES


def _receipt_rate_limited(user_id: int) -> bool:
    """True if this user uploaded a (valid) receipt < _RECEIPT_MIN_INTERVAL_SEC ago. Records the
    timestamp only when NOT limited, so the window runs from the last accepted attempt."""
    now = time.monotonic()
    last = _last_receipt_upload.get(user_id)
    if last is not None and (now - last) < _RECEIPT_MIN_INTERVAL_SEC:
        return True
    _last_receipt_upload[user_id] = now
    return False


@router.message(Registration.receipt_upload, F.document)
async def process_receipt_document(message: types.Message, state: FSMContext):  # IN-03: bot param was unused
    if message.document.mime_type not in _RECEIPT_DOC_MIME_ALLOWLIST:
        await message.answer(
            "❌ Принимается только PDF-документ. Для скриншота используй функцию отправки фото."
        )
        return
    if _receipt_too_large(message.document.file_size):
        await message.answer("❌ Файл слишком большой (максимум 10 МБ). Пришли чек меньшего размера.")
        return
    if _receipt_rate_limited(message.from_user.id):
        await message.answer("⏳ Слишком часто. Подожди пару секунд и попробуй снова.")
        return
    await _finalize_receipt(message, state, message.document.file_id)


@router.message(Registration.receipt_upload, F.photo)
async def process_receipt_photo(message: types.Message, state: FSMContext):  # IN-03: bot param was unused
    if _receipt_too_large(message.photo[-1].file_size):
        await message.answer("❌ Изображение слишком большое (максимум 10 МБ). Пришли чек меньшего размера.")
        return
    if _receipt_rate_limited(message.from_user.id):
        await message.answer("⏳ Слишком часто. Подожди пару секунд и попробуй снова.")
        return
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
        await notify_by_capability(message.bot, "moderate_receipts", note, parse_mode="HTML")  # D-13
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
