"""«🛍 Магазин баллов» (раздел «🎮 Геймификация») и команда /shop: витрина, стойка выдачи, рейтинг.

INVARIANT: каждый @router.* — декоратор в ОДНУ строку (golden-снапшот роутеров читает их построчно).

Стойка: «🛒 Выдать товар» → @username или фамилия делегата → карточка с балансом и кнопками
товаров → подтверждение → баллы и остаток списываются одной транзакцией (`shop_db.sell`),
делегату приходит чек. Ошибочную выдачу отменяют кнопкой «↩️ Отменить выдачу» с подтверждением.

Защита от двойного нажатия «✅ Выдать»: кнопка несёт одноразовый номер из FSM-данных, номер
забирается под замком — второй тап получает «уже выдано», а не второе списание.

Товары, цены, остатки, лимит в одни руки — кнопками; витрина у делегатов включается тумблером.
Рейтинг баллов по убыванию — только здесь, в админке, плюс выгрузки в CSV.

Право — `moderate_game` («🎮 Менеджер геймификации»): его и выдают людям на стойке. Шов той же
формы, что соседние: хендлеры на общем `admin.router`, импорт хвостом `handlers/admin.py`."""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import secrets

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from database import shop_db
from database.db import get_balance, get_user
from handlers.access.admin_caps import has_capability
from handlers.admin import router
from handlers.states import ShopAdmin
from services.access.person_search import search_people
from services.infra.ru_plural import points_word

logger = logging.getLogger(__name__)

_CAP = "moderate_game"
_NO_ACCESS = "Недостаточно прав: нужна роль «🎮 Менеджер геймификации»."
_PAGE = 25
_sell_lock = asyncio.Lock()


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


async def shop_title() -> str:
    from domain.settings.schema import get_setting_typed

    event = (await get_setting_typed("event_name") or "").strip()
    return f"{event}-шоп" if event else "Магазин баллов"


def _who(user: dict | None, tid: int) -> str:
    from services.game.shop_card import who

    return who(user, tid)


def _stock(item: dict) -> str:
    if item["stock_left"] is None:
        return "без ограничения"
    return f"осталось {item['stock_left']} из {item['stock_total']}"


