"""Списочные настройки (`type == "list"` в SETTINGS_SCHEMA): правка по одному пункту.

Откуда. Прод 17.08 и 20.08: `source_options` дважды схлопывался в один пункт — менеджер
присылал ОДИН новый источник, а экран правки заменял весь список целиком. Подсказка «пришлите
ВЕСЬ список» (quick 260820-rms) — это предупреждение, а не механика. Здесь механика:

- «➕ Добавить пункт» — одно сообщение = один новый пункт в конец списка;
- «🗑 Удалить пункт» — кнопка на каждый пункт, тап убирает ровно его;
- «✏️ Заменить список целиком» — прежний путь, теперь явный и не единственный (с тем же
  предупреждением о полной замене).

Экран `settings_edit:{list_key}` больше НЕ открывает FSM на ввод — случайное сообщение в
него уже не сотрёт список; ввод начинается только с кнопки.

Шов к общему `admin.router` (техника 13-02/13-03): импортируется ПОСЛЕДНЕЙ строкой
`handlers/admin_settings.py`, так что хендлеры встают сразу за хендлерами настроек при любом
порядке импорта модулей (тот же приём, что admin_gamification -> admin_game_tasks). Зависит
от admin_settings односторонне.

Per-city. Ни один списочный ключ сегодня не `per_city`, но путь тот же, что у
`settings_edit_city`/`settings_edit_value`: если в шапке выбран реальный город и ключ
per_city — пишем в составной ключ `per_city_key(key, code)`, отталкиваясь от текущего
эффективного списка (своё значение города или общее). Право на запись перепроверяется в
момент записи (09.3 WR-01), а не только при нажатии кнопки.
"""
import html as html_module
import logging
import zlib

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from settings_schema import SETTINGS_SCHEMA
from database.db import get_setting, set_setting, delete_setting
from cities import ALL_CITIES, admin_selected_city, is_per_city, per_city_key
from handlers.states import EditSetting
from handlers.settings_validation import is_command_like
from handlers.admin import router
from handlers.admin_settings import (
    SETTINGS_FIELDS,
    _per_city_visible_codes,
    _settings_edit_screen,
)

logger = logging.getLogger(__name__)

# Служебные слова регистрации (см. WR-02 в settings_edit_value): пункт с таким текстом
# недостижим для выбора — предупреждаем, но сохраняем.
_RESERVED_WORDS = {"отмена", "другое", "пропустить"}


def split_list_items(raw: str | None) -> list[str]:
    """Тот же разбор, что у `get_setting_typed` для `type == "list"`: строки + «;»."""
    if not raw:
        return []
    return [seg.strip() for line in raw.splitlines() for seg in line.split(";") if seg.strip()]


def _item_tag(item: str) -> str:
    """4 hex-символа от текста пункта — защита кнопки удаления от устаревшего индекса
    (список поменяли с другого экрана, а старая клавиатура осталась в чате)."""
    return f"{zlib.crc32(item.encode('utf-8')) & 0xFFFF:04x}"


def list_edit_rows(key: str) -> list[list[InlineKeyboardButton]]:
    """Ряды кнопок экрана `settings_edit:{key}` для списочного ключа. Вставляются
    `_settings_edit_screen` перед «❌ Отмена»."""
    return [
        [InlineKeyboardButton(text="➕ Добавить пункт", callback_data=f"settings_list_add:{key}")],
        [InlineKeyboardButton(text="🗑 Удалить пункт", callback_data=f"settings_list_del:{key}")],
        [InlineKeyboardButton(
            text="✏️ Заменить список целиком", callback_data=f"settings_list_replace:{key}",
        )],
    ]


async def _resolve_target(admin_id: int, key: str) -> tuple[str | None, str | None, str | None]:
    """-> (ключ записи, базовый ключ для per-city или None, текст ошибки или None).

    Шапка = реальный город и ключ per_city -> составной ключ (та же цепочка проверок, что в
    `settings_edit_city`: город виден этому админу, код из реестра). Иначе — сам `key`."""
    header = await admin_selected_city(admin_id)
    if not header or header == ALL_CITIES or not is_per_city(key):
        return key, None, None
    if header not in await _per_city_visible_codes(admin_id):
        return None, None, "Этот город правит суперадмин"
    composed = per_city_key(key, header)
    if composed is None:
        return None, None, "Неизвестный город"
    return composed, key, None


