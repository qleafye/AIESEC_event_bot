"""Раздел «🤝 Амбассадоры» → «📎 Закрепить приглашённого» (право moderate_game).

Делегат пришёл без реф-ссылки, но его привёл конкретный человек («пригласила, скрины в
чате»). Менеджер вручную закрепляет его за пригласившим: кто пришёл → кто привёл → заметка →
подтверждение с последствиями. Закрепление видно в журнале зачётов с пометкой «вручную»:
автор и заметка. Одобренному приглашённому зачёт и баллы ложатся сразу, ещё не одобренному
— после одобрения. Уже закреплённого перезакрепить нельзя: зачёт уже у прежнего пригласившего.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/admin_amb_bulk.py`.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import city_label_or_none
from database import db as _db
from handlers.admin import router
from handlers.admin_amb_bulk import _PICK_MAX, _forwarded_id, _scope
from handlers.admin_amb_candidates import _alert, _name
from handlers.states import AmbAttach
from services import amb_journal, person_search
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_NOTE_MAX = 300
_INVITEE_PROMPT = (
    "📎 <b>Закрепить приглашённого</b>\n\n"
    "Кого привели? Пришлите @ник, ссылку t.me, Telegram ID или перешлите сообщение этого "
    "делегата."
)
_REFERRER_PROMPT = (
    "Кто привёл? Пришлите @ник, ссылку t.me, Telegram ID или перешлите сообщение этого "
    "делегата."
)
_NOTE_PROMPT = (
    "Откуда известно, что его привёл этот человек? Например: «скрины в чате». "
    "Пришлите «-», чтобы без заметки."
)
_NOT_FOUND = "Не нашёл такого делегата. Проверьте ник или перешлите его сообщение."
_HIDDEN_FORWARD = (
    "В этой пересылке не видно аккаунта человека — у него скрыт аккаунт при пересылке. "
    "Пришлите его @ник или Telegram ID."
)
_SELF = "Нельзя закрепить человека за самим собой."
_CANCELLED = "Отменено."
_STALE = "Этот выбор устарел — начните закрепление заново."


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="ambj_cancel")]])


def _is_cancel(message) -> bool:
    body = (message.text or "").strip()
    return body.startswith("/") or body.lower() in {"отмена", "❌ отмена"}


async def _find(message, scope) -> list[dict] | str:
    """Делегаты, подавшие анкету, в городе админа — или текст ошибки."""
    tid, was_forward = _forwarded_id(message)
    if was_forward:
        if tid is None:
            return _HIDDEN_FORWARD
        query = str(tid)
    else:
        query = (message.text or "").strip()
        if not query:
            return _NOT_FOUND
    found = await person_search.search_people(query, city_scope=scope, limit=_PICK_MAX + 1,
                                              include_started=False)
    return found or _NOT_FOUND


async def _already_text(invitee: dict, current_referrer_id: int) -> str:
    current = await _db.get_user(current_referrer_id)
    who = _name(current) if current else f"ID {current_referrer_id}"
    return (
        f"{_name(invitee)} уже числится за {who} — пришёл по её ссылке или закреплён раньше. "
        "Переписать приглашённого на другого нельзя: зачёт уже у неё. Если это накрутка — "
        "исключите его из зачёта на экране «🎓 Ступени амбассадоров»."
    )


async def _invitee_problem(tid: int) -> str | None:
    """Почему этого человека нельзя закреплять (уже закреплён), либо None."""
    user = await _db.get_user(tid)
    if not user:
        return _NOT_FOUND
    current = user.get("referrer_id")
    if current and int(current) != 0:
        return await _already_text(user, int(current))
    return None


async def _pick_keyboard(found: list[dict], step: str) -> InlineKeyboardMarkup:
    rows = []
    for person in found[:_PICK_MAX]:
        label = str(person.get("full_name") or "").strip() or "без имени"
        city = await city_label_or_none(person.get("city"))
        if city:
            label += f" · {city}"
        rows.append([InlineKeyboardButton(
            text=label[:60], callback_data=f"ambj_pick:{step}:{person['user_id']}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ambj_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _after_search(message: types.Message, state: FSMContext, step: str) -> None:
    scope = await _scope(message.from_user.id)
    found = await _find(message, scope)
    if isinstance(found, str):
        await message.answer(found, reply_markup=_cancel_kb())
        return
    if len(found) == 1:
        await _accept(message, state, step, int(found[0]["user_id"]))
        return
    more = (f"\nПоказаны первые {_PICK_MAX} — уточните запрос, если нужного нет."
            if len(found) > _PICK_MAX else "")
    await message.answer(f"Нашлось несколько делегатов — выберите нужного.{more}",
                         reply_markup=await _pick_keyboard(found, step))


async def _accept(message: types.Message, state: FSMContext, step: str, tid: int) -> None:
    """Принять найденного человека на шаге `step` ('i' — кого привели, 'r' — кто привёл)."""
    if step == "i":
        problem = await _invitee_problem(tid)
        if problem:
            await message.answer(problem, parse_mode="HTML", reply_markup=_cancel_kb())
            return
        await state.update_data(invitee_id=tid)
        await state.set_state(AmbAttach.waiting_referrer)
        await message.answer(_REFERRER_PROMPT, reply_markup=_cancel_kb())
        return
    data = await state.get_data()
    if int(data.get("invitee_id") or 0) == tid:
        await message.answer(_SELF, reply_markup=_cancel_kb())
        return
    if not await _db.get_user(tid):
        await message.answer(_NOT_FOUND, reply_markup=_cancel_kb())
        return
    await state.update_data(referrer_id=tid)
    await state.set_state(AmbAttach.waiting_note)
    await message.answer(_NOTE_PROMPT, reply_markup=_cancel_kb())


async def _confirm_screen(data: dict) -> tuple[str, InlineKeyboardMarkup] | None:
    invitee = await _db.get_user(int(data["invitee_id"]))
    referrer = await _db.get_user(int(data["referrer_id"]))
    if not invitee or not referrer:
        return None
    note = data.get("note")
    if invitee.get("status") == "approved":
        coins = 0
        if int(referrer.get("is_ambassador") or 0) == 1:
            coins = max(int(await get_setting_typed("ambassador_referral_coins") or 0), 0)
        effect = (f"Заявка {_name(invitee)} одобрена — {_name(referrer)} засчитается приглашённый"
                  + (f" и {coins} баллов." if coins else "."))
    else:
        effect = f"Заявка {_name(invitee)} ждёт решения — зачёт появится после одобрения."
    text = (
        f"Закрепить <b>{_name(invitee)}</b> за <b>{_name(referrer)}</b>?\n{effect}\n"
        + (f"Заметка: «{html.escape(note)}»\n" if note else "")
        + "\nВ журнале зачётов это будет видно как «вручную»: кто закрепил и с какой заметкой."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Закрепить", callback_data="ambj_go")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ambj_cancel")],
    ])
    return text, kb


@router.callback_query(F.data == "admin_amb_attach")
async def attach_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AmbAttach.waiting_invitee)
    await callback.message.answer(_INVITEE_PROMPT, parse_mode="HTML", reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "ambj_cancel")
async def attach_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(_CANCELLED)
    await callback.answer()


@router.message(AmbAttach.waiting_invitee)
async def attach_invitee_step(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer(_CANCELLED)
        return
    await _after_search(message, state, "i")


@router.message(AmbAttach.waiting_referrer)
async def attach_referrer_step(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer(_CANCELLED)
        return
    await _after_search(message, state, "r")


@router.callback_query(F.data.startswith("ambj_pick:"))
async def attach_pick(callback: types.CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    current = await state.get_state()
    expected = {"i": AmbAttach.waiting_invitee.state, "r": AmbAttach.waiting_referrer.state}
    try:
        step, tid = parts[1], int(parts[2])
    except (IndexError, ValueError):
        step, tid = "", 0
    if step not in expected or current != expected[step]:
        await callback.answer(_alert(_STALE), show_alert=True)
        return
    await _accept(callback.message, state, step, tid)
    await callback.answer()


@router.message(AmbAttach.waiting_note)
async def attach_note_step(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer(_CANCELLED)
        return
    body = (message.text or "").strip()
    if not body:
        await message.answer(_NOTE_PROMPT, reply_markup=_cancel_kb())
        return
    note = None if body in {"-", "—"} else body[:_NOTE_MAX]
    await state.update_data(note=note)
    data = await state.get_data()
    screen = await _confirm_screen(data)
    if screen is None:
        await state.clear()
        await message.answer(_NOT_FOUND)
        return
    await state.set_state(AmbAttach.waiting_confirm)
    await message.answer(screen[0], parse_mode="HTML", reply_markup=screen[1])


@router.message(AmbAttach.waiting_confirm)
async def attach_confirm_text(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer(_CANCELLED)
        return
    await message.answer("Нажмите «✅ Закрепить» или «❌ Отмена» под сообщением выше.")


@router.callback_query(F.data == "ambj_go")
async def attach_go(callback: types.CallbackQuery, state: FSMContext):
    if await state.get_state() != AmbAttach.waiting_confirm.state:
        await callback.answer(_alert(_STALE), show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    invitee_id, referrer_id = int(data["invitee_id"]), int(data["referrer_id"])
    result = await amb_journal.manual_attach(
        invitee_id, referrer_id, by=callback.from_user.id, note=data.get("note"))
    if result["error"] == "self":
        await callback.message.answer(_SELF)
    elif result["error"] == "no_user":
        await callback.message.answer(_NOT_FOUND)
    elif result["error"] == "already":
        invitee = await _db.get_user(invitee_id) or {}
        await callback.message.answer(
            await _already_text(invitee, int(result["current_referrer"])), parse_mode="HTML")
    else:
        invitee = await _db.get_user(invitee_id)
        referrer = await _db.get_user(referrer_id)
        if result["approved"]:
            tail = "Зачёт записан в журнал" + (f", +{result['coins']} баллов." if result["coins"] else ".")
        else:
            tail = "Заявка ждёт решения — зачёт запишется после одобрения."
        await callback.message.answer(
            f"✅ {_name(invitee)} закреплён за {_name(referrer)}. {tail}", parse_mode="HTML")
    await callback.answer()


# Экран «🪜 Лестница ступеней» (admin_amb_tier_ladder, ambl*) — хвост admin.router после
# хендлеров этого файла.
from handlers import admin_amb_tier_ladder  # noqa: E402,F401
