"""Квик 260927: экран «🏆 Рейтинг чата» (раздел «🔧 Управление»).

Менеджер города выбирает, как считать рейтинг чата делегатов — «📈 По формуле активности»
(общая калиброванная формула, веса в группе «💬 Чат делегатов») или «🪙 По правилам города»
(пресет СПб «коины»: комментарии под постами команды, приглашённые, упоминания в соцсетях, дни
форума). Суммы правил вводятся числом, задания для «упоминаний» отмечаются галочками по
названиям — коды ключей, городов и режимов на экране не появляются (бот для людей).

Экран только настраивает расчёт: сам рейтинг считает дашборд (chat_score.score_city_rules),
бот никому ничего не начисляет и не пишет.

Город экрана — шапка админки (`cities.admin_selected_city`): реальный город -> пишутся
per-city ключи (только через `cities.per_city_key`); модуль городов выключен или выбраны «все
города» -> общие ключи. Город зашит в каждую кнопку и перепроверяется на каждом тапе
(стейл-кнопка другого города ничего не пишет). Регистрируется на общий `admin.router`
хвостовым импортом `handlers/admin.py`.
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import shared.chat_score as chat_score
from cities import (
    ALL_CITIES,
    admin_selected_city,
    city_codes,
    city_label,
    city_scope,
    per_city_key,
)
from database.db import get_setting, get_task, list_all_tasks, task_title
from handlers.admin import router
from handlers.admin_settings import _per_city_visible_codes
from domain.settings.validation import is_command_like, validate_setting_value
from handlers.states import ChatRatingEdit
from services.settings.audit import delete_setting_by_admin, set_setting_by_admin
from domain.settings.schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test): every `@router.*` decorator below MUST fit on ONE line.

_MODE_LABELS = SETTINGS_SCHEMA[chat_score.MODE_KEY]["option_labels"]
_GLOBAL = "-"  # токен «общие значения» в callback_data (модуль городов выключен / все города)

# Порядок кнопок правил и короткие подписи (полная подпись и подсказка — в реестре).
_RULE_BUTTONS = (
    ("comment_points", "🪙 Комментарий под постом"),
    ("valuable_points", "🪙 Ценный комментарий"),
    ("valuable_min_chars", "📏 Ценный — от"),
    ("post_min_chars", "📏 Пост команды — от"),
    ("referral_points", "🪙 Привёл друга"),
    ("social_points", "🪙 Упоминание в соцсетях"),
    ("social_max", "🔢 Упоминаний не больше"),
    ("checkin_points", "🪙 День форума с отметкой входа"),
    ("checkin_max", "🔢 Дней форума не больше"),
)
_CURRENCY = "currency"
_EDITABLE = {name: chat_score.RULE_SETTING_KEYS[name] for name, _ in _RULE_BUTTONS}
_EDITABLE[_CURRENCY] = chat_score.CURRENCY_KEY
_TASKS_LIMIT = 80  # кнопок заданий на экране (у Telegram потолок 100 кнопок на сообщение)


def _num(value) -> str:
    return f"{value:g}".replace(".", ",")


async def _screen_city(admin_id: int) -> tuple[str | None, str | None]:
    """(код города или None для общих ключей, шапка экрана или None без модуля городов)."""
    header = await admin_selected_city(admin_id)
    if header is None:
        return None, None
    if header == ALL_CITIES:
        return None, await city_label(ALL_CITIES)
    return header, await city_label(header)


def _key(base: str, code: str | None) -> str | None:
    return per_city_key(base, code) if code else base


async def _raw(base: str, code: str | None) -> tuple[str | None, bool]:
    """Сырое значение для города (своё -> общее) и признак «у города своё»."""
    if code:
        own = await get_setting(per_city_key(base, code))
        if own is not None:
            return own, True
    return await get_setting(base), False


async def _effective(code: str | None) -> tuple[str, dict, str, list[int]]:
    """(режим, правила, название баллов, id заданий для упоминаний) для города экрана."""
    raw_mode, _ = await _raw(chat_score.MODE_KEY, code)
    mode = raw_mode if raw_mode in chat_score.MODES else chat_score.MODES[0]
    raw = {}
    for key in chat_score.RULE_SETTING_KEYS.values():
        raw[key], _ = await _raw(key, code)
    rules = chat_score.rules_from_settings(raw)
    currency, _ = await _raw(chat_score.CURRENCY_KEY, code)
    tasks_raw, _ = await _raw(chat_score.SOCIAL_TASKS_KEY, code)
    return mode, rules, (currency or chat_score.DEFAULT_CURRENCY), _task_ids(tasks_raw)


def _task_ids(raw: str | None) -> list[int]:
    """id заданий из значения настройки; «0» — маркер «у города пусто» (см. chrate_task_toggle)."""
    ids = []
    for piece in (raw or "").replace(";", "\n").splitlines():
        piece = piece.strip()
        if piece.isdigit() and int(piece) > 0 and int(piece) not in ids:
            ids.append(int(piece))
    return ids


def _rule_value_text(name: str, rules: dict, currency: str) -> str:
    value = rules[name]
    if name.endswith("_chars"):
        return "любое сообщение" if value == 0 else f"{_num(value)} симв."
    if name.endswith("_max"):
        return _num(value)
    return f"{_num(value)} {currency}"


async def render_chat_rating_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    code, header = await _screen_city(admin_id)
    mode, rules, currency, task_ids = await _effective(code)
    token = code or _GLOBAL

    title = "🏆 <b>Рейтинг чата</b>"
    if header:
        title = f"🏆 <b>Рейтинг чата — {html_module.escape(header)}</b>"
    lines = [title, "", f"Как считаем: <b>{html_module.escape(_MODE_LABELS[mode])}</b>"]
    if mode == "formula":
        lines.append(
            "Общий балл за сообщения, ответы, реакции и регулярность. Веса формулы одни на все "
            "города — кнопка «⚖️ Веса формулы»."
        )
    else:
        lines.append("")
        lines.extend(
            f"• {html_module.escape(line)}"
            for line in chat_score.describe_rules(rules, currency)
        )
        lines.append(
            f"• Заданий для «упоминаний» выбрано: {len(task_ids)}" if task_ids
            else "• Задания для «упоминаний» не выбраны — упоминания пока не считаются."
        )
    lines.append("")
    if code:
        lines.append("<i>Режим и суммы — только для этого города; чего у города нет своего, "
                     "берётся из общих значений.</i>")
    elif header:
        lines.append("<i>Это общие значения — их берут города без своих.</i>")
    lines.append(
        "<i>Рейтинг виден на веб-дашборде, страница «💬 Чат». Бот никому ничего не начисляет; "
        "в чат пишет, только если включить «📣 Публиковать рейтинг в чат».</i>"
    )

    rows = []
    for option in chat_score.MODES:
        mark = "✅ " if option == mode else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{_MODE_LABELS[option]}", callback_data=f"chrate:mode:{token}:{option}",
        )])
    if mode == "rules":
        for name, label in _RULE_BUTTONS:
            rows.append([InlineKeyboardButton(
                text=f"{label}: {_rule_value_text(name, rules, currency)}",
                callback_data=f"chrate:edit:{token}:{name}",
            )])
        rows.append([InlineKeyboardButton(
            text=f"🏷 Название баллов: {currency}", callback_data=f"chrate:edit:{token}:{_CURRENCY}",
        )])
        rows.append([InlineKeyboardButton(
            text="📋 Задания для «упоминаний»", callback_data=f"chrate:tasks:{token}",
        )])
    # Еженедельный пост рейтинга в чат города (handlers/admin_chat_rating_post.py) — ленивый
    # импорт: тот модуль сам импортирует этот.
    from handlers.admin_chat_rating_post import toggle_row
    rows.extend(await toggle_row(code, header))
    rows.append([InlineKeyboardButton(text="📥 Загрузить историю чата", callback_data="chimp:open")])
    rows.append([InlineKeyboardButton(text="⚖️ Веса формулы", callback_data="settings_group:chat")])
    if header:
        rows.append([InlineKeyboardButton(text="🏙 Сменить город", callback_data="admin_city_switch:manage")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_sec:manage")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _checked_city(callback: types.CallbackQuery, token: str) -> tuple[bool, str | None]:
    """Город из кнопки против текущей шапки: неизвестный код, сменившийся город или чужой
    город — ничего не пишем, объясняем и (если экран жив) перерисовываем."""
    code = None if token == _GLOBAL else token
    if code is not None and code not in city_codes():
        await callback.answer("Кнопка устарела — откройте «🏆 Рейтинг чата» заново.", show_alert=True)
        return False, None
    current, _header = await _screen_city(callback.from_user.id)
    if current != code:
        await callback.answer(
            "Город админки сменился — экран обновлён, нажмите ещё раз.", show_alert=True,
        )
        text, kb = await render_chat_rating_screen(callback.from_user.id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return False, None
    if code is not None and code not in await _per_city_visible_codes(callback.from_user.id):
        await callback.answer("Этот город правит суперадмин.", show_alert=True)
        return False, None
    return True, code


@router.callback_query(F.data == "admin_chat_rating")
async def chat_rating_open(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_chat_rating_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "chrate:back")
async def chrate_back(callback: types.CallbackQuery, state: FSMContext):
    await chat_rating_open(callback, state)


@router.callback_query(F.data.startswith("chrate:mode:"))
async def chrate_mode(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[3] not in chat_score.MODES:
        await callback.answer("Кнопка устарела — откройте экран заново.", show_alert=True)
        return
    ok, code = await _checked_city(callback, parts[2])
    if not ok:
        return
    key = _key(chat_score.MODE_KEY, code)
    await set_setting_by_admin(callback.from_user.id, key, parts[3])
    text, kb = await render_chat_rating_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(f"Теперь: {_MODE_LABELS[parts[3]]}")


async def _edit_screen(name: str, code: str | None, token: str) -> tuple[str, InlineKeyboardMarkup]:
    base = _EDITABLE[name]
    entry = SETTINGS_SCHEMA[base]
    value, own = await _raw(base, code)
    if value is None:
        value = entry["default"]
    if name == _CURRENCY:
        current = html_module.escape(str(value or chat_score.DEFAULT_CURRENCY))
    else:
        rules = chat_score.rules_from_settings({base: value})
        current = html_module.escape(_num(rules[name]))
    where = ""
    if code:
        where = " — своё у города" if own else " — как в остальных городах"
    lines = [
        f"✏️ <b>{html_module.escape(entry['label'])}</b>",
        "",
        f"Сейчас: <b>{current}</b>{where}",
        "",
        html_module.escape(entry["prompt"]),
        "",
        "<i>Пришлите новое значение сообщением. «-» — вернуть значение по умолчанию.</i>"
        if not code else
        "<i>Пришлите новое значение сообщением.</i>",
    ]
    rows = []
    if code and own:
        rows.append([InlineKeyboardButton(
            text="↩️ Как в остальных городах", callback_data=f"chrate:rst:{token}:{name}",
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="chrate:back")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("chrate:edit:"))
async def chrate_edit(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[3] not in _EDITABLE:
        await callback.answer("Кнопка устарела — откройте экран заново.", show_alert=True)
        return
    ok, code = await _checked_city(callback, parts[2])
    if not ok:
        return
    name = parts[3]
    text, kb = await _edit_screen(name, code, parts[2])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(ChatRatingEdit.waiting_for_value)
    await state.set_data({"chrate_key": _key(_EDITABLE[name], code), "chrate_city": code})
    await callback.answer()


@router.callback_query(F.data.startswith("chrate:rst:"))
async def chrate_reset(callback: types.CallbackQuery, state: FSMContext):
    """«↩️ Как в остальных городах»: удаляется только своё значение города — общее и значения
    других городов не трогаются, поэтому без подтверждения (сказано в ответе на кнопку)."""
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[3] not in _EDITABLE or parts[2] == _GLOBAL:
        await callback.answer("Кнопка устарела — откройте экран заново.", show_alert=True)
        return
    ok, code = await _checked_city(callback, parts[2])
    if not ok:
        return
    await delete_setting_by_admin(callback.from_user.id, per_city_key(_EDITABLE[parts[3]], code))
    await state.clear()
    text, kb = await render_chat_rating_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(
        "Своё значение города удалено — теперь как в остальных городах. Общее значение не менялось."
    )


@router.message(StateFilter(ChatRatingEdit.waiting_for_value))
async def chrate_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key, code = data.get("chrate_key"), data.get("chrate_city")
    value = (message.text or "").strip()
    if not key:
        await state.clear()
        return
    if not value or is_command_like(value):
        await message.answer(
            "Не понял значение — пришлите его текстом одним сообщением, например "
            "<code>10</code> (дробное — через запятую: <code>2,5</code>).\n\n"
            "Передумали — «❌ Отмена» под сообщением выше.",
            parse_mode="HTML",
        )
        return
    # TOCTOU: пока менеджер набирал, город шапки мог смениться — пишем только в тот город,
    # который был на экране.
    current, _header = await _screen_city(message.from_user.id)
    if current != code or (code and code not in await _per_city_visible_codes(message.from_user.id)):
        await state.clear()
        await message.answer("Город админки изменился — начните правку заново.")
        return
    if value == "-":
        await delete_setting_by_admin(message.from_user.id, key)
    else:
        value, error = validate_setting_value(key, value)
        if error:
            await message.answer(error, parse_mode="HTML")
            return
        await set_setting_by_admin(message.from_user.id, key, value)
    logger.info(f"admin {message.from_user.id} правит настройку {key}")
    await state.clear()
    text, kb = await render_chat_rating_screen(message.from_user.id)
    await message.answer("✅ Сохранено\n\n" + text, parse_mode="HTML", reply_markup=kb)


async def _tasks_for(code: str | None) -> list[dict]:
    """Задания игры, доступные городу экрана: свои + «для всех городов» (event_city NULL)."""
    return await list_all_tasks(city_scope=city_scope(code), include_null=True)


async def _render_tasks(code: str | None, token: str) -> tuple[str, InlineKeyboardMarkup]:
    tasks = await _tasks_for(code)
    _mode, _rules, _currency, selected = await _effective(code)
    lines = [
        "📋 <b>Задания для «упоминаний в соцсетях»</b>",
        "",
        "Отметьте задания игры, одобренная сдача которых считается упоминанием в соцсетях. "
        "Нажатие ставит или снимает ✅.",
    ]
    if not tasks:
        lines += ["", "<i>Заданий пока нет — заведите их в разделе «🎮 Геймификация».</i>"]
    shown = tasks[:_TASKS_LIMIT]
    if len(tasks) > len(shown):
        lines += ["", f"<i>Показаны последние {len(shown)} из {len(tasks)}.</i>"]
    rows = []
    for task in shown:
        mark = "✅ " if task["id"] in selected else ""
        archived = " (архив)" if task.get("archived_at") else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{task_title(task)}{archived}"[:60],
            callback_data=f"chrate:task:{token}:{task['id']}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ К рейтингу чата", callback_data="chrate:back")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("chrate:tasks:"))
async def chrate_tasks(callback: types.CallbackQuery):
    ok, code = await _checked_city(callback, callback.data.split(":", 2)[2])
    if not ok:
        return
    text, kb = await _render_tasks(code, code or _GLOBAL)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("chrate:task:"))
async def chrate_task_toggle(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4 or not parts[3].isdigit():
        await callback.answer("Кнопка устарела — откройте экран заново.", show_alert=True)
        return
    ok, code = await _checked_city(callback, parts[2])
    if not ok:
        return
    task_id = int(parts[3])
    task = await get_task(task_id)
    if task is None or (code and task.get("event_city") not in (None, code)):
        await callback.answer("Такого задания у города нет — список обновлён.", show_alert=True)
        text, kb = await _render_tasks(code, parts[2])
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return
    _mode, _rules, _currency, selected = await _effective(code)
    if task_id in selected:
        selected.remove(task_id)
        note = "Снято"
    else:
        selected.append(task_id)
        note = "Отмечено"
    key = _key(chat_score.SOCIAL_TASKS_KEY, code)
    if selected:
        await set_setting_by_admin(callback.from_user.id, key, "\n".join(str(i) for i in selected))
    elif code:
        # Сняты все галочки у города — это «у города пусто», а не «как в остальных городах»:
        # пустую строку настройка не хранит, поэтому маркер — несуществующий id 0 (читатели
        # берут только id > 0).
        await set_setting_by_admin(callback.from_user.id, key, "0")
    else:
        await delete_setting_by_admin(callback.from_user.id, key)
    text, kb = await _render_tasks(code, parts[2])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(note)


# «📥 Загрузить историю чата» (chimp:*, ввод ChatExportImport) — хвост admin.router после хендлеров этого файла.
from handlers import admin_chat_import  # noqa: E402,F401
