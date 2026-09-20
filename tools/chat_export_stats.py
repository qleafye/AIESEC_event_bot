"""Статистика активности участников чата по экспорту Telegram Desktop.

Bot API не отдаёт историю чата за период ДО того, как в него добавили бота — единственный
источник для поста «самые активные делегаты чата» это ручной экспорт (Telegram Desktop → чат →
⋮ → «Экспорт истории чата» → формат «Machine-readable JSON»). Этот тул считает по такому
`result.json` воспроизводимый и объяснимый рейтинг: любой менеджер может прочитать в шапке
вывода формулу и веса и объяснить делегату, откуда взялся балл — а не сослаться на «на глаз».

Примеры запуска:
    python3 tools/chat_export_stats.py result.json
    python3 tools/chat_export_stats.py result.json --db data/forum.db --csv rating.csv
    python3 tools/chat_export_stats.py result.json --since 2026-09-01 --until 2026-09-15 --top 30

Коды выхода: 0 — посчитано (даже если часть контекста недоступна — см. предупреждения в шапке
вывода); 1 — файл экспорта не читается или не в ожидаемом формате.

Только stdlib. Импортов проекта нет — тул запускается и на сервере, и на ноутбуке менеджера.
"""
import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

_HOWTO = (
    "Как экспортировать правильно: Telegram Desktop → нужный чат → ⋮ → "
    "«Экспорт истории чата» → Формат: Machine-readable JSON."
)

# Веса формулы балла — единственный источник, --weights накатывает частичный JSON поверх этого
# словаря (merge, не замена целиком), поэтому пользовательский файл может переопределить только
# нужные ключи.
WEIGHTS = {
    "day_cap": 25.0,              # анти-флуд: очки одного дня не выше этого потолка
    "burst_log_base": 80.0,       # длина реплики (символов) на единицу в формуле log2
    "burst_log_cap": 3.0,         # потолок логарифмического бонуса за длину реплики
    "burst_media_score": 1.0,     # реплика без текста, но с не-стикерным медиа
    "burst_sticker_score": 0.5,   # реплика только из стикеров/GIF
    "resonance_reply": 2.0,       # вес одного засчитанного (≤5/сообщение) полученного ответа
    "resonance_reply_cap": 5,     # потолок засчитанных ответов на одно сообщение
    "resonance_reaction": 0.5,    # вес одной засчитанной (≤10/сообщение) полученной реакции
    "resonance_reaction_cap": 10,  # потолок засчитанных реакций на одно сообщение
    "regularity_per_day": 3.0,    # вес одного активного дня
    "giving_per_reaction": 0.25,  # вес одной поставленной реакции
}

CSV_COLUMNS = [
    "place", "telegram_id", "name", "username", "full_name", "status",
    "score", "volume", "resonance", "regularity", "giving",
    "messages", "bursts", "short", "long", "chars_total", "media", "stickers",
    "replies_given", "replies_received", "reactions_received", "reactions_given",
    "active_days", "first_seen", "last_seen",
]


class ExportError(Exception):
    """Экспорт не читается или не в ожидаемом формате Telegram Desktop."""


# ---------------------------------------------------------------------------
# Разбор входа
# ---------------------------------------------------------------------------

