"""Админка «📊 Опросы»: список, карточка с итогами, закрыть / в таблицу / удалить.

Шов на общий `admin.router` (техника Phase 13). Мастер создания — в соседнем
handlers/admin_poll_wizard.py (импортируется в хвосте этого файла, чтобы порядок регистрации
не зависел от порядка импорта — прецедент admin_gamification → admin_game_tasks).

Право — `broadcast` (handlers/admin_caps.py): опрос уходит той же аудитории тем же каналом,
что рассылка. Город из шапки (Phase 09.3) ограничивает и список (опросы этого города + общие),
и аудиторию мастера.

INVARIANT (13-01 cap-test): каждый декоратор `@router.*` — в ОДНУ строку.
"""
import html as html_module
import logging

from aiogram import F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import (
    get_poll,
    list_polls,
    delete_poll,
    count_poll_respondents,
    get_poll_results,
    POLL_STATUS_LABELS,
)
from services.polls import (
    close_poll,
    export_polls_to_sheet,
    render_results_text,
    audience_label,
)
from services.scheduler import cancel_poll_job
from handlers.admin_core import _admin_city_view
from handlers.admin import router

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("scheduled", "sending", "open")
_LIST_LIMIT = 15

_OUT_OF_SCOPE = "Этот опрос адресован другому городу — переключите город в шапке."


