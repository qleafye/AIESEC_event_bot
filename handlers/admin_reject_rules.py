"""Phase 31 (31-08, D-09/D-10/D-12/D-15/D-16/D-21): раздел «🚫 Правила автоотказа» — список
правил с человеческим автоописанием, карточка правила по принятому макету (D-09), общий
рубильник (D-15), копирование в другой город (D-12) и удаление с подтверждением.

Форма шва — точная копия `handlers/admin_faq.py`: своего `Router()` нет, хендлеры декорируют
ОБЩИЙ `handlers.admin.router`; каждый декоратор — в одну строку (инвариант cap-теста).
`handlers.admin` импортируется на уровне модуля, `handlers.admin_sections` — лениво внутри
функций (цикл на уровне модуля: `admin_sections` импортирует этот шов хвостом).

Конструктор условий («➕ условие в группу N» / «➕ новая группа (ИЛИ)») и журнал автоотказов —
ОТДЕЛЬНЫЕ швы (планы 31-10/31-11, потолок размера модуля, не архитектурная граница): здешние
кнопки-заготовки ведут на заглушку `arr_noop`.

Запись правила — ТОЛЬКО через `services.reject_rules.save_rule`/`delete_rule` (план 31-04),
второй двери в `reject_rules` здесь нет. Право по городу (`can_edit_city`, D-16) перепроверяется
в КАЖДОМ мутирующем хендлере ПЕРЕД действием — клавиатуры в чате не истекают (T-31-08-01, тот
же приём, что `handlers/admin_faq.py::_card_out_of_scope`). Копия правила (D-12) ВСЕГДА
выключена; удаление подтверждается экраном, который называет последствия (CLAUDE.md, форма
`handlers/admin_faq.py::afaq_delete_confirm`)."""
import html as html_module
import json

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from config import config
from cities import ALL_CITIES, ALL_CITIES_LABEL, city_codes, city_label
from database.db import get_reject_rule, get_staff_city, list_auto_reject_log
from handlers.admin import router
from handlers.admin_core import _admin_city_view
from handlers.states import RejectRuleEdit
from keyboards.builders import get_cancel_kb
from reg_engine import label_for
from services.reject_rules import (
    RULE_PRESETS,
    can_edit_city,
    delete_rule,
    rule_summary,
    rules_for_admin,
    save_rule,
)
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed

RULES_PAGE = 8

# Человеческие подписи операторов (D-01/D-09) — своя копия таблицы `services.reject_rules.
# rule_summary` (не импорт приватного имени соседнего модуля): карточка печатает условия
# построчно (bullet-список), автоописание — одной строкой через " и ".
_OPERATOR_LABELS = {
    "in": "один из", "not_in": "ни один из",
    "lt": "меньше", "gt": "больше", "between": "между",
    "before": "раньше", "after": "позже",
    "age_on_forum_lt": "возраст на дату форума меньше",
    "filled": "заполнено", "empty": "не заполнено",
    "has_file": "есть", "no_file": "нет",
}
_NO_VALUE_OPERATORS = ("filled", "empty", "has_file", "no_file")

# D-05: три чекбокса «Полная / Краткая / Вечеринка» — «Вечеринка» покрывает ОБА внутренних
# кода сразу (как `services.applications.TRACK_FILTERS`); внутренние коды менеджеру не
# показываются нигде (T-31-08-03: код в callback_data — один из ТРЁХ UI-кодов, закрытое множество).
_TRACK_UI = (("full", "Полная"), ("short", "Краткая"), ("party", "Вечеринка"))
_TRACK_CODES = {
    "full": ("full",),
    "short": ("short",),
    "party": ("party_overnight", "party_noovernight"),
}


def _parse_id(callback_data: str) -> int | None:
    try:
        return int(callback_data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _parse_id_and_code(callback_data: str) -> tuple[int | None, str | None]:
    parts = callback_data.split(":", 2)
    if len(parts) < 3:
        return None, None
    try:
        rule_id = int(parts[1])
    except ValueError:
        return None, None
    return rule_id, parts[2]


def _short(text: str | None, limit: int) -> str:
    """Обрезка с многоточием для подписи кнопки (не парсит разметку — экранировать нечего)."""
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: max(0, limit - 1)].rstrip() + "…"


