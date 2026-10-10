"""Идеи №31/№32 бэклога чек-ина: «📓 Журнал площадки» (кто что сделал в день форума) и снятие
ошибочной отметки менеджером — экраны бота. Домен — `services/venue_log.py`, БД —
`database/db.py` (`venue_log`, `revoke_checkin`).

Форма шва — та же, что `handlers/applications/admin_reject_journal.py`: своего `Router()` нет, хендлеры
декорируют ОБЩИЙ `handlers.admin.router`, каждый декоратор — в одну строку (инвариант
cap-теста). Право — `moderate_reg` (как у «⚙️ Настройки QR»/«Перевыпустить QR» — управление
отметками, не рутинное сканирование волонтёра), и каждый хендлер перепроверяет его сам:
вход в журнал стоит на экране «✅ Отметки на форуме», открытом под правом `checkin`.

Вход:
- кнопка «📓 Журнал площадки» на «✅ Отметки на форуме» (`venue_entry_rows` — её рисует
  `handlers/forum/admin_checkin.py`, только держателям `moderate_reg`);
- в журнале — «🗑 Снять отметку делегату»: поиск по фамилии/@username -> отметки делегата
  -> подтверждение.

Снятие — только одной выбранной отметки, через экран подтверждения, где написано, что именно
пропадёт (CLAUDE.md, разрушительные операции). Городской скоуп — `_card_out_of_scope`: менеджер,
у которого в шапке выбран город, чужих делегатов не снимает."""
import html
import logging

from aiogram import F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from domain.cities import city_label_or_none
from database.db import get_checkin, get_user, list_checkins_for_user, venue_log_page, venue_log_staff
from handlers.admin import router
from handlers.access.admin_caps import has_capability
from handlers.admin_core import _admin_city_view, _card_out_of_scope
from handlers.states import VenueRevokeFind
from keyboards.builders import get_cancel_kb
from services import venue_log
from services.person_search import search_people

logger = logging.getLogger(__name__)

_CAP = "moderate_reg"
_NO_ACCESS = "Недостаточно прав"
_OUT_OF_SCOPE = "Этот делегат из другого города — переключите город в шапке."
PAGE = 10
_STAFF_BUTTONS_MAX = 30
_SEARCH_LIMIT = 10


async def venue_entry_rows(admin_id: int) -> list[list[InlineKeyboardButton]]:
    """Кнопка входа для экрана «✅ Отметки на форуме» — только тем, кто может ей пользоваться
    (волонтёру с одним правом `checkin` не рисуем кнопку, которая ответит «Недостаточно прав»)."""
    if not await has_capability(admin_id, _CAP):
        return []
    return [[InlineKeyboardButton(text="📓 Журнал площадки", callback_data="admin_venue_log")]]


def _parse_ints(data: str, n: int) -> list[int] | None:
    """Числа из `prefix:a:b`. `None` — callback битый/от старой версии экрана: хендлер отвечает
    «экран устарел», а не действует над id 0."""
    parts = (data or "").split(":")[1:]
    if len(parts) < n:
        return None
    try:
        return [int(x) for x in parts[:n]]
    except ValueError:
        return None


_STALE = "Экран устарел — откройте журнал площадки заново."


