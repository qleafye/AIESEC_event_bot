"""Идея №29 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): «Твой Юлид в
цифрах» — картинка-итог делегату после форума (сколько дней пришёл, сколько сессий посетил,
любимый зал, монеты, место в рейтинге, «с нами с …»).

Данные — ТОЛЬКО реальные: `collect_stats` пропускает (возвращает `None`) каждую строку, для
которой у делегата нет данных (не отмечался на входе -> нет строки «дней», ни одной сессии ->
нет ни «сессий», ни «любимого зала», ни одной строки в леджере монет -> нет ни «монет», ни
«места в рейтинге»); рендер рисует только то, что реально есть.

Рейтинг — СТРОГО собственное место делегата (уточнение координатора 25.09): «место N из M»,
никаких чужих имён/топа на карточке нет вовсе — `get_user_rank`/`get_leaderboard` дают ровно
эти два числа, сама функция никогда не тянет чужие строки леджера на отображение.

Название мероприятия на картинке — из «🎪 Название мероприятия» (`event_name`) как есть, в том
числе в EN-версии (уточнение координатора 25.09: бренд кириллицей, закон РФ + правило проекта
CLAUDE.md) — см. `_title_text`/`_footer_line`. Не задано — нейтральные «Итоги в цифрах» и футер
без названия.

Подписи карточки (шесть коротких фраз) — код-литералы ЭТОГО модуля, не реестр и не
`services/i18n_form_manual.py`: текст рисуется ПИКСЕЛЯМИ, никогда не идёт через
`handlers.i18n.reg_i18n.tr_text`/`services.i18n.tr`, поэтому DB-перевод (ярус B) здесь не участвует
— задание допускало оба варианта («ключи реестра или литералы с ручным EN»), решение
зафиксировано как деviation в отчёте исполнителя. ПОДПИСЬ к самому фото (сообщение, которое
реально уходит боту делегата) — обычный реестровый ключ `forum_stats_card_caption_text`,
переводится штатно через `tr_fmt` (плейсхолдер `{name}` — ПОСЛЕ перевода, как везде в проекте).

Фон — глобальная photo-запись реестра `forum_stats_card` (D-10: медиа-ключ мимо обычного
резолвера, per_city на photo/file запрещён `tests/test_settings_percity_resolver.py::
test_no_per_city_key_is_photo_or_file_type`). Без загруженного фона — однотонный фон брендового
акцента активного пресета Mini App (`web_theme.PRESETS`), НЕ цвет RealTalk, если активен другой
пресет — читаем реально сохранённые ручки темы, не хардкодим один пресет.

Рендер — Pillow, в `asyncio.to_thread` (CPU-bound, не блокирует event loop рассылки). Шрифты —
те же woff2, что грузит Mini App (`miniapp/static/fonts/*.woff2`); Pillow/FreeType открывает
woff2 напрямую и корректно рендерит кириллицу (проверено вручную при разработке — открывает без
конвертации в ttf, тест `test_font_renders_cyrillic_glyph` в тестах модуля проверяет это же на
голом уровне глифа).

Рассылка — идемпотентна по (делегат, сезон) (`database.db.forum_stats_card_sends`,
`UNIQUE(telegram_id, season)`), захват от двойного тапа — `asyncio.Lock` НА ГОРОД (тот же приём,
что `services.checkin_broadcast.send_broadcast`). В ОТЛИЧИЕ от QR (D-35: служебное сообщение,
тихие часы и «🔕» не действуют) — эта рассылка ОБЫЧНАЯ: уважает `services.quiet_hours`
(делегата в окне тишины пропускаем, НЕ отмечаем отправленным — следующий тап подхватит) и
«🔕 Не присылать сегодня» (`database.db.get_muted_today_ids`) — этот модуль поэтому явно
добавлен во владельцы механизма «🔕» в
`tests/test_broadcast_mute_system_isolation_260924.py::_ALLOWED_OWNERS`.

Редизайн композиции (правка координатора 25.09, «делегат должен ЗАХОТЕТЬ поделиться в
сторис, не отчёт»): одна крупная АКЦЕНТНАЯ цифра-герой (дней на форуме, а если дней нет —
сессий) оранжевым `#F48924` — ЕДИНСТВЕННАЯ оранжевая деталь карточки, всё остальное синее/
белое, чёрного нет вовсе. Остальные показатели — плашками в 2 колонки, раскладка адаптивна к
их числу (1–5, последняя нечётная — во всю ширину, без дыр). Нулевые значения (проверено
только у монет — `days`/`sessions`/`rank` и так `None` при нуле, см. `collect_stats`) на
КАРТИНКЕ не показываются — `collect_stats` продолжает отличать «нет данных» от настоящего
нуля для остальных потребителей (тест `test_collect_stats_coins_zero_after_debit_is_real_data_
not_missing` не трогать), фильтр нуля — только в `render_card_sync`. Низ карточки — лого
мероприятия (`miniapp_logo`, тот же download-приём, что фон) + строка «Юлид · Город, даты»
(`_footer_line`/`_resolve_footer_parts`): город — свой (`cities.city_label_or_none`,
переведённый через `services.i18n.tr` тем же `tr_map`, что и остальной делегатский текст —
город УЖЕ зарегистрирован для перевода в `services.i18n_sources.city_texts`, в отличие от
шести фиксированных подписей этого модуля выше), даты — окно форума
`services.sos.sos_active_window(city)` (`forum_date` + `sos_active_days`), месяц — родительный
падеж (RU) / «Month D» (EN), обе таблицы месяцев — код-литералы этого модуля (тот же довод, что
`services.applications._MONTH_NAMES_GENITIVE`: своя копия, не импорт).

«С нами с …» (сезон предыдущей регистрации) — сезонный код людям читаем ТОЛЬКО если это
свободный текст `event_season`, который администратор сам вписал при открытии сезона
(`season`/`prev_season` в `users` — снимок значения `event_season` НА МОМЕНТ регистрации,
отдельной таблицы код -> человеческое имя в проекте нет). Строка скрывается, если `since`
пуст ИЛИ совпадает (без учёта регистра/пробелов) с ТЕКУЩИМ `event_season` — это одновременно
покрывает «текущий сезон» и «первый сезон» (у новичка `since` = его же `season`, который при
регистрации всегда равен текущему `event_season`).
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
from collections import Counter
from typing import Any

from database.db import (
    first_entry_scanned_at,
    forum_stats_card_mark_sent,
    forum_stats_card_sent_ids,
    forum_stats_card_summary,
    get_balance,
    get_leaderboard,
    get_program_hall,
    get_program_session,
    get_setting,
    get_user_rank,
    list_approved_users,
    list_checkins_for_user,
)
from services import scheduler as _sched
from services.checkin import ENTRY_POINT, checkin_denial
from services.ru_plural import ru_plural
from services.text_fill import event_kind
from services.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

CARD_WIDTH = 1080
CARD_HEIGHT = 1350

_FONT_TITLE = "miniapp/static/fonts/raleway-800.woff2"
_FONT_LABEL = "miniapp/static/fonts/lato-400.woff2"
_FONT_VALUE = "miniapp/static/fonts/lato-700.woff2"

# Реестровый суффикс photo-записи — тот же, что у program/speakers/venue
# (handlers/admin_settings.py::PHOTO_FIELDS/settings_receive_photo).
BACKGROUND_SETTING_KEY = "forum_stats_card_photo_file_id"

# Лого мероприятия — общий ключ реестра Mini App (`settings_schema.SETTINGS_SCHEMA["miniapp_logo"]`,
# type="photo"), второго ключа под карточку не заводим (см. докстринг модуля).
LOGO_SETTING_KEY = "miniapp_logo"

# Единственная оранжевая деталь карточки (координатор 25.09) — фиксированный бренд-акцент
# АЙСЕК, НЕ цвет активного пресета Mini App (тот красит только фон-заглушку, см. `_brand_colors`).
_ORANGE_HEX = "#F48924"

# Месяцы для «30–31 октября» / «October 30–31» — код-литералы этого модуля, своя копия таблицы
# (тот же довод, что `services.applications._MONTH_NAMES_GENITIVE`: dashboard/miniapp/services
# исторически не делят модули друг с другом, здесь то же самое правило распространено на этот
# модуль — подписи карточки уже код-литералы, см. докстринг).
_MONTH_GENITIVE_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_MONTH_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


# ── Подписи (RU/EN код-литералы, см. докстринг модуля) ──────────────────────────────────────

_ru_plural = ru_plural  # общая функция склонения (services/ru_plural.py)


def _hero_caption(key: str, n: int, lang: str, event_type: str | None = None) -> str:
    """Подпись под цифрой-героем в согласии с числом: «1 день на форуме», «2 дня», «5 дней».
    Вид события — по «🎭 Тип события» («на конференции», «на мероприятии»)."""
    kind = event_kind(event_type, "en" if lang == "en" else "ru")
    if lang == "en":
        word = {"days": ("day", "days"), "sessions": ("session", "sessions")}[key]
        return f"{word[0] if int(n) == 1 else word[1]} at the {kind}"
    forms = {"days": ("день", "дня", "дней"), "sessions": ("сессия", "сессии", "сессий")}[key]
    return f"{_ru_plural(n, *forms)} на {kind}"


# Сезон вида «26/1» / «YL 26/1» — служебный код, людям непонятен: на картинке не показываем.
_SEASON_CODE_RE = re.compile(r"[A-Za-zА-Яа-я]{0,4}\s*\d{1,2}\s*/\s*\d{1,2}")


def _human_season(value: str | None) -> str | None:
    if not value or _SEASON_CODE_RE.fullmatch(value.strip()):
        return None
    return value.strip()


_LABELS: dict[str, dict[str, str]] = {
    "ru": {
        "title": "{event} в цифрах",
        "title_plain": "Итоги в цифрах",
        "days": "Дней на {kind}",
        "sessions": "Сессий на {kind}",
        "hall": "Любимый зал",
        "coins": "Баллов заработано",
        "rank": "Место в рейтинге",
        "since": "С нами с",
        "rank_fmt": "{rank} из {total}",
    },
    "en": {
        # Название мероприятия — как его вписал менеджер (кириллицей и в EN-версии, см.
        # докстринг модуля), без перевода.
        "title": "{event} in numbers",
        "title_plain": "Results in numbers",
        "days": "{Kind} days",
        "sessions": "{Kind} sessions",
        "hall": "Favorite hall",
        "coins": "Points earned",
        "rank": "Leaderboard place",
        "since": "With us since",
        "rank_fmt": "{rank} of {total}",
    },
}


def label_set(lang: str, event_type: str | None = None) -> dict[str, str]:
    """`lang == "en"` -> английские подписи (бренд всё равно кириллицей); всё остальное
    (`"ru"`, `"ask"`, неизвестное) -> русские, fail-soft тот же, что у остального проекта.
    `{kind}`/`{Kind}` — вид события по «🎭 Тип события» (`text_fill.event_kind`)."""
    en = lang == "en"
    kind = event_kind(event_type, "en" if en else "ru")
    return {
        k: v.replace("{kind}", kind).replace("{Kind}", kind.capitalize())
        for k, v in _LABELS["en" if en else "ru"].items()
    }


# ── Сбор данных делегата (только реальное — см. докстринг модуля) ───────────────────────────

async def collect_stats(user: dict) -> dict[str, Any]:
    tid = user["telegram_id"]
    rows = await list_checkins_for_user(tid)
    entry_days = {r["day"] for r in rows if r.get("point") == ENTRY_POINT}
    session_rows = [r for r in rows if r.get("point") != ENTRY_POINT]

    hall_counts: Counter[str] = Counter()
    for row in session_rows:
        point = row.get("point") or ""
        if not point.startswith("session:"):
            continue
        try:
            session_id = int(point.split(":", 1)[1])
        except ValueError:
            continue
        session = await get_program_session(session_id)
        hall_id = (session or {}).get("hall_id")
        if not hall_id:
            continue
        hall = await get_program_hall(hall_id)
        hall_name = (hall or {}).get("name")
        if hall_name:
            hall_counts[hall_name] += 1
    favorite_hall = hall_counts.most_common(1)[0][0] if hall_counts else None

    # get_user_rank возвращает None, если у делегата нет ни одной строки в леджере монет —
    # единый признак «данных нет» для ОБЕИХ строк (coins при этом может быть настоящим нулём
    # после списаний, это уже не «нет данных»).
    rank = await get_user_rank(tid)
    coins = await get_balance(tid) if rank is not None else None
    rank_total = len(await get_leaderboard(10_000)) if rank is not None else None

    # «С нами с …» — лучшее доступное приближение: prev_season хранит только ОДИН шаг назад
    # (database.db.finalize_registration), не полную историю сезонов делегата. Скрываем, если
    # пусто ИЛИ совпадает с текущим event_season (текущий/первый сезон — см. докстринг модуля).
    since = (user.get("prev_season") or user.get("season") or "").strip() or None
    if since:
        current_season = (await get_setting_typed("event_season") or "").strip()
        if current_season and since.lower() == current_season.lower():
            since = None
    name = (user.get("full_name") or "").strip() or None

    return {
        "name": name,
        "days": len(entry_days) or None,
        "sessions": len(session_rows) or None,
        "hall": favorite_hall,
        "coins": coins,
        "rank": rank,
        "rank_total": rank_total,
        "since": since,
    }


# ── Аудитория ─────────────────────────────────────────────────────────────────────────────

async def eligible_recipients(city: str | None, *, only_arrived: bool) -> list[dict]:
    """Одобренные текущего сезона города через `services.checkin.checkin_denial` НА КАЖДОЙ
    строке (единая точка правды допуска D-02, тот же приём, что `services.checkin_broadcast.
    eligible_recipients`) — `only_arrived=True` дополнительно требует хотя бы один вход
    (`database.db.first_entry_scanned_at`, любой день форума)."""
    import domain.cities as _cities

    candidates = await list_approved_users(city_scope=_cities.city_scope(city))
    eligible = [u for u in candidates if await checkin_denial(u) is None]
    if not only_arrived:
        return eligible
    arrived: list[dict] = []
    for user in eligible:
        if await first_entry_scanned_at(user["telegram_id"]) is not None:
            arrived.append(user)
    return arrived


async def audience_counts(city: str | None) -> dict:
    """«N пришли хотя бы раз / M всего одобрены» — для экрана выбора аудитории перед
    рассылкой (handlers/forum/admin_forum_stats_card.py)."""
    all_approved = await eligible_recipients(city, only_arrived=False)
    arrived = await eligible_recipients(city, only_arrived=True)
    return {"all": len(all_approved), "arrived": len(arrived)}


async def enabled_for(city: str | None) -> bool:
    from domain.cities import get_setting_typed_for_city
    return await get_setting_typed_for_city("forum_stats_card_enabled", city) == "on"


async def sent_summary(city: str | None) -> dict:
    """«Отправлено N» — знаменатель «из M» считает вызывающий сам (`audience_counts`)."""
    import domain.cities as _cities

    season = (await get_setting_typed("event_season") or "").strip()
    return await forum_stats_card_summary(season, city_scope=_cities.city_scope(city))


def format_forum_dates(start, end, lang: str) -> str:
    """«30–31 октября» (RU, родительный падеж) / «October 30–31» (EN) — окно форума `[start,
    end]` включительно (`services.sos.sos_active_window`). Один день -> без диапазона (
    «30 октября» / «October 30»); разные месяцы — оба месяца пишутся полностью («30 октября –
    1 ноября» / «October 30 – November 1»). Чистая функция (без БД) — вызывающий сам достаёт
    `start`/`end` через `_resolve_footer_parts`."""
    if lang == "en":
        months = _MONTH_EN
        if start == end:
            return f"{months[start.month - 1]} {start.day}"
        if start.month == end.month:
            return f"{months[start.month - 1]} {start.day}–{end.day}"
        return f"{months[start.month - 1]} {start.day} – {months[end.month - 1]} {end.day}"
    months = _MONTH_GENITIVE_RU
    if start == end:
        return f"{start.day} {months[start.month - 1]}"
    if start.month == end.month:
        return f"{start.day}–{end.day} {months[end.month - 1]}"
    return f"{start.day} {months[start.month - 1]} – {end.day} {months[end.month - 1]}"


def _footer_line(
    city_label_text: str | None, date_range_text: str | None, event_name: str | None = None,
) -> str:
    """«Юлид · Москва, 30–31 октября» (координатор 25.09) — название мероприятия из
    «🎪 Название мероприятия» (`event_name`), город/даты — только если реально известны
    (пропущенная часть просто не попадает в строку, без пустых «, »). Название не задано —
    строка без него: зашитый «Юлид» уезжал на карточку конференции и СкиллАпа."""
    tail = ", ".join(p for p in (city_label_text, date_range_text) if p)
    brand = (event_name or "").strip()
    return " · ".join(p for p in (brand, tail) if p)


def _title_text(labels: dict[str, str], event_name: str | None) -> str:
    """«Юлид в цифрах» при заданном названии мероприятия, иначе нейтральное «Итоги в цифрах»."""
    name = (event_name or "").strip()
    return labels["title"].replace("{event}", name) if name else labels["title_plain"]


async def _resolve_footer_parts(
    city: str | None, lang: str, tr_map: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Город делегата (переведённый тем же `tr_map`, что остальной делегатский текст — см.
    докстринг модуля) + даты форума этого города — единственная точка, где рендер карточки
    трогает БД/сеть за пределами `collect_stats`/фона/лого, поэтому вызывается из async-кода
    ДО `render_card_sync` (чистая синхронная функция)."""
    from domain.cities import city_label_or_none
    from services import i18n as i18n_service
    from services import sos as sos_service

    label = await city_label_or_none(city)
    if label:
        label = i18n_service.tr(label, lang, tr_map or {})
    window = await sos_service.sos_active_window(city)
    date_text = format_forum_dates(window[0], window[1], lang) if window else None
    return label, date_text


