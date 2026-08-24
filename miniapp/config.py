"""Phase 19 (D-01): конфигурация процесса Mini App и константы-лимиты.

Конфигурация — тот же плоский `DashboardConfig` (общий `.env` стека: `DASHBOARD_SESSION_SECRET`
даёт ОДИНАКОВУЮ подпись cookie `yl_dash` в обоих процессах — без этого cookie-ветка D-05 не
работала бы). Не копия, а тонкая обёртка над `dashboard.config.load_config`.

Лимиты сдачи продублированы из `handlers/user_actions.py` (`MAX_PARTS`, `MAX_TEXT_PART`)
сознательно — импортировать их нельзя (пакет `handlers` тянет aiogram); паритет числа
закреплён тестом `tests/test_miniapp_registry.py`. При расхождении с ботом ломается
паритет сдач: делегат из приложения смог бы приложить больше частей, чем из бота.
"""
from __future__ import annotations

from dashboard.config import DashboardConfig, load_config

# D-03 / RESEARCH Pitfall 7: единый потолок файла = лимит `getFile` (20 МБ) — больше
# менеджер не сможет открыть из приложения. Фото крупнее 10 МБ уходят как документ
# (`sendPhoto` принимает ≤ 10 МБ).
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PHOTO_MAX_BYTES = 10 * 1024 * 1024

# 260824-8qw (HG-01): единственный источник потолков ТЕЛА запроса для `miniapp.body_limit`.
# MULTIPART_SLACK — обвязка multipart поверх самого файла (имя поля, boundary, заголовки
# частей формы) — без запаса честная загрузка ровно в MAX_UPLOAD_BYTES отсеклась бы как
# "слишком большая" из-за нескольких лишних байт протокола, не из-за размера файла.
# MAX_BODY_BYTES — потолок ТЕЛА маршрута загрузки (файл + обвязка) — больше MAX_UPLOAD_BYTES
# ровно на MULTIPART_SLACK; человеку в ответе 413 всё равно показывается MAX_UPLOAD_BYTES
# (потолок файла, а не потолок тела с обвязкой). DEFAULT_MAX_BODY_BYTES — общий потолок тела
# для всех остальных маршрутов `/app/api/*` (JSON-тела в килобайтах — миллиона байт хватает
# с большим запасом; раньше эти маршруты были безлимитными).
MULTIPART_SLACK = 64 * 1024
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + MULTIPART_SLACK
DEFAULT_MAX_BODY_BYTES = 1024 * 1024

# handlers/user_actions.py: MAX_PARTS / MAX_TEXT_PART.
MAX_PARTS = 20
MAX_TEXT_PART = 1000

# D-09: окно свежести `auth_date` как у Login Widget (сутки). Не занижать «до 5 минут» —
# `auth_date` фиксируется при открытии и не обновляется, пока приложение открыто
# (RESEARCH Pitfall 2) — короткое окно убило бы менеджерские сессии.
INIT_DATA_MAX_AGE = 86400


def load_miniapp_config(env: dict | None = None) -> DashboardConfig:
    """Тот же `.env`, тот же разбор, та же ошибка при пустом `DASHBOARD_SESSION_SECRET`."""
    return load_config(env)


__all__ = [
    "DashboardConfig",
    "DEFAULT_MAX_BODY_BYTES",
    "INIT_DATA_MAX_AGE",
    "MAX_BODY_BYTES",
    "MAX_PARTS",
    "MAX_TEXT_PART",
    "MAX_UPLOAD_BYTES",
    "MULTIPART_SLACK",
    "PHOTO_MAX_BYTES",
    "load_miniapp_config",
]
