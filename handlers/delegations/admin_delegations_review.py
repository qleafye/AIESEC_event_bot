"""«❔ Проверить курс», «⏳ Не зашли» и «🔗 Привязать вручную» на экране «🏫 Делегации».

Ничего не решается молча. Ответы, курс которых парсер не разобрал, менеджер разбирает
карточками: курс показан так, как написан в форме, решение — две кнопки «✅ ЦА» / «🚫 Не ЦА».
Решение пишется в `delegation_answers` с автором (`set_decision`), а все последствия (поиск
человека по нику, превращение в делегата, отметка колонки «В боте») идут через единственную
точку эффектов `services.delegations.on_answer_available(reason="manual")`.

Карточка человека, которому в боте уже отказали, несёт пометку «в боте отказ»: автоматика его
не одобрила намеренно; «✅ ЦА» менеджера — авторизация, после которой `convert_to_delegate`
проводит его из rejected через pending в approved и называет менеджера в журнале решений.

«⏳ Не зашли» — делегаты ЦА без аккаунта в боте, по вузам. Ввод «кого привязать» — тот же,
что у выдачи ролей (`handlers.access.admin_roles._resolve_staff_input`): пересланное сообщение,
@ник или числовой id; поиска по имени нет — чужое имя стало бы чужим одобрением. После
подтверждения делегат превращается с `link_how='manual'`, строка листа уходит на перезапись
(колонка M покажет «✅ зашёл») — это делает сам `convert_to_delegate`.

Шов на общий `handlers.admin.router` (импорт из хвоста handlers/delegations/admin_delegations.py,
декораторы в одну строку). Права — `dlg_*` и `state:DelegationLink:*` → `moderate_reg`.
ФИО, вуз, курс и ник — чужой текст, в HTML только через `_e()`; в лог — только id.
"""
import logging

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import delegations_db as ddb
from database import ext_forms_db as ef
from database.db import get_reg_started_by_id, get_user
from handlers.admin import router
from handlers.delegations.admin_delegations import (
    _admin_id, _btn, _cancel_input, _cancel_kb, _current_form, _cut, _e, _is_cancel, _kb,
    _show, _tail_int, _to_screen, render_screen,
)
from handlers.states import DelegationLink
from services import delegations
from services.access.person_label import person_label
from services.access.person_search import search_people

logger = logging.getLogger(__name__)

PAGE = 8
_PICK_MAX = 8

_ROW_GONE = "Ответ не найден — обновите список"
_REVIEW_EMPTY = "❔ <b>Проверить курс</b>\n\nВсе ответы разобраны — проверять нечего."
_ABSENT_EMPTY = "⏳ <b>Не зашли в бота</b>\n\nВсе делегаты ЦА уже в боте."
_REJECTED_MARK = "⚠️ В боте у этого человека отказ — одобряйте только если уверены."
_REJECT_CONFIRM = (
    "⚠️ <b>В боте у этого человека отказ</b>\n\n"
    "Если подтвердить: отказ будет снят, заявка одобрена без анкеты, человек получит сообщение "
    "и доступ к QR на вход. Одобрить?")
_AMBIGUOUS_MARK = ("⚠️ Этот ник числится за несколькими людьми в боте — бот не знает, кого "
                   "одобрять. Нажмите «✅ ЦА» и привяжите нужного вручную в «⏳ Не зашли».")
_ALREADY_LINKED = "✅ Уже делегат в боте"
_NOT_FOUND = ("Не нашёл такого человека в боте. Проверьте ник или попросите его нажать /start "
              "и пришлите ещё раз.")
_OTHER_ROW = "Этот человек уже привязан к другой строке формы — сначала разберитесь с ней."
_ROW_TAKEN = "Эта строка формы уже привязана к другому человеку."
_STALE_LINK = "Этот выбор устарел — начните заново"
_ALREADY_LINKED_ALERT = "Этот ответ уже привязан к делегату"
_NOT_ARMED_LINK = (
    "Делегации ещё не включены — бот пока никого не одобряет. Нажмите «✅ Включить делегации» "
    "на экране «🏫 Делегации»."
)
_STATUS_WORDS = {"ok": "ЦА", "no": "не ЦА", "check": "проверить"}


