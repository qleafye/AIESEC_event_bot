"""Phase 30 (30-02, A2-03): единственное место правил справочника ВУЗ/город для типа шага
`lookup` — «Анкета 2.0». Бот (план 30-06) и Mini App (`GET /app/api/reg/suggest`, задача 4
этого плана) читают ОДНИ правила отсюда, второй копии нормализации/ранжирования в проекте
нет и не будет (та же дисциплина, что у `services/faq.py::normalize_question` — докстринг
там же объясняет, зачем такой модуль существует).

`normalize_alias` — чистая функция: без импорта `database.db`, без импорта `aiogram`,
тестируется без БД (`tests/test_lookup_normalize_260912.py`). `search_lookup`/`top_chips`/
`enqueue_merge`/`pin_chip`/`pinned_chips` ходят в `lookup_entries`/`lookup_merge_queue`
(`database/db.py`, задача 1 этого плана) через ОТЛОЖЕННЫЙ импорт `_connect` внутри функции —
та же дисциплина разрыва цикла, что у `services/i18n_sources.py`/`services/scheduler.py`
(`database.db.seed_lookup_from_snapshot`, в свою очередь, тем же приёмом в обратную сторону
отложенно импортирует `normalize_alias` отсюда).

`kind` — закрытый словарь: `"university"`, `"city"`. Владелец (12.09, решение 1): ДаData/
Getgeo не используем ни в каком виде — эти данные из офлайн-снапшота (`tools/seed_universities.py`).
"""
from __future__ import annotations

import re

from services.timeutil import msk_now

