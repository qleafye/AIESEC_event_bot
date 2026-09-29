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

Только stdlib плюс корневой chat_score.py того же чекаута (формула общая с дашбордом) —
тул запускается и на сервере, и на ноутбуке менеджера из клона репозитория.
"""
import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.chat_score import (  # noqa: E402 — после бутстрапа sys.path
    WEIGHTS,
    AuthorAgg,  # noqa: F401 — реэкспорт для тестов и внешних скриптов
    BurstInfo,  # noqa: F401 — реэкспорт
    ChatRecord,
    aggregate_records,
    describe_formula,
    score_authors,
)

_HOWTO = (
    "Как экспортировать правильно: Telegram Desktop → нужный чат → ⋮ → "
    "«Экспорт истории чата» → Формат: Machine-readable JSON."
)

# WEIGHTS / AuthorAgg / BurstInfo / score_authors реэкспортируются из chat_score — тесты и
# внешние скрипты обращаются к ним как к атрибутам тула.

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
# Агрегация и балл — в chat_score.py
# ---------------------------------------------------------------------------

def to_records(messages, reply_index) -> list:
    """ParsedMessage -> ChatRecord. Здесь живёт правило корня топика: ответ засчитывается,
    только если цель — обычное сообщение человека. Ответ на СЕРВИСНОЕ сообщение (обычно
    topic_created) не ответ — иначе в форум-группах КАЖДОЕ сообщение формально «reply» на корень
    топика, и счёт ответов превращается в счёт сообщений. Цель ищется по всему экспорту, поэтому
    отклик засчитывается, даже если сама цель лежит вне окна --since/--until."""
    records = []
    for m in messages:
        reply_mid = reply_author = None
        if m.reply_to is not None:
            target = reply_index.get(m.reply_to)
            if target is not None:
                t_type, t_author = target
                if t_type == "message" and t_author is not None:
                    reply_mid, reply_author = m.reply_to, t_author
        records.append(ChatRecord(
            message_id=m.id,
            author_id=m.author_id,
            ts=m.ts,
            text_len=m.length,
            has_media=m.has_media,
            is_sticker=m.is_sticker_or_gif,
            reply_to_message_id=reply_mid,
            reply_to_author_id=reply_author,
            reactions_received=m.reactions_received,
            reaction_giver_ids=tuple(m.reaction_giver_ids),
            author_name=m.author_name or "",
        ))
    return records


def aggregate(messages, reply_index, *, long_chars=120, burst_gap=120, since=None, until=None):
    """Сырые метрики на автора — тонкая обёртка над chat_score.aggregate_records."""
    return aggregate_records(
        to_records(messages, reply_index),
        long_chars=long_chars, burst_gap=burst_gap, since=since, until=until,
    )


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

    lines.append(describe_formula(weights))

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

    # Порядок принципиален (см. комментарий в chat_score.aggregate_records): агрегация уже учла ответы/реакции
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
