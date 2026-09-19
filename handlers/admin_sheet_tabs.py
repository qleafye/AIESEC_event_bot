"""Quick 260919-mlu — шов admin_sections: операции переименования вкладок Google-таблицы.

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_sections.py` — `handlers/admin_settings.py` стоит
на потолке размера (`tests/test_module_size_convention_260816.py`), новый ветвящийся код
сюда не влезает.

Проблема (память проекта sheet-tab-rename-trap): смена ключа-имени вкладки в настройках
СЕГОДНЯ не переименовывает реальный лист — `services/sheets.py` на `WorksheetNotFound`
заводит НОВУЮ пустую вкладку, а старая с данными остаётся сиротой. Существующий гейт (квик
260815-3hw, `sheets_tab_confirm`/`sheets_tab_cancel` в `admin_settings.py`) предупреждает
только о ДРУГОЙ беде — «вкладка с НОВЫМ именем уже есть, её перезапишут» — и ничего не
знает о брошенной старой.

Task 3: `tab_change_screen` — развилка на 4 клетки таблицы «old_exists × new_exists» (см.
её докстринг); `admin_settings.py::settings_edit_value` зовёт её ДО существующего гейта
260815-3hw. `None` = развилки нет — вызывающий код сохраняет как раньше (byte-for-byte для
веток, которых новая развилка не касается: ключ не про вкладки, старой вкладки нет,
проверка не удалась).

FSM (Task 3): те же `EditSetting.waiting_for_tab_confirm` данные, что и у 260815-3hw
(`pending_tab_key`/`pending_tab_value`), плюс новый `pending_tab_old` — второе состояние не
заводим.

Task 4: массовые кнопки «🤖 Добавить префикс ко всем вкладкам бота» / «🧹 Убрать префикс» на
экране «📄 Вкладки таблицы» — `settings_ops.current_tab_titles`/`plan_prefix_renames`
считают план, `sheet_tabs_prefix_add`/`_del` показывают экран подтверждения (БЕЗ единого
вызова записи), `sheet_tabs_prefix_add_go`/`_del_go` исполняют его. План не хранится в FSM
между экраном подтверждения и исполнением — оба зовут одни и те же чистые функции над
одним и тем же состоянием настроек, пересчёт идемпотентен и дешевле, чем тащить dataclass
через сериализацию FSM-хранилища.
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database.db import get_setting, update_city
from cities import reload_cities
from handlers.admin import router
from handlers.states import EditSetting
from settings_audit import delete_setting_by_admin, set_setting_by_admin
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from settings_ops import (
    SHEET_TAB_WRITE_MODE, after_tab_setting_saved, bot_tab_prefix, current_tab_titles,
    plan_prefix_renames,
)
from services.sheets import list_worksheet_titles, rename_worksheet, tab_row_count

logger = logging.getLogger(__name__)

# Тот же ключ, что services/sheets.py::_PINNED_MAIN_TAB_SETTING_KEY — легаси-пин основной
# вкладки, третья ступень резолва main_sheet_tab. Не импортируем константу оттуда (модуль
# services.sheets — sync-ядро с приватными sqlite3-хелперами, а не async-API уровня этого
# шва); литерал дублируется так же, как в settings_ops.current_tab_titles.
_PINNED_MAIN_TAB_SETTING_KEY = "sheets_main_tab_pinned_title"


async def current_key_tab_name(key: str) -> str:
    """Что этот ключ называет СЕЙЧАС, до текущей правки. Для `main_sheet_tab` — та же
    4-ступенчатая цепочка резолва, что `services.sheets._get_sheet`/
    `settings_ops.current_tab_titles` (bot_settings -> .env -> легаси-пин): без неё
    переименование основной вкладки, чьё имя пришло со ступени 2/3, решило бы, что старой
    вкладки «нет», и не предложило бы её переименовать. Для остальных ключей — просто
    текущая настройка или дефолт реестра."""
    value = await get_setting_typed(key)
    if value:
        return value
    if key == "main_sheet_tab":
        env_value = (config.GOOGLE_SHEET_TAB or "").strip().strip('"').strip("'").strip()
        if env_value:
            return env_value
        return (await get_setting(_PINNED_MAIN_TAB_SETTING_KEY)) or ""
    return SETTINGS_SCHEMA.get(key, {}).get("default") or ""


def _tab_rename_keyboard(*, has_rename: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_rename:
        buttons.append([InlineKeyboardButton(
            text="✏️ Переименовать вкладку", callback_data="sheet_tab_rename_go",
        )])
        buttons.append([InlineKeyboardButton(
            text="📄 Создать новую пустую", callback_data="sheet_tab_newtab_go",
        )])
    else:
        buttons.append([InlineKeyboardButton(
            text="📄 Писать в существующую", callback_data="sheet_tab_reuse_go",
        )])
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data="sheets_tab_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def tab_change_screen(
    key: str, old_value: str, new_value: str,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Развилка при смене ОДНОГО ключа-имени вкладки. `None` = развилки нет, вызывающий код
    (`settings_edit_value`) ведёт себя как раньше (тихое сохранение либо старый гейт
    260815-3hw, который смотрит только на НОВОЕ имя):

    | старая вкладка | новая вкладка | Экран |
    |---|---|---|
    | нет | (любая) | `None` — этой развилки нет, решает старый гейт/тихое сохранение |
    | да | нет | «Переименовать» + «Создать новую пустую» + «Отмена» |
    | да | да | «Писать в существующую» (без переименования — Google не даёт двух листов с |
    |    |    | одним именем) + «Отмена» |

    `old_exists`/`new_exists` берутся из `tab_row_count` — `None` (проверка не удалась)
    трактуется как «нет», текущее поведение и предупреждение о непроверенной вкладке
    сохраняются: и старая, и новая ветки в этом случае просто не активируются здесь, решение
    остаётся за вызывающим кодом (byte-for-byte старое поведение)."""
    if key not in SHEET_TAB_WRITE_MODE or not new_value or new_value == "-":
        return None
    if not old_value or old_value == new_value:
        return None

    old_probe = await tab_row_count(old_value)
    if not old_probe or not old_probe[0]:
        return None  # старой вкладки нет (или проверка не удалась) — гейтить нечего
    old_rows = old_probe[1]

    new_probe = await tab_row_count(new_value)
    new_exists = bool(new_probe and new_probe[0])

    label_h = html_module.escape(SETTINGS_SCHEMA.get(key, {}).get("label", key))
    old_h = html_module.escape(old_value)
    new_h = html_module.escape(new_value)

    if new_exists:
        new_rows = new_probe[1]
        text = (
            f"⚠️ <b>{label_h}</b>\n\n"
            f"Сейчас бот пишет в «{old_h}» ({old_rows} строк). Вкладка «{new_h}» уже есть в "
            f"таблице ({new_rows} строк).\n\n"
            f"Переименовать «{old_h}» в «{new_h}» нельзя — у Google два листа не могут "
            f"называться одинаково. Можно писать в «{new_h}» вместо «{old_h}»."
        )
        return text, _tab_rename_keyboard(has_rename=False)

    text = (
        f"⚠️ <b>{label_h}</b>\n\n"
        f"Сейчас бот пишет в «{old_h}» ({old_rows} строк). Если просто сохранить новое имя "
        f"— бот заведёт пустую «{new_h}», а все {old_rows} строк останутся в «{old_h}» и "
        "обновляться перестанут.\n\n"
        f"Переименовать «{old_h}» в «{new_h}» — данные останутся на месте."
    )
    return text, _tab_rename_keyboard(has_rename=True)


