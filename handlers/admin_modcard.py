"""Quick 260902-tzh — шов admin_moderation: экран «🧾 Поля карточки заявки».

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_moderation.py` (не `admin_settings.py` — тот стоит
ровно на потолке размера, `tests/test_module_size_convention_260816.py`), как и остальные
швы Phase 13.

Что здесь: тумблеры по каждому вопросу анкеты из `moderation_card.CARD_STEPS` (реестр
`modcard_fields`) + пресеты лимита длины ответа (реестр `modcard_answer_limit`) — то же
«кнопки вместо кодов», что у `handlers/admin_roles.py::build_role_caps_keyboard` (CLAUDE.md:
кодовые значения человеку не показываем). Хранение набора вопросов — та же форма, что
`role_caps_*`: по одной строке на step_key, пустой набор — сентинел `moderation_card.
EMPTY_SENTINEL` (иначе `_parse_setting` вернул бы дефолтные 20 вопросов на пустую строку).
"""
from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import moderation_card
from database.db import set_setting
from settings_schema import get_setting_typed
from handlers.admin import router

# Пресеты длины ответа (символов); последний — «не обрезать» (2 полных сообщения Telegram
# с запасом на служебные строки карточки).
_LIMIT_PRESETS: list[tuple[int, str]] = [
    (200, "200 символов"),
    (300, "300 символов"),
    (500, "500 символов"),
    (1000, "1000 символов"),
    (4000, "не обрезать"),
]


def _limit_label(limit: int) -> str:
    for value, label in _LIMIT_PRESETS:
        if value == limit:
            return label
    return f"{limit} символов"


async def render_modcard_text() -> str:
    steps = moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))
    limit = await get_setting_typed("modcard_answer_limit")
    lines = [
        "🧾 <b>Поля карточки заявки</b>", "",
        "Отметьте, какие ответы показывать в карточке. Нажатие сразу сохраняется.", "",
        f"Длина ответа: {_limit_label(limit)}.", "",
    ]
    for step_key, label in moderation_card.CARD_STEPS.items():
        mark = "✅" if step_key in steps else "☐"
        lines.append(f"{mark} {label}")
    return "\n".join(lines)


def build_modcard_keyboard(steps: list[str], limit: int) -> InlineKeyboardMarkup:
    from handlers.admin_sections import back_button  # ленивый шов (см. докстринг модуля)

    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if step_key in steps else "☐ ") + label,
            callback_data=f"modcard_toggle:{step_key}",
        )]
        for step_key, label in moderation_card.CARD_STEPS.items()
    ]
    buttons.append([InlineKeyboardButton(text="── длина ответа ──", callback_data="modcard_noop")])
    buttons.append([
        InlineKeyboardButton(
            text=(("✅ " if value == limit else "") + preset_label),
            callback_data=f"modcard_limit:{value}",
        )
        for value, preset_label in _LIMIT_PRESETS
    ])
    buttons.append([back_button("modcard_open")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_modcard(callback: types.CallbackQuery) -> None:
    steps = moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))
    limit = await get_setting_typed("modcard_answer_limit")
    await callback.message.edit_text(
        await render_modcard_text(),
        parse_mode="HTML",
        reply_markup=build_modcard_keyboard(steps, limit),
    )


@router.callback_query(F.data == "modcard_open")
async def modcard_open(callback: types.CallbackQuery):
    await _show_modcard(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("modcard_toggle:"))
async def modcard_toggle(callback: types.CallbackQuery):
    step_key = callback.data.split(":", 1)[1]
    if step_key not in moderation_card.CARD_STEPS:
        await callback.answer("Неизвестный вопрос", show_alert=True)
        return
    label = moderation_card.CARD_STEPS[step_key]
    steps = moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))
    if step_key in steps:
        steps = [s for s in steps if s != step_key]
        toast = f"{label}: скрыт"
    else:
        # Порядок как в CARD_STEPS, а не «в порядке нажатий» — чтобы список в
        # render_modcard_text не прыгал между перерисовками (тот же приём, что roles_cap).
        steps = [s for s in moderation_card.CARD_STEPS if s == step_key or s in steps]
        toast = f"{label}: показываем"
    await set_setting(
        "modcard_fields",
        "\n".join(steps) if steps else moderation_card.EMPTY_SENTINEL,
    )
    await callback.answer(toast)
    await _show_modcard(callback)


@router.callback_query(F.data.startswith("modcard_limit:"))
async def modcard_limit(callback: types.CallbackQuery):
    raw = callback.data.split(":", 1)[1]
    try:
        value = int(raw)
    except ValueError:
        await callback.answer("Неизвестное значение", show_alert=True)
        return
    await set_setting("modcard_answer_limit", str(value))
    await callback.answer(f"Длина ответа: {_limit_label(value)}")
    await _show_modcard(callback)


@router.callback_query(F.data == "modcard_noop")
async def modcard_noop(callback: types.CallbackQuery):
    await callback.answer()
