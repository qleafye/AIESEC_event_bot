"""Phase 30 (30-07, A2-03, 30-CONTEXT.md § «Архитектурные ориентиры»): экран «📚 Справочники»
— тонкая настройка справочника ВУЗ/город, необязательна (`services/lookup.py`, планы 30-02/
30-07 — единственное место правил и SQL этой темы, здесь только UI). Менеджер разбирает
очередь «Другое → влить как псевдоним/отклонить» и закрепляет ≤8 чипов кнопками, без похода
в базу.

Форма шва — `handlers/admin_faq.py` (постраничный список + карточка), точная копия приёма:
свой класс роутера не заводится, хендлеры декорируют ОБЩИЙ `handlers.admin.router`, каждый
декоратор — в одну строку (инвариант cap-теста `tests/test_roles_phase8.py`).
`handlers.admin_sections.back_button` — ленивый импорт внутри функций (D-03: цикл на уровне
модуля, admin_sections импортирует этот шов хвостом).

`LookupAdmin` — `StatesGroup` в `handlers/states.py` (не локально в шве): `tests/test_roles_phase8.py`'s автовывод capability-ключа для message-хендлеров admin-роутера находит группу состояний ТОЛЬКО там (`hasattr(states_mod, group_name)`), в отличие от `handlers/reg_types_lookup.py` (30-06, чат-роутер регистрации, другая проверка).

Callback-схема — один префикс `admin_lookup` (капабилити `settings`, `handlers/admin_caps.py`:
`admin_lookup` + `admin_lookup:*`):
  admin_lookup                     -> выбор вида справочника (ВУЗы/Города)
  admin_lookup:kind:{kind}         -> сводка вида (счётчик очереди/чипов)
  admin_lookup:q:{kind}:{offset}   -> очередь «Другое», страница
  admin_lookup:qm:{kind}:{off}:{id}-> начать слияние записи id (поиск каноники)
  admin_lookup:qr:{kind}:{off}:{id}-> отклонить запись id
  admin_lookup:c:{kind}            -> список закреплённых чипов
  admin_lookup:cu:{kind}:{idx}     -> открепить чип по индексу текущего списка
  admin_lookup:cp:{kind}           -> начать закрепление (поиск каноники)
  admin_lookup:sel:{n}             -> выбор n-го варианта поиска (см. LookupAdmin.search)
"""
import html as html_module

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from handlers.admin import router
from handlers.states import LookupAdmin
from keyboards.builders import get_cancel_kb
from services import lookup as lookup_service

QUEUE_PAGE = 5
CHIPS_MAX = 8

KIND_LABELS = {"university": "🎓 ВУЗы", "city": "🏙 Города"}
KIND_NOUN = {"university": "ВУЗ", "city": "город"}


def _kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def _short(text: str, limit: int = 60) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── экран 1: выбор вида справочника ─────────────────────────────────────────────────────────

