"""«🚦 Готовность к форуму» (бэклог чек-ина №25, урезанный): один экран-светофор по городу —
«всё ли готово СЕЙЧАС», а не «как настроить» (это мастер первой настройки). Семь строк:
дата форума, вход по QR и его рассылка, волонтёры с правом отметки, программа, чат SOS, запись
в таблицу, чат делегатов. У красной/жёлтой строки — кнопка на РОДНОЙ экран этой настройки
(существующие callback'и, свой логики записи здесь нет — экран только читает).

Рассылку QR проверяем чтением планировщика (`get_job`), НЕ вызовом
`services.checkin_broadcast.schedule_city_jobs` — тот переставляет джобы.

Вход — кнопка хаба «🎪 Форум: функции» (`handlers/admin_forum_functions.py`), город приходит
в callback_data оттуда. Форма шва — как у соседей: `from handlers.admin import router`, импорт
из хвоста `handlers/admin.py`. Право — `moderate_reg`, как у самого хаба."""
import html
import inspect
import logging
import time
from datetime import datetime

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import cities_module_on, city_label, city_scope, get_setting_typed_for_city, normalize_city
from config import config
from database.db import checkin_qr_send_counts, get_staff_city, sheet_arrival_queue_stats
from handlers.admin import router
from handlers.admin_caps import _holds, capability_holders, required_capability, resolve_capabilities
from handlers.admin_checkin import _CITY_FORBIDDEN_ALERT, _city_allowed, _decode_city, _encode_city
from services.checkin_arrival import count_program_sessions
from services.program import resolve_program_photo
from services.reject_rules import forum_date_for
from services.timeutil import msk_now
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

GREEN, YELLOW, RED, GRAY = "🟢", "🟡", "🔴", "⚪"
_SHEET_FRESH_SECONDS = 3600
_SHEET_QUEUE_STALE_MINUTES = 5


def _row(light: str, text: str, fix: tuple[str, str] | None = None) -> dict:
    """Строка светофора; `fix` — (подпись кнопки, callback_data) для жёлтой/красной строки."""
    return {"light": light, "text": text, "fix": fix}


async def _row_forum_date(code: str | None) -> dict:
    date_str = await forum_date_for(code)
    if not date_str:
        return _row(RED, "Дата форума не задана — без неё не уйдут QR и шпаргалка",
                    ("🗓 Задать дату форума", "settings_edit:forum_date"))
    forum_day = datetime.strptime(date_str, "%d.%m.%Y").date()
    today = msk_now().date()
    if forum_day < today:
        # Дата прошлого форума, которую забыли обновить, молча выключает рассылки QR и
        # шпаргалки — ловим её здесь, а не в день форума.
        return _row(YELLOW, f"Дата форума прошла ({date_str}) — это прошлый форум? Обновите дату",
                    ("🗓 Обновить дату форума", "settings_edit:forum_date"))
    if forum_day == today:
        return _row(GREEN, f"Дата форума: {date_str} — сегодня")
    return _row(GREEN, f"Дата форума: {date_str}")


def _job_next_run(job_id: str):
    from services import scheduler as _sched
    job = _sched.get_scheduler().get_job(job_id)
    return getattr(job, "next_run_time", None) if job is not None else None


