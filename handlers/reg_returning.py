"""/start «прошлого делегата»: возвращенец прошлого сезона и отклонённый в этом сезоне.

Обоих `cmd_start` ловит одним предикатом `reg_engine.is_returning_row` (оба не должны упереться
в тупик «уже зарегистрирован»), но экраны у них разные. Приёмка 09.10 (D3): отклонённый в
ТЕКУЩЕМ сезоне получал баннер возвращенца «Ты уже был(а) с нами на <текущий сезон>» — теперь:

- заявка из ПРОШЛОГО сезона (`reg_engine.is_past_season_row`) — баннер `start_text_returning`
  с названием того сезона, как его когда-то ввёл менеджер в «🎉 Сезон события»;
- отклонённый в этом сезоне — `start_text_rejected` (сезон не называется) с той же кнопкой
  «🚀 Обновить анкету», а при запрете повторной подачи — обычное приветствие и текст закрытия.
"""
import logging

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import domain.regform.engine as reg_engine
from database.db import get_setting
from handlers.i18n import reg_i18n
from services import reg_edit_policy
from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed

logger = logging.getLogger(__name__)

PAST_EVENT_FALLBACK = "прошлом событии"


def past_season_label(user: dict | None) -> str:
    """Название прошлого сезона для {season}: значение «🎉 Сезон события», под которым делегат
    подавал анкету (поле задумано как имя для людей: «YL'26»). Пустое — «прошлом событии»."""
    return ((user or {}).get("season") or "").strip() or PAST_EVENT_FALLBACK


async def _screen_text(message: types.Message, user: dict, event_season: str | None) -> str:
    if reg_engine.is_past_season_row(user, event_season):
        text = await get_setting("start_text_returning") or SETTINGS_SCHEMA["start_text_returning"]["default"]
        text = await reg_i18n.tr_for(message, text)  # Quick 260906: перевод ДО подстановки сезона
        # str.replace, не .format(): в тексте менеджера могут быть посторонние {}.
        return text.replace("{season}", past_season_label(user))
    return await get_setting("start_text_rejected") or SETTINGS_SCHEMA["start_text_rejected"]["default"]


async def offer_returning(
    message: types.Message, state: FSMContext, user: dict, event_season: str | None,
    start_text: str, start_photo: str | None, *, referrer_id=None, source_tag=None,
    party_track=None, dl_event_city=None,
) -> None:
    """Экран /start для строки, где `is_returning_row` истинна. Всегда завершает /start."""
    from handlers import registration as reg  # циклический импорт: шов подключается из registration

    user_id = message.from_user.id
    menu_kb = await reg.get_main_menu_kb(user_id)
    # Квик 260922-wrg: при «нельзя» отклонённый этого сезона не видит кнопку вовсе — только текст
    # закрытия. Прошлого сезона гейт не касается (всегда True).
    can_resubmit, closed_text = await reg_edit_policy.resubmit_gate(user)
    if not can_resubmit:
        await reg._send_welcome(message, start_text, start_photo, menu_kb, user_id)
        await reg_i18n.say(message, closed_text)
        return

    await reg._send_welcome(message, await _screen_text(message, user, event_season), start_photo, menu_kb, user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\U0001f680 Обновить анкету", callback_data="rereg_start")
    ]])
    await reg_i18n.say(message, await get_setting_typed("start_returning_cta_text"), reply_markup=kb)
    # Тап rereg_start придёт отдельным апдейтом, когда локальные переменные этого /start уже
    # пропадут, — сохраняем атрибуцию диплинка в FSM (тот же приём, что у развилки города).
    if referrer_id:
        await state.update_data(referrer_id=referrer_id)
    if source_tag:
        await state.update_data(source=source_tag, _source_from_tag=True)
    if party_track:
        await state.update_data(participant_type=party_track, _track_from_link=True)
    if dl_event_city:
        await state.update_data(event_city=dl_event_city)
