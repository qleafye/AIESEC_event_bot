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
же приём, что `handlers/admin_faq.py::_card_out_of_scope`).

Задача 1 (этот срез): экран списка, вход в разделе, capability, общий рубильник, заготовки.
Карточка правила (задача 2) и копирование/удаление (задача 3) — в следующих коммитах того же
плана; пока `arr_t`/создание из заготовки возвращают на список (задача 2 заменит это открытием
карточки, как только она появится)."""
import html as html_module
import json

from aiogram import F, types

from config import config
from database.db import get_reject_rule, get_staff_city
from handlers.admin import router
from handlers.admin_core import _admin_city_view
from services.reject_rules import (
    RULE_PRESETS,
    can_edit_city,
    rule_summary,
    rules_for_admin,
    save_rule,
)
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

RULES_PAGE = 8


def _parse_id(callback_data: str) -> int | None:
    try:
        return int(callback_data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


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
    """Задача 1: перерисовывает СПИСОК после переключения (карточки ещё нет — задача 2
    заменит редрей на карточку, как только `render_rule_card` появится)."""
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
    text, kb = await render_rules_screen(callback.from_user.id)
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
    """Задача 1: после создания возвращает на СПИСОК (карточки ещё нет — задача 2 заменит
    редрей на открытие созданного правила картой)."""
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
    _new_id, error = await save_rule(callback.from_user.id, None, **fields)
    if error:
        await callback.answer(error, show_alert=True)
        return
    text, kb = await render_rules_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Правило создано и выключено — включите его в списке, когда проверите условия.")
