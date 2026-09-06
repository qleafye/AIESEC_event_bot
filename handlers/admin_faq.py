"""Quick 260906-8uq (FAQ-01..06): раздел менеджера «❓ Частые вопросы» — список пунктов,
карточка с правкой/порядком/городом/удалением, мастер добавления. Правило видимости пункта
делегату живёт ОДИН раз в `services/faq.py` — этот шов его не переопределяет, только пишет и
читает `faq_items` через аксессоры `database/db.py`.

Форма шва — Phase 13 (REFAC-01), точная копия `handlers/admin_questions.py`: своего `Router()`
нет, хендлеры декорируют ОБЩИЙ `handlers.admin.router`; каждый декоратор — в одну строку
(инвариант cap-теста 13-01). `admin_core` импортируется на уровне модуля (безопасно — не
создаёт цикл), `admin_sections` — лениво внутри функций (цикл на уровне модуля: admin_sections
импортирует admin_settings, тот — обратно к admin_core)."""
import html as html_module

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.db import (
    create_faq_item,
    delete_faq_item,
    get_faq_item,
    list_faq_items,
    reorder_faq_items,
    update_faq_item,
)
from handlers.admin import router
from handlers.admin_core import _admin_city_view
from handlers.states import FaqItem
from keyboards.builders import get_cancel_kb
from cities import ALL_CITIES, admin_selected_city, city_label
from services import faq as faq_service

FAQ_PAGE = 8


# T-FAQ-02 (стейл-клавиатура): перед КАЖДЫМ действием над пунктом перечитываем его
# `get_faq_item` и сверяем со scope шапки — клавиатура в чате живёт вечно, шапка города могла
# смениться (или пункт — уехать в другой город) между открытием карточки и тапом по кнопке.
def _card_out_of_scope(row: dict, scope) -> bool:
    """`scope` — дескриптор `_admin_city_view` (None = без ограничения: модуль выключен или
    шапка «Все города», пункт всегда в scope). Пункт «все города» (`city is None`) виден из
    ЛЮБОЙ шапки — та же семантика, что у делегатской видимости и у `_city_clause`."""
    if scope is None:
        return False
    code, exclude = scope
    city = row.get("city")
    if city is None:
        return False
    if exclude:
        return city in exclude
    return city != code


