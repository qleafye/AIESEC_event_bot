"""Phase 33 (delegate-card admin actions): «🏙 Перевести в город» — кнопка на карточке `/find`
(`handlers/admin.py::cmd_find_user`), сам перевод — `services/city_move.py`.

Не форумная функция (админ-действие модератора, не тумблер делегатского флоу) — своей строки
в хабе «🎪 Форум: функции» нет и не нужно (см. рабочее задание фазы).

Шов той же формы, что соседние (`admin_program_halls.py`, `admin_checkin.py`): своего `Router()`
нет, хендлеры декорируют ОБЩИЙ `admin.router`, модуль импортируется ХВОСТОМ `handlers/admin.py`
(golden snapshot: чистая вставка). `_city_allowed` — ИМПОРТ ИЗ `handlers/admin_checkin.py`
(владелец функции — параллельный трек 6b, самим файлом не владеем, только вызываем)."""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import city_codes, city_label, cities_module_on, get_city, normalize_city
from database.db import get_user
from handlers.admin import router
from handlers.admin_checkin import _city_allowed
from services.city_move import (
    STATUS_MODE_KEEP,
    STATUS_MODE_TO_MODERATION,
    move_user_city,
    preview_track_change,
)

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN_ALERT = "Этот город вне вашей зоны ответственности."
_MODULE_OFF_ALERT = "Модуль городов выключен — переводить некуда."
_NOT_FOUND_ALERT = "Делегат не найден — возможно, карточка устарела."

_TRACK_LABELS = {"short": "⚡ Краткая", "full": "📋 Полная"}
_STATUS_LABELS = {
    "pending": "⏳ На рассмотрении", "approved": "✅ Одобрена", "rejected": "❌ Отклонена",
    "waitlist": "📋 Лист ожидания",
}


def _track_label(participant_type: str | None) -> str:
    if participant_type in ("party_overnight", "party_noovernight"):
        return "🎉 Вечеринка"
    return _TRACK_LABELS.get(participant_type or "full", "📋 Полная")


def _status_label(status: str | None) -> str:
    return _STATUS_LABELS.get(status or "", status or "-")


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.lstrip("-").isdigit() else None


@router.callback_query(F.data.startswith("citymv_start:"))
async def citymove_start(callback: types.CallbackQuery):
    if not await cities_module_on():
        await callback.answer(_MODULE_OFF_ALERT, show_alert=True)
        return
    tid = _parse_tid(callback.data.split(":", 1)[1])
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    admin_id = callback.from_user.id
    old_city = normalize_city(user.get("event_city"))
    if not await _city_allowed(admin_id, old_city):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    buttons = []
    for code in city_codes():
        if code == old_city:
            continue
        if not await _city_allowed(admin_id, code):
            continue
        buttons.append([InlineKeyboardButton(
            text=await city_label(code), callback_data=f"citymv_pick:{tid}:{code}",
        )])
    if not buttons:
        await callback.answer("Нет доступных городов, кроме текущего.", show_alert=True)
        return
    buttons.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=f"citymv_cancel:{tid}")])

    name = html_module.escape(str(user.get("full_name") or "-"))
    old_label = html_module.escape(await city_label(old_city))
    text = f"🏙 <b>В какой город перевести?</b>\n\n{name}\nСейчас: {old_label}"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("citymv_pick:"))
async def citymove_pick_city(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = _parse_tid(parts[1])
    code = parts[2]
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    if get_city(code) is None:
        await callback.answer("Такого города нет.", show_alert=True)
        return

    admin_id = callback.from_user.id
    old_city = normalize_city(user.get("event_city"))
    # Callback-данные подделываемы (T-33 threat register): оба города — и старый, и новый —
    # перепроверяются на КАЖДОМ шаге, не только здесь.
    if not await _city_allowed(admin_id, old_city) or not await _city_allowed(admin_id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    if code == old_city:
        await callback.answer("Делегат уже в этом городе.", show_alert=True)
        return

    participant_type = user.get("participant_type")
    new_track, track_changed = await preview_track_change(participant_type, code)

    name = html_module.escape(str(user.get("full_name") or "-"))
    old_label = html_module.escape(await city_label(old_city))
    new_label = html_module.escape(await city_label(code))
    status_label = _status_label(user.get("status"))

    lines = [
        f"🏙 <b>Перевод в другой город</b>\n",
        f"{name}",
        f"Город: {old_label} → {new_label}",
    ]
    if track_changed:
        lines.append(
            f"Трек: {_track_label(participant_type)} → {_track_label(new_track)} "
            f"(у «{new_label}» нет трека «{_track_label(participant_type)}»)"
        )
    lines.append(f"Статус сейчас: {status_label}")
    lines.append("")
    lines.append(
        "Что изменится: строка в Google-таблице переедет на вкладку нового города; открытый "
        "черновик анкеты (если есть) переедет вместе с городом. Делегату ничего не приходит."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Оставить статус ({status_label})",
            callback_data=f"citymv_apply:{tid}:{code}:{STATUS_MODE_KEEP}",
        )],
        [InlineKeyboardButton(
            text="↩️ Вернуть на модерацию",
            callback_data=f"citymv_apply:{tid}:{code}:{STATUS_MODE_TO_MODERATION}",
        )],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"citymv_cancel:{tid}")],
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("citymv_apply:"))
async def citymove_apply(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = _parse_tid(parts[1])
    code = parts[2]
    mode_raw = parts[3]
    mode = STATUS_MODE_TO_MODERATION if mode_raw == STATUS_MODE_TO_MODERATION else STATUS_MODE_KEEP
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    if get_city(code) is None:
        await callback.answer("Такого города нет.", show_alert=True)
        return

    admin_id = callback.from_user.id
    old_city = normalize_city(user.get("event_city"))
    # Тот же двойной перечёт прав, что на предыдущем экране — callback_data доехал сюда через
    # тап пользователя, но между экранами могло пройти любое время.
    if not await _city_allowed(admin_id, old_city) or not await _city_allowed(admin_id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    report = await move_user_city(tid, code, status_mode=mode, by_admin=admin_id, dry_run=False)

    if not report.get("ok"):
        await callback.message.edit_text(
            f"❌ Перевод не выполнен: {html_module.escape(str(report.get('error') or '-'))}",
        )
        await callback.answer()
        return

    name = html_module.escape(str(user.get("full_name") or "-"))
    new_label = html_module.escape(await city_label(code))
    lines = [f"✅ <b>{name}</b> переведён(а) в {new_label}."]
    if report.get("track_changed"):
        lines.append(f"Трек: {_track_label(report['after']['participant_type'])}")
    if report.get("status_changed"):
        lines.append("Статус возвращён на модерацию.")
    note = report.get("status_note")
    if note:
        lines.append(f"⚠️ {html_module.escape(note)}")
    sheet = report.get("sheet") or {}
    if sheet.get("moved"):
        lines.append("Строка в таблице перенесена.")
    if sheet.get("error"):
        lines.append(f"⚠️ Лист: {html_module.escape(str(sheet['error']))}")
    changed = report.get("db_changes") or []
    if changed:
        lines.append(f"Обновлено в базе: {', '.join(changed)}.")

    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("citymv_cancel:"))
async def citymove_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("✖️ Перевод отменён.")
    await callback.answer()
