"""Phase 23 (APP-TINDER-03, D-02): аватар делегата на карточке заявки.

Очередь модерации может показывать до тысячи заявок за сессию менеджера — без кеша каждый
показ карточки бьёт по `getUserProfilePhotos` (Bot API), это тысяча сетевых вызовов и
гарантированный 429 от Telegram. Поэтому кешируется ОБА исхода: положительный (`file_id`
навсегда — ссылка на файл в Telegram постоянна, перепроверять незачем) и отрицательный
(«фото нет / профиль закрыт» — Bot API не различает эти два случая, и для UI разницы нет —
на сутки, `TTL_SECONDS`: делегат мог сменить настройки приватности или загрузить фото).

Любой сбой Bot API (сеть, 429, что угодно) молча проглатывается в лог: карточка заявки важнее
аватара. `resolve_avatar` в этом случае ничего не кеширует и возвращает `None` — следующий
показ карточки попробует снова, а не застрянет на ошибке навсегда.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from database.db import set_user_avatar
from miniapp import telegram_api
from miniapp.telegram_api import TelegramApiError

logger = logging.getLogger(__name__)

# Срок жизни ОТРИЦАТЕЛЬНОГО результата («фото нет»); положительный file_id не протухает вовсе.
TTL_SECONDS = 24 * 60 * 60

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return datetime.now().strftime(_TS_FORMAT)


def _negative_cache_fresh(checked_at: str | None) -> bool:
    if not checked_at:
        return False
    try:
        checked = datetime.strptime(checked_at, _TS_FORMAT)
    except ValueError:
        return False
    return datetime.now() - checked < timedelta(seconds=TTL_SECONDS)


async def resolve_avatar(cfg, user: dict) -> str | None:
    """`user` — строка `users` (нужны минимум `telegram_id`, `avatar_file_id`,
    `avatar_checked_at`). Кеш -> запрос к Bot API -> кеш результата; ни одного сетевого
    вызова, пока кеш (положительный или ещё не протухший отрицательный) в силе."""
    avatar_file_id = user.get("avatar_file_id")
    if avatar_file_id:
        return avatar_file_id
    if _negative_cache_fresh(user.get("avatar_checked_at")):
        return None

    telegram_id = user["telegram_id"]
    try:
        result = await telegram_api.get_user_profile_photos(cfg, telegram_id, limit=1)
    except TelegramApiError:
        logger.warning("avatars: getUserProfilePhotos недоступен для %s", telegram_id)
        return None

    photos = result.get("photos") or []
    file_id = photos[0][0]["file_id"] if photos else None
    await set_user_avatar(telegram_id, file_id, _now())
    return file_id


def initials(full_name: str | None) -> str:
    """Фолбэк «инициалы на фоне токена темы» (D-02): «Иван Петров» -> «ИП», «Иван» -> «И»,
    пусто/None -> «?». Рисует их фронт, правило вычисления живёт здесь — чтобы не расползлось."""
    parts = (full_name or "").split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[1][0]).upper()
