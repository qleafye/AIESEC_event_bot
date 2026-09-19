"""Quick 260919-mlu (Task 3) — шов admin_sections: развилка при смене одного ключа-имени
вкладки Google-таблицы.

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_sections.py` — `handlers/admin_settings.py` стоит
на потолке размера (`tests/test_module_size_convention_260816.py`), новый ветвящийся код
сюда не влезает.

Проблема (память проекта sheet-tab-rename-trap): смена ключа-имени вкладки в настройках
СЕГОДНЯ не переименовывает реальный лист — `services/sheets.py` на `WorksheetNotFound`
заводит НОВУЮ пустую вкладку, а старая с данными остаётся сиротой. Существующий гейт (квик
260815-3hw, `sheets_tab_confirm`/`sheets_tab_cancel` в `admin_settings.py`) предупреждает
только о ДРУГОЙ беде — «вкладка с НОВЫМ именем уже есть, её перезапишут» — и ничего не
знает о брошенной старой. Этот шов добавляет вторую половину: если у ключа была валидная
СТАРАЯ вкладка, менеджеру предлагается её ПЕРЕИМЕНОВАТЬ (данные остаются), а не молча
потерять.

`tab_change_screen` — развилка на 4 клетки таблицы «old_exists × new_exists» (см. её
докстринг); `admin_settings.py::settings_edit_value` зовёт её ДО существующего гейта
260815-3hw. `None` = развилки нет — вызывающий код сохраняет как раньше (byte-for-byte для
веток, которых новая развилка не касается: ключ не про вкладки, старой вкладки нет,
проверка не удалась).

FSM: те же `EditSetting.waiting_for_tab_confirm` данные, что и у 260815-3hw
(`pending_tab_key`/`pending_tab_value`), плюс новый `pending_tab_old` — второе состояние не
заводим.
"""
import html as html_module

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database.db import get_setting
from handlers.admin import router
from handlers.states import EditSetting
from settings_audit import set_setting_by_admin
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from settings_ops import SHEET_TAB_WRITE_MODE, after_tab_setting_saved
from services.sheets import rename_worksheet, tab_row_count

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
