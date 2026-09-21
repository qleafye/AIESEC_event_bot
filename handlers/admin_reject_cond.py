"""Phase 31 (31-10, D-01/D-02/D-09/D-11/D-13/D-16): конструктор условий правила автоотказа —
вопрос → оператор → значения, группы И/ИЛИ, заготовки в один тап, счётчик dry-run перед
включением.

Отдельный шов от `handlers/admin_reject_rules.py` — потолок размера модуля (тот файл уже стоит
на 850 строках), НЕ архитектурная граница. Импортируется хвостом из `admin_reject_rules.py`
(`from handlers import admin_reject_cond` в самом конце файла) — тот же приём, каким
`admin_sections.py` подключает соседние швы; цикла нет, тот модуль уже полностью загружен.

Набор операторов и список значений вопроса НИКОГДА не хардкодятся здесь — обе таблицы читаются
из `reg_engine` на каждый вызов (`condition_operators`/`options`), иначе конструктор разъедется
с анкетой, которую менеджер уже отредактировал в реестре (тот же анти-паттерн, которого избегает
`services.reject_rules.RULE_PRESETS`).

Значения условия уезжают в `callback_data` ИНДЕКСОМ, не текстом (кириллица не влезает в 64
байта Telegram — тот же приём, что `admin_broadcasts._value_picker_kb`); индекс разрешается
против ЖИВОГО `reg_engine.options(step)` в момент каждого тапа И при сохранении (T-31-10-03) —
список из FSM никогда не идёт в `validate_condition` напрямую, только уже разрешённые подписи.

Право по городу (`can_edit_city`, D-16) перепроверяется в КАЖДОМ мутирующем и КАЖДОМ рендерящем
шаге конструктора (T-31-10-01) — инлайн-клавиатуры в чате не истекают, а FSM-состояние между
шагами (id правила, группа, шаг/оператор/отмеченные индексы) может пережить чужую правку.

Форма шва — та же, что `admin_reject_rules.py`: общий `handlers.admin.router`, декоратор в
одну строку. `render_rule_card` импортируется оттуда (публичная функция); `_load_rule`/
`_save_patch` — свои копии той же формы (приватные имена соседнего модуля не импортируются)."""
import html as html_module
import json

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from config import config
from database.db import get_reject_rule, get_staff_city
from handlers.admin import router
from handlers.admin_reject_rules import render_rule_card
from handlers.states import RejectCond
from keyboards.builders import get_cancel_kb
from reg_engine import (
    REG_FLOW,
    condition_operators,
    is_step_enabled_for_track,
    label_for,
    options,
    reject_condition_category,
)
from services.reject_rules import (
    RULE_PRESETS,
    can_edit_city,
    dry_run_count,
    forum_date_for,
    save_rule,
    validate_condition,
)

STEP_PAGE = 8
VALUE_PAGE = 8

# Человеческие подписи операторов (D-01/D-09) — своя копия, не импорт приватного словаря
# соседнего модуля (та же дисциплина, что уже принята в admin_reject_rules.py).
_OPERATOR_LABELS = {
    "in": "один из", "not_in": "ни один из",
    "lt": "меньше", "gt": "больше", "between": "между",
    "before": "раньше", "after": "позже",
    "age_on_forum_lt": "возраст на дату форума меньше",
    "filled": "заполнено", "empty": "не заполнено",
    "has_file": "есть", "no_file": "нет",
}

# Пример нужного формата ввода (CLAUDE.md: текстовый ввод только для произвольных величин, с
# примером формата) — ключ (категория, оператор), значение — готовая подсказка.
_EXAMPLE_TEXT = {
    ("int", "between"): "Пришлите два числа через «;», например: 18;25",
    ("date", "before"): "Пришлите дату в формате ДД.ММ.ГГГГ, например: 15.10.2026",
    ("date", "after"): "Пришлите дату в формате ДД.ММ.ГГГГ, например: 15.10.2026",
    ("birth_date", "before"): "Пришлите дату в формате ДД.ММ.ГГГГ, например: 15.10.2026",
    ("birth_date", "after"): "Пришлите дату в формате ДД.ММ.ГГГГ, например: 15.10.2026",
    ("birth_date", "age_on_forum_lt"): "Пришлите число лет, например: 18",
}
_DEFAULT_EXAMPLE = "Пришлите число, например: 18"

