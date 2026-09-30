"""Экран «🎓 Ступени амбассадоров» (раздел «🎮 Геймификация», право moderate_game).

Что здесь есть:
- сводка программы цифрами (включена ли, пороги, разборы резюме выдано/квота, лист ожидания,
  дедлайн) и два тумблера кнопками — «🎓 Программа» и «🙈 Имена приглашённых»;
- «📥 Выгрузить CSV по амбассадорам» — строка на амбассадора, по приглашённым только числа
  (`database.amb_tiers_db.export_ambassador_tiers_csv`);
- ручное исключение приглашённого из зачёта (накрутка): кого → причина → подтверждение;
  список исключённых по 10 с «↩️ Вернуть в зачёт».

Число ступеней, пороги, квоты и тексты собираются на экране «🪜 Лестница ступеней»
(`handlers/admin_amb_tier_ladder.py`) — здесь кнопка ведёт туда. Правила подсчёта и выдачи ступеней — в
`services/amb_tiers.py` (одна точка), этот модуль их не дублирует.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router` и подключается хвостовым
импортом `handlers/admin_game_wave_wizard.py` — последнего файла игровой цепочки
(admin_gamification → admin_game_tasks → admin_game_waves → admin_game_wave_wizard), чтобы не
растить эти модули и не трогать main.py (docs/CONVENTIONS.md, приём швов).

Исключение не снимает уже выданных ступеней; возврат в зачёт пересчитывает ступени только
вверх (`check_tiers` лишь добавляет строки). Каждое действие пишется в лог с id менеджера,
причина и автор хранятся в самой строке `ambassador_exclusions`.
"""
from __future__ import annotations

import csv
import html
import io
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from database import amb_journal_db
from database import amb_tiers_db
from database import db
from reg_labels import STATUS_LABELS
from services import amb_tiers
from settings_audit import set_setting_by_admin
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from handlers.states import AmbExclude
from handlers.admin import router

logger = logging.getLogger(__name__)

_PAGE = 10
_STAMP = "%Y-%m-%d %H:%M:%S"

_TOGGLES = {
    # callback-суффикс -> (ключ, подпись «вкл», подпись «выкл»)
    "program": ("amb_qualified_program", "включена", "выключена"),
    "hide": ("amb_hide_invitee_names", "скрыты", "видны"),
}

_PERSON_PROMPT = (
    "Кого исключить из зачёта амбассадора? Пришлите @username человека, его telegram id "
    "(число) или перешлите сюда любое его сообщение.\n\nПередумали — нажмите «❌ Отмена»."
)
_NOT_FOUND = (
    "Не нашёл такого человека среди зарегистрированных. Пришлите @username, telegram id "
    "или перешлите его сообщение."
)
_NOT_INVITED = "Этот человек пришёл не по чьей-то ссылке — исключать из зачёта нечего."
_REASON_PROMPT = (
    "Коротко напишите причину — она сохранится в журнале.\n"
    "Например: накрутка — аккаунт создан в день регистрации"
)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ambt_excl_cancel")],
    ])


def _person_label(user: dict | None, fallback_id: int | None = None) -> str:
    if not user:
        return f"id {fallback_id}" if fallback_id else "—"
    name = html.escape(str(user.get("full_name") or user.get("telegram_id")))
    username = (user.get("username") or "").strip().lstrip("@")
    return f"{name} (@{html.escape(username)})" if username else name


