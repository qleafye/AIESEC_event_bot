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
import reg_engine
from settings_audit import set_setting_by_admin
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


# Квик 260919-m9x: этот экран и экран вопросов анкеты жили порознь. 16–17.09 на проде
# выключили «Возраст» и включили «Дату рождения» — карточка заявки об этом не узнала, и
# менеджер пришёл с «в каких-то заявках пропал возраст, баг?». Теперь вопрос, который анкета
# спрашивает, а карточка не показывает, помечен прямо в списке, а кнопка ниже включает такие
# вопросы разом. Обратной автоматики нет намеренно: выключенный в анкете вопрос остаётся в
# карточке, пока менеджер сам его не снимет, — у старых заявок ответ на него уже есть, и
# прятать его нельзя (ровно так и пропал возраст у заявок до 17.09).
async def asked_steps() -> set[str]:
    """step_key вопросов карточки, которые анкета сейчас спрашивает на основном треке."""
    asked = set()
    for step_key in moderation_card.CARD_STEPS:
        setting_key = reg_engine.SETTING_KEY_BY_STEP.get(step_key)
        if setting_key and await reg_engine.is_step_enabled_for_track(setting_key, "full"):
            asked.add(step_key)
    return asked


def step_mark(step_key: str, steps: list[str], asked: set[str]) -> str:
    """Отметка строки: ✅ показываем · ⚠️ спрашиваем, но не показываем · ☐ не показываем."""
    if step_key in steps:
        return "✅"
    return "⚠️" if step_key in asked else "☐"


def missing_steps(steps: list[str], asked: set[str]) -> list[str]:
    """Вопросы, которые анкета спрашивает, а карточка не показывает — в порядке CARD_STEPS."""
    return [s for s in moderation_card.CARD_STEPS if s in asked and s not in steps]


async def render_modcard_text() -> str:
    steps = moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))
    limit = await get_setting_typed("modcard_answer_limit")
    asked = await asked_steps()
    missing = missing_steps(steps, asked)
    lines = [
        "🧾 <b>Поля карточки заявки</b>", "",
        "Отметьте, какие ответы показывать в карточке. Нажатие сразу сохраняется.", "",
        f"Длина ответа: {_limit_label(limit)}.", "",
    ]
    if missing:
        lines.append(
            f"⚠️ Спрашиваем в анкете, но не показываем в карточке: {len(missing)}. "
            "Кнопка «Показать всё, что спрашиваем» включит их разом.",
        )
        lines.append("")
    for step_key, label in moderation_card.CARD_STEPS.items():
        lines.append(f"{step_mark(step_key, steps, asked)} {label}")
    return "\n".join(lines)


def build_modcard_keyboard(steps: list[str], limit: int,
                           asked: set[str] | None = None) -> InlineKeyboardMarkup:
    from handlers.admin_sections import back_button  # ленивый шов (см. докстринг модуля)

    asked = asked or set()
    buttons = [
        [InlineKeyboardButton(
            text=f"{step_mark(step_key, steps, asked)} {label}",
            callback_data=f"modcard_toggle:{step_key}",
        )]
        for step_key, label in moderation_card.CARD_STEPS.items()
    ]
    if missing_steps(steps, asked):
        buttons.append([InlineKeyboardButton(
            text="⚠️ Показать всё, что спрашиваем", callback_data="modcard_sync",
        )])
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
        reply_markup=build_modcard_keyboard(steps, limit, await asked_steps()),
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
    await set_setting_by_admin(
        callback.from_user.id,
        "modcard_fields",
        "\n".join(steps) if steps else moderation_card.EMPTY_SENTINEL,
    )
    await callback.answer(toast)
    await _show_modcard(callback)


@router.callback_query(F.data == "modcard_sync")
async def modcard_sync(callback: types.CallbackQuery):
    """Показать в карточке все вопросы, которые анкета спрашивает. Кнопка только ДОБАВЛЯЕТ:
    уже включённые вопросы не снимаются, даже если анкета их больше не спрашивает (у старых
    заявок ответ на них есть — см. комментарий у `asked_steps`)."""
    steps = moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))
    asked = await asked_steps()
    added = missing_steps(steps, asked)
    if not added:
        await callback.answer("Уже показываем всё, что спрашиваем")
        return
    merged = [s for s in moderation_card.CARD_STEPS if s in asked or s in steps]
    await set_setting_by_admin(
        callback.from_user.id,
        "modcard_fields",
        "\n".join(merged) if merged else moderation_card.EMPTY_SENTINEL,
    )
    await callback.answer(f"Добавили в карточку: {len(added)}")
    await _show_modcard(callback)


@router.callback_query(F.data.startswith("modcard_limit:"))
async def modcard_limit(callback: types.CallbackQuery):
    raw = callback.data.split(":", 1)[1]
    try:
        value = int(raw)
    except ValueError:
        await callback.answer("Неизвестное значение", show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, "modcard_answer_limit", str(value))
    await callback.answer(f"Длина ответа: {_limit_label(value)}")
    await _show_modcard(callback)


@router.callback_query(F.data == "modcard_noop")
async def modcard_noop(callback: types.CallbackQuery):
    await callback.answer()
