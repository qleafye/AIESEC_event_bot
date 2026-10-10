"""Проверка подстановок `{имя}` в текстах настроек при сохранении.

Корневой aiogram-free модуль: его зовут и бот (`handlers/admin_settings_placeholders.py`),
и веб-процесс Mini App (`settings_ops.validate_batch_item`), а пакет `handlers` тянет aiogram.

Менеджер правит тексты сам и может случайно стереть `{deadline}` или опечататься
(`{dedline}`) — делегат получил бы «Оплатите до {dedline}». `check` ловит оба случая до записи.
Считаются только токены вида `{имя}`; скобки без имени (HTML, JSON, `{1}`) не трогаются.

Набор подстановок ключа: поле `placeholders` записи реестра; иначе имена из default
плюс `PROMPT_ONLY_PLACEHOLDERS` (ключи, где подстановка описана только в prompt).
Подписи берутся из `PLACEHOLDER_LABELS` — центральной таблицы, чтобы не править сотню записей.
"""
import difflib
import re
from dataclasses import dataclass, field

from domain.cities import split_per_city_key
from domain.settings.schema import SETTINGS_SCHEMA

TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

PLACEHOLDER_LABELS: dict[str, str] = {
    "absent": "число отсутствующих", "amount": "сумма", "balance": "баланс",
    "breakdown": "разбивка по заданиям", "cities": "список городов", "city": "город",
    "claim_status": "статус обращения", "coins": "число коинов", "competency": "название компетенции",
    "consequence": "последствие изменения", "count": "количество", "date": "дата",
    "dates": "даты", "day": "день форума", "days": "число дней", "deadline": "дата закрытия записи",
    "default": "значение по умолчанию", "delta": "изменение баланса", "display": "отображаемое имя",
    "domain": "домен", "done": "число выполненного", "ends": "дата окончания волны",
    "entity": "название сущности", "filled": "число заполненных полей", "gap": "отставание до соседа",
    "id": "номер обращения", "intro": "вводный текст", "label": "подпись", "left": "сколько осталось",
    "level": "уровень делегата", "link": "ссылка", "list": "список", "max": "максимум", "min": "минимум",
    "minutes": "число минут", "n": "номер", "name": "имя", "new": "название новой сессии",
    "next_step": "следующий шаг", "noun": "название пункта", "not_found": "число ненайденных",
    "old": "название прежней сессии", "option": "вариант оплаты", "page": "номер страницы",
    "pending": "число ожидающих", "penalized": "число штрафов", "penalties": "штрафы",
    "place": "место", "points": "баллы", "present": "число присутствующих", "prize": "приз",
    "qualified": "число зачтённых", "query": "поисковый запрос", "rank": "место в рейтинге",
    "reason": "причина", "requisites": "реквизиты", "rows": "число строк", "section": "раздел",
    "selected": "число выбранного", "sessions": "сессии", "shown": "число показанных",
    "skipped": "число пропущенных", "status": "статус", "step": "номер шага", "suggestions": "подсказки",
    "tab": "название вкладки", "target_city": "город переноса", "task": "название задания",
    "tasks": "задания", "time": "время", "title": "название сессии", "top": "лидеры",
    "total": "общее число", "university": "вуз", "value": "значение", "wave": "название волны",
    "week": "неделя", "where": "место", "who": "кто", "winners": "победители",
    "season": "название сезона", "remaining": "сколько осталось", "currency": "название валюты",
    "event": "название мероприятия",
}

PROMPT_ONLY_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "start_text_returning": ("season",),
    "nudge_text": ("remaining",),
    "amb_tier1_next_label": ("n",),
    "wave_results_prize_text": ("prize",),
    "miniapp_hub_pending_days": ("days",),
    "chat_rating_post_total_title": ("week", "currency"),
    "chat_rating_post_footer": ("week", "currency"),
    "leaderboard_rank_line_text": ("total",),
    "chat_rating_post_title_rules": ("currency",),
}


def _base_key(key: str) -> str:
    split = split_per_city_key(key)
    return split[0] if split else key


def expected_placeholders(key: str) -> dict[str, str]:
    """Имя подстановки -> человеческая подпись для ключа (составной per_city сводится к базовому)."""
    base = _base_key(key)
    entry = SETTINGS_SCHEMA.get(base) or {}
    names: dict[str, str] = dict(entry.get("placeholders") or {})
    if not names:
        default = entry.get("default")
        for text in default if isinstance(default, list) else [default]:
            if isinstance(text, str):
                for n in TOKEN_RE.findall(text):
                    names.setdefault(n, "")
        for n in PROMPT_ONLY_PLACEHOLDERS.get(base, ()):
            names.setdefault(n, "")
    return {n: lbl or PLACEHOLDER_LABELS.get(n, "") for n, lbl in names.items()}


@dataclass
class PlaceholderCheck:
    missing: list[tuple[str, str]] = field(default_factory=list)
    unknown: list[tuple[str, str | None]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unknown


def _names(text) -> list[str]:
    return list(dict.fromkeys(TOKEN_RE.findall(text))) if isinstance(text, str) else []


def check(key: str, value, previous) -> PlaceholderCheck:
    """Пропавшие (были в previous, нет в value) и неизвестные (нет среди подстановок ключа)."""
    expected = expected_placeholders(key)
    new = _names(value)
    res = PlaceholderCheck()
    for n in _names(previous):
        if n in expected and n not in new:
            res.missing.append((n, expected[n] or n))
    for n in new:
        if n not in expected:
            close = difflib.get_close_matches(n, list(expected), n=1, cutoff=0.6)
            res.unknown.append((n, close[0] if close else None))
    # «{dedline}» вместо «{deadline}» — это опечатка, а не две проблемы: пропажу не дублируем
    fixed = {sug for _n, sug in res.unknown if sug}
    res.missing = [(n, lbl) for n, lbl in res.missing if n not in fixed]
    return res


def problem_text(key: str, result: PlaceholderCheck) -> str:
    lines = [f"Пропала {{{n}}} — в сообщении не будет: {lbl}." for n, lbl in result.missing]
    for n, sug in result.unknown:
        tail = f" — может, {{{sug}}}?" if sug else "."
        lines.append(f"Не знаю {{{n}}}{tail}")
    return "\n".join(lines)


def hint(key: str, prompt: str | None = None) -> str:
    """«Подстановки: {x} — подпись; … Скобки бот заменит сам.» или пустая строка. Если в
    подсказке ключа (`prompt`) подстановки уже перечислены, строка не дублируется."""
    expected = expected_placeholders(key)
    if not expected or (prompt and "Подстановки:" in prompt):
        return ""
    parts = "; ".join(f"{{{n}}} — {lbl or n}" for n, lbl in expected.items())
    return f"Подстановки: {parts}. Скобки бот заменит сам."