# Путь менеджера к настройке даты форума (D-31 anti-pattern guard) — тот же экран, на который
# план 31-03 посадил ключ `forum_date` («🎪 Событие/Медиа», рядом с `event_date`).
_FORUM_DATE_PATH = "«⚙️ Настройки» → «🎪 Событие» → «🎪 Событие/Медиа» → «Дата начала форума»"


def _parse_id(callback_data: str) -> int | None:
    try:
        return int(callback_data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _short(text: str, limit: int) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: max(0, limit - 1)].rstrip() + "…"


async def _load_rule(rule_id: int) -> dict | None:
    """Свежая копия правила (T-31-10-01) — та же форма, что `admin_reject_rules._load_rule`;
    своя копия, а не импорт приватного имени соседнего модуля."""
    row = await get_reject_rule(rule_id)
    if row is None:
        return None
    rule = dict(row)
    try:
        rule["tracks"] = json.loads(row.get("tracks") or "[]") or []
    except (TypeError, ValueError):
        rule["tracks"] = []
    try:
        rule["conditions"] = json.loads(row.get("conditions") or "[]") or []
    except (TypeError, ValueError):
        rule["conditions"] = []
    return rule


async def _save_patch(admin_id: int, rule: dict, **overrides) -> tuple[int | None, str | None]:
    """Единственная дверь мутации (`save_rule` не умеет частичный PATCH) — та же форма, что
    `admin_reject_rules._save_patch`."""
    fields = {
        "name": rule.get("name"), "city": rule.get("city"),
        "tracks": rule.get("tracks") or ["full"], "conditions": rule.get("conditions") or [],
        "action": rule.get("action"), "reject_text": rule.get("reject_text"),
        "enabled": rule.get("enabled"),
    }
    fields.update(overrides)
    return await save_rule(admin_id, rule["id"], **fields)


async def _default_city(admin_id: int) -> str | None:
    """Тот же выбор дефолтного города, что `admin_reject_rules._default_new_rule_city`."""
    if admin_id in config.ADMIN_IDS:
        return None
    return await get_staff_city(admin_id)


async def _enabled_steps(rule: dict) -> list[str]:
    """Вопросы, реально включённые хотя бы для одного трека этого правила (D-14 anti-pattern
    guard: выключенный вопрос предлагать нельзя — правило на нём сразу встанет на паузу).
    Правило может стоять сразу на нескольких треках — шаг предлагается, если включён хотя бы
    для одного из них."""
    tracks = rule.get("tracks") or ["full"]
    city = rule.get("city")
    enabled: list[str] = []
    for step_key, setting_key, _t in REG_FLOW:
        for track in tracks:
            if await is_step_enabled_for_track(setting_key, track, city):
                enabled.append(step_key)
                break
    return enabled


def _insert_condition(conditions: list[list[dict]], group: int, cond: dict) -> list[list[dict]]:
    """`group == -1` (или вне диапазона существующих групп) — новая группа ИЛИ; иначе условие
    дописывается в существующую группу И (D-02)."""
    groups = [list(g) for g in (conditions or [])]
    if group == -1 or group >= len(groups):
        groups.append([cond])
    else:
        groups[group].append(cond)
    return groups


def _remove_condition(conditions: list[list[dict]], group: int, idx: int) -> list[list[dict]]:
    """Пустая группа после удаления последнего условия убирается сама (T-31-10-05: пустые
    группы читаются как «всегда истинно» при беглом взгляде)."""
    groups = [list(g) for g in (conditions or [])]
    del groups[group][idx]
    if not groups[group]:
        del groups[group]
    return groups


