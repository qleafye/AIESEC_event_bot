"""Phase 19 (D-05/D-06/D-09): модель доступа Mini App — зависимости FastAPI.

Две ветки аутентификации в одном `principal` (D-05):
1. заголовок `X-Telegram-Init-Data` — подпись Bot API (`miniapp.auth.verify_init_data`);
2. cookie-сессия дашборда `yl_dash` (тот же `DASHBOARD_SESSION_SECRET`) — ТОЛЬКО для
   staff: без единого права — 403 `staff_only`. Делегатские экраны по cookie закрыты
   отдельно в `delegate_gate` (`kind: "cookie"`), даже если человек одновременно staff —
   у него для этого есть initData-вход.

Права и город пересчитываются на КАЖДЫЙ запрос (`dashboard.access.resolve_capabilities`
/`staff_city` по read-only `sqlite3`-подключению) — снятое в боте право отражается на
следующем запросе без перелогина. В этом модуле нет и не может быть мемоизации прав
(D-09, T-19-05): ни декоратора, ни модульного словаря, ни чего-либо, переживающего один
вызов — сторожевой тест в `tests/test_miniapp_headers.py` ловит слово-триггер грепом,
потому здесь нет и самого слова.

CSRF (T-19-04): мутирующие запросы (POST/PATCH/PUT/DELETE) по cookie-ветке требуют
заголовка `X-Requested-With: fetch` — `SameSite=Lax` пропускает top-level POST с чужого
сайта, а кастомный заголовок cross-site форма поставить не может. Для initData-ветки
требование не действует: подпись в заголовке — сама по себе доказательство намерения.

Модуль aiogram-free: `_gate_decision` СКОПИРОВАН из `handlers/user_actions.py:55-66`
(импортировать нельзя — пакетный `handlers/__init__.py` тянет aiogram).

Коды ошибок — по `<api_contract>` плана 19-01 (см. докстринг `miniapp/routers/__init__.py`):
`detail` исключения — словарь с полем `reason`, обработчик в `miniapp.main` отдаёт его
телом ответа как есть.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from dashboard.access import resolve_capabilities, staff_city
from dashboard.db import read_conn
from database.db import get_reg_draft
from settings_schema import _parse_setting

from miniapp.auth import verify_init_data

CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "fetch"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Права, дающие staff-ветку `upload_actor` (подпись `miniapp_upload_caption_staff`, тот же
# маршрут и лимит): `moderate_game` — обложки заданий (19-06), `settings` — фото/файлы
# настроек (Phase 22, D-05/T-22-05: нового транспорта для них не заводится).
STAFF_UPLOAD_CAPS = frozenset({"moderate_game", "settings"})

# Разделы-чекбоксы D-06: имя раздела -> ключ реестра `miniapp_section_{section}`.
# "form" (Phase 21 Plan 02, FORM-SYNC-05, D-08) — рядом с "profile": оба делегатские разделы.
SECTIONS = (
    "tasks",
    "coins",
    "leaderboard",
    "profile",
    "form",
    "review",
    "admin_tasks",
    "stats",
    "settings",
)


@dataclass(frozen=True)
class Principal:
    telegram_id: int
    via: str  # "initdata" | "cookie"
    caps: frozenset[str]
    city: str | None  # staff_city; None = все города
    # Phase 21 (21-10, FORM-SYNC-04): username из подписанного initData.user (cookie-ветка ->
    # None — дашборд его не несёт). Нужен ТОЛЬКО форме анкеты (services.reg_finalize.finalize_data
    # ждёт настоящий username для НОВОЙ регистрации целиком через Mini App, без единого касания
    # чата бота); остальные потребители Principal поле не используют.
    username: str | None = None

    @property
    def is_staff(self) -> bool:
        return bool(self.caps)


@dataclass(frozen=True)
class UploadActor:
    """Результат `upload_actor`: кто загружает файл и какую подпись ему ставить —
    `miniapp_upload_caption_staff` при `is_staff_upload`, иначе
    `miniapp_upload_caption_delegate`."""
    telegram_id: int
    via: str
    caps: frozenset[str]
    city: str | None
    is_staff_upload: bool


def read_setting(conn, key: str):
    """Типизированное чтение ключа реестра по синхронному read-only подключению: тот же
    `settings_schema._parse_setting`, что у `get_setting_typed`, но без aiosqlite/async —
    дефолт реестра при отсутствующей строке."""
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
    raw = row["value"] if row is not None else None
    return _parse_setting(key, raw)


def _gate_decision(status) -> tuple[bool, str | None]:
    """Копия `handlers.user_actions._gate_decision`: legacy/missing/unknown -> allowed."""
    status = status or "approved"
    if status == "pending":
        return False, "pending"
    if status == "rejected":
        return False, "rejected"
    return True, None


def _user_status(conn, telegram_id: int) -> tuple[bool, str | None]:
    """`(registered, status)` — нет строки в `users` -> незарегистрирован."""
    row = conn.execute(
        "SELECT status FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if row is None:
        return False, None
    return True, row["status"]


def delegate_denial(conn, p: Principal) -> str | None:
    """Причина отказа делегатского гейта или `None` (пропущен). Вынесено из зависимости,
    чтобы `/app/api/me` мог отдать `is_delegate` без исключения."""
    if p.via == "cookie":
        return "cookie"  # D-05: делегатские экраны по cookie недоступны в принципе
    if read_setting(conn, "miniapp_staff_only") == "on" and not p.caps:
        return "staff_only_mode"
    registered, status = _user_status(conn, p.telegram_id)
    if not registered:
        # Аналог `ensure_registered` в боте: без анкеты заданий нет.
        return "unregistered"
    allowed, kind = _gate_decision(status)
    return None if allowed else kind


def principal(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
) -> Principal:
    cfg = request.app.state.cfg
    username: str | None = None
    if x_telegram_init_data:
        data = verify_init_data(x_telegram_init_data, cfg.bot_token)
        if data is None:
            raise HTTPException(401, {"reason": "bad_initdata"})
        telegram_id, via = int(data["user"]["id"]), "initdata"
        username = data["user"].get("username")
    elif "session" in request.scope and request.session.get("telegram_id"):
        try:
            telegram_id = int(request.session["telegram_id"])
        except (TypeError, ValueError):
            raise HTTPException(401, {"reason": "no_auth"})
        via = "cookie"
        if (
            request.method.upper() in _MUTATING_METHODS
            and request.headers.get(CSRF_HEADER, "").lower() != CSRF_HEADER_VALUE
        ):
            raise HTTPException(403, {"reason": "csrf"})
    else:
        raise HTTPException(401, {"reason": "no_auth"})

    with read_conn(cfg.db_path) as conn:
        caps = frozenset(resolve_capabilities(conn, telegram_id, cfg.admin_ids))
        city = staff_city(conn, telegram_id)

    if via == "cookie" and not caps:
        raise HTTPException(403, {"reason": "staff_only"})
    return Principal(telegram_id=telegram_id, via=via, caps=caps, city=city, username=username)


def require_cap(cap: str):
    def _dep(p: Principal = Depends(principal)) -> Principal:
        if cap not in p.caps:
            raise HTTPException(403, {"reason": "no_cap", "cap": cap})
        return p

    return _dep


def require_section(section: str):
    if section not in SECTIONS:
        raise ValueError(f"unknown miniapp section: {section!r}")

    def _dep(request: Request, p: Principal = Depends(principal)) -> Principal:
        with read_conn(request.app.state.cfg.db_path) as conn:
            if read_setting(conn, f"miniapp_section_{section}") != "on":
                raise HTTPException(403, {"reason": "section_off", "section": section})
        return p

    return _dep


def delegate_gate(request: Request, p: Principal = Depends(principal)) -> Principal:
    with read_conn(request.app.state.cfg.db_path) as conn:
        kind = delegate_denial(conn, p)
    if kind is not None:
        raise HTTPException(403, {"reason": "delegate_gate", "kind": kind})
    return p


def form_gate(request: Request, p: Principal = Depends(principal)) -> Principal:
    """Анкета — НЕ `delegate_gate` (RESEARCH Pitfall 9): незарегистрированный/pending/rejected
    делегат обязан пройти — это ровно тот, у кого черновик `kind='new'` и есть (`delegate_gate`
    отдал бы ему 403 на его же анкету). Уважает только режим доступа: cookie-ветка закрыта
    (анкета — исключительно initData, как и остальные делегатские экраны, D-05), и
    `miniapp_staff_only` — как у `delegate_denial`, но без `_user_status`/`_gate_decision`."""
    if p.via == "cookie":
        raise HTTPException(403, {"reason": "delegate_gate", "kind": "cookie"})
    with read_conn(request.app.state.cfg.db_path) as conn:
        if read_setting(conn, "miniapp_staff_only") == "on" and not p.caps:
            raise HTTPException(403, {"reason": "delegate_gate", "kind": "staff_only_mode"})
    return p


async def upload_actor(request: Request, p: Principal = Depends(principal)) -> UploadActor:
    """Три сценария на один маршрут `POST /app/api/uploads`: делегат прикладывает часть сдачи,
    менеджер с правом из `STAFF_UPLOAD_CAPS` грузит обложку задания (план 19-06) или
    фото/файл настройки (Phase 22), ЛИБО (план 21-10, D-05)
    делегат без прошедшего делегатского гейта (`unregistered`/`pending`/`rejected`), но с живым
    черновиком анкеты, грузит резюме — тот же маршрут, третья ветка `delegate_denial`, не копия
    (RESEARCH Pitfall 9: анкета — не `delegate_gate`). `cookie`/`staff_only_mode` НЕ пропускаются
    этой веткой — это гейты режима доступа, а не статуса регистрации."""
    with read_conn(request.app.state.cfg.db_path) as conn:
        kind = delegate_denial(conn, p)
    if kind is None:
        staff_upload = False
    elif p.caps & STAFF_UPLOAD_CAPS:
        staff_upload = True
    elif kind in ("unregistered", "pending", "rejected") and await get_reg_draft(p.telegram_id) is not None:
        staff_upload = False
    else:
        raise HTTPException(403, {"reason": "delegate_gate", "kind": kind})
    return UploadActor(
        telegram_id=p.telegram_id,
        via=p.via,
        caps=p.caps,
        city=p.city,
        is_staff_upload=staff_upload,
    )


__all__ = [
    "CSRF_HEADER",
    "CSRF_HEADER_VALUE",
    "Principal",
    "SECTIONS",
    "STAFF_UPLOAD_CAPS",
    "UploadActor",
    "delegate_denial",
    "delegate_gate",
    "form_gate",
    "principal",
    "read_setting",
    "require_cap",
    "require_section",
    "upload_actor",
]