# Хвостовой скобочный довесок («СПбГУТ (бывш. ЛЭИС)», «Университет (филиал в …)») — снимается
# ПОСЛЕ casefold, чтобы регистр скобок не имел значения.
_PAREN_TAIL_RE = re.compile(r"\([^)]*\)\s*$")
# «им.»/«имени» с последующим пробелом — «Университет им. Бонча-Бруевича» и «Университет
# Бонча-Бруевича» обязаны схлопнуться в одну запись, «им.»/«имени» несёт ноль различительной
# информации о ВУЗе.
_IM_RE = re.compile(r"\bим(?:ени)?\.?\s+")
# Кавычки (в т.ч. ёлочки)/точки/запятые — пунктуация, которую менеджер и делегат расставляют
# непоследовательно вокруг одного и того же названия.
_PUNCT_RE = re.compile(r'["\'«»,.]')
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_alias(text: str | None) -> str:
    """`casefold()` (не `lower()` — устойчиво к языковым спецсимволам, тот же выбор, что у
    `services.faq.normalize_question`) + `ё`→`е` + снятие скобочного хвоста + «им.»/«имени» +
    кавычки/точки/запятые + схлопывание пробелов. Единственное правило сравнения псевдонимов
    ВУЗа/города в проекте: и посев (`database.db.seed_lookup_from_snapshot`), и поиск
    (`search_lookup`), и очередь слияния (`enqueue_merge`) проходят через эту функцию — иначе
    «СПбГАСУ» и «спбгасу» стали бы двумя разными записями справочника."""
    normalized = (text or "").strip().casefold()
    normalized = normalized.replace("ё", "е")
    normalized = _PAREN_TAIL_RE.sub("", normalized)
    normalized = _IM_RE.sub("", normalized)
    normalized = _PUNCT_RE.sub("", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def _escape_like(raw: str) -> str:
    """Экранирование `%`/`_`/`\\` перед `LIKE ... ESCAPE '\\'` (T-30-03, 30-RESEARCH.md §
    Security Domain) — без этого пользовательский `q` из одного символа `%` читался бы SQLite
    как «любая строка», а `_` — как «любой один символ»."""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_lookup(kind: str, q: str, limit: int = 10) -> list[dict]:
    """Ранжирование ТРЕМЯ отдельными параметризованными запросами (30-RESEARCH.md Pattern 2 —
    читаемый стиль `database/db.py`, не одна `CASE WHEN`-эквилибристика): точное совпадение
    `alias_norm` → префикс → подстрока. Каждый следующий запрос добирает результат только до
    `limit` (T-30-04: `LIMIT` в каждом из трёх, DoS длинным подстрочным сканом закрыт). Дубли
    по `canonical` схлопываются с сохранением порядка первого попадания — точное совпадение
    важнее, каким бы конкретным psевдонимом оно ни пришло."""
    from database.db import _connect

    norm = normalize_alias(q)
    if not norm:
        return []
    escaped = _escape_like(norm)

    seen: set[str] = set()
    result: list[dict] = []

    queries = [
        (
            "SELECT canonical, alias FROM lookup_entries "
            "WHERE kind = ? AND alias_norm = ? ORDER BY canonical LIMIT ?",
            (kind, norm, limit),
        ),
        (
            "SELECT canonical, alias FROM lookup_entries "
            "WHERE kind = ? AND alias_norm LIKE ? ESCAPE '\\' ORDER BY canonical LIMIT ?",
            (kind, escaped + "%", limit),
        ),
        (
            "SELECT canonical, alias FROM lookup_entries "
            "WHERE kind = ? AND alias_norm LIKE ? ESCAPE '\\' ORDER BY canonical LIMIT ?",
            (kind, "%" + escaped + "%", limit),
        ),
    ]

    async with _connect() as conn:
        for sql, params in queries:
            if len(result) >= limit:
                break
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            for canonical, alias in rows:
                if canonical in seen:
                    continue
                seen.add(canonical)
                result.append({"canonical": canonical, "alias": alias, "kind": kind})
                if len(result) >= limit:
                    break
    return result


# Phase 30 (30-02, A2-03, 30-CONTEXT.md решение владельца №4): колонка `users`, по которой
# считается частота ответов сезона для топ-8 — закрытый словарь тем же ключом `kind`, что и
# у справочника (`reg_engine.STEP_TO_COLUMN["university"] == "university"`,
# `STEP_TO_COLUMN["city"] == "city"` — оба совпадают со `step_key` дословно, вторая карта не
# заводится). Никакой подстановки имени колонки шаблоном строки в SQL ниже (T-30-03) — на
# каждый `kind` своя пара буквальных запросов, а не собранный на лету текст.
_SEASON_COLUMN = {"university": "university", "city": "city"}

_SEASON_FREQ_SQL = {
    "university": (
        "SELECT university, COUNT(*) AS cnt FROM users "
        "WHERE university IS NOT NULL AND university != '' AND season = ? "
        "GROUP BY university ORDER BY cnt DESC",
        "SELECT university, COUNT(*) AS cnt FROM users "
        "WHERE university IS NOT NULL AND university != '' "
        "GROUP BY university ORDER BY cnt DESC",
    ),
    "city": (
        "SELECT city, COUNT(*) AS cnt FROM users "
        "WHERE city IS NOT NULL AND city != '' AND season = ? "
        "GROUP BY city ORDER BY cnt DESC",
        "SELECT city, COUNT(*) AS cnt FROM users "
        "WHERE city IS NOT NULL AND city != '' "
        "GROUP BY city ORDER BY cnt DESC",
    ),
}

# Чат-форма (`reg_engine`) пишет эти значения в `users.university`/`users.city`, когда делегат
# пропустил шаг — «-»/«Пропустить» не ответы делегата, а служебная отметка пропуска, и в топ-8
# самых частых ответов попадать не должны (прод 15.09: «-» 125 и «Пропустить» 65 в топ-8 вместо
# настоящих ВУЗов).
_PLACEHOLDER_ANSWERS = frozenset(
    {"-", "—", "–", "пропустить", "skip", "нет", "не учусь", "не получал", "не получала", ""}
)

# Квик 260916 (UAT-SEED-06): `/uat` (`handlers/uat_seed.py::_SEED_ANSWERS`) сеет правдоподобные,
# но фиктивные ответы вроде «Тестовый университет (приёмка)» — до первой настоящей заявки
# сезона такая строка легко становится топ-1 подсказкой (стенд 16.09). «(приёмка)» — единый
# маркер засеянной строки в открытых для подсказок колонках (university/city); настоящий
# делегат его не напишет. Не импортируем `handlers.uat_seed` напрямую (это aiogram-модуль —
# этот файл читает и Mini App, см. докстринг наверху), сверяем по литеральному суффиксу.
_UAT_SEED_MARKER = "(приёмка)"


async def top_chips(kind: str, event_city: str | None, limit: int = 8) -> list[str]:
    """Топ-8 чипов (30-CONTEXT.md решение владельца №4): закреплённые менеджером ПЕРВЫМИ
    (порядок `canonical`, план 30-07 — экран закрепления), затем самые частые ОТВЕТЫ ТЕКУЩЕГО
    сезона (`bot_settings.event_season`, тот же ключ, каким сезон читают соседние агрегаты —
    `handlers/admin_cities.py`/`dashboard/queries.py`) по колонке `users`, добито до `limit`.
    Пустой сезон/колонка без данных/легаси-схема без `season` (тестовые БД старых миграций,
    `database/db.py::_column_exists` — тот же fail-soft приём, здесь через `try/except`, т.к.
    модуль не лезет в приватные хелперы `database.db`) — просто закреплённые (или пустой
    список), НИКОГДА исключение. `event_city` сегодня не фильтрует выборку (справочник ВУЗов/
    городов не завязан на конкретный event_city иначе, чем через сам список городов
    мероприятия) — параметр зарезервирован контрактом на случай будущей city-scoped политики
    чипов, читается сигнатурой ради совместимости вызывающих (задача 4).

    Перед ранжированием сырые ответы `users` очищаются от заглушек пропуска шага
    (`_PLACEHOLDER_ANSWERS`) и схлопываются через таблицу псевдонимов `lookup_entries`
    (`alias`/`canonical`, регистронезависимо) — «ВШЭ» и «НИУ ВШЭ» считаются одним ВУЗом, счёт
    суммируется под именем каноники."""
    from database.db import _connect

    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT canonical FROM lookup_entries "
            "WHERE kind = ? AND pinned = 1 ORDER BY canonical",
            (kind,),
        )
        pinned_rows = await cursor.fetchall()
        chips = [row[0] for row in pinned_rows]
        if len(chips) >= limit:
            return chips[:limit]

        freq_sqls = _SEASON_FREQ_SQL.get(kind)
        if freq_sqls is None:
            return chips
        sql_with_season, sql_without_season = freq_sqls

        season = None
        try:
            cursor = await conn.execute("SELECT value FROM bot_settings WHERE key = 'event_season'")
            season_row = await cursor.fetchone()
            season = season_row[0] if season_row else None
        except Exception:
            season = None

        freq_rows: list[tuple] = []
        try:
            if season:
                cursor = await conn.execute(sql_with_season, (season,))
            else:
                cursor = await conn.execute(sql_without_season)
            freq_rows = await cursor.fetchall()
        except Exception:
            # Легаси-тестовая БД без колонки `season`/`university`/`city` на `users` — топ-8
            # деградирует до закреплённых, а не роняет вызывающего (тот же класс fail-soft,
            # что у `database/db.py` UPDATE-миграции source/source_legacy — квик 260912-lwy).
            freq_rows = []

        alias_map: dict[str, str] = {}
        if freq_rows:
            cursor = await conn.execute(
                "SELECT alias, canonical FROM lookup_entries WHERE kind = ?", (kind,)
            )
            for alias, canonical in await cursor.fetchall():
                alias_map[alias.strip().lower()] = canonical
                alias_map[canonical.strip().lower()] = canonical

        folded: dict[str, int] = {}
        for value, cnt in freq_rows:
            normalized_value = (value or "").strip().lower()
            if normalized_value in _PLACEHOLDER_ANSWERS:
                continue
            if _UAT_SEED_MARKER in normalized_value:
                continue
            canonical_value = alias_map.get(normalized_value, value)
            folded[canonical_value] = folded.get(canonical_value, 0) + cnt

        for value in sorted(folded, key=lambda v: folded[v], reverse=True):
            if value in chips:
                continue
            chips.append(value)
            if len(chips) >= limit:
                break
    return chips[:limit]


