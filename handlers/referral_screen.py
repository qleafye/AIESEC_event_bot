"""Экран «Моя ссылка» в боте — тело без роутера.

Хендлеры (`my_referral_link`, `ambjoin`, `ambleave*`, `ambpath:*`) остаются в
`handlers/user_actions.py` на своих местах — порядок регистрации = поведение (golden-снимок
`tests/test_refac_snapshot_260816.py`); там же экран импортирован под прежним именем
`_referral_screen`.

«Моя ссылка» — единственное место амбассадорского самообслуживания в чате. Ссылка для
приглашений показывается всем: приглашать может любой делегат, приглашения до вступления
в команду засчитываются. Амбассадору — прогресс, выбор пути (меняет только порядок заданий)
и кнопка выхода. Не-амбассадору — по `services.amb_status.delegate_state`: кнопка
«Хочу стать амбассадором», строка «заявка рассматривается» (режим отбора) или строка
«места заняты» (лимит набран / отказано в этом сезоне).
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_user
from handlers import reg_i18n
from reg_engine import build_referral_link
from services import amb_progress, amb_status
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_PATHS = (
    ("invite", "ambassador_path_label_invite"),
    ("content", "ambassador_path_label_content"),
    ("none", "ambassador_path_label_none"),
)


def amb_tr(lang: str, tr_map: dict | None):
    async def tr_key(key: str) -> str:  # колбэк перевода для services.amb_progress
        return reg_i18n.tr_text(await get_setting_typed(key), lang, tr_map or {})
    return tr_key


async def _tr_key(key: str, lang: str, tr_map: dict) -> str:
    return reg_i18n.tr_text(await get_setting_typed(key), lang, tr_map)


async def _state(user_id: int) -> str:
    """Fail-open: сбой чтения статуса показывает кнопку — `request_join` перепроверит."""
    try:
        return await amb_status.delegate_state(user_id)
    except Exception:
        logger.exception("referral_screen: delegate_state не прочитан (tid=%s)", user_id)
        return "open"


async def referral_screen(
    user_id: int, bot: Bot, lang: str = "ru", tr_map: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    tr_map = tr_map or {}
    user = await get_user(user_id)
    bot_user = await bot.get_me()
    referral_link = build_referral_link(bot_user.username, user_id)
    tpl = await get_setting_typed("referral_link_prompt_text")
    text = reg_i18n.tr_fmt(tpl, lang, tr_map, link=referral_link)

    buttons: list[list[InlineKeyboardButton]] = []
    if user and user.get("is_ambassador"):
        progress = await amb_progress.render_progress(user_id, amb_tr(lang, tr_map))
        if progress is not None:
            text += "\n\n" + progress
        text += "\n\n" + await _tr_key("ambassador_path_prompt_text", lang, tr_map)
        current_path = user.get("ambassador_path") or "none"  # NULL -> метка "none"
        path_row = []
        for code, key in _PATHS:
            label = await _tr_key(key, lang, tr_map)
            mark = "✅ " if current_path == code else ""
            path_row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"ambpath:{code}"))
        buttons.append(path_row)
        leave_label = await _tr_key("ambassador_leave_button_text", lang, tr_map)
        buttons.append([InlineKeyboardButton(text=leave_label, callback_data="ambleave")])
    else:
        state = await _state(user_id)
        if state == "candidate":
            line = await _tr_key("amb_status_candidate_text", lang, tr_map)
            if line:
                text += "\n\n" + line
        elif state in ("full", "declined"):
            line = await _tr_key("amb_slots_full_text", lang, tr_map)
            if line:
                text += "\n\n" + line
        else:
            cta_label = await _tr_key("miniapp_form_ambassador_cta_text", lang, tr_map)
            buttons.append([InlineKeyboardButton(text=cta_label, callback_data="ambjoin")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return text, kb
