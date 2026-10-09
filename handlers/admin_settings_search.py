"""«🔎 Найти настройку» в админке бота.

Менеджер пишет слово («приветствие», «оплата») — бот показывает до восьми подходящих
настроек кнопками «подпись · раздел». Кнопка ведёт ровно туда же, куда кнопка настройки на
экране её группы (`settings_edit:` / `settings_photo:` / `settings_file:`), поэтому экран
правки, проверки, права и «Назад» после сохранения — общие, второго редактора нет.

Ищем только то, что бот и так показывает на экранах групп настроек (`SETTINGS_GROUPS` +
«📦 Прочие» + фото/файлы события): найденная настройка обязана открываться здесь же, в боте.
Вход — первая строка раздела «🔧 Управление» (`admin_sections.SECTIONS`): корень админки по
решению фазы 20 — только разделы, не больше десяти строк.
Сопоставление и ранжирование — `settings_search` (тот же модуль отдаёт синонимы Mini App).

Права: вход и сам поиск — право «⚙️ Настройки» (как у всех `settings_*`). Менеджер, у
которого в правах один город, а в шапке «Все города», не увидит городских настроек — общее
значение городской настройки он править не может (то же правило, что `editable` в Mini App).

Шов к общему `admin.router`, импортируется хвостом `handlers/admin.py`.
"""
from __future__ import annotations

import html

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import ALL_CITIES, admin_selected_city, city_codes, city_label, cities_module_on, is_per_city
from handlers.admin import router
from handlers.states import SettingsSearch
from settings_schema import SETTINGS_SCHEMA
from settings_search import Candidate, search, search_terms

# INVARIANT (13-01 cap-test): каждый `@router.*` декоратор ниже — в ОДНУ строку.

RESULT_LIMIT = 8
_BUTTON_MAX = 60

ENTRY_BUTTON_TEXT = "🔎 Найти настройку"
PROMPT_TEXT = (
    "🔎 <b>Найти настройку</b>\n\n"
    "Напишите, что ищете, одним-двумя словами — например «оплата», «приветствие» или «дата». "
    "Покажу подходящие настройки кнопками."
)
NOT_FOUND_TEXT = "Не нашёл. Попробуйте другое слово, например «оплата» или «приветствие»."
NOT_TEXT_TEXT = "Пришлите слово текстом — например «оплата» или «приветствие»."


def entry_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=ENTRY_BUTTON_TEXT, callback_data="settings_search")


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="settings_search_cancel")],
    ])


def candidates() -> list[Candidate]:
    """Все настройки, которые бот показывает на экранах групп, — с подписью раздела."""
    from handlers import admin_settings as s  # ленивый шов: admin_settings импортирует этот модуль хвостом цепочки

    field_labels = {k: lbl for k, lbl, _ in s.SETTINGS_FIELDS}
    out: list[Candidate] = []
    seen: set[str] = set()
    for group_label, token in s._settings_nav_groups():
        for key in s._settings_group_keys(token):
            if key in seen:
                continue
            seen.add(key)
            entry = SETTINGS_SCHEMA.get(key, {})
            out.append(Candidate(
                key=key,
                label=field_labels.get(key) or entry.get("label") or key,
                help=entry.get("prompt") or "",
                terms=search_terms(key),
                extra={"cb": f"settings_edit:{key}", "section": group_label},
            ))
    media_section = s._settings_group_label(s.PHOTO_FILE_GROUP)
    for fields, cb in ((s.PHOTO_FIELDS, "settings_photo"), (s.FILE_FIELDS, "settings_file")):
        for prefix, label, prompt in fields:
            out.append(Candidate(
                key=prefix, label=label, help=prompt,
                terms=search_terms(prefix) + search_terms(f"{prefix}_photo_file_id"),
                extra={"cb": f"{cb}:{prefix}", "section": media_section, "media": True},
            ))
    return out


async def _hide_city_keys(admin_id: int, header: str | None) -> bool:
    """Скрывать ли городские настройки: менеджер видит не все города, а в шапке не его город
    (общее значение городской настройки ему не принадлежит)."""
    if not await cities_module_on():
        return False
    from settings_ops import per_city_visible_codes

    visible = await per_city_visible_codes(admin_id)
    if visible == city_codes():
        return False
    return header in (None, ALL_CITIES) or header not in visible


async def find(admin_id: int, query: str) -> tuple[list[Candidate], int]:
    """(до RESULT_LIMIT найденных, сколько нашлось всего) — с учётом прав на город."""
    header = await admin_selected_city(admin_id)
    pool = candidates()
    if await _hide_city_keys(admin_id, header):
        pool = [c for c in pool if c.extra.get("media") or not is_per_city(c.key)]
    found = search(pool, query)
    return found[:RESULT_LIMIT], len(found)


def _button_text(c: Candidate) -> str:
    text = f"{c.label} · {c.extra['section']}"
    return text if len(text) <= _BUTTON_MAX else text[: _BUTTON_MAX - 1] + "…"


async def results_screen(admin_id: int, query: str) -> tuple[str, InlineKeyboardMarkup] | None:
    """Экран результатов или `None`, если ничего не нашлось."""
    shown, total = await find(admin_id, query)
    if not shown:
        return None
    q = html.escape(query.strip())
    lines = [f"🔎 По запросу «{q}» нашлось: {total}. Нажмите нужную настройку:"]
    if total > len(shown):
        lines.append(f"<i>Показываю первые {len(shown)} — уточните слово, если нужной нет.</i>")
    header = await admin_selected_city(admin_id)
    if header and header != ALL_CITIES and any(is_per_city(c.key) for c in shown if not c.extra.get("media")):
        lines.append(f"🏙 В шапке {html.escape(await city_label(header))}: городские настройки откроются для этого города.")
    rows = [[InlineKeyboardButton(text=_button_text(c), callback_data=c.extra["cb"])] for c in shown]
    rows.append([InlineKeyboardButton(text="🔎 Искать другое", callback_data="settings_search")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="settings_search_cancel")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "settings_search")
async def settings_search_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SettingsSearch.waiting_query)
    await callback.message.edit_text(PROMPT_TEXT, parse_mode="HTML", reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "settings_search_cancel")
async def settings_search_cancel(callback: types.CallbackQuery, state: FSMContext):
    from handlers.admin_sections import settings_return_screen  # ленивый шов (цикл импортов)

    await state.clear()
    # Строка «🔎 Найти настройку» объявлена в разделе «🔧 Управление» — туда и возвращаемся.
    text, kb = await settings_return_screen(callback.from_user.id, callback_data="settings_search")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(SettingsSearch.waiting_query, F.text)
async def settings_search_query(message: types.Message, state: FSMContext):
    screen = await results_screen(message.from_user.id, message.text or "")
    if screen is None:
        # Состояние остаётся: следующее слово можно прислать сразу, без лишней кнопки.
        await message.answer(NOT_FOUND_TEXT, reply_markup=_cancel_kb())
        return
    await state.clear()
    text, kb = screen
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(SettingsSearch.waiting_query)
async def settings_search_not_text(message: types.Message):
    await message.answer(NOT_TEXT_TEXT, reply_markup=_cancel_kb())