async def _edit_or_answer(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── главный экран ──────────────────────────────────────────────────────────────────────

async def home_text_kb() -> tuple[str, InlineKeyboardMarkup]:
    items = await shop_db.list_items()
    active = [i for i in items if i["active"]]
    stats = await shop_db.sales_stats()
    is_open = await shop_db.is_open()
    lines = [
        f"🛍 <b>{html.escape(await shop_title())}</b>", "",
        f"Витрина у делегатов: {'✅ видна' if is_open else '❌ скрыта'} (на экране «🪙 Мои баллы»)",
        f"Выдано: {stats['count']}, потрачено {stats['spent']} {points_word(stats['spent'])}", "",
    ]
    if active:
        lines.append("<b>На витрине</b>")
        for i in active:
            lines.append(f"• {html.escape(i['title'])} — {i['price']} · {_stock(i)}")
    else:
        lines.append("Товаров пока нет — добавьте их в «📦 Товары».")
    lines += ["", "На стойке: «🛒 Выдать товар» → ник или фамилия делегата → товар. Быстрый вход — команда /shop."]
    kb = _kb([
        [("🛒 Выдать товар", "shp_sell")],
        [("📦 Товары", "shp_items"), ("🏆 Рейтинг баллов", "shp_top:0")],
        [("🧾 Последние выдачи", "shp_sales:0")],
        [("🙈 Скрыть витрину у делегатов" if is_open else "👁 Показать витрину делегатам", "shp_open")],
        [("◀️ Назад", "admin_sec:game")],
    ])
    return "\n".join(lines), kb


@router.message(Command("shop"))
async def shop_command(message: types.Message, state: FSMContext):
    if not await has_capability(message.from_user.id, _CAP):
        await message.answer(_NO_ACCESS)
        return
    await state.set_state(None)
    text, kb = await home_text_kb()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin_shop")
async def shop_home(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    text, kb = await home_text_kb()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "shp_home")
async def shop_home_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    text, kb = await home_text_kb()
    await _edit_or_answer(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "shp_open")
async def shop_toggle_open(callback: types.CallbackQuery):
    now = not await shop_db.is_open()
    if now and not await shop_db.list_items(active_only=True):
        await callback.answer("Сначала добавьте хотя бы один товар в «📦 Товары».", show_alert=True)
        return
    await shop_db.set_open(now)
    text, kb = await home_text_kb()
    await _edit_or_answer(callback, text, kb)
    await callback.answer("Витрина видна делегатам" if now else "Витрина скрыта")


# ── стойка: найти делегата ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "shp_sell")
async def shop_sell_start(callback: types.CallbackQuery, state: FSMContext):
    if not await shop_db.list_items(active_only=True):
        await callback.answer("На витрине нет товаров — добавьте их в «📦 Товары».", show_alert=True)
        return
    await state.set_state(ShopAdmin.find)
    await callback.message.answer(
        "Кому выдаём? Пришлите @username делегата или его фамилию.\n\nПример: <code>@ivanova</code> или <code>Иванова</code>",
        parse_mode="HTML", reply_markup=_kb([[("◀️ В магазин", "shp_home")]]),
    )
    await callback.answer()


@router.message(ShopAdmin.find, F.text)
async def shop_find_input(message: types.Message, state: FSMContext):
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("Пришлите @username или фамилию — хотя бы 2 буквы.")
        return
    found = [p for p in await search_people(query, limit=10, include_started=False) if p["source"] == "users"]
    if not found:
        await message.answer(
            "Никого не нашёл. Проверьте написание или попросите показать @username в Телеграме.",
            reply_markup=_kb([[("◀️ В магазин", "shp_home")]]),
        )
        return
    if len(found) == 1:
        await state.set_state(None)
        text, kb = await card_text_kb(found[0]["user_id"], state)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    rows = []
    for p in found:
        nick = f" · @{p['username'].lstrip('@')}" if p.get("username") else ""
        rows.append([(((p.get("full_name") or "—") + nick)[:60], f"shp_u:{p['user_id']}")])
    rows.append([("◀️ В магазин", "shp_home")])
    await message.answer("Нашёл несколько — выберите:", reply_markup=_kb(rows))


async def card_text_kb(tid: int, state: FSMContext) -> tuple[str, InlineKeyboardMarkup]:
    from services.game.shop_card import card

    await state.update_data(shp_nonce=None)
    text, rows = await card(tid)
    return text, _kb(rows)


@router.callback_query(F.data.startswith("shp_u:"))
async def shop_card(callback: types.CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":", 1)[1])
    await state.set_state(None)
    text, kb = await card_text_kb(tid, state)
    await _edit_or_answer(callback, text, kb)
    await callback.answer()


# ── стойка: выбрать товар, подтвердить, выдать ─────────────────────────────────────────

@router.callback_query(F.data.startswith("shp_p:"))
async def shop_pick(callback: types.CallbackQuery, state: FSMContext):
    _, tid_raw, item_raw = callback.data.split(":")
    tid, item_id = int(tid_raw), int(item_raw)
    item = await shop_db.get_item(item_id)
    balance = await get_balance(tid)
    if item is None or not item["active"]:
        await callback.answer("Этого товара уже нет на витрине.", show_alert=True)
        return
    if item["stock_left"] is not None and item["stock_left"] <= 0:
        await callback.answer(f"«{item['title']}» закончился.", show_alert=True)
        return
    if item["price"] > balance:
        await callback.answer(f"Не хватает баллов: нужно {item['price']}, на счету {balance}.", show_alert=True)
        return
    if item["per_person"]:
        bought = await shop_db.count_bought(tid, item_id)
        if bought >= item["per_person"]:
            await callback.answer(f"Не больше {item['per_person']} в одни руки — уже выдано {bought}.", show_alert=True)
            return
    nonce = secrets.token_hex(4)
    await state.update_data(shp_nonce=nonce, shp_tid=tid, shp_item=item_id)
    user = await get_user(tid)
    text = (
        f"Выдать <b>{html.escape(item['title'])}</b>\n{_who(user, tid)}\n\n"
        f"Спишем {item['price']} {points_word(item['price'])}, останется {balance - item['price']}."
    )
    await _edit_or_answer(callback, text, _kb([[("✅ Выдать", f"shp_go:{nonce}")], [("◀️ Назад", f"shp_u:{tid}")]]))
    await callback.answer()


