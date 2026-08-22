"""Phase 15 Plan 04 (STAT-02, D-09/D-03): подпись Telegram Login Widget и параметры сессии.

Алгоритм проверки — `core.telegram.org/widgets/login`: строка для подписи склеивается из
ВСЕХ полей, кроме `hash`, отсортированных по имени, как `k=v` через `\n`; секрет —
`sha256(bot_token)`; сравнение — ТОЛЬКО `hmac.compare_digest` (не `==` — тайминг-атака на
byte-by-byte сравнение строк иначе утекает подпись по времени ответа).

Работа за Cloudflare Tunnel (D-03, пересмотр 22.08): до origin всегда доходит `http` (TLS
терминирует Cloudflare, порт наружу не открыт вовсе — план 15-06). `https_only=True` в
параметрах сессии — КОНСТАНТА, а не производная от схемы запроса: если выставлять флаг «по
факту схемы», Secure не выставится никогда, потому что схема на origin всегда `http`.
Внешний канал при этом всегда HTTPS. Не «чинить» это обратно на `request.url.scheme == "https"`.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Mapping

# D-09: сессия живёт 30 дней в подписанной cookie.
SESSION_COOKIE = "yl_dash"
SESSION_MAX_AGE = 30 * 24 * 3600

# Допустимый забег часов вперёд (`auth_date` из будущего) — Telegram и origin не идеально
# синхронизированы по времени; дальше минуты — уже подозрительно, отвергаем.
_FUTURE_SKEW_SECONDS = 60


@dataclass(frozen=True)
class LoginUser:
    """Поля виджета, пригодные для отрисовки экрана входа. НЕ логировать целиком — как
    минимум `id`/`username` являются идентифицирующими данными (см. Phase 17, п.1 «в логах
    нет ПД»)."""
    id: int
    username: str | None
    first_name: str | None
    photo_url: str | None


def verify_login_payload(
    params: Mapping[str, str],
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
    now: float | None = None,
) -> int | None:
    """Возвращает `telegram_id` при валидной свежей подписи, иначе `None` — никогда не
    поднимает исключение на кривом вводе (отсутствующий `hash`, нечисловой/отсутствующий
    `auth_date`, метка из будущего дальше минуты — всё сводится к `None`).

    `params` не мутируется: работаем с копией, потому что вызывающий код передаёт сюда
    query-параметры реального запроса, которые не должны портиться побочным эффектом.
    `now` — только для тестов (не спать секундами в реальном времени)."""
    data = dict(params)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(data.get("auth_date"))
    except (TypeError, ValueError):
        return None

    current = time.time() if now is None else now
    age = current - auth_date
    if age < -_FUTURE_SKEW_SECONDS:
        return None
    if age > max_age_seconds:
        return None

    try:
        return int(data.get("id"))
    except (TypeError, ValueError):
        return None


def session_middleware_kwargs(cfg) -> dict:
    """Параметры для `starlette.middleware.sessions.SessionMiddleware`, заданные в ОДНОМ
    месте, чтобы план 15-05 их не изобретал заново. `https_only=True` — см. модульный
    докстринг про Cloudflare Tunnel; `HttpOnly` Starlette ставит сам, отдельно передавать
    не нужно."""
    return {
        "secret_key": cfg.session_secret,
        "session_cookie": SESSION_COOKIE,
        "max_age": SESSION_MAX_AGE,
        "same_site": "lax",
        "https_only": True,
    }


def client_ip(headers: Mapping[str, str]) -> str | None:
    """Адрес клиента за Cloudflare — ТОЛЬКО для логов и грубого троттлинга. Ни решение о
    доступе (dashboard/access.py), ни ключ антиспама D-11 (dashboard/notify.py) НИКОГДА не
    смотрят на этот заголовок: `CF-Connecting-IP`/`X-Forwarded-For` ставит прокси, а
    подделать их тривиально всякому, кто дотянется до origin в обход туннеля."""
    cf_ip = headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip() or None
    forwarded = headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return None
