"""Phase 33 (delegate-card admin actions, задачи 2/3): персональные ОДНОРАЗОВЫЕ исключения
из двух глобальных положений — `reg_resubmit_after_reject` («🔁 Разрешить повторную подачу»,
`handlers/applications/admin_resubmit_grant.py`) и `reg_edit_policy` («✏️ Открыть правку после решения»,
`handlers/applications/admin_edit_grant.py`). Общий примитив (та же роль, что `services/city_move.py` для
обеих карточных операций города) — своя таблица `admin_delegate_overrides`
(`database/db.py::grant_delegate_override`/`get_active_delegate_override`/
`revoke_delegate_override`/`consume_delegate_override`).

Инвариант «не больше одной активной строки на (telegram_id, kind)» держит `grant_override`
здесь (проверяет `active_override` ДО вставки) — таблица append-only, повторная выдача после
отзыва/использования заводит НОВУЮ строку, не перезаписывает старую (история выдач видна
целиком, тот же приём, что у `reg_answer_history`).

`services/reg_edit_policy.py::resubmit_gate`/`edit_gate` — ЕДИНСТВЕННЫЕ читатели
`active_override` на пути делегата (peek, не consume: гейт может дёрнуться много раз за один
поход делегата в анкету, гасить исключение на первом же взгляде было бы неправильно).
Погашение — `consume_override`, зовётся РОВНО в точке фактического использования:
`services/reg_finalize.py` (resubmit — ветка «статус rejected -> pending»; edit — любая
реально применённая правка, независимо от того, чем был разрешён этот конкретный вызов —
глобальной политикой или личным исключением, гасить несуществующее активное исключение
безвредно, `consume_delegate_override` тогда просто no-op).

aiogram-free по импортам — вызывается и ботом (`handlers/applications/admin_resubmit_grant.py`/
`admin_edit_grant.py`, `services/reg_finalize.py`), и, если понадобится, веб-процессом
Mini App (тот же гейт `reg_edit_policy` уже общий для обеих поверхностей)."""
from __future__ import annotations

import logging

from database.db import (
    consume_delegate_override,
    get_active_delegate_override,
    grant_delegate_override,
    record_answer_history,
    revoke_delegate_override,
)
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

KIND_RESUBMIT = "resubmit"
KIND_EDIT = "edit"
KINDS = (KIND_RESUBMIT, KIND_EDIT)


def _now() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


async def active_override(telegram_id: int, kind: str) -> dict | None:
    """Публичный peek — `services/reg_edit_policy.py` зовёт это на каждом проходе гейта, БЕЗ
    погашения (см. докстринг модуля)."""
    return await get_active_delegate_override(telegram_id, kind)


async def grant_override(telegram_id: int, kind: str, admin_id: int) -> dict:
    """`{"ok": True, "id", "granted_at"}` при успехе; `{"ok": False, "error"}` — уже есть
    активное исключение того же вида (менеджер сначала должен отозвать старое, экран
    подтверждения не должен молча плодить дубли).

    Атомарно: вставку и проверку «нет активной» держит сам `INSERT OR IGNORE` в
    `grant_delegate_override` поверх частичного UNIQUE-индекса
    (`idx_admin_delegate_overrides_unique_active`) — двойной тап «Выдать» (или гонка двух
    менеджеров) даёт ровно одну активную строку, а не check-then-insert с окном гонки между
    отдельным чтением и вставкой."""
    if kind not in KINDS:
        return {"ok": False, "error": f"Неизвестный вид исключения: {kind!r}"}
    now = _now()
    override_id = await grant_delegate_override(telegram_id, kind, admin_id, now)
    if override_id is None:
        existing = await get_active_delegate_override(telegram_id, kind)
        return {"ok": False, "error": "Исключение уже выдано — сначала отзовите его.", "existing": existing}
    await record_answer_history(
        telegram_id, [{"column": f"{kind}_override", "old": None, "new": "granted"}],
        source=f"admin:{admin_id}",
    )
    return {"ok": True, "id": override_id, "granted_at": now}


async def revoke_override(telegram_id: int, kind: str, admin_id: int) -> dict:
    """`{"ok": True}` при успехе; `{"ok": False, "error"}` — нечего отзывать (уже отозвано или
    делегат успел использовать раньше — не ошибка менеджера, просто гонка)."""
    now = _now()
    revoked = await revoke_delegate_override(telegram_id, kind, admin_id, now)
    if not revoked:
        return {"ok": False, "error": "Исключения уже нет — возможно, его отозвали или делегат успел им воспользоваться."}
    await record_answer_history(
        telegram_id, [{"column": f"{kind}_override", "old": "granted", "new": "revoked"}],
        source=f"admin:{admin_id}",
    )
    return {"ok": True}


async def consume_override(telegram_id: int, kind: str) -> bool:
    """Гасит активное исключение — вызывается из `services/reg_finalize.py` В ТОЧКЕ
    фактического использования (см. докстринг модуля), fail-soft на стороне вызывающего
    (сбой погашения не должен рвать саму подачу/правку делегата)."""
    return await consume_delegate_override(telegram_id, kind, _now())
