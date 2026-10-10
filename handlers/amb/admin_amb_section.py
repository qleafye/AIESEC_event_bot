"""Раздел «🤝 Амбассадоры» → экран «🚪 Вход и лимит» (право moderate_game).

Что здесь есть:
- способ входа в команду кнопкой — «⚡ Сразу по кнопке» или «🗳 Отбор менеджером» — через
  экран подтверждения, на котором написано, что изменится у делегатов (кода режима менеджер
  не видит и не вводит);
- лимит мест («пакетов амбассадора») вводом числа с примером формата; лимит меньше уже
  занятых мест не принимается — ошибка объясняет, что сделать;
- счётчики: занято мест из лимита, кандидатов, в команде (и сколько из них без пакета),
  отказано;
- «✏️ Тексты для делегатов» — подменю шести текстов реестра через общий редактор настроек
  (`settings_edit:<key>`, право «⚙️ Настройки»); без этого права — строка, у кого оно есть.

Правила входа — в `services/amb/amb_status.py` (одна точка), этот модуль их не дублирует: только
читает счётчики и пишет две настройки (`amb_join_mode`, `amb_slots_limit`) через
`settings_audit.set_setting_by_admin` — тот же путь с логом `admin=…`, что у остальных экранов.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router` и подключается хвостовым
импортом `handlers/forum/admin_onsite_reg.py` — последнего файла цепочки admin.router, чтобы не
растить admin.py и не трогать main.py (docs/CONVENTIONS.md, приём швов).
"""
from __future__ import annotations

import html
import logging

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import amb_status_db
from handlers.admin import router
from handlers.access.admin_caps import has_capability
from handlers.states import AmbSlotsEdit
from services.amb import amb_status
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

_MODE_LABELS = {
    amb_status.MODE_INSTANT: "⚡ Сразу по кнопке",
    amb_status.MODE_SELECTION: "🗳 Отбор менеджером",
}

# Что делегат увидит после переключения — и в тексте подтверждения, и в алерте (≤200 символов).
_MODE_EFFECT = {
    amb_status.MODE_SELECTION: (
        "Кнопка «Хочу свою ссылку» и ответ «да» в анкете будут делать делегата "
        "кандидатом, а в команду его берёте вы кнопкой «Взять»."
    ),
    amb_status.MODE_INSTANT: (
        "Кнопка «Хочу свою ссылку» снова будет сразу принимать делегата в команду "
        "(ответ «да» в анкете — только показывает эту кнопку после анкеты)."
    ),
}

# Шесть текстов делегату про вход в команду — в порядке пути делегата.
TEXT_KEYS = (
    "amb_candidate_ack_text",
    "amb_status_candidate_text",
    "amb_slots_full_text",
    "amb_taken_text",
    "amb_removed_text",
    "amb_decline_all_text",
)

_NO_SETTINGS_RIGHT = "Тексты меняет тот, у кого есть право «⚙️ Настройки»."

_LIMIT_PROMPT = (
    "🎁 <b>Мест в команде амбассадоров</b>\n\n"
    "Сейчас: <b>{current}</b>. Занято мест: <b>{taken}</b>.\n\n"
    "Пришлите число мест, например <code>17</code>. <code>0</code> — без лимита.\n\n"
    "Место занимает только амбассадор с одобренной заявкой на форум. Когда места "
    "заполнены, кнопка и вопрос анкеты прячутся сами."
)
_LIMIT_NOT_NUMBER = (
    "Не понял. Пришлите целое число, например 17, или 0, чтобы снять лимит."
)
_LIMIT_TOO_SMALL = (
    "Уже занято {taken} мест — лимит не может быть меньше. Освободите места в списке "
    "команды или введите {taken} и больше."
)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ambs_limit_cancel")],
    ])


def _limit_text(limit: int) -> str:
    return str(limit) if limit > 0 else "без лимита"