async def _load_rule(rule_id: int) -> dict | None:
    """Свежая копия правила с разобранными JSON-колонками — зовётся ПЕРЕД КАЖДЫМ действием
    (T-31-08-01), не кешируется между рендером карточки и тапом по кнопке."""
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
    """Единственная дверь мутации (`save_rule` не умеет частичный PATCH) — поля берутся из уже
    перечитанного правила, подмешивается РОВНО то одно, что меняет вызывающий хендлер."""
    fields = {
        "name": rule.get("name"),
        "city": rule.get("city"),
        "tracks": rule.get("tracks") or ["full"],
        "conditions": rule.get("conditions") or [],
        "action": rule.get("action"),
        "reject_text": rule.get("reject_text"),
        "enabled": rule.get("enabled"),
    }
    fields.update(overrides)
    return await save_rule(admin_id, rule["id"], **fields)


async def _default_new_rule_city(admin_id: int) -> str | None:
    """Привязанному менеджеру — его город (единственный доступный, D-16); суперадмину/
    непривязанному — «Все города» (переназначить можно сразу кнопкой города на карточке)."""
    if admin_id in config.ADMIN_IDS:
        return None
    return await get_staff_city(admin_id)


def _tracks_label(tracks: list[str]) -> str:
    active = [ui_label for ui_code, ui_label in _TRACK_UI if any(c in tracks for c in _TRACK_CODES[ui_code])]
    return " / ".join(active) if active else "—"


def _condition_line(cond: dict) -> str:
    step = (cond or {}).get("step")
    op = (cond or {}).get("op")
    values = (cond or {}).get("values") or []
    step_label = label_for(step) if step else "?"
    op_label = _OPERATOR_LABELS.get(op, op or "?")
    if op in _NO_VALUE_OPERATORS:
        return f"• {step_label} — {op_label}"
    if op == "between" and len(values) == 2:
        return f"• {step_label} — {op_label} {html_module.escape(str(values[0]))} и {html_module.escape(str(values[1]))}"
    values_text = ", ".join(html_module.escape(str(v)) for v in values)
    return f"• {step_label} — {op_label}: {values_text}"


def _condition_groups_text(conditions: list[list[dict]]) -> str:
    """Блок условий по макету D-09: «Группа 1 (все условия сразу): •... — ИЛИ — / Группа 2:
    •...» — квалификатор «(все условия сразу)» только у первой группы, дословно как у владельца."""
    if not conditions:
        return "Условий пока нет — добавьте первое кнопкой ниже."
    blocks = []
    for idx, group in enumerate(conditions, start=1):
        header = "Группа 1 (все условия сразу):" if idx == 1 else f"Группа {idx}:"
        cond_lines = [_condition_line(cond) for cond in (group or [])] or ["• (условий в группе пока нет)"]
        blocks.append("\n".join([header] + cond_lines))
    return "\n— ИЛИ —\n".join(blocks)


async def _rule_reject_count(rule_id: int) -> int:
    """Число ЖИВЫХ строк журнала с ИМЕННО этим правилом — читает журнал целиком (правило может
    быть «все города») и разбирает JSON `rule_ids` в Python; второго счётчика не заводим."""
    rows = await list_auto_reject_log(city_scope=None, limit=100000, offset=0, include_returned=False)
    count = 0
    for row in rows:
        try:
            ids = json.loads(row.get("rule_ids") or "[]")
        except (TypeError, ValueError):
            ids = []
        if rule_id in ids:
            count += 1
    return count


# ── Экран списка ──────────────────────────────────────────────────────────────

