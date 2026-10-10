"""«🔎 Найти настройку» в админке бота.

Менеджер пишет слово («приветствие», «оплата») — бот показывает до восьми подходящих
настроек кнопками «подпись · раздел». Кнопка ведёт ровно туда же, куда кнопка настройки на
экране её группы (`settings_edit:` / `settings_photo:` / `settings_file:`), поэтому экран
правки, проверки, права и «Назад» после сохранения — общие, второго редактора нет.

Ищем только то, что бот и так показывает на экранах групп настроек (`SETTINGS_GROUPS` +
«📦 Прочие» + фото/файлы события) и экраны-кнопки разделов (`admin_sections.SECTIONS`,
«🖼 Аватар бота», «👥 Роли и доступы»…): найденное обязано открываться здесь же, в боте.
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
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from domain.cities import ALL_CITIES, admin_selected_city, city_codes, city_label, cities_module_on, is_per_city
from handlers.admin import router
from handlers.states import SettingsSearch
from domain.settings.schema import SETTINGS_SCHEMA
from domain.settings.search import Candidate, search, search_terms

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
TOO_LONG_TEXT = "Слишком длинно, напишите одно-два слова — например «оплата» или «приветствие»."
# Запрос длиннее — не поиск, а, скорее всего, случайно отправленный текст; эхо в ответе режем,
# чтобы длинный запрос не раздул ответ сверх 4096 символов.
QUERY_MAX = 100
ECHO_MAX = 60


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
                terms=search_terms(key, bot=True),
                extra={"cb": f"settings_edit:{key}", "section": group_label},
            ))
    media_section = s._settings_group_label(s.PHOTO_FILE_GROUP)
    for fields, cb in ((s.PHOTO_FIELDS, "settings_photo"), (s.FILE_FIELDS, "settings_file")):
        for prefix, label, prompt in fields:
            out.append(Candidate(
                key=prefix, label=label, help=prompt,
                terms=search_terms(prefix, bot=True) + search_terms(f"{prefix}_photo_file_id", bot=True),
                extra={"cb": f"{cb}:{prefix}", "section": media_section, "media": True},
            ))
    return out


async def screen_candidates(admin_id: int) -> list[Candidate]:
    """Экраны-кнопки разделов («🖼 Аватар бота», «👥 Роли и доступы»…): их нет в реестре, но
    менеджер ищет их тем же словом. Кнопка — тот же callback, что из раздела; видимость — та
    же, что у строки раздела (`visible_rows`: право строки, «только суперадмину»)."""
    from handlers import admin_sections as sec  # ленивый шов (цикл импортов)

    caps = await sec.resolve_capabilities(admin_id)
    is_super = admin_id in config.ADMIN_IDS
    amb_on = await sec._amb_section_on()
    out: list[Candidate] = []
    for token, section_label, _rows in sec.SECTIONS:
        for row in sec.visible_rows(token, caps, is_super):
            cb = sec.row_callback(row)
            if row[0] not in ("screen", "screen_admin") or cb == "settings_search":
                continue
            label = section_label
            if token == "amb" and not amb_on:
                # Модуль отбора выключен — раздела нет; волны и ступени живут в «🎮 Геймификации».
                if cb not in sec._AMB_OFF_GAME_ROWS:
                    continue
                label = sec._SECTION_LABELS["game"]
            out.append(Candidate(key=f"screen:{cb}", label=row[2], extra={"cb": cb, "section": label, "screen": True}))
    return out


async def _hide_city_keys(admin_id: int, header: str | None) -> bool:
    """Скрывать ли городские настройки: менеджер видит не все города, а в шапке не его город
    (общее значение городской настройки ему не принадлежит)."""
    if not await cities_module_on():
        return False
    from domain.settings.ops import per_city_visible_codes

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
    pool += await screen_candidates(admin_id)
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
    q = query.strip()
    q = html.escape(q if len(q) <= ECHO_MAX else q[: ECHO_MAX - 1] + "…")
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
    from keyboards.builders import all_menu_button_texts
    from keyboards.menu_dynamic import is_dynamic_menu_text

    text = message.text or ""
    # Команда или тап по кнопке меню — не запрос: менеджер ушёл из поиска. Сбрасываем ожидание
    # и отдаём сообщение настоящему обработчику дальше по роутерам (тот же приём, что у правки
    # настройки в admin_settings.settings_edit_value).
    if text.startswith("/") or text in all_menu_button_texts() or await is_dynamic_menu_text(text):
        await state.clear()
        raise SkipHandler
    if len(text.strip()) > QUERY_MAX:
        await message.answer(TOO_LONG_TEXT, reply_markup=_cancel_kb())
        return
    screen = await results_screen(message.from_user.id, text)
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
