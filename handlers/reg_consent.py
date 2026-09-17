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
from services.consent import recollect_gate_on, outstanding_consents, tapped_button_text
from handlers.registration import router, _consent_entries, _prompt
# Phase 27 (27-05, LANG-02/LANG-09): say()/tr_for() переводят UI-обвязку экрана пересогласия
# (интро/подтверждение/алерт). Сам текст согласия (caption) переводится ТОЛЬКО ярусом A с
# пустым tr_map (Квик 260917-en, находка «б») — покрывает НАЗВАНИЕ документа по умолчанию,
# машинный перевод (легальный override менеджера) сюда не подключается ни при каких условиях
# (LANG-09), PDF остаётся русским всегда.
from handlers import reg_i18n

logger = logging.getLogger(__name__)

RENEW_PREFIX = "consent_renew:"


async def _send_renew_card(message: types.Message, label: str, consent_key: str) -> None:
    """Та же карточка, что у шага consent:* в анкете (registration._ask_step), но с
    callback'ом пересогласия.

    Phase 27 (27-05) / Квик 260917-en (находка «б» живой проверки 17.09): `caption` по
    умолчанию (без менеджерского override `reg_prompt_consent_{key}`) — НАЗВАНИЕ документа
    (`label`), не юридический текст, переводится через ярус A с ПУСТЫМ `tr_map` (тот же приём,
    что `handlers/registration.py::_ask_step`) — машинный перевод (легальный override) сюда не
    подключается ни при каких условиях, LANG-09 не нарушается. PDF остаётся русским. `btn_text`
    (подпись кнопки «Согласен(-на)») — не юридический текст, переводится обычным ярусом A/tr_map
    (ручная правка менеджера). Прямые `message.answer_document`/`message.answer`, не `say()` —
    намеренно (`say()` перевёл бы caption ещё и через машинный ярус)."""
    pdf_file_id = await get_setting(f"consent_pdf_{consent_key}")
    prompt_text = await _prompt(f"consent_{consent_key}", label)
    lang, _tr_map = await reg_i18n.ctx_for(message)
    caption = html.escape(reg_i18n.tr_text(prompt_text, lang, {}))
    btn_text = await get_setting("consent_button_text") or "Согласен(-на)"
    # Квик 260917-en (приёмка 17.09): кнопка — не текст согласия (caption/PDF выше остаются
    # русскими, LANG-09), переводим только её (см. handlers/registration.py::_ask_step,
    # тот же приём).
    btn_text = await reg_i18n.tr_for(message, btn_text)
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
            # Интро-уведомление О пересогласии (не сам текст согласия) — переводимая обвязка.
            await reg_i18n.say(message, html.escape(intro))
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
    # Quick 260907-4ai: снимок текста НАЖАТОЙ кнопки — читаем markup ДО edit_reply_markup
    # ниже; фолбэк — та же настройка/литерал, что и в _send_renew_card.
    raw_button = tapped_button_text(callback) or await get_setting("consent_button_text") or "Согласен(-на)"
    await record_user_consent(user_id, consent_key, raw_button=raw_button)  # новая строка аудита с текущей версией
    await callback.answer(await reg_i18n.tr_for(callback, "✅ Принято"))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    pending = await outstanding_consents(user_id, entries)
    if pending:
        label, key = pending[0]
        await _send_renew_card(callback.message, label, key)
        return
    await reg_i18n.say(callback.message, "✅ Спасибо! Согласие обновлено.")
