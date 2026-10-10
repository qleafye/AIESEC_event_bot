"""«📥 Перенос баллов из таблицы» (раздел «🎮 Геймификация»): разовый перенос старых баллов,
которые гейм-менеджеры вели в Google-таблице до бота, в монеты бота.

Ход: ссылка на таблицу → (если вкладок несколько и в ссылке нет gid) выбор вкладки кнопкой →
предпросмотр: сколько людей и монет, кого не нашли в боте, кому перенос уже был → «✅ Перенести»
(с сообщением каждому или без). Повторный запуск безопасен: перенесённым второй раз не
начисляется, так что после правки ников в таблице можно запустить ещё раз.

Сама логика — `services/game/coins_transfer.py`. Шов той же формы, что соседние: своего `Router()`
нет, хендлеры на общем `admin.router`, модуль импортируется хвостом `handlers/admin.py`.
"""
import asyncio
import html
import logging

from aiogram import F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_balance
from services.infra.ru_plural import points_word  # «1 балл», «5 баллов» в текстах менеджеру
from handlers.admin import router
from handlers.states import CoinsTransfer
from services.game import coins_transfer
from services.ext_forms import ext_forms_google as gsheet

logger = logging.getLogger(__name__)

_LIST_LIMIT = 40

_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✖️ Отмена", callback_data="cointr_cancel")],
])


def _intro() -> str:
    email = gsheet.service_account_email()
    access = (
        f"Если таблица закрыта, откройте доступ «Читатель» для адреса <code>{html.escape(email)}</code>."
        if email else ""
    )
    return (
        "📥 <b>Перенос баллов из таблицы</b>\n\n"
        "Перенесу в бот баллы, которые вы считали в Google-таблице. Нужна вкладка, где в одной "
        "колонке @ник делегата, а в остальных — баллы (например, по заданиям). Баллы одного ника "
        "складываются, человек находится по @нику.\n\n"
        "Сначала покажу, кому и сколько начислю, — без подтверждения ничего не запишется.\n\n"
        "Пришлите ссылку на вкладку — откройте её в браузере и скопируйте адрес, например:\n"
        "https://docs.google.com/spreadsheets/d/1AbC…/edit#gid=123\n\n"
        f"{access}"
    ).strip()


@router.callback_query(F.data == "admin_coins_transfer")
async def admin_coins_transfer(callback: types.CallbackQuery, state: FSMContext):
    await state.set_data({})
    await state.set_state(CoinsTransfer.link)
    await callback.message.answer(_intro(), parse_mode="HTML", reply_markup=_CANCEL_KB,
                                  disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "cointr_cancel")
async def cointr_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await state.set_data({})
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Перенос отменён — ничего не записано.")