async def render_lookup_home() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📚 <b>Справочники</b>\n\n"
        "Здесь можно (но не обязательно) навести порядок в справочнике ВУЗов и городов: "
        "разобрать очередь «Другое» и закрепить свои чипы наверху списка."
    )
    from handlers.admin_sections import back_button  # ленивый шов (D-03)

    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"admin_lookup:kind:{kind}")]
        for kind, label in KIND_LABELS.items()
    ]
    buttons.append([back_button("admin_lookup")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_lookup")
async def admin_lookup(callback: types.CallbackQuery):
    text, kb = await render_lookup_home()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── экран 2: сводка вида справочника ────────────────────────────────────────────────────────

async def render_lookup_kind(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    queue = await lookup_service.merge_queue_items(kind, status="new")
    chips = await lookup_service.pinned_chips(kind)
    noun = KIND_NOUN.get(kind, kind)
    lines = [f"📚 <b>Справочники — {_kind_label(kind)}</b>", ""]
    if queue:
        lines.append(f"В очереди «Другое»: {len(queue)}")
    else:
        lines.append("Очередь «Другое» пуста — новых вариантов нет.")
    lines.append(f"Закреплено чипов: {len(chips)}/{CHIPS_MAX}")
    lines.append("")
    lines.append(
        f"Разбирать очередь и закреплять чипы не обязательно — если {noun}а нет в списке, "
        "делегат впишет его сам, ответ не теряется."
    )
    text = "\n".join(lines)

    from handlers.admin_sections import back_button  # ленивый шов (D-03)

    buttons = [
        [InlineKeyboardButton(
            text=f"🗂 Очередь «Другое» ({len(queue)})", callback_data=f"admin_lookup:q:{kind}:0",
        )],
        [InlineKeyboardButton(
            text=f"📌 Закреплённые чипы ({len(chips)}/{CHIPS_MAX})",
            callback_data=f"admin_lookup:c:{kind}",
        )],
        [back_button("admin_lookup")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("admin_lookup:kind:"))
async def admin_lookup_kind(callback: types.CallbackQuery):
    kind = callback.data.split(":", 2)[2]
    text, kb = await render_lookup_kind(kind)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── экран 3: очередь «Другое» (постраничная) ────────────────────────────────────────────────

async def render_lookup_queue(kind: str, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    items = await lookup_service.merge_queue_items(kind, status="new")
    lines = [f"🗂 <b>Очередь «Другое» — {_kind_label(kind)}</b>"]
    buttons: list[list[InlineKeyboardButton]] = []

    if not items:
        # CLAUDE.md: пустая очередь — спокойный текст, а не пустой экран.
        lines.append("")
        lines.append("Пусто — все ответы делегатов уже разобраны или очередь ещё не копилась.")
    else:
        lines.append(f"Всего: {len(items)}")
        page = items[offset: offset + QUEUE_PAGE]
        for row in page:
            lines.append("")
            lines.append(f"«{html_module.escape(_short(row['raw_text']))}»")
            lines.append(f"<i>{html_module.escape(str(row['created_at']))}</i>")
            buttons.append([
                InlineKeyboardButton(
                    text="✅ Влить как псевдоним",
                    callback_data=f"admin_lookup:qm:{kind}:{offset}:{row['id']}",
                ),
                InlineKeyboardButton(
                    text="🚫 Отклонить (уйдёт из очереди)",
                    callback_data=f"admin_lookup:qr:{kind}:{offset}:{row['id']}",
                ),
            ])
        nav_row: list[InlineKeyboardButton] = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton(
                text="⬅️", callback_data=f"admin_lookup:q:{kind}:{max(0, offset - QUEUE_PAGE)}",
            ))
        if offset + QUEUE_PAGE < len(items):
            nav_row.append(InlineKeyboardButton(
                text="➡️", callback_data=f"admin_lookup:q:{kind}:{offset + QUEUE_PAGE}",
            ))
        if nav_row:
            buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(
        text="← Назад", callback_data=f"admin_lookup:kind:{kind}",
    )])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("admin_lookup:q:"))
async def admin_lookup_queue(callback: types.CallbackQuery):
    _, _, kind, offset_raw = callback.data.split(":", 3)
    text, kb = await render_lookup_queue(kind, int(offset_raw))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_lookup:qr:"))
async def admin_lookup_reject(callback: types.CallbackQuery):
    _, _, kind, offset_raw, item_id_raw = callback.data.split(":", 4)
    ok = await lookup_service.merge_reject(int(item_id_raw), callback.from_user.id)
    await callback.answer("Отклонено — ушло из очереди." if ok else "Уже обработано кем-то другим.")
    text, kb = await render_lookup_queue(kind, int(offset_raw))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("admin_lookup:qm:"))
