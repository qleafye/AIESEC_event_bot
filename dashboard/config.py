"""Phase 15 (D-05): плоская конфигурация процесса дашборда.

Никакой pydantic (в отличие от бота) — образ дашборда должен тянуть минимум зависимостей,
а набор ключей плоский, не вложенный. `load_config` читает окружение (по умолчанию
`os.environ`, параметр — для тестов).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# D-03 (пересмотр 22.08): подсеть docker-сети `edge`, в которой живёт контейнер cloudflared —
# единственный возможный источник запроса к origin (порты наружу не публикуются, план 15-06).
DEFAULT_TRUSTED_PROXIES = "172.31.0.0/16"


def _parse_admin_ids(raw: str) -> tuple[int, ...]:
    """Тот же формат, что у бота (`.env.example`: `ADMIN_IDS=[12345678, 87654321]`) —
    квадратные скобки не обязательны, разделитель — запятая. Мусорные токены пропускаются
    молча: список используется только для нотификаций (план 15-04), не для допуска к данным,
    так что падать из-за одного битого токена незачем."""
    cleaned = (raw or "").strip().strip("[]")
    if not cleaned:
        return ()
    ids = []
    for token in cleaned.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError:
            continue
    return tuple(ids)


@dataclass(frozen=True)
class DashboardConfig:
    db_path: str
    public_url: str
    session_secret: str
    bot_username: str
    bot_token: str
    admin_ids: tuple[int, ...]
    proxy_url: str | None
    event_city_default: str
    trusted_proxies: str


def load_config(env: dict | None = None) -> DashboardConfig:
    """Пустой `DASHBOARD_SESSION_SECRET` — не молчаливый дефолт: подпись cookie-сессии с
    предсказуемым секретом хуже, чем явный сбой запуска (`RuntimeError` с понятным текстом,
    не голым `KeyError`)."""
    source = os.environ if env is None else env
    session_secret = source.get("DASHBOARD_SESSION_SECRET", "")
    if not session_secret:
        raise RuntimeError(
            "DASHBOARD_SESSION_SECRET не задан в .env этого стека — без него подпись "
            "cookie-сессии предсказуема. Сгенерируйте случайную строку, например: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    trusted_proxies = source.get("DASHBOARD_TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES) or DEFAULT_TRUSTED_PROXIES
    if trusted_proxies.strip() == "*":
        logger.warning(
            "DASHBOARD_TRUSTED_PROXIES=* — доверяем заголовкам X-Forwarded-Proto/-For от "
            "кого угодно. Так можно, только если снаружи к порту 8000 нет доступа."
        )

    return DashboardConfig(
        db_path=source.get("DASHBOARD_DB_PATH", "data/forum.db"),
        public_url=source.get("DASHBOARD_PUBLIC_URL", ""),
        session_secret=session_secret,
        bot_username=source.get("DASHBOARD_BOT_USERNAME", ""),
        bot_token=source.get("BOT_TOKEN", ""),
        admin_ids=_parse_admin_ids(source.get("ADMIN_IDS", "")),
        proxy_url=source.get("PROXY_URL") or None,
        event_city_default=source.get("EVENT_CITY_DEFAULT", "msk"),
        trusted_proxies=trusted_proxies,
    )