@router.message(StateFilter(CoinsTransfer), Command("cancel"))
@router.message(StateFilter(CoinsTransfer), F.text == "Отмена")
async def cointr_cancel_text(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await state.set_data({})
    await message.answer("Перенос отменён — ничего не записано.")


@router.message(CoinsTransfer.link)
async def cointr_link(message: types.Message, state: FSMContext):
    parsed = gsheet.parse_sheet_url(message.text or "")
    if parsed is None:
        await message.answer(
            "Не понял ссылку. Пришлите адрес вкладки из браузера — он начинается с "
            "https://docs.google.com/spreadsheets/d/…",
            reply_markup=_CANCEL_KB,
        )
        return
    sheet_id, gid = parsed
    try:
        title, tabs = await gsheet.list_tabs(sheet_id)
    except gsheet.GoogleFormError as e:
        await message.answer(f"Не открыл таблицу: {e.human}.", reply_markup=_CANCEL_KB)
        return
    await state.update_data(sheet_id=sheet_id)
    if gid is None and len(tabs) == 1:
        gid = tabs[0][0]
    if gid is None:
        rows = [[InlineKeyboardButton(text=name[:60], callback_data=f"cointr_tab:{tab_gid}")]
                for tab_gid, name in tabs[:60]]
        rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="cointr_cancel")])
        await message.answer(
            f"В таблице «{html.escape(title)}» несколько вкладок. На какой баллы?",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return
    tab_name = next((name for tab_gid, name in tabs if tab_gid == gid), None)
    if tab_name is None:
        await message.answer("В таблице нет такой вкладки — проверьте ссылку.", reply_markup=_CANCEL_KB)
        return
    await _preview(message, state, sheet_id, gid, tab_name)


@router.callback_query(F.data.startswith("cointr_tab:"))
async def cointr_tab(callback: types.CallbackQuery, state: FSMContext):
    sheet_id = (await state.get_data()).get("sheet_id")
    if not sheet_id:
        await callback.answer("Начните перенос заново", show_alert=True)
        return
    gid = int(callback.data.split(":", 1)[1])
    await callback.answer()
    try:
        _, tabs = await gsheet.list_tabs(sheet_id)
    except gsheet.GoogleFormError as e:
        await callback.message.answer(f"Не открыл таблицу: {e.human}.", reply_markup=_CANCEL_KB)
        return
    tab_name = next((name for tab_gid, name in tabs if tab_gid == gid), None)
    if tab_name is None:
        await callback.message.answer("Этой вкладки больше нет — пришлите ссылку ещё раз.",
                                      reply_markup=_CANCEL_KB)
        return
    await _preview(callback.message, state, sheet_id, gid, tab_name)


async def _load_plan(sheet_id: str, gid: int):
    values = await gsheet.read_values(sheet_id, gid)
    parsed = coins_transfer.parse_values(values)
    if parsed is None:
        return None, None
    return parsed, await coins_transfer.build_plan(parsed)


def _nick_list(people) -> str:
    shown = [f"@{html.escape(p.nick)}" for p in people[:_LIST_LIMIT]]
    rest = len(people) - len(shown)
    return ", ".join(shown) + (f" и ещё {rest}" if rest > 0 else "")


async def _preview(target: types.Message, state: FSMContext, sheet_id: str, gid: int, tab_name: str):
    try:
        parsed, plan = await _load_plan(sheet_id, gid)
    except gsheet.GoogleFormError as e:
        await target.answer(f"Не прочитал вкладку: {e.human}.", reply_markup=_CANCEL_KB)
        return
    if parsed is None:
        await target.answer(
            f"На вкладке «{html.escape(tab_name)}» не нашёл колонку с @никами. Нужна колонка с "
            "заголовком «Ник» или с никами вида @username — пришлите ссылку на другую вкладку.",
            parse_mode="HTML", reply_markup=_CANCEL_KB,
        )
        return
    if not parsed.people:
        await target.answer(
            f"На вкладке «{html.escape(tab_name)}» нет ни одного ника с баллами — переносить нечего.",
            parse_mode="HTML", reply_markup=_CANCEL_KB,
        )
        return
    await state.update_data(sheet_id=sheet_id, gid=gid, tab_name=tab_name)

    lines = [f"📥 <b>Перенос с вкладки «{html.escape(tab_name)}»</b>", ""]
    lines.append(f"В таблице: {len(parsed.people)} ников с баллами.")
    lines.append(f"✅ Начислю: <b>{len(plan.matched)}</b> делегатам, всего <b>{plan.total}</b> {points_word(plan.total)}.")
    for person, _ in plan.matched[:10]:
        lines.append(f"  · @{html.escape(person.nick)} — +{person.total}")
    if len(plan.matched) > 10:
        lines.append(f"  · …и ещё {len(plan.matched) - 10}")
    if plan.already:
        lines.append("")
        lines.append(f"⏭ Уже переносили раньше, второй раз не начислю: {len(plan.already)}.")
    if plan.unknown:
        lines.append("")
        lines.append(f"❓ Не нашёл в боте ({len(plan.unknown)}) — им ничего не начислится:")
        lines.append(_nick_list(plan.unknown))
        lines.append("Обычно это опечатка в нике или человек не подавал анкету. Поправьте ник в "
                     "таблице и запустите перенос ещё раз — уже перенесённым не задвоится.")
    if parsed.bad_rows:
        lines.append("")
        lines.append(f"⚠️ Строк с баллами без понятного ника: {parsed.bad_rows} — их пропущу.")
    lines.append("")
    lines.append("Баллы придут с причиной «перенос из таблицы» и списком заданий. Снять их потом "
                 "можно через «🪙 Баллы вручную».")

    rows = []
    if plan.matched:
        rows.append([InlineKeyboardButton(text="✅ Перенести и написать каждому",
                                          callback_data="cointr_go:notify")])
        rows.append([InlineKeyboardButton(text="✅ Перенести без сообщений",
                                          callback_data="cointr_go:silent")])
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="cointr_cancel")])
    await target.answer("\n".join(lines), parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("cointr_go:"))
async def cointr_go(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sheet_id, gid, tab_name = data.get("sheet_id"), data.get("gid"), data.get("tab_name")
    if not sheet_id or gid is None or not tab_name:
        await callback.answer("Перенос уже выполнен или отменён", show_alert=True)
        return
    notify = callback.data.endswith(":notify")
    await state.set_state(None)
    await state.set_data({})
    await callback.answer("Переношу…")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        parsed, plan = await _load_plan(sheet_id, gid)
    except gsheet.GoogleFormError as e:
        await callback.message.answer(f"Не прочитал вкладку: {e.human}. Ничего не записано.")
        return
    if parsed is None:
        await callback.message.answer("Вкладка изменилась — колонки с никами больше нет. Ничего не записано.")
        return
    done = await coins_transfer.apply_plan(plan, tab_name, callback.from_user.id)
    total = sum(p.total for p, _ in done)
    logger.info("coins_transfer: %s человек, %s монет, by=%s", len(done), total, callback.from_user.id)
    try:
        from services.game.game_sync import request_resync

        request_resync()
    except Exception as e:
        logger.warning("coins_transfer: пересборка вкладок геймы не запрошена: %s", type(e).__name__)

    delivered = 0
    if notify and done:
        from services.game.coins_notify import notify_manual_coins

        await callback.message.answer(f"Пишу делегатам ({len(done)})…")
        for person, tid in done:
            if await notify_manual_coins(callback.bot, tid, person.total, person.reason(tab_name),
                                         await get_balance(tid)):
                delivered += 1
            await asyncio.sleep(0.05)

    lines = [f"✅ Перенесено: {len(done)} делегатам, {total} {points_word(total)}."]
    if notify and done:
        lines.append(f"Сообщение получили: {delivered} из {len(done)}.")
    if plan.unknown:
        lines.append(f"Не нашёл в боте: {len(plan.unknown)} — после правки ников запустите перенос ещё раз.")
    lines.append("Все строки — в «📜 Ручные начисления».")
    await callback.message.answer("\n".join(lines))
