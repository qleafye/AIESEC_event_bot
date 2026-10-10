"""«🚀 Первая настройка» в боте (бэклог «🛠», P1): тот же мастер, что в приложении, — для
событий без приложения и для суперадмина, который настраивает всё из чата.

Шаги — ОДИН список с приложением (`miniapp/setup_wizard.py::STEPS`, чистый модуль без FastAPI):
второго порядка шагов в проекте нет. Мастер не заводит своего редактора: кнопка поля ведёт в
тот же экран правки, что кнопка на экране группы (`settings_edit:` / `settings_photo:`), — её
находит `admin_settings_search.candidates()`. После сохранения `settings_return_screen`
возвращает менеджера в шаг мастера, а не на экран группы (`pop_return`).

Поле, которого в боте нет (оформление приложения), показывается подписью «в приложении»;
кнопки меню ведут на «🔘 Кнопки меню», шаг «Вопросы анкеты» — в раздел «📝 Анкета».

Только суперадмин (`config.ADMIN_IDS`) — гейт повторён в каждом хендлере, как у «🔗 Какая
таблица». Шов к общему `admin.router`, импорт из хвоста `handlers/admin.py`.
"""
from __future__ import annotations

import html
import time

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database.db import get_setting
from handlers.admin import router
from miniapp.setup_wizard import STEPS, TEXTS, WizardStep, step_done, visible_steps
from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed

ONLY_SUPERADMIN = "Первую настройку проходит суперадмин."
NOT_IN_BOT = "в приложении"
APP_HINT = "Это поле настраивается в приложении: «Настройки» → поиск по названию."

# Флаги модулей, от которых зависят шаги (`WizardStep.requires`) — тот же набор, что у роутера
# приложения (`miniapp/routers/settings.py::_SETUP_MODULE_FLAGS`).
_MODULE_FLAGS = tuple(sorted({s.requires for s in STEPS if s.requires}))

# «Вернуть в шаг мастера после сохранения»: админ -> (ключ шага, момент). Живёт в памяти
# процесса: после рестарта менеджер просто вернётся на экран группы, как без мастера. Срок —
# чтобы брошенная правка не утащила в мастер через час совсем другую правку.
_RETURN_TTL_S = 30 * 60
_return_to: dict[int, tuple[str, str, float]] = {}


def set_return(admin_id: int, step_key: str, field_key: str) -> None:
    _return_to[admin_id] = (step_key, field_key, time.monotonic())


# Экраны бота, которые мастер открывает целиком (не поле): их «Назад» зовёт
# `settings_return_screen(callback_data=<экран>)` — по нему и возвращаемся в шаг.
_SCREENS = {
    "admin_menu_buttons": "🔘 Кнопки меню",
    "admin_reg_questions": "📋 Вопросы регистрации",
}
_SCREEN_MARK = "screen:"


def _matches(field_key: str, setting_key: str | None, callback_data: str | None = None) -> bool:
    """Сохранили именно поле мастера: тот же ключ, его городское значение
    (`{key}__city__{code}`) или файл фото/документа поля (`{key}_photo_file_id`); для экрана —
    выход с этого экрана («Назад» зовёт возврат с его callback_data)."""
    if field_key.startswith(_SCREEN_MARK):
        return callback_data == field_key[len(_SCREEN_MARK):]
    if not setting_key:
        return False
    base = setting_key.split("__city__", 1)[0]
    return base in (field_key, f"{field_key}_photo_file_id", f"{field_key}_doc_file_id")


def pop_return(admin_id: int, setting_key: str | None = None, callback_data: str | None = None) -> str | None:
    """Шаг мастера, если сохранение — правка поля, открытого из мастера (ревью 10.10: раньше
    возврат перехватывал ЛЮБОЕ сохранение и тумблер в течение 30 минут). Чужой возврат
    отметку не трогает — она снимается своим сохранением, сроком или входом в мастер."""
    entry = _return_to.get(admin_id)
    if entry is None:
        return None
    step_key, field_key, at = entry
    if time.monotonic() - at > _RETURN_TTL_S:
        _return_to.pop(admin_id, None)
        return None
    if not _matches(field_key, setting_key, callback_data):
        return None
    _return_to.pop(admin_id, None)
    return step_key