@router.callback_query(F.data.startswith("shp_go:"))
async def shop_go(callback: types.CallbackQuery, state: FSMContext):
    nonce = callback.data.split(":", 1)[1]
    async with _sell_lock:
        data = await state.get_data()
        if not nonce or data.get("shp_nonce") != nonce:
            await callback.answer("Эта выдача уже проведена или устарела — откройте карточку делегата заново.", show_alert=True)
            return
        await state.update_data(shp_nonce=None)
        tid, item_id = data["shp_tid"], data["shp_item"]
        try:
            sale = await shop_db.sell(item_id, tid, callback.from_user.id)
        except shop_db.SaleError as e:
            await callback.answer(e.human, show_alert=True)
            return
    user = await get_user(tid)
    text = (
        f"✅ Выдано: <b>{html.escape(sale['title'])}</b>\n{_who(user, tid)}\n\n"
        f"Списано {sale['price']}, на счету {sale['balance']} {points_word(sale['balance'])}."
    )
    await _edit_or_answer(callback, text, _kb([
        [("🛒 Ещё товар ему же", f"shp_u:{tid}")],
        [("🔎 Следующий делегат", "shp_sell")],
        [("↩️ Отменить выдачу", f"shp_undo:{sale['id']}")],
    ]))
    await callback.answer("Выдано")
    try:
        await callback.bot.send_message(
            tid,
            f"🛍 {html.escape(await shop_title())}: «{html.escape(sale['title'])}» — списано {sale['price']} {points_word(sale['price'])}.\n"
            f"На счету: {sale['balance']}.",
        )
    except Exception as e:
        logger.info(f"shop_go: чек делегату {tid} не ушёл: {type(e).__name__}")
    _resync()


def _resync() -> None:
    try:
        from services.game.game_sync import request_resync

        request_resync()
    except Exception as e:
        logger.warning(f"shop: пересборка вкладок геймы не запрошена: {type(e).__name__}")


