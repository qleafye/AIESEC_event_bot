"""Quick 260822 — шов registration: пересогласие уже зарегистрированного делегата.

Когда менеджер поднял `consent_version` (правка текста/PDF) и включил тумблер
«🔁 Просить пересогласие при новой редакции», делегат с подписью старой редакции при /start
видит согласие ещё раз — той же карточкой, что и в анкете (PDF + кнопка), но по СВОЕМУ
callback'у `consent_renew:<key>`: `consent_accept:*` из reg_flow привязан к состоянию
`Registration.consent_pending` и после принятия двигает АНКЕТУ дальше (спрашивает ФИО), что
для зарегистрированного делегата недопустимо. Здесь FSM не трогаем вовсе.

Регистрирует хендлер на общий `router` владельца (`handlers.registration`) и импортируется
из его хвоста, как reg_flow/reg_steps.
"""
import html
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_setting, record_user_consent
from settings_schema import get_setting_typed
from services.consent import recollect_gate_on, outstanding_consents
from handlers.registration import router, _consent_entries, _prompt

logger = logging.getLogger(__name__)

RENEW_PREFIX = "consent_renew:"


async def _send_renew_card(message: types.Message, label: str, consent_key: str) -> None:
    """Та же карточка, что у шага consent:* в анкете (registration._ask_step), но с
    callback'ом пересогласия."""
    pdf_file_id = await get_setting(f"consent_pdf_{consent_key}")
    caption = html.escape(await _prompt(f"consent_{consent_key}", label))
    btn_text = await get_setting("consent_button_text") or "Согласен(-на)"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=btn_text, callback_data=f"{RENEW_PREFIX}{consent_key}")
    ]])
    if pdf_file_id:
        try:
            await message.answer_document(pdf_file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            return
        except Exception as e:
            logger.warning(f"consent renew: PDF {consent_key} не отправился, шлём текстом: {e}")
    await message.answer(caption, reply_markup=kb, parse_mode="HTML")


async def maybe_offer_consent_recollect(message: types.Message, user_id: int) -> bool:
    """Вызывается из cmd_start для уже зарегистрированного. True = согласие показано.
    Гейт выключен (дефолт) или всё подписано текущей редакцией — молча False. Fail-soft:
    любая ошибка здесь не должна стоить делегату его /start."""
    try:
        if not await recollect_gate_on():
            return False
        pending = await outstanding_consents(user_id, await _consent_entries())
        if not pending:
            return False
        intro = await get_setting_typed("consent_recollect_text")
        if intro:
            await message.answer(html.escape(intro))
        label, key = pending[0]
        await _send_renew_card(message, label, key)
        return True
    except Exception as e:
        logger.error(f"consent recollect skipped for {user_id}: {e}")
        return False


@router.callback_query(F.data.startswith("consent_renew:"))
async def consent_renew_accept(callback: types.CallbackQuery):
    consent_key = callback.data[len(RENEW_PREFIX):]
    user_id = callback.from_user.id
    entries = await _consent_entries()
    if not await recollect_gate_on() or consent_key not in {k for _lbl, k in entries}:
        # Старая карточка в чате после выключения гейта / смены списка — просто гасим.
        await callback.answer()
        return
    await record_user_consent(user_id, consent_key)  # новая строка аудита с текущей версией
    await callback.answer("✅ Принято")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    pending = await outstanding_consents(user_id, entries)
    if pending:
        label, key = pending[0]
        await _send_renew_card(callback.message, label, key)
        return
    await callback.message.answer("✅ Спасибо! Согласие обновлено.")
