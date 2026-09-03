"""Phase 13 (13-06, REFAC-01): moderation seam.

`admin.py` (contiguous slice, originally lines 2371-2977) moved byte-for-byte onto the SAME
shared `admin.router` (13-02..13-05 shared-router seam-import technique) — application
moderation (`appr_*`) and receipt moderation (`rcpt_*`), with their shared private helpers
(`_parse_appr`/`_parse_rcpt`, `_render_application_card`/`_render_receipt_card`,
`_appr_card_kb`/`_rcpt_card_kb`, `_show_current_card`/`_show_current_receipt_card`,
`_welcome_flipped`) moved together, internal order intact.

Phase 21 (21-07, FORM-SYNC-04, D-14/D-15): «✏️ Изменена»/«🔁 Повторная подача» card badges
and the «🕓 История» screen (`appr_history`, tail handler) read `users.edited_at`/
`edited_source` + `database.db.get_answer_history` — written by `update_user_answers`/
`record_answer_history`/`mark_user_edited` from plan 21-05, called by the edit-submit flow
plan 21-08 wires up next. This plan is read-only against that trail.

Phase 23 (23-02, APP-TINDER-01): the queue/card core (`_edit_badges_for`/`_format_edited_date`/
`_EDITED_SOURCE_LABELS`/track labels) and the bot-only tail (welcome + reject message + sheet
sync, formerly `_welcome_flipped`) moved to `services/applications.py`/
`services/application_effects.py` — aiogram-free ground floor the Mini App queue (`miniapp/`)
can call without pulling the bot in. Module-level aliases under the old private names keep this
file's handler bodies and order untouched (same technique as `settings_ops.py`, Phase 22).
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.db import (
    get_user,
    get_setting,
    get_pending_users,
    get_pending_count,
    approve_all_pending,
    get_receipt_pending_users,
    get_receipt_pending_count,
    update_payment_status,
    get_answer_history,
)
from services.applications import (
    TRACK_LABELS,
    claim_approve,
    claim_reject,
    EDITED_SOURCE_LABELS as _EDITED_SOURCE_LABELS,
    format_edited_date as _format_edited_date,
    edit_badges_for as _edit_badges_for,
)
from services.application_effects import apply_decision_effects, mass_approve_effects
from services.background import spawn as _spawn
from services.consent import consent_card_line
from handlers.states import Approval, ReceiptReview
from keyboards.builders import get_cancel_kb, get_main_menu_kb
from reg_engine import STEP_TO_COLUMN, label_for
import moderation_card
from settings_schema import get_setting_typed
from cities import city_label, admin_selected_city, city_scope, city_codes, normalize_city, ALL_CITIES, ALL_CITIES_LABEL
from handlers.admin_core import admin_keyboard_for, _admin_city_view, _card_out_of_scope, _OUT_OF_SCOPE_ALERT
from handlers.admin import router

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test, extended by 13-04 to scan every handlers/admin*.py file): every
# `@router.*` decorator below MUST fit on ONE line.


# column (users row) -> human label, via reg_engine.label_for (the engine already resolves the
# nine steps where setting_key != f"reg_q_{step}", plan 21-13) — no second label table.
_COLUMN_TO_LABEL = {col: label_for(step) for step, col in STEP_TO_COLUMN.items()}


# ── Phase 2: application review queue ("Заявки", tinder UI) ───────────────────

def _parse_appr(data: str) -> tuple[str, int | None]:
    """'appr_approve:123' -> ('appr_approve', 123); 'appr_all' -> ('appr_all', None)."""
    if ":" in data:
        prefix, tid = data.split(":", 1)
        try:
            return prefix, int(tid)
        except ValueError:
            return prefix, None
    return data, None


def _render_application_card(user: dict, position: int, total: int, city_label_text: str | None = None, consent_line: str | None = None, edited_line: str | None = None, resubmit_line: str | None = None, fields: list[tuple[str, str]] | None = None, show_resume: bool = True) -> str:
    """HTML card for one pending application; all free-text escaped. `city_label_text` (Phase
    07.2, CITY-02) appends «· 🏙 {label}» to the header when an admin city is selected; None
    keeps the header byte-identical to the pre-CITY-02 line (module off / no city chosen).
    `edited_line`/`resubmit_line` (Phase 21, 21-07, D-14/D-10) are pre-resolved by the caller
    (`_edit_badges_for`, registry template + human date/source already substituted) — this
    function only decides WHETHER to print them, same optional-line contract as `consent_line`.
    Quick 260902-tzh: `fields` — `[(label, value)]` из `moderation_card.card_answers`, вопросы
    анкеты по выбору менеджера (реестр `modcard_fields`); `None` (все старые вызовы) печатает
    ни одной строки вопроса — карточка байт-совместима с версией до этой правки. Шесть
    захардкоженных полей (образование/город/лок.комитет/позиция/аламни/возраст) заменены этим
    циклом; резюме — отдельный блок ниже, гасится целиком только `show_resume=False`."""
    def esc(v):
        return html_module.escape(str(v)) if v not in (None, "", "-") else None

    header = f"📋 <b>Заявка {position}/{total}</b>"
    if city_label_text is not None:
        header += f" · 🏙 {html_module.escape(str(city_label_text))}"
    lines = [header, ""]
    name = esc(user.get("full_name")) or "—"
    uname = esc(user.get("username"))
    lines.append(f"👤 {name}" + (f" ({uname})" if uname else ""))
    # Phase 5 (D-14): shared queue — one extra line for a non-full track, no second queue,
    # no track predicate anywhere in get_pending_users/get_pending_count. Unrecognised values
    # are HTML-escaped (T-05-03-03): the raw DB column value can never inject markup here.
    track = user.get("participant_type") or "full"
    if track != "full":
        # Phase 23 (23-02): подписи треков — services.applications.TRACK_LABELS, тот же
        # словарь, что читает карточка Mini App (card_payload).
        track_label = TRACK_LABELS.get(track, f"🎉 Трек: {html_module.escape(str(track))}")
        lines.append(track_label)
    # Phase 07.3 (05, RET-03): prev_season — сырая строка из БД, пришедшая изначально из
    # текстовой настройки event_season (см. threat T-073-05-01) — обязана пройти то же
    # экранирование, что и неопознанный track выше. Служебный литерал "legacy" (плана 01/04,
    # означает «регистрация до эпохи сезонов») менеджеру не показываем текстом — CLAUDE.md
    # запрещает показывать коды человеку (T-073-05-02).
    prev_season_raw = (user.get("prev_season") or "").strip()
    if prev_season_raw:
        if prev_season_raw == "legacy":
            lines.append("🔁 Повторный: был(а) на прошлом событии")
        else:
            lines.append(f"🔁 Повторный: был(а) в {html_module.escape(prev_season_raw)}")
    # Quick 260902-tzh: ответы анкеты по выбору менеджера (реестр modcard_fields) — единая
    # схема (moderation_card.card_answers), не девять захардкоженных полей.
    for label, value in fields or []:
        lines.append(f"{label}: {html_module.escape(value)}")
    # Резюме: файлом, текстом или нет. Текст показываем прямо в карточке (Таня п.4),
    # обрезая длинные — полный текст доступен по кнопке «📎 Резюме». show_resume=False гасит
    # блок целиком — шаг «resume» выключен в наборе полей карточки.
    if show_resume:
        if user.get("resume_file_id"):
            lines.append("📎 Резюме: файлом (кнопка ниже)")
        elif esc(user.get("resume_text")):
            rt = str(user.get("resume_text"))
            preview = html_module.escape(rt[:300] + ("…" if len(rt) > 300 else ""))
            lines.append(f"📎 Резюме (текст): {preview}")
        else:
            lines.append("📎 Резюме: нет")
    # Phase 21 (21-07, D-14/D-10): «✏️ Изменена …» / «🔁 Повторная подача» — пометки для
    # менеджера о правке уже поданной анкеты, ПЕРЕД строкой согласия (та всегда идёт последней).
    if edited_line:
        lines.append(edited_line)
    if resubmit_line:
        lines.append(resubmit_line)
    # Quick 260822: одна строка «Согласие: v…» (+ маркер старой редакции) — готовый текст
    # от services.consent.consent_card_line; None = подписей нет (модуль выключен/legacy).
    if consent_line:
        lines.append(consent_line)
    return "\n".join(lines)


async def _appr_card_kb(tid: int, has_resume: bool, total: int, has_history: bool = False, has_full: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"appr_approve:{tid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"appr_reject:{tid}"),
        ],
    ]
    third = []
    if has_resume:
        third.append(InlineKeyboardButton(text="📎 Резюме", callback_data=f"appr_resume:{tid}"))
    if has_history:
        # Phase 21 (21-07, D-15): подпись — из реестра (reg_edit_history_button_label), не
        # литерал в коде; кнопка видна только при непустой истории (get_answer_history limit=1).
        history_label = await get_setting("reg_edit_history_button_label") or "🕓 История"
        third.append(InlineKeyboardButton(text=history_label, callback_data=f"appr_history:{tid}"))
    if has_full:
        # Quick 260902-tzh: карточка не влезла в лимит Telegram (moderation_card.fit_card) —
        # хвост отдаём отдельными сообщениями по appr_full. Кнопка видна только при переполнении.
        third.append(InlineKeyboardButton(text="📄 Полная анкета", callback_data=f"appr_full:{tid}"))
    third.append(InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"appr_skip:{tid}"))
    rows.append(third)
    rows.append([InlineKeyboardButton(text=f"✅ Одобрить все ({total})", callback_data="appr_all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_current_card(target: types.Message, state: FSMContext):
    """Render the oldest non-skipped pending card (DB-driven, restart-safe). Phase 07.2
    (CITY-02): city-scoped through _admin_city_view (_admin_city_scope's single-read form) —
    the admin id comes from state.key.user_id because `target` may be the bot's own message
    (callback.message), whose from_user is the bot, not the admin."""
    admin_id = state.key.user_id
    # WR-05: ONE read — the rows shown and the city named in the header must agree.
    scope, label = await _admin_city_view(admin_id)
    skipped = set((await state.get_data()).get("appr_skipped", []))
    total = await get_pending_count(city_scope=scope)
    offset = 0
    visible: list[dict] = []
    while not visible and offset < total:
        batch = await get_pending_users(limit=50, offset=offset, city_scope=scope)
        if not batch:
            break
        visible = [u for u in batch if u["telegram_id"] not in skipped]
        offset += len(batch)
    if not visible:
        # CR-01: admin-editable label + global HTML parse_mode → escape, or an «<» in the
        # setting makes Telegram reject the message and the empty-queue screen never opens.
        empty_text = (
            "✅ Заявок нет." if label is None
            else f"✅ Заявок нет — «{html_module.escape(str(label))}»."
        )
        await target.answer(empty_text, reply_markup=await admin_keyboard_for(admin_id))
        return
    current = visible[0]
    # M-02: position = how many the admin has already skipped + 1 (the shown card is the first
    # not-yet-skipped pending item). The old total - len(visible) + 1 returned e.g. 51/100 for
    # the first card whenever a full 50-row batch was unskipped. Cap at total for safety.
    position = min(len(skipped) + 1, total)
    # Phase 09.3 (09.3-02, CITY-08): in ALL_CITIES mode the queue holds every city at once — one
    # shared "🌍 Все города" header on every card would tell the manager nothing about WHICH
    # delegate they're approving. Resolve the CARD's own city instead of the header label, only
    # in this one mode; the normal city-selected/module-off cases pass `label` through unchanged.
    card_label = label
    if label == ALL_CITIES_LABEL:
        card_label = await city_label(normalize_city(current.get("event_city")))
    # Phase 21 (21-07, D-14/D-15/D-10): одна выборка истории обслуживает и пометку карточки,
    # и видимость кнопки «🕓 История» — см. докстринг _edit_badges_for.
    edited_line, resubmit_line, has_history = await _edit_badges_for(current)
    # Quick 260902-tzh: набор вопросов и лимит длины ответа — реестром (экран «🧾 Поля
    # карточки заявки»), не девять захардкоженных полей. «resume» — отдельный блок карточки
    # (файлом/текстом/нет), из fields исключается и управляет только show_resume.
    steps = moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))
    answer_limit = await get_setting_typed("modcard_answer_limit")
    fields = moderation_card.card_answers(current, [s for s in steps if s != "resume"], answer_limit)
    card_text, overflow = moderation_card.fit_card(
        _render_application_card(
            current, position, total, city_label_text=card_label,
            consent_line=await consent_card_line(current["telegram_id"]),
            edited_line=edited_line, resubmit_line=resubmit_line,
            fields=fields, show_resume=("resume" in steps),
        )
    )
    await target.answer(
        card_text,
        parse_mode="HTML",
        reply_markup=await _appr_card_kb(
            current["telegram_id"],
            bool(current.get("resume_file_id") or current.get("resume_text")),
            total,
            has_history=has_history,
            has_full=overflow,
        ),
    )


@router.callback_query(F.data == "admin_applications")
async def show_applications(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(appr_skipped=[])  # session-only skip set (D-07)
    await callback.answer()
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_skip:"))
async def appr_skip(callback: types.CallbackQuery, state: FSMContext):
    _, tid = _parse_appr(callback.data)
    data = await state.get_data()
    skipped = list(data.get("appr_skipped", []))
    if tid is not None and tid not in skipped:
        skipped.append(tid)
    await state.update_data(appr_skipped=skipped)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Пропущено")
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_resume:"))
async def appr_resume(callback: types.CallbackQuery):
    _, tid = _parse_appr(callback.data)
    user = await get_user(tid) if tid is not None else None
    if user and user.get("resume_file_id"):
        try:
            await callback.message.answer_document(user["resume_file_id"])
        except Exception as e:
            logger.error(f"Failed to re-send resume for {tid}: {e}")
            await callback.message.answer("Не удалось открыть резюме.")
    elif user and user.get("resume_text"):
        await callback.message.answer(
            f"📄 Резюме (текст):\n\n{html_module.escape(str(user['resume_text']))}",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer("Резюме не приложено.")
    await callback.answer()


@router.callback_query(F.data.startswith("appr_approve:"))
async def appr_approve(callback: types.CallbackQuery, state: FSMContext):
    _, tid = _parse_appr(callback.data)
    # WR-03: карточка могла быть отрисована для другого города (кнопки не истекают).
    if await _card_out_of_scope(callback.from_user.id, tid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    won = await claim_approve(tid) if tid is not None else False
    if won:
        # Хвост (приветствие ровно один раз D-10 + автосинк статуса в таблицу, Таня п.5) —
        # services/application_effects.py, fire-and-forget fail-soft.
        _spawn(apply_decision_effects(callback.bot, tid, "approved"))
        logger.info(f"admin={callback.from_user.id} action=approve user={tid}")
        await callback.answer("Одобрено")
    else:
        await callback.answer("Уже обработано")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_reject:"))
async def appr_reject_start(callback: types.CallbackQuery, state: FSMContext):
    _, tid = _parse_appr(callback.data)
    # WR-03: проверяем ДО запроса причины — иначе менеджер напишет причину впустую.
    if await _card_out_of_scope(callback.from_user.id, tid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    await state.update_data(appr_reject_id=tid)
    await callback.message.answer("Укажи причину отклонения:", reply_markup=get_cancel_kb())
    await state.set_state(Approval.reason)
    await callback.answer()


# WR-03: admin.router is first, so appr_reject_reason (the Approval.reason catch-all below)
# would otherwise SWALLOW any /command typed mid-rejection as the rejection reason — the
# rejection fires with a garbage reason and the command never runs. Catch «Отмена» AND any
# «/...» command here first, aborting the rejection cleanly so the admin can re-issue it.
@router.message(Approval.reason, F.text.in_({"Отмена"}) | F.text.startswith("/"))
async def appr_reject_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    text = (message.text or "").strip()
    if text not in ("Отмена", "/cancel"):
        note = "Отклонение отменено (введена команда). При необходимости повторите её."
    else:
        note = "Отклонение отменено."
    await message.answer(note, reply_markup=ReplyKeyboardRemove())
    await _show_current_card(message, state)


@router.message(Approval.reason)
async def appr_reject_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("appr_reject_id")
    reason = message.text or "-"
    ok = await claim_reject(tid) if tid is not None else False
    if ok:
        # Хвост (сообщение делегату + автосинк статуса в таблицу, Таня п.5) —
        # services/application_effects.py, fire-and-forget fail-soft.
        _spawn(apply_decision_effects(message.bot, tid, "rejected", reason))
        logger.info(f"admin={message.from_user.id} action=reject user={tid} reason={reason!r}")
        await message.answer("Заявка отклонена.", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Заявка уже обработана.", reply_markup=ReplyKeyboardRemove())
    await state.set_state(None)
    await _show_current_card(message, state)


@router.callback_query(F.data == "appr_all")
async def appr_all_confirm(callback: types.CallbackQuery, state: FSMContext):
    # T-072-07 (Repudiation): the confirmation text must name BOTH the city and the count —
    # this is an irreversible mass operation, and a global-looking button at 3 cities means
    # one tap flips a city the admin never chose.
    code = await admin_selected_city(callback.from_user.id)  # None = модуль выключен
    scope = city_scope(code)
    label = None if code is None else await city_label(code)
    total = await get_pending_count(city_scope=scope)
    if total == 0:
        await callback.answer("Заявок нет")
        await _show_current_card(callback.message, state)
        return
    # CR-02: «Да» обязана подтверждать ИМЕННО тот город, который назван в тексте выше.
    # Код города едет в callback_data, и appr_all_yes сверяет его с текущим выбором —
    # иначе переключение города в соседнем сообщении (выбор живёт в bot_settings и
    # переживает перезапуск, кнопки не истекают) необратимо одобряет чужую очередь.
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"appr_all_yes:{code or ''}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="appr_all_no"),
    ]])
    # CR-01: escape the admin-editable label — this screen is the LAST thing shown before an
    # irreversible mass approval; a broken parse here means it cannot be opened at all.
    # Phase 09.3 (CITY-08, T-093-10): THREE branches now, not two — `label` is non-None both
    # for a real city AND for ALL_CITIES mode (city_label("*") -> ALL_CITIES_LABEL, plan 01),
    # so a plain `label is None` ternary would silently print the per-city phrasing on a
    # cross-city mass approval. The ALL_CITIES branch must honestly say "по всем городам" —
    # this IS the irreversible-scope disclosure T-093-10/T-093-11 rely on.
    if label is None:
        text = f"Одобрить все {total} заявок?"
    elif label == ALL_CITIES_LABEL:
        text = (
            f"Одобрить все {total} заявок по всем городам? "
            "Будут затронуты заявки всех городов."
        )
    else:
        text = (
            f"Одобрить все {total} заявок в городе «{html_module.escape(str(label))}»? "
            "Заявки других городов не будут затронуты."
        )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "appr_all_no")
async def appr_all_no(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Отменено")
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_all_yes"))
async def appr_all_yes(callback: types.CallbackQuery, state: FSMContext):
    # CR-02: массовое одобрение необратимо, поэтому оно fail-closed. Город берётся из
    # callback_data (тот, что назван в тексте подтверждения), а не из текущего выбора
    # админа, и должен ему СОВПАДАТЬ. Если админ переключил город после показа диалога —
    # отказываем и просим подтвердить заново, а не одобряем «то, что выбрано сейчас».
    # Старая (до CR-02) кнопка без двоеточия даёт confirmed=None: при включённом модуле
    # это гарантированно не совпадёт с выбранным городом и будет отвергнуто; при
    # выключенном модуле current тоже None — это и есть путь module-off (скоуп None).
    raw = callback.data.split(":", 1)[1].strip() if ":" in callback.data else ""
    confirmed = raw or None
    current = await admin_selected_city(callback.from_user.id)
    if confirmed != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        await _show_current_card(callback.message, state)
        return
    # Phase 09.3 (CITY-08, Pitfall 1 / T-093-10): "*" is the ALL_CITIES marker, not a member
    # of the closed city registry — without this exception every confirmed ALL_CITIES mass
    # approval would hit "Неизвестный город" here even though the roundtrip check above just
    # passed. `city_scope(confirmed)` below already resolves "*" to None (no filter, plan 01);
    # only THIS membership guard needed the extra branch.
    if confirmed is not None and confirmed != ALL_CITIES and confirmed not in city_codes():
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-072-03/T-072-07: city condition lives in the WHERE of this SAME atomic
    # UPDATE ... RETURNING — structurally cannot flip another city's rows.
    ids = await approve_all_pending(city_scope=city_scope(confirmed))  # atomic flip first (D-11)
    # WR-04: a stale confirm dialog re-clicked (buttons never expire) hits approve_all_pending
    # again — atomic, so it returns [] the second time. Don't run the drain or claim a count;
    # tell the admin it's already done and refresh the card.
    if not ids:
        try:
            await callback.message.edit_text("Заявки уже обработаны.", reply_markup=await admin_keyboard_for(callback.from_user.id))
        except Exception:
            pass
        await callback.answer("Уже обработано")
        await _show_current_card(callback.message, state)
        return
    # WR-01: schedule the welcome drain (and status sync) BEFORE the fragile edit_text. Inline
    # buttons never expire, but Telegram rejects editing a message >48h old, and the card may
    # have been deleted — if the edit threw first, the N just-approved users would be left
    # `approved` in DB with no welcome/menu/payment requisites (violates D-11 "welcome exactly
    # once"). Ordering the background sends first makes delivery independent of the edit.
    # services/application_effects.py::mass_approve_effects — welcome drain + один batch-sync
    # в лист (Таня п.5), fire-and-forget fail-soft.
    _spawn(mass_approve_effects(callback.bot, ids))
    try:
        await callback.message.edit_text(
            f"✅ Одобрено: {len(ids)}. Рассылаю приветствия…",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )
    except Exception as e:
        logger.warning(f"appr_all_yes: confirm edit failed (welcome drain already scheduled): {e}")
    await callback.answer()


# ── Phase 4: receipt verification queue ("Чеки", tinder UI, D-12) ─────────────

def _parse_rcpt(data: str) -> tuple[str, int | None]:
    """'rcpt_confirm:123' -> ('rcpt_confirm', 123)."""
    if ":" in data:
        prefix, uid = data.split(":", 1)
        try:
            return prefix, int(uid)
        except ValueError:
            return prefix, None
    return data, None


def _render_receipt_card(user: dict, position: int, total: int, city_label_text: str | None = None) -> str:
    """Phase 07.2 (CITY-02): same header-suffix convention as _render_application_card —
    `city_label_text` appends «· 🏙 {label}» only when an admin city is selected; None keeps
    the header byte-identical to the pre-CITY-02 line."""
    header = f"🧾 <b>Чек {position}/{total}</b>"
    if city_label_text is not None:
        header += f" · 🏙 {html_module.escape(str(city_label_text))}"
    lines = [header, ""]
    lines.append(f"👤 {html_module.escape(str(user.get('full_name') or '—'))}")
    lines.append(f"💳 Вариант: {html_module.escape(str(user.get('payment_option') or '—'))}")
    lines.append(f"📎 Чек: {'загружен' if user.get('receipt_file_id') else 'нет'}")
    return "\n".join(lines)


def _rcpt_card_kb(uid: int, has_receipt: bool, total: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"rcpt_confirm:{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rcpt_reject:{uid}"),
    ]]
    third = []
    if has_receipt:
        third.append(InlineKeyboardButton(text="🧾 Чек", callback_data=f"rcpt_view:{uid}"))
    third.append(InlineKeyboardButton(text="⏭ Следующий", callback_data=f"rcpt_skip:{uid}"))
    rows.append(third)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_current_receipt_card(target: types.Message, state: FSMContext):
    """Phase 07.2 (CITY-02): the SECOND moderation queue — reads the selected city through the
    same _admin_city_view / _admin_city_scope resolver as _show_current_card. If a third queue
    is ever added it plugs in here too (see the comment anchor next to _admin_city_view)."""
    admin_id = state.key.user_id
    # WR-05: ONE read — the rows shown and the city named in the header must agree.
    scope, label = await _admin_city_view(admin_id)
    skipped = set((await state.get_data()).get("rcpt_skipped", []))
    total = await get_receipt_pending_count(city_scope=scope)
    offset = 0
    visible: list[dict] = []
    while not visible and offset < total:
        batch = await get_receipt_pending_users(limit=50, offset=offset, city_scope=scope)
        if not batch:
            break
        visible = [u for u in batch if u["telegram_id"] not in skipped]
        offset += len(batch)
    if not visible:
        # CR-01: same escaping as the applications queue above.
        empty_text = (
            "✅ Чеков на проверке нет." if label is None
            else f"✅ Чеков на проверке нет — «{html_module.escape(str(label))}»."
        )
        await target.answer(empty_text, reply_markup=await admin_keyboard_for(admin_id))
        return
    current = visible[0]
    # M-02: position = skipped-so-far + 1 (the shown card is the first not-yet-skipped receipt).
    # The old total - len(visible) + 1 returned e.g. 51/100 for the first card on a >50 queue.
    position = min(len(skipped) + 1, total)
    # Phase 09.3 (09.3-02, CITY-08): same per-card resolve as the applications queue above — in
    # ALL_CITIES mode the shared header label would misname every card but its own delegate.
    card_label = label
    if label == ALL_CITIES_LABEL:
        card_label = await city_label(normalize_city(current.get("event_city")))
    await target.answer(
        _render_receipt_card(current, position, total, city_label_text=card_label),
        parse_mode="HTML",
        reply_markup=_rcpt_card_kb(current["telegram_id"], bool(current.get("receipt_file_id")), total),
    )


@router.callback_query(F.data == "admin_receipts")
async def show_receipts(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(rcpt_skipped=[])
    await callback.answer()
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_confirm:"))
async def rcpt_confirm(callback: types.CallbackQuery, state: FSMContext):
    _, uid = _parse_rcpt(callback.data)
    # WR-03: та же проверка, что и в очереди заявок — карточка чека тоже не истекает.
    if await _card_out_of_scope(callback.from_user.id, uid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    rows = await update_payment_status(uid, "paid") if uid is not None else 0
    if rows == 0:
        # Atomic guard (T-04-05-02): another manager already confirmed.
        await callback.answer("Чек уже обработан.")
        await _show_current_receipt_card(callback.message, state)
        return
    # H-01: disable this card's buttons now that it's confirmed, so scrolling back and
    # tapping ❌ Отклонить on it can't fire a stale reject (the db guard also blocks it).
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    from services.scheduler import cancel_payment_reminders
    cancel_payment_reminders(uid)  # cancel BEFORE notifying — no reminder after paid
    try:
        await callback.bot.send_message(
            uid,
            "✅ <b>Оплата подтверждена!</b>\n\nСпасибо, ваш взнос получен.",
            parse_mode="HTML",
            reply_markup=await get_main_menu_kb(uid),  # first menu after the payment journey
        )
        # WR-04: payment-confirm must mirror the non-payment approval path — deliver the
        # configured completion text + registration bonus. Menu already sent above.
        # WR-01: resolve the track before calling send_completion_and_bonus, mirroring the
        # pattern in approve_user (registration.py) — this is the primary PAID party path, so
        # without this a paying party delegate always got the full-track approve_text.
        from handlers.registration import send_completion_and_bonus
        try:
            user_row = await get_user(uid)
            participant_type = (user_row or {}).get("participant_type") or "full"
        except Exception as e2:
            logger.error(f"rcpt_confirm: failed to resolve participant_type for {uid}, defaulting to 'full': {e2}")
            participant_type = "full"
        await send_completion_and_bonus(callback.bot, uid, with_menu=False, participant_type=participant_type)
    except Exception as e:
        logger.error(f"Failed to notify user {uid} of payment confirmation: {e}")
    await callback.answer("Оплата подтверждена")
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_reject:"))
async def rcpt_reject_start(callback: types.CallbackQuery, state: FSMContext):
    _, uid = _parse_rcpt(callback.data)
    # WR-03: проверяем ДО запроса причины.
    if await _card_out_of_scope(callback.from_user.id, uid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    await state.update_data(rcpt_reject_uid=uid)
    await state.set_state(ReceiptReview.reject_reason)
    await callback.message.answer("Укажи причину отклонения (или «-» без объяснений):", reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(ReceiptReview.reject_reason, F.text.in_({"Отмена", "/cancel"}))
async def rcpt_reject_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отклонение отменено.", reply_markup=ReplyKeyboardRemove())
    await _show_current_receipt_card(message, state)


@router.message(ReceiptReview.reject_reason)
async def rcpt_reject_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("rcpt_reject_uid")
    if uid is not None:
        # H-01: guard the reset — a stale/already-confirmed card tapped ❌ must NOT flip a
        # 'paid' user back to 'not_paid'. Only a row still in 'receipt_sent' is rejectable.
        rows = await update_payment_status(uid, "not_paid", require_status="receipt_sent")
        if rows == 0:
            await state.set_state(None)
            await message.answer(
                "Чек уже обработан (оплата подтверждена или чек не в очереди) — отклонение пропущено.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await _show_current_receipt_card(message, state)
            return
        reason_text = (message.text or "").strip()
        user_msg = "❌ Чек отклонён."
        if reason_text and reason_text != "-":
            user_msg += f" Причина: {html_module.escape(reason_text)}"
        user_msg += "\n\nЗагрузи чек повторно через бота."
        try:
            await message.bot.send_message(uid, user_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify user {uid} of receipt rejection: {e}")
    await state.set_state(None)
    await message.answer("Готово.", reply_markup=ReplyKeyboardRemove())
    await _show_current_receipt_card(message, state)


@router.callback_query(F.data.startswith("rcpt_skip:"))
async def rcpt_skip(callback: types.CallbackQuery, state: FSMContext):
    _, uid = _parse_rcpt(callback.data)
    data = await state.get_data()
    skipped = list(data.get("rcpt_skipped", []))
    if uid is not None and uid not in skipped:
        skipped.append(uid)
    await state.update_data(rcpt_skipped=skipped)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_view:"))
async def rcpt_view(callback: types.CallbackQuery):
    _, uid = _parse_rcpt(callback.data)
    user = await get_user(uid) if uid is not None else None
    if user and user.get("receipt_file_id"):
        file_id = user["receipt_file_id"]
        try:
            await callback.message.answer_document(file_id, caption=f"Чек пользователя {uid}")
        except Exception:
            # receipt may be a photo file_id, not a document — fall back to photo send
            try:
                await callback.message.answer_photo(file_id, caption=f"Чек пользователя {uid}")
            except Exception as e:
                logger.error(f"Failed to show receipt for {uid}: {e}")
                await callback.answer("Не удалось открыть чек.", show_alert=True)
                return
        await callback.answer()
    else:
        await callback.answer("Чек не найден.", show_alert=True)


# ── Phase 21 (21-07, FORM-SYNC-04, D-15): «🕓 История» — правки анкеты для менеджера ──────────

@router.callback_query(F.data.startswith("appr_history:"))
async def appr_history(callback: types.CallbackQuery):
    """Человекочитаемый список «было → стало» по reg_answer_history — без похода в БД и без
    разработчика. T-21-22 (Info Disclosure): в лог идёт только telegram_id и число записей,
    сами значения changes не логируются."""
    _, tid = _parse_appr(callback.data)
    if tid is None:
        await callback.answer()
        return
    rows = await get_answer_history(tid, limit=5)
    logger.info(f"admin={callback.from_user.id} action=view_edit_history user={tid} count={len(rows)}")
    if not rows:
        await callback.message.answer("🕓 Правок пока нет.")
        await callback.answer()
        return
    lines = ["🕓 <b>История правок</b>", ""]
    for row in rows:
        when = _format_edited_date(row.get("changed_at"))
        src = _EDITED_SOURCE_LABELS.get(row.get("source"), html_module.escape(str(row.get("source") or "")))
        for ch in row.get("changes") or []:
            column = ch.get("column")
            if column == "status":
                # маркер повторной подачи (D-10) — уже показан отдельной строкой в карточке,
                # это не поле анкеты, в списке «было → стало» не дублируем.
                continue
            label = html_module.escape(str(_COLUMN_TO_LABEL.get(column, column)))
            old = html_module.escape(str(ch.get("old"))) if ch.get("old") not in (None, "") else "—"
            new = html_module.escape(str(ch.get("new"))) if ch.get("new") not in (None, "") else "—"
            lines.append(f"{label}: {old} → {new} ({when}, {src})")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")
    await callback.answer()


# ── Quick 260902-tzh: «📄 Полная анкета» — хвост карточки, не влезающий в лимит Telegram ──────

@router.callback_query(F.data.startswith("appr_full:"))
async def appr_full(callback: types.CallbackQuery):
    """Все включённые вопросы БЕЗ обрезки ответа, отдельными сообщениями (карточка сама
    обрывается по лимиту Telegram — moderation_card.fit_card). WR-03: тот же гейт чужого
    города, что у appr_approve — кнопки карточки не истекают. В лог — только telegram_id
    (T-21-22): значения ответов не логируются."""
    _, tid = _parse_appr(callback.data)
    if tid is None:
        await callback.answer()
        return
    if await _card_out_of_scope(callback.from_user.id, tid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    user = await get_user(tid)
    if not user:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    steps = moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))
    fields = moderation_card.card_answers(user, [s for s in steps if s != "resume"], None)
    lines = ["📋 <b>Полная анкета</b>", ""]
    for label, value in fields:
        lines.append(f"{label}: {html_module.escape(value)}")
    logger.info(f"admin={callback.from_user.id} action=view_full_card user={tid}")
    for chunk in moderation_card.split_for_telegram("\n".join(lines)):
        await callback.message.answer(chunk, parse_mode="HTML")
    await callback.answer()


# Quick 260902-tzh: handlers/admin_modcard.py (экран «🧾 Поля карточки заявки» — тумблеры
# вопросов + пресеты лимита) decorates the same admin.router. Imported LAST, right after
# every handler above, so its handlers land right after this module at any import order —
# same seam-import technique as admin_gamification/admin_polls at the tail of admin.py.
# Golden snapshot: tests/test_refac_snapshot_260816.py.
from handlers import admin_modcard  # noqa: E402,F401