@router.callback_query(F.data.startswith("shp_undo:"))
async def shop_undo(callback: types.CallbackQuery):
    sale = await shop_db.get_sale(int(callback.data.split(":", 1)[1]))
    if sale is None or sale["undone_at"]:
        await callback.answer("Эта выдача уже отменена.", show_alert=True)
        return
    user = await get_user(sale["telegram_id"])
    await callback.message.answer(
        f"Отменить выдачу <b>{html.escape(sale['title'])}</b> — {_who(user, sale['telegram_id'])}?\n\n"
        f"Вернём делегату {sale['price']} {points_word(sale['price'])}, товар вернётся в остаток.",
        parse_mode="HTML",
        reply_markup=_kb([[("↩️ Да, отменить", f"shp_undogo:{sale['id']}"), ("Не отменять", "shp_home")]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shp_undogo:"))
async def shop_undo_go(callback: types.CallbackQuery):
    sale = await shop_db.undo_sale(int(callback.data.split(":", 1)[1]), callback.from_user.id)
    if sale is None:
        await callback.answer("Эта выдача уже отменена.", show_alert=True)
        return
    balance = await get_balance(sale["telegram_id"])
    await _edit_or_answer(
        callback,
        f"↩️ Выдача «{html.escape(sale['title'])}» отменена, {sale['price']} {points_word(sale['price'])} вернули. На счету: {balance}.",
        _kb([[("◀️ В магазин", "shp_home")]]),
    )
    await callback.answer("Отменено")
    try:
        await callback.bot.send_message(
            sale["telegram_id"],
            f"🛍 {html.escape(await shop_title())}: выдачу «{html.escape(sale['title'])}» отменили, {sale['price']} {points_word(sale['price'])} вернулись. На счету: {balance}.",
        )
    except Exception as e:
        logger.info(f"shop_undo: сообщение делегату не ушло: {type(e).__name__}")
    _resync()


# ── товары ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "shp_items")
async def shop_items(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    items = await shop_db.list_items()
    rows = [[(f"{'' if i['active'] else '🙈 '}{i['title']} · {i['price']}"[:60], f"shp_it:{i['id']}")] for i in items]
    rows.append([("➕ Добавить товар", "shp_new")])
    rows.append([("◀️ В магазин", "shp_home")])
    text = "📦 <b>Товары</b>\n\n" + ("Нажмите на товар, чтобы изменить." if items else "Пока пусто.") + "\n🙈 — снят с витрины."
    await _edit_or_answer(callback, text, _kb(rows))
    await callback.answer()


async def item_text_kb(item: dict) -> tuple[str, InlineKeyboardMarkup]:
    limit = f"{item['per_person']} в одни руки" if item["per_person"] else "без лимита"
    text = (
        f"📦 <b>{html.escape(item['title'])}</b>\n\n"
        f"Цена: {item['price']} {points_word(item['price'])}\n"
        f"Количество: {_stock(item)}\n"
        f"Лимит: {limit}\n"
        f"На витрине: {'✅ да' if item['active'] else '🙈 снят'}"
    )
    iid = item["id"]
    kb = _kb([
        [("✏️ Название", f"shp_ed:{iid}:title"), ("💰 Цена", f"shp_ed:{iid}:price")],
        [("📦 Количество", f"shp_ed:{iid}:stock"), ("👤 Лимит в одни руки", f"shp_ed:{iid}:per_person")],
        [("🙈 Снять с витрины" if item["active"] else "👁 Вернуть на витрину", f"shp_act:{iid}")],
        [("🗑 Удалить", f"shp_del:{iid}")],
        [("◀️ К товарам", "shp_items")],
    ])
    return text, kb


@router.callback_query(F.data.startswith("shp_it:"))
async def shop_item(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    item = await shop_db.get_item(int(callback.data.split(":", 1)[1]))
    if item is None:
        await callback.answer("Товар уже удалён.", show_alert=True)
        return
    text, kb = await item_text_kb(item)
    await _edit_or_answer(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("shp_act:"))
async def shop_item_toggle(callback: types.CallbackQuery):
    item = await shop_db.get_item(int(callback.data.split(":", 1)[1]))
    if item is None:
        await callback.answer("Товар уже удалён.", show_alert=True)
        return
    await shop_db.update_item(item["id"], active=0 if item["active"] else 1)
    text, kb = await item_text_kb(await shop_db.get_item(item["id"]))
    await _edit_or_answer(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("shp_del:"))
async def shop_item_delete(callback: types.CallbackQuery):
    item = await shop_db.get_item(int(callback.data.split(":", 1)[1]))
    if item is None:
        await callback.answer("Товар уже удалён.", show_alert=True)
        return
    await callback.message.answer(
        f"Удалить «{html.escape(item['title'])}»? Он пропадёт из витрины и списка товаров.\n\n"
        "Если этот товар уже выдавали, удалить нельзя — только снять с витрины (выдачи остаются в истории).",
        parse_mode="HTML",
        reply_markup=_kb([[("🗑 Да, удалить", f"shp_delgo:{item['id']}"), ("Отмена", f"shp_it:{item['id']}")]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shp_delgo:"))
async def shop_item_delete_go(callback: types.CallbackQuery):
    item_id = int(callback.data.split(":", 1)[1])
    if not await shop_db.delete_item(item_id):
        await callback.answer("Этот товар уже выдавали — удалить нельзя. Снимите его с витрины.", show_alert=True)
        return
    await callback.answer("Удалено")
    await _edit_or_answer(callback, "🗑 Товар удалён.", _kb([[("◀️ К товарам", "shp_items")]]))


# ── добавление и правка товара (текстовый ввод) ────────────────────────────────────────

_PROMPTS = {
    "title": "Название товара — как его увидят делегаты.\n\nПример: Шоппер Юлида",
    "price": "Цена в баллах — целое число.\n\nПример: 150",
    "stock": "Сколько штук всего? Целое число, или «-» — без ограничения.\n\nПример: 30",
    "per_person": "Сколько штук можно одному человеку? Целое число, или «-» — без лимита.\n\nПример: 1",
}
_NEW_STEPS = ["title", "price", "stock", "per_person"]


def _parse(field: str, raw: str):
    """Значение поля или `ValueError` с текстом, что поправить."""
    raw = raw.strip()
    if field == "title":
        if not 2 <= len(raw) <= 60:
            raise ValueError("Название — от 2 до 60 символов.")
        return raw
    if field in ("stock", "per_person") and raw == "-":
        return None
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError("Нужно целое число больше нуля" + (", или «-»." if field != "price" else "."))
    return int(raw)


@router.callback_query(F.data == "shp_new")
async def shop_new(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ShopAdmin.new_item)
    await state.update_data(shp_new={}, shp_step=0)
    await callback.message.answer("➕ Новый товар, шаг 1 из 4.\n\n" + _PROMPTS["title"], reply_markup=_kb([[("◀️ Отмена", "shp_items")]]))
    await callback.answer()


@router.message(ShopAdmin.new_item, F.text)
async def shop_new_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    step, draft = data.get("shp_step", 0), dict(data.get("shp_new") or {})
    field = _NEW_STEPS[step]
    try:
        draft[field] = _parse(field, message.text)
    except ValueError as e:
        await message.answer(f"Не получилось: {e}\n\n{_PROMPTS[field]}")
        return
    step += 1
    if step < len(_NEW_STEPS):
        await state.update_data(shp_new=draft, shp_step=step)
        await message.answer(f"Шаг {step + 1} из 4.\n\n{_PROMPTS[_NEW_STEPS[step]]}", reply_markup=_kb([[("◀️ Отмена", "shp_items")]]))
        return
    await state.set_state(None)
    item_id = await shop_db.add_item(draft["title"], draft["price"], draft["stock"], draft["per_person"])
    text, kb = await item_text_kb(await shop_db.get_item(item_id))
    await message.answer("✅ Товар добавлен.\n\n" + text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("shp_ed:"))
async def shop_edit(callback: types.CallbackQuery, state: FSMContext):
    _, item_raw, field = callback.data.split(":")
    if field not in _PROMPTS:
        await callback.answer("Не понял, что меняем — откройте товар заново.", show_alert=True)
        return
    await state.set_state(ShopAdmin.edit_item)
    await state.update_data(shp_item=int(item_raw), shp_field=field)
    await callback.message.answer(_PROMPTS[field], reply_markup=_kb([[("◀️ Отмена", f"shp_it:{item_raw}")]]))
    await callback.answer()


@router.message(ShopAdmin.edit_item, F.text)
async def shop_edit_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    item_id, field = data.get("shp_item"), data.get("shp_field")
    item = await shop_db.get_item(item_id) if item_id else None
    if item is None or field not in _PROMPTS:
        await state.set_state(None)
        await message.answer("Товар не найден — откройте «📦 Товары» заново.")
        return
    try:
        value = _parse(field, message.text)
    except ValueError as e:
        await message.answer(f"Не получилось: {e}\n\n{_PROMPTS[field]}")
        return
    if field == "stock":
        await shop_db.set_stock(item_id, value)
    else:
        await shop_db.update_item(item_id, **{field: value})
    await state.set_state(None)
    text, kb = await item_text_kb(await shop_db.get_item(item_id))
    await message.answer("✅ Сохранено.\n\n" + text, parse_mode="HTML", reply_markup=kb)


# ── рейтинг и выдачи ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("shp_top:"))
async def shop_top(callback: types.CallbackQuery):
    offset = max(0, int(callback.data.split(":", 1)[1]))
    total = await shop_db.leaderboard_count()
    rows = await shop_db.leaderboard(_PAGE, offset)
    lines = [f"🏆 <b>Рейтинг баллов</b> — {total} чел.", "Баланс = задания + чат + перенос из таблицы − покупки.", ""]
    for n, r in enumerate(rows, start=offset + 1):
        nick = f" @{html.escape(r['username'].lstrip('@'))}" if r.get("username") else ""
        spent = f" (потратил {r['spent']})" if r["spent"] else ""
        lines.append(f"{n}. {html.escape(r.get('full_name') or str(r['telegram_id']))}{nick} — <b>{r['balance']}</b>{spent}")
    nav = []
    if offset > 0:
        nav.append(("← Выше", f"shp_top:{max(0, offset - _PAGE)}"))
    if offset + _PAGE < total:
        nav.append(("Ниже →", f"shp_top:{offset + _PAGE}"))
    kb_rows = [nav] if nav else []
    kb_rows += [[("📥 Весь рейтинг в CSV", "shp_topcsv")], [("◀️ В магазин", "shp_home")]]
    await _edit_or_answer(callback, "\n".join(lines), _kb(kb_rows))
    await callback.answer()


def _csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


@router.callback_query(F.data == "shp_topcsv")
async def shop_top_csv(callback: types.CallbackQuery):
    rows = await shop_db.leaderboard(100_000, 0)
    data = _csv(
        ["Место", "Имя", "Username", "Telegram ID", "Баланс", "Потрачено в магазине"],
        [[n, r.get("full_name") or "", r.get("username") or "", r["telegram_id"], r["balance"], r["spent"]]
         for n, r in enumerate(rows, start=1)],
    )
    await callback.message.answer_document(BufferedInputFile(data, filename="rating.csv"), caption="🏆 Рейтинг баллов")
    await callback.answer()


@router.callback_query(F.data.startswith("shp_sales:"))
async def shop_sales(callback: types.CallbackQuery):
    offset = max(0, int(callback.data.split(":", 1)[1]))
    sales = await shop_db.list_sales(20, offset)
    lines = ["🧾 <b>Последние выдачи</b>", ""]
    if not sales:
        lines.append("Пока ничего не выдавали.")
    for s in sales:
        when = (s["sold_at"] or "")[5:16].replace("-", ".")
        who = html.escape(s.get("full_name") or str(s["telegram_id"]))
        mark = " ↩️ отменена" if s["undone_at"] else ""
        lines.append(f"{when} · {who} — {html.escape(s['title'])} ({s['price']}){mark}")
    nav = []
    if offset > 0:
        nav.append(("← Новее", f"shp_sales:{max(0, offset - 20)}"))
    if len(sales) == 20:
        nav.append(("Старше →", f"shp_sales:{offset + 20}"))
    kb_rows = [nav] if nav else []
    kb_rows += [[("📥 Все выдачи в CSV", "shp_salescsv")], [("◀️ В магазин", "shp_home")]]
    await _edit_or_answer(callback, "\n".join(lines), _kb(kb_rows))
    await callback.answer()


@router.callback_query(F.data == "shp_salescsv")
async def shop_sales_csv(callback: types.CallbackQuery):
    sales = await shop_db.list_sales(100_000, 0)
    data = _csv(
        ["Когда", "Имя", "Username", "Telegram ID", "Товар", "Баллов", "Выдал (ID)", "Отменена"],
        [[s["sold_at"], s.get("full_name") or "", s.get("username") or "", s["telegram_id"], s["title"],
          s["price"], s["sold_by"], s["undone_at"] or ""] for s in sales],
    )
    await callback.message.answer_document(BufferedInputFile(data, filename="shop_sales.csv"), caption="🧾 Выдачи магазина")
    await callback.answer()
