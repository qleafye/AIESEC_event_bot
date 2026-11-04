"""Нажатие делегата на кнопку согласия под рассылкой.

В кнопке только номер рассылки. Ключ читается из журнала, не из callback.
"""
from __future__ import annotations

from aiogram import F, types

from database.db import get_broadcast, record_user_consent
from domain.regform.engine import broadcast_consent_entries
from domain.settings.schema import get_setting_typed
from handlers.i18n import reg_i18n
from handlers.user_actions import router
from services.infra.timeutil import msk_now

_THANKS = "Принято. Спасибо."
_STALE = "Эта кнопка больше не действует."


async def _phrase(key: str, fallback: str, callback: types.CallbackQuery) -> str:
    raw = await get_setting_typed(key)
    text = raw if isinstance(raw, str) and raw.strip() else fallback
    lang, tr_map = await reg_i18n.ctx_for(callback)
    return reg_i18n.tr_text(text, lang, tr_map)


async def _remember_tap(broadcast_id: int, user_id: int) -> None:
    from database.db import _connect

    async with _connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO broadcast_consent_taps "
            "(broadcast_id, user_id, accepted_at) VALUES (?, ?, ?)",
            (broadcast_id, user_id, msk_now().isoformat()),
        )
        await conn.commit()


@router.callback_query(F.data.startswith("cns:"))
async def consent_tap(callback: types.CallbackQuery):
    tail = callback.data.split(":", 1)[1]
    try:
        broadcast_id = int(tail)
    except ValueError:
        await callback.answer(await _phrase("consent_broadcast_stale_text", _STALE, callback), show_alert=True)
        return
    row = await get_broadcast(broadcast_id)
    key = ((row or {}).get("consent_key") or "").strip()
    if not row or not key:
        await callback.answer(await _phrase("consent_broadcast_stale_text", _STALE, callback), show_alert=True)
        return
    entries = await broadcast_consent_entries()
    label = next((lbl for lbl, item in entries if item == key), key)
    await record_user_consent(callback.from_user.id, key, raw_button=label)
    await _remember_tap(broadcast_id, callback.from_user.id)
    await callback.answer(await _phrase("consent_broadcast_thanks_text", _THANKS, callback), show_alert=True)
