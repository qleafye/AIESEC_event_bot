"""Раздел «🤝 Амбассадоры» → «🙋 Кандидаты и команда»: массовые действия (право moderate_game).

- «🙅 Вежливо отказать всем оставшимся (N)» — когда состав финальный. Сначала подтверждение:
  скольким уйдёт, какой текст (`amb_decline_all_text` из реестра) и что будет после. Число
  на кнопке «✅ Отправить N» сверяется перед выполнением: изменилось — ничего не шлём и
  показываем новое. Рассылка идёт фоном через тихие часы, отметка «письмо ушло» ставится ДО
  отправки (`claim_decline_notice`) — повтор досылает только тем, кому ещё не ушло. В конце
  менеджеру приходит отчёт «отправлено / отложено до утра / не доставлено».
- «➕ Назначить амбассадором» — любого делегата, подавшего анкету: @ник, ссылка t.me,
  Telegram ID или пересланное сообщение → подтверждение с именем, городом, статусом заявки и
  текстом, который ему придёт → то же, что «✅ Взять» (`take_and_notify`).
- «📥 Прошлые сезоны (CSV)» — архив статусов, который «🔄 Новый сезон» откладывает перед
  сбросом (`season_reset_line` / `season_reset_apply` зовёт мастер сезона в admin_cities).

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/admin_amb_candidates.py`.
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from cities import city_label_or_none
from database import amb_status_db
from database import db as _db
from handlers.admin import router
from handlers.admin_amb_candidates import (
    AMB_STATUS_LABELS,
    APP_STATUS_LABELS,
    _alert,
    _edit_or_send,
    _name,
    _nick,
    _notice_suffix,
    _notify,
    _now,
    _take_alert,
    render_list,
    render_person,
    take_and_notify,
)
from handlers.admin_caps import has_capability
from handlers.states import AmbAppoint
from services import amb_status, person_search
from services.background import spawn
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

_PAUSE = 0.05  # между сообщениями рассылки — не упираться в лимит Telegram

_NO_CANDIDATES = "Кандидатов нет — отказывать некому."
_ALL_CITIES_ONLY = (
    "Отказ всем действует на кандидатов всех городов. Выберите в админке «Все города», "
    "чтобы увидеть полное число, и нажмите ещё раз."
)


async def bulk_buttons(scope) -> list[list[InlineKeyboardButton]]:
    """Кнопки массовых действий под списком «🙋 Кандидаты и команда»."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Назначить амбассадором", callback_data="ambc_add")],
    ]
    if scope is None:
        n = await amb_status_db.count_by_filter("candidates")
        if n:
            rows.append([InlineKeyboardButton(
                text=f"🙅 Вежливо отказать всем оставшимся ({n})", callback_data="ambc_decl")])
        else:
            pending = len(await amb_status_db.declined_pending_notice())
            if pending:
                rows.append([InlineKeyboardButton(
                    text=f"🙅 Дослать отказ ({pending})", callback_data="ambc_decl")])
    rows.append([InlineKeyboardButton(text="📥 Прошлые сезоны (CSV)", callback_data="ambc_arch_csv")])
    if scope is None:
        rows.append([InlineKeyboardButton(text="🧹 Сбросить статусы", callback_data="ambrst")])
    return rows


# ── вежливый отказ всем оставшимся ───────────────────────────────────────────────────────

async def _decline_text() -> str:
    return html.escape(await get_setting_typed("amb_decline_all_text") or "")