async def render_rules_screen(admin_id: int, offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    _scope, label = await _admin_city_view(admin_id)
    rules = await rules_for_admin(admin_id)
    kill_switch_on = await get_setting_typed("reject_rules_enabled")

    lines = ["🚫 <b>Правила автоотказа</b>"]
    if label:
        lines.append(html_module.escape(str(label)))
    lines.append("")
    if kill_switch_on:
        lines.append("Сейчас правила работают.")
    else:
        lines.append("Сейчас все правила выключены — состояние каждого правила сохранено.")
    lines.append("")

    buttons: list[list[InlineKeyboardButton]] = []
    if rules:
        lines.append(f"Всего правил: {len(rules)}")
        lines.append("")
        page = rules[offset: offset + RULES_PAGE]
        for idx, rule in enumerate(page, start=offset + 1):
            enabled = bool(rule.get("enabled"))
            paused = rule.get("paused_reason")
            icon = "⚠️" if paused else ("✅" if enabled else "🚫")
            name = rule.get("name")
            summary = await rule_summary(rule)
            title = html_module.escape(str(name)) if name else html_module.escape(summary)
            lines.append(f"{idx}. {icon} {title}")
            if name:
                lines.append(f"   {html_module.escape(summary)}")
            if paused:
                lines.append(
                    f"   Опиралось на вопрос «{html_module.escape(str(paused))}», а он выключен "
                    "или его вариант удалён из анкеты."
                )
            button_label = _short(f"{idx}. {icon} {name or summary}", 45)
            rule_id = rule["id"]
            buttons.append([InlineKeyboardButton(text=button_label, callback_data=f"arr_v:{rule_id}")])
    else:
        lines.append("Правил пока нет. Заготовка «Младше 18 на дату форума» собирается в два тапа.")

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"arr_p:{max(0, offset - RULES_PAGE)}"))
    if offset + RULES_PAGE < len(rules):
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"arr_p:{offset + RULES_PAGE}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="➕ Новое правило", callback_data="arr_new")])
    master_label = "🚫 Выключить все правила" if kill_switch_on else "✅ Включить все правила"
    buttons.append([InlineKeyboardButton(text=master_label, callback_data="arr_master")])

    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    buttons.append([back_button("admin_reject_rules")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reject_rules")
async def admin_reject_rules(callback: types.CallbackQuery):
    text, kb = await render_rules_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arr_p:"))
async def arr_page(callback: types.CallbackQuery):
    offset = _parse_id(callback.data)
    if offset is None or offset < 0:
        offset = 0
    text, kb = await render_rules_screen(callback.from_user.id, offset)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "arr_master")
async def arr_master_toggle(callback: types.CallbackQuery):
    """D-15: пишет ТОЛЬКО `reject_rules_enabled` (глобальная, не city-scoped настройка);
    `enabled` каждого правила не трогается — состояние каждого сохраняется."""
    current_on = await get_setting_typed("reject_rules_enabled")
    new_val = "off" if current_on else "on"
    await set_setting_by_admin(callback.from_user.id, "reject_rules_enabled", new_val)
    text, kb = await render_rules_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    if new_val == "on":
        await callback.answer("✅ Правила снова работают.", show_alert=True)
    else:
        await callback.answer(
            "🚫 Все правила выключены. Состояние каждого правила сохранено — включить всё "
            "обратно можно этой же кнопкой.", show_alert=True,
        )


@router.callback_query(F.data.startswith("arr_t:"))
async def arr_toggle_enabled(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    new_enabled = 0 if rule.get("enabled") else 1
    _, error = await _save_patch(callback.from_user.id, rule, enabled=new_enabled)
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    if new_enabled:
        await callback.answer("✅ Правило включено и теперь действует на подходящие заявки.", show_alert=True)
    else:
        await callback.answer(
            "🚫 Правило выключено, но осталось в списке — включить обратно можно в любой момент.",
            show_alert=True,
        )


@router.callback_query(F.data == "arr_noop")
async def arr_noop(callback: types.CallbackQuery):
    await callback.answer("Скоро — конструктор условий появится в следующем обновлении.", show_alert=True)


# ── Новое правило: заготовка или своё ─────────────────────────────────────────

async def render_new_rule_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    presets = await RULE_PRESETS()
    lines = [
        "🚫 <b>Новое правило</b>", "",
        "➕ Из заготовки — тап, поправите значения, включите:",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, preset in enumerate(presets):
        buttons.append([InlineKeyboardButton(
            text=f"🧩 {preset.get('name')}", callback_data=f"arr_preset:{idx}",
        )])
    buttons.append([InlineKeyboardButton(text="✏️ Своё правило", callback_data=f"arr_preset:{len(presets)}")])
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data="arr_p:0")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "arr_new")
async def arr_new_start(callback: types.CallbackQuery):
    text, kb = await render_new_rule_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arr_preset:"))
async def arr_preset_pick(callback: types.CallbackQuery):
    n = _parse_id(callback.data)
    if n is None or n < 0:
        await callback.answer("Не понял выбор — откройте экран заново.", show_alert=True)
        return
    presets = await RULE_PRESETS()
    default_city = await _default_new_rule_city(callback.from_user.id)
    if not await can_edit_city(callback.from_user.id, default_city):
        await callback.answer("Нет прав создать правило в этом городе.", show_alert=True)
        return
    if 0 <= n < len(presets):
        preset = presets[n]
        fields = dict(
            name=preset.get("name"), city=default_city, tracks=["full"],
            conditions=preset.get("conditions") or [], action=preset.get("action") or "reject",
            reject_text=preset.get("reject_text"), enabled=0,
        )
    elif n == len(presets):
        # «Своё правило» (D-11): пустое правило, условия добавляются конструктором (план 31-10).
        fields = dict(
            name=None, city=default_city, tracks=["full"], conditions=[],
            action="reject", reject_text=None, enabled=0,
        )
    else:
        await callback.answer("Такой заготовки нет — обновите экран.", show_alert=True)
        return
    new_id, error = await save_rule(callback.from_user.id, None, **fields)
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, new_id)
    if screen is None:
        await callback.answer("Правило создано, но не открылось — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Правило создано и выключено — включите его, когда проверите условия.")