async def enqueue_merge(kind: str, raw_text: str, step_key: str, telegram_id: int) -> None:
    """Черновик «Другое» → запись в `lookup_merge_queue` для менеджера (план 30-07, тонкая
    настройка, необязательна) — ответ делегата не теряется (must-have плана), а откладывается
    на ручное решение «влить как псевдоним»/«отклонить». Дубль по `(kind, raw_norm,
    status='new')` не плодится (T-30-05): пять одинаковых «Другое» от одного делегата (или от
    пяти разных, но с тем же нормализованным текстом) не дают пять строк очереди — уже стоящая
    необработанная запись достаточна, rate-limit вне фазы (30-RESEARCH.md § Security Domain,
    объём сезона 1000-1500 делегатов)."""
    from database.db import _connect

    raw_text = (raw_text or "").strip()
    if not raw_text:
        return
    raw_norm = normalize_alias(raw_text)
    if not raw_norm:
        return
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")

    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM lookup_merge_queue WHERE kind = ? AND raw_norm = ? AND status = 'new'",
            (kind, raw_norm),
        )
        if await cursor.fetchone():
            return
        await conn.execute(
            "INSERT INTO lookup_merge_queue "
            "(kind, raw_text, raw_norm, step_key, telegram_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'new', ?)",
            (kind, raw_text, raw_norm, step_key, telegram_id, now),
        )
        await conn.commit()