async def _decline_confirm(admin_id: int, n: int, pending: int,
                           prefix: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Экран подтверждения: n кандидатов (или досылка pending отказанным при n = 0)."""
    text_line = f"Текст: «{await _decline_text()}»"
    if n:
        text = (
            f"{prefix}<b>🙅 Вежливо отказать всем оставшимся?</b>\n\n"
            f"Уйдёт {n} кандидатам (включая отложенных «в запасе»). {text_line}.\n\n"
            "После этого кнопки «Хочу свою ссылку» у них не будет в этом сезоне. "
            "Вернуть человека можно: «🙋 Кандидаты и команда» → «Отказано» → «Взять».\n\n"
            "Сообщения уходят с учётом тихих часов; когда закончу — пришлю отчёт."
        )
        go = f"✅ Отправить {n}"
    else:
        text = (
            f"{prefix}<b>🙅 Дослать отказ?</b>\n\n"
            f"Кандидатов не осталось, но {pending} отказанным сообщение ещё не ушло (например, "
            "бот перезапускался во время рассылки). Уйдёт только им, повторно никому. "
            f"{text_line}."
        )
        go = f"✅ Дослать {pending}"
    rows = [[InlineKeyboardButton(text=go, callback_data=f"ambc_decl_go:{n}")]]
    if await has_capability(admin_id, "settings"):
        rows.append([InlineKeyboardButton(
            text="✏️ Изменить текст", callback_data="settings_edit:amb_decline_all_text")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ambc_decl_no")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _scope(admin_id: int):
    from handlers.admin_core import _admin_city_view  # ленивый шов, как у экранов заявок

    scope, _label = await _admin_city_view(admin_id)
    return scope


@router.callback_query(F.data == "ambc_decl")
async def decline_all_confirm(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if await _scope(admin_id) is not None:
        await callback.answer(_ALL_CITIES_ONLY, show_alert=True)
        return
    n = await amb_status_db.count_by_filter("candidates")
    pending = 0 if n else len(await amb_status_db.declined_pending_notice())
    if not n and not pending:
        await callback.answer(_NO_CANDIDATES, show_alert=True)
        return
    await _edit_or_send(callback.message, *await _decline_confirm(admin_id, n, pending))
    await callback.answer()


@router.callback_query(F.data == "ambc_decl_no")
async def decline_all_cancel(callback: types.CallbackQuery):
    text, kb = await render_list(callback.from_user.id, "candidates", 0)
    await _edit_or_send(callback.message, text, kb)
    await callback.answer("Отменено — никому ничего не ушло.")


@router.callback_query(F.data.startswith("ambc_decl_go:"))
async def decline_all_go(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if await _scope(admin_id) is not None:
        await callback.answer(_ALL_CITIES_ONLY, show_alert=True)
        return
    try:
        expected = int(callback.data.split(":", 1)[1])
    except ValueError:
        expected = -1
    n = await amb_status_db.count_by_filter("candidates")
    pending = len(await amb_status_db.declined_pending_notice())
    if not n and not pending:
        await callback.answer(_NO_CANDIDATES, show_alert=True)
        text, kb = await render_list(admin_id, "candidates", 0)
        await _edit_or_send(callback.message, text, kb)
        return
    if n != expected:
        prefix = (f"⚠️ Число кандидатов изменилось: теперь {n}. "
                  "Проверьте и подтвердите ещё раз.\n\n")
        await _edit_or_send(callback.message, *await _decline_confirm(admin_id, n, pending, prefix))
        await callback.answer()
        return
    ids = await amb_status_db.decline_remaining(at=_now(), by=admin_id)
    to_send = len(await amb_status_db.declined_pending_notice())
    logger.info("admin=%s amb_decline_all n=%s to_send=%s", admin_id, len(ids), to_send)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="← К списку", callback_data="ambc:declined:0")]])
    await _edit_or_send(
        callback.message,
        f"🙅 Отказано кандидатам: {len(ids)}. Отправляю сообщение ({to_send}) — "
        "когда закончу, пришлю отчёт.",
        kb,
    )
    await callback.answer()
    spawn(notify_declined(callback.bot, admin_id))


async def notify_declined(bot, admin_id: int) -> tuple[int, int, int]:
    """Обходит ВСЕХ отказанных без отметки (не только отказанных этим нажатием — так повтор
    досылает хвост после рестарта). Отметка до отправки: вторая вкладка / повтор второе письмо
    не шлют. Ошибка одному не останавливает остальных. -> (отправлено, отложено, не доставлено)."""
    sent = deferred = failed = 0
    try:
        for tid in await amb_status_db.declined_pending_notice():
            if not await amb_status_db.claim_decline_notice(tid, at=_now()):
                continue
            result = await _notify(bot, tid, "amb_decline_all_text")
            if result is True:
                sent += 1
            elif result is False:
                deferred += 1
            else:
                failed += 1
            await asyncio.sleep(_PAUSE)
    except Exception:
        logger.exception("amb_decline_all: рассылка прервана (admin=%s)", admin_id)
    logger.info("admin=%s amb_decline_all_done sent=%s deferred=%s failed=%s",
                admin_id, sent, deferred, failed)
    report = (f"🙅 Отказ кандидатам. Готово: отправлено {sent}, отложено до утра {deferred}, "
              f"не доставлено {failed}.")
    if failed:
        report += "\nНе доставлено — скорее всего, эти люди заблокировали бота."
    try:
        await bot.send_message(admin_id, report)
    except Exception:
        logger.exception("amb_decline_all: отчёт менеджеру не доставлен (admin=%s)", admin_id)
    return sent, deferred, failed


# ── назначить любого делегата ────────────────────────────────────────────────────────────

_APPOINT_PROMPT = (
    "➕ <b>Назначить амбассадором</b>\n\n"
    "Пришлите @ник, ссылку t.me, Telegram ID или перешлите сообщение делегата. "
    "«❌ Отмена» — выйти."
)
_NOT_FOUND = (
    "Не нашёл такого делегата. Проверьте ник или перешлите его сообщение. "
    "Назначить можно только того, кто подал анкету."
)
_HIDDEN_FORWARD = (
    "В этой пересылке не видно аккаунта человека — у него скрыт аккаунт при пересылке. "
    "Пришлите его @ник или Telegram ID."
)
_ALREADY = "Он уже в команде."
_CANCELLED = "Отменено — никого не назначили."
_PICK_MAX = 10


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="ambc_add_cancel")]])


def _forwarded_id(message) -> tuple[int | None, bool]:
    """(id, это была пересылка). Пересылка со скрытым аккаунтом -> (None, True).
    `forward_origin` первым — старые поля Bot API 7.0 больше не присылает."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        return (sender.id if sender is not None else None), True
    forwarded = getattr(message, "forward_from", None)
    if forwarded is not None:
        return forwarded.id, True
    return None, False


async def _find(message, scope) -> list[dict] | str:
    """Кого можно назначить (строки users в городе админа) или текст ошибки."""
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


async def _appoint_confirm(tid: int, scope) -> tuple[str, InlineKeyboardMarkup] | str:
    """Экран подтверждения назначения или текст отказа (не найден / уже в команде)."""
    user = await _db.get_user(tid)
    if not user or not person_search._city_matches(user.get("event_city"), scope):
        return _NOT_FOUND
    st = await amb_status_db.get_status(tid) or {}
    if st.get("status") == "active":
        return _ALREADY
    bits = [_name(user)]
    nick = _nick(user)
    if nick:
        bits.append(nick)
    city = await city_label_or_none(user.get("event_city"))
    if city:
        bits.append(html.escape(city))
    bits.append("заявка: " + APP_STATUS_LABELS.get(user.get("status") or "", "—"))
    now = AMB_STATUS_LABELS.get(st.get("status") or "none", "—")
    taken = html.escape((await get_setting_typed("amb_taken_text") or "")
                        .replace("{link}", "[его ссылка для приглашений]"))
    text = (
        f"<b>Сделать амбассадором:</b> {' · '.join(bits)}?\n"
        + (f"Сейчас: {now}.\n" if now != "—" else "")
        + f"\nЕму придёт сообщение «{taken}».\n\n"
        "Место в команде выдаётся, если заявка одобрена и места есть; иначе — «без пакета»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Назначить", callback_data=f"ambc_add_go:{tid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ambc_add_cancel")],
    ])
    return text, kb


