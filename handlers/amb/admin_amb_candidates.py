"""Раздел «🤝 Амбассадоры» → экран «🙋 Кандидаты и команда» (право moderate_game).

Менеджер за пару дней выбирает команду из желающих:
- список постранично (не сообщение на человека), фильтры кнопками «Кандидаты / В команде /
  Без пакета / Отказано», сверху «Занято мест: 12 из 17», в строке — имя, ник, город, статус
  заявки на форум, «привёл N / прошли M» и метка «⏸ в запасе»;
- карточка человека: «✅ Взять» (кандидата, отказанного, вышедшего), «⏸ Не сейчас» (ничего
  не пишет, оставляет в запасе), «🎁 Пакет выдан», «🎟 Дать место», «🚪 Вывести из команды»
  (через подтверждение, где сказано, что будет с местом и какое сообщение придёт человеку),
  «🧾 Анкета» — та же карточка, что в очереди заявок (право moderate_reg);
- выгрузка CSV всех со статусом амбассадора.

Правила входа и места — `services/amb/amb_status.py` и `database/amb_status_db.py`; здесь только
экран и сообщения человеку. Тексты человеку — ключи реестра `amb_taken_text` /
`amb_removed_text` на его языке, через тихие часы (`quiet_hours.send_or_queue_text`).

«Привёл / прошли отбор» — `amb_tiers_db.referral_counts_bulk` одним запросом на страницу.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/amb/admin_amb_section.py`.
"""
from __future__ import annotations

import csv
import html
import io
import logging

from aiogram import F, types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import city_label_or_none
from database import amb_status_db, amb_tiers_db
from database import db as _db
from handlers.admin import router
from handlers.access.admin_caps import has_capability
from services.amb import amb_status, amb_tiers
from services.infra.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

PAGE = 10
_ALERT_MAX = 200
_STAMP = "%Y-%m-%d %H:%M:%S"

FILTER_LABELS = {
    "candidates": "🙋 Кандидаты",
    "team": "🤝 В команде",
    "no_pack": "📭 Без пакета",
    "declined": "🚫 Отказано",
}
_EMPTY = {
    "candidates": (
        "Кандидатов пока нет. Они появятся, когда делегаты нажмут «Хочу свою ссылку» "
        "или ответят «да» в анкете."
    ),
    "team": "В команде пока никого. Возьмите людей из списка «Кандидаты».",
    "no_pack": "Без пакета никого: у всех в команде есть место.",
    "declined": "Отказанных нет.",
}
APP_STATUS_LABELS = {"pending": "на рассмотрении", "approved": "одобрена", "rejected": "отклонена"}
AMB_STATUS_LABELS = {
    "candidate": "кандидат",
    "active": "в команде",
    "left": "вышел",
    "declined": "отказано",
    "none": "—",
}
_ZERO = {"total": 0, "pending": 0, "qualified": 0, "arrived": 0}
_STALE = "Человек не найден — обновите список."


def _now() -> str:
    return msk_now().strftime(_STAMP)


def _alert(text: str) -> str:
    return text if len(text) <= _ALERT_MAX else text[:_ALERT_MAX - 1].rstrip() + "…"


def _name(row: dict) -> str:
    return html.escape(str(row.get("full_name") or "").strip() or "без имени")


def _nick(row: dict) -> str | None:
    raw = str(row.get("username") or "").strip().lstrip("@")
    return html.escape(raw) if raw else None


def _slots_line(taken: int, limit: int) -> str:
    return f"Занято мест: <b>{taken} из {limit}</b>" if limit > 0 else f"Занято мест: <b>{taken}</b>"


