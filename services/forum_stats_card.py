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

Бренд «Юлид»/«АЙСЕК» — КИРИЛЛИЦЕЙ на картинке всегда, в том числе в EN-версии (уточнение
координатора 25.09, закон РФ + правило проекта CLAUDE.md): латиница («YouLead») на карточку не
попадает вовсе — см. `_LABELS["en"]["title"]`.

Подписи карточки (шесть коротких фраз) — код-литералы ЭТОГО модуля, не реестр и не
`services/i18n_form_manual.py`: текст рисуется ПИКСЕЛЯМИ, никогда не идёт через
`handlers.reg_i18n.tr_text`/`services.i18n.tr`, поэтому DB-перевод (ярус B) здесь не участвует
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
"""
from __future__ import annotations

import asyncio
import io
import logging
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
from services.timeutil import msk_now
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

CARD_WIDTH = 1080
CARD_HEIGHT = 1350

_FONT_TITLE = "miniapp/static/fonts/raleway-800.woff2"
_FONT_LABEL = "miniapp/static/fonts/lato-400.woff2"
_FONT_VALUE = "miniapp/static/fonts/lato-700.woff2"

# Реестровый суффикс photo-записи — тот же, что у program/speakers/venue
# (handlers/admin_settings.py::PHOTO_FIELDS/settings_receive_photo).
BACKGROUND_SETTING_KEY = "forum_stats_card_photo_file_id"


# ── Подписи (RU/EN код-литералы, см. докстринг модуля) ──────────────────────────────────────

_LABELS: dict[str, dict[str, str]] = {
    "ru": {
        "title": "Юлид в цифрах",
        "days": "Дней на форуме",
        "sessions": "Сессий посетил(а)",
        "hall": "Любимый зал",
        "coins": "Монет заработано",
        "rank": "Место в рейтинге",
        "since": "С нами с",
        "rank_fmt": "{rank} из {total}",
    },
    "en": {
        # Бренд кириллицей и в EN-версии тоже (см. докстринг модуля) — не "YouLead in numbers".
        "title": "Юлид in numbers",
        "days": "Forum days",
        "sessions": "Sessions attended",
        "hall": "Favorite hall",
        "coins": "Coins earned",
        "rank": "Leaderboard place",
        "since": "With us since",
        "rank_fmt": "{rank} of {total}",
    },
}


def label_set(lang: str) -> dict[str, str]:
    """`lang == "en"` -> английские подписи (бренд всё равно кириллицей); всё остальное
    (`"ru"`, `"ask"`, неизвестное) -> русские, fail-soft тот же, что у остального проекта."""
    return _LABELS["en"] if lang == "en" else _LABELS["ru"]


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
    # (database.db.finalize_registration), не полную историю сезонов делегата.
    since = (user.get("prev_season") or user.get("season") or "").strip() or None
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
    import cities as _cities

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
    рассылкой (handlers/admin_forum_stats_card.py)."""
    all_approved = await eligible_recipients(city, only_arrived=False)
    arrived = await eligible_recipients(city, only_arrived=True)
    return {"all": len(all_approved), "arrived": len(arrived)}


async def enabled_for(city: str | None) -> bool:
    from cities import get_setting_typed_for_city
    return await get_setting_typed_for_city("forum_stats_card_enabled", city) == "on"


async def sent_summary(city: str | None) -> dict:
    """«Отправлено N» — знаменатель «из M» считает вызывающий сам (`audience_counts`)."""
    import cities as _cities

    season = (await get_setting_typed("event_season") or "").strip()
    return await forum_stats_card_summary(season, city_scope=_cities.city_scope(city))


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


