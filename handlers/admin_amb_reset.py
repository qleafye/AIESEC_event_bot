"""«🤝 Амбассадоры → 🙋 Кандидаты и команда» -> «🧹 Сбросить статусы»: сброс статусов амбассадора,
оставшихся после миграции (кандидаты и амбассадоры прошлых сезонов, «да» из старой анкеты).

Менеджер выбирает кнопкой, что сбросить, видит предпросмотр с перечнем, что пропадёт и у скольких
людей, и подтверждает отдельной кнопкой. Логика — `services.amb_status_reset` (она же под
`tools/amb_status_reset.py`). Сообщений людям не уходит, баллы не трогаются, место за человеком с
выданным пакетом остаётся. Кнопка подтверждения несёт отпечаток списка, который менеджер видел: если за это
время список изменился, сброс не выполняется, а предпросмотр показывается заново.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/admin_amb_candidates.py`. Право — `moderate_game`, как у соседних кнопок.
Экран общий для всех городов, поэтому доступен только админу без городского фильтра.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from services import amb_status_reset as svc

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_CODES = {"past": svc.SCOPE_PAST, "cand": svc.SCOPE_CANDIDATES}
_BACK = "admin_amb_candidates"
_STALE = "Кнопка устарела — откройте экран заново"


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="← К кандидатам и команде", callback_data=_BACK)]


async def _edit(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _is_global(admin_id: int) -> bool:
    from handlers.admin_core import _admin_city_view

    scope, _ = await _admin_city_view(admin_id)
    return scope is None


_NOT_GLOBAL = (
    "Сброс статусов касается всех городов сразу, поэтому он доступен только при выбранном режиме "
    "«Все города». Переключите город в админке и откройте экран заново."
)


@router.callback_query(F.data == "ambrst")
async def amb_reset_menu(callback: types.CallbackQuery):
    if not await _is_global(callback.from_user.id):
        await callback.answer(_NOT_GLOBAL, show_alert=True)
        return
    if not await svc.has_status_column():
        await _edit(callback, svc.NO_COLUMN, InlineKeyboardMarkup(inline_keyboard=[_back_row()]))
        await callback.answer()
        return
    past = len((await svc.preview(svc.SCOPE_PAST))["rows"])
    cand = len((await svc.preview(svc.SCOPE_CANDIDATES))["rows"])
    text = (
        "<b>🧹 Сбросить статусы амбассадоров</b>\n\n"
        "Для уборки после обновления: кто остался со статусом от прошлых сезонов или от старой "
        "анкеты. Сначала покажу, у кого и что пропадёт, и только потом сброшу — по вашей кнопке.\n\n"
        f"• Статусы прошлых сезонов: {past}\n"
        f"• Кандидаты этого сезона: {cand}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Прошлых сезонов ({past})", callback_data="ambrst_p:past")],
        [InlineKeyboardButton(text=f"Кандидатов этого сезона ({cand})", callback_data="ambrst_p:cand")],
        _back_row(),
    ])
    await _edit(callback, text, kb)
    await callback.answer()


async def _render_preview(code: str) -> tuple[str, InlineKeyboardMarkup]:
    plan = await svc.preview(_CODES[code])
    season, rows = plan["season"], plan["rows"]
    title = "статусы прошлых сезонов" if code == "past" else "кандидатов этого сезона"
    head = f"<b>🧹 Сбросить {title}</b>"
    season_line = f"Сезон события: {('«' + html.escape(season) + '»') if season else 'не задан'}"
    back = [InlineKeyboardButton(text="← Назад", callback_data="ambrst")]
    if not rows:
        return f"{head}\n\n{season_line}\n\nСбрасывать нечего.", InlineKeyboardMarkup(inline_keyboard=[back])
    active = sum(1 for r in rows if r[1] == "active")
    lines = [head, "", season_line, f"Будет сброшено людей: {len(rows)}"]
    lines += [html.escape(x) for x in svc.breakdown(rows)]
    lines.append("")
    if code == "past":
        lines.append(
            "Что пропадёт: у этих людей исчезнет статус амбассадора, кандидата или отказа. "
            "Вернуть можно только вручную — кнопкой «➕ Назначить амбассадором»."
        )
        if active:
            lines.append(
                f"Внимание: среди них действующие амбассадоры прошлых сезонов — {active}. "
                "Они перестанут считаться амбассадорами."
            )
    else:
        lines.append(
            "Что пропадёт: эти люди перестанут быть кандидатами и исчезнут из списка. Чтобы снова "
            "стать кандидатом, им придётся отправить заявку заново."
        )
    lines.append(
        "Сообщений людям не уйдёт, баллы не изменятся. Если человека успели взять в команду, "
        "его пропустим."
    )
    if not season:
        lines += ["", f"⚠️ {svc.NO_SEASON}"]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[back])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Сбросить ({len(rows)})", callback_data=f"ambrst_go:{code}:{svc.ids_digest(rows)}",
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ambrst")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("ambrst_p:"))
async def amb_reset_preview(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if code not in _CODES:
        await callback.answer(_STALE, show_alert=True)
        return
    if not await _is_global(callback.from_user.id):
        await callback.answer(_NOT_GLOBAL, show_alert=True)
        return
    text, kb = await _render_preview(code)
    await _edit(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ambrst_go:"))
async def amb_reset_go(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in _CODES or not parts[2]:
        await callback.answer(_STALE, show_alert=True)
        return
    code, expected = parts[1], parts[2]
    if not await _is_global(callback.from_user.id):
        await callback.answer(_NOT_GLOBAL, show_alert=True)
        return
    if _lock.locked():
        await callback.answer("Сброс уже идёт — дождитесь итога.", show_alert=True)
        return
    async with _lock:
        plan = await svc.preview(_CODES[code])
        if not plan["season"]:
            await callback.answer(svc.NO_SEASON, show_alert=True)
            return
        if svc.ids_digest(plan["rows"]) != expected:
            text, kb = await _render_preview(code)
            await callback.answer("Список изменился, проверьте ещё раз.", show_alert=True)
            await _edit(callback, text, kb)
            return
        await callback.answer()
        try:
            result = await svc.apply(_CODES[code], plan)
        except Exception:
            logger.exception("amb_status_reset: сброс оборвался by=%s режим=%s", callback.from_user.id, _CODES[code])
            await callback.message.answer(
                "Не получилось сбросить статусы до конца. Часть людей могла успеть сброситься. "
                "Откройте «🧹 Сбросить статусы» заново: бот покажет, кто остался, повторный сброс "
                "никого не задвоит. Если снова не выйдет — напишите @qleafye."
            )
            return
    done, skipped = result["done"], result["skipped"]
    logger.info(
        "amb_status_reset: by=%s режим=%s сброшено=%s пропущено=%s",
        callback.from_user.id, _CODES[code], len(done), len(skipped),
    )
    lines = [f"Готово. Сброшено людей: {len(done)}."]
    if done:
        lines += [html.escape(x) for x in svc.breakdown(done)]
    if skipped:
        lines.append(
            f"Пропущено: {len(skipped)} — статус успел смениться (например, вы взяли человека в команду)."
        )
    lines.append("Сообщений никому не отправлено.")
    await _edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[_back_row()]))


__all__ = ["amb_reset_menu", "amb_reset_preview", "amb_reset_go"]
