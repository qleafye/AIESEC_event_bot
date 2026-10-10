"""Раздел «🚫 Правила автоотказа» -> «🕘 Применить к уже поданным»: ретро-применение действующих
правил к заявкам, поданным раньше, чем правило появилось. Период выбирается кнопками, дальше
предпросмотр (сколько отклонится с письмом, сколько получит пометку, сколько одобренных не тронем)
и отдельная кнопка «применить». Логика — `services/reject_retro.py`.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; импортируется хвостом
`handlers/settings/admin_sections.py`. Право — как у экрана правил (`settings`); применять может только
руководитель без привязки к городу (правила действуют на все города сразу).
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import timedelta

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from services import reject_retro
from services.reject_rules import can_edit_city
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

_PERIODS = (
    ("3", "За 3 дня"), ("7", "За неделю"), ("14", "За 2 недели"), ("30", "За месяц"),
    ("all", "За всё время"),
)
_DENIED = "Применять правила к уже поданным заявкам может только руководитель без привязки к городу."
_lock = asyncio.Lock()


def _since(period: str) -> str:
    if period == "all":
        return "0000-00-00"
    return (msk_now() - timedelta(days=int(period))).strftime("%Y-%m-%d")


def _period_label(period: str) -> str:
    return dict(_PERIODS).get(period, "За всё время").lower()


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="← К правилам автоотказа", callback_data="admin_reject_rules")]


async def _edit(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


def _period_of(data: str) -> str | None:
    period = data.split(":", 1)[-1]
    return period if period in dict(_PERIODS) else None


@router.callback_query(F.data == "rjretro")
async def reject_retro_menu(callback: types.CallbackQuery):
    if not await can_edit_city(callback.from_user.id, None):
        await callback.answer(_DENIED, show_alert=True)
        return
    text = (
        "<b>🕘 Применить правила к уже поданным заявкам</b>\n\n"
        "Если правило завели позже, чем делегаты подали анкеты, оно не сработало на этих заявках. "
        "Здесь можно проверить уже поданные: бот покажет, кого отклонят правила, и ничего не изменит, "
        "пока вы не нажмёте «Применить».\n\n"
        "За какой период проверить заявки?"
    )
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rjretro_p:{code}")] for code, label in _PERIODS]
    rows.append(_back_row())
    await _edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def _render_preview(period: str) -> tuple[str, InlineKeyboardMarkup, dict]:
    report = await reject_retro.preview(_since(period))
    again = [InlineKeyboardButton(text="← Другой период", callback_data="rjretro")]
    if not report["pending"] and not report["rejected"]:
        text = (
            f"<b>🕘 Проверка заявок {_period_label(period)}</b>\n\n"
            "Под действующие правила никто не подходит — применять нечего."
        )
        if report["approved"]:
            text += f"\nОдобренных, которые подошли бы под правила: {report['approved']} — их бот не трогает."
        return text, InlineKeyboardMarkup(inline_keyboard=[again, _back_row()]), report
    lines = [
        f"<b>🕘 Проверка заявок {_period_label(period)}</b>",
        "",
        f"Будут отклонены правилами, делегаты получат письмо с причиной: {report['pending']}",
        f"Уже отклонены вручную — получат только пометку «отклонено правилом», писем не будет: {report['rejected']}",
        f"Одобренные, которые подошли бы под правила, не трогаем: {report['approved']}",
    ]
    if report["examples"]:
        lines.append("")
        lines.append("Например:")
        lines += [f"• {html.escape(x)}" for x in report["examples"]]
        rest = report["pending"] - len(report["examples"])
        if rest > 0:
            lines.append(f"…и ещё {rest}")
    lines += [
        "",
        "Кнопкой отменить отклонение нельзя — делегата можно вернуть на модерацию вручную в журнале "
        "«🤖 Автоотказы». Повторное применение никого не задвоит. Ночью письма встанут в очередь до "
        "утра (тихие часы).",
    ]
    rows = [
        [InlineKeyboardButton(text="✅ Применить", callback_data=f"rjretro_go:{period}:{report['digest']}")],
        again,
        _back_row(),
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), report


@router.callback_query(F.data.startswith("rjretro_p:"))
async def reject_retro_preview(callback: types.CallbackQuery):
    if not await can_edit_city(callback.from_user.id, None):
        await callback.answer(_DENIED, show_alert=True)
        return
    period = _period_of(callback.data)
    if period is None:
        await callback.answer("Период не распознан — откройте экран заново.", show_alert=True)
        return
    text, kb, _ = await _render_preview(period)
    await _edit(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("rjretro_go:"))
async def reject_retro_go(callback: types.CallbackQuery):
    if not await can_edit_city(callback.from_user.id, None):
        await callback.answer(_DENIED, show_alert=True)
        return
    parts = callback.data.split(":")
    period = parts[1] if len(parts) > 1 and parts[1] in dict(_PERIODS) else None
    if period is None or len(parts) != 3:
        await callback.answer("Период не распознан — откройте экран заново.", show_alert=True)
        return
    digest = parts[2]
    if _lock.locked():
        await callback.answer("Применение уже идёт — дождитесь итога.", show_alert=True)
        return
    async with _lock:
        targets = reject_retro.split_targets(await reject_retro.collect(_since(period)))
        current = targets["pending"] + targets["rejected"]
        if reject_retro.ids_digest(current) != digest:
            text, kb, _ = await _render_preview(period)
            await callback.answer("Список изменился, проверьте ещё раз.", show_alert=True)
            await _edit(callback, text, kb)
            return
        await callback.answer("Применяю, это займёт немного времени…")
        ids = {int(u["telegram_id"]) for u, _ in current}
        try:
            done = await reject_retro.apply(callback.bot, _since(period), ids=ids)
        except Exception:
            logger.exception("reject_retro: применение оборвалось by=%s period=%s", callback.from_user.id, period)
            await callback.message.answer(
                "Применение оборвалось на полпути. Часть заявок могла успеть обработаться — "
                "откройте «Применить к уже поданным» заново: бот покажет, что осталось, и "
                "обработанное не задвоится."
            )
            return
    logger.info("reject_retro: by=%s period=%s итог=%s", callback.from_user.id, period, done)
    text = (
        f"Готово. Отклонено правилами: {done['pending']}, "
        f"пометка у отклонённых вручную: {done['rejected']}."
    )
    if done.get("skipped"):
        text += f"\nПропущено: {done['skipped']} — их успели одобрить или отклонить вручную, пока шло применение."
    if done["failed"]:
        text += (
            f"\nНе удалось обработать: {done['failed']} — откройте «🕘 Применить к уже поданным» заново и "
            "нажмите «✅ Применить» — обработанное не задвоится."
        )
    if done.get("sheet_failed"):
        text += (
            "\nТаблица не обновилась — статусы в ней могут отставать от бота. "
            "Нажмите «🔍 Сверить таблицу с базой» в админке."
        )
    await callback.message.answer(text)
    try:
        from handlers.applications.admin_reject_rules import render_rules_screen
        screen, kb = await render_rules_screen(callback.from_user.id)
        await callback.message.answer(screen, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("reject_retro: экран правил не показан")


__all__ = ["reject_retro_menu", "reject_retro_preview", "reject_retro_go"]