# ── общие кусочки ─────────────────────────────────────────────────────────────────────────

async def _form_and_keys() -> tuple[dict | None, dict]:
    form = await _current_form()
    return form, (await delegations.field_keys() if form else {})


async def _fields_for(form: dict, row: dict, keys: dict) -> dict:
    """Поля делегата из ответа: payload приходит вместе со строкой списка, для строки
    `get_by_id` ответ дочитывается."""
    payload = row.get("payload")
    if payload is None:
        answer = await ef.get_answer(int(row["form_id"]), str(row["answer_id"]))
        payload = (answer or {}).get("payload") or []
    fields = delegations.extract_fields(form, payload, keys)
    fields["course_canonical"] = row.get("course_canonical")
    return fields


def _name(fields: dict) -> str:
    return (fields.get("full_name") or "").strip() or "без имени"


def _univ(row: dict) -> str:
    return (row.get("university") or "").strip() or "вуз не указан"


def _nick(row: dict) -> str:
    needle = (row.get("username_needle") or "").strip()
    return f"@{needle}" if needle else "—"


def _pager(prefix: str, offset: int, total: int) -> list[InlineKeyboardButton]:
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(_btn("⬅️", f"{prefix}:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(_btn("➡️", f"{prefix}:{offset + PAGE}"))
    return nav


def _clamp(offset: int | None, total: int) -> int:
    """Смещение страницы в пределах списка (список мог сократиться, пока кнопка лежала)."""
    offset = max(0, offset or 0)
    if offset >= total:
        offset = max(0, (total - 1) // PAGE * PAGE)
    return offset


async def _row_of_form(row_id: int | None) -> dict | None:
    """Строка оценки, если она принадлежит текущей форме делегаций."""
    if row_id is None:
        return None
    row = await ddb.get_by_id(row_id)
    fid = await delegations.delegation_form_id()
    if row is None or fid is None or int(row["form_id"]) != fid:
        return None
    return row


# ── «❔ Проверить курс» ────────────────────────────────────────────────────────────────────

async def _review_screen(target, offset: int | None) -> None:
    form, keys = await _form_and_keys()
    if form is None:
        await render_screen(target)
        return
    fid = int(form["id"])
    total = await ddb.count_by_status(fid, "check", linked=None)
    if total == 0:
        await _show(target, _REVIEW_EMPTY, _kb([[_to_screen()]]))
        return
    offset = _clamp(offset, total)
    rows_db = await ddb.list_by_status(fid, "check", linked=None, offset=offset, limit=PAGE)
    rows: list[list[InlineKeyboardButton]] = []
    for r in rows_db:
        fields = await _fields_for(form, r, keys)
        rows.append([_btn(_cut(f"{_name(fields)} — {_univ(r)}"), f"dlg_card:{r['id']}")])
    nav = _pager("dlg_review", offset, total)
    if nav:
        rows.append(nav)
    rows.append([_to_screen()])
    text = f"❔ <b>Проверить курс</b> ({total})\nНажмите на человека, чтобы решить."
    await _show(target, text, _kb(rows))


@router.callback_query(F.data.startswith("dlg_review:"))
async def dlg_review(callback: types.CallbackQuery):
    await _review_screen(callback, _tail_int(callback.data))
    await callback.answer()


async def _rejected_in_bot_now(row: dict) -> bool:
    """Отказ в боте — по живому статусу человека, а не по пометке: пометка ставится только на
    одном из путей, а менеджер мог и сам отклонить, и автоотказ сработать позже."""
    if row.get("linked_telegram_id") is not None:
        return False
    if row.get("note") == delegations.NOTE_REJECTED_IN_BOT:
        return True
    tid, _where = await delegations.find_person(row.get("username_needle"))
    if tid is None:
        return False
    user = await get_user(tid)
    return bool(user and user.get("status") == "rejected")


@router.callback_query(F.data.startswith("dlg_card:"))
async def dlg_card(callback: types.CallbackQuery):
    form, keys = await _form_and_keys()
    row = await _row_of_form(_tail_int(callback.data)) if form else None
    if row is None:
        await callback.answer(_ROW_GONE, show_alert=True)
        return
    fields = await _fields_for(form, row, keys)
    linked = row.get("linked_telegram_id") is not None
    lines = [
        f"❔ <b>{_e(_name(fields))}</b>",
        f"Вуз: {_e(_univ(row))}",
        f"Курс как в форме: «{_e(row.get('course_raw') or '—')}»",
        f"Ответ: {_e(row.get('answered_at') or '—')}",
        f"Почта: {_e(fields.get('email') or '—')}",
        f"Ник: {_e(_nick(row))}",
    ]
    if row.get("ta_status") != "check":
        lines.append(f"Сейчас: {_STATUS_WORDS.get(row.get('ta_status'), '—')}")
    if await _rejected_in_bot_now(row):
        lines.append(f"\n{_REJECTED_MARK}")
    if row.get("note") == delegations.NOTE_AMBIGUOUS_NICK:
        lines.append(f"\n{_AMBIGUOUS_MARK}")
    if linked:
        lines.append(f"\n{_ALREADY_LINKED}")
    rows: list[list[InlineKeyboardButton]] = []
    if not linked:
        rows.append([_btn("✅ ЦА", f"dlg_ta:{row['id']}:ok"), _btn("🚫 Не ЦА", f"dlg_ta:{row['id']}:no")])
    rows.append([_btn("← К списку", "dlg_review:0"), _to_screen()])
    await _show(callback, "\n".join(lines), _kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("dlg_ta:"))
async def dlg_ta(callback: types.CallbackQuery):
    parts = str(callback.data).split(":")
    try:
        row_id, status = int(parts[1]), parts[2]
    except (IndexError, ValueError):
        row_id, status = None, ""
    row = await _row_of_form(row_id)
    if row is None or status not in ("ok", "okc", "no"):
        await callback.answer(_ROW_GONE, show_alert=True)
        return
    if row.get("linked_telegram_id") is not None:
        # Карточка устарела: человек уже делегат, «не ЦА» не должен перекрашивать его строку.
        await callback.answer(_ALREADY_LINKED_ALERT, show_alert=True)
        return
    if status == "ok" and await _rejected_in_bot_now(row):
        # Одобрение отклонённого — второй кнопкой и с названием того, что отменяется.
        await _show(callback, _REJECT_CONFIRM, _kb([
            [_btn("✅ Да, одобрить", f"dlg_ta:{row['id']}:okc")],
            [_btn("← Назад", f"dlg_card:{row['id']}")],
        ]))
        await callback.answer()
        return
    if status == "okc":
        status = "ok"
    admin = _admin_id(callback)
    await ddb.set_decision(row["id"], status, admin)
    # Единственная точка последствий: поиск по нику, превращение, отметка колонки «В боте».
    res = await delegations.on_answer_available(int(row["form_id"]), str(row["answer_id"]),
                                                reason="manual")
    logger.info("delegations: решение менеджера %s по ответу %s (admin=%s, converted=%s)",
                status, row["id"], admin, bool(res.get("converted")))
    if status == "no":
        toast = "Отмечено: не ЦА"
    elif res.get("converted") or res.get("verdict") == "already":
        toast = "Отмечено: ЦА — заявка одобрена"
    elif res.get("waiting") == "not_armed":
        toast = "Отмечено: ЦА — одобрим и напишем после «✅ Включить делегации»"
    elif res.get("waiting") == "ambiguous_nick":
        toast = "Отмечено: ЦА — ник у нескольких людей, привяжите нужного в «⏳ Не зашли»"
    else:
        toast = "Отмечено: ЦА — человек ещё не заходил в бота, появится в «⏳ Не зашли»"
    await callback.answer(toast)
    await _review_screen(callback, 0)


# ── «⏳ Не зашли» ──────────────────────────────────────────────────────────────────────────

async def _absent_screen(target, offset: int | None) -> None:
    form, keys = await _form_and_keys()
    if form is None:
        await render_screen(target)
        return
    fid = int(form["id"])
    total = await ddb.count_by_status(fid, "ok", linked=False)
    if total == 0:
        await _show(target, _ABSENT_EMPTY, _kb([[_to_screen()]]))
        return
    offset = _clamp(offset, total)
    rows_db = await ddb.list_by_status(fid, "ok", linked=False, offset=offset, limit=PAGE)
    lines = [f"⏳ <b>Не зашли в бота</b> ({total})",
             "ЦА по форме, но бот их не узнал по нику. Нажмите, чтобы привязать вручную.\n"]
    rows: list[list[InlineKeyboardButton]] = []
    last_univ = None
    for r in rows_db:
        fields = await _fields_for(form, r, keys)
        if _univ(r) != last_univ:
            last_univ = _univ(r)
            lines.append(f"🏫 {_e(last_univ)}")
        label = f"{_name(fields)} ({_nick(r)})"
        lines.append(f"• {_e(label)}")
        rows.append([_btn(_cut(f"🔗 {label}"), f"dlg_link:{r['id']}")])
    nav = _pager("dlg_absent", offset, total)
    if nav:
        rows.append(nav)
    rows.append([_to_screen()])
    await _show(target, "\n".join(lines), _kb(rows))


@router.callback_query(F.data.startswith("dlg_absent:"))
async def dlg_absent(callback: types.CallbackQuery):
    await _absent_screen(callback, _tail_int(callback.data))
    await callback.answer()


# ── «🔗 Привязать вручную» ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dlg_link:"))
async def dlg_link(callback: types.CallbackQuery, state: FSMContext):
    if not await delegations.is_armed():
        await callback.answer(_NOT_ARMED_LINK, show_alert=True)
        return
    form, keys = await _form_and_keys()
    row = await _row_of_form(_tail_int(callback.data)) if form else None
    if row is None:
        await callback.answer(_ROW_GONE, show_alert=True)
        return
    if row.get("linked_telegram_id") is not None:
        await callback.answer(_ALREADY_LINKED_ALERT, show_alert=True)
        return
    fields = await _fields_for(form, row, keys)
    await state.set_state(DelegationLink.waiting_person)
    await state.update_data(dlg_link_row=int(row["id"]), dlg_link_tid=None)
    text = (f"🔗 <b>Привязать {_e(_name(fields))} ({_e(_univ(row))})</b>\n\n"
            "Кого это? Пришлите @ник, Telegram ID или перешлите сообщение этого человека. "
            "Привязать можно только того, кто уже открывал бота.")
    await _show(callback, text, _cancel_kb())
    await callback.answer()


def _pick_kb(found: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for person in found[:_PICK_MAX]:
        label = (str(person.get("full_name") or "").strip() or "без имени")
        if person.get("username"):
            label += f" (@{str(person['username']).lstrip('@')})"
        rows.append([_btn(_cut(label), f"dlg_pick:{person['user_id']}")])
    rows.append([_btn("❌ Отмена", "dlg_cancel")])
    return _kb(rows)


async def _confirm(target, state: FSMContext, tid: int, *, edit: bool) -> None:
    """Экран подтверждения: кого из формы к кому из бота; человек должен быть известен боту
    и не привязан к другой строке формы."""
    form, keys = await _form_and_keys()
    data = await state.get_data()
    row = await _row_of_form(data.get("dlg_link_row")) if form else None
    if row is None or row.get("linked_telegram_id") is not None:
        await state.clear()
        await _show(target, _STALE_LINK, _kb([[_to_screen()]]), edit=edit)
        return
    user = await get_user(tid)
    if user is None and await get_reg_started_by_id(tid) is None:
        await _show(target, _NOT_FOUND, _cancel_kb(), edit=edit)
        return
    bound = str((user or {}).get("delegation_answer_id") or "")
    if bound and bound != str(row["answer_id"]):
        await _show(target, _OTHER_ROW, _cancel_kb(), edit=edit)
        return
    fields = await _fields_for(form, row, keys)
    await state.set_state(DelegationLink.waiting_confirm)
    await state.update_data(dlg_link_tid=int(tid))
    who = f"{_e(await person_label(tid))} (id {int(tid)})"
    text = (f"Привязать <b>{_e(_name(fields))}</b> ({_e(_univ(row))}) к <b>{who}</b>?\n"
            "Заявка будет одобрена без анкеты, человек получит сообщение и доступ к QR на вход.")
    if (user or {}).get("status") == "rejected":
        text += "\n\n⚠️ В боте ему отказали — ручная привязка это решение отменит и одобрит заявку."
    kb = _kb([[_btn("✅ Привязать", "dlg_link_yes")], [_btn("❌ Отмена", "dlg_cancel")]])
    await _show(target, text, kb, edit=edit)


@router.message(StateFilter(DelegationLink.waiting_person))
async def dlg_link_person(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await _cancel_input(message, state)
        return
    from handlers.access.admin_roles import _resolve_staff_input  # ленивый шов: тот же роутер
    tid, marker = _resolve_staff_input(message)
    if tid is None and marker and marker.startswith("@"):
        found = await search_people(marker, include_started=True)
        if not found:
            await message.answer(_NOT_FOUND, reply_markup=_cancel_kb())
            return
        if len(found) > 1:
            more = (f"\nПоказаны первые {_PICK_MAX} — уточните запрос, если нужного нет."
                    if len(found) > _PICK_MAX else "")
            await message.answer(f"Нашлось несколько человек — выберите нужного.{more}",
                                 reply_markup=_pick_kb(found))
            return
        tid = int(found[0]["user_id"])
    if tid is None:
        await message.answer(marker or _NOT_FOUND, reply_markup=_cancel_kb())
        return
    await _confirm(message, state, int(tid), edit=False)


@router.callback_query(F.data.startswith("dlg_pick:"))
async def dlg_pick(callback: types.CallbackQuery, state: FSMContext):
    tid = _tail_int(callback.data)
    if tid is None or await state.get_state() != DelegationLink.waiting_person.state:
        await callback.answer(_STALE_LINK, show_alert=True)
        return
    await _confirm(callback, state, tid, edit=True)
    await callback.answer()


@router.callback_query(F.data == "dlg_link_yes")
async def dlg_link_yes(callback: types.CallbackQuery, state: FSMContext):
    if not await delegations.is_armed():
        await state.clear()
        await callback.answer(_NOT_ARMED_LINK, show_alert=True)
        return
    if await state.get_state() != DelegationLink.waiting_confirm.state:
        await callback.answer(_STALE_LINK, show_alert=True)
        return
    data = await state.get_data()
    form, keys = await _form_and_keys()
    row = await _row_of_form(data.get("dlg_link_row")) if form else None
    tid = data.get("dlg_link_tid")
    if row is None or tid is None:
        await state.clear()
        await callback.answer(_STALE_LINK, show_alert=True)
        return
    tid = int(tid)
    user = await get_user(tid)
    verdict = delegations.decide_link(row, user, tid, how=delegations.LINK_HOW_MANUAL)
    if verdict == "conflict":
        await state.clear()
        await callback.answer(_ROW_TAKEN, show_alert=True)
        await _absent_screen(callback, 0)
        return
    admin = _admin_id(callback)
    fields = await _fields_for(form, row, keys)
    # Решение менеджера с автором — до превращения: привязка вручную и есть решение «ЦА».
    await ddb.set_decision(row["id"], "ok", admin)
    row = await ddb.get_by_id(row["id"])
    res = await delegations.convert_to_delegate(tid, row, fields, how="manual", by=admin)
    await state.clear()
    logger.info("delegations: ручная привязка ответа %s к tid=%s (admin=%s): %s",
                row["id"], tid, admin, sorted(res))
    if res.get("refused"):
        await callback.answer("Не получилось привязать — человек отклонён в боте", show_alert=True)
    elif res.get("already"):
        await callback.answer("Уже привязан")
    elif res.get("conflict"):
        await callback.answer(_ROW_TAKEN, show_alert=True)
    elif res.get("duplicate"):
        await callback.answer("Этот человек уже делегат по другому ответу — ответ привязан тихо")
    else:
        await callback.answer("Привязано — заявка одобрена")
    await _absent_screen(callback, 0)
