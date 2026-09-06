"""Quick 260906-8uq (FAQ-01..06): единственное место, где живёт правило видимости пункта
FAQ делегату.

Чистый модуль (форма `services/questions.py`, только этот вовсе не ходит в базу — ни одного
импорта `database.db`, ни одного импорта `aiogram`): бот и Mini App читают правило отсюда
ОДИН раз, второй копии «городской пункт перекрывает общий» в проекте нет и не будет.

Правило (интерфейсный контракт квика):
    видимые делегату = пункты, у которых city IS NULL («все города») или city = город делегата;
    если городской пункт несёт ТОТ ЖЕ нормализованный вопрос, что и общий — общий скрывается.

Перекрытие сравнивается по НОРМАЛИЗОВАННОМУ вопросу, а не второй колонкой-ссылкой (например
`overrides_id`), потому что менеджер заводит городской пункт КОПИРУЯ вопрос руками (CLAUDE.md:
кнопки и человеческий ввод, а не связывание пунктов по id, которого менеджер даже не видит) —
вторая колонка потребовала бы отдельного экрана «привязать к пункту», а совпадение текста
вопроса и есть тот сигнал, которым сам менеджер помечает «это тот же вопрос, но для моего
города»."""
from __future__ import annotations

import re

# Хвостовые кавычки/пунктуация, которые срезаются ПОСЛЕ схлопывания пробелов — делегат может
# набрать вопрос с двойным «??» или пробелом перед знаком, менеджер при копировании вопроса в
# городской пункт — без него; оба варианта обязаны нормализоваться в один и тот же ключ.
_TRAILING_PUNCT_RE = re.compile(r'[\s?!.,;:"\'«»“”]+$')
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_question(text: str | None) -> str:
    """`casefold` (не `lower` — устойчиво к языковым спецсимволам), схлопывание внутренних
    пробелов в один, срез хвостовых `?!.` и кавычек. Используется и правилом перекрытия
    (`apply_city_overrides`), и будущей подсказкой «спрашивали N раз» в журнале вопросов
    (`services.questions`, follow-up — см. not_in_scope квика)."""
    normalized = (text or "").strip().casefold()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    normalized = _TRAILING_PUNCT_RE.sub("", normalized)
    return normalized


def apply_city_overrides(rows: list[dict], city_code: str | None) -> list[dict]:
    """Из произвольного набора строк `faq_items` (city IS NULL или любой другой код) отбирает
    видимые делегату этого города и скрывает общий пункт, если ГОРОДСКОЙ пункт несёт тот же
    нормализованный вопрос. `city_code=None` -> видны только пункты с `city is None` (ветка
    `r.get("city") == city_code` при `city_code=None` истинна ТОЛЬКО когда `r["city"]` тоже
    `None` — второй записи этого правила не требуется).

    Сортировка — ЕДИНСТВЕННОЕ место, где заводится порядок (position, id): равные `position`
    (легаси-нули, перестановка в другом городе) не должны давать случайный порядок между
    запусками, вторичный ключ `id` (порядок создания) — детерминированная тай-брейк-развязка."""
    visible = [r for r in rows if r.get("city") is None or r.get("city") == city_code]
    city_questions = {
        normalize_question(r.get("question")) for r in visible if r.get("city") is not None
    }
    result = [
        r for r in visible
        if not (r.get("city") is None and normalize_question(r.get("question")) in city_questions)
    ]
    result.sort(key=lambda r: (r.get("position") or 0, r.get("id") or 0))
    return result


def city_badge(label: str | None) -> str:
    """Значок «кому виден пункт» для строки списка/карточки менеджера. `label=None` — пункт
    без городской привязки («все города»); иначе — человеческая подпись города, которую
    менеджеру УЖЕ показывает `cities.city_label` (сюда приходит готовая строка, не код)."""
    if label is None:
        return "🌍 все города"
    return f"🏙 {label}"


def short(text: str | None, limit: int) -> str:
    """Обрезка с многоточием для подписи кнопки/строки списка. Не экранирует HTML — вызывающий
    решает сам, куда идёт результат (подпись inline-кнопки Telegram не парсит разметку вовсе,
    текст сообщения — по месту использования)."""
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: max(0, limit - 1)].rstrip() + "…"
