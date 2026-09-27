"""Рейтинг активности чата — одна формула для тула по экспорту, реестра настроек и дашборда.

Модуль работает на нормализованных записях ChatRecord, а не на формате экспорта Telegram
Desktop: разбор экспорта живёт в tools/chat_export_stats.py, чтение живого чата из БД — в
дашборде. Обе стороны сводят свои данные к ChatRecord и считают здесь одинаково.

Только stdlib и ни одного импорта проекта: файл копируется в образ дашборда и импортируется
реестром настроек, лишняя зависимость сломала бы и то, и другое.

ПРИВАТНОСТЬ: текста сообщений здесь нет вовсе — ChatRecord хранит только длину.
"""
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

# Веса формулы балла — единственный источник. Тул накатывает поверх них --weights, дашборд —
# значения из bot_settings (weights_from_settings), и то и другое merge, а не замена целиком.
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

# Потолки — целые счётчики «сколько ответов/реакций на сообщение засчитать».
_INT_WEIGHTS = {"resonance_reply_cap", "resonance_reaction_cap"}

DEFAULT_BURST_GAP = 120   # секунд: свои сообщения ближе этой паузы склеиваются в одну реплику
DEFAULT_LONG_CHARS = 120  # символов: порог «длинного» сообщения (только счётчик, не балл)

# Вес формулы -> ключ bot_settings (реестр настроек, раздел «Рейтинг чата»).
SETTING_KEYS = {
    "day_cap": "chat_rating_day_cap",
    "burst_log_base": "chat_rating_length_base",
    "burst_log_cap": "chat_rating_length_cap",
    "burst_media_score": "chat_rating_media_score",
    "burst_sticker_score": "chat_rating_sticker_score",
    "resonance_reply": "chat_rating_reply_weight",
    "resonance_reply_cap": "chat_rating_reply_cap",
    "resonance_reaction": "chat_rating_reaction_weight",
    "resonance_reaction_cap": "chat_rating_reaction_cap",
    "regularity_per_day": "chat_rating_day_weight",
    "giving_per_reaction": "chat_rating_giving_weight",
}
BURST_GAP_KEY = "chat_rating_burst_gap_seconds"
RETENTION_KEY = "chat_rating_retention_days"
DEFAULT_RETENTION_DAYS = 180
MODE_KEY = "chat_rating_mode"
MODES = ("formula", "rules")


@dataclass(frozen=True)
class ChatRecord:
    """Одно сообщение чата, сведённое к числам.

    reply_to_message_id / reply_to_author_id выставляются ТОЛЬКО для настоящего ответа
    человеку: формальный «ответ на корень топика» в форум-группах отсекает адаптер источника
    (экспорт или живой чат), здесь его уже нет.
    """
    message_id: int
    author_id: int | None
    ts: datetime                      # наивное время по Москве
    text_len: int = 0
    has_media: bool = False           # любое медиа, включая стикер/GIF
    is_sticker: bool = False          # стикер или анимация (GIF)
    reply_to_message_id: int | None = None
    reply_to_author_id: int | None = None
    reactions_received: int = 0
    reaction_giver_ids: tuple = ()
    is_channel_post: bool = False     # пост от имени канала/чата — не автор рейтинга
    author_name: str = ""

    @property
    def day(self) -> date:
        return self.ts.date()


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
    # (иначе смена веса меняла бы потолок в описании формулы, но не в самом расчёте).
    reply_counts: list = field(default_factory=list)
    reaction_counts: list = field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None