async def _edit_or_send(message: types.Message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


def _parse_nav(parts: list[str]) -> tuple[str, int]:
    """Хвост callback «…:<фильтр>:<смещение>»; мусор — первая страница кандидатов."""
    flt = parts[0] if parts and parts[0] in FILTER_LABELS else "candidates"
    try:
        offset = max(0, int(parts[1])) if len(parts) > 1 else 0
    except ValueError:
        offset = 0
    return flt, offset


def _parse_person(data: str) -> tuple[int | None, str, int]:
    """«prefix:<tid>[:<фильтр>:<смещение>]» -> (tid, фильтр, смещение)."""
    parts = data.split(":")[1:]
    try:
        tid = int(parts[0]) if parts else None
    except ValueError:
        tid = None
    flt, offset = _parse_nav(parts[1:])
    return tid, flt, offset


# ── список ───────────────────────────────────────────────────────────────────────────────

def _row_line(number: int, row: dict, counts: dict, city: str | None) -> str:
    bits = [f"{number}. {_name(row)}"]
    nick = _nick(row)
    if nick:
        bits.append(nick)
    if city:
        bits.append(html.escape(city))
    bits.append("заявка: " + APP_STATUS_LABELS.get(row.get("status") or "", "—"))
    bits.append(f"привёл {counts['total']} / прошли {counts['qualified']}")
    if row.get("ambassador_status") == "candidate" and row.get("ambassador_reserve_at"):
        bits.append("⏸ в запасе")
    if row.get("ambassador_status") == "active":
        if not row.get("ambassador_slot_at"):
            bits.append("без пакета")
        if row.get("ambassador_pack_at"):
            bits.append("🎁 пакет выдан")
    return " · ".join(bits)


async def render_list(admin_id: int, flt: str = "candidates",
                      offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    from handlers.settings.admin_core import _admin_city_view  # ленивый шов, как у экранов заявок
    from handlers.settings.admin_sections import back_button

    scope, city = await _admin_city_view(admin_id)
    total = await amb_status_db.count_by_filter(flt, city_scope=scope)
    if offset >= total:
        offset = max(0, (total - 1) // PAGE * PAGE)
    rows = await amb_status_db.list_page(flt, offset=offset, limit=PAGE, city_scope=scope)
    counts = await amb_tiers_db.referral_counts_bulk(
        [r["telegram_id"] for r in rows], await amb_tiers.current_season(),
    )
    taken, limit = await amb_status.slot_counter()
    pages = max(1, (total + PAGE - 1) // PAGE)

    lines = ["<b>🤝 Амбассадоры → 🙋 Кандидаты и команда</b>", "", _slots_line(taken, limit)]
    if city:
        lines.append(f"Город: {html.escape(str(city))}")
    lines.append(f"Показаны: {FILTER_LABELS[flt]} ({total})")
    lines.append(f"Стр. {offset // PAGE + 1} из {pages}")
    lines.append("")
    if not rows:
        lines.append(_EMPTY[flt])
    person_buttons = []
    for i, row in enumerate(rows):
        tid = int(row["telegram_id"])
        city_text = await city_label_or_none(row.get("event_city"))
        lines.append(_row_line(offset + i + 1, row, counts.get(tid, _ZERO), city_text))
        label = str(row.get("full_name") or "").strip() or "без имени"
        person_buttons.append([InlineKeyboardButton(
            text=f"{offset + i + 1}. {label[:40]}", callback_data=f"ambp:{tid}:{flt}:{offset}",
        )])
    lines += ["", "Нажмите на человека, чтобы взять его в команду, отложить или открыть анкету."]

    filters = [
        InlineKeyboardButton(text=("• " if key == flt else "") + label, callback_data=f"ambc:{key}:0")
        for key, label in FILTER_LABELS.items()
    ]
    kb_rows = [filters[:2], filters[2:], *person_buttons]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"ambc:{flt}:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(InlineKeyboardButton(text="Дальше ▶️", callback_data=f"ambc:{flt}:{offset + PAGE}"))
    if nav:
        kb_rows.append(nav)
    from handlers.amb.admin_amb_bulk import bulk_buttons  # шов массовых действий, хвост этого файла
    kb_rows += await bulk_buttons(scope)
    kb_rows.append([InlineKeyboardButton(text="📥 Выгрузить в таблицу (CSV)", callback_data="ambc_csv")])
    kb_rows.append([back_button("admin_amb_candidates")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "admin_amb_candidates")
async def show_candidates(callback: types.CallbackQuery):
    text, kb = await render_list(callback.from_user.id)
    await _edit_or_send(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ambc:"))
async def candidates_page(callback: types.CallbackQuery):
    flt, offset = _parse_nav(callback.data.split(":")[1:])
    text, kb = await render_list(callback.from_user.id, flt, offset)
    await _edit_or_send(callback.message, text, kb)
    await callback.answer()


# ── карточка человека ────────────────────────────────────────────────────────────────────

async def _can_claim_slot(user: dict, st: dict) -> bool:
    """Показывать ли «🎟 Дать место»: в команде без места, заявка одобрена в этом сезоне и
    место свободно."""
    if st["status"] != "active" or st["slot_at"] or user.get("status") != "approved":
        return False
    if (user.get("season") or "") != await amb_tiers.current_season():
        return False
    return not await amb_status.slots_full()


async def render_person(admin_id: int, tid: int, flt: str,
                        offset: int) -> tuple[str, InlineKeyboardMarkup] | None:
    user = await _db.get_user(tid)
    st = await amb_status_db.get_status(tid)
    if not user or not st:
        return None
    counts = (await amb_tiers_db.referral_counts_bulk([tid], await amb_tiers.current_season())
              ).get(tid, _ZERO)
    status = st["status"]
    lines = [f"<b>🙋 {_name(user)}</b>", ""]
    nick = _nick(user)
    lines.append(f"Ник: @{nick}" if nick else "Ник: нет")
    city = await city_label_or_none(user.get("event_city"))
    if city:
        lines.append(f"Город: {html.escape(city)}")
    lines.append("Заявка на форум: " + APP_STATUS_LABELS.get(user.get("status") or "", "—"))
    amb_line = "Амбассадор: " + AMB_STATUS_LABELS.get(status, "—")
    if status == "candidate" and st["reserve_at"]:
        amb_line += " · ⏸ в запасе"
    lines.append(amb_line)
    if status == "active" or st["slot_at"]:
        lines.append("Место в команде: есть" if st["slot_at"] else "Место в команде: нет (без пакета)")
    if st["pack_at"]:
        lines.append(f"🎁 Пакет выдан: {html.escape(str(st['pack_at'])[:10])}")
    lines.append(f"Привёл по ссылке: {counts['total']} · прошли отбор: {counts['qualified']}")
    taken, limit = await amb_status.slot_counter()
    lines += ["", _slots_line(taken, limit)]

    tail = f"{tid}:{flt}:{offset}"
    rows = []
    if status in ("candidate", "declined", "left", "none"):
        take = [InlineKeyboardButton(text="✅ Взять", callback_data=f"ambc_take:{tail}")]
        if status == "candidate":
            take.append(InlineKeyboardButton(text="⏸ Не сейчас", callback_data=f"ambc_later:{tail}"))
        rows.append(take)
    if status == "active":
        pack = "да" if st["pack_at"] else "нет"
        rows.append([InlineKeyboardButton(text=f"🎁 Пакет выдан: {pack}", callback_data=f"ambc_pack:{tail}")])
        if await _can_claim_slot(user, st):
            rows.append([InlineKeyboardButton(text="🎟 Дать место", callback_data=f"ambc_slot:{tail}")])
        rows.append([InlineKeyboardButton(text="🚪 Вывести из команды", callback_data=f"ambc_rm:{tail}")])
    if await has_capability(admin_id, "moderate_reg"):
        rows.append([InlineKeyboardButton(text="🧾 Анкета", callback_data=f"ambc_card:{tid}")])
    rows.append([InlineKeyboardButton(text="← К списку", callback_data=f"ambc:{flt}:{offset}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_person(callback: types.CallbackQuery, tid: int, flt: str, offset: int) -> bool:
    screen = await render_person(callback.from_user.id, tid, flt, offset)
    if screen is None:
        return False
    await _edit_or_send(callback.message, *screen)
    return True


@router.callback_query(F.data.startswith("ambp:"))
async def person_card(callback: types.CallbackQuery):
    tid, flt, offset = _parse_person(callback.data)
    if tid is None or not await _show_person(callback, tid, flt, offset):
        await callback.answer(_STALE, show_alert=True)
        return
    await callback.answer()


# ── сообщение человеку ───────────────────────────────────────────────────────────────────

async def _notify(bot, tid: int, key: str) -> bool | None:
    """Текст реестра `key` человеку на его языке через тихие часы; `{link}` — его реф-ссылка.
    True — отправлено, False — придёт утром, None — не удалось (заблокировал бота и т.п.)."""
    try:
        from domain.regform.engine import build_referral_link
        from services import quiet_hours
        from services.i18n import context as i18n_context, tr as i18n_tr
        from services.scheduler import _now_moscow_naive

        lang, tr_map = await i18n_context(tid)
        text = i18n_tr(await get_setting_typed(key) or "", lang, tr_map)
        if "{link}" in text:
            me = await bot.get_me()
            text = text.replace("{link}", build_referral_link(me.username, tid))
        if not text.strip():
            return None
        return await quiet_hours.send_or_queue_text(
            _now_moscow_naive(), tid, text,
            sender=lambda: bot.send_message(tid, text, parse_mode="HTML"),
        )
    except Exception:
        logger.exception("amb_candidates: сообщение %s не доставлено (tid=%s)", key, tid)
        return None


def _notice_suffix(sent: bool | None) -> str:
    if sent is None:
        return " Сообщение ему не доставлено — возможно, он заблокировал бота."
    return "" if sent else " Сообщение придёт утром."


# ── действия ─────────────────────────────────────────────────────────────────────────────

async def _take_alert(tid: int, slot: bool) -> str:
    taken, limit = await amb_status.slot_counter()
    if slot:
        return f"Взят. Место за ним — {taken} из {limit}." if limit > 0 else "Взят. Место за ним."
    user = await _db.get_user(tid) or {}
    if user.get("status") != "approved" or (user.get("season") or "") != await amb_tiers.current_season():
        return "Взят без пакета: заявка на форум ещё не одобрена."
    if limit > 0 and taken >= limit:
        return f"Взят без пакета: все {limit} мест заняты."
    return "Взят без пакета."


async def take_and_notify(bot, admin_id: int, tid: int) -> tuple[amb_status.JoinResult, bool | None]:
    """«Взять» + сообщение `amb_taken_text` — общее для карточки и «➕ Назначить амбассадором».
    Сообщение уходит только при outcome `taken`; второй раз «Взять» ничего не шлёт."""
    result = await amb_status.take(tid, by=admin_id)
    sent = None
    if result.outcome == "taken":
        sent = await _notify(bot, tid, "amb_taken_text")
        logger.info("admin=%s amb_take tid=%s slot=%s notice_now=%s", admin_id, tid, result.slot, sent)
    return result, sent


@router.callback_query(F.data.startswith("ambc_take:"))
async def take_person(callback: types.CallbackQuery):
    tid, flt, offset = _parse_person(callback.data)
    if tid is None:
        await callback.answer(_STALE, show_alert=True)
        return
    result, sent = await take_and_notify(callback.bot, callback.from_user.id, tid)
    if result.outcome == "already_active":
        await callback.answer("Уже в команде", show_alert=True)
    elif result.outcome != "taken":
        await callback.answer(_STALE, show_alert=True)
        return
    else:
        await callback.answer(_alert(await _take_alert(tid, result.slot) + _notice_suffix(sent)),
                              show_alert=True)
    await _show_person(callback, tid, flt, offset)


@router.callback_query(F.data.startswith("ambc_later:"))
async def later_person(callback: types.CallbackQuery):
    tid, flt, offset = _parse_person(callback.data)
    if tid is None or not await _db.get_user(tid):
        await callback.answer(_STALE, show_alert=True)
        return
    if await amb_status_db.set_reserve(tid, at=_now()):
        logger.info("admin=%s amb_reserve tid=%s", callback.from_user.id, tid)
        await callback.answer("Оставлен в запасе — ему ничего не придёт.", show_alert=True)
    else:
        await callback.answer("Он уже не кандидат — карточка обновлена.", show_alert=True)
    await _show_person(callback, tid, flt, offset)


@router.callback_query(F.data.startswith("ambc_pack:"))
async def toggle_pack(callback: types.CallbackQuery):
    tid, flt, offset = _parse_person(callback.data)
    st = await amb_status_db.get_status(tid) if tid is not None else None
    if not st:
        await callback.answer(_STALE, show_alert=True)
        return
    given = not st["pack_at"]
    await amb_status_db.set_pack(tid, given, at=_now())
    logger.info("admin=%s amb_pack tid=%s given=%s", callback.from_user.id, tid, given)
    await callback.answer("🎁 Отмечено: пакет выдан." if given else "Отметка «пакет выдан» снята.")
    await _show_person(callback, tid, flt, offset)


@router.callback_query(F.data.startswith("ambc_slot:"))
async def give_slot(callback: types.CallbackQuery):
    tid, flt, offset = _parse_person(callback.data)
    user = await _db.get_user(tid) if tid is not None else None
    st = await amb_status_db.get_status(tid) if user else None
    if not st:
        await callback.answer(_STALE, show_alert=True)
        return
    limit = await amb_status.slots_limit()
    season = await amb_tiers.current_season()
    if await amb_status_db.try_claim_slot(tid, limit=limit, season=season, at=_now()):
        taken = await amb_status_db.slots_taken()
        logger.info("admin=%s amb_slot tid=%s", callback.from_user.id, tid)
        note = f"Место выдано — {taken} из {limit}." if limit > 0 else "Место выдано."
    elif st["status"] != "active":
        note = "Нельзя: он не в команде."
    elif st["slot_at"]:
        note = "Место уже за ним."
    elif user.get("status") != "approved" or (user.get("season") or "") != season:
        note = "Нельзя: заявка на форум не одобрена."
    else:
        note = "Нельзя: все места заняты. Освободите место или увеличьте лимит в «🚪 Вход и лимит»."
    await callback.answer(_alert(note), show_alert=True)
    await _show_person(callback, tid, flt, offset)


@router.callback_query(F.data.startswith("ambc_rm:"))
async def remove_confirm(callback: types.CallbackQuery):
    tid, flt, offset = _parse_person(callback.data)
    user = await _db.get_user(tid) if tid is not None else None
    st = await amb_status_db.get_status(tid) if user else None
    if not st:
        await callback.answer(_STALE, show_alert=True)
        return
    if st["status"] != "active":
        await callback.answer("Он уже не в команде", show_alert=True)
        await _show_person(callback, tid, flt, offset)
        return
    if st["pack_at"]:
        slot_note = "Пакет уже выдан — место останется за ним и не освободится."
    elif st["slot_at"]:
        slot_note = "Место освободится — можно взять следующего из запаса."
    else:
        slot_note = "Места у него нет — счётчик мест не изменится."
    message_text = html.escape(await get_setting_typed("amb_removed_text") or "")
    text = (
        f"<b>Вывести {_name(user)} из команды?</b>\n\n{slot_note}\n\n"
        f"Ему придёт сообщение: «{message_text}»\n\n"
        "Ссылка для приглашений у него продолжит работать, баллы останутся за ним."
    )
    tail = f"{tid}:{flt}:{offset}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚪 Да, вывести из команды", callback_data=f"ambc_rm_go:{tail}")],
        [InlineKeyboardButton(text="← Не выводить", callback_data=f"ambp:{tail}")],
    ])
    await _edit_or_send(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ambc_rm_go:"))
async def remove_apply(callback: types.CallbackQuery):
    tid, flt, offset = _parse_person(callback.data)
    if tid is None or not await _db.get_user(tid):
        await callback.answer(_STALE, show_alert=True)
        return
    if not await amb_status.remove(tid, by=callback.from_user.id):
        await callback.answer("Он уже не в команде", show_alert=True)
    else:
        sent = await _notify(callback.bot, tid, "amb_removed_text")
        taken, limit = await amb_status.slot_counter()
        counter = f"{taken} из {limit}" if limit > 0 else str(taken)
        await callback.answer(
            _alert(f"Выведен из команды. Занято мест: {counter}." + _notice_suffix(sent)),
            show_alert=True,
        )
    await _show_person(callback, tid, flt, offset)


CSV_HEADERS = [
    "Имя", "username", "Город", "Статус заявки", "Статус амбассадора", "Место", "Пакет выдан",
    "Пришли по ссылке", "Прошли отбор", "Вступил", "Telegram ID",
]


async def export_csv(city_scope=None) -> tuple[bytes, int]:
    """Файл «Амбассадоры и кандидаты»: все со статусом амбассадора (в городе админа), `;`,
    utf-8-sig. Ник без «@» (ведущая «@» — триггер формулы), каждая строковая ячейка —
    через `_csv_safe`. Возвращает (байты, число людей)."""
    rows = await amb_status_db.export_rows(city_scope=city_scope)
    counts = await amb_tiers_db.referral_counts_bulk(
        [r["telegram_id"] for r in rows], await amb_tiers.current_season(),
    )
    safe = _db._csv_safe
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_HEADERS)
    for row in rows:
        tid = int(row["telegram_id"])
        c = counts.get(tid, _ZERO)
        writer.writerow([
            safe(str(row.get("full_name") or "")),
            safe(str(row.get("username") or "").strip().lstrip("@")),
            safe(await city_label_or_none(row.get("event_city")) or ""),
            APP_STATUS_LABELS.get(row.get("status") or "", "—"),
            AMB_STATUS_LABELS.get(row.get("ambassador_status") or "none", "—"),
            "да" if row.get("ambassador_slot_at") else "нет",
            safe(str(row.get("ambassador_pack_at") or "")),
            c["total"], c["qualified"],
            safe(str(row.get("ambassador_since") or "")),
            tid,
        ])
    return output.getvalue().encode("utf-8-sig"), len(rows)


@router.callback_query(F.data == "ambc_csv")
async def candidates_csv(callback: types.CallbackQuery):
    from handlers.settings.admin_core import _admin_city_view

    scope, _city = await _admin_city_view(callback.from_user.id)
    data, count = await export_csv(scope)
    logger.info("admin=%s amb_team_csv rows=%s", callback.from_user.id, count)
    await callback.message.answer_document(
        BufferedInputFile(data, filename="ambassadors_team.csv"),
        caption=f"Амбассадоры и кандидаты: {count} человек",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ambc_card:"))
async def show_form_card(callback: types.CallbackQuery):
    from handlers.applications.admin_modcard_render import build_card_text

    tid, _flt, _offset = _parse_person(callback.data)
    user = await _db.get_user(tid) if tid is not None else None
    if not user:
        await callback.answer(_STALE, show_alert=True)
        return
    card = await build_card_text(user)
    await callback.message.answer(card.text, parse_mode="HTML")
    await callback.answer()


# Массовые действия (admin_amb_bulk: ambc_decl*, ambc_add*, ambc_arch_csv) — хвост admin.router
# после хендлеров этого файла.
from handlers.amb import admin_amb_bulk  # noqa: E402,F401
# «🧹 Сбросить статусы» (ambrst*) — хвост admin.router после хендлеров этого файла.
from handlers.amb import admin_amb_reset  # noqa: E402,F401