@router.callback_query(F.data == "sheet_tab_rename_go")
async def sheet_tab_rename_go(callback: types.CallbackQuery, state: FSMContext):
    """Переименовать существующий лист (`rename_worksheet`) вместо того, чтобы бросить его
    сиротой. На `"ok"` ключ сохраняется ВСЕГДА (в том числе для `main_sheet_tab`, чьё старое
    имя могло прийти со ступени `.env`/легаси-пина — резолв в этом случае навсегда
    перекрывается ступенью `bot_settings`, ловушка описана в докстринге плана квика)."""
    data = await state.get_data()
    key = data.get("pending_tab_key")
    new_value = data.get("pending_tab_value")
    old_value = data.get("pending_tab_old")
    await state.clear()
    from handlers.admin_sections import settings_return_screen  # ленивый шов

    if not key or new_value is None or not old_value:
        text, kb = await settings_return_screen(callback.from_user.id, group_token="sheets")
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer("Данные правки устарели — начните заново", show_alert=True)
        return

    result = await rename_worksheet(old_value, new_value)
    text, kb = await settings_return_screen(callback.from_user.id, group_token="sheets")
    if result == "ok":
        await set_setting_by_admin(callback.from_user.id, key, new_value)
        await after_tab_setting_saved(key)
        old_h, new_h = html_module.escape(old_value), html_module.escape(new_value)
        await callback.message.edit_text(
            text + f"\n\n✅ Вкладка «{old_h}» переименована в «{new_h}», данные на месте.",
            parse_mode="HTML", reply_markup=kb,
        )
        await callback.answer("✅ Переименовано")
        return

    reasons = {
        "duplicate": f"вкладка «{html_module.escape(new_value)}» уже есть в таблице",
        "not_found": f"вкладка «{html_module.escape(old_value)}» не найдена в таблице",
    }
    reason = reasons.get(result, "не удалось обратиться к Google-таблице")
    await callback.message.edit_text(
        text + f"\n\n❌ Переименовать не получилось: {reason}. Настройка не изменена.",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer("Не удалось переименовать", show_alert=True)


@router.callback_query(F.data == "sheet_tab_reuse_go")
async def sheet_tab_reuse_go(callback: types.CallbackQuery, state: FSMContext):
    """Писать в уже существующую вкладку под новым именем — то же сохранение, что у
    сегодняшнего `sheets_tab_confirm` (гейт 260815-3hw), просто из развилки задачи 3 (клетка
    «старая есть, новая тоже есть» — переименовать нельзя, Google не даёт двух листов с
    одним именем)."""
    data = await state.get_data()
    key = data.get("pending_tab_key")
    new_value = data.get("pending_tab_value")
    await state.clear()
    if key and new_value is not None:
        await set_setting_by_admin(callback.from_user.id, key, new_value)
        if key in SHEET_TAB_WRITE_MODE:
            await after_tab_setting_saved(key)
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(callback.from_user.id, group_token="sheets")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Сохранено")


@router.callback_query(F.data == "sheet_tab_newtab_go")
async def sheet_tab_newtab_go(callback: types.CallbackQuery, state: FSMContext):
    """Завести новую пустую вкладку — сохраняет ключ, старый лист остаётся в таблице
    нетронутым (бот просто перестаёт в него писать)."""
    data = await state.get_data()
    key = data.get("pending_tab_key")
    new_value = data.get("pending_tab_value")
    old_value = data.get("pending_tab_old")
    await state.clear()
    warning = ""
    if key and new_value is not None:
        await set_setting_by_admin(callback.from_user.id, key, new_value)
        if key in SHEET_TAB_WRITE_MODE:
            await after_tab_setting_saved(key)
        if old_value:
            old_h = html_module.escape(old_value)
            warning = (
                f"\n\n📄 Старая вкладка «{old_h}» останется в таблице, бот в неё больше не "
                "пишет."
            )
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(callback.from_user.id, group_token="sheets")
    await callback.message.edit_text(text + warning, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Сохранено")


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 4: массовые «Добавить/Убрать префикс» — экран «📄 Вкладки таблицы»
# ═══════════════════════════════════════════════════════════════════════════════════════════

_PREFIX_PREVIEW_LIMIT = 20


def sheet_tabs_group_extra_buttons() -> list[list[InlineKeyboardButton]]:
    """Две кнопки на экране «📄 Вкладки таблицы» (образец —
    `handlers/admin_consent.py::consent_group_extra_buttons`)."""
    return [
        [InlineKeyboardButton(
            text="🤖 Добавить префикс ко всем вкладкам бота", callback_data="sheet_tabs_prefix_add",
        )],
        [InlineKeyboardButton(text="🧹 Убрать префикс", callback_data="sheet_tabs_prefix_del")],
    ]


def _skip_reason_breakdown(skipped: list[tuple]) -> str:
    """«нет в таблице (2), имя занято (1)» — без счётчика-обёртки, вызывающий текст сам решает
    формулировку вокруг (экран подтверждения и итоговый отчёт формулируют по-разному)."""
    if not skipped:
        return ""
    counts: dict[str, int] = {}
    for _target, _old, reason in skipped:
        counts[reason] = counts.get(reason, 0) + 1
    return ", ".join(f"{reason} ({n})" for reason, n in counts.items())


async def _prefix_plan_screen(*, add: bool) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает план (`current_tab_titles` + `list_worksheet_titles` + `bot_tab_prefix` +
    `plan_prefix_renames`) и рисует экран подтверждения — БЕЗ единого вызова записи."""
    action_label = (
        "🤖 Добавить префикс ко всем вкладкам бота" if add
        else "🧹 Убрать префикс со всех вкладок бота"
    )
    cancel_row = [InlineKeyboardButton(text="← Отмена", callback_data="sheets_tab_cancel")]

    titles = await list_worksheet_titles()
    if titles is None:
        text = (
            f"⚠️ <b>{action_label}</b>\n\n"
            "❌ Не удалось обратиться к Google-таблице — переименовывать вслепую не буду. "
            "Попробуйте позже."
        )
        return text, InlineKeyboardMarkup(inline_keyboard=[cancel_row])

    targets = await current_tab_titles()
    prefix = await bot_tab_prefix()
    renames, skipped = plan_prefix_renames(targets, titles, prefix, add=add)

    lines = [f"⚠️ <b>{action_label}</b>", ""]
    if not renames:
        if not prefix:
            lines.append("Префикс не задан — задайте его в «🤖 Префикс вкладок бота» выше.")
        elif not skipped:
            lines.append("У бота пока нет ни одной именованной вкладки — нечего трогать.")
        else:
            lines.append("Изменений нет — все вкладки уже в нужном состоянии.")
    else:
        preview = renames[:_PREFIX_PREVIEW_LIMIT]
        for _target, old, new in preview:
            lines.append(f"«{html_module.escape(old)}» → «{html_module.escape(new)}»")
        if len(renames) > _PREFIX_PREVIEW_LIMIT:
            lines.append(f"…и ещё {len(renames) - _PREFIX_PREVIEW_LIMIT}")

    if skipped:
        lines.append(f"\n{len(skipped)} вкладок не трогаю: {_skip_reason_breakdown(skipped)}.")

    lines.append("\n🎯 Отобранные не трогаю — её заполняете вы.")
    lines.append(
        "\n⚠️ Формулы и IMPORTRANGE в ДРУГИХ таблицах ссылаются на имя вкладки строкой — "
        "после переименования они сломаются и их придётся поправить вручную. Внутри этой "
        "таблицы Google чинит формулы сам."
    )
    lines.append(
        "\nОперация дёргает Google по одному вызову на лист — жать кнопку лучше в тихий "
        "час, не в разгар регистрации."
    )
    text = "\n".join(lines)

    buttons = []
    if renames:
        go_cb = "sheet_tabs_prefix_add_go" if add else "sheet_tabs_prefix_del_go"
        buttons.append([InlineKeyboardButton(
            text=f"✅ Да, переименовать {len(renames)} вкладок", callback_data=go_cb,
        )])
    buttons.append(cancel_row)
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "sheet_tabs_prefix_add")
async def sheet_tabs_prefix_add(callback: types.CallbackQuery):
    text, kb = await _prefix_plan_screen(add=True)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "sheet_tabs_prefix_del")
async def sheet_tabs_prefix_del(callback: types.CallbackQuery):
    text, kb = await _prefix_plan_screen(add=False)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _apply_city_rename_result(admin_id: int, target, new_title: str) -> None:
    """`origin == "city" and kind == "main"` -> база города переезжает на новое имя листа
    (зеркало `handlers/admin_cities.py::city_edit_tab_step`); `kind != "main"` -> лист трека
    уже переименован, настройку (`city_tab_suffix__*`) трогать не нужно — имя трека всегда
    вычисляется как база + приписка, и после смены БАЗЫ (эта функция, вызванная для
    `kind == "main"`) оно автоматически становится верным."""
    if target.kind == "main":
        await update_city(target.city_code, tab_base=new_title)
        await delete_setting_by_admin(admin_id, f"city_tab__{target.city_code}")
        await reload_cities()


def _ordered_for_execution(renames: list[tuple]) -> list[tuple]:
    """Порядок исполнения: сначала цели `origin == "key"` (детерминированно — в порядке
    `current_tab_titles`), потом городские, СНАЧАЛА треки, ПОТОМ главный лист города — база
    города пишется последней, иначе промежуточное состояние (переименован трек, база ещё
    старая) на секунду разъедется с реальными листами."""
    key_renames = [r for r in renames if r[0].origin == "key"]
    city_codes_seen: list[str] = []
    by_city: dict[str, list] = {}
    for r in renames:
        if r[0].origin != "city":
            continue
        code = r[0].city_code
        if code not in by_city:
            by_city[code] = []
            city_codes_seen.append(code)
        by_city[code].append(r)
    ordered_city: list = []
    for code in city_codes_seen:
        group = by_city[code]
        ordered_city.extend(r for r in group if r[0].kind != "main")
        ordered_city.extend(r for r in group if r[0].kind == "main")
    return key_renames + ordered_city


async def _run_prefix_plan(callback: types.CallbackQuery, *, add: bool) -> None:
    """Исполнение плана — пересчитан заново (не хранится в FSM, см. докстринг модуля).
    Ошибка на одной вкладке НЕ прерывает остальные (fail-soft), но попадает в отчёт и лог."""
    admin_id = callback.from_user.id
    titles = await list_worksheet_titles()
    if titles is None:
        await callback.answer("❌ Таблица недоступна — ничего не сделано", show_alert=True)
        return
    targets = await current_tab_titles()
    prefix = await bot_tab_prefix()
    renames, skipped = plan_prefix_renames(targets, titles, prefix, add=add)
    ordered = _ordered_for_execution(renames)

    done = 0
    failed: list[str] = []
    total = len(ordered)
    for i, (target, old, new) in enumerate(ordered, start=1):
        result = await rename_worksheet(old, new)
        if result == "ok":
            if target.origin == "key":
                await set_setting_by_admin(admin_id, target.key, new)
                await after_tab_setting_saved(target.key)
            else:
                await _apply_city_rename_result(admin_id, target, new)
            done += 1
        else:
            failed.append(f"«{old}» → «{new}» ({result})")
            logger.error(f"sheet_tabs_prefix_go: переименование {old!r} -> {new!r}: {result}")
        if total > 10 and i % 10 == 0:
            try:
                await callback.message.edit_text(f"⏳ Переименовываю… ({i}/{total})")
            except Exception:
                pass

    report = [f"✅ Переименовано {done}."]
    skip_line = f"Пропущено {len(skipped)}"
    if skipped:
        skip_line += f" ({_skip_reason_breakdown(skipped)})"
    report.append(skip_line + ".")
    if failed:
        report.append(f"❌ Не удалось {len(failed)}: " + "; ".join(failed))

    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(admin_id, group_token="sheets")
    await callback.message.edit_text(
        "\n".join(report) + "\n\n" + text, parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "sheet_tabs_prefix_add_go")
async def sheet_tabs_prefix_add_go(callback: types.CallbackQuery):
    await _run_prefix_plan(callback, add=True)


@router.callback_query(F.data == "sheet_tabs_prefix_del_go")
async def sheet_tabs_prefix_del_go(callback: types.CallbackQuery):
    await _run_prefix_plan(callback, add=False)