async def _edit_or_send(message: types.Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Тот же приём, что у `admin_amb_tiers._edit_or_send`: правим сообщение, «not modified» —
    молча, прочие отказы (фото, удалено) — новым сообщением."""
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── тумблер модуля «🤝 Отбор амбассадоров» ────────────────────────────────────────────────
# Выключен — раздела нет в корне /admin (handlers/admin_core.build_admin_keyboard), а устаревшие
# кнопки раздела в чате отвечают алертом, как его включить. Кнопка тумблера — на экране группы
# «🎮 Геймификация → ⚙️ Тексты и настройки» (handlers/admin_settings), не в самом разделе:
# иначе выключенный раздел было бы нечем включить.

SECTION_OFF_ALERT = (
    "Раздел «🤝 Амбассадоры» выключен. Включить: /admin → 🎮 Геймификация → "
    "⚙️ Тексты и настройки → «🤝 Отбор амбассадоров»."
)

_SECTION_CALLBACKS = ("admin_amb_entry", "admin_amb_candidates", "admin_amb_points")
_SECTION_PREFIXES = ("ambs_", "ambc", "ambp:", "ambpt_")


def is_section_callback(data: str | None) -> bool:
    """Кнопка раздела «🤝 Амбассадоры» (вход, кандидаты, массовые действия, выгрузки)."""
    data = data or ""
    return data in _SECTION_CALLBACKS or data.startswith(_SECTION_PREFIXES)


async def _section_off(callback: types.CallbackQuery) -> bool:
    return is_section_callback(callback.data) and not await amb_status.selection_enabled()


@router.callback_query(_section_off)
async def amb_section_off(callback: types.CallbackQuery, state: FSMContext):
    """Модуль выключен, а в чате осталась кнопка раздела — объясняем, где включить."""
    await state.clear()
    await callback.answer(SECTION_OFF_ALERT, show_alert=True)


async def selection_toggle_button() -> InlineKeyboardButton:
    on = await amb_status.selection_enabled()
    state = "✅ Вкл → ❌ Выкл" if on else "❌ Выкл → ✅ Вкл"
    return InlineKeyboardButton(text=f"🤝 Отбор амбассадоров: {state}",
                                callback_data="toggle_amb_team_selection")


_TOGGLE_ALERT = {
    "on": ("🤝 Отбор амбассадоров: ✅ включён. Раздел «🤝 Амбассадоры» — в корне /admin: "
           "способ входа, лимит мест, кандидаты."),
    "off": ("🤝 Отбор амбассадоров: ❌ выключен. Амбассадором снова становятся сразу по кнопке, "
            "без лимита. Кандидаты и настройки сохранены до включения."),
}


@router.callback_query(F.data == "toggle_amb_team_selection")
async def toggle_amb_team_selection(callback: types.CallbackQuery):
    from handlers.settings.admin_settings import build_settings_group_keyboard, render_settings_group_text

    new_val = "off" if await amb_status.selection_enabled() else "on"
    await set_setting_by_admin(callback.from_user.id, amb_status.TOGGLE_KEY, new_val)
    logger.info("admin=%s %s=%s", callback.from_user.id, amb_status.TOGGLE_KEY, new_val)
    alert = _TOGGLE_ALERT[new_val]
    if new_val == "on":
        # Тумблер сам способ входа не меняет — говорим, как вступают прямо сейчас (≤200 символов).
        mode = await amb_status.join_mode()
        _, limit = await amb_status.slot_counter()
        hint = ("Чтобы брать людей вручную" if mode == amb_status.MODE_INSTANT else "Настройки")
        alert = (f"🤝 Отбор включён. Сейчас: {_MODE_LABELS[mode]}, мест: {_limit_text(limit)}. "
                 f"{hint} — «🤝 Амбассадоры» → «🚪 Вход и лимит».")
    await callback.answer(alert, show_alert=True)
    text = await render_settings_group_text("game", callback.from_user.id)
    kb = await build_settings_group_keyboard("game", callback.from_user.id)
    await _edit_or_send(callback.message, text, kb)


# ── главный экран ────────────────────────────────────────────────────────────────────────

async def render_entry_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from handlers.settings.admin_core import _admin_city_view  # ленивый шов, как у экранов заявок
    from handlers.settings.admin_sections import back_button

    mode = await amb_status.join_mode()
    taken, limit = await amb_status.slot_counter()
    # Места — общий лимит события; списки — в городе, выбранном в шапке админки.
    scope, city = await _admin_city_view(admin_id)
    candidates = await amb_status_db.count_by_filter("candidates", city_scope=scope)
    team = await amb_status_db.count_by_filter("team", city_scope=scope)
    no_pack = await amb_status_db.count_by_filter("no_pack", city_scope=scope)
    declined = await amb_status_db.count_by_filter("declined", city_scope=scope)

    lines = ["<b>🤝 Амбассадоры → 🚪 Вход и лимит</b>", ""]
    lines.append(f"Как вступают: <b>{_MODE_LABELS[mode]}</b>")
    lines.append(f"Мест в команде: <b>{_limit_text(limit)}</b>")
    lines.append(
        f"Занято мест: <b>{taken} из {limit}</b>" if limit > 0 else f"Занято мест: <b>{taken}</b>"
    )
    lines.append("")
    if city:
        lines.append(f"Город: {html.escape(city)}")
    lines.append(
        f"Кандидатов: {candidates} · В команде: {team} (без пакета: {no_pack}) · "
        f"Отказано: {declined}"
    )
    lines += [
        "",
        "Место занимает амбассадор с одобренной заявкой на форум. «Без пакета» — в команде, "
        "но заявка ещё не одобрена.",
    ]
    rows = [
        [InlineKeyboardButton(text=f"Способ входа: {_MODE_LABELS[mode]}", callback_data="ambs_mode")],
        [InlineKeyboardButton(text=f"🎁 Мест в команде: {_limit_text(limit)}", callback_data="ambs_limit")],
        [InlineKeyboardButton(text=f"🙋 Кандидаты: {candidates}", callback_data="admin_amb_candidates")],
        [InlineKeyboardButton(text="✏️ Тексты для делегатов", callback_data="ambs_texts")],
        [back_button("admin_amb_entry")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_amb_entry")
async def show_amb_entry(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_entry_screen(callback.from_user.id)
    await _edit_or_send(callback.message, text, kb)
    await callback.answer()


# ── способ входа ─────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ambs_mode")
async def amb_mode_confirm(callback: types.CallbackQuery):
    mode = await amb_status.join_mode()
    target = amb_status.MODE_INSTANT if mode == amb_status.MODE_SELECTION else amb_status.MODE_SELECTION
    text = (
        "<b>🚪 Как становятся амбассадорами</b>\n\n"
        f"Сейчас: <b>{_MODE_LABELS[mode]}</b>\n"
        f"Переключить на: <b>{_MODE_LABELS[target]}</b>\n\n"
        f"Что изменится для делегатов: {_MODE_EFFECT[target]}\n\n"
        "С теми, кто уже в команде или уже попросился, ничего не произойдёт."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Переключить: {_MODE_LABELS[target]}",
                              callback_data=f"ambs_mode_go:{target}")],
        [InlineKeyboardButton(text="← Назад", callback_data="admin_amb_entry")],
    ])
    await _edit_or_send(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ambs_mode_go:"))
async def amb_mode_apply(callback: types.CallbackQuery):
    target = callback.data.split(":", 1)[1]
    if target not in _MODE_LABELS:
        await callback.answer("Кнопка устарела — откройте экран заново", show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, "amb_join_mode", target)
    logger.info("admin=%s amb_join_mode=%s", callback.from_user.id, target)
    await callback.answer(f"Теперь: {_MODE_LABELS[target]}\n\n{_MODE_EFFECT[target]}", show_alert=True)
    text, kb = await render_entry_screen(callback.from_user.id)
    await _edit_or_send(callback.message, text, kb)


# ── лимит мест ───────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ambs_limit")
async def amb_limit_start(callback: types.CallbackQuery, state: FSMContext):
    taken, limit = await amb_status.slot_counter()
    await state.set_state(AmbSlotsEdit.waiting_for_limit)
    await callback.message.answer(
        _LIMIT_PROMPT.format(current=_limit_text(limit), taken=taken),
        parse_mode="HTML", reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "ambs_limit_cancel")
async def amb_limit_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_entry_screen(callback.from_user.id)
    await callback.message.answer("Отменено, лимит не менялся.")
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(StateFilter(AmbSlotsEdit.waiting_for_limit))
async def amb_limit_value(message: types.Message, state: FSMContext):
    body = (message.text or "").strip()
    if body.startswith("/") or body.lower() in {"отмена", "❌ отмена"}:
        await state.clear()
        await message.answer("Отменено, лимит не менялся.")
        return
    if not (body.isascii() and body.isdigit()):
        await message.answer(_LIMIT_NOT_NUMBER, reply_markup=_cancel_kb())
        return
    value = int(body)
    taken = await amb_status_db.slots_taken()
    if value and value < taken:
        await message.answer(_LIMIT_TOO_SMALL.format(taken=taken), reply_markup=_cancel_kb())
        return
    await set_setting_by_admin(message.from_user.id, "amb_slots_limit", str(value))
    logger.info("admin=%s amb_slots_limit=%s", message.from_user.id, value)
    await state.clear()
    text, kb = await render_entry_screen(message.from_user.id)
    await message.answer("✅ Сохранено\n\n" + text, parse_mode="HTML", reply_markup=kb)


# ── тексты для делегатов ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ambs_texts")
async def amb_texts_menu(callback: types.CallbackQuery):
    lines = [
        "<b>✏️ Тексты для делегатов</b>",
        "",
        "Что делегат получает, когда просится в команду амбассадоров, когда его берут, "
        "выводят или вежливо отказывают.",
    ]
    rows = []
    if await has_capability(callback.from_user.id, "settings"):
        rows = [
            [InlineKeyboardButton(text=SETTINGS_SCHEMA[key]["label"], callback_data=f"settings_edit:{key}")]
            for key in TEXT_KEYS
        ]
    else:
        lines += ["", _NO_SETTINGS_RIGHT]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="admin_amb_entry")])
    await _edit_or_send(callback.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


# Экран «🙋 Кандидаты и команда» (admin_amb_candidates, ambc*/ambp:*) — хвост admin.router
# после хендлеров этого файла.
from handlers.amb import admin_amb_candidates  # noqa: E402,F401


@router.callback_query(F.data == "amb_sep")
async def amb_separator(callback: types.CallbackQuery):
    # Подзаголовок «── Общее ──» и соседи в разделе — не кнопка действия.
    await callback.answer("Это подзаголовок — кнопки группы под ним.")