async def _add_condition(
    admin_id: int, rule_id: int, group: int, step: str, op: str, values: list,
) -> tuple[int | None, str | None]:
    """Перечитывает правило, перепроверяет право (T-31-10-01), валидирует условие через
    `validate_condition` (T-31-10-02/03 — единственная дверь проверки) и сохраняет."""
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(admin_id, rule.get("city")):
        return None, "Правило недоступно — обновите список."
    cond, error = await validate_condition(step, op, values, event_city=rule.get("city"))
    if error:
        return None, error
    new_conditions = _insert_condition(rule.get("conditions") or [], group, cond)
    _, error = await _save_patch(admin_id, rule, conditions=new_conditions)
    if error:
        return None, error
    return rule_id, None


async def _finish_condition(
    callback: types.CallbackQuery, state: FSMContext,
    rule_id: int, group: int, step: str, op: str, values: list,
) -> None:
    await state.set_state(None)
    await state.update_data(arc_step=None, arc_op=None, arc_checked=[])
    _, error = await _add_condition(callback.from_user.id, rule_id, group, step, op, values)
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    if screen is None:
        await callback.answer("Условие сохранено, но карточка не открылась — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Условие добавлено.")


async def _finish_condition_msg(
    message: types.Message, rule_id: int, group: int, step: str, op: str, values: list,
) -> None:
    _, error = await _add_condition(message.from_user.id, rule_id, group, step, op, values)
    if error:
        await message.answer(f"Не сохранено: {error}", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer("✅ Условие добавлено.", reply_markup=ReplyKeyboardRemove())
    screen = await render_rule_card(message.from_user.id, rule_id)
    if screen is not None:
        text, kb = screen
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


def _dry_run_text(matched: int, total: int) -> str:
    """D-13/D-19: честное число + объяснение, что превью ничего не меняет — уже поданные
    заявки не тронет ни одно значение этого счётчика."""
    lines = [
        f"Под это правило сейчас попали бы {matched} из {total} заявок.",
        "",
        "Это превью — уже поданные заявки не изменятся. Правило начнёт работать только для "
        "тех, кто подаст заявку или поправит анкету ПОСЛЕ включения.",
    ]
    if total and matched > total / 2:
        lines.append("")
        lines.append("⚠️ Похоже, правило слишком широкое — проверьте условия.")
    return "\n".join(lines)


# ── Шаг 1: выбор вопроса ────────────────────────────────────────────────────────────────────

async def render_step_screen(
    admin_id: int, rule_id: int, group: int, offset: int = 0,
) -> tuple[str, InlineKeyboardMarkup] | None:
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(admin_id, rule.get("city")):
        return None
    steps = await _enabled_steps(rule)
    lines = ["🧩 <b>Условие — выберите вопрос анкеты</b>", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    page = steps[offset: offset + STEP_PAGE]
    for step in page:
        buttons.append([InlineKeyboardButton(
            text=label_for(step), callback_data=f"arc_step:{rule_id}:{group}:{step}",
        )])
    if not steps:
        lines.append("Все вопросы анкеты сейчас выключены для города/трека этого правила.")
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"arc_steppage:{rule_id}:{group}:{max(0, offset - STEP_PAGE)}"))
    if offset + STEP_PAGE < len(steps):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"arc_steppage:{rule_id}:{group}:{offset + STEP_PAGE}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"arc_cancel:{rule_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("arc_add:"))