async def _current_items(target: str, base: str | None) -> list[str]:
    """Эффективный список: своё значение города, иначе общее (для per-city без своего
    значения «добавить пункт» стартует от общего списка, а не от пустого)."""
    raw = await get_setting(target)
    if not raw and base:
        raw = await get_setting(base)
    return split_list_items(raw)


async def _write_items(target: str, items: list[str]) -> None:
    if items:
        await set_setting(target, "\n".join(items))
    else:
        await delete_setting(target)


async def _rerender(callback: types.CallbackQuery, key: str) -> None:
    header = await admin_selected_city(callback.from_user.id)
    text, kb = await _settings_edit_screen(key, header)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


def _cancel_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"settings_edit:{key}")],
    ])


def _label(key: str) -> str:
    return html_module.escape(SETTINGS_SCHEMA.get(key, {}).get("label", key))


def _reserved_warning(key: str, items: list[str]) -> str:
    if not key.endswith("_options"):
        return ""
    clashes = sorted({i for i in items if i.lower() in _RESERVED_WORDS})
    if not clashes:
        return ""
    return (
        "\n\n⚠️ Внимание: варианты "
        + ", ".join(f"«{html_module.escape(c)}»" for c in clashes)
        + " совпадают со служебными словами бота и будут недоступны для выбора. "
        "Переименуйте их."
    )


# ── ➕ Добавить пункт ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings_list_add:"))
async def settings_list_add_start(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    target, base, error = await _resolve_target(callback.from_user.id, key)
    if error:
        await callback.answer(error, show_alert=True)
        return
    items = await _current_items(target, base)
    text = (
        f"➕ <b>{_label(key)}</b> — новый пункт\n\n"
        "Пришлите текст пункта одним сообщением — он встанет в конец списка.\n"
        "Один пункт за сообщение (например: <code>Узнал(-а) от друга</code>).\n\n"
        f"Сейчас в списке: {len(items)}."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_kb(key))
    await state.clear()
    await state.set_state(EditSetting.waiting_for_list_item)
    await state.update_data(list_key=target, list_base=key)
    await callback.answer()


@router.message(EditSetting.waiting_for_list_item)
async def settings_list_add_item(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("list_base") or ""
    value = (message.text or "").strip()

    if not value or value == "-":
        await message.answer(
            "Не понял пункт — пришлите его <b>текстом</b> одним сообщением "
            "(например: <code>Узнал(-а) от друга</code>).\n\n"
            "Выйти без изменений — «❌ Отмена».",
            parse_mode="HTML",
        )
        return
    if is_command_like(value):
        await message.answer(
            f"<code>{html_module.escape(value)}</code> — это команда, а не пункт списка, "
            "сохранять её не стал.\n\nПришлите текст пункта или нажмите «❌ Отмена».",
            parse_mode="HTML",
        )
        return
    if "\n" in value or ";" in value:
        await message.answer(
            "Здесь добавляется <b>один</b> пункт за сообщение — без переносов строки и «;».\n\n"
            "Нужно переписать весь список сразу — нажмите «❌ Отмена», затем "
            "«✏️ Заменить список целиком».",
            parse_mode="HTML",
        )
        return

    # 09.3 WR-01: право и контекст перепроверяются в момент записи — пока менеджер печатал,
    # шапка/привязка к городу могли измениться.
    target, base, error = await _resolve_target(message.from_user.id, key)
    if error or target != data.get("list_key"):
        await state.clear()
        await message.answer(error or "Город админки изменился — начните правку заново.")
        return

    items = await _current_items(target, base)
    if any(i.casefold() == value.casefold() for i in items):
        await message.answer(
            f"Такой пункт уже есть: «{html_module.escape(value)}». Пришлите другой "
            "или нажмите «❌ Отмена».",
            parse_mode="HTML",
        )
        return

    items.append(value)
    logger.info(f"admin {message.from_user.id} добавляет пункт в {target}")
    await _write_items(target, items)
    await state.clear()

    header = await admin_selected_city(message.from_user.id)
    text, kb = await _settings_edit_screen(key, header)
    note = f"✅ Добавил «{html_module.escape(value)}».{_reserved_warning(key, [value])}\n\n"
    await message.answer(note + text, parse_mode="HTML", reply_markup=kb)


# ── 🗑 Удалить пункт ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings_list_del:"))
async def settings_list_del_pick(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    target, base, error = await _resolve_target(callback.from_user.id, key)
    if error:
        await callback.answer(error, show_alert=True)
        return
    await state.clear()
    items = await _current_items(target, base)
    if not items:
        await callback.answer("Список пуст — удалять нечего", show_alert=True)
        return
    rows = [
        [InlineKeyboardButton(
            text=f"✖ {item[:40]}{'…' if len(item) > 40 else ''}",
            callback_data=f"settings_list_rm:{key}:{idx}:{_item_tag(item)}",
        )]
        for idx, item in enumerate(items)
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"settings_edit:{key}")])
    await callback.message.edit_text(
        f"🗑 <b>{_label(key)}</b> — какой пункт убрать?\n\n"
        "Нажмите на пункт — он пропадёт из списка. Вернуть его можно через «➕ Добавить пункт».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings_list_rm:"))