def render_card_sync(
    stats: dict[str, Any], background_bytes: bytes | None, lang: str, accent_hex: str,
) -> bytes:
    """Чистая (без БД/сети) синхронная функция — единственная, что зовёт `asyncio.to_thread`.
    Никогда не падает на длинном имени/пустых данных/отсутствующем фоне (см. докстринг
    модуля) — недостающие строки просто не рисуются, длинные — обрезаются `_truncate`."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    labels = label_set(lang)
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

    title_font = ImageFont.truetype(_FONT_TITLE, 74)
    name_font = ImageFont.truetype(_FONT_TITLE, 50)
    label_font = ImageFont.truetype(_FONT_LABEL, 32)
    value_font = ImageFont.truetype(_FONT_VALUE, 52)

    white = (255, 255, 255, 255)
    muted_white = (255, 255, 255, 205)
    pad = 72
    content_width = width - 2 * pad

    y = 96
    draw.text((pad, y), _truncate(draw, labels["title"], title_font, content_width), font=title_font, fill=white)
    y += 108

    name = stats.get("name")
    if name:
        draw.text((pad, y), _truncate(draw, str(name), name_font, content_width), font=name_font, fill=white)
        y += 78

    y += 36

    rows: list[tuple[str, str]] = []
    if stats.get("days"):
        rows.append((labels["days"], str(stats["days"])))
    if stats.get("sessions"):
        rows.append((labels["sessions"], str(stats["sessions"])))
    if stats.get("hall"):
        rows.append((labels["hall"], str(stats["hall"])))
    if stats.get("coins") is not None:
        rows.append((labels["coins"], str(stats["coins"])))
    if stats.get("rank"):
        rank_text = labels["rank_fmt"].format(rank=stats["rank"], total=stats.get("rank_total") or "—")
        rows.append((labels["rank"], rank_text))
    if stats.get("since"):
        rows.append((labels["since"], str(stats["since"])))

    row_height = 130
    row_gap = 22
    # Плашки — отдельным слоем с альфа-смешиванием: ImageDraw на RGBA не смешивает, а
    # заменяет пиксели, и полупрозрачная заливка превращалась в сплошной чёрный прямоугольник.
    cards = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    cards_draw = ImageDraw.Draw(cards)
    cy = y
    for _ in rows:
        cards_draw.rounded_rectangle(
            (pad, cy, width - pad, cy + row_height), radius=28, fill=(255, 255, 255, 34),
        )
        cy += row_height + row_gap
    img = Image.alpha_composite(img, cards)
    draw = ImageDraw.Draw(img)
    for label_text, value_text in rows:
        draw.text((pad + 32, y + 20), _truncate(draw, label_text, label_font, content_width - 64), font=label_font, fill=muted_white)
        draw.text((pad + 32, y + 60), _truncate(draw, value_text, value_font, content_width - 64), font=value_font, fill=white)
        y += row_height + row_gap

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def _load_background_bytes() -> bytes | None:
    file_id = await get_setting(BACKGROUND_SETTING_KEY)
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
        logger.error(f"forum_stats_card: не удалось скачать фон ({e}) — однотонный фон бренда")
        return None


async def _brand_colors() -> str:
    """Акцент активного пресета Mini App (`web_theme.PRESETS`) — читаем РЕАЛЬНО сохранённые
    ручки темы, не хардкодим пресет "youlead": бот универсальный (CLAUDE.md), карточка обязана
    красить фон в цвет ТЕКУЩЕГО события, не только YouLead."""
    import web_theme

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


async def render_preview(lang: str = "ru") -> bytes:
    background = await _load_background_bytes()
    accent = await _brand_colors()
    return await asyncio.to_thread(render_card_sync, _PREVIEW_STATS, background, lang, accent)


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
        from handlers import reg_i18n
        from services import i18n as i18n_service
        from services import quiet_hours
        from cities import get_setting_typed_for_city

        # Фон/акцент читаются ОДИН раз на всю рассылку (не на каждого делегата) — фон качается
        # из Telegram один раз, а не N раз подряд.
        background = await _load_background_bytes()
        accent = await _brand_colors()
        caption_base = await get_setting_typed_for_city("forum_stats_card_caption_text", city)

        now = msk_now()
        muted = await get_muted_today_ids(now.strftime("%Y-%m-%d"))

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
                png = await asyncio.to_thread(render_card_sync, stats, background, render_lang, accent)
                caption = reg_i18n.tr_fmt(caption_base, lang, tr_map, name=stats.get("name") or "")
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