async def render_faq_screen(admin_id: int, offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Список пунктов в scope текущей шапки — пустой список не рисует пустое сообщение,
    а зовёт добавить первый пункт кнопкой (CLAUDE.md: разрушительная пустота объясняется,
    а не молчит)."""
    scope, label = await _admin_city_view(admin_id)
    items = await list_faq_items(city_scope=scope)

    lines = ["❓ <b>Частые вопросы</b>"]
    if label:
        lines.append(html_module.escape(str(label)))
    if items:
        lines.append(f"Всего пунктов: {len(items)}")
    else:
        lines.append("")
        lines.append("Пока ни одного пункта — добавьте первый кнопкой ниже.")
    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    page = items[offset: offset + FAQ_PAGE]
    for idx, item in enumerate(page, start=offset + 1):
        status_icon = "✅" if item.get("enabled") else "🚫"
        item_city = item.get("city")
        city_label_text = await city_label(item_city) if item_city else None
        badge = faq_service.city_badge(city_label_text)
        row_label = (
            f"{idx}. {status_icon} {badge} "
            f"{faq_service.short(str(item.get('question') or ''), 40)}"
        )
        buttons.append([InlineKeyboardButton(text=row_label, callback_data=f"afaq_v:{item['id']}")])

    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="afaq_new")])

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"afaq_p:{max(0, offset - FAQ_PAGE)}",
        ))
    if offset + FAQ_PAGE < len(items):
        nav_row.append(InlineKeyboardButton(
            text="➡️", callback_data=f"afaq_p:{offset + FAQ_PAGE}",
        ))
    if nav_row:
        buttons.append(nav_row)

    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    buttons.append([back_button("admin_faq")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_faq_card(admin_id: int, item_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """`None` — пункт удалили/скрыли из scope между рендерами (стейл-клавиатура); вызывающий
    отвечает алертом, не правкой."""
    scope, _label = await _admin_city_view(admin_id)
    row = await get_faq_item(item_id)
    if row is None or _card_out_of_scope(row, scope):
        return None

    header_code = await admin_selected_city(admin_id)

    row_city = row.get("city")
    city_label_text = await city_label(row_city) if row_city else None
    badge = faq_service.city_badge(city_label_text)
    status_text = "✅ показывается делегатам" if row.get("enabled") else "🚫 скрыт от делегатов"

    lines = [
        "❓ <b>Пункт FAQ</b>",
        f"Кому виден: {badge}",
        status_text,
        "",
        f"<b>Вопрос:</b> {html_module.escape(str(row.get('question') or ''))}",
        "",
        f"<b>Ответ:</b> {html_module.escape(str(row.get('answer') or ''))}",
    ]

    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✏️ Вопрос", callback_data=f"afaq_eq:{item_id}")],
        [InlineKeyboardButton(text="✏️ Ответ", callback_data=f"afaq_ea:{item_id}")],
        [
            InlineKeyboardButton(text="⬆️", callback_data=f"afaq_up:{item_id}"),
            InlineKeyboardButton(text="⬇️", callback_data=f"afaq_dn:{item_id}"),
        ],
        [InlineKeyboardButton(
            text=("🚫 Скрыть" if row.get("enabled") else "✅ Показать"),
            callback_data=f"afaq_t:{item_id}",
        )],
    ]

    # T-FAQ-01/CLAUDE.md: переключатель города только когда шапка называет КОНКРЕТНЫЙ город —
    # иначе непонятно, в какой город класть пункт, и кнопку заменяет строка-объяснение.
    if header_code and header_code != ALL_CITIES:
        if row_city == header_code:
            toggle_label = "🌍 Для всех городов"
        else:
            header_label = await city_label(header_code)
            toggle_label = f"🏙 Только {header_label}"
        buttons.append([InlineKeyboardButton(text=toggle_label, callback_data=f"afaq_c:{item_id}")])
    else:
        lines.append("")
        lines.append(
            "Переключение «свой город / все города» доступно, когда шапка панели указывает "
            "конкретный город."
        )

    buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"afaq_d:{item_id}")])
    buttons.append([InlineKeyboardButton(text="← К списку", callback_data="afaq_p:0")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def _parse_id(callback_data: str) -> int | None:
    try:
        return int(callback_data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


@router.callback_query(F.data == "admin_faq")
async def admin_faq(callback: types.CallbackQuery):
    text, kb = await render_faq_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("afaq_p:"))
async def afaq_page(callback: types.CallbackQuery):
    offset = _parse_id(callback.data)
    if offset is None or offset < 0:
        offset = 0
    text, kb = await render_faq_screen(callback.from_user.id, offset)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("afaq_v:"))
async def afaq_view(callback: types.CallbackQuery):
    item_id = _parse_id(callback.data)
    if item_id is None:
        await callback.answer("Пункт не найден.", show_alert=True)
        return
    screen = await render_faq_card(callback.from_user.id, item_id)
    if screen is None:
        await callback.answer("Пункт недоступен — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _afaq_move(callback: types.CallbackQuery, direction: int) -> None:
    """`direction` — -1 (вверх) или +1 (вниз). Перестановка меняет местами соседей ВНУТРИ
    видимого в этом scope списка и пишет позиции только этому подмножеству
    (`reorder_faq_items` — см. докстринг в database/db.py про доопределение порядка чужого
    города вторичным ключом `id`)."""
    item_id = _parse_id(callback.data)
    if item_id is None:
        await callback.answer("Пункт не найден.", show_alert=True)
        return
    scope, _label = await _admin_city_view(callback.from_user.id)
    row = await get_faq_item(item_id)
    if row is None or _card_out_of_scope(row, scope):
        await callback.answer("Пункт недоступен — обновите список.", show_alert=True)
        return
    items = await list_faq_items(city_scope=scope)
    ids = [r["id"] for r in items]
    if item_id in ids:
        idx = ids.index(item_id)
        new_idx = idx + direction
        if 0 <= new_idx < len(ids):
            ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
            await reorder_faq_items(ids)
    screen = await render_faq_card(callback.from_user.id, item_id)
    if screen is None:
        await callback.answer("Пункт недоступен — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("afaq_up:"))
async def afaq_move_up(callback: types.CallbackQuery):
    await _afaq_move(callback, -1)


@router.callback_query(F.data.startswith("afaq_dn:"))
async def afaq_move_down(callback: types.CallbackQuery):
    await _afaq_move(callback, 1)


@router.callback_query(F.data.startswith("afaq_t:"))
async def afaq_toggle_enabled(callback: types.CallbackQuery):
    item_id = _parse_id(callback.data)
    if item_id is None:
        await callback.answer("Пункт не найден.", show_alert=True)
        return
    scope, _label = await _admin_city_view(callback.from_user.id)
    row = await get_faq_item(item_id)
    if row is None or _card_out_of_scope(row, scope):
        await callback.answer("Пункт недоступен — обновите список.", show_alert=True)
        return
    await update_faq_item(item_id, enabled=0 if row.get("enabled") else 1)
    screen = await render_faq_card(callback.from_user.id, item_id)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("afaq_c:"))
async def afaq_toggle_city(callback: types.CallbackQuery):
    item_id = _parse_id(callback.data)
    if item_id is None:
        await callback.answer("Пункт не найден.", show_alert=True)
        return
    scope, _label = await _admin_city_view(callback.from_user.id)
    row = await get_faq_item(item_id)
    if row is None or _card_out_of_scope(row, scope):
        await callback.answer("Пункт недоступен — обновите список.", show_alert=True)
        return
    header_code = await admin_selected_city(callback.from_user.id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Сначала выберите конкретный город в шапке.", show_alert=True)
        return
    new_city = None if row.get("city") == header_code else header_code
    await update_faq_item(item_id, city=new_city)
    screen = await render_faq_card(callback.from_user.id, item_id)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("afaq_d:"))
async def afaq_delete_confirm(callback: types.CallbackQuery):
    item_id = _parse_id(callback.data)
    if item_id is None:
        await callback.answer("Пункт не найден.", show_alert=True)
        return
    scope, _label = await _admin_city_view(callback.from_user.id)
    row = await get_faq_item(item_id)
    if row is None or _card_out_of_scope(row, scope):
        await callback.answer("Пункт недоступен — обновите список.", show_alert=True)
        return
    row_city = row.get("city")
    city_label_text = await city_label(row_city) if row_city else None
    who = "у всех городов" if city_label_text is None else f"у делегатов города «{city_label_text}»"
    text = (
        "🗑 <b>Удалить пункт FAQ?</b>\n\n"
        f"«{html_module.escape(str(row.get('question') or ''))}»\n\n"
        f"Пропадёт {who}. Отменить нельзя."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"afaq_dgo:{item_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"afaq_v:{item_id}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("afaq_dgo:"))
async def afaq_delete_go(callback: types.CallbackQuery):
    item_id = _parse_id(callback.data)
    if item_id is None:
        await callback.answer("Пункт не найден.", show_alert=True)
        return
    scope, _label = await _admin_city_view(callback.from_user.id)
    row = await get_faq_item(item_id)
    if row is None or _card_out_of_scope(row, scope):
        await callback.answer("Пункт уже недоступен.", show_alert=True)
        return
    await delete_faq_item(item_id)
    text, kb = await render_faq_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Пункт удалён.")


@router.callback_query(F.data == "afaq_new")
async def afaq_new_start(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(faq_mode="new", faq_step="question")
    await state.set_state(FaqItem.text)
    await callback.message.answer("Пришлите текст вопроса.", reply_markup=get_cancel_kb())
    await callback.answer()


async def _afaq_edit_start(callback: types.CallbackQuery, state: FSMContext, field: str, prompt: str) -> None:
    item_id = _parse_id(callback.data)
    if item_id is None:
        await callback.answer("Пункт не найден.", show_alert=True)
        return
    scope, _label = await _admin_city_view(callback.from_user.id)
    row = await get_faq_item(item_id)
    if row is None or _card_out_of_scope(row, scope):
        await callback.answer("Пункт недоступен — обновите список.", show_alert=True)
        return
    await state.update_data(faq_mode="edit", faq_id=item_id, faq_field=field)
    await state.set_state(FaqItem.text)
    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("afaq_eq:"))
async def afaq_edit_question_start(callback: types.CallbackQuery, state: FSMContext):
    await _afaq_edit_start(callback, state, "question", "Пришлите новый текст вопроса.")


@router.callback_query(F.data.startswith("afaq_ea:"))
async def afaq_edit_answer_start(callback: types.CallbackQuery, state: FSMContext):
    await _afaq_edit_start(callback, state, "answer", "Пришлите новый текст ответа.")


# WR-03-class guard (форма `aq_answer_cancel`): зарегистрирован ПЕРВЫМ — «Отмена» или команда,
# набранная посреди ввода, не должны уехать в вопрос/ответ пункта FAQ.
@router.message(FaqItem.text, F.text.in_({"Отмена", "/cancel"}))
async def afaq_text_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(FaqItem.text)
async def afaq_text_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("faq_mode")

    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, пришлите текст сообщением.")
        return
    text_value = message.text.strip()

    if mode == "new" and data.get("faq_step") == "question":
        # Мастер «вопрос -> ответ»: остаёмся в том же состоянии, копим вопрос в data.
        await state.update_data(faq_question=text_value, faq_step="answer")
        await message.answer("Теперь пришлите ответ на этот вопрос.", reply_markup=get_cancel_kb())
        return

    await state.set_state(None)

    if mode == "new":
        question = data.get("faq_question", "")
        header_code = await admin_selected_city(message.from_user.id)
        city = header_code if header_code and header_code != ALL_CITIES else None
        await create_faq_item(
            city=city, question=question, answer=text_value, created_by=message.from_user.id,
        )
        await message.answer("✅ Пункт добавлен в FAQ.", reply_markup=ReplyKeyboardRemove())
        text, kb = await render_faq_screen(message.from_user.id)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    if mode == "edit":
        item_id = data.get("faq_id")
        field = data.get("faq_field")
        scope, _label = await _admin_city_view(message.from_user.id)
        row = await get_faq_item(item_id) if item_id is not None else None
        if row is None or field not in ("question", "answer") or _card_out_of_scope(row, scope):
            await message.answer(
                "Пункт больше недоступен — откройте FAQ заново.", reply_markup=ReplyKeyboardRemove(),
            )
            return
        await update_faq_item(item_id, **{field: text_value})
        await message.answer("✅ Сохранено.", reply_markup=ReplyKeyboardRemove())
        screen = await render_faq_card(message.from_user.id, item_id)
        if screen is not None:
            text, kb = screen
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    await message.answer("Что-то пошло не так — откройте FAQ заново.", reply_markup=ReplyKeyboardRemove())