async def _row_qr(code: str | None) -> dict:
    if await get_setting_typed("checkin_qr_enabled") != "on":
        return _row(RED, "Вход по QR выключен — делегаты не получат QR",
                    ("🎟 Включить вход по QR", "admin_forum_functions"))
    cfg_fix = ("🎟 Настройки рассылки QR", f"checkinqr_cfg:{_encode_city(code)}")
    if await get_setting_typed_for_city("checkin_qr_broadcast_enabled", code) == "off":
        return _row(YELLOW, "Вход по QR включён, но рассылка QR накануне выключена", cfg_fix)
    from services.checkin_broadcast import evening_job_id, morning_job_id
    # Джобы и счётчик отправок при выключенном модуле городов живут под `None`, не под
    # дефолтным кодом, который хаб кладёт в callback_data (см. reconcile_broadcasts).
    jc = code if await cities_module_on() else None
    try:
        runs = [t for t in (_job_next_run(evening_job_id(jc)), _job_next_run(morning_job_id(jc))) if t]
    except Exception as e:  # планировщик не поднят (тест/сбой старта) — не роняем экран
        logger.warning("forum_ready: не удалось прочитать планировщик: %s", e)
        return _row(YELLOW, "Вход по QR включён; расписание рассылки прочитать не удалось", cfg_fix)
    if runs:
        return _row(GREEN, f"Вход по QR включён, рассылка QR запланирована на {min(runs).strftime('%d.%m %H:%M')}")
    got, _confirmed = await checkin_qr_send_counts(city_scope=city_scope(jc))
    if got:
        return _row(GREEN, f"Вход по QR включён, QR уже разослан: {got}")
    if await forum_date_for(code) is None:
        return _row(RED, "Вход по QR включён, но рассылка не поставлена — нет даты форума", cfg_fix)
    return _row(YELLOW, "Вход по QR включён, но рассылка QR не запланирована", cfg_fix)


async def _row_volunteers(code: str | None) -> dict:
    fix = ("👥 Назначить волонтёров", "admin_roles")
    holders = [tid for tid in await capability_holders("checkin") if tid not in config.ADMIN_IDS]
    if code is None or not await cities_module_on():
        n, bound = len(holders), 0
    else:
        n = bound = 0
        for tid in holders:
            staff_city = await get_staff_city(tid)
            if not staff_city:
                n += 1
            elif normalize_city(staff_city) == code:
                n += 1
                bound += 1
    if n == 0:
        return _row(RED, "Никому, кроме суперадминов, не выдано право отметки на входе", fix)
    tail = f" (из них привязаны к этому городу: {bound})" if bound else ""
    return _row(GREEN, f"С правом отметки на входе: {n} чел.{tail}")


async def _row_program(code: str | None) -> dict:
    fix = ("🗓 Программа форума", "admin_program")
    sessions = await count_program_sessions(code) if code else 0
    photo = bool(await resolve_program_photo(code))
    if sessions:
        return _row(GREEN, f"Программа заведена: сессий {sessions}" + (", есть фото" if photo else ""))
    if photo:
        return _row(YELLOW, "Есть только фото программы — сессий нет, на сессиях отмечать некуда", fix)
    return _row(RED, "Программа не заведена — ни сессий, ни фото", fix)


async def _row_sos(code: str | None, bot) -> dict:
    fix = ("🆘 Настройки SOS", "admin_sos")
    if await get_setting_typed_for_city("menu_sos", code) == "off":
        return _row(GRAY, "SOS выключен в меню делегата — чат оргов не нужен")
    from services.sos import _bot_is_chat_member, sos_chat_for_city
    chat = await sos_chat_for_city(code)
    if chat is None:
        return _row(RED, "Чат SOS не привязан — сигналы уйдут в личку админам", fix)
    title = html.escape(chat["title"] or "чат оргов")
    if not await _bot_is_chat_member(bot, chat["chat_id"]):
        return _row(RED, f"Бота нет в чате SOS «{title}» — верните бота в чат", fix)
    return _row(GREEN, f"Чат SOS «{title}», бот в чате")


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин назад"
    return f"{minutes // 60} ч {minutes % 60} мин назад"


