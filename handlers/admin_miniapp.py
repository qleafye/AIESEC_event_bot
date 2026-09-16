"""Phase 19 (08, D-06) + Phase 19.1 (07, D-20): шов admin_miniapp — экран «🎨 Оформление» Mini
App (тумблеры/разделы) — точка входа в блок пресетов и ручек кастома.

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_settings.py`, ПОСЛЕДНЕЙ строкой (после
`admin_dashboard`), как и остальные швы Phase 13/15.

Что здесь: два тумблера («Mini App включён», «Только менеджерам»), восемь чекбоксов разделов
приложения (`miniapp_section_*`, подписи из SETTINGS_SCHEMA — код ключа менеджеру никогда не
показывается, CLAUDE.md «бот для людей») и кнопка входа в пресеты/ручки кастома. Сама правка
цвета/шрифта/лого/обложки/стикеров (D-04/D-20) — ВТОРОЙ шов, `handlers/admin_miniapp_theme.py`
(план 19.1-07): вынесен в отдельный файл потолком размера модуля (docs/CONVENTIONS.md), но делит с
этим файлом одну и ту же FSM-группу `MiniAppTheme` и общий `router`.

Точки входа приложения (текстовая кнопка меню / inline web_app / кнопка меню чата) и
`sync_chat_menu_button` — план 19-08, задача 2, тот же файл.
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

from config import config
from settings_audit import set_setting_by_admin
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from handlers.admin import router

logger = logging.getLogger(__name__)

# Порядок разделов на экране (D-06): зеркало экранов делегата/менеджера фазы 19.
# "miniapp_section_form" (Phase 21 Plan 02, FORM-SYNC-05, D-08) — раздел «📝 Анкета»; общий
# toggle_miniapp_section подхватывает суффикс сам, нового хендлера не заводим.
SECTION_KEYS = [
    "miniapp_section_tasks",
    "miniapp_section_coins",
    "miniapp_section_leaderboard",
    "miniapp_section_profile",
    "miniapp_section_form",
    # Quick 260906-8uq (FAQ-05): раздел «❓ Частые вопросы» — рядом с «form»/«profile»
    # (делегатские разделы вместе), тот же общий toggle_miniapp_section подхватывает суффикс сам.
    "miniapp_section_faq",
    "miniapp_section_review",
    # Phase 23 (APP-TINDER-01, D-09): раздел «🗂 Отбор заявок» — рядом с «review» (менеджерские
    # разделы вместе), тот же общий toggle_miniapp_section подхватывает суффикс сам.
    "miniapp_section_applications",
    # Quick 260904-2cj (QJRN-01..04): раздел «❓ Вопросы делегатов» — рядом с «applications»
    # (менеджерские разделы вместе), тот же общий toggle_miniapp_section подхватывает суффикс сам.
    "miniapp_section_questions",
    "miniapp_section_admin_tasks",
    "miniapp_section_stats",
    "miniapp_section_settings",
]

_SECTION_BY_SUFFIX = {key[len("miniapp_section_"):]: key for key in SECTION_KEYS}


async def render_miniapp_settings_text() -> str:
    enabled = await get_setting_typed("miniapp_enabled") == "on"
    staff_only = await get_setting_typed("miniapp_staff_only") == "on"
    motion = await get_setting_typed("miniapp_motion")

    lines = ["🎨 <b>Оформление приложения</b>", ""]
    lines.append(
        ("✅" if enabled else "☐")
        + " Приложение включено — кнопка в меню бота видна, только пока включено."
    )
    lines.append(
        ("✅" if staff_only else "☐")
        + " Только менеджерам — делегаты кнопку не увидят, у менеджеров есть запасной вход."
    )
    motion_label = SETTINGS_SCHEMA["miniapp_motion"]["option_labels"].get(motion, motion)
    lines.append(f"✨ Анимации приложения: {motion_label}")
    lines.append("")
    lines.append("Разделы, которые видны в приложении:")
    for key in SECTION_KEYS:
        on = await get_setting_typed(key) == "on"
        label = SETTINGS_SCHEMA[key]["label"]
        lines.append(("✅ " if on else "☐ ") + label)
    lines.append("")
    if config.DASHBOARD_PUBLIC_URL:
        url = config.DASHBOARD_PUBLIC_URL.rstrip("/") + "/app"
        lines.append(f"Адрес приложения: {html_module.escape(url)}")
    else:
        lines.append(
            "⚠️ Адрес приложения не задан — точки входа скрыты, пока его не настроят при деплое."
        )
    return "\n".join(lines)


async def build_miniapp_settings_keyboard() -> InlineKeyboardMarkup:
    enabled = await get_setting_typed("miniapp_enabled") == "on"
    staff_only = await get_setting_typed("miniapp_staff_only") == "on"

    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if enabled else "☐ ") + "Mini App включён",
            callback_data="miniapp_toggle_enabled",
        )],
        [InlineKeyboardButton(
            text=("✅ " if staff_only else "☐ ") + "Только менеджерам",
            callback_data="miniapp_toggle_staff_only",
        )],
    ]
    for key in SECTION_KEYS:
        on = await get_setting_typed(key) == "on"
        label = SETTINGS_SCHEMA[key]["label"]
        suffix = key[len("miniapp_section_"):]
        buttons.append([InlineKeyboardButton(
            text=("✅ " if on else "☐ ") + label,
            callback_data=f"miniapp_section:{suffix}",
        )])
    # Quick 260915-4mw (ANIM-01..06): циклический тумблер (не чекбокс — три состояния, не два)
    # той же кнопкой, что мастер первой настройки в приложении подхватывает сам ключ реестра.
    motion = await get_setting_typed("miniapp_motion")
    motion_label = SETTINGS_SCHEMA["miniapp_motion"]["option_labels"].get(motion, motion)
    buttons.append([InlineKeyboardButton(
        text=f"✨ Анимации: {motion_label}", callback_data="miniapp_cycle_motion",
    )])
    # Phase 19.1 (07, D-20): вход во второй шов — пресеты BlueBook/YouLead/Своя и ручки
    # кастома (цвета/шрифт/тон/лого/обложка/паттерн/стикеры/иконка монеты).
    # handlers/admin_miniapp_theme.py.
    buttons.append([InlineKeyboardButton(
        text="🎭 Пресеты и ручки оформления", callback_data="miniapp_theme_open",
    )])
    # Phase 20 (20-03): «Назад» ведёт в раздел-владелец экрана («🔧 Управление»).
    from handlers.admin_sections import back_button  # ленивый шов: модульный импорт даст цикл
    buttons.append([back_button("admin_miniapp_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def sync_chat_menu_button(bot, chat_id: int | None = None, lang: str = "ru") -> None:
    """Phase 19 (08, D-10/T-19-52): the ONE function that sets the chat menu button, called
    from THREE places — `main.py` at startup, `toggle_miniapp_enabled` below (right after the
    setting is written), and `handlers/reg_lang.py::lang_pick_choose` (right after a delegate
    picks/switches their language). Without a single shared function called from all three,
    the toggle would only take visible effect on the NEXT bot restart, breaking the
    "выключение тумблера убирает точки входа сразу" success criterion. Fail-soft is the
    CALLER's job (every call site wraps this in try/except) — an unreachable Telegram must
    never break the settings screen, block startup, or break language selection.

    Квик 260915-skg (P7) заводил ЭТУ функцию как глобальную (без chat_id/lang) — «языка
    конкретного делегата здесь нет и быть не может» было верно ТОЛЬКО для двух исходных call
    site'ов (старт бота, тумблер оформления), у которых действительно нет делегата под рукой.
    Квик 260917-en (приёмка 17.09, п.1): Telegram Bot API поддерживает `setChatMenuButton` с
    `chat_id` — кнопку МОЖНО ставить индивидуально на чат, и `lang_pick_choose` знает и chat_id,
    и только что выбранный язык делегата. `chat_id=None` (два старых call site'а) — глобальный
    дефолт для чатов, для которых своя кнопка ещё не ставилась, byte-for-byte прежнее поведение
    (`lang="ru"` по умолчанию — перевод не запускается вовсе, `tr_text` вернёт тот же объект)."""
    enabled = await get_setting_typed("miniapp_enabled") == "on"
    url = config.DASHBOARD_PUBLIC_URL
    kwargs = {"chat_id": chat_id} if chat_id is not None else {}
    if enabled and url:
        button_text = await get_setting_typed("miniapp_open_button")
        if lang == "en":
            from handlers import reg_i18n
            from services import i18n as i18n_service
            tr_map = await i18n_service.load_map("en")
            button_text = reg_i18n.tr_text(button_text, "en", tr_map)
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text=button_text,
                web_app=WebAppInfo(url=url.rstrip("/") + "/app"),
            ),
            **kwargs,
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault(), **kwargs)


async def _rerender(callback: types.CallbackQuery):
    await callback.message.edit_text(
        await render_miniapp_settings_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_settings_keyboard(),
    )


@router.callback_query(F.data == "admin_miniapp_settings")
async def open_miniapp_settings(callback: types.CallbackQuery, state: FSMContext):
    # Defensive clear (тот же приём, что `settings_edit_start`): заход на экран не должен
    # оставлять зависшую FSM правки ручки оформления (второй шов) с прошлого визита.
    await state.clear()
    await callback.message.edit_text(
        await render_miniapp_settings_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_settings_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "miniapp_toggle_enabled")
async def toggle_miniapp_enabled(callback: types.CallbackQuery):
    current = await get_setting_typed("miniapp_enabled")
    new_val = "off" if current == "on" else "on"
    await set_setting_by_admin(callback.from_user.id, "miniapp_enabled", new_val)
    toast = "Приложение: " + ("включено" if new_val == "on" else "выключено")
    # T-19-52: kept in sync with the toggle immediately, not only at next restart. Fail-soft —
    # an unreachable Telegram must not break this screen, only delay the chat menu button.
    try:
        await sync_chat_menu_button(callback.bot)
    except Exception:
        logger.warning("sync_chat_menu_button failed after toggle", exc_info=True)
        toast += " — кнопка меню обновится при следующем запуске"
    await callback.answer(toast)
    await _rerender(callback)


@router.callback_query(F.data == "miniapp_toggle_staff_only")
async def toggle_miniapp_staff_only(callback: types.CallbackQuery):
    current = await get_setting_typed("miniapp_staff_only")
    new_val = "off" if current == "on" else "on"
    await set_setting_by_admin(callback.from_user.id, "miniapp_staff_only", new_val)
    await callback.answer("Только менеджерам: " + ("да" if new_val == "on" else "нет"))
    await _rerender(callback)


@router.callback_query(F.data.startswith("miniapp_section:"))
async def toggle_miniapp_section(callback: types.CallbackQuery):
    suffix = callback.data.split(":", 1)[1]
    key = _SECTION_BY_SUFFIX.get(suffix)
    if key is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return

    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    await set_setting_by_admin(callback.from_user.id, key, new_val)
    label = SETTINGS_SCHEMA[key]["label"]
    toast = f"{label}: {'показываем' if new_val == 'on' else 'скрыт'}"
    await callback.answer(toast)
    await _rerender(callback)


_MOTION_CYCLE = {"auto": "micro", "micro": "off", "off": "auto"}


@router.callback_query(F.data == "miniapp_cycle_motion")
async def cycle_miniapp_motion(callback: types.CallbackQuery):
    """Quick 260915-4mw (ANIM-01..06): циклический тумблер auto -> micro -> off -> auto — три
    состояния, не два, поэтому не чекбокс (toggle_miniapp_section), а свой цикл, тем же
    приёмом (set_setting_by_admin + toast с человеческой подписью + _rerender)."""
    current = await get_setting_typed("miniapp_motion")
    new_val = _MOTION_CYCLE.get(current, "auto")
    await set_setting_by_admin(callback.from_user.id, "miniapp_motion", new_val)
    label = SETTINGS_SCHEMA["miniapp_motion"]["option_labels"].get(new_val, new_val)
    await callback.answer(f"Анимации приложения: {label}")
    await _rerender(callback)
