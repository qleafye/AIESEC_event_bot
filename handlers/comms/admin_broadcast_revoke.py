"""Удаление рассылки у получателей: «🗑 Удалить» → подтверждение → прогресс → итог.

Инлайн-кнопки не истекают, поэтому каждое нажатие перепроверяет статус и окно 48 ч: удалённая
отвечает «Уже удалена у получателей.», второй тап «🗑 Да, удалить» не запускает второй прогон
(`claim_revoke`), «❌ Отмена» возвращает карточку рассылки, а после ⛔ рассылка не помечается
удалённой — остальное можно удалить ещё раз.

Шов к общему `admin.router`; импортируется из середины `handlers/comms/admin_broadcasts.py`
(порядок регистрации прежний), сам `admin_broadcasts` берёт лениво — тесты подменяют там `_spawn`."""
import html as html_module
import re

from aiogram import Bot, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_broadcast, list_broadcast_messages
from handlers.admin import router
from services.comms.broadcast_run import can_revoke, claim_revoke, release_revoke, run_revoke

# INVARIANT (13-01 cap-test): каждый `@router.*` декоратор ниже — в ОДНУ строку.


async def _revoke_refused(callback: types.CallbackQuery, row: dict) -> bool:
    """Общий отказ для «🗑 Удалить» и «🗑 Да, удалить»: инлайн-кнопки не истекают, поэтому
    статус и окно 48 ч перепроверяются на каждом нажатии."""
    if row["status"] == "revoked":
        await callback.answer("Уже удалена у получателей.", show_alert=True)
        return True
    if not can_revoke(row.get("started_at")):
        await callback.answer("Удалить нельзя: прошло больше 48 часов.", show_alert=True)
        return True
    return False


def _back_to_broadcasts_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin_broadcast")
    ]])


@router.callback_query(F.data.startswith("bc_rev:"))
async def bc_rev(callback: types.CallbackQuery):
    """Подтверждение отзыва — гейт 48 ч перепроверяется ЗДЕСЬ: инлайн-кнопки не истекают,
    карточка списка могла быть нарисована вчера."""
    try:
        bid = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    row = await get_broadcast(bid)
    if not row:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return
    if await _revoke_refused(callback, row):
        return
    n = len(await list_broadcast_messages(bid))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"bc_revgo:{bid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"bc_revno:{bid}")],
    ])
    await callback.message.edit_text(
        f"Сообщение удалится у {n} человек. Вернуть нельзя. Удалить?", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bc_revno"))
async def bc_revno(callback: types.CallbackQuery):
    """«❌ Отмена» возвращает карточку рассылки. Кнопка без номера (экраны до 11.10) —
    прежний ответ: карточку рисовать не из чего."""
    from handlers.comms import admin_broadcasts as ab  # цикл импорта
    _, _, bid_s = callback.data.partition(":")
    row = await get_broadcast(int(bid_s)) if bid_s.isdigit() else None
    if row:
        text, kb = ab._broadcast_card(row)
        await callback.message.edit_text(text, reply_markup=kb)
    else:
        await callback.message.edit_text("Удаление отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("bc_revgo:"))
async def bc_revgo(callback: types.CallbackQuery, bot: Bot):
    from handlers.comms import admin_broadcasts as ab  # цикл импорта
    try:
        bid = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    row = await get_broadcast(bid)
    if not row:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return
    if await _revoke_refused(callback, row):
        return
    n = len(await list_broadcast_messages(bid))
    if not claim_revoke(bid):
        await callback.answer("Удаление уже идёт.", show_alert=True)
        return
    try:
        await _start_revoke(callback, bot, bid, n, ab._spawn)
    except BaseException:
        release_revoke(bid)  # прогон не стартовал — иначе «Удаление уже идёт.» до рестарта
        raise


async def _start_revoke(callback: types.CallbackQuery, bot: Bot, bid: int, n: int, spawn) -> None:
    stop_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⛔ Остановить", callback_data=f"bc_stop:{bid}")
    ]])
    try:
        await callback.message.edit_text(f"🗑 Удалено 0 из {n}…", reply_markup=stop_kb)
    except Exception:
        pass
    await callback.answer()

    async def on_progress(deleted, failed, total_n):
        try:
            await callback.message.edit_text(f"🗑 Удалено {deleted} из {total_n}…", reply_markup=stop_kb)
        except Exception:
            pass

    async def on_finish(deleted, failed, stopped=False):
        if stopped:
            text = (f"⛔ Удаление рассылки #{bid} остановлено. Удалено: {deleted}, не удалось: {failed}.\n"
                    "Остальное можно удалить ещё раз с карточки в «🗒 Последние рассылки».")
        else:
            text = f"🗑 Рассылка #{bid} удалена у получателей: {deleted}, не удалось {failed}."
        try:
            await callback.message.edit_text(text, reply_markup=_back_to_broadcasts_kb())
        except Exception:
            pass

    spawn(run_revoke(bot, bid, on_progress=on_progress, on_finish=on_finish, claimed=True))


def revoked_line(row: dict) -> str:
    preview = re.sub(r"<[^>]+>", "", row.get("text_preview") or "").replace("\n", " ")
    if len(preview) > 40:  # режем до экранирования — иначе можно разрезать &amp;
        preview = preview[:40].rstrip() + "…"
    preview = html_module.escape(preview)
    return f"🗑 #{row.get('id')} — {row.get('started_at') or '—'} — удалена у получателей: {preview}"
