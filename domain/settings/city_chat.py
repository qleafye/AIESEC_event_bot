"""Тексты геймы и волн по городам и ссылка на чат города (`{chat}`).

Зачем: «вся инфа по гейме — в чате». В общий текст напоминания о дедлайне или итогов волны
ссылку на чат города не вставить — при нескольких городах один текст вёл бы всех в один чат.
Поэтому 29 сообщений, которые делегат получает целым текстом, — городские (`"per_city": True`
в реестре) и понимают `{chat}`: бот подставит ссылку из настройки «💬 Ссылка на чат города»
(`delegate_chat_link`, у каждого города своя). Сам бот ссылку на чат не хранит
(`services/chat_tracking.py` знает только id и название), её задаёт менеджер.

Почему не все ключи групп amb/game: подписи кнопок (ссылка в кнопке не нажимается), строки
экранов с цифрами и заголовки, подсказки шагов сдачи, причины в журнале баллов и менеджерские
тексты остаются общими — вопрос «нужны ли по городам и эти» задан владельцу.

Модуль без импортов проекта: его читают реестр-проверки (placeholders, validation) и бот,
и веб-процесс Mini App.
"""
from __future__ import annotations

import re

CHAT_LINK_KEY = "delegate_chat_link"

CITY_CHAT_TEXT_KEYS: frozenset[str] = frozenset({
    # волны
    "wave_start_message_text", "wave_deadline_reminder_text", "wave_results_announce_text",
    "wave_results_winner_text", "wave_results_prize_text", "wave_rating_closed_text",
    # команда амбассадоров
    "amb_candidate_ack_text", "amb_status_candidate_text", "amb_status_pack_text",
    "amb_status_no_pack_text", "amb_slots_full_text", "amb_taken_text", "amb_removed_text",
    "amb_decline_all_text",
    # ступени
    "amb_tier1_text", "amb_tier2_granted_text", "amb_tier3_text", "amb_tier4_text",
    "amb_tier5_text", "amb_tier1_waitlist_text", "amb_tier2_waitlist_text",
    "amb_tier3_waitlist_text", "amb_tier4_waitlist_text", "amb_tier5_waitlist_text",
    # ссылка и выход
    "referral_link_prompt_text", "ambassador_leave_done_text",
    # гейма
    "game_submit_accepted_text", "game_task_list_empty", "coins_manual_notify_text",
})

_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_+/-]+)", re.IGNORECASE)
_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")
_NO_LINK = ("нет", "без ссылки")


def normalize_chat_link(value: str) -> str | None:
    """Ссылка на чат Telegram -> `https://t.me/<путь>`; «нет» -> "" (убрать ссылку);
    всё остальное -> None. Путь — только `[A-Za-z0-9_+/-]`: такая строка переживает и
    html.escape, и `.format()` вызывающего кода, и не уводит на чужой домен."""
    stripped = (value or "").strip()
    if stripped.lower() in _NO_LINK:
        return ""
    match = _LINK_RE.fullmatch(stripped)
    if match:
        return f"https://t.me/{match.group(1)}"
    match = _USERNAME_RE.fullmatch(stripped)
    if match:
        return f"https://t.me/{match.group(1)}"
    return None
