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

from domain.cities import city_codes, city_label, cities_module_on, get_city, is_city_enabled, normalize_city
from database.db import get_user
from handlers.i18n import reg_i18n
from handlers.admin import router
from handlers.admin_checkin import _city_allowed
from services import i18n as i18n_service
from services.city_move import (
    STATUS_MODE_KEEP,
    STATUS_MODE_TO_MODERATION,
    move_user_city,
    preview_city_move,
)
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN_ALERT = "Этот город вне вашей зоны ответственности."
_CITY_DISABLED_ALERT = "Этот город выключен. Включите его в «🏙 Города» или выберите другой."
_MODULE_OFF_ALERT = "Модуль городов выключен — переводить некуда."
_NOT_FOUND_ALERT = "Делегат не найден — возможно, карточка устарела."

_TRACK_LABELS = {"short": "⚡ Краткая", "full": "📋 Полная"}
_STATUS_LABELS = {
    "pending": "⏳ На рассмотрении", "approved": "✅ Одобрена", "rejected": "❌ Отклонена",
    "waitlist": "📋 Лист ожидания",
}

# Тумблер «🔔 Сообщить делегату» на экране подтверждения — состояние едет в callback_data
# (FSM тут не заведена, экран одношаговый), дефолт «да». Перепроверяется на КАЖДОМ шаге,
# как и коды городов (callback_data подделываема).
_NOTIFY_ON = "1"
_NOTIFY_OFF = "0"


def _track_label(participant_type: str | None) -> str:
    if participant_type in ("party_overnight", "party_noovernight"):
        return "🎉 Вечеринка"
    return _TRACK_LABELS.get(participant_type or "full", "📋 Полная")


def _status_label(status: str | None) -> str:
    return _STATUS_LABELS.get(status or "", status or "-")


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.lstrip("-").isdigit() else None


async def _notify_delegate(bot, tid: int, city_label_plain: str) -> bool:
    """Личное сообщение делегату о переводе, `city_move_delegate_notice_text` (группа "reg",
    переводится корпусом). Сбой (делегат заблокировал бота, сеть) — fail-soft: сам перевод
    уже применён и откатывать его из-за недоставленного уведомления нельзя, вызывающий сам
    решает, что сказать менеджеру."""
    try:
        template = await get_setting_typed("city_move_delegate_notice_text")
        if not (template or "").strip():
            return False
        lang, tr_map = await i18n_service.context(tid)
        text = reg_i18n.tr_fmt(template, lang, tr_map, city=city_label_plain)
        await bot.send_message(tid, text)
        return True
    except Exception as e:
        logger.warning("admin_city_move._notify_delegate(%s): %s", tid, e, exc_info=True)
        return False


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
        # Выключенный город (его нет в анкете и меню) — не цель перевода: делегат оказался бы
        # там, где у события нет ни программы, ни чата.
        if not await is_city_enabled(code):
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


def _notify_toggle_button(tid: int, code: str, notify: str) -> InlineKeyboardButton:
    is_on = notify == _NOTIFY_ON
    label = "🔔 Сообщить делегату: да" if is_on else "🔕 Сообщить делегату: нет"
    next_notify = _NOTIFY_OFF if is_on else _NOTIFY_ON
    return InlineKeyboardButton(text=label, callback_data=f"citymv_notify:{tid}:{code}:{next_notify}")


