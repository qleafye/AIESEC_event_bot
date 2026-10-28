"""Админка анкеты после отправки: карточка, итоги по вопросам, свободные ответы, копирование.

Шов на общий `admin.router`. Подключается хвостовым импортом из admin_polls.py (как мастер
опросов), чтобы порядок регистрации не зависел от порядка импорта. Право — `broadcast`
(psv_* в admin_caps.py). Все экраны проверяют город менеджера через `_poll_out_of_scope`.

INVARIANT (13-01 cap-test): каждый декоратор `@router.*` — в ОДНУ строку.
"""
import html as html_module

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import POLL_STATUS_LABELS, count_poll_deliveries, get_poll
from database.survey_db import copy_survey, count_survey_progress, get_survey_question
from handlers.admin import router
from handlers.comms.admin_polls import _OUT_OF_SCOPE, _fmt_dt, _poll_out_of_scope
from handlers.comms.admin_survey_builder import render_survey_draft
from services.comms import survey_report
from services.comms.polls import audience_label, results_hint_line

_GONE = "Анкета уже удалена."


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _esc(value) -> str:
    return html_module.escape(str(value or ""))


def _ints(data: str, n: int) -> list[int] | None:
    """Числа после префикса callback'а: 'psv_res:5:0' -> [5, 0]. None — разбор не удался."""
    parts = data.split(":")[1:]
    if len(parts) < n:
        return None
    try:
        return [int(p) for p in parts[:n]]
    except ValueError:
        return None


async def render_survey_card(poll: dict) -> tuple[str, InlineKeyboardMarkup]:
    pid = poll["id"]
    status = POLL_STATUS_LABELS.get(poll["status"], poll["status"])
    started, finished = await count_survey_progress(pid)
    ok, failed = await count_poll_deliveries(pid)
    lines = [
        f"📋 <b>{_esc(poll['question'])}</b>",
        f"{status} · анкета",
        f"Кому: {_esc(audience_label(poll.get('audience')))}",
    ]
    if poll["status"] == "scheduled":
        lines.append(f"Отправка: {_fmt_dt(poll.get('scheduled_at'))}")
    else:
        lines.append(f"Доставлено: {ok} · недоступно: {failed}")
        lines.append(f"Начали: {started} · Закончили: {finished}")
        hint = await results_hint_line()
        if hint:
            lines.extend(["", hint])
    rows = []
    if poll["status"] in ("open", "closed"):
        rows.append([_btn("📊 Итоги по вопросам", f"psv_res:{pid}:0")])
        rows.append([_btn("📄 В таблицу", f"poll_export:{pid}")])
    rows.append([_btn("📄 Копировать опрос", f"psv_copy:{pid}")])
    if poll["status"] == "open":
        rows.append([_btn("⏹ Закрыть", f"poll_close:{pid}")])
    del_text = "❌ Отменить отправку" if poll["status"] == "scheduled" else "🗑 Удалить"
    rows.append([_btn(del_text, f"poll_del:{pid}")])
    rows.append([_btn("⬅️ Назад", "admin_polls")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(callback: types.CallbackQuery, text: str, kb) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _load_survey(callback: types.CallbackQuery, pid: int):
    """Анкета своего города или None (алерт уже показан)."""
    poll = await get_poll(pid)
    if poll is None or poll.get("kind") != "survey":
        await callback.answer(_GONE, show_alert=True)
        return None
    if await _poll_out_of_scope(callback.from_user.id, poll):
        await callback.answer(_OUT_OF_SCOPE, show_alert=True)
        return None
    return poll


def _nav(prefix: str, key: int, page: int, pages: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(_btn("◀️", f"{prefix}:{key}:{page - 1}"))
    if page < pages - 1:
        row.append(_btn("▶️", f"{prefix}:{key}:{page + 1}"))
    return row


@router.callback_query(F.data.startswith("psv_res:"))
async def survey_results(callback: types.CallbackQuery):
    nums = _ints(callback.data, 2)
    if nums is None:
        await callback.answer("Не удалось открыть итоги — вернитесь к карточке анкеты.", show_alert=True)
        return
    pid, page = nums
    poll = await _load_survey(callback, pid)
    if poll is None:
        return
    results = await survey_report.build_survey_results(pid)
    started, finished = await count_survey_progress(pid)
    pages = survey_report.render_results_pages(poll, results, started, finished)
    page = max(0, min(page, len(pages) - 1))
    rows = []
    nav = _nav("psv_res", pid, page, len(pages))
    if nav:
        rows.append(nav)
    for k, item in enumerate(results, start=1):
        if item["type"] == "text" or item.get("texts"):
            rows.append([_btn(f"💬 Ответы: {k}", f"psv_txt:{item['q']['id']}:0")])
    rows.append([_btn("⬅️ К карточке", f"poll_card:{pid}")])
    await _edit(callback, pages[page], InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("psv_txt:"))
async def survey_text_answers(callback: types.CallbackQuery):
    nums = _ints(callback.data, 2)
    q = await get_survey_question(nums[0]) if nums else None
    if q is None:
        await callback.answer("Вопрос не найден — вернитесь к карточке анкеты.", show_alert=True)
        return
    pid = q["poll_id"]
    if await _load_survey(callback, pid) is None:
        return
    text, pages = await survey_report.render_text_answers_page(pid, q["id"], nums[1])
    page = max(0, min(nums[1], pages - 1))
    rows = []
    nav = _nav("psv_txt", q["id"], page, pages)
    if nav:
        rows.append(nav)
    rows.append([_btn("⬅️ К итогам", f"psv_res:{pid}:0")])
    await _edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("psv_copy:"))
async def survey_copy(callback: types.CallbackQuery):
    nums = _ints(callback.data, 1)
    if nums is None:
        await callback.answer("Не удалось скопировать — откройте карточку анкеты заново.", show_alert=True)
        return
    poll = await _load_survey(callback, nums[0])
    if poll is None:
        return
    new_id = await copy_survey(nums[0], callback.from_user.id)
    screen = await render_survey_draft(new_id)
    await callback.answer("Копия создана — это черновик, исходная анкета не тронута.")
    await _edit(callback, *screen)