@router.callback_query(F.data == "ambc_add")
async def appoint_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AmbAppoint.waiting_for_person)
    await callback.message.answer(_APPOINT_PROMPT, parse_mode="HTML", reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "ambc_add_cancel")
async def appoint_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_list(callback.from_user.id, "candidates", 0)
    await _edit_or_send(callback.message, text, kb)
    await callback.answer(_CANCELLED)


@router.message(AmbAppoint.waiting_for_person)
async def appoint_person_step(message: types.Message, state: FSMContext):
    body = (message.text or "").strip()
    if body.startswith("/") or body.lower() in {"отмена", "❌ отмена"}:
        await state.clear()
        await message.answer(_CANCELLED)
        return
    scope = await _scope(message.from_user.id)
    found = await _find(message, scope)
    if isinstance(found, str):
        await message.answer(found, reply_markup=_cancel_kb())
        return
    if len(found) == 1:
        screen = await _appoint_confirm(int(found[0]["user_id"]), scope)
        if isinstance(screen, str):
            await message.answer(screen, reply_markup=_cancel_kb())
            return
        await message.answer(screen[0], parse_mode="HTML", reply_markup=screen[1])
        return
    rows = []
    for person in found[:_PICK_MAX]:
        label = str(person.get("full_name") or "").strip() or "без имени"
        city = await city_label_or_none(person.get("city"))
        if city:
            label += f" · {city}"
        rows.append([InlineKeyboardButton(text=label[:60],
                                          callback_data=f"ambc_add_pick:{person['user_id']}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ambc_add_cancel")])
    more = (f"\nПоказаны первые {_PICK_MAX} — уточните запрос, если нужного нет."
            if len(found) > _PICK_MAX else "")
    await message.answer(f"Нашлось несколько делегатов — выберите нужного.{more}",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def _tid(data: str) -> int | None:
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


@router.callback_query(F.data.startswith("ambc_add_pick:"))
async def appoint_pick(callback: types.CallbackQuery):
    tid = _tid(callback.data)
    screen = (await _appoint_confirm(tid, await _scope(callback.from_user.id))
              if tid is not None else _NOT_FOUND)
    if isinstance(screen, str):
        await callback.answer(_alert(screen), show_alert=True)
        return
    await _edit_or_send(callback.message, *screen)
    await callback.answer()


@router.callback_query(F.data.startswith("ambc_add_go:"))
async def appoint_go(callback: types.CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    tid = _tid(callback.data)
    user = await _db.get_user(tid) if tid is not None else None
    if not user or not person_search._city_matches(user.get("event_city"), await _scope(admin_id)):
        await callback.answer(_alert(_NOT_FOUND), show_alert=True)
        return
    result, sent = await take_and_notify(callback.bot, admin_id, tid)
    if result.outcome == "already_active":
        await callback.answer(_ALREADY, show_alert=True)
    elif result.outcome != "taken":
        await callback.answer(_alert(_NOT_FOUND), show_alert=True)
        return
    else:
        logger.info("admin=%s amb_appoint tid=%s", admin_id, tid)
        await callback.answer(_alert(await _take_alert(tid, result.slot) + _notice_suffix(sent)),
                              show_alert=True)
    await state.clear()
    screen = await render_person(admin_id, tid, "team", 0)
    if screen is not None:
        await _edit_or_send(callback.message, *screen)


# ── прошлые сезоны: архив и сброс ────────────────────────────────────────────────────────

ARCHIVE_HEADERS = ["Сезон", "Имя", "username", "Город", "Статус", "Вступил", "Было место",
                   "Пакет выдан", "Telegram ID"]


async def export_archive_csv(city_scope=None) -> tuple[bytes, int]:
    """Архив прошлых сезонов: `;`, utf-8-sig, ник без «@», строки через `_csv_safe`."""
    safe = _db._csv_safe
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(ARCHIVE_HEADERS)
    count = 0
    for row in await amb_status_db.export_archive_rows():
        if not person_search._city_matches(row.get("event_city"), city_scope):
            continue
        count += 1
        writer.writerow([
            safe(str(row.get("season") or "")),
            safe(str(row.get("full_name") or "")),
            safe(str(row.get("username") or "").strip().lstrip("@")),
            safe(await city_label_or_none(row.get("event_city")) or ""),
            AMB_STATUS_LABELS.get(row.get("status") or "none", "—"),
            safe(str(row.get("since") or "")),
            "да" if row.get("slot_at") else "нет",
            safe(str(row.get("pack_at") or "")),
            int(row["telegram_id"]),
        ])
    return output.getvalue().encode("utf-8-sig"), count


@router.callback_query(F.data == "ambc_arch_csv")
async def archive_csv(callback: types.CallbackQuery):
    data, count = await export_archive_csv(await _scope(callback.from_user.id))
    if not count:
        await callback.answer("Прошлых сезонов пока нет.", show_alert=True)
        return
    logger.info("admin=%s amb_archive_csv rows=%s", callback.from_user.id, count)
    await callback.message.answer_document(
        BufferedInputFile(data, filename="ambassadors_past_seasons.csv"),
        caption=f"Амбассадоры прошлых сезонов: {count} записей",
    )
    await callback.answer()


async def season_reset_line() -> str:
    """Строка экрана чисел мастера «🔄 Новый сезон». Сбой чтения — пустая строка: мастер
    сезона не должен падать из-за амбассадоров. Модуль отбора выключен — строки нет: статусы
    не сбрасываются."""
    try:
        if not await amb_status.selection_enabled():
            return ""
        n = await amb_status_db.count_with_status()
    except Exception:
        logger.error("season_reset: не прочитал число амбассадоров", exc_info=True)
        return ""
    if not n:
        return ""
    return (f"• Сбросятся статусы амбассадоров: {n} (история останется в выгрузке "
            "«Прошлые сезоны»).\n")


async def season_reset_apply(old_season: str) -> str:
    """Сброс статусов с архивом прошлого сезона; строка итога для мастера. Сбой — лог и
    честная строка, сезон всё равно меняется. Модуль отбора выключен — статусы не трогаются."""
    try:
        if not await amb_status.selection_enabled():
            return ""
        n = await amb_status_db.archive_and_reset_season(old_season or "", at=_now())
    except Exception:
        logger.error("season_reset: статусы амбассадоров не сброшены (old=%r)", old_season,
                     exc_info=True)
        return "\n⚠️ Статусы амбассадоров сбросить не удалось — напишите разработчику."
    logger.warning("season_reset: amb statuses archived and reset n=%s old=%r", n, old_season)
    return f"\nСтатусы амбассадоров сброшены: {n}." if n else ""


# «Закрепить приглашённого» (admin_amb_journal: admin_amb_attach, ambj_*) - хвост admin.router.
from handlers import admin_amb_journal  # noqa: E402,F401