async def _render_confirm_screen(
    tid: int, user: dict, code: str, notify: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран подтверждения перевода — вынесен отдельно от `citymove_pick_city`, чтобы тумблер
    «🔔 Сообщить делегату» мог перерисовать ТОТ ЖЕ экран, не дублируя вёрстку."""
    old_city = normalize_city(user.get("event_city"))
    participant_type = user.get("participant_type")
    preview = await preview_city_move(participant_type, code, tid)

    name = html_module.escape(str(user.get("full_name") or "-"))
    old_label = html_module.escape(await city_label(old_city))
    new_label = html_module.escape(await city_label(code))
    status_label = _status_label(user.get("status"))

    lines = [
        f"🏙 <b>Перевод в другой город</b>\n",
        f"{name}",
        f"Город: {old_label} → {new_label}",
    ]
    if not preview["track_supported"]:
        track_label = html_module.escape(_track_label(participant_type))
        lines.append(
            f"⚠️ У «{new_label}» нет анкеты «{track_label}» — делегат останется с треком "
            f"«{track_label}»; если нужен другой трек — верните на модерацию и попросите "
            "делегата дозаполнить анкету."
        )
    sheet_preview = preview["sheet"]
    target_tab = sheet_preview["target_tab"]
    write_tab = sheet_preview["write_tab"]
    if target_tab is not None and write_tab != target_tab:
        target_esc = html_module.escape(target_tab)
        if write_tab is False:
            lines.append(f"⚠️ Лист: вкладки «{target_esc}» нет — строка НЕ уйдёт в таблицу, добавьте её вручную.")
        else:
            write_esc = html_module.escape(write_tab) if write_tab else "главный лист"
            lines.append(f"Строка уйдёт на вкладку «{write_esc}» — вкладки «{target_esc}» пока нет в таблице.")
    if preview.get("enrollments"):
        lines.append(f"⚠️ Записи на сессии старого города пропадут: {preview['enrollments']}")
    lines.append(f"Статус сейчас: {status_label}")
    lines.append("")
    lines.append(
        "Что изменится: строка в Google-таблице переедет на вкладку нового города; открытый "
        "черновик анкеты (если есть) переедет вместе с городом. Делегату придёт уведомление, "
        "если ниже включена кнопка «🔔 Сообщить делегату»."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Оставить статус ({status_label})",
            callback_data=f"citymv_apply:{tid}:{code}:{STATUS_MODE_KEEP}:{notify}",
        )],
        [InlineKeyboardButton(
            text="↩️ Вернуть на модерацию",
            callback_data=f"citymv_apply:{tid}:{code}:{STATUS_MODE_TO_MODERATION}:{notify}",
        )],
        [_notify_toggle_button(tid, code, notify)],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"citymv_cancel:{tid}")],
    ])
    return "\n".join(lines), kb


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
    if not await is_city_enabled(code):
        await callback.answer(_CITY_DISABLED_ALERT, show_alert=True)
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

    text, kb = await _render_confirm_screen(tid, user, code, _NOTIFY_ON)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("citymv_notify:"))
async def citymove_notify_toggle(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = _parse_tid(parts[1])
    code = parts[2]
    notify = parts[3]
    if tid is None or notify not in (_NOTIFY_ON, _NOTIFY_OFF):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    if get_city(code) is None:
        await callback.answer("Такого города нет.", show_alert=True)
        return
    if not await is_city_enabled(code):
        await callback.answer(_CITY_DISABLED_ALERT, show_alert=True)
        return

    admin_id = callback.from_user.id
    old_city = normalize_city(user.get("event_city"))
    if not await _city_allowed(admin_id, old_city) or not await _city_allowed(admin_id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    text, kb = await _render_confirm_screen(tid, user, code, notify)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("citymv_apply:"))
async def citymove_apply(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = _parse_tid(parts[1])
    code = parts[2]
    mode_raw = parts[3]
    notify = parts[4] if parts[4] == _NOTIFY_ON else _NOTIFY_OFF
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
    if not await is_city_enabled(code):
        await callback.answer(_CITY_DISABLED_ALERT, show_alert=True)
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
    new_label_plain = await city_label(code)
    new_label = html_module.escape(new_label_plain)
    lines = [f"✅ <b>{name}</b> переведён(а): город — {new_label}."]
    if report.get("status_changed"):
        lines.append("Статус возвращён на модерацию.")
    note = report.get("status_note")
    if note:
        lines.append(f"⚠️ {html_module.escape(note)}")
    sheet = report.get("sheet") or {}
    if sheet.get("moved"):
        write_tab = sheet.get("write_tab")
        target_tab = sheet.get("target_tab")
        if target_tab is not None and write_tab != target_tab:
            lines.append(f"Строка в таблице перенесена (на вкладку «{html_module.escape(str(write_tab))}»).")
        else:
            lines.append("Строка в таблице перенесена.")
    if sheet.get("error"):
        lines.append(f"⚠️ Лист: {html_module.escape(str(sheet['error']))}")
    # T-33: имена таблиц БД — не для человека (`db_changes` несёт технические имена вроде
    # "users"/"reg_drafts") — только факт, что данные делегата обновлены.
    if report.get("db_changes"):
        lines.append("Данные делегата обновлены.")

    if notify == _NOTIFY_ON:
        sent = await _notify_delegate(callback.bot, tid, new_label_plain)
        if not sent:
            lines.append("⚠️ Делегату сообщить не удалось (возможно, заблокировал бота).")

    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("citymv_cancel:"))
async def citymove_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("✖️ Перевод отменён.")
    await callback.answer()