async def render_log_screen(admin_id: int, staff_id: int = 0, offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    scope, label = await _admin_city_view(admin_id)
    rows, total = await venue_log_page(
        city_scope=scope, staff_id=staff_id or None, offset=offset, limit=PAGE,
    )
    lines = ["📓 <b>Журнал площадки</b>"]
    if label:
        lines.append(html.escape(str(label)))
    if staff_id:
        staff_name = next(
            (s.get("staff_name") for s in await venue_log_staff(city_scope=scope) if s["staff_id"] == staff_id),
            None,
        ) or await venue_log.person_name(staff_id)
        lines.append(f"Волонтёр: {html.escape(staff_name)}")
    pages = max(1, (total + PAGE - 1) // PAGE)
    lines.append(f"Событий: {total} · страница {offset // PAGE + 1} из {pages}")
    lines.append("")
    if not rows:
        lines.append(
            "Пока пусто. Сюда попадают отметки из сканера и поиска, отмены сканов, снятые "
            "отметки, перевыпуск QR и загрузки файлов сканера."
        )
    for row in rows:
        lines.append("• " + html.escape(await venue_log.describe(row)))

    buttons: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Новее", callback_data=f"vlog:{staff_id}:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(InlineKeyboardButton(text="Старше ➡️", callback_data=f"vlog:{staff_id}:{offset + PAGE}"))
    if nav:
        buttons.append(nav)
    if staff_id:
        buttons.append([InlineKeyboardButton(text="👥 Все волонтёры", callback_data="vlog:0:0")])
    buttons.append([InlineKeyboardButton(text="👤 Фильтр по волонтёру", callback_data="vlogst")])
    buttons.append([InlineKeyboardButton(text="🗑 Снять отметку делегату", callback_data="vrv_find")])
    buttons.append([InlineKeyboardButton(text="← К отметкам на форуме", callback_data="admin_checkin")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _edit_or_answer(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:  # noqa: BLE001 — сообщение без текста/слишком старое: просто новое
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin_venue_log")
async def venue_log_open(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, _CAP):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    text, kb = await render_log_screen(callback.from_user.id)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("vlog:"))
async def venue_log_page_cb(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, _CAP):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    parsed = _parse_ints(callback.data, 2)
    if parsed is None:
        await callback.answer(_STALE, show_alert=True)
        return
    staff_id, offset = parsed
    text, kb = await render_log_screen(callback.from_user.id, staff_id=staff_id, offset=max(0, offset))
    await _edit_or_answer(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "vlogst")
async def venue_log_staff_pick(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, _CAP):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    scope, _label = await _admin_city_view(callback.from_user.id)
    staff = await venue_log_staff(city_scope=scope)
    if not staff:
        await callback.answer("В журнале пока нет ни одного действия.", show_alert=True)
        return
    buttons = []
    for s in staff[:_STAFF_BUTTONS_MAX]:
        name = s.get("staff_name") or await venue_log.person_name(s["staff_id"])
        buttons.append([InlineKeyboardButton(text=f"{name} · {s['n']}", callback_data=f"vlog:{s['staff_id']}:0")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="vlog:0:0")])
    await _edit_or_answer(
        callback, "👤 <b>Чьи действия показать?</b>\nРядом с именем — сколько действий в журнале.",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


# ── Снятие отметки менеджером (идея №32) ─────────────────────────────────────────────────────

@router.callback_query(F.data == "vrv_find")
async def venue_revoke_find(callback: types.CallbackQuery, state: FSMContext):
    if not await has_capability(callback.from_user.id, _CAP):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    await state.set_state(VenueRevokeFind.waiting_query)
    await callback.message.answer(
        "Кому снять отметку? Пришлите фамилию делегата (например: <code>Иванов</code>) или "
        "его @username.",
        parse_mode="HTML", reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(VenueRevokeFind), Command("cancel"))
@router.message(StateFilter(VenueRevokeFind), F.text == "Отмена")
async def venue_revoke_find_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено. Отметки не менялись.", reply_markup=ReplyKeyboardRemove())


@router.message(VenueRevokeFind.waiting_query)
async def venue_revoke_find_step(message: types.Message, state: FSMContext):
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Не понял. Пришлите фамилию (хотя бы 2 буквы) или @username делегата.")
        return
    scope, _label = await _admin_city_view(message.from_user.id)
    found = [p for p in await search_people(query, city_scope=scope, limit=_SEARCH_LIMIT) if p["source"] == "users"]
    if not found:
        await message.answer(
            "Никого не нашёл — проверьте написание или пришлите @username. Отмена — кнопкой ниже.",
        )
        return
    await state.set_state(None)
    buttons = []
    for p in found:
        meta = " · ".join(str(x) for x in (await city_label_or_none(p.get("city")), f"@{p['username']}" if p.get("username") else None) if x)
        text = f"{p.get('full_name') or '—'}" + (f" · {meta}" if meta else "")
        buttons.append([InlineKeyboardButton(text=text[:60], callback_data=f"vrv_u:{p['user_id']}")])
    await message.answer("Нашёл:", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите делегата:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


def _mark_button_text(label: str, row: dict) -> str:
    source = venue_log.SOURCE_LABELS.get(row.get("source") or "", row.get("source") or "")
    stamp = row.get("scanned_at") or ""
    when = f"{stamp[8:10]}.{stamp[5:7]} {stamp[11:16]}" if len(stamp) >= 16 else stamp
    return f"🗑 {label} — {when} ({source})"[:64]


async def render_marks_screen(tid: int) -> tuple[str, InlineKeyboardMarkup]:
    user = await get_user(tid)
    name = html.escape(await venue_log.person_name(tid))
    marks = await list_checkins_for_user(tid)
    lines = [f"✅ <b>Отметки делегата</b>\n{name}"]
    if user is None:
        lines.append("\nДелегат не найден — возможно, удалён.")
    buttons = []
    if not marks:
        lines.append("\nОтметок нет — ни на входе, ни на сессиях.")
    else:
        lines.append("\nКакую отметку снять? Снимается только выбранная.")
        for row in marks:
            label = await venue_log.point_label(row["point"])
            buttons.append([InlineKeyboardButton(text=_mark_button_text(label, row), callback_data=f"vrv_p:{row['id']}")])
    buttons.append([InlineKeyboardButton(text="📓 Журнал площадки", callback_data="admin_venue_log")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("vrv_u:"))
async def venue_revoke_user(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, _CAP):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    parsed = _parse_ints(callback.data, 1)
    if parsed is None:
        await callback.answer(_STALE, show_alert=True)
        return
    (tid,) = parsed
    if await _card_out_of_scope(callback.from_user.id, tid):
        await callback.answer(_OUT_OF_SCOPE, show_alert=True)
        return
    text, kb = await render_marks_screen(tid)
    await _edit_or_answer(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("vrv_p:"))
async def venue_revoke_confirm(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, _CAP):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    parsed = _parse_ints(callback.data, 1)
    if parsed is None:
        await callback.answer(_STALE, show_alert=True)
        return
    (cid,) = parsed
    row = await get_checkin(cid)
    if row is None:
        await callback.answer("Этой отметки уже нет — список обновлён.", show_alert=True)
        return
    tid = row["telegram_id"]
    if await _card_out_of_scope(callback.from_user.id, tid):
        await callback.answer(_OUT_OF_SCOPE, show_alert=True)
        return
    name = html.escape(await venue_log.person_name(tid))
    label = html.escape(await venue_log.point_label(row["point"]))
    stamp = row.get("scanned_at") or ""
    lines = [f"🗑 <b>Снять отметку?</b>\n\n{name} — {label}, отмечен(а) {html.escape(stamp[:16])}", "", "Что изменится:"]
    if row["point"] == venue_log.ENTRY_POINT:
        # Вход каждый день: снимается вход ЭТОГО дня, входы других дней остаются.
        day = row.get("day") or stamp[:10]
        day_label = f"{day[8:10]}.{day[5:7]}"
        others = [m for m in await list_checkins_for_user(tid)
                  if m["point"] == venue_log.ENTRY_POINT and m["id"] != row["id"]]
        lines.append(f"• делегат снова считается не пришедшим {day_label}: пропадёт из «Пришли» "
                     "за этот день, попадёт в «Не пришли» и в рассылки для не пришедших;")
        if others:
            lines.append("• вход в другие дни форума останется, в колонке «Пришёл» таблицы "
                         "будет время самого раннего из них.")
        else:
            lines.append("• время в колонке «Пришёл» таблицы очистится.")
        sessions = [m for m in await list_checkins_for_user(tid)
                    if m["point"] != venue_log.ENTRY_POINT and (m.get("day") or "") == day]
        if sessions:
            lines.append(
                f"\n⚠️ Отметки на сессиях ({len(sessions)}) останутся — если делегата не было "
                "вовсе, снимите их отдельно."
            )
    else:
        lines.append("• делегат пропадёт из отмеченных на этой сессии и из её счётчика;")
        lines.append("• вопрос-отзыв о сессии ему не придёт, а если уже пришёл — оценку поставить не сможет.")
        lines.append("\nОтметка на входе останется.")
    lines.append("\nВ журнале площадки останется запись, кто и когда снял.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, снять", callback_data=f"vrv_go:{cid}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"vrv_u:{tid}")],
    ])
    await _edit_or_answer(callback, "\n".join(lines), kb)
    await callback.answer()


@router.callback_query(F.data.startswith("vrv_go:"))
async def venue_revoke_go(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, _CAP):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    parsed = _parse_ints(callback.data, 1)
    if parsed is None:
        await callback.answer(_STALE, show_alert=True)
        return
    (cid,) = parsed
    row = await get_checkin(cid)
    if row is not None and await _card_out_of_scope(callback.from_user.id, row["telegram_id"]):
        await callback.answer(_OUT_OF_SCOPE, show_alert=True)
        return
    removed = await venue_log.revoke_mark(cid, staff_id=callback.from_user.id, staff_name=venue_log.staff_name_of(callback.from_user))
    if removed is None:
        await callback.answer("Этой отметки уже нет — ничего не снято.", show_alert=True)
        if row is not None:
            text, kb = await render_marks_screen(row["telegram_id"])
            await _edit_or_answer(callback, text, kb)
        return
    text, kb = await render_marks_screen(removed["telegram_id"])
    await _edit_or_answer(callback, "✅ Отметка снята.\n\n" + text, kb)
    await callback.answer("Отметка снята")
