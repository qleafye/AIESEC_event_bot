"""Phase 19 Plan 01 Task 2 (WEBAPP-01, D-09, T-19-01/T-19-02): `miniapp.auth.verify_init_data`.

Helpers `sign_init_data`/`make_init_data` — общая фикстура initData с известной подписью,
переиспользуется всеми последующими планами фазы. Независимый оракул —
`aiogram.utils.web_app.check_webapp_signature` (в тестах aiogram доступен; сам пакет
`miniapp` его не импортирует — сторож в `tests/test_miniapp_headers.py`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from aiogram.utils.web_app import check_webapp_signature

from miniapp.auth import verify_init_data

TOKEN = "123456:ABCDEF-testtoken"
USER_ID = 900001


def sign_init_data(fields: dict, token: str = TOKEN) -> str:
    """Подписывает пары как это делает Telegram: check-string из ДЕКОДИРОВАННЫХ значений,
    результат — URL-encoded query-string (то, что лежит в `Telegram.WebApp.initData`)."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def make_init_data(
    user_id: int = USER_ID,
    auth_date: int | None = None,
    *,
    token: str = TOKEN,
    first_name: str = "Тест",
    user_extra: dict | None = None,
    **extra,
) -> str:
    user = {"id": user_id, "first_name": first_name, "username": "t", "language_code": "ru"}
    user.update(user_extra or {})
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
        **extra,
    }
    return sign_init_data(fields, token)


# ── фикстура согласована с оракулом ──────────────────────────────────────────────────────

def test_fixture_is_valid_for_aiogram_oracle():
    assert check_webapp_signature(TOKEN, make_init_data())


def test_valid_init_data_returns_parsed_user():
    data = verify_init_data(make_init_data(), TOKEN)
    assert data is not None
    assert data["user"]["id"] == USER_ID
    assert data["user"]["first_name"] == "Тест"
    assert "hash" not in data
    assert data["query_id"] == "AAHdF6IQAAAAAN0XohDhrOrc"


def test_signature_field_stays_in_check_string():
    """Bot API 7.10: `signature` (Ed25519) — обычное поле, из check-string исключается
    ТОЛЬКО `hash`; оракул aiogram считает так же."""
    s = make_init_data(signature="abcDEF123")
    assert check_webapp_signature(TOKEN, s)
    assert verify_init_data(s, TOKEN)["signature"] == "abcDEF123"


# ── отказы ───────────────────────────────────────────────────────────────────────────────

def test_tampered_user_rejected_by_both():
    s = make_init_data().replace(str(USER_ID), "900002")
    assert verify_init_data(s, TOKEN) is None
    assert not check_webapp_signature(TOKEN, s)


def test_wrong_token_rejected():
    s = make_init_data(token="999:other")
    assert verify_init_data(s, TOKEN) is None


def test_missing_hash_rejected():
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": USER_ID})}
    assert verify_init_data(urlencode(fields), TOKEN) is None


def test_empty_and_garbage_input_do_not_raise():
    assert verify_init_data("", TOKEN) is None
    assert verify_init_data("not=a&valid", TOKEN) is None
    assert verify_init_data("hash=abc", TOKEN) is None
    assert verify_init_data("&&&=", TOKEN) is None  # strict_parsing -> ValueError -> None
    assert verify_init_data(None, TOKEN) is None  # type: ignore[arg-type]


def test_expired_auth_date_rejected():
    now = 1_800_000_000
    s = make_init_data(auth_date=now - 86400 - 1)
    assert verify_init_data(s, TOKEN, now=now) is None
    assert verify_init_data(make_init_data(auth_date=now - 86400 + 5), TOKEN, now=now)


def test_custom_max_age_respected():
    now = 1_800_000_000
    s = make_init_data(auth_date=now - 600)
    assert verify_init_data(s, TOKEN, max_age_seconds=300, now=now) is None
    assert verify_init_data(s, TOKEN, max_age_seconds=900, now=now)


def test_auth_date_from_future_beyond_skew_rejected():
    now = 1_800_000_000
    assert verify_init_data(make_init_data(auth_date=now + 61), TOKEN, now=now) is None
    assert verify_init_data(make_init_data(auth_date=now + 30), TOKEN, now=now)


def test_non_numeric_or_missing_auth_date_rejected():
    fields = {"user": json.dumps({"id": USER_ID}), "auth_date": "soon"}
    assert verify_init_data(sign_init_data(fields), TOKEN) is None
    fields = {"user": json.dumps({"id": USER_ID})}
    assert verify_init_data(sign_init_data(fields), TOKEN) is None


def test_without_user_rejected():
    """Reply-кнопка `web_app` даёт «простой» web view без `user` (RESEARCH Pitfall 1)."""
    fields = {"auth_date": str(int(time.time())), "query_id": "x"}
    s = sign_init_data(fields)
    assert check_webapp_signature(TOKEN, s)  # подпись верна…
    assert verify_init_data(s, TOKEN) is None  # …но аутентифицировать некого


def test_user_without_id_or_non_json_rejected():
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"first_name": "x"})}
    assert verify_init_data(sign_init_data(fields), TOKEN) is None
    fields = {"auth_date": str(int(time.time())), "user": "not-json"}
    assert verify_init_data(sign_init_data(fields), TOKEN) is None
    fields = {"auth_date": str(int(time.time())), "user": json.dumps([1, 2])}
    assert verify_init_data(sign_init_data(fields), TOKEN) is None


# ── Pitfall 6: не-ASCII в user ───────────────────────────────────────────────────────────

def test_cyrillic_and_emoji_in_first_name_pass():
    s = make_init_data(first_name="Алёна 🎯 ✨", user_extra={"last_name": "Ёжикова"})
    assert check_webapp_signature(TOKEN, s)
    data = verify_init_data(s, TOKEN)
    assert data is not None
    assert data["user"]["first_name"] == "Алёна 🎯 ✨"
    assert data["user"]["last_name"] == "Ёжикова"


def test_init_data_is_ascii_safe_for_http_header():
    s = make_init_data(first_name="Алёна 🎯")
    s.encode("ascii")  # query-string percent-encoded — в заголовок уходит как есть