async def settings_list_rm_go(callback: types.CallbackQuery, state: FSMContext):
    try:
        _, key, idx_s, tag = callback.data.split(":", 3)
        idx = int(idx_s)
    except ValueError:
        await callback.answer("Не понял, какой пункт убрать", show_alert=True)
        return
    target, base, error = await _resolve_target(callback.from_user.id, key)
    if error:
        await callback.answer(error, show_alert=True)
        return
    await state.clear()
    items = await _current_items(target, base)
    if idx >= len(items) or _item_tag(items[idx]) != tag:
        await callback.answer("Список уже изменился — откройте его заново", show_alert=True)
        await _rerender(callback, key)
        return
    removed = items.pop(idx)
    logger.info(f"admin {callback.from_user.id} убирает пункт из {target}")
    await _write_items(target, items)
    await _rerender(callback, key)
    await callback.answer(f"🗑 Убрал «{removed[:60]}»", show_alert=True)


# ── ✏️ Заменить список целиком ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings_list_replace:"))
async def settings_list_replace_start(callback: types.CallbackQuery, state: FSMContext):
    """Прежний путь полной замены — теперь по явной кнопке. Сохранение идёт через
    `settings_edit_value` (тот же FSM, те же проверки прав/команд/«-»)."""
    key = callback.data.split(":", 1)[1]
    target, base, error = await _resolve_target(callback.from_user.id, key)
    if error:
        await callback.answer(error, show_alert=True)
        return
    prompts = {k: p for k, _, p in SETTINGS_FIELDS}
    prompt = prompts.get(key) or SETTINGS_SCHEMA.get(key, {}).get("prompt") or "Введите значение"
    items = await _current_items(target, base)
    listed = "\n".join(f"• {html_module.escape(i)}" for i in items) or "<i>пусто</i>"
    text = (
        f"✏️ <b>{_label(key)}</b> — заменить список целиком\n\n"
        f"Сейчас в списке ({len(items)}):\n{listed}\n\n"
        f"{html_module.escape(prompt)}\n\n"
        "<i>⚠️ Пришлите ВЕСЬ список целиком — новое сообщение заменит его полностью, "
        "а не добавит пункт. Каждый пункт с новой строки или через «;». "
        "Чтобы очистить список — отправьте «-».</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_kb(key))
    await state.clear()
    await state.set_state(EditSetting.waiting_for_value)
    if base:
        await state.update_data(setting_key=target, per_city_base=base)
    else:
        await state.update_data(setting_key=target)
    await callback.answer()