# ── Карточка правила ──────────────────────────────────────────────────────────

async def render_rule_card(admin_id: int, rule_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """`None` — правило удалили или город правила больше не в праве этого менеджера между
    рендерами (T-31-08-01) — вызывающий отвечает алертом, не правкой."""
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(admin_id, rule.get("city")):
        return None

    name = rule.get("name")
    summary = await rule_summary(rule)
    lines = ["🚫 <b>Правило автоотказа</b>", ""]
    if name:
        lines.append(f"<b>{html_module.escape(str(name))}</b>")
        lines.append(html_module.escape(summary))
    else:
        lines.append(f"<b>{html_module.escape(summary)}</b>")

    status_line = (
        "⚠️ На паузе" if rule.get("paused_reason")
        else ("✅ Включено" if rule.get("enabled") else "🚫 Выключено")
    )
    lines.append(status_line)
    if rule.get("paused_reason"):
        lines.append(
            f"Опиралось на вопрос «{html_module.escape(str(rule['paused_reason']))}», а он "
            "выключен или его вариант удалён из анкеты. Поправьте условие или уберите его."
        )

    lines.append("")
    lines.append(_condition_groups_text(rule.get("conditions") or []))
    lines.append("")

    action_label = "🚫 Отклонять" if rule.get("action") == "reject" else "⚠️ Помечать для модератора"
    city_text = "Все города" if not rule.get("city") else await city_label(rule["city"])
    lines.append(f"Действие: {action_label}")
    lines.append(f"Город: {html_module.escape(str(city_text))}")
    lines.append(f"Треки: {_tracks_label(rule.get('tracks') or ['full'])}")

    lines.append("")
    if rule.get("reject_text"):
        lines.append("<b>Текст отказа делегату:</b>")
        lines.append(html_module.escape(str(rule["reject_text"])))
    else:
        lines.append("Текст отказа пока не задан.")

    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    groups = rule.get("conditions") or []
    if not groups:
        buttons.append([InlineKeyboardButton(text="➕ условие в группу 1", callback_data="arr_noop")])
    else:
        for idx in range(1, len(groups) + 1):
            buttons.append([InlineKeyboardButton(text=f"➕ условие в группу {idx}", callback_data="arr_noop")])
    buttons.append([InlineKeyboardButton(text="➕ новая группа (ИЛИ)", callback_data="arr_noop")])

    buttons.append([InlineKeyboardButton(text=f"↔ Действие: {action_label}", callback_data=f"arr_act:{rule_id}")])
    buttons.append([InlineKeyboardButton(text=f"🏙 Город: {city_text}", callback_data=f"arr_city:{rule_id}")])

    track_codes = rule.get("tracks") or ["full"]
    track_row = []
    for ui_code, ui_label in _TRACK_UI:
        checked = any(code in track_codes for code in _TRACK_CODES[ui_code])
        mark = "✅" if checked else "⬜"
        track_row.append(InlineKeyboardButton(text=f"{mark} {ui_label}", callback_data=f"arr_track:{rule_id}:{ui_code}"))
    buttons.append(track_row)

    buttons.append([InlineKeyboardButton(text="✏ Имя правила", callback_data=f"arr_name:{rule_id}")])
    buttons.append([InlineKeyboardButton(text="✏ Текст отказа", callback_data=f"arr_text:{rule_id}")])
    buttons.append([InlineKeyboardButton(
        text=("🚫 Выключить" if rule.get("enabled") else "✅ Включить"),
        callback_data=f"arr_t:{rule_id}",
    )])
    buttons.append([InlineKeyboardButton(text="📋 Скопировать в другой город", callback_data=f"arr_copy:{rule_id}")])
    buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"arr_d:{rule_id}")])
    buttons.append([InlineKeyboardButton(text="← К списку", callback_data="arr_p:0")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("arr_v:"))
async def arr_view(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    if screen is None:
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arr_act:"))
async def arr_act_toggle(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    new_action = "flag" if rule.get("action") == "reject" else "reject"
    _, error = await _save_patch(callback.from_user.id, rule, action=new_action)
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    if new_action == "flag":
        await callback.answer(
            "⚠️ Теперь правило только помечает заявку для модератора — пробный режим, "
            "делегат ничего не получает, решение принимает человек.", show_alert=True,
        )
    else:
        await callback.answer(
            "🚫 Теперь правило отклоняет заявку и отправляет делегату текст отказа.",
            show_alert=True,
        )


# ── Город правила ─────────────────────────────────────────────────────────────

async def render_city_assign_screen(admin_id: int, rule_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(admin_id, rule.get("city")):
        return None
    buttons: list[list[InlineKeyboardButton]] = []
    for code in city_codes():
        if not await can_edit_city(admin_id, code):
            continue
        mark = "✅ " if code == rule.get("city") else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{await city_label(code)}", callback_data=f"arr_citypick:{rule_id}:{code}",
        )])
    if await can_edit_city(admin_id, None):
        mark = "✅ " if rule.get("city") is None else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{ALL_CITIES_LABEL}", callback_data=f"arr_citypick:{rule_id}:{ALL_CITIES}",
        )])
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"arr_v:{rule_id}")])
    text = "🏙 <b>Город правила</b>\n\nВыберите город, к которому относится это правило."
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("arr_city:"))
async def arr_city_start(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    screen = await render_city_assign_screen(callback.from_user.id, rule_id)
    if screen is None:
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arr_citypick:"))
async def arr_citypick(callback: types.CallbackQuery):
    rule_id, code = _parse_id_and_code(callback.data)
    if rule_id is None or not code:
        await callback.answer("Не понял выбор — обновите экран.", show_alert=True)
        return
    if code != ALL_CITIES and code not in city_codes():
        await callback.answer("Такого города нет — обновите экран.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    new_city = None if code == ALL_CITIES else code
    if not await can_edit_city(callback.from_user.id, new_city):
        await callback.answer("Нет прав сохранить правило в этот город.", show_alert=True)
        return
    _, error = await _save_patch(callback.from_user.id, rule, city=new_city)
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Город правила изменён.")


@router.callback_query(F.data.startswith("arr_track:"))
async def arr_track_toggle(callback: types.CallbackQuery):
    rule_id, ui_code = _parse_id_and_code(callback.data)
    if rule_id is None or ui_code not in _TRACK_CODES:
        await callback.answer("Не понял трек — обновите экран.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    current = list(rule.get("tracks") or ["full"])
    underlying = _TRACK_CODES[ui_code]
    checked = any(code in current for code in underlying)
    if checked:
        remaining_groups = [
            g for g, codes in _TRACK_CODES.items()
            if g != ui_code and any(c in current for c in codes)
        ]
        if not remaining_groups:
            await callback.answer(
                "Без единого трека правило никогда не сработает — оставьте хотя бы один.",
                show_alert=True,
            )
            return
        new_tracks = [c for c in current if c not in underlying]
    else:
        new_tracks = current + [c for c in underlying if c not in current]
    _, error = await _save_patch(callback.from_user.id, rule, tracks=new_tracks)
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, rule_id)
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Имя и текст отказа (FSM) ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("arr_name:"))
async def arr_name_start(callback: types.CallbackQuery, state: FSMContext):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    await state.update_data(rre_rule_id=rule_id)
    await state.set_state(RejectRuleEdit.name)
    await callback.message.answer(
        "Пришлите короткое имя правила для списка (необязательно — просто удобная подпись для "
        "вас, делегат его не увидит). Например: «Младше 18».",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("arr_text:"))
async def arr_text_start(callback: types.CallbackQuery, state: FSMContext):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    await state.update_data(rre_rule_id=rule_id)
    await state.set_state(RejectRuleEdit.text)
    await callback.message.answer(
        "Пришлите текст, который увидит делегат при отказе по этому правилу. Можно несколько "
        "строк — разделите их знаком «;», если пишете одним сообщением с телефона.\n\n"
        "⚠️ Точная формулировка причины подскажет делегату, как переподать анкету с другим "
        "ответом и пройти снова — пишите так, как готовы, чтобы он это прочитал.",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


# WR-03-class guard (форма `afaq_text_cancel`): зарегистрирован ПЕРВЫМ — «Отмена» не уедет в имя/текст.
@router.message(RejectRuleEdit.name, F.text.in_({"Отмена", "/cancel"}))
@router.message(RejectRuleEdit.text, F.text.in_({"Отмена", "/cancel"}))
async def arr_text_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(RejectRuleEdit.name)
async def arr_name_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rule_id = data.get("rre_rule_id")
    rule = await _load_rule(rule_id) if rule_id is not None else None
    if rule is None or not await can_edit_city(message.from_user.id, rule.get("city")):
        await state.set_state(None)
        await message.answer(
            "Правило больше недоступно — откройте список заново.", reply_markup=ReplyKeyboardRemove(),
        )
        return
    raw = (message.text or "").strip()
    name_value = raw if raw and raw != "-" else None
    await state.set_state(None)
    _, error = await _save_patch(message.from_user.id, rule, name=name_value)
    if error:
        await message.answer(f"Не сохранено: {error}", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer("✅ Имя сохранено.", reply_markup=ReplyKeyboardRemove())
    screen = await render_rule_card(message.from_user.id, rule_id)
    if screen is not None:
        text, kb = screen
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(RejectRuleEdit.text)
async def arr_text_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rule_id = data.get("rre_rule_id")
    rule = await _load_rule(rule_id) if rule_id is not None else None
    if rule is None or not await can_edit_city(message.from_user.id, rule.get("city")):
        await state.set_state(None)
        await message.answer(
            "Правило больше недоступно — откройте список заново.", reply_markup=ReplyKeyboardRemove(),
        )
        return
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, пришлите текст сообщением.")
        return
    # Ловушка «Enter = отправить» на мобильных (см. CLAUDE.md): «;» разделяет строки.
    raw = message.text.strip()
    text_value = "\n".join(part.strip() for part in raw.split(";") if part.strip())
    await state.set_state(None)
    _, error = await _save_patch(message.from_user.id, rule, reject_text=text_value)
    if error:
        await message.answer(f"Не сохранено: {error}", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer("✅ Текст отказа сохранён.", reply_markup=ReplyKeyboardRemove())
    screen = await render_rule_card(message.from_user.id, rule_id)
    if screen is not None:
        text, kb = screen
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Копирование в другой город (D-12) ─────────────────────────────────────────

async def render_copy_screen(admin_id: int, rule_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(admin_id, rule.get("city")):
        return None
    codes = []
    for code in city_codes():
        if code == rule.get("city"):
            continue
        if await can_edit_city(admin_id, code):
            codes.append(code)
    lines = [
        "📋 <b>Скопировать правило в другой город</b>", "",
        html_module.escape(await rule_summary(rule)), "",
    ]
    if codes:
        lines.append("Копия создаётся выключенной — включите её на новом экране, когда проверите.")
    else:
        lines.append("Нет доступных городов — у вас нет права ни на один город кроме текущего.")
    buttons = [
        [InlineKeyboardButton(text=await city_label(code), callback_data=f"arr_copygo:{rule_id}:{code}")]
        for code in codes
    ]
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"arr_v:{rule_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("arr_copy:"))
async def arr_copy_start(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    screen = await render_copy_screen(callback.from_user.id, rule_id)
    if screen is None:
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arr_copygo:"))
async def arr_copy_go(callback: types.CallbackQuery):
    rule_id, code = _parse_id_and_code(callback.data)
    if rule_id is None or not code or code not in city_codes():
        await callback.answer("Такого города нет — обновите экран.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    if code == rule.get("city"):
        await callback.answer("Этот город уже у самого правила — выберите другой.", show_alert=True)
        return
    if not await can_edit_city(callback.from_user.id, code):
        await callback.answer("Нет прав скопировать правило в этот город.", show_alert=True)
        return
    # D-12/T-31-08-04: копия ВСЕГДА выключена — не начнёт отклонять людей до того, как менеджер её посмотрел.
    new_id, error = await save_rule(
        callback.from_user.id, None,
        name=rule.get("name"), city=code, tracks=rule.get("tracks") or ["full"],
        conditions=rule.get("conditions") or [], action=rule.get("action"),
        reject_text=rule.get("reject_text"), enabled=0,
    )
    if error:
        await callback.answer(error, show_alert=True)
        return
    screen = await render_rule_card(callback.from_user.id, new_id)
    if screen is None:
        await callback.answer("Копия создана, но не открылась — обновите список.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    city_text = await city_label(code)
    await callback.answer(
        f"Копия создана в городе «{city_text}» и выключена — включите её здесь, когда проверите.",
        show_alert=True,
    )


# ── Удаление с подтверждением ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("arr_d:"))
async def arr_delete_confirm(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило недоступно — обновите список.", show_alert=True)
        return
    name_or_summary = rule.get("name") or await rule_summary(rule)
    rejected_count = await _rule_reject_count(rule_id)
    lines = [
        "🗑 <b>Удалить правило навсегда?</b>", "",
        f"«{html_module.escape(str(name_or_summary))}»", "",
    ]
    if rejected_count:
        lines.append(f"Уже отклонило заявок: {rejected_count}. Их статус и журнал не изменятся.")
    else:
        lines.append("Пока не отклонило ни одной заявки.")
    lines.append("")
    lines.append(
        "Отменить нельзя. Если не уверены — можно вместо удаления просто выключить правило: "
        "оно останется в списке и перестанет срабатывать, включить его можно будет обратно."
    )
    buttons = [[InlineKeyboardButton(text="🗑 Да, удалить навсегда", callback_data=f"arr_dgo:{rule_id}")]]
    if rule.get("enabled"):
        buttons.append([InlineKeyboardButton(text="🚫 Выключить вместо удаления", callback_data=f"arr_t:{rule_id}")])
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"arr_v:{rule_id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arr_dgo:"))
async def arr_delete_go(callback: types.CallbackQuery):
    rule_id = _parse_id(callback.data)
    if rule_id is None:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    rule = await _load_rule(rule_id)
    if rule is None or not await can_edit_city(callback.from_user.id, rule.get("city")):
        await callback.answer("Правило уже недоступно.", show_alert=True)
        return
    # D-26/T-31-08-06: удаление не трогает журнал автоотказов и не меняет статус делегата — правило уходит, история остаётся.
    deleted, error = await delete_rule(callback.from_user.id, rule_id)
    if not deleted:
        await callback.answer(error or "Не удалось удалить правило.", show_alert=True)
        return
    text, kb = await render_rules_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Правило удалено навсегда.")
