"""Phase 26.1 Plan 02 (SD-03, T-26.1-02-01/02/03/04/05/07): периметр супердашборда —
Cloudflare Access. Единственное место в проекте, где проверяется подпись
`Cf-Access-Jwt-Assertion`.

Решение владельца 06.09 (`26.1-CONTEXT.md` `<owner_decision_260906_access>`): вход на
супердашборд НЕ через Telegram Login Widget — приложение доверяет только Cloudflare Access.
Cookie `CF_Authorization` НЕ читается вовсе (Cloudflare параллельно её ставит, но заголовок —
единственный источник, который приложение обязано проверить само на случай запроса,
пришедшего в origin мимо туннеля).

Алгоритм фиксируется списком `["RS256"]` в САМОМ вызове `jwt.decode` — `alg` из заголовка
токена нигде не читается. Это закрывает и `alg: none`, и подмену на HS256 с публичным ключом
Cloudflare в роли HMAC-секрета (T-26.1-02-01): даже если бы такой токен дошёл до `jwt.decode`,
явный список допустимых алгоритмов заставит библиотеку отклонить его до проверки подписи.

Любая ошибка проверки (просрочен, чужой `aud`/`iss`, битая подпись, неизвестный `kid`,
недоступный JWKS, отсутствующий `email`) сводится к `None` — исключение наружу никогда не
поднимается (T-26.1-02-06: недоступность JWKS — тоже отказ, fail-closed). В лог идёт только
причина отказа, НИКОГДА сам токен или его claims (T-26.1-02-07).
"""
from __future__ import annotations

import logging
from typing import Optional

import jwt

logger = logging.getLogger(__name__)

# Заголовок, который Cloudflare Access ставит на запросы, прошедшие проверку команды —
# единственный источник токена. Cookie CF_Authorization намеренно не читается (один источник).
ACCESS_HEADER = "Cf-Access-Jwt-Assertion"

# JWKS команды кэшируется на уровне процесса — модульный ленивый синглтон, а не аргумент по
# умолчанию функции (иначе каждый импорт модуля в тестах плодил бы свой клиент). Время жизни
# кэша ключей — 10 минут (T-26.1-02: «порядка 5-10 минут», ротация ключей Cloudflare не настолько
# частая, чтобы бить по JWKS на каждый запрос).
_JWKS_CACHE_LIFESPAN_SECONDS = 600
_jwks_client: "jwt.PyJWKClient | None" = None


def reset_jwks_cache() -> None:
    """Сбрасывает синглтон JWKS-клиента — используется тестами (и годится для ручного сброса
    при смене команды Zero Trust без рестарта процесса)."""
    global _jwks_client
    _jwks_client = None


def _get_jwks_client(cfg) -> "jwt.PyJWKClient":
    global _jwks_client
    if _jwks_client is None:
        url = f"https://{cfg.access_team_domain}.cloudflareaccess.com/cdn-cgi/access/certs"
        _jwks_client = jwt.PyJWKClient(
            url, cache_keys=True, lifespan=_JWKS_CACHE_LIFESPAN_SECONDS,
        )
    return _jwks_client


def verify_access_token(
    token: str,
    cfg,
    *,
    jwks_client=None,
    now: Optional[float] = None,
) -> Optional[str]:
    """Возвращает e-mail в нижнем регистре при валидном токене, иначе `None` — никогда не
    поднимает исключение наружу.

    `jwks_client` — шов для тестов (фикстурный ключ без сети); без него используется модульный
    ленивый синглтон на `cfg.access_team_domain`. `now` принят для единообразия с остальными
    функциями дашборда, проверяемыми временем (`dashboard.auth.verify_login_payload`), но не
    участвует в проверке `exp`/`iat` — эту часть делает сама PyJWT по реальным часам процесса
    (`leeway=60`); тесты просрочки строят токен со сдвигом ОТНОСИТЕЛЬНО реального времени, а не
    управляют временем через этот параметр.
    """
    if not token:
        return None
    client = jwks_client if jwks_client is not None else _get_jwks_client(cfg)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=cfg.access_aud,
            issuer=f"https://{cfg.access_team_domain}.cloudflareaccess.com",
            leeway=60,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed: ЛЮБАЯ ошибка проверки = отказ.
        logger.warning(
            "Cloudflare Access: токен отклонён (%s)", type(exc).__name__,
        )
        return None

    email = claims.get("email")
    if not email or not isinstance(email, str):
        logger.warning("Cloudflare Access: в валидном токене нет поля email")
        return None
    return email.strip().lower()


def require_superadmin_email(request, cfg, *, jwks_client=None) -> tuple[Optional[str], str]:
    """Единственная точка входа маршрута `/compare`: `(e-mail | None, причина)`. Причина —
    `"no_header" | "bad_token" | "not_allowed" | "ok"` — идёт в лог маршрута, на страницу
    попадает только человеческий текст без деталей проверки (T-26.1-02-07).

    `cfg.access_dev_bypass` — ТОЛЬКО `DASHBOARD_ACCESS_DEV_BYPASS=1`, локальный запуск/тесты
    рендера: периметр полностью отключён, `logger.warning` пишется на каждый вызов — так
    случайно оставленный байпас в проде виден по логам с первого же запроса (T-26.1-02-05).
    """
    if cfg.access_dev_bypass:
        logger.warning(
            "Cloudflare Access: DASHBOARD_ACCESS_DEV_BYPASS включён — периметр отключён, "
            "допуск выдан без проверки токена. В проде эту переменную не задавать."
        )
        return "dev@local", "ok"

    token = request.headers.get(ACCESS_HEADER)
    if not token:
        return None, "no_header"

    email = verify_access_token(token, cfg, jwks_client=jwks_client)
    if email is None:
        return None, "bad_token"

    # Fail-closed: пустой cfg.superadmin_emails = отказ ВСЕМ, инверсии условия быть не должно.
    if email not in cfg.superadmin_emails:
        logger.warning("Cloudflare Access: e-mail %s не входит в список суперадминов", email)
        return None, "not_allowed"

    return email, "ok"