# ── Рендер (Pillow, синхронный ядро в asyncio.to_thread) ────────────────────────────────────

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or "#037EF3").lstrip("#")
    if len(h) != 6:
        h = "037EF3"
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _truncate(draw, text: str, font, max_width: int) -> str:
    """Одна строка, обрезанная многоточием, если не влезает — вместо переноса (длинное имя
    зала/делегата не должно ломать высоту фиксированной строки-плашки)."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if draw.textlength(candidate, font=font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ellipsis) if lo < len(text) else text


def _fit_font(draw, text: str, font_path: str, start_size: int, min_size: int, max_width: int):
    """Герой-цифра — редкий, но не невозможный случай трёхзначного числа сессий: уменьшаем
    кегль шагом 10, пока строка не влезет, вместо обрезки многоточием (обрезанная крупная
    цифра — самое заметное, что может сломаться на шеринговой картинке)."""
    from PIL import ImageFont

    size = start_size
    while size > min_size:
        font = ImageFont.truetype(font_path, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 10
    return ImageFont.truetype(font_path, min_size)


def render_card_sync(
    stats: dict[str, Any], background_bytes: bytes | None, lang: str, accent_hex: str,
    *,
    logo_bytes: bytes | None = None,
    city_label_text: str | None = None,
    date_range_text: str | None = None,
    event_name: str | None = None,
    event_type: str | None = None,
) -> bytes:
    """Чистая (без БД/сети) синхронная функция — единственная, что зовёт `asyncio.to_thread`.
    Никогда не падает на длинном имени/пустых данных/отсутствующем фоне/лого (см. докстринг
    модуля) — недостающие строки просто не рисуются, длинные — обрезаются `_truncate`.

    Композиция (координатор 25.09): заголовок+имя -> герой-цифра (дни, а если дней нет —
    сессии) ЕДИНСТВЕННЫМ оранжевым `_ORANGE_HEX` элементом карточки -> остальные показатели
    плашками в 2 колонки (нулевые/пустые пропускаются, последняя нечётная плашка — во всю
    ширину) -> футер, ПРИЖАТЫЙ К НИЗУ независимо от объёма контента выше (`footer_y = max(...)`)
    — нижняя треть карточки поэтому никогда не пустует: лого мероприятия (если задано) + строка
    `_footer_line`."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    labels = label_set(lang, event_type)
    width, height = CARD_WIDTH, CARD_HEIGHT

    base = None
    if background_bytes:
        try:
            opened = Image.open(io.BytesIO(background_bytes))
            opened = ImageOps.exif_transpose(opened) or opened
            base = ImageOps.fit(opened.convert("RGB"), (width, height), method=Image.LANCZOS)
        except Exception as e:
            logger.error(f"forum_stats_card.render_card_sync: не удалось открыть фон ({e}) — однотонный фон бренда")
            base = None
    if base is None:
        base = Image.new("RGB", (width, height), _hex_to_rgb(accent_hex))

    # Полупрозрачная затемняющая подложка — белый текст читаем и на светлом фото, и на фото
    # со сложным рисунком; на однотонном брендовом фоне просто слегка темнее исходного.
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 120))
    img = Image.alpha_composite(base.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(img)

    title_font = ImageFont.truetype(_FONT_TITLE, 66)
    name_font = ImageFont.truetype(_FONT_TITLE, 42)
    label_font = ImageFont.truetype(_FONT_LABEL, 28)
    value_font = ImageFont.truetype(_FONT_VALUE, 40)
    hero_caption_font = ImageFont.truetype(_FONT_LABEL, 46)
    footer_font = ImageFont.truetype(_FONT_LABEL, 30)

    white = (255, 255, 255, 255)
    muted_white = (255, 255, 255, 205)
    orange = _hex_to_rgb(_ORANGE_HEX) + (255,)
    pad = 72
    content_width = width - 2 * pad

    y = 96
    title_text = _truncate(draw, _title_text(labels, event_name), title_font, content_width)
    draw.text((pad, y), title_text, font=title_font, fill=white)
    y = draw.textbbox((pad, y), title_text, font=title_font)[3] + 26

    name = stats.get("name")
    if name:
        name_text = _truncate(draw, str(name), name_font, content_width)
        draw.text((pad, y), name_text, font=name_font, fill=white)
        y = draw.textbbox((pad, y), name_text, font=name_font)[3] + 24

    content_start = y  # верх зоны героя/плашек — низ шапки (заголовок + имя)

    # ── герой: единственная крупная цифра карточки — дни, а если дней нет, сессии ──
    stats = {**stats, "since": _human_season(stats.get("since"))}
    hero_key = "days" if stats.get("days") else ("sessions" if stats.get("sessions") else None)
    remaining_keys = [k for k in ("days", "sessions", "hall", "coins", "rank", "since") if k != hero_key]

    # ── плашки: всё, кроме героя, пустого и нуля (координатор 25.09: ноль на шеринговой
    # картинке не показываем, хотя collect_stats честно отличает 0 от «нет данных» — см.
    # докстринг модуля), 2 колонки, последняя нечётная — во всю ширину ──
    rows: list[tuple[str, str]] = []
    for key in remaining_keys:
        value = stats.get(key)
        if not value:
            continue
        if key == "rank":
            rank_text = labels["rank_fmt"].format(rank=value, total=stats.get("rank_total") or "—")
            rows.append((labels["rank"], rank_text))
        else:
            rows.append((labels[key], str(value)))

    # ── футер: геометрия считается ДО героя/плашек — прижат к низу карточки константным
    # отступом, героя/плашки размещаем ВЫШЕ него (а не «сверху вниз, что получится»), поэтому
    # нижняя треть никогда не пустует даже при минимуме данных (координатор 25.09) ──
    footer_text = _footer_line(city_label_text, date_range_text, event_name)
    footer_bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
    footer_h = footer_bbox[3] - footer_bbox[1]

    logo_img = None
    if logo_bytes:
        try:
            logo_img = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            max_h, max_w = 120, content_width
            ratio = min(max_h / logo_img.height, max_w / logo_img.width, 1.0)
            new_size = (max(1, round(logo_img.width * ratio)), max(1, round(logo_img.height * ratio)))
            logo_img = logo_img.resize(new_size, Image.LANCZOS)
        except Exception as e:
            logger.error(f"forum_stats_card.render_card_sync: не удалось открыть лого ({e}) — карточка без лого")
            logo_img = None
    logo_gap = 24
    logo_block_h = (logo_img.height + logo_gap) if logo_img is not None else 0
    footer_top = height - 88 - footer_h - logo_block_h  # верх (лого +) строки футера

    if hero_key:
        hero_text = str(stats[hero_key])
        hero_font = _fit_font(draw, hero_text, _FONT_TITLE, 340, 160, content_width)
        hero_num_h = draw.textbbox((0, 0), hero_text, font=hero_font)[3]
        caption_text = _truncate(draw, _hero_caption(hero_key, stats[hero_key], lang, event_type), hero_caption_font, content_width)
        cap_h = draw.textbbox((0, 0), caption_text, font=hero_caption_font)[3]
        hero_block_h = hero_num_h + 6 + cap_h

        if rows:
            # Есть плашки — герой сразу под шапкой, дальше плашки, как раньше.
            hero_y = content_start + 10
        else:
            # Плашек нет (мало данных) — герой ЦЕНТРИРУЕТСЯ в зоне между шапкой и футером,
            # чтобы единственная цифра не терялась в пустоте (правка после самопросмотра
            # min-примера: герой прижатый к шапке при пустой середине карточки выглядел
            # незаконченным).
            available = max(hero_block_h, footer_top - 40 - content_start)
            hero_y = content_start + max(10, (available - hero_block_h) // 2)

        draw.text((pad, hero_y), hero_text, font=hero_font, fill=orange)
        draw.text((pad, hero_y + hero_num_h + 6), caption_text, font=hero_caption_font, fill=muted_white)
        y = hero_y + hero_block_h + 48
    else:
        y = content_start + 20

    if rows:
        gap = 24
        tile_w = (content_width - gap) // 2
        tile_h = 140
        n = len(rows)
        positions: list[tuple[int, int, int, int]] = []
        for i in range(n):
            row = i // 2
            alone = (i == n - 1) and (n % 2 == 1)
            if alone:
                x0, x1 = pad, width - pad
            else:
                col = i % 2
                x0 = pad + col * (tile_w + gap)
                x1 = x0 + tile_w
            y0 = y + row * (tile_h + gap)
            y1 = y0 + tile_h
            positions.append((x0, y0, x1, y1))

        # Плашки — отдельным слоем с альфа-смешиванием: ImageDraw на RGBA не смешивает, а
        # заменяет пиксели, и полупрозрачная заливка превращалась в сплошной чёрный
        # прямоугольник (commit 8153797 — не регрессировать).
        cards = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        cards_draw = ImageDraw.Draw(cards)
        for x0, y0, x1, y1 in positions:
            cards_draw.rounded_rectangle((x0, y0, x1, y1), radius=26, fill=(255, 255, 255, 34))
        img = Image.alpha_composite(img, cards)
        draw = ImageDraw.Draw(img)

        for (label_text, value_text), (x0, y0, x1, y1) in zip(rows, positions):
            inner_w = (x1 - x0) - 64
            draw.text((x0 + 32, y0 + 20), _truncate(draw, label_text, label_font, inner_w), font=label_font, fill=muted_white)
            draw.text((x0 + 32, y0 + 58), _truncate(draw, value_text, value_font, inner_w), font=value_font, fill=white)
        rows_count = (n + 1) // 2
        y = y + rows_count * (tile_h + gap)

    # Контента оказалось больше, чем в среднем случае (герой + 5 плашек мелким шрифтом) —
    # футер сдвигается вниз вслед за контентом, а не наезжает на него (fail-soft край).
    footer_top = max(footer_top, y)

    if logo_img is not None:
        logo_x = pad + (content_width - logo_img.width) // 2
        img.alpha_composite(logo_img, (logo_x, footer_top))
        draw = ImageDraw.Draw(img)
        footer_y = footer_top + logo_img.height + logo_gap
    else:
        footer_y = footer_top

    footer_x = pad + (content_width - (footer_bbox[2] - footer_bbox[0])) // 2
    draw.text((footer_x, footer_y), footer_text, font=footer_font, fill=muted_white)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def _download_setting_file(setting_key: str, *, purpose: str) -> bytes | None:
    """Общий download-приём фона/лого — оба ключи реестра типа `"photo"`, скачиваются тем же
    путём (`bot.get_file` -> `bot.download_file`). `purpose` — только для лога, чтобы отличить
    сбой фона от сбоя лого."""
    file_id = await get_setting(setting_key)
    if not file_id:
        return None
    bot = _sched._bot
    if bot is None:
        return None
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"forum_stats_card: не удалось скачать {purpose} ({e}) — карточка без него")
        return None


async def _load_background_bytes() -> bytes | None:
    return await _download_setting_file(BACKGROUND_SETTING_KEY, purpose="фон")


async def _load_logo_bytes() -> bytes | None:
    return await _download_setting_file(LOGO_SETTING_KEY, purpose="лого")


async def _brand_colors() -> str:
    """Акцент активного пресета Mini App (`web_theme.PRESETS`) — читаем РЕАЛЬНО сохранённые
    ручки темы, не хардкодим пресет "youlead": бот универсальный (CLAUDE.md), карточка обязана
    красить фон в цвет ТЕКУЩЕГО события, не только YouLead."""
    import shared.web_theme as web_theme

    settings = {}
    for key in web_theme.THEME_KEYS.values():
        settings[key] = await get_setting_typed(key)
    resolved = web_theme.resolve_theme(settings)
    return resolved["accent"]


# Представительные данные превью менеджеру себе — он не обязан быть делегатом с реальными
# отметками/леджером, чтобы увидеть, как карточка выглядит с данными.
_PREVIEW_STATS: dict[str, Any] = {
    "name": "Иванова Мария", "days": 2, "sessions": 5, "hall": "Зал «Лидер»",
    "coins": 340, "rank": 12, "rank_total": 340, "since": "26/1",
}


async def render_preview(lang: str = "ru", city: str | None = None) -> bytes:
    background = await _load_background_bytes()
    logo = await _load_logo_bytes()
    accent = await _brand_colors()
    render_lang = "en" if lang == "en" else "ru"
    city_label_text, date_range_text = await _resolve_footer_parts(city, render_lang)
    from services.text_fill import event_name

    return await asyncio.to_thread(
        render_card_sync, _PREVIEW_STATS, background, lang, accent,
        logo_bytes=logo, city_label_text=city_label_text, date_range_text=date_range_text,
        event_name=await event_name(),
        event_type=await get_setting_typed("event_type"),
    )


# ── Рассылка (идемпотентная, захват двойного тапа, тихие часы + «🔕» — см. докстринг) ───────

_city_locks: dict[str | None, asyncio.Lock] = {}


def _get_city_lock(city: str | None) -> asyncio.Lock:
    lock = _city_locks.get(city)
    if lock is None:
        lock = asyncio.Lock()
        _city_locks[city] = lock
    return lock


async def send_broadcast(city: str | None, *, only_arrived: bool) -> dict:
    lock = _get_city_lock(city)
    if lock.locked():
        logger.info(f"forum_stats_card.send_broadcast({city!r}): уже идёт — повторный вызов отклонён")
        return {"sent": 0, "failed": 0, "quiet": 0, "muted": 0, "total": 0, "already_running": True}

    async with lock:
        bot = _sched._bot
        if bot is None:
            return {"sent": 0, "failed": 0, "quiet": 0, "muted": 0, "total": 0}
        if not await enabled_for(city):
            return {"sent": 0, "failed": 0, "quiet": 0, "muted": 0, "total": 0, "disabled": True}

        season = (await get_setting_typed("event_season") or "").strip()
        recipients = await eligible_recipients(city, only_arrived=only_arrived)
        already = await forum_stats_card_sent_ids(season)
        targets = [u for u in recipients if u["telegram_id"] not in already]
        if not targets:
            return {"sent": 0, "failed": 0, "quiet": 0, "muted": 0, "total": 0}

        from aiogram.types import BufferedInputFile
        from database.db import get_muted_today_ids
        from handlers.i18n import reg_i18n
        from services import i18n as i18n_service
        from services import quiet_hours
        from domain.cities import get_setting_typed_for_city

        # Фон/лого/акцент читаются ОДИН раз на всю рассылку (не на каждого делегата) — качаются
        # из Telegram один раз, а не N раз подряд.
        background = await _load_background_bytes()
        logo = await _load_logo_bytes()
        accent = await _brand_colors()
        caption_base = await get_setting_typed_for_city("forum_stats_card_caption_text", city)
        from services.text_fill import event_name, fill_event

        event_title = await event_name()
        event_type = await get_setting_typed("event_type")
        if not (caption_base or "").strip():
            # Экран обещает менеджеру «подпись пуста — рассылка НЕ уйдёт»; без этой проверки
            # уходило фото без подписи.
            logger.info(f"forum_stats_card.send_broadcast({city!r}): подпись пуста — не отправляем")
            return {"sent": 0, "failed": 0, "quiet": 0, "muted": 0, "total": 0, "empty_caption": True}

        now = msk_now()
        muted = await get_muted_today_ids(now.strftime("%Y-%m-%d"))

        # Город/даты футера кэшируются по (город делегата, язык рендера) — в рассылке «всем
        # городам» (city=None) у делегатов РАЗНЫЙ event_city, поэтому кэш не по city-параметру
        # функции, а по фактическому городу каждого получателя.
        footer_cache: dict[tuple[str | None, str], tuple[str | None, str | None]] = {}

        sent = failed = quiet_n = muted_n = 0
        for user in targets:
            tid = user["telegram_id"]
            if tid in muted:
                muted_n += 1
                continue
            if await quiet_hours.defer_until(now, tid) is not None:
                quiet_n += 1
                continue
            try:
                lang, tr_map = await i18n_service.context(tid)
                render_lang = "en" if lang == "en" else "ru"
                stats = await collect_stats(user)
                recipient_city = user.get("event_city")
                cache_key = (recipient_city, render_lang)
                if cache_key not in footer_cache:
                    footer_cache[cache_key] = await _resolve_footer_parts(recipient_city, render_lang, tr_map)
                city_label_text, date_range_text = footer_cache[cache_key]
                png = await asyncio.to_thread(
                    render_card_sync, stats, background, render_lang, accent,
                    logo_bytes=logo, city_label_text=city_label_text, date_range_text=date_range_text,
                    event_name=event_title,
                    event_type=event_type,
                )
                # Подпись уходит с parse_mode=HTML: «<» или «&» в имени давали 400 этому делегату.
                caption = reg_i18n.tr_fmt(
                    caption_base, lang, tr_map, name=html.escape(stats.get("name") or ""),
                )
                caption = fill_event(caption, event_title, lang, escape=True)
            except Exception as e:
                logger.error(f"forum_stats_card.send_broadcast: build for {tid} failed: {e}")
                failed += 1
                continue

            async def _factory(cid, _png=png, _caption=caption):
                return await bot.send_photo(
                    cid, BufferedInputFile(_png, filename="yulead_stats.png"), caption=_caption,
                )

            ok = await _sched._safe_send(_factory, tid)
            if ok:
                await forum_stats_card_mark_sent(
                    tid, user.get("event_city"), season, now.strftime("%Y-%m-%d %H:%M:%S"),
                )
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)

        logger.info(
            f"forum_stats_card.send_broadcast({city!r}): sent {sent}, failed {failed}, "
            f"quiet {quiet_n}, muted {muted_n} of {len(targets)}"
        )
        return {"sent": sent, "failed": failed, "quiet": quiet_n, "muted": muted_n, "total": len(targets)}