def aggregate_records(records, *, long_chars=DEFAULT_LONG_CHARS, burst_gap=DEFAULT_BURST_GAP,
                      since=None, until=None):
    """Считает сырые метрики на автора. Балл здесь НЕ считается — это работа score_authors,
    которая применяет веса; агрегат хранит достаточно данных (bursts по дням), чтобы
    посчитать дневной потолок позже, не пересчитывая склейку реплик заново."""
    people = [r for r in records if not r.is_channel_post and r.author_id is not None]
    in_window = [r for r in people if _in_window(r.day, since, until)]

    # Имя автора — «последнее непустое имя» по ВСЕМ записям, а не только по окну: имя это
    # просто подпись для вывода, а не метрика активности за период.
    names: dict[int, str] = {}
    for r in sorted((rr for rr in people if rr.author_name), key=lambda x: x.ts):
        names[r.author_id] = r.author_name

    by_author: dict[int, list] = defaultdict(list)
    for r in in_window:
        by_author[r.author_id].append(r)

    aggs: dict[int, AuthorAgg] = {}
    # Сколько валидных ответов пришло на каждое сообщение-цель — нужно ПОСЛЕ прохода по
    # репликующим, т.к. владелец цели может не иметь ни одного сообщения в окне (цель лежит
    # раньше since), но резонанс ему всё равно причитается.
    replies_to_message: Counter = Counter()

    for author_id, msgs in by_author.items():
        msgs.sort(key=lambda r: r.ts)
        agg = AuthorAgg(telegram_id=author_id, name=names.get(author_id, ""))
        agg.messages = len(msgs)
        agg.first_seen = msgs[0].ts
        agg.last_seen = msgs[-1].ts

        current = None  # текущая незакрытая реплика (склейка своих подряд идущих сообщений)
        for m in msgs:
            agg.chars_total += m.text_len
            if m.text_len > 0:
                if m.text_len <= long_chars:
                    agg.short += 1
                else:
                    agg.long += 1
            if m.has_media:
                agg.media += 1
            if m.is_sticker:
                agg.stickers += 1
            agg.active_days_set.add(m.day)
            agg.reactions_received += m.reactions_received
            if m.reactions_received:
                agg.reaction_counts.append(m.reactions_received)

            if current is not None and (m.ts - current["last_ts"]).total_seconds() <= burst_gap:
                current["length"] += m.text_len
                current["nonsticker_media"] = current["nonsticker_media"] or (
                    m.has_media and not m.is_sticker
                )
                current["all_sticker"] = current["all_sticker"] and m.is_sticker
                current["last_ts"] = m.ts
            else:
                if current is not None:
                    agg.bursts.append(BurstInfo(
                        day=current["day"], length=current["length"],
                        has_nonsticker_media=current["nonsticker_media"],
                        stickers_only=current["all_sticker"],
                    ))
                current = {
                    "day": m.day, "length": m.text_len,
                    "nonsticker_media": m.has_media and not m.is_sticker,
                    "all_sticker": m.is_sticker, "last_ts": m.ts,
                }

            # Ответ самому себе — не резонанс; ответ без адресата-человека тоже.
            if m.reply_to_author_id is not None and m.reply_to_author_id != author_id:
                agg.replies_given += 1
                replies_to_message[(m.reply_to_message_id, m.reply_to_author_id)] += 1
        if current is not None:
            agg.bursts.append(BurstInfo(
                day=current["day"], length=current["length"],
                has_nonsticker_media=current["nonsticker_media"],
                stickers_only=current["all_sticker"],
            ))

        agg.active_days = len(agg.active_days_set)
        aggs[author_id] = agg

    # ВАЖНО, порядок: сначала посчитали ответы/резонанс для ВСЕХ авторов (включая staff), и
    # только вызывающий после score_authors режет исключённых из рейтинга перед выводом. Если
    # поменять порядок местами (сначала исключить, потом агрегировать), резонанс делегатов,
    # которым отвечали в основном организаторы, молча обнулится.
    for (_target_mid, t_author), count in replies_to_message.items():
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
# Настройки -> веса, описание формулы
# ---------------------------------------------------------------------------

def _parse_non_negative(raw) -> float | None:
    """Число, введённое менеджером: запятая допустима, отрицательное/нечисло/бесконечность ->
    None (вызывающий берёт дефолт). Никогда не бросает — кривое значение в настройках не должно
    ронять дашборд."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def weights_from_settings(raw) -> tuple:
    """bot_settings (ключ -> строка) -> (weights, burst_gap). Отсутствующее/кривое -> дефолт."""
    raw = raw or {}
    weights = dict(WEIGHTS)
    for name, key in SETTING_KEYS.items():
        value = _parse_non_negative(raw.get(key))
        if value is None:
            continue
        weights[name] = int(value) if name in _INT_WEIGHTS else value
    gap = _parse_non_negative(raw.get(BURST_GAP_KEY))
    burst_gap = int(gap) if gap is not None and int(gap) > 0 else DEFAULT_BURST_GAP
    return weights, burst_gap


def describe_formula(w: dict) -> str:
    """Формула человеческим языком — шапка вывода тула и подсказка дашборда."""
    return (
        "Формула балла: score = volume + resonance + regularity + giving. "
        f"Реплика: 1+min({w['burst_log_cap']:g}, log2(1+длина/{w['burst_log_base']:g})) за текст, "
        f"{w['burst_media_score']:g} за медиа без текста, {w['burst_sticker_score']:g} за стикер/GIF; "
        f"объём дня — не выше {w['day_cap']:g}. "
        f"Резонанс = {w['resonance_reply']:g}×ответы(≤{w['resonance_reply_cap']:g}/сообщение) "
        f"+ {w['resonance_reaction']:g}×реакции(≤{w['resonance_reaction_cap']:g}/сообщение). "
        f"Регулярность = {w['regularity_per_day']:g}×активных дней. "
        f"Отдача = {w['giving_per_reaction']:g}×поставленных реакций."
    )
