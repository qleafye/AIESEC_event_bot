"""Имена ключей реестра для ступеней амбассадоров 1–5.

Чистый модуль без импортов проекта: его копирует в образ дашборд (отдельный контейнер, как
`chat_score.py`), а бот и веб берут отсюда одну и ту же карту.

Ступень 2 и «следующий шаг» ступеней 2 и 3 появились раньше обобщения и живут под прежними
ключами (`amb_tier2_granted_text`, `amb_o2o_quota`, `amb_next_step_o2o_text` ...). Переименовывать
их нельзя — сохранённые значения стеков пропали бы. Остальные ключи строятся по шаблону
`amb_tier{N}_{вид}`.
"""
from __future__ import annotations

MAX_TIERS = 5

KINDS = ("threshold", "text", "quota", "quota_on", "waitlist", "next")

_LEGACY_TIER_KEYS: dict[tuple[int, str], str] = {
    (2, "text"): "amb_tier2_granted_text",
    (2, "quota"): "amb_o2o_quota",
    (2, "waitlist"): "amb_tier2_waitlist_text",
    (2, "next"): "amb_next_step_o2o_text",
    (3, "next"): "amb_next_step_networking_text",
}

_TEMPLATES = {
    "threshold": "amb_tier{n}_threshold",
    "text": "amb_tier{n}_text",
    "quota": "amb_tier{n}_quota",
    "quota_on": "amb_tier{n}_quota_on",
    "waitlist": "amb_tier{n}_waitlist_text",
    "next": "amb_tier{n}_next_label",
}


def tier_key(n: int, kind: str) -> str:
    """Ключ реестра ступени `n` (1..5) для вида `kind` (threshold/text/quota/quota_on/waitlist/
    next). Вне диапазона или неизвестный вид — ValueError."""
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_TIERS:
        raise ValueError(f"ступень вне диапазона 1..{MAX_TIERS}: {n!r}")
    if kind not in _TEMPLATES:
        raise ValueError(f"неизвестный вид ключа ступени: {kind!r}")
    return _LEGACY_TIER_KEYS.get((n, kind)) or _TEMPLATES[kind].format(n=n)