async def arc_add(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    try:
        rule_id, group = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Не понял — обновите карточку.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(arc_rule=rule_id, arc_group=group, arc_step=None, arc_op=None, arc_checked=[], arc_voff=0)
    screen = await render_step_screen(callback.from_user.id, rule_id, group)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arc_steppage:"))
async def arc_steppage(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    try:
        rule_id, group, offset = int(parts[1]), int(parts[2]), max(0, int(parts[3]))
    except (IndexError, ValueError):
        await callback.answer("Не понял — обновите карточку.", show_alert=True)
        return
    screen = await render_step_screen(callback.from_user.id, rule_id, group, offset)
    if screen is None:
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Шаг 2: выбор оператора ──────────────────────────────────────────────────────────────────

async def render_op_screen(
    admin_id: int, rule_id: int, group: int, step: str,
) -> tuple[str, InlineKeyboardMarkup] | None:
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(admin_id, rule.get("city")):
        return None
    lines = [f"🧩 <b>{html_module.escape(label_for(step))}</b>", "", "Выберите условие:"]
    buttons = [
        [InlineKeyboardButton(text=_OPERATOR_LABELS.get(op, op), callback_data=f"arc_op:{rule_id}:{group}:{op}")]
        for op in condition_operators(step)
    ]
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"arc_cancel:{rule_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("arc_step:"))
async def arc_step(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    try:
        rule_id, group = int(parts[1]), int(parts[2])
    except ValueError:
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    step = parts[3]
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    if step not in await _enabled_steps(rule):
        await callback.answer("Такой вопрос сейчас недоступен — обновите экран.", show_alert=True)
        return
    await state.update_data(arc_rule=rule_id, arc_group=group, arc_step=step, arc_op=None, arc_checked=[], arc_voff=0)
    screen = await render_op_screen(callback.from_user.id, rule_id, group, step)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Шаг 3: значения ─────────────────────────────────────────────────────────────────────────

async def render_value_screen(
    admin_id: int, rule_id: int, group: int, step: str, checked: set, offset: int = 0,
) -> tuple[str, InlineKeyboardMarkup] | None:
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(admin_id, rule.get("city")):
        return None
    opts = await options(step)
    lines = [f"🧩 <b>{html_module.escape(label_for(step))}</b>", "", "Отметьте варианты:"]
    buttons: list[list[InlineKeyboardButton]] = []
    page = list(enumerate(opts))[offset: offset + VALUE_PAGE]
    for idx, label in page:
        mark = "✅" if idx in checked else "⬜"
        buttons.append([InlineKeyboardButton(text=_short(f"{mark} {label}", 64), callback_data=f"arc_val:{rule_id}:{group}:{idx}")])
    if not opts:
        lines.append("У этого вопроса сейчас нет ни одного варианта — выберите другой вопрос.")
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"arc_valpage:{rule_id}:{group}:{max(0, offset - VALUE_PAGE)}"))
    if offset + VALUE_PAGE < len(opts):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"arc_valpage:{rule_id}:{group}:{offset + VALUE_PAGE}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"arc_valdone:{rule_id}:{group}")])
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"arc_cancel:{rule_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _op_stale(state: FSMContext, rule_id: int, group: int) -> dict | None:
    """Возвращает данные FSM, если они относятся к ТЕКУЩЕЙ паре (rule, group) и шаг уже
    выбран — иначе `None` (экран устарел, T-31-10-01/02: callback_data чужой пары не
    принимается)."""
    data = await state.get_data()
    if data.get("arc_rule") != rule_id or data.get("arc_group") != group or not data.get("arc_step"):
        return None
    return data


@router.callback_query(F.data.startswith("arc_op:"))
async def arc_op(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    try:
        rule_id, group = int(parts[1]), int(parts[2])
    except ValueError:
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    op = parts[3]
    data = await _op_stale(state, rule_id, group)
    if data is None:
        await callback.answer("Экран устарел — начните заново кнопкой «➕ условие».", show_alert=True)
        return
    step = data["arc_step"]
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    if op not in condition_operators(step):
        await callback.answer("Такое условие недоступно для этого вопроса — обновите экран.", show_alert=True)
        return
    category = reject_condition_category(step)
    await state.update_data(arc_op=op)

    if category in ("select", "multi"):
        await state.update_data(arc_checked=[], arc_voff=0)
        screen = await render_value_screen(callback.from_user.id, rule_id, group, step, set())
        text, kb = screen
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer()
        return

    if category in ("text", "file"):
        await _finish_condition(callback, state, rule_id, group, step, op, [])
        return

    # int / date / birth_date — только текстом, с примером формата (D-01).
    if category == "birth_date" and op == "age_on_forum_lt" and not await forum_date_for(rule.get("city")):
        await callback.answer(
            f"Дата форума не задана — такое правило никогда не сработает. Задайте её: "
            f"{_FORUM_DATE_PATH}, потом вернитесь сюда.",
            show_alert=True,
        )
        return
    example = _EXAMPLE_TEXT.get((category, op), _DEFAULT_EXAMPLE)
    lines = [
        f"🧩 <b>{html_module.escape(label_for(step))}</b>", "",
        f"Условие: {_OPERATOR_LABELS.get(op, op)}.", example,
    ]
    buttons = [
        [InlineKeyboardButton(text="✏️ Ввести значение", callback_data=f"arc_num:{rule_id}:{group}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"arc_cancel:{rule_id}")],
    ]
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("arc_val:"))
async def arc_val_toggle(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    try:
        rule_id, group, idx = int(parts[1]), int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    data = await _op_stale(state, rule_id, group)
    if data is None:
        await callback.answer("Экран устарел — начните заново кнопкой «➕ условие».", show_alert=True)
        return
    step = data["arc_step"]
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    opts = await options(step)
    offset = data.get("arc_voff", 0)
    checked = set(data.get("arc_checked") or [])
    if 0 <= idx < len(opts):
        checked.symmetric_difference_update({idx})
        await state.update_data(arc_checked=sorted(checked))
    else:
        await callback.answer("Такого варианта больше нет — список обновлён.", show_alert=True)
    screen = await render_value_screen(callback.from_user.id, rule_id, group, step, checked, offset)
    if screen is not None:
        text, kb = screen
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    if 0 <= idx < len(opts):
        await callback.answer()


@router.callback_query(F.data.startswith("arc_valpage:"))
async def arc_valpage(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    try:
        rule_id, group, offset = int(parts[1]), int(parts[2]), max(0, int(parts[3]))
    except (IndexError, ValueError):
        await callback.answer("Не понял — обновите карточку.", show_alert=True)
        return
    data = await _op_stale(state, rule_id, group)
    if data is None:
        await callback.answer("Экран устарел — начните заново кнопкой «➕ условие».", show_alert=True)
        return
    await state.update_data(arc_voff=offset)
    checked = set(data.get("arc_checked") or [])
    screen = await render_value_screen(callback.from_user.id, rule_id, group, data["arc_step"], checked, offset)
    if screen is None:
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arc_valdone:"))
async def arc_valdone(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    try:
        rule_id, group = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Не понял — обновите карточку.", show_alert=True)
        return
    data = await _op_stale(state, rule_id, group)
    if data is None or not data.get("arc_op"):
        await callback.answer("Экран устарел — начните заново кнопкой «➕ условие».", show_alert=True)
        return
    checked = sorted(set(data.get("arc_checked") or []))
    if not checked:
        await callback.answer("Отметьте хотя бы один вариант.", show_alert=True)
        return
    opts = await options(data["arc_step"])
    values = [opts[i] for i in checked if 0 <= i < len(opts)]
    if not values:
        await callback.answer("Отмеченные варианты пропали — обновите экран и отметьте заново.", show_alert=True)
        return
    await _finish_condition(callback, state, rule_id, group, data["arc_step"], data["arc_op"], values)


# ── Числовой/датовый ввод (FSM) ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("arc_num:"))
async def arc_num_start(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    try:
        rule_id, group = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Не понял — обновите карточку.", show_alert=True)
        return
    data = await _op_stale(state, rule_id, group)
    if data is None or not data.get("arc_op"):
        await callback.answer("Экран устарел — начните заново кнопкой «➕ условие».", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    category = reject_condition_category(data["arc_step"])
    prompt = _EXAMPLE_TEXT.get((category, data["arc_op"]), _DEFAULT_EXAMPLE)
    await state.set_state(RejectCond.num)
    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(RejectCond.num, F.text.in_({"Отмена", "/cancel"}))
async def arc_num_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(RejectCond.num)
async def arc_num_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rule_id, group = data.get("arc_rule"), data.get("arc_group")
    step, op = data.get("arc_step"), data.get("arc_op")
    if rule_id is None or not step or not op:
        await state.set_state(None)
        await message.answer("Экран устарел — откройте карточку правила заново.", reply_markup=ReplyKeyboardRemove())
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(message.from_user.id, rule.get("city")):
        await state.set_state(None)
        await message.answer("Правило больше недоступно — откройте список заново.", reply_markup=ReplyKeyboardRemove())
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пожалуйста, пришлите значение сообщением.")
        return
    # Ловушка «Enter = отправить» на мобильных (CLAUDE.md): «;» или пробел разделяют два числа «между».
    if op == "between":
        values = [p for p in raw.replace(";", " ").split() if p]
    else:
        values = [raw]
    await state.set_state(None)
    await _finish_condition_msg(message, rule_id, group, step, op, values)


# ── Удаление условия ─────────────────────────────────────────────────────────────────────────

async def render_delete_screen(admin_id: int, rule_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(admin_id, rule.get("city")):
        return None
    groups = rule.get("conditions") or []
    lines = ["🗑 <b>Удалить условие</b>", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for gi, group in enumerate(groups):
        for ci, cond in enumerate(group):
            step_label = label_for(cond.get("step")) if cond.get("step") else "?"
            buttons.append([InlineKeyboardButton(
                text=_short(f"Группа {gi + 1}: {step_label}", 60), callback_data=f"arc_del:{rule_id}:{gi}:{ci}",
            )])
    if not buttons:
        lines.append("Условий больше нет.")
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data=f"arr_v:{rule_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("arc_dellist:"))
async def arc_dellist(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    screen = await render_delete_screen(callback.from_user.id, rule_id)
    if screen is None:
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arc_del:"))
async def arc_del(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    try:
        rule_id, group, idx = int(parts[1]), int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    groups = rule.get("conditions") or []
    if not (0 <= group < len(groups) and 0 <= idx < len(groups[group])):
        await callback.answer("Условие уже удалено — обновите экран.", show_alert=True)
        return
    cond = groups[group][idx]
    step_label = label_for(cond.get("step")) if cond.get("step") else "?"
    new_conditions = _remove_condition(groups, group, idx)
    overrides = {"conditions": new_conditions}
    turned_off = not new_conditions and bool(rule.get("enabled"))
    if turned_off:
        overrides["enabled"] = 0
    _, error = await _save_patch(callback.from_user.id, rule, **overrides)
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    msg = f"🗑 Условие «{step_label}» удалено."
    if turned_off:
        msg += " Условий не осталось — правило выключено."
    await callback.answer(msg, show_alert=True)


# ── Отмена сборки ────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("arc_cancel:"))
async def arc_cancel(callback: types.CallbackQuery, state: FSMContext):
    rule_id = _parse_id(callback.data)
    await state.set_state(None)
    await state.update_data(arc_step=None, arc_op=None, arc_checked=[])
    if rule_id is None:
        await callback.answer("Отменено.")
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    if screen is None:
        await callback.answer("Отменено — правило недоступно, обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Отменено.")


# ── Заготовки в один тап (D-11) ─────────────────────────────────────────────────────────────

async def render_preset_screen(admin_id: int, rule_or_new: str) -> tuple[str, InlineKeyboardMarkup] | None:
    if rule_or_new != "new":
        try:
            rule_id = int(rule_or_new)
        except ValueError:
            return None
        rule = await _load_rule(rule_id)
        if rule is None or not await can_edit_city(admin_id, rule.get("city")):
            return None
    presets = await RULE_PRESETS()
    lines = [
        "🧩 <b>Заготовка условий</b>", "",
        "Тап заполнит правило значениями заготовки, ВСЕГДА выключенным — поправите и включите сами.",
    ]
    buttons = [
        [InlineKeyboardButton(text=f"🧩 {p.get('name')}", callback_data=f"arc_preset:{rule_or_new}:{idx}")]
        for idx, p in enumerate(presets)
    ]
    back = "arr_p:0" if rule_or_new == "new" else f"arr_v:{rule_or_new}"
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=back)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("arc_presetlist:"))
async def arc_presetlist(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    screen = await render_preset_screen(callback.from_user.id, str(rule_id))
    if screen is None:
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arc_preset:"))
async def arc_preset_pick(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    rule_or_new, n_raw = parts[1], parts[2]
    try:
        n = int(n_raw)
    except ValueError:
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    presets = await RULE_PRESETS()
    if not (0 <= n < len(presets)):
        await callback.answer("Такой заготовки нет — обновите экран.", show_alert=True)
        return
    preset = presets[n]
    admin_id = callback.from_user.id

    if rule_or_new == "new":
        city = await _default_city(admin_id)
        if not await can_edit_city(admin_id, city):
            await callback.answer("Нет прав создать правило в этом городе.", show_alert=True)
            return
        target_id, error = await save_rule(
            admin_id, None, name=preset.get("name"), city=city, tracks=["full"],
            conditions=preset.get("conditions") or [], action=preset.get("action") or "reject",
            reject_text=preset.get("reject_text"), enabled=0,
        )
        made_text = "Правило создано из заготовки и выключено — поправьте значения и включите."
    else:
        try:
            target_id = int(rule_or_new)
        except ValueError:
            await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
            return
        rule = await _load_rule(target_id)
        if rule is None or not await can_edit_city(admin_id, rule.get("city")):
            await callback.answer("Правило недоступно — обновите список.", show_alert=True)
            return
        _, error = await _save_patch(
            admin_id, rule, name=rule.get("name") or preset.get("name"),
            conditions=preset.get("conditions") or [], action=preset.get("action") or rule.get("action"),
            reject_text=rule.get("reject_text") or preset.get("reject_text"), enabled=0,
        )
        made_text = "Правило заполнено заготовкой и выключено — поправьте значения и включите."

    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(admin_id, target_id)
    if screen is None:
        await callback.answer("Готово, но карточка не открылась — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(made_text, show_alert=True)


# ── Счётчик dry-run перед включением (D-13/D-19) ────────────────────────────────────────────

@router.callback_query(F.data.startswith("arc_dry:"))
async def arc_dry(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    matched, total = await dry_run_count(rule)
    await callback.answer(_dry_run_text(matched, total), show_alert=True)


@router.callback_query(F.data.startswith("arc_gate:"))
async def arc_gate(callback: types.CallbackQuery):
    """Заменяет собой прямой тап «✅ Включить»/«🚫 Выключить» на карточке (карточка отдаёт
    сюда, не в `arr_t` — тот остаётся доступен из экрана удаления, где гейт не нужен, D-19).
    Выключение не требует счётчика; включение НИКОГДА не срабатывает с первого тапа — сначала
    менеджер обязан увидеть число (T-31-10-04)."""
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    if rule.get("enabled"):
        _, error = await _save_patch(callback.from_user.id, rule, enabled=0)
        if error:
            await callback.answer(error, show_alert=True)
            return
        screen = await render_rule_card(callback.from_user.id, rule_id)
        text, kb = screen
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer(
            "🚫 Правило выключено, но осталось в списке — включить обратно можно в любой момент.",
            show_alert=True,
        )
        return
    matched, total = await dry_run_count(rule)
    lines = ["✅ <b>Включить правило?</b>", "", _dry_run_text(matched, total)]
    buttons = [
        [InlineKeyboardButton(text="✅ Всё равно включить", callback_data=f"arc_dry_go:{rule_id}")],
        [InlineKeyboardButton(text="← Назад", callback_data=f"arr_v:{rule_id}")],
    ]
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("arc_dry_go:"))
async def arc_dry_go(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    _, error = await _save_patch(callback.from_user.id, rule, enabled=1)
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Правило включено и теперь действует на подходящие заявки.", show_alert=True)