def load_export(path: str) -> dict:
    """Читает и валидирует result.json. Не тот файл/формат -> ExportError с русским текстом."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        raise ExportError(f"Не удалось открыть файл «{path}»: {e}. {_HOWTO}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ExportError(f"Файл «{path}» — не JSON ({e}). {_HOWTO}") from e
    if not isinstance(data, dict):
        raise ExportError(
            f"Корень экспорта должен быть объектом, а получено {type(data).__name__}. {_HOWTO}"
        )
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ExportError(
            f"В файле «{path}» нет списка «messages» — это не экспорт чата Telegram Desktop. {_HOWTO}"
        )
    return data


def _author_id_from_from_id(raw) -> int | None:
    """"user123456" -> 123456. Каналы ("channel…") и всё остальное — не персональный автор."""
    if not isinstance(raw, str) or not raw.startswith("user"):
        return None
    try:
        return int(raw[len("user"):])
    except ValueError:
        return None


def _text_length(text) -> int:
    """ПРИВАТНОСТЬ: отсюда наружу уходит только ЧИСЛО (длина). Сам текст никуда не сохраняется
    и не возвращается — ни в этой функции, ни где-либо ниже по цепочке parse -> aggregate ->
    score -> render/csv. Ни одна структура данных тула не хранит поле с текстом сообщения."""
    if isinstance(text, str):
        return len(text)
    if isinstance(text, list):
        total = 0
        for piece in text:
            if isinstance(piece, str):
                total += len(piece)
            elif isinstance(piece, dict) and isinstance(piece.get("text"), str):
                total += len(piece["text"])
        return total
    return 0


def _has_media(raw: dict) -> bool:
    return bool(raw.get("photo")) or bool(raw.get("file")) or bool(raw.get("media_type"))


def _parse_message_ts(raw: dict) -> datetime:
    """"date" — локальное время экспорта (ISO). При сбое падаем на date_unixtime (UTC), но
    приводим к наивному datetime — иначе разница между локальным и UTC-путём ломает вычитание
    времени между репликами одного автора (offset-naive vs offset-aware)."""
    raw_date = raw.get("date")
    if isinstance(raw_date, str):
        try:
            return datetime.fromisoformat(raw_date)
        except ValueError:
            pass
    raw_unix = raw.get("date_unixtime")
    if raw_unix is not None:
        try:
            return datetime.fromtimestamp(int(raw_unix), tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OSError):
            pass
    return datetime.min


def _parse_reactions(raw: dict):
    """count суммируется в reactions_received; recent[].from_id — по одному очку дарителю
    за запись (Telegram кладёт неполный список recent — это приближение, не точный счёт)."""
    reactions = raw.get("reactions")
    received = 0
    giver_ids: list[int] = []
    if isinstance(reactions, list):
        for r in reactions:
            if not isinstance(r, dict):
                continue
            count = r.get("count")
            if isinstance(count, int):
                received += count
            recent = r.get("recent")
            if isinstance(recent, list):
                for entry in recent:
                    if not isinstance(entry, dict):
                        continue
                    gid = _author_id_from_from_id(entry.get("from_id"))
                    if gid is not None:
                        giver_ids.append(gid)
    return received, giver_ids


@dataclass
class ParsedMessage:
    id: int
    type: str
    author_id: int | None
    author_name: str | None
    ts: datetime
    day: date
    length: int
    has_media: bool
    is_sticker_or_gif: bool
    reply_to: int | None
    reactions_received: int
    reaction_giver_ids: list


def parse_messages(raw_messages: list):
    """Возвращает (messages, reply_index, reactions_present).

    reply_index — по ВСЕМУ экспорту (id -> (type, author_id)), нужен, чтобы отличать реальный
    ответ от формального «reply на корень топика» (topic_created, type="service") и чтобы
    начислять резонанс автору цели, даже если сама цель лежит вне окна --since/--until.
    messages — только успешно разобранные пользовательские сообщения (type="message" и
    from_id вида "user…"); каналы и сервисные остаются только в индексе.
    """
    messages: list[ParsedMessage] = []
    reply_index: dict[int, tuple] = {}
    reactions_present = False

    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        if "reactions" in raw:
            reactions_present = True

        try:
            mid = int(raw.get("id"))
        except (TypeError, ValueError):
            mid = None

        mtype = raw.get("type") or "message"
        author_id = _author_id_from_from_id(raw.get("from_id")) if mtype == "message" else None

        if mid is not None:
            reply_index[mid] = (mtype, author_id)

        if mtype != "message" or author_id is None:
            continue  # канал / сервисное / без from_id — индекс заполнен выше, дальше не идёт

        from_name = raw.get("from")
        author_name = from_name if isinstance(from_name, str) and from_name else None

        is_forwarded = bool(raw.get("forwarded_from"))
        # Чужой текст (пересланное сообщение) не собственный вклад автора — длина 0.
        length = 0 if is_forwarded else _text_length(raw.get("text"))

        reply_to_raw = raw.get("reply_to_message_id")
        try:
            reply_to = int(reply_to_raw) if reply_to_raw is not None else None
        except (TypeError, ValueError):
            reply_to = None

        reactions_received, giver_ids = _parse_reactions(raw)

        ts = _parse_message_ts(raw)
        if ts == datetime.min:
            continue  # без даты сообщение дало бы фантомный «активный день» 0001-01-01

        messages.append(ParsedMessage(
            id=mid if mid is not None else -1,
            type=mtype,
            author_id=author_id,
            author_name=author_name,
            ts=ts,
            day=ts.date(),
            length=length,
            has_media=_has_media(raw),
            is_sticker_or_gif=raw.get("media_type") in {"sticker", "animation"},
            reply_to=reply_to,
            reactions_received=reactions_received,
            reaction_giver_ids=giver_ids,
        ))

    return messages, reply_index, reactions_present


# ---------------------------------------------------------------------------
# Агрегация
# ---------------------------------------------------------------------------

def _in_window(day: date, since: date | None, until: date | None) -> bool:
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


@dataclass
class BurstInfo:
    day: date
    length: int
    has_nonsticker_media: bool
    stickers_only: bool


@dataclass
class AuthorAgg:
    telegram_id: int
    name: str = ""
    messages: int = 0
    chars_total: int = 0
    short: int = 0
    long: int = 0
    media: int = 0
    stickers: int = 0
    active_days: int = 0
    active_days_set: set = field(default_factory=set)
    bursts: list = field(default_factory=list)
    replies_given: int = 0
    replies_received: int = 0
    reactions_received: int = 0
    reactions_given: int = 0
    # Сырые счётчики НА СООБЩЕНИЕ: потолки резонанса — это веса, их применяет score_authors
    # (иначе --weights менял бы потолок в шапке вывода, но не в самом расчёте).
    reply_counts: list = field(default_factory=list)
    reaction_counts: list = field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None


def aggregate(messages, reply_index, *, long_chars=120, burst_gap=120, since=None, until=None):
    """Считает сырые метрики на автора. Балл здесь НЕ считается — это работа score_authors,
    которая применяет веса; aggregate хранит достаточно данных (bursts по дням), чтобы
    посчитать дневной потолок позже, не пересчитывая склейку реплик заново."""
    in_window = [
        m for m in messages
        if m.type == "message" and m.author_id is not None and _in_window(m.day, since, until)
    ]

    # Имя автора — «последнее непустое from в экспорте» смотрим по ВСЕМУ экспорту, а не только
    # по окну: имя это просто подпись для вывода, а не метрика активности за период.
    names: dict[int, str] = {}
    for m in sorted(
        (mm for mm in messages if mm.author_id is not None and mm.author_name), key=lambda x: x.ts
    ):
        names[m.author_id] = m.author_name

    by_author: dict[int, list] = defaultdict(list)
    for m in in_window:
        by_author[m.author_id].append(m)

    aggs: dict[int, AuthorAgg] = {}
    # Владелец каждого сообщения-цели, получившего валидный ответ, и сколько их пришло — нужно
    # ПОСЛЕ прохода по репликующим, т.к. владелец цели может не иметь ни одного сообщения в
    # окне (цель лежит раньше --since), но резонанс ему всё равно причитается.
    replies_to_message: Counter = Counter()

    for author_id, msgs in by_author.items():
        msgs.sort(key=lambda m: m.ts)
        agg = AuthorAgg(telegram_id=author_id, name=names.get(author_id, ""))
        agg.messages = len(msgs)
        agg.first_seen = msgs[0].ts
        agg.last_seen = msgs[-1].ts

        current = None  # текущая незакрытая реплика (склейка своих подряд идущих сообщений)
        for m in msgs:
            agg.chars_total += m.length
            if m.length > 0:
                if m.length <= long_chars:
                    agg.short += 1
                else:
                    agg.long += 1
            if m.has_media:
                agg.media += 1
            if m.is_sticker_or_gif:
                agg.stickers += 1
            agg.active_days_set.add(m.day)
            agg.reactions_received += m.reactions_received
            if m.reactions_received:
                agg.reaction_counts.append(m.reactions_received)

            if current is not None and (m.ts - current["last_ts"]).total_seconds() <= burst_gap:
                current["length"] += m.length
                current["nonsticker_media"] = current["nonsticker_media"] or (
                    m.has_media and not m.is_sticker_or_gif
                )
                current["all_sticker"] = current["all_sticker"] and m.is_sticker_or_gif
                current["last_ts"] = m.ts
            else:
                if current is not None:
                    agg.bursts.append(BurstInfo(
                        day=current["day"], length=current["length"],
                        has_nonsticker_media=current["nonsticker_media"],
                        stickers_only=current["all_sticker"],
                    ))
                current = {
                    "day": m.day, "length": m.length,
                    "nonsticker_media": m.has_media and not m.is_sticker_or_gif,
                    "all_sticker": m.is_sticker_or_gif, "last_ts": m.ts,
                }

            if m.reply_to is not None:
                target = reply_index.get(m.reply_to)
                if target is not None:
                    t_type, t_author = target
                    # Правило корня топика: ответ на СЕРВИСНОЕ сообщение (typically
                    # topic_created) не считается ответом — иначе в форум-группах КАЖДОЕ
                    # сообщение формально «reply» на корень топика, и счёт ответов превращается
                    # в счёт сообщений. Ответ самому себе тоже не резонанс.
                    if t_type == "message" and t_author is not None and t_author != author_id:
                        agg.replies_given += 1
                        replies_to_message[m.reply_to] += 1
        if current is not None:
            agg.bursts.append(BurstInfo(
                day=current["day"], length=current["length"],
                has_nonsticker_media=current["nonsticker_media"],
                stickers_only=current["all_sticker"],
            ))

        agg.active_days = len(agg.active_days_set)
        aggs[author_id] = agg

    # ВАЖНО, порядок: сначала посчитали ответы/резонанс для ВСЕХ авторов (включая staff), и
    # только main() после score_authors режет исключённых из рейтинга перед выводом. Если
    # поменять порядок местами (сначала исключить, потом агрегировать), резонанс делегатов,
    # которым отвечали в основном организаторы, молча обнулится.
    for target_mid, count in replies_to_message.items():
        t_type, t_author = reply_index[target_mid]
        if t_author is None:
            continue
        agg = aggs.get(t_author)
        if agg is None:
            agg = AuthorAgg(telegram_id=t_author, name=names.get(t_author, ""))
            aggs[t_author] = agg
        agg.replies_received += count
        agg.reply_counts.append(count)

    for m in in_window:
        for gid in m.reaction_giver_ids:
            agg = aggs.get(gid)
            if agg is None:
                agg = AuthorAgg(telegram_id=gid, name=names.get(gid, ""))
                aggs[gid] = agg
            agg.reactions_given += 1

    return aggs


# ---------------------------------------------------------------------------
# Балл
# ---------------------------------------------------------------------------

def _burst_score(b: BurstInfo, w: dict) -> float:
    if b.length > 0:
        return 1 + min(w["burst_log_cap"], math.log2(1 + b.length / w["burst_log_base"]))
    if b.has_nonsticker_media:
        return w["burst_media_score"]
    if b.stickers_only:
        return w["burst_sticker_score"]
    return 0.0  # пересланный чужой текст без медиа — реплика есть, объёма не приносит


def score_authors(aggs: dict, weights: dict | None = None) -> dict:
    w = dict(WEIGHTS)
    if weights:
        w.update(weights)

    results = {}
    for author_id, agg in aggs.items():
        day_scores: dict = defaultdict(float)
        for b in agg.bursts:
            day_scores[b.day] += _burst_score(b, w)
        volume = sum(min(w["day_cap"], v) for v in day_scores.values())
        resonance = (
            w["resonance_reply"] * sum(min(w["resonance_reply_cap"], c) for c in agg.reply_counts)
            + w["resonance_reaction"]
            * sum(min(w["resonance_reaction_cap"], c) for c in agg.reaction_counts)
        )
        regularity = w["regularity_per_day"] * agg.active_days
        giving = w["giving_per_reaction"] * agg.reactions_given
        results[author_id] = {
            "volume": volume, "resonance": resonance,
            "regularity": regularity, "giving": giving,
            "score": volume + resonance + regularity + giving,
        }
    return results


# ---------------------------------------------------------------------------
# Контекст из БД: исключение команды + подписи из анкет
# ---------------------------------------------------------------------------

def _normalize_username(raw) -> str:
    """В экспорте Telegram Desktop юзернеймов НЕТ (только отображаемое имя) — @ник берём из
    `users.username`. Там лежат и плейсхолдеры («», «-», «@») — их считаем отсутствием ника."""
    value = str(raw or "").strip().lstrip("@")
    if not value or value == "-":
        return ""
    return f"@{value}"


def load_db_context(db_path: str | None, exclude_ids, exclude_file: str | None):
    """Возвращает (excluded_ids, staff_ids, manual_ids, user_info, warnings).

    БД открывается СТРОГО read-only (`mode=ro`) — тул физически не может ничего в неё записать.
    Отсутствующая таблица staff и вовсе недоступная БД — не ошибка (код 1), а предупреждение в
    шапку: экспорт посчитан, исключений просто не будет.
    """
    excluded = set(exclude_ids or [])
    staff_ids: set = set()
    user_info: dict = {}
    warnings: list = []

    if exclude_file:
        try:
            with open(exclude_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        excluded.add(int(line))
                    except ValueError:
                        warnings.append(f"Не разобрал строку в --exclude-file: «{line}» — пропущена.")
        except OSError as e:
            warnings.append(f"Не удалось прочитать --exclude-file «{exclude_file}»: {e}")

    if db_path:
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as e:
            warnings.append(f"БД «{db_path}» недоступна: {e}. Исключение по ролям не применено.")

        if conn is not None:
            try:
                try:
                    rows = conn.execute("SELECT DISTINCT telegram_id FROM staff").fetchall()
                    staff_ids.update(r[0] for r in rows)
                    excluded.update(staff_ids)
                except sqlite3.OperationalError as e:
                    if "no such table" in str(e):
                        warnings.append(
                            "В БД нет таблицы staff (старая схема) — исключение по ролям не применено."
                        )
                    else:
                        warnings.append(f"Не удалось прочитать staff: {e}")
                try:
                    rows = conn.execute(
                        "SELECT telegram_id, username, full_name, status FROM users"
                    ).fetchall()
                    for tid, username, full_name, status in rows:
                        user_info[tid] = (_normalize_username(username), full_name or "", status or "")
                except sqlite3.OperationalError as e:
                    warnings.append(f"Не удалось прочитать users: {e}")
            finally:
                conn.close()

    manual_ids = excluded - staff_ids
    return excluded, staff_ids, manual_ids, user_info, warnings


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

def _build_rows(aggs: dict, scores: dict, user_info: dict) -> list:
    rows = []
    for author_id, agg in aggs.items():
        sc = scores[author_id]
        username, full_name, status = user_info.get(author_id, ("", "", ""))
        rows.append({
            "telegram_id": author_id,
            "name": agg.name or f"id{author_id}",
            "username": username,
            "full_name": full_name,
            "status": status,
            "score": round(sc["score"], 2),
            "volume": round(sc["volume"], 2),
            "resonance": round(sc["resonance"], 2),
            "regularity": round(sc["regularity"], 2),
            "giving": round(sc["giving"], 2),
            "messages": agg.messages,
            "bursts": len(agg.bursts),
            "short": agg.short,
            "long": agg.long,
            "chars_total": agg.chars_total,
            "media": agg.media,
            "stickers": agg.stickers,
            "replies_given": agg.replies_given,
            "replies_received": agg.replies_received,
            "reactions_received": agg.reactions_received,
            "reactions_given": agg.reactions_given,
            "active_days": agg.active_days,
            "first_seen": agg.first_seen.isoformat() if agg.first_seen else "",
            "last_seen": agg.last_seen.isoformat() if agg.last_seen else "",
        })
    return rows


def _sort_rows(rows: list, sort_key: str) -> list:
    primary = "score" if sort_key == "score" else "messages"
    rows_sorted = sorted(rows, key=lambda r: (-r[primary], -r["messages"], r["name"]))
    for i, r in enumerate(rows_sorted, start=1):
        r["place"] = i
    return rows_sorted


def render_console(*, chat_name, since, until, total_messages, total_authors,
                    staff_excluded, manual_excluded, weights, reactions_present,
                    warnings, rows, top) -> None:
    lines = [f"Чат: {chat_name}"]
    if since or until:
        lines.append(f"Период: {since.isoformat() if since else '…'} — {until.isoformat() if until else '…'}")
    else:
        lines.append("Период: весь экспорт")
    lines.append(f"Сообщений учтено: {total_messages}, авторов: {total_authors}")

    excl_bits = []
    if staff_excluded:
        excl_bits.append(f"сотрудники: {staff_excluded}")
    if manual_excluded:
        excl_bits.append(f"ручной список: {manual_excluded}")
    if excl_bits:
        lines.append("Исключено из рейтинга: " + ", ".join(excl_bits))

    w = weights
    lines.append(
        "Формула балла: score = volume + resonance + regularity + giving. "
        f"Реплика: 1+min({w['burst_log_cap']:g}, log2(1+длина/{w['burst_log_base']:g})) за текст, "
        f"{w['burst_media_score']:g} за медиа без текста, {w['burst_sticker_score']:g} за стикер/GIF; "
        f"объём дня — не выше {w['day_cap']:g}. "
        f"Резонанс = {w['resonance_reply']:g}×ответы(≤{w['resonance_reply_cap']:g}/сообщение) "
        f"+ {w['resonance_reaction']:g}×реакции(≤{w['resonance_reaction_cap']:g}/сообщение). "
        f"Регулярность = {w['regularity_per_day']:g}×активных дней. "
        f"Отдача = {w['giving_per_reaction']:g}×поставленных реакций."
    )

    if not reactions_present:
        lines.append("В экспорте нет блока реакций (старый формат экспорта) — реакции не учтены.")
    else:
        lines.append("Реакции подаренные — приблизительно: Telegram хранит неполный список дарителей.")

    for warning in warnings:
        lines.append(f"⚠ {warning}")

    print("\n".join(lines))
    print()

    headers = ["#", "Имя", "Ник", "Балл", "Сообщ.", "Реплик", "Длинных", "Ответ.получ.", "Реак.получ.", "Дни"]
    widths = [3, 22, 18, 7, 7, 7, 8, 12, 11, 4]
    print(" ".join(h.ljust(w2) for h, w2 in zip(headers, widths)))
    for r in rows[:top]:
        vals = [
            str(r["place"]), r["name"][:22], r["username"][:18], f"{r['score']:.1f}",
            str(r["messages"]),
            str(r["bursts"]), str(r["long"]), str(r["replies_received"]),
            str(r["reactions_received"]), str(r["active_days"]),
        ]
        print(" ".join(v.ljust(w2) for v, w2 in zip(vals, widths)))


def write_csv(path: str, rows: list) -> None:
    # utf-8-sig — иначе Excel показывает кириллицу кракозябрами (тот же приём, что в выгрузках
    # handlers/admin_*.py).
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_COLUMNS})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date_arg(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as e:
        raise ExportError(f"Не разобрал дату «{value}» — используйте формат ГГГГ-ММ-ДД.") from e


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Активность участников чата по экспорту Telegram Desktop (result.json)."
    )
    ap.add_argument("export", help="путь к result.json (Machine-readable JSON)")
    ap.add_argument("--top", type=int, default=20, help="строк в консольной таблице, по умолчанию 20")
    ap.add_argument("--csv", default=None, help="куда выгрузить полную таблицу (utf-8-sig)")
    ap.add_argument("--long-chars", type=int, default=120, help="порог «длинного» сообщения, символов")
    ap.add_argument("--burst-gap", type=int, default=120, help="пауза склейки реплики, секунд")
    ap.add_argument("--since", default=None, help="ГГГГ-ММ-ДД, включительно")
    ap.add_argument("--until", default=None, help="ГГГГ-ММ-ДД, включительно")
    ap.add_argument("--db", default=None, help="forum.db для исключения staff и подписи ФИО/статуса")
    ap.add_argument("--exclude-ids", default=None, help="telegram_id через запятую")
    ap.add_argument("--exclude-file", default=None, help="файл: по одному telegram_id на строку")
    ap.add_argument("--weights", default=None, help="JSON с переопределением части весов формулы")
    ap.add_argument("--sort", choices=["score", "messages"], default="score")
    return ap


def main(argv=None) -> int:
    # Формула в шапке использует ×/≤/⚠ — консоль Windows по умолчанию в cp1251 и падает
    # UnicodeEncodeError на этих символах. reconfigure есть с Python 3.7; там, где его нет
    # или поток уже не текстовый (перенаправлен во что-то экзотическое), тихо смиряемся.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = build_arg_parser()
    args = ap.parse_args(argv)

    try:
        data = load_export(args.export)
        since = _parse_date_arg(args.since)
        until = _parse_date_arg(args.until)
    except ExportError as e:
        print(str(e), file=sys.stderr)
        return 1

    chat_name = data.get("name") or "без названия"
    messages, reply_index, reactions_present = parse_messages(data["messages"])

    aggregated = aggregate(
        messages, reply_index,
        long_chars=args.long_chars, burst_gap=args.burst_gap, since=since, until=until,
    )

    weights_override = None
    if args.weights:
        try:
            with open(args.weights, encoding="utf-8") as f:
                weights_override = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Не удалось прочитать --weights «{args.weights}»: {e}", file=sys.stderr)
            return 1
    weights = dict(WEIGHTS)
    if weights_override is not None:
        # Опечатка в ключе иначе молча оставила бы дефолтный вес — а менеджер был бы уверен,
        # что считает по-своему.
        problem = None
        if not isinstance(weights_override, dict):
            problem = "ожидается объект вида {\"day_cap\": 30}"
        else:
            unknown = sorted(k for k in weights_override if k not in WEIGHTS)
            not_numbers = sorted(
                k for k, v in weights_override.items()
                if isinstance(v, bool) or not isinstance(v, (int, float))
            )
            if unknown:
                problem = f"неизвестные ключи: {', '.join(unknown)}. Допустимые: {', '.join(WEIGHTS)}"
            elif not_numbers:
                problem = f"значения должны быть числами: {', '.join(not_numbers)}"
        if problem:
            print(f"Файл --weights «{args.weights}»: {problem}", file=sys.stderr)
            return 1
        weights.update(weights_override)

    scores = score_authors(aggregated, weights)

    exclude_ids_cli = []
    if args.exclude_ids:
        for piece in args.exclude_ids.split(","):
            piece = piece.strip()
            if piece:
                try:
                    exclude_ids_cli.append(int(piece))
                except ValueError:
                    print(f"Не разобрал telegram_id в --exclude-ids: «{piece}»", file=sys.stderr)
                    return 1

    excluded_ids, staff_ids, manual_ids, user_info, db_warnings = load_db_context(
        args.db, exclude_ids_cli, args.exclude_file,
    )

    rows_all = _build_rows(aggregated, scores, user_info)
    total_messages = sum(a.messages for a in aggregated.values())
    total_authors = sum(1 for a in aggregated.values() if a.messages > 0)

    # Порядок принципиален (см. комментарий в aggregate): агрегация уже учла ответы/реакции
    # исключённых, режем их из рейтинга только на этом, последнем шаге.
    kept_rows = _sort_rows(
        [r for r in rows_all if r["telegram_id"] not in excluded_ids], args.sort,
    )

    render_console(
        chat_name=chat_name, since=since, until=until,
        total_messages=total_messages, total_authors=total_authors,
        # Считаем только тех исключённых, кто реально есть в экспорте: «сотрудники: 40» при
        # восьми писавших выглядело бы так, будто из рейтинга выкинули пол-чата.
        staff_excluded=len(staff_ids & aggregated.keys()),
        manual_excluded=len(manual_ids & aggregated.keys()),
        weights=weights, reactions_present=reactions_present,
        warnings=db_warnings, rows=kept_rows, top=args.top,
    )

    if args.csv:
        write_csv(args.csv, kept_rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