def _parse_id(data: str) -> int | None:
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _short(text: str, n: int = 40) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _fmt_dt(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        from datetime import datetime
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(raw)


async def _poll_out_of_scope(admin_id: int, poll: dict) -> bool:
    scope, _ = await _admin_city_view(admin_id)
    if scope is None or not poll.get("city"):
        return False
    return poll["city"] != scope[0]


async def render_poll_list(admin_id: int, *, closed: bool) -> tuple[str, InlineKeyboardMarkup]:
    scope, city_text = await _admin_city_view(admin_id)
    statuses = ("closed",) if closed else _ACTIVE_STATUSES
    polls = await list_polls(statuses=statuses, city_scope=scope)
    head = "📊 <b>Опросы</b>" + (" — закрытые" if closed else "")
    if city_text:
        head += f"\n{html_module.escape(city_text)}"
    lines = [head]
    rows: list[list[InlineKeyboardButton]] = []
    if not polls:
        lines.append("\nПока ничего нет." if closed else "\nОткрытых опросов нет — создайте первый.")
    for p in polls[:_LIST_LIMIT]:
        n = await count_poll_respondents(p["id"]) if not p["is_anonymous"] else None
        status = POLL_STATUS_LABELS.get(p["status"], p["status"])
        tail = f" · {n} отв." if n is not None else " · анон."
        if p["status"] == "scheduled":
            tail = f" · {_fmt_dt(p.get('scheduled_at'))}"
        rows.append([InlineKeyboardButton(
            text=f"{status.split(' ', 1)[0]} {_short(p['question'], 28)}{tail}",
            callback_data=f"poll_card:{p['id']}",
        )])
    if len(polls) > _LIST_LIMIT:
        lines.append(f"\nПоказаны {_LIST_LIMIT} из {len(polls)}.")
    if not closed:
        rows.append([InlineKeyboardButton(text="➕ Новый опрос", callback_data="poll_new")])
        rows.append([InlineKeyboardButton(text="📁 Закрытые опросы", callback_data="admin_polls_closed")])
    else:
        rows.append([InlineKeyboardButton(text="← Открытые опросы", callback_data="admin_polls")])
    rows.append([InlineKeyboardButton(text="← В админку", callback_data="admin_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_polls")
async def show_admin_polls(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_poll_list(callback.from_user.id, closed=False)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin_polls_closed")
async def show_admin_polls_closed(callback: types.CallbackQuery):
    text, kb = await render_poll_list(callback.from_user.id, closed=True)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def render_poll_card(poll_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    results = await get_poll_results(poll_id)
    if results is None:
        return None
    poll = results["poll"]
    status = POLL_STATUS_LABELS.get(poll["status"], poll["status"])
    flags = []
    flags.append("анонимный" if poll["is_anonymous"] else "по людям")
    flags.append("несколько вариантов" if poll["allows_multiple"] else "один вариант")
    lines = [
        f"📊 <b>{html_module.escape(poll['question'])}</b>",
        f"{status} · {', '.join(flags)}",
        f"Кому: {html_module.escape(audience_label(poll.get('audience')))}",
    ]
    if poll["status"] == "scheduled":
        lines.append(f"Отправка: {_fmt_dt(poll.get('scheduled_at'))}")
    lines.append("")
    lines.append(render_results_text(results))
    rows = []
    if poll["status"] == "open":
        rows.append([InlineKeyboardButton(text="⏹ Закрыть опрос", callback_data=f"poll_close:{poll_id}")])
    if poll["status"] in ("open", "closed"):
        rows.append([InlineKeyboardButton(text="📄 В таблицу", callback_data=f"poll_export:{poll_id}")])
    del_text = "❌ Отменить отправку" if poll["status"] == "scheduled" else "🗑 Удалить"
    rows.append([InlineKeyboardButton(text=del_text, callback_data=f"poll_del:{poll_id}")])
    rows.append([InlineKeyboardButton(text="← К опросам", callback_data="admin_polls")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("poll_card:"))
async def show_poll_card(callback: types.CallbackQuery):
    pid = _parse_id(callback.data)
    poll = await get_poll(pid) if pid is not None else None
    if poll is None:
        await callback.answer("Опрос уже удалён.", show_alert=True)
        return
    if await _poll_out_of_scope(callback.from_user.id, poll):
        await callback.answer(_OUT_OF_SCOPE, show_alert=True)
        return
    text, kb = await render_poll_card(pid)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("poll_close:"))
async def poll_close_action(callback: types.CallbackQuery, bot: Bot):
    pid = _parse_id(callback.data)
    poll = await get_poll(pid) if pid is not None else None
    if poll is None:
        await callback.answer("Опрос уже удалён.", show_alert=True)
        return
    if await _poll_out_of_scope(callback.from_user.id, poll):
        await callback.answer(_OUT_OF_SCOPE, show_alert=True)
        return
    if poll["status"] != "open":
        await callback.answer("Опрос не открыт — закрывать нечего.", show_alert=True)
        return
    await callback.answer("Закрываю опрос у всех получивших…")
    ok, failed = await close_poll(bot, pid)
    note = f"⏹ Опрос закрыт: остановлен у {ok}"
    if failed:
        note += f", не удалось у {failed} (чат недоступен — ответы от них уже не придут)"
    text, kb = await render_poll_card(pid)
    await callback.message.edit_text(f"{note}\n\n{text}", parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("poll_export:"))
async def poll_export_action(callback: types.CallbackQuery):
    pid = _parse_id(callback.data)
    poll = await get_poll(pid) if pid is not None else None
    if poll is None:
        await callback.answer("Опрос уже удалён.", show_alert=True)
        return
    await callback.answer("Выгружаю в таблицу…")
    written = await export_polls_to_sheet()
    if written < 0:
        await callback.message.answer(
            "⚠️ Таблица сейчас недоступна (не настроена или ошибка Google). "
            "Результаты видны здесь, в боте — попробуйте выгрузить позже."
        )
        return
    await callback.message.answer(
        f"📄 Вкладка «Опросы» обновлена: {written} строк (все опросы, вкладка перезаписана целиком)."
    )


@router.callback_query(F.data.startswith("poll_del:"))
async def poll_delete_confirm(callback: types.CallbackQuery):
    pid = _parse_id(callback.data)
    poll = await get_poll(pid) if pid is not None else None
    if poll is None:
        await callback.answer("Опрос уже удалён.", show_alert=True)
        return
    if await _poll_out_of_scope(callback.from_user.id, poll):
        await callback.answer(_OUT_OF_SCOPE, show_alert=True)
        return
    if poll["status"] == "scheduled":
        what = "Отправка будет отменена, делегаты ничего не получат."
    else:
        n = await count_poll_respondents(pid)
        what = (
            f"Пропадут все ответы ({n} чел.) и итоги — восстановить будет нельзя. "
            "Уже отправленные опросы в чатах делегатов останутся, но ответы по ним больше не учитываются."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"poll_del_go:{pid}")],
        [InlineKeyboardButton(text="← Назад", callback_data=f"poll_card:{pid}")],
    ])
    await callback.message.edit_text(
        f"Удалить опрос «{html_module.escape(_short(poll['question'], 60))}»?\n\n{what}",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("poll_del_go:"))
async def poll_delete_go(callback: types.CallbackQuery, state: FSMContext):
    pid = _parse_id(callback.data)
    poll = await get_poll(pid) if pid is not None else None
    if poll is None:
        await callback.answer("Опрос уже удалён.", show_alert=True)
        return
    if await _poll_out_of_scope(callback.from_user.id, poll):
        await callback.answer(_OUT_OF_SCOPE, show_alert=True)
        return
    if poll["status"] == "scheduled":
        cancel_poll_job(pid)
    await delete_poll(pid)
    await callback.answer("Удалено")
    text, kb = await render_poll_list(callback.from_user.id, closed=False)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# Мастер создания регистрируется ПОСЛЕ экранов списка/карточки — независимо от того, какой
# модуль импортирован первым (см. docstring и прецедент admin_gamification → admin_game_tasks).
from handlers import admin_poll_wizard  # noqa: E402, F401
