"""«📍 Сейчас на площадке» (бэклог чек-ина №12): живая картина дня форума для DXP/ОК —
сколько одобренных пришли сегодня и сколько за последние 15 минут, какие сессии идут сейчас
и сколько на них отметилось, сколько отметила каждая стойка входа. Всё за СЕГОДНЯ (МСК): вход
отмечается каждый день форума. Числа — общие запросы `shared/arrival_stats.py` (их же рисует дашборд
в разделе «Приход»), исполняет `services/checkin_arrival.py`.

Город — из шапки, та же развилка, что у «📊 Статистика прихода» (`handlers/admin_checkin_stats.py`):
закреплённый город / модуль выключен — один отчёт, иначе «Все города» с построчной разбивкой.

Форма шва — как у соседей: своего `Router()` нет, `from handlers.admin import router`, импорт из
хвоста `handlers/admin.py`. Право — `moderate_reg`, как у статистики прихода."""
import html
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import shared.arrival_stats as arrival_stats
from cities import cities_module_on, city_label, city_scope, enabled_cities
from handlers.admin import router
from handlers.admin_core import _admin_city_scope
from services.checkin_arrival import floor_report
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

_TOP_STANDS = 10
_TITLE_LIMIT = 40


async def _collect(admin_id: int, now) -> tuple[str | None, list[tuple[str, dict]], dict, dict]:
    """(подпись города | None, [(подпись, отчёт)] по городам, итог, код -> подпись)."""
    own_scope = await _admin_city_scope(admin_id)
    if own_scope is not None:
        code = own_scope[0]
        label = await city_label(code)
        rep = await floor_report(own_scope, code, now)
        return label, [(label, rep)], rep, {code: label}
    if not await cities_module_on():
        rep = await floor_report(None, None, now)
        return None, [("Весь форум", rep)], rep, {}
    per_city: list[tuple[str, dict]] = []
    labels: dict = {}
    for c in await enabled_cities():
        code = c["code"]
        label = await city_label(code)
        labels[code] = label
        per_city.append((label, await floor_report(city_scope(code), code, now)))
    total = arrival_stats.merge_floors([rep for _l, rep in per_city])
    # Стойки — один запрос без городского фильтра: волонтёр мог отмечать делегатов разных городов.
    total["stands"] = (await floor_report(None, None, now))["stands"]
    return "Все города", per_city, total, labels


def _present_line(rep: dict) -> str:
    pct = f" ({rep['pct']}%)" if rep["pct"] is not None else ""
    return f"{rep['present']} из {rep['approved']}{pct}"


def _session_line(s: dict, city_prefix: str | None) -> str:
    title = s["title"]
    if len(title) > _TITLE_LIMIT:
        title = title[: _TITLE_LIMIT - 1].rstrip() + "…"
    hall = f" · {html.escape(s['hall'])}" if s["hall"] else ""
    prefix = f"{html.escape(city_prefix)} · " if city_prefix else ""
    count = f"{s['count']} из {s['capacity']} ({s['fill_pct']}%)" if s["capacity"] else str(s["count"])
    return f"• {prefix}{s['start']}–{s['end']}{hall} · {html.escape(title)} — отметились {count}"


def _stand_line(pos: int, st: dict) -> str:
    """Стойка: сканов за день, темп (медиана интервала), простой (бэклог №13)."""
    line = f"{pos}. {html.escape(st['name'])} — {st['count']}"
    if st["median_gap_sec"] is not None:
        line += f" · {arrival_stats.gap_text(st['median_gap_sec'])}"
    if st["idle"]:
        line += f" · ⏸ простаивает {st['since_last_min']} мин"
    return line


async def render_floor(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    now = msk_now()
    header, per_city, total, labels = await _collect(admin_id, now)
    lines = ["📍 <b>Сейчас на площадке</b>" + (f" — {html.escape(header)}" if header else "")]
    lines.append(
        f"Сегодня {arrival_stats.day_short(now.strftime('%Y-%m-%d'))}, "
        f"обновлено {now.strftime('%H:%M:%S')} МСК"
    )
    lines.append("")
    lines.append(f"🚪 Сегодня пришли {_present_line(total)} одобренных")
    lines.append(f"За последние {arrival_stats.RECENT_MINUTES} мин: +{total['recent']}")
    multi = len(per_city) > 1
    if multi:
        for label, rep in per_city:
            if rep["approved"] or rep["present"]:
                lines.append(f"{html.escape(label)}: {_present_line(rep)}, +{rep['recent']}")

    lines.append("")
    live = total["live_sessions"]
    if live:
        lines.append("<b>Идут сейчас</b>:")
        for s in live:
            lines.append(_session_line(s, labels.get(s["city"]) if multi else None))
    else:
        lines.append("Сейчас ни одна сессия программы не идёт.")

    stands = total["stands"]
    lines.append("")
    if stands:
        top = stands[:_TOP_STANDS]
        title = (f"<b>Стойки входа</b> — топ-{len(top)} из {len(stands)}, отметили за день:"
                 if len(stands) > len(top) else "<b>Стойки входа</b> — отметили за день:")
        lines.append(title)
        for pos, st in enumerate(top, 1):
            lines.append(_stand_line(pos, st))
        # Простаивающая стойка вне топа тоже должна быть видна — ради неё экран и открывают.
        for pos, st in enumerate(stands[len(top):], len(top) + 1):
            if st["idle"]:
                lines.append(_stand_line(pos, st))
        lines.append(
            f"<i>«Раз в …» — обычный интервал между сканами; ⏸ — стойка не сканирует дольше "
            f"{arrival_stats.IDLE_MINUTES} мин, хотя другие за это время сканировали.</i>"
        )
    else:
        lines.append("Сегодня сканером и поиском по фамилии ещё никого не отметили.")
    lines.append("")
    lines.append(
        "<i>Отметки из файла офлайн-сканера входят в «пришли», но не в стойки: у них бывает "
        "примерное время.</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="checkin_floor_refresh")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_checkin")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data == "checkin_floor")
async def checkin_floor_open(callback: types.CallbackQuery):
    text, kb = await render_floor(callback.from_user.id)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "checkin_floor_refresh")
async def checkin_floor_refresh(callback: types.CallbackQuery):
    text, kb = await render_floor(callback.from_user.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await callback.answer("Обновлено")
