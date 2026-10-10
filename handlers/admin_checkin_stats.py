"""«📊 Статистика прихода» (бэклог чек-ина п.10): для менеджера в день форума и после —
одобрено / пришли / не пришли и % явки по городу, люди по дням форума, отметки на сессиях
программы с заполненностью зала. Компактное сообщение (итоги + топ сессий) и CSV со всем
остальным. Числа — общие запросы `shared/arrival_stats.py` (их же показывает дашборд, блок «Приход»),
исполняет `services/checkin_arrival.py`.

Город — из шапки: закреплённый за менеджером город / модуль городов выключен — один отчёт;
иначе «Все города» построчно по включённым городам + итог (та же развилка, что у счётчика
`handlers.admin_checkin._counter_line`).

Форма шва — как у соседей (`handlers/admin_forum_functions.py`): своего `Router()` нет,
`from handlers.admin import router`, импорт из хвоста `handlers/admin.py`. Право —
`moderate_reg`, как у остальных менеджерских экранов чек-ина (рассылки QR, «Не пришли»), не
волонтёрское `checkin`: волонтёру на стойке сводка по всему форуму не нужна."""
import html
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import shared.arrival_stats as arrival_stats
from cities import cities_module_on, city_label, city_labels_map, city_scope, enabled_cities
from handlers.admin import router
from handlers.admin_core import _admin_city_scope
from services.checkin_arrival import arrival_report
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

_TOP_SESSIONS = 8
_TITLE_LIMIT = 40


async def _collect(admin_id: int) -> tuple[str | None, list[tuple[str, dict]], dict, dict]:
    """(подпись города в заголовке | None, [(подпись, отчёт)] по городам, итог, код -> подпись).
    Для одного города список из одной пары, итог — он же."""
    own_scope = await _admin_city_scope(admin_id)
    if own_scope is not None:
        code = own_scope[0]
        label = await city_label(code)
        rep = await arrival_report(own_scope, code)
        return label, [(label, rep)], rep, {code: label}
    if not await cities_module_on():
        rep = await arrival_report(None, None)
        # Подписи всех городов реестра — колонка «Город» у сессий (CSV) и при выключенном
        # модуле городов показывает «Москва», а не код «msk».
        return None, [("Весь форум", rep)], rep, await city_labels_map()
    per_city: list[tuple[str, dict]] = []
    labels: dict = {}
    for c in await enabled_cities():
        code = c["code"]
        label = await city_label(code)
        labels[code] = label
        per_city.append((label, await arrival_report(city_scope(code), code)))
    total = arrival_stats.merge_reports([rep for _l, rep in per_city])
    return "Все города", per_city, total, labels


def _arrived_line(rep: dict) -> str:
    pct = f" ({rep['pct']}%)" if rep["pct"] is not None else ""
    return f"пришли {rep['arrived']} из {rep['approved']}{pct}"


def _session_line(s: dict, city_prefix: str | None) -> str:
    title = s["title"]
    if len(title) > _TITLE_LIMIT:
        title = title[: _TITLE_LIMIT - 1].rstrip() + "…"
    hall = f" · {html.escape(s['hall'])}" if s["hall"] else ""
    prefix = f"{html.escape(city_prefix)} · " if city_prefix else ""
    if s["capacity"]:
        count = f"{s['count']} из {s['capacity']} ({s['fill_pct']}%)"
    else:
        count = str(s["count"])
    return f"• {prefix}{s['day_label']} {s['start']}–{s['end']}{hall} · {html.escape(title)} — {count}"


async def render_stats(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    header, per_city, total, labels = await _collect(admin_id)
    lines = ["📊 <b>Статистика прихода</b>" + (f" — {html.escape(header)}" if header else "")]
    lines.append(f"Обновлено {msk_now().strftime('%H:%M:%S')} МСК")
    lines.append("")

    multi = len(per_city) > 1
    if multi:
        shown = [(label, rep) for label, rep in per_city if rep["approved"] or rep["has_marks"]]
        for label, rep in shown:
            lines.append(f"{html.escape(label)}: {_arrived_line(rep)}")
        lines.append(f"<b>Итого:</b> {_arrived_line(total)}, не пришли {total['not_arrived']}")
    else:
        lines.append(f"🚪 Вход: {_arrived_line(total)}")
        lines.append(f"Не пришли: {total['not_arrived']}")
    if total["approx"]:
        lines.append(
            f"⏱ У {total['approx']} отметок время примерное — в файле сканера не было времени "
            "скана, взято время загрузки."
        )

    if total["days"]:
        lines.append("")
        lines.append("<b>По дням</b> (пришли — вход в этот день · впервые на форуме):")
        for d in total["days"]:
            lines.append(f"{d['label']}: {d['present']} · впервые {d['first_entry']}")

    sessions = total["sessions"]
    if sessions:
        top = arrival_stats.top_sessions(total, _TOP_SESSIONS)
        lines.append("")
        if len(sessions) > len(top):
            lines.append(f"<b>Сессии</b> — топ-{len(top)} из {len(sessions)} по отметкам:")
        else:
            lines.append("<b>Сессии</b> — отметились:")
        for s in top:
            lines.append(_session_line(s, labels.get(s["city"]) if multi else None))
        if len(sessions) > len(top):
            lines.append("Остальные сессии — в выгрузке CSV.")

    lines.append("")
    if not total["has_marks"]:
        lines.append("Отметок пока нет.")
    lines.append(
        "<i>Если регион отмечает офлайн-сканером, числа растут после загрузки файла в "
        "«✅ Отметки на форуме».</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="checkin_stats_refresh")],
        [InlineKeyboardButton(text="📥 Выгрузить CSV", callback_data="checkin_stats_csv")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_checkin")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data == "checkin_stats")
async def checkin_stats_open(callback: types.CallbackQuery):
    text, kb = await render_stats(callback.from_user.id)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "checkin_stats_refresh")
async def checkin_stats_refresh(callback: types.CallbackQuery):
    text, kb = await render_stats(callback.from_user.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await callback.answer("Обновлено")


@router.callback_query(F.data == "checkin_stats_csv")
async def checkin_stats_csv(callback: types.CallbackQuery):
    header, per_city, total, labels = await _collect(callback.from_user.id)
    data = arrival_stats.csv_bytes(per_city, labels, total if len(per_city) > 1 else None)
    stamp = msk_now().strftime("%Y-%m-%d_%H-%M")
    await callback.message.answer_document(
        BufferedInputFile(data, filename=f"prihod_{stamp}.csv"),
        caption="📥 Статистика прихода: итоги, по дням, по сессиям. Время — МСК.",
    )
    await callback.answer()
