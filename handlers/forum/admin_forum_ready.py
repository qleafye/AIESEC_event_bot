"""«🚦 Готовность к форуму» (бэклог чек-ина №25, урезанный): один экран-светофор по городу —
«всё ли готово СЕЙЧАС», а не «как настроить» (это мастер первой настройки). Семь строк:
дата форума, вход по QR и его рассылка, волонтёры с правом отметки, программа, чат SOS, запись
в таблицу, чат делегатов. У красной/жёлтой строки — кнопка на РОДНОЙ экран этой настройки
(существующие callback'и, свой логики записи здесь нет — экран только читает).

Рассылку QR проверяем чтением планировщика (`get_job`), НЕ вызовом
`services.checkin_broadcast.schedule_city_jobs` — тот переставляет джобы.

Вход — кнопка хаба «🎪 Форум: функции» (`handlers/forum/admin_forum_functions.py`), город приходит
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

from domain.cities import cities_module_on, city_label, city_scope, get_setting_typed_for_city, normalize_city
from config import config
from services import sheet_target as _sheet_target
from database.db import (
    checkin_qr_send_counts, get_staff_city, sheet_arrival_count_with_error, sheet_arrival_queue_stats,
)
from handlers.admin import router
from handlers.access.admin_caps import _holds, capability_holders, required_capability, resolve_capabilities
from handlers.forum.admin_checkin import _CITY_FORBIDDEN_ALERT, _city_allowed, _decode_city, _encode_city
from services.checkin_arrival import count_program_sessions
from services.program import own_program_photo, resolve_program_photo
from services.reject_rules import forum_date_for
from services.infra.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

GREEN, YELLOW, RED, GRAY = "🟢", "🟡", "🔴", "⚪"
_SHEET_FRESH_SECONDS = 3600
_SHEET_QUEUE_STALE_MINUTES = 5


def _row(light: str, text: str, fix=None) -> dict:
    """Строка светофора; `fix` — (подпись кнопки, callback_data) для жёлтой/красной строки
    или список таких пар (кнопки одним рядом; у зелёной строки — «Изменить» без цвета)."""
    return {"light": light, "text": text, "fix": fix}


def _days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


async def _edit_cb(key: str, code: str | None) -> str:
    """Правка для города СВЕТОФОРА, а не шапки админки (handlers/forum/admin_forum_date.py)."""
    if code and await cities_module_on():
        from handlers.forum.admin_forum_date import city_edit_callback
        return city_edit_callback(key, code)
    return f"settings_edit:{key}"


async def _row_forum_date(code: str | None) -> dict:
    date_str = await forum_date_for(code)
    date_cb, days_cb = await _edit_cb("forum_date", code), await _edit_cb("sos_active_days", code)
    if not date_str:
        return _row(RED, "Дата форума не задана — без неё не уйдут QR и шпаргалка",
                    ("🗓 Задать дату форума", date_cb))
    from services.sos import sos_active_window
    window = await sos_active_window(code)
    if window is None:  # дата не парсится — тот же случай, что «не задана»
        return _row(RED, f"Дата форума не читается ({html.escape(date_str)}) — задайте заново",
                    ("🗓 Задать дату форума", date_cb))
    start, end = window
    days = (end - start).days + 1
    span = start.strftime("%d.%m") if days == 1 else f"{start:%d.%m}–{end:%d.%m}"
    today = msk_now().date()
    if end < today:
        # Дата прошлого форума, которую забыли обновить, молча выключает рассылки QR и
        # шпаргалки — ловим её здесь, а не в день форума.
        return _row(YELLOW, f"Дата форума прошла ({span}) — это прошлый форум? Обновите дату",
                    ("🗓 Обновить дату форума", date_cb))
    edit = [("🗓 Изменить дату", date_cb), ("🗓 Сколько дней идёт", days_cb)]
    tail = " — идёт сегодня" if start <= today else ""
    if days != 1:
        # Не ошибка (у Москвы два дня законно), но жёлтым: однодневный региональный форум с
        # длиной 2 получает второй «день форума» — отчёт «пришли 0», SOS и меню ещё сутки.
        return _row(YELLOW, f"Форум: {span} ({_days_word(days)}){tail} — ⚠️ дольше одного дня. "
                    "Если форум однодневный, поправьте «Сколько дней идёт»", edit)
    return _row(GREEN, f"Форум: {span} ({_days_word(days)}){tail}", edit)


def _job_next_run(job_id: str):
    from services import scheduler as _sched
    job = _sched.get_scheduler().get_job(job_id)
    return getattr(job, "next_run_time", None) if job is not None else None


async def _row_qr(code: str | None) -> dict:
    if await get_setting_typed("checkin_qr_enabled") != "on":
        return _row(RED, "Вход по QR выключен — делегаты не получат QR",
                    ("🎟 Включить вход по QR", f"forumfn_qr:{_encode_city(code)}"))
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
        # При сессиях делегаты города видят только СВОЁ фото города, общее — нет.
        if await own_program_photo(code):
            tail = ", есть фото"
        elif photo:
            tail = ". Общее фото делегатам города не показывается — загрузите фото для города"
        else:
            tail = ""
        return _row(GREEN, f"Программа заведена: сессий {sessions}{tail}")
    if photo:
        return _row(YELLOW, "Есть только фото программы — сессий нет, на сессиях отмечать некуда", fix)
    return _row(RED, "Программа не заведена — ни сессий, ни фото", fix)


async def _row_sos(code: str | None, bot) -> dict:
    # Город светофора — в кнопке (светофор Тюмени при шапке «СПб» настраивает SOS Тюмени).
    fix = ("🆘 Настройки SOS", f"asos_city:{code}" if code and await cities_module_on() else "admin_sos")
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
    row = await _row_sheet_write()
    # Ждущие строку в листе (делегата ещё нет в таблице) — не затор записи: их бот держит до
    # 7 дней, и в «таблица не принимает запись» они бы горели жёлтым всю неделю. Отдельной
    # припиской к любой строке, цвет не меняют.
    if row["light"] != GRAY:
        from services.sheet_arrival_sync import MISSING_ERROR
        no_row = await sheet_arrival_count_with_error(MISSING_ERROR)
        if no_row:
            row["text"] += (f"\n⏳ «Пришёл» ждут своей строки в листе: {no_row} — этих делегатов ещё "
                            "нет в таблице, бот допишет отметку, когда строка появится")
    return row


async def _row_sheet_write() -> dict:
    if not _sheet_target.sheets_enabled():
        return _row(GRAY, "Таблица не подключена")
    from services.sheets import last_write_state
    from services.sheet_arrival_sync import MISSING_ERROR
    state = last_write_state()
    now = time.time()
    ok, fail = state.get("ok"), state.get("fail")
    # «Пришёл» пишется в лист джобой из очереди (services/sheet_arrival_sync.py) — застрявшая
    # очередь значит, что отметки входа до таблицы не доходят.
    queued, oldest = await sheet_arrival_queue_stats(exclude_error=MISSING_ERROR)
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
    # Сначала кнопки красных/жёлтых строк, потом «Изменить» у зелёных (строка даты форума).
    for problem in (True, False):
        for r in rows:
            if not r["fix"] or (r["light"] in (RED, YELLOW)) != problem or r["light"] == GRAY:
                continue
            fixes = r["fix"] if isinstance(r["fix"], list) else [r["fix"]]
            prefix = f"{r['light']} " if problem else ""
            row_btns = []
            for text, cb in fixes:
                if cb not in seen and visible(cb):
                    seen.add(cb)
                    row_btns.append(InlineKeyboardButton(text=f"{prefix}{text}", callback_data=cb))
            if row_btns:
                buttons.append(row_btns)
    buttons.append([InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"forum_ready_re:{_encode_city(code)}")])
    # Назад — в хаб того же города (светофор открывается из хаба), а не в выбор города.
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"forumfn_back:{_encode_city(code)}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("forum_ready:"))
async def forum_ready_open(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await render_ready(callback.from_user.id, code, callback.bot)
    from handlers.forum.admin_forum_hub_nav import edit_or_answer  # правкой хаба, а не новым сообщением
    await edit_or_answer(callback, text, kb)
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