async def pin_chip(kind: str, canonical: str, on: bool) -> None:
    """Закрепление/открепление чипа менеджером (план 30-07) — правит ВСЕ строки
    `lookup_entries` этой каноники разом: псевдонимы одной каноники делят один статус
    закрепления, у чипа нет смысла «закреплён под одним псевдонимом, но не под другим»."""
    from database.db import _connect

    async with _connect() as conn:
        await conn.execute(
            "UPDATE lookup_entries SET pinned = ? WHERE kind = ? AND canonical = ?",
            (1 if on else 0, kind, canonical),
        )
        await conn.commit()


async def merge_queue_items(kind: str, status: str = "new") -> list[dict]:
    """Очередь «Другое» одного справочника/статуса, новые сверху для менеджера (план 30-07,
    экран «📚 Справочники») — постраничность режет список СЮДА возвращённый (тот же приём,
    что `handlers/admin_faq.py::render_faq_screen` — `items[offset:offset+PAGE]` в хендлере,
    второй копии постраничной логики здесь не заводим)."""
    from database.db import _connect

    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT id, raw_text, step_key, telegram_id, created_at FROM lookup_merge_queue "
            "WHERE kind = ? AND status = ? ORDER BY created_at DESC",
            (kind, status),
        )
        rows = await cursor.fetchall()
    return [
        {"id": r[0], "raw_text": r[1], "step_key": r[2], "telegram_id": r[3], "created_at": r[4]}
        for r in rows
    ]


async def merge_apply(item_id: int, canonical: str, admin_id: int) -> bool:
    """«Влить как псевдоним» (план 30-07): очередной сырой ответ делегата становится алиасом
    ВЫБРАННОЙ менеджером каноники (новой или уже существующей — `search_lookup` не различает).
    `INSERT OR IGNORE` — тот же уникальный индекс `(kind, alias_norm)`, что у идемпотентного
    посева, повторное слияние того же текста не плодит вторую строку `lookup_entries`.
    Возвращает `False`, если очередь уже не содержит запись со статусом `new` (обработана
    другим менеджером/из другой вкладки) — хендлер отвечает human-текстом, не тихо молчит."""
    from database.db import _connect

    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT kind, raw_text, raw_norm FROM lookup_merge_queue "
            "WHERE id = ? AND status = 'new'", (item_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        row_kind, raw_text, raw_norm = row
        now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
        await conn.execute(
            "INSERT OR IGNORE INTO lookup_entries "
            "(kind, canonical, alias, alias_norm, source, pinned, added_by, created_at) "
            "VALUES (?, ?, ?, ?, 'manager_merge', 0, ?, ?)",
            (row_kind, canonical, raw_text, raw_norm, admin_id, now),
        )
        await conn.execute(
            "UPDATE lookup_merge_queue SET status = 'merged', decided_by = ?, decided_at = ? "
            "WHERE id = ?",
            (admin_id, now, item_id),
        )
        await conn.commit()
    return True


async def merge_reject(item_id: int, admin_id: int) -> bool:
    """«Отклонить» (план 30-07) — запись уходит из очереди без записи в справочник (мусор/
    опечатка/спам). `WHERE status = 'new'` — тот же guard от двойной обработки, что у
    `merge_apply`."""
    from database.db import _connect

    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as conn:
        cursor = await conn.execute(
            "UPDATE lookup_merge_queue SET status = 'rejected', decided_by = ?, decided_at = ? "
            "WHERE id = ? AND status = 'new'",
            (admin_id, now, item_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def pinned_chips(kind: str) -> list[str]:
    """Список закреплённых каноник (для экрана менеджера «Справочники», план 30-07) —
    отдельно от `top_chips`, которая уже подмешивает частоту сезона; здесь только то, что
    закреплено руками."""
    from database.db import _connect

    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT canonical FROM lookup_entries "
            "WHERE kind = ? AND pinned = 1 ORDER BY canonical",
            (kind,),
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]