async def admin_lookup_merge_start(callback: types.CallbackQuery, state: FSMContext):
    _, _, kind, offset_raw, item_id_raw = callback.data.split(":", 4)
    await state.update_data(
        lookup_mode="merge", lookup_kind=kind, lookup_item_id=int(item_id_raw),
        lookup_queue_offset=int(offset_raw), lookup_candidates=[],
    )
    await state.set_state(LookupAdmin.search)
    noun = KIND_NOUN.get(kind, kind)
    await callback.message.answer(
        f"Напиши первые буквы канонического названия ({noun}), с которым слить эту запись.",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


# ── экран 4: закреплённые чипы ───────────────────────────────────────────────────────────────

async def render_lookup_chips(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    chips = await lookup_service.pinned_chips(kind)
    lines = [f"📌 <b>Закреплённые чипы — {_kind_label(kind)}</b>", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if chips:
        lines.append("Идут первыми в списке частых значений у делегата.")
        for idx, canonical in enumerate(chips):
            buttons.append([InlineKeyboardButton(
                text=f"✖ {_short(canonical, 40)}", callback_data=f"admin_lookup:cu:{kind}:{idx}",
            )])
    else:
        lines.append("Пока ничего не закреплено — топ-8 собирается по частоте ответов сезона.")
    if len(chips) < CHIPS_MAX:
        buttons.append([InlineKeyboardButton(
            text="➕ Закрепить ещё", callback_data=f"admin_lookup:cp:{kind}",
        )])
    else:
        lines.append("")
        lines.append(f"Показывается только {CHIPS_MAX} — открепи один, чтобы закрепить другой.")
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data=f"admin_lookup:kind:{kind}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("admin_lookup:c:"))
async def admin_lookup_chips(callback: types.CallbackQuery):
    kind = callback.data.split(":", 2)[2]
    text, kb = await render_lookup_chips(kind)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_lookup:cu:"))
async def admin_lookup_unpin(callback: types.CallbackQuery):
    _, _, kind, idx_raw = callback.data.split(":", 3)
    chips = await lookup_service.pinned_chips(kind)
    idx = int(idx_raw)
    if 0 <= idx < len(chips):
        await lookup_service.pin_chip(kind, chips[idx], False)
        await callback.answer("Откреплено.")
    else:
        await callback.answer("Список уже изменился — обновляю.")
    text, kb = await render_lookup_chips(kind)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("admin_lookup:cp:"))
async def admin_lookup_pin_start(callback: types.CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 2)[2]
    chips = await lookup_service.pinned_chips(kind)
    if len(chips) >= CHIPS_MAX:
        await callback.answer(f"Уже закреплено {CHIPS_MAX} — сначала открепи один.", show_alert=True)
        return
    await state.update_data(lookup_mode="pin", lookup_kind=kind, lookup_candidates=[])
    await state.set_state(LookupAdmin.search)
    noun = KIND_NOUN.get(kind, kind)
    await callback.message.answer(
        f"Напиши первые буквы {noun}а, который хочешь закрепить чипом.",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


# ── общий шаг поиска каноники (слияние И закрепление) ───────────────────────────────────────

@router.message(LookupAdmin.search, F.text.in_({"Отмена", "/cancel"}))
async def admin_lookup_search_cancel(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(None)
    kind = data.get("lookup_kind")
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    if kind:
        text, kb = await render_lookup_kind(kind)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(LookupAdmin.search)
async def admin_lookup_search_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("lookup_kind")
    query = (message.text or "").strip()
    if not kind or not query:
        await message.answer("Пришли текстом первые буквы названия.")
        return

    results = await lookup_service.search_lookup(kind, query, limit=5)
    candidates = [r["canonical"] for r in results]
    if not candidates:
        await message.answer(
            "Ничего не нашли по этому запросу — попробуй другие буквы или пришли «Отмена».",
        )
        return
    await state.update_data(lookup_candidates=candidates)
    buttons = [
        [InlineKeyboardButton(text=_short(name, 60), callback_data=f"admin_lookup:sel:{i}")]
        for i, name in enumerate(candidates)
    ]
    await message.answer(
        "Выбери вариант:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(LookupAdmin.search, F.data.startswith("admin_lookup:sel:"))
async def admin_lookup_search_pick(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    candidates = data.get("lookup_candidates") or []
    idx = int(callback.data.split(":", 2)[2])
    if idx < 0 or idx >= len(candidates):
        await callback.answer("Список устарел — напиши буквы ещё раз.", show_alert=True)
        return
    canonical = candidates[idx]
    kind = data.get("lookup_kind")
    mode = data.get("lookup_mode")
    await state.set_state(None)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass  # T-30 fail-soft (30-PATTERNS.md): сообщение могло устареть/удалиться

    if mode == "merge":
        item_id = data.get("lookup_item_id")
        offset = data.get("lookup_queue_offset", 0)
        ok = await lookup_service.merge_apply(item_id, canonical, callback.from_user.id)
        await callback.answer("Влито как псевдоним." if ok else "Уже обработано кем-то другим.")
        text, kb = await render_lookup_queue(kind, offset)
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    elif mode == "pin":
        await lookup_service.pin_chip(kind, canonical, True)
        await callback.answer("Закреплено.")
        text, kb = await render_lookup_chips(kind)
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.answer()