async def _edit_or_send(message: types.Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Тот же приём, что `admin_gamification._edit_or_send_screen`: правим сообщение,
    «not modified» — молча, прочие отказы (фото, удалено) — новым сообщением."""
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── главный экран ────────────────────────────────────────────────────────────────────────

async def _deadline_line() -> str:
    raw = ((await get_setting_typed("amb_count_deadline")) or "").strip()
    if not raw:
        return "без дедлайна"
    if await amb_tiers.deadline_passed():
        return f"{html.escape(raw)} (МСК) — уже прошёл, новые ступени не выдаются"
    return f"{html.escape(raw)} (МСК)"


async def _tiers_screen() -> tuple[str, InlineKeyboardMarkup]:
    from handlers.admin_sections import owner_back_button

    program = await amb_tiers.program_on()
    hide = await get_setting_typed("amb_hide_invitee_names") == "on"
    cfg = await amb_tiers.tiers_config()
    summary = await amb_tiers_db.tiers_summary()
    excluded = await amb_tiers_db.count_exclusions()

    lines = [
        "<b>🎓 Ступени амбассадоров</b>",
        "Амбассадору засчитываются только приглашённые, прошедшие отбор (заявка одобрена).",
        "",
        f"Программа: <b>{'✅ включена' if program else '❌ выключена'}</b>",
    ]
    if not program:
        lines.append(
            "Пока выключено, ступени не выдаются и сообщений амбассадорам нет. Выгрузка и "
            "исключения работают — можно подготовиться заранее."
        )
    lines.append("")
    for c in cfg:
        stat = summary.get(c.n, {"reached": 0, "granted": 0, "waitlist": 0})
        line = f"Ступень {c.n} — {c.threshold} прошедших отбор: получили {stat['reached']}"
        if c.quota is not None:
            line += f", наград выдано {stat['granted']} из {c.quota}, ждут {stat['waitlist']}"
        lines.append(line)
    lines += [
        "",
        f"Дедлайн подсчёта: {await _deadline_line()}",
        f"Имена приглашённых амбассадору: {'скрыты' if hide else 'видны'}",
        f"Исключено из зачёта: {excluded}",
    ]
    rows = [
        [InlineKeyboardButton(
            text=f"🎓 Программа: {'включена' if program else 'выключена'}",
            callback_data="ambt_toggle:program",
        )],
        [InlineKeyboardButton(
            text=f"🙈 Имена приглашённых: {'скрыты' if hide else 'видны'}",
            callback_data="ambt_toggle:hide",
        )],
        [InlineKeyboardButton(text="📥 Выгрузить CSV по амбассадорам", callback_data="ambt_csv")],
        [InlineKeyboardButton(text="🚫 Исключить приглашённого из зачёта", callback_data="ambt_excl")],
        [InlineKeyboardButton(text=f"📋 Исключённые ({excluded})", callback_data="ambt_excl_list:0")],
        [InlineKeyboardButton(text="🪜 Лестница ступеней", callback_data="ambl:main")],
        [await owner_back_button("admin_amb_tiers")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_amb_tiers")
async def show_amb_tiers(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await _tiers_screen()
    await _edit_or_send(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ambt_toggle:"))
async def amb_tiers_toggle(callback: types.CallbackQuery):
    spec = _TOGGLES.get(callback.data.split(":", 1)[1])
    if spec is None:
        await callback.answer("Кнопка устарела — откройте экран заново", show_alert=True)
        return
    key, on_label, off_label = spec
    new_val = "off" if await get_setting_typed(key) == "on" else "on"
    await set_setting_by_admin(callback.from_user.id, key, new_val)
    note = f"{SETTINGS_SCHEMA[key]['label']}: {on_label if new_val == 'on' else off_label}"
    # Алерт Telegram — не длиннее 200 символов: подпись реестра + одна фраза о том, что
    # изменилось; полное описание программы — на самом экране.
    if key == "amb_qualified_program":
        note += (
            "\n\nПересчёт для одобренных раньше ещё не запускали? Выключите и попросите "
            "разработчика: сначала пересчёт, потом включение." if new_val == "on"
            else "\n\nНовые ступени не выдаются, уже выданные остаются."
        )
    else:
        note += (
            "\n\nАмбассадор видит только цифры, без имён." if new_val == "on"
            else "\n\nАмбассадор снова видит имена приглашённых."
        )
    await callback.answer(note, show_alert=True)
    text, kb = await _tiers_screen()
    await _edit_or_send(callback.message, text, kb)


@router.callback_query(F.data == "ambt_csv")
async def amb_tiers_csv(callback: types.CallbackQuery):
    headers, rows = await amb_tiers_db.export_ambassador_tiers_csv(await amb_tiers.current_season())
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    document = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="ambassadors_tiers.csv")
    await callback.message.answer_document(document, caption="Амбассадоры: прогресс и ступени")
    await callback.answer()


# ── исключение приглашённого ─────────────────────────────────────────────────────────────

def _resolve_person_input(message) -> tuple[int | None, str | None, str | None]:
    """`(telegram_id, username, ошибка)` из ввода менеджера. Пересылка (`forward_origin`
    первым — старые поля Bot API 7.0 больше не присылает), число — id, `@…` — username."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        if sender is not None:
            return sender.id, None, None
        return None, None, (
            "В этой пересылке не видно аккаунта человека — у него скрыт аккаунт при "
            "пересылке или сообщение из чата. Пришлите его @username или telegram id."
        )
    forwarded = getattr(message, "forward_from", None)
    if forwarded is not None:
        return forwarded.id, None, None
    body = (message.text or "").strip()
    if body.startswith("@") and len(body) > 1:
        return None, body, None
    if body.isascii() and body.isdigit():
        return int(body), None, None
    return None, None, (
        "Не понял, кого исключить. Пришлите @username, telegram id (число) или перешлите "
        "сообщение этого человека."
    )


def _is_cancel(message) -> bool:
    body = (message.text or "").strip()
    return body.startswith("/") or body.lower() in {"отмена", "❌ отмена"}


@router.callback_query(F.data == "ambt_excl")
async def amb_exclude_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AmbExclude.waiting_for_person)
    await callback.message.answer(_PERSON_PROMPT, reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "ambt_excl_cancel")