async def _row_sheet() -> dict:
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return _row(GRAY, "Таблица не подключена")
    from services.sheets import last_write_state
    state = last_write_state()
    now = time.time()
    ok, fail = state.get("ok"), state.get("fail")
    # «Пришёл» пишется в лист джобой из очереди (services/sheet_arrival_sync.py) — застрявшая
    # очередь значит, что отметки входа до таблицы не доходят.
    queued, oldest = await sheet_arrival_queue_stats()
    age_min = int((msk_now() - datetime.strptime(oldest, "%Y-%m-%d %H:%M:%S")).total_seconds() // 60) if oldest else 0
    queue_tail = f"; «Пришёл» ждут записи: {queued}, старейшая {age_min} мин" if queued else ""
    if fail and (not ok or fail > ok):
        return _row(RED, f"Последняя запись в таблицу не прошла ({_ago(now - fail)}) — проверьте доступ" + queue_tail,
                    ("🔄 Досинхронизировать таблицу", "admin_sync_sheet"))
    if queued and age_min > _SHEET_QUEUE_STALE_MINUTES:
        return _row(YELLOW, f"Отметки «Пришёл» копятся: в очереди {queued}, старейшая {age_min} мин — "
                            "таблица не принимает запись, бот повторяет сам")
    if ok and now - ok < _SHEET_FRESH_SECONDS:
        return _row(GREEN, f"Таблица пишется: последняя запись {_ago(now - ok)}")
    if ok:
        return _row(YELLOW, f"Последняя запись в таблицу {_ago(now - ok)} — нормально, если заявок не было")
    return _row(YELLOW, "С перезапуска бота записей в таблицу ещё не было")


async def _row_delegate_chat(code: str | None) -> dict:
    from services.chat_tracking import chat_for_city
    chat = await chat_for_city(code if await cities_module_on() else None)
    if chat is None:
        return _row(YELLOW, "Чат делегатов не привязан — сделайте бота админом в чате, "
                            "бот спросит город в личке")
    return _row(GREEN, f"Чат делегатов «{html.escape(chat['title'] or 'чат')}»")


async def _safe_row(title: str, fn, *args) -> dict:
    """Сбой одной проверки (база, Telegram, планировщик) не должен ронять весь экран накануне
    форума — строка становится серой с просьбой перепроверить, остальные рисуются как есть."""
    try:
        result = fn(*args)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception:
        logger.exception("forum_ready: проверка «%s» упала", title)
        return _row(GRAY, f"{title}: не удалось проверить — нажмите «🔄 Проверить снова»")


async def render_ready(admin_id: int, code: str | None, bot) -> tuple[str, InlineKeyboardMarkup]:
    caps = await resolve_capabilities(admin_id)

    def visible(callback_data: str) -> bool:
        cap = required_capability(callback_data=callback_data)
        return cap is not None and _holds(caps, cap)

    rows = [
        await _safe_row("Дата форума", _row_forum_date, code),
        await _safe_row("Вход по QR", _row_qr, code),
        await _safe_row("Право отметки на входе", _row_volunteers, code),
        await _safe_row("Программа", _row_program, code),
        await _safe_row("Чат SOS", _row_sos, code, bot),
        await _safe_row("Таблица", _row_sheet),
        await _safe_row("Чат делегатов", _row_delegate_chat, code),
    ]
    label = await city_label(code) if code and await cities_module_on() else None
    lines = ["🚦 <b>Готовность к форуму</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Проверено {msk_now().strftime('%H:%M:%S')} МСК")
    lines.append("")
    lines += [f"{r['light']} {r['text']}" for r in rows]
    red = sum(1 for r in rows if r["light"] == RED)
    lines.append("")
    lines.append("Всё готово." if red == 0 else f"Красных строк: {red} — начните с них.")

    buttons: list[list[InlineKeyboardButton]] = []
    seen: set[str] = set()
    for r in rows:
        if r["fix"] and r["light"] in (RED, YELLOW):
            text, cb = r["fix"]
            if cb not in seen and visible(cb):
                seen.add(cb)
                buttons.append([InlineKeyboardButton(text=f"{r['light']} {text}", callback_data=cb)])
    buttons.append([InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"forum_ready_re:{_encode_city(code)}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("forum_ready:"))
async def forum_ready_open(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await render_ready(callback.from_user.id, code, callback.bot)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forum_ready_re:"))
async def forum_ready_refresh(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await render_ready(callback.from_user.id, code, callback.bot)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await callback.answer("Проверено")
