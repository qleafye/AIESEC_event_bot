"""Phase 15 (D-05): плоская конфигурация процесса дашборда.

Никакой pydantic (в отличие от бота) — образ дашборда должен тянуть минимум зависимостей,
а набор ключей плоский, не вложенный. `load_config` читает окружение (по умолчанию
`os.environ`, параметр — для тестов).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from dashboard import registry

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


def _parse_emails(raw: str) -> tuple[str, ...]:
    """Phase 26.1 (SD-01/T-26.1-01-02): список e-mail суперадминов супердашборда. Те же
    разделители, что у списков в проекте (запятая, «;», перевод строки — ловушка «мобильный
    Enter = отправка», CLAUDE.md), обрезка пробелов, приведение к нижнему регистру,
    дедупликация с сохранением порядка. Токен без «@» — опечатка, а не адрес: пропускается
    с `logger.warning`, не попадает в список молча."""
    if not raw:
        return ()
    tokens: list[str] = []
    for line in raw.splitlines():
        for chunk in line.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                tokens.append(chunk)
    emails: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        normalized = token.lower()
        if "@" not in normalized:
            logger.warning(
                "DASHBOARD_SUPERADMIN_EMAILS: %r похоже на опечатку (нет «@») — "
                "пропущено", token,
            )
            continue
        if normalized in seen:
            continue
        emails.append(normalized)
        seen.add(normalized)
    return tuple(emails)


_TEAM_DOMAIN_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_TEAM_DOMAIN_SUFFIX_RE = re.compile(r"\.cloudflareaccess\.com$", re.IGNORECASE)


def _normalize_team_domain(raw: str) -> str:
    """`DASHBOARD_ACCESS_TEAM_DOMAIN` ожидается как голый поддомен команды Zero Trust
    (напр. `aiesec`). Если владелец по ошибке вписал полный URL — отрезаем схему и хвост
    `.cloudflareaccess.com`, иначе адрес JWKS соберётся кривым (план 26.1-02)."""
    value = (raw or "").strip()
    if not value:
        return ""
    value = _TEAM_DOMAIN_SCHEME_RE.sub("", value)
    value = value.split("/", 1)[0]
    value = _TEAM_DOMAIN_SUFFIX_RE.sub("", value)
    return value


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
    # Phase 26.1 (SD-01): поля СТРОГО с дефолтами и СТРОГО в конце списка — у всех полей
    # выше дефолтов нет, существующие вызовы `DashboardConfig(**base)` с явным набором
    # kwargs не должны сломаться (tests/test_dashboard_render.py::_cfg и аналоги).
    events: tuple = ()
    superadmin_emails: tuple[str, ...] = ()
    access_team_domain: str = ""
    access_aud: str = ""
    access_dev_bypass: bool = False


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

    # Phase 26.1 (SD-01, T-26.1-01-02/07): реестр событий + периметр Cloudflare Access.
    # `DASHBOARD_SUPERADMINS` (telegram_id) сознательно НЕ заводится — решение владельца
    # 06.09 (26.1-CONTEXT.md `<owner_decision_260906_access>`): вход не через Telegram.
    events = registry.parse_events(source.get("DASHBOARD_EVENTS", ""))
    superadmin_emails = _parse_emails(source.get("DASHBOARD_SUPERADMIN_EMAILS", ""))
    access_team_domain = _normalize_team_domain(source.get("DASHBOARD_ACCESS_TEAM_DOMAIN", ""))
    access_aud = source.get("DASHBOARD_ACCESS_AUD", "")
    access_dev_bypass = source.get("DASHBOARD_ACCESS_DEV_BYPASS", "") == "1"

    if registry.multi_mode(events):
        if not superadmin_emails:
            logger.warning(
                "DASHBOARD_EVENTS задаёт %d событий (мульти-режим), а "
                "DASHBOARD_SUPERADMIN_EMAILS пуст — сводный экран будет закрыт для всех "
                "(fail-closed: пусто значит никому, не всем)", len(events),
            )
        if not access_team_domain or not access_aud:
            logger.warning(
                "DASHBOARD_EVENTS задаёт мульти-режим, а DASHBOARD_ACCESS_TEAM_DOMAIN/"
                "DASHBOARD_ACCESS_AUD не заданы — проверить токен Cloudflare Access будет "
                "нечем, сводный экран будет отдавать отказ всем"
            )
    if access_dev_bypass:
        logger.warning(
            "DASHBOARD_ACCESS_DEV_BYPASS включён — периметр Cloudflare Access отключён. "
            "Так можно запускать только локально, в проде эту переменную не задавать."
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
        events=events,
        superadmin_emails=superadmin_emails,
        access_team_domain=access_team_domain,
        access_aud=access_aud,
        access_dev_bypass=access_dev_bypass,
    )