async def amb_exclude_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await _tiers_screen()
    await callback.message.answer("Отменено, никого не исключили.")
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(AmbExclude.waiting_for_person)
async def amb_exclude_person_step(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer("Отменено, никого не исключили.")
        return
    tid, username, error = _resolve_person_input(message)
    if error:
        await message.answer(error, reply_markup=_cancel_kb())
        return
    user = await db.get_user_by_username(username) if username else await db.get_user(tid)
    if not user:
        await message.answer(_NOT_FOUND, reply_markup=_cancel_kb())
        return
    invitee_id = int(user["telegram_id"])
    referrer_id = user.get("referrer_id")
    if not referrer_id or int(referrer_id) == invitee_id:
        await message.answer(_NOT_INVITED, reply_markup=_cancel_kb())
        return
    if await amb_tiers_db.get_exclusion(invitee_id):
        await message.answer(
            f"{_person_label(user)} уже исключён из зачёта. Вернуть его можно в «📋 Исключённые».",
            parse_mode="HTML", reply_markup=_cancel_kb(),
        )
        return
    referrer = await db.get_user(int(referrer_id))
    status = STATUS_LABELS.get(user.get("status") or "", user.get("status") or "—")
    await state.update_data(
        invitee_id=invitee_id, referrer_id=int(referrer_id),
        invitee_label=_person_label(user), referrer_label=_person_label(referrer, int(referrer_id)),
    )
    await state.set_state(AmbExclude.waiting_for_reason)
    await message.answer(
        f"<b>{_person_label(user)}</b>\n"
        f"Пригласил: {_person_label(referrer, int(referrer_id))}\n"
        f"Статус заявки: {html.escape(str(status))}\n\n{html.escape(_REASON_PROMPT)}",
        parse_mode="HTML", reply_markup=_cancel_kb(),
    )


@router.message(AmbExclude.waiting_for_reason)
async def amb_exclude_reason_step(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer("Отменено, никого не исключили.")
        return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer(
            "Нужна причина текстом. " + _REASON_PROMPT, reply_markup=_cancel_kb(),
        )
        return
    reason = reason[:500]
    data = await state.update_data(reason=reason)
    await state.set_state(AmbExclude.waiting_for_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Исключить", callback_data="ambt_excl_go"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ambt_excl_cancel"),
    ]])
    row = await amb_journal_db.get_row(int(data["invitee_id"]))
    coins = int(row.get("coins") or 0) if row and not row.get("excluded_at") else 0
    if coins > 0:
        wave_part = ", очки текущей волны" if row.get("wave_id") else ""
        removed = (
            f"Снимется: {coins} баллов (обратной строкой в истории){wave_part}, зачёт в "
            "прогрессе, выгрузке и рейтинге чата."
        )
    elif row:
        removed = "Снимется зачёт в прогрессе, выгрузке и рейтинге чата. Баллов за него не начислялось."
    else:
        removed = "Баллов за него пока не начислено; при одобрении он не засчитается."
    await message.answer(
        f"Исключить {data['invitee_label']} из зачёта амбассадора {data['referrer_label']}? "
        f"{removed} Уже выданную ступень это не снимает — для этого есть «Снять ступень» "
        "на экране ступеней.\n\n"
        f"Причина: {html.escape(reason)}",
        parse_mode="HTML", reply_markup=kb,
    )


