"""Phase 28 (28-08, SU-08) — шов admin_moderation: экран «🧮 Правила балла».

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_moderation.py`, сразу после `handlers.admin_modcard`
(та же позиция в снапшоте — quick 260902-tzh), той же формой: `render_*_text()` +
`build_*_keyboard()` + toggle/limit-хендлеры + noop.

Что здесь: чекбокс-пикеры четырёх скоринговых множеств (`reg_engine.scoring_rules()`,
план 28-07) — те же «кнопки вместо кодов», что у `handlers/admin_modcard.py`, но с ОДНИМ
принципиальным отличием: `admin_modcard.py` рисует ЗАКРЫТЫЙ список (`moderation_card.
CARD_STEPS` — модульная константа), а варианты здесь ДИНАМИЧЕСКИЕ — текущий список ответов
вопроса анкеты (`reg_engine.options(step_key)`), который менеджер правит сам через
«✏️ Направления обучения (варианты)» и т.п. Каждый рендер этого экрана заново читает
`reg_engine.options(...)` — поменял вопрос, пикер это сразу видит (в этом разница с
`type: "multi"`, который резолвит статический словарь модуля, RESEARCH Anti-Patterns).

Хранение набора — та же форма, что `modcard_fields`: порядок ВАРИАНТОВ (не порядок нажатий),
пустой набор — сентинел `moderation_card.EMPTY_SENTINEL` (иначе `_parse_setting` вернул бы
`raw` как единственный элемент списка — не «дефолт», реестровый default этих ключей `None`,
но сентинел одинаков во всех пикерах проекта, тот же приём, что и у `role_caps_*`). Подписи,
которые менеджер отметил РАНЬШЕ, но которых уже нет в текущем списке вариантов вопроса, не
пропадают молча — отдельная строка «⚠️ …» в тексте и своя кнопка «✕ убрать» в клавиатуре
(28-UI-SPEC §8, тот же принцип, что у веб-чипа `form.js::multiControl`).
"""
from __future__ import annotations

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import reg_engine
from moderation_card import EMPTY_SENTINEL
from settings_audit import set_setting_by_admin
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from handlers.admin import router

# (ключ реестра, step_key анкеты, заголовок группы на экране, вес в формуле ТЗ §3.6 — только
# для текста, сама формула жёстко зашита в reg_engine.compute_score).
_SET_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("score_it_fields", "study_field", "🎯 Направления обучения, которые считаются IT (вместе со «Стек от» — 2 балла)"),
    ("score_senior_statuses", "education_status", "🎓 Статусы образования, которые считаются «старшими» (вместе с «Курс от» — 2 балла)"),
    ("score_readiness_counts", "readiness", "🚀 Готовность выйти на работу, которая даёт балл (1 балл)"),
    ("score_experience_counts", "experience", "💼 Опыт работы, который даёт балл (1 балл)"),
)
_GROUP_BY_KEY: dict[str, tuple[str, str, str]] = {group[0]: group for group in _SET_GROUPS}

# Пресеты порогов (план 28-08: «Курс от» 1..6, «Стек от» 1..5).
_LIMIT_RANGES: dict[str, range] = {
    "score_course_from": range(1, 7),
    "score_stack_from": range(1, 6),
}

_UNKNOWN_SET_TEXT = "Неизвестный набор — обновите экран"
_UNKNOWN_VALUE_TEXT = "Неизвестное значение"
_STALE_MARK_TEXT = "варианта больше нет"


def _chosen(raw: list[str] | None) -> list[str]:
    """Значение ключа реестра (`type: "list"`) без сентинела пустого набора — тот же приём,
    что `moderation_card.enabled_steps` для `modcard_fields`."""
    if not raw or list(raw) == [EMPTY_SENTINEL]:
        return []
    return list(raw)


async def _group_state(key: str, step_key: str) -> tuple[list[str], list[str], list[str]]:
    """`(варианты_сейчас, отмеченные_из_текущих_в_их_порядке, пропавшие_в_порядке_хранения)`."""
    variants = await reg_engine.options(step_key)
    raw = _chosen(await get_setting_typed(key))
    chosen = set(raw)
    valid = [v for v in variants if v in chosen]
    stale = [v for v in raw if v not in variants]
    return variants, valid, stale


async def _save_group(
    admin_id: int | None, key: str, variants: list[str], chosen: set[str], stale: list[str],
) -> None:
    new_value = [v for v in variants if v in chosen] + stale
    await set_setting_by_admin(
        admin_id, key, "\n".join(new_value) if new_value else EMPTY_SENTINEL,
    )


async def render_scoring_text() -> str:
    lines = [
        "🧮 <b>Правила балла</b>", "",
        "Заявки получают балл по ответам анкеты — чем он выше, тем раньше заявку стоит "
        "разобрать (максимум 7). Отметьте галочками варианты ответов, которые должны его "
        "давать; курс и стек — порогом в самом низу.",
    ]
    for key, step_key, title in _SET_GROUPS:
        variants, valid, stale = await _group_state(key, step_key)
        chosen_set = set(valid)
        lines.append("")
        lines.append(f"<b>{title}</b>")
        if not variants:
            lines.append("(в вопросе анкеты сейчас нет ни одного варианта)")
        for variant in variants:
            mark = "✅" if variant in chosen_set else "☐"
            lines.append(f"{mark} {variant}")
        for label in stale:
            lines.append(f"⚠️ {label} — {_STALE_MARK_TEXT}")
    course_from = await get_setting_typed("score_course_from")
    stack_from = await get_setting_typed("score_stack_from")
    lines.append("")
    lines.append(f"<b>🎓 Курс от:</b> {course_from}")
    lines.append(f"<b>🧰 Стек от (пунктов):</b> {stack_from}")
    return "\n".join(lines)