def _is_super(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _editors() -> dict[str, str]:
    """Ключ поля -> callback экрана правки бота (тот же, что у кнопки на экране группы)."""
    from handlers.settings.admin_settings_search import candidates  # ленивый шов (цикл импортов)

    return {c.key: c.extra["cb"] for c in candidates()}


def _photo_prefixes() -> set[str]:
    from handlers.settings import admin_settings as s  # ленивый шов

    return {prefix for prefix, _label, _prompt in s.PHOTO_FIELDS}


def _field_label(key: str) -> str:
    from handlers.settings import admin_settings as s  # ленивый шов

    for prefix, label, _prompt in s.PHOTO_FIELDS:
        if prefix == key:
            return label
    return SETTINGS_SCHEMA.get(key, {}).get("label") or key


async def _filled(step: WizardStep, key: str, photos: set[str], city: str | None = None) -> bool:
    """То же правило «задано», что у приложения: своё значение (или явный выбор), а для
    `accept_default` — и значение по умолчанию, если оно не пустое. При городе в шапке
    городская настройка считается заданной и своим значением города (его пишет редактор),
    и общим — город его наследует."""
    if key in photos:
        return bool(await get_setting(f"{key}_photo_file_id"))
    if city and SETTINGS_SCHEMA.get(key, {}).get("per_city"):
        from domain.cities import per_city_key

        composed = per_city_key(key, city)
        raw_city = await get_setting(composed) if composed else None
        if raw_city is not None and str(raw_city) != "":
            return True
    raw = await get_setting(key)
    if raw is not None and str(raw) != "":
        return True
    if step.explicit_choice:
        return False
    if step.accept_default:
        return bool(SETTINGS_SCHEMA.get(key, {}).get("default"))
    return False


async def _steps() -> list[WizardStep]:
    event_type = await get_setting_typed("event_type")
    flags = {name: (await get_setting_typed(name)) == "on" for name in _MODULE_FLAGS}
    return visible_steps(event_type, flags)


async def _header_city(admin_id: int | None) -> str | None:
    """Город шапки админки (как у редактора бота), `None` — все города или модуль выключен."""
    if admin_id is None:
        return None
    from domain.cities import ALL_CITIES, admin_selected_city, cities_module_on

    if not await cities_module_on():
        return None
    city = await admin_selected_city(admin_id)
    return None if city in (None, ALL_CITIES) else city


async def _status(steps: list[WizardStep], admin_id: int | None = None) -> dict[str, tuple[bool, dict[str, bool]]]:
    photos = _photo_prefixes()
    city = await _header_city(admin_id)
    out = {}
    for step in steps:
        filled = {key: await _filled(step, key, photos, city) for key in step.fields}
        out[step.key] = (step_done(step, filled), filled)
    return out


async def overview_screen(admin_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    from handlers.settings.admin_sections import back_button  # ленивый шов

    steps = await _steps()
    status = await _status(steps, admin_id)
    counted = [s for s in steps if s.kind == "fields"]
    done = sum(1 for s in counted if status[s.key][0])
    text = (
        f"<b>{TEXTS['title']}</b>\n\n"
        f"{TEXTS['progress_note_text'].format(done=done, total=len(counted))}\n\n"
        "Пройдите шаги по порядку — каждый ведёт в обычный экран правки, после сохранения бот "
        "вернёт сюда. ✅ — шаг закрыт, ⬜ — ещё нет, ℹ️ — подсказка без полей."
    )
    rows = []
    for step in steps:
        mark = "ℹ️" if step.kind != "fields" else ("✅" if status[step.key][0] else "⬜")
        rows.append([InlineKeyboardButton(text=f"{mark} {step.title}", callback_data=f"setupw_s:{step.key}")])
    rows.append([back_button("admin_setup_wizard")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def step_screen(step_key: str, admin_id: int | None = None) -> tuple[str, InlineKeyboardMarkup] | None:
    steps = await _steps()
    keys = [s.key for s in steps]
    if step_key not in keys:
        return None
    index = keys.index(step_key)
    step = steps[index]
    done, filled = (await _status([step], admin_id))[step.key]

    lines = [
        TEXTS["step_of_text"].format(n=index + 1, m=len(steps)),
        f"<b>{html.escape(step.title)}</b>",
        "",
        html.escape(step.hint),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    editors = _editors()
    app_only = False
    if step.kind == "fields":
        lines.append("")
        screens_added: set[str] = set()
        for key in step.fields:
            label = _field_label(key)
            state = TEXTS["value_set_text"] if filled.get(key) else TEXTS["value_default_text"]
            if key in editors:
                rows.append([InlineKeyboardButton(
                    text=f"{'✅' if filled.get(key) else '⬜'} {label}"[:60],
                    callback_data=f"setupw_f:{step.key}:{key}",
                )])
                lines.append(f"• {html.escape(label)} — {state}")
            elif key.startswith(("menu_", "reg_q_")):
                # Кнопки меню и вопросы анкеты правятся своими экранами бота, а не полем.
                screen = "admin_menu_buttons" if key.startswith("menu_") else "admin_reg_questions"
                if screen not in screens_added:
                    rows.append([InlineKeyboardButton(
                        text=_SCREENS[screen], callback_data=f"setupw_scr:{step.key}:{screen}",
                    )])
                    screens_added.add(screen)
                lines.append(f"• {html.escape(label)} — {state}")
            else:
                app_only = True
                lines.append(f"• {html.escape(label)} — {state} ({NOT_IN_BOT})")
        if app_only:
            lines += ["", APP_HINT]
    elif step.kind == "link":
        rows.append([InlineKeyboardButton(text="📝 Открыть раздел «Анкета»", callback_data="admin_sec:form")])

    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text=f"← {TEXTS['back_text']}", callback_data=f"setupw_s:{keys[index - 1]}"))
    if index + 1 < len(keys):
        nav.append(InlineKeyboardButton(text=f"{TEXTS['next_text']} →", callback_data=f"setupw_s:{keys[index + 1]}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=f"📋 {TEXTS['all_steps_text']}", callback_data="admin_setup_wizard")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_setup_wizard")
async def setup_wizard_overview(callback: types.CallbackQuery):
    if not _is_super(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    _return_to.pop(callback.from_user.id, None)
    text, kb = await overview_screen(callback.from_user.id)
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("setupw_s:"))
async def setup_wizard_step(callback: types.CallbackQuery):
    if not _is_super(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    screen = await step_screen(callback.data.split(":", 1)[1], callback.from_user.id)
    if screen is None:
        # Шаг исчез (сменили тип события или выключили модуль) — показываем список заново.
        screen = await overview_screen(callback.from_user.id)
    await _show(callback, *screen)
    await callback.answer()


@router.callback_query(F.data.startswith("setupw_f:"))
async def setup_wizard_field(callback: types.CallbackQuery, state: FSMContext):
    if not _is_super(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    _prefix, step_key, key = callback.data.split(":", 2)
    target = _editors().get(key)
    if target is None:
        await callback.answer(APP_HINT, show_alert=True)
        return
    set_return(callback.from_user.id, step_key, key)
    from handlers.settings import admin_settings  # ленивый шов

    edit = callback.model_copy(update={"data": target})
    if target.startswith("settings_photo:"):
        await admin_settings.settings_photo_start(edit, state)
    elif target.startswith("settings_file:"):
        await admin_settings.settings_file_start(edit, state)
    else:
        await admin_settings.settings_edit_start(edit, state)


@router.callback_query(F.data.startswith("setupw_scr:"))
async def setup_wizard_screen(callback: types.CallbackQuery):
    """Экран бота из шага мастера («🔘 Кнопки меню», «📋 Вопросы регистрации»): «Назад» с
    него вернёт в тот же шаг (отметка возврата по callback_data экрана)."""
    if not _is_super(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    _prefix, step_key, screen = callback.data.split(":", 2)
    if screen not in _SCREENS:
        await callback.answer()
        return
    set_return(callback.from_user.id, step_key, _SCREEN_MARK + screen)
    opened = callback.model_copy(update={"data": screen})
    if screen == "admin_menu_buttons":
        from handlers.regform.admin_reg_config import show_menu_buttons  # ленивый шов
        await show_menu_buttons(opened)
    else:
        from handlers.regform.admin_reg_percity import show_reg_questions  # ленивый шов
        await show_reg_questions(opened)