@router.message(AmbExclude.waiting_for_confirm)
async def amb_exclude_confirm_hint(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer("Отменено, никого не исключили.")
        return
    await message.answer(
        "Нажмите «✅ Исключить» или «❌ Отмена» в сообщении выше.", reply_markup=_cancel_kb(),
    )


@router.callback_query(F.data == "ambt_excl_go")
async def amb_exclude_go(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    invitee_id, reason = data.get("invitee_id"), data.get("reason")
    if not invitee_id or not reason:
        await state.clear()
        await callback.answer("Кнопка устарела — начните исключение заново", show_alert=True)
        return
    await state.clear()
    from services import amb_journal
    before = await amb_journal_db.get_row(int(invitee_id))
    won = await amb_journal.exclude(int(invitee_id), by=callback.from_user.id, reason=reason)
    if won:
        spent = int(before.get("coins") or 0) if before and not before.get("excluded_at") else 0
        tail = f", списано {spent} баллов" if spent > 0 else ""
        await callback.message.answer(
            f"Готово: {data.get('invitee_label')} исключён{tail}. Уже выданная ступень "
            "амбассадора осталась — снять её можно кнопкой «Снять ступень».", parse_mode="HTML",
        )
    else:
        await callback.message.answer("Этот человек уже исключён — ничего не изменилось.")
    text, kb = await _tiers_screen()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── список исключённых и возврат в зачёт ─────────────────────────────────────────────────

def _parse_int_suffix(data: str) -> int | None:
    tail = data.rsplit(":", 1)[-1]
    return int(tail) if tail.isascii() and tail.isdigit() else None


async def _exclusions_screen(offset: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await amb_tiers_db.count_exclusions()
    offset = max(0, min(offset, max(total - 1, 0) // _PAGE * _PAGE))
    rows = await amb_tiers_db.list_exclusions(_PAGE, offset)
    if not rows:
        text = "<b>📋 Исключённые из зачёта</b>\n\nПока никого не исключали."
    else:
        lines = [f"<b>📋 Исключённые из зачёта</b> ({offset + 1}–{offset + len(rows)} из {total})", ""]
        for n, row in enumerate(rows, start=offset + 1):
            name = html.escape(str(row.get("invitee_name") or row["invitee_id"]))
            referrer = html.escape(str(row.get("referrer_name") or row.get("referrer_id") or "—"))
            lines.append(
                f"{n}. {name} — пригласил {referrer}\n"
                f"   Причина: {html.escape(str(row['reason']))} ({html.escape(str(row['excluded_at']))[:16]})"
            )
        text = "\n".join(lines)
    buttons = [
        [InlineKeyboardButton(
            text=f"↩️ Вернуть в зачёт: {str(row.get('invitee_name') or row['invitee_id'])[:40]}",
            callback_data=f"ambt_unexcl:{row['invitee_id']}",
        )]
        for row in rows
    ]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️ Раньше", callback_data=f"ambt_excl_list:{max(offset - _PAGE, 0)}"))
    if offset + _PAGE < total:
        nav.append(InlineKeyboardButton(text="Дальше ▶️", callback_data=f"ambt_excl_list:{offset + _PAGE}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="← К ступеням амбассадоров", callback_data="admin_amb_tiers")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("ambt_excl_list:"))
async def amb_exclusions_list(callback: types.CallbackQuery):
    text, kb = await _exclusions_screen(_parse_int_suffix(callback.data) or 0)
    await _edit_or_send(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ambt_unexcl:"))
async def amb_unexclude_confirm(callback: types.CallbackQuery):
    invitee_id = _parse_int_suffix(callback.data)
    row = await amb_tiers_db.get_exclusion(invitee_id) if invitee_id else None
    if row is None:
        await callback.answer("Этого человека уже вернули в зачёт", show_alert=True)
        return
    user = await db.get_user(invitee_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Вернуть", callback_data=f"ambt_unexcl_go:{invitee_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ambt_excl_list:0"),
    ]])
    journal = await amb_journal_db.get_row(invitee_id)
    coins = int(journal.get("coins") or 0) if journal and journal.get("reversal_coin_id") else 0
    back = f"Амбассадору вернутся {coins} баллов новой строкой. " if coins > 0 else ""
    await _edit_or_send(
        callback.message,
        f"Вернуть {_person_label(user, invitee_id)} в зачёт? {back}Он снова будет "
        "учитываться в прогрессе, выгрузке и очках волны; если из-за этого амбассадор дотянет "
        "до следующей ступени — он её получит. Запись об исключении удалится.",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ambt_unexcl_go:"))
async def amb_unexclude_go(callback: types.CallbackQuery):
    invitee_id = _parse_int_suffix(callback.data)
    from services import amb_journal
    won = await amb_journal.unexclude(invitee_id, by=callback.from_user.id) if invitee_id else False
    if not won:
        await callback.answer("Этого человека уже вернули в зачёт", show_alert=True)
        return
    user = await db.get_user(invitee_id)
    referrer_id = (user or {}).get("referrer_id")
    if referrer_id:
        try:
            await amb_tiers.check_tiers([int(referrer_id)])
        except Exception:
            logger.exception("amb_tiers: пересчёт после возврата в зачёт не прошёл")
    await callback.answer("Вернули в зачёт", show_alert=False)
    text, kb = await _exclusions_screen(0)
    await _edit_or_send(callback.message, text, kb)


__all__ = [
    "show_amb_tiers", "amb_tiers_toggle", "amb_tiers_csv",
    "amb_exclude_start", "amb_exclude_cancel", "amb_exclude_person_step",
    "amb_exclude_reason_step", "amb_exclude_confirm_hint", "amb_exclude_go",
    "amb_exclusions_list", "amb_unexclude_confirm", "amb_unexclude_go",
]