def build_scoring_keyboard(
    groups_state: list[tuple[str, list[str], list[str], list[str]]],
    course_from: int,
    stack_from: int,
) -> InlineKeyboardMarkup:
    from handlers.admin_sections import back_button  # ленивый шов (см. докстринг модуля)

    buttons: list[list[InlineKeyboardButton]] = []
    for key, variants, valid, stale in groups_state:
        chosen_set = set(valid)
        for idx, variant in enumerate(variants):
            mark = "✅ " if variant in chosen_set else "☐ "
            buttons.append([InlineKeyboardButton(text=mark + variant, callback_data=f"scoring_toggle:{key}:{idx}")])
        for idx, label in enumerate(stale):
            buttons.append([InlineKeyboardButton(
                text=f"✕ {label} — {_STALE_MARK_TEXT}", callback_data=f"scoring_drop:{key}:{idx}",
            )])
        buttons.append([InlineKeyboardButton(text="── · ──", callback_data="scoring_noop")])
    buttons.append([
        InlineKeyboardButton(
            text=(("✅ " if value == course_from else "") + str(value)),
            callback_data=f"scoring_limit:score_course_from:{value}",
        )
        for value in _LIMIT_RANGES["score_course_from"]
    ])
    buttons.append([
        InlineKeyboardButton(
            text=(("✅ " if value == stack_from else "") + str(value)),
            callback_data=f"scoring_limit:score_stack_from:{value}",
        )
        for value in _LIMIT_RANGES["score_stack_from"]
    ])
    buttons.append([back_button("admin_reg_scoring")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_scoring(callback: types.CallbackQuery) -> None:
    groups_state = []
    for key, step_key, _title in _SET_GROUPS:
        variants, valid, stale = await _group_state(key, step_key)
        groups_state.append((key, variants, valid, stale))
    course_from = await get_setting_typed("score_course_from")
    stack_from = await get_setting_typed("score_stack_from")
    await callback.message.edit_text(
        await render_scoring_text(),
        parse_mode="HTML",
        reply_markup=build_scoring_keyboard(groups_state, course_from, stack_from),
    )


@router.callback_query(F.data == "admin_reg_scoring")
async def admin_reg_scoring(callback: types.CallbackQuery):
    await _show_scoring(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("scoring_toggle:"))
async def scoring_toggle(callback: types.CallbackQuery):
    _, key, idx_raw = callback.data.split(":", 2)
    group = _GROUP_BY_KEY.get(key)
    if group is None:
        await callback.answer(_UNKNOWN_SET_TEXT, show_alert=True)
        return
    _key, step_key, _title = group
    try:
        idx = int(idx_raw)
    except ValueError:
        await callback.answer()
        return
    variants, valid, stale = await _group_state(key, step_key)
    if idx < 0 or idx >= len(variants):
        # Список вариантов вопроса поменялся между рендером и тапом — редкий случай (менеджер
        # правит вопрос анкеты и пикер балла одновременно), fail-soft: просто перерисовать.
        await callback.answer(_UNKNOWN_SET_TEXT, show_alert=True)
        await _show_scoring(callback)
        return
    variant = variants[idx]
    chosen = set(valid)
    if variant in chosen:
        chosen.discard(variant)
        toast = f"{variant}: снято"
    else:
        chosen.add(variant)
        toast = f"{variant}: отмечено"
    await _save_group(callback.from_user.id, key, variants, chosen, stale)
    await callback.answer(toast)
    await _show_scoring(callback)


@router.callback_query(F.data.startswith("scoring_drop:"))
async def scoring_drop(callback: types.CallbackQuery):
    _, key, idx_raw = callback.data.split(":", 2)
    group = _GROUP_BY_KEY.get(key)
    if group is None:
        await callback.answer(_UNKNOWN_SET_TEXT, show_alert=True)
        return
    _key, step_key, _title = group
    try:
        idx = int(idx_raw)
    except ValueError:
        await callback.answer()
        return
    variants, valid, stale = await _group_state(key, step_key)
    if idx < 0 or idx >= len(stale):
        await callback.answer(_UNKNOWN_SET_TEXT, show_alert=True)
        await _show_scoring(callback)
        return
    dropped = stale[idx]
    new_stale = [label for i, label in enumerate(stale) if i != idx]
    await _save_group(callback.from_user.id, key, variants, set(valid), new_stale)
    await callback.answer(f"{dropped}: убрано")
    await _show_scoring(callback)


@router.callback_query(F.data.startswith("scoring_limit:"))
async def scoring_limit(callback: types.CallbackQuery):
    _, key, raw_value = callback.data.split(":", 2)
    allowed = _LIMIT_RANGES.get(key)
    if allowed is None:
        await callback.answer(_UNKNOWN_VALUE_TEXT, show_alert=True)
        return
    try:
        value = int(raw_value)
    except ValueError:
        await callback.answer(_UNKNOWN_VALUE_TEXT, show_alert=True)
        return
    if value not in allowed:
        await callback.answer(_UNKNOWN_VALUE_TEXT, show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, key, str(value))
    label = SETTINGS_SCHEMA[key]["label"]
    await callback.answer(f"{label}: {value}")
    await _show_scoring(callback)


@router.callback_query(F.data == "scoring_noop")
async def scoring_noop(callback: types.CallbackQuery):
    await callback.answer()
