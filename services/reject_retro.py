"""Ретро-применение правил автоотказа к уже поданным заявкам (предпросмотр + применение).

Одна реализация на кнопку «Применить к уже поданным» (handlers/admin_reject_retro.py) и на
`tools/retro_auto_reject.py`. Решение по каждому делегату берёт тот же движок, что на подаче
анкеты (`services.reg_finalize._auto_reject_patch`):

- «на рассмотрении» -> автоотказ целиком (колонки, статус `rejected`, журнал, письмо с текстом
  правила через `apply_decision_effects` — тихие часы соблюдаются, решение
  `application_decisions` от имени автоотказа);
- уже отклонён вручную -> только пометка (колонки + журнал), второго письма нет;
- одобрен -> не трогается.

Уже помеченные автоотказом пропускаются, поэтому повторное применение ничего не удвоит.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

AUTO_COLUMNS = ["auto_reject_rule_ids", "auto_rejected_at", "flagged_rule_ids", "auto_rule_note", "rejected_at"]
EXAMPLES_LIMIT = 5


async def collect(since: str) -> list[tuple[dict, dict]]:
    """Кого затронут правила: пары (строка делегата, патч движка). Только чтение."""
    from database.db import get_all_users_dicts, get_setting
    from services import reg_finalize

    season = await get_setting("event_season")
    out = []
    for user in await get_all_users_dicts():
        if str(user.get("registration_date") or "")[:10] < since:
            continue
        if user.get("season") not in (season, None):
            continue
        if user.get("auto_reject_rule_ids"):
            continue
        patch = await reg_finalize._auto_reject_patch(user["telegram_id"], user)
        if patch.get("status_override") == "rejected":
            out.append((user, patch))
    return out


def split_targets(pairs: list[tuple[dict, dict]]) -> dict[str, list[tuple[dict, dict]]]:
    """Раскладка по статусу: pending -> автоотказ, rejected -> пометка, approved -> не трогаем."""
    groups: dict[str, list[tuple[dict, dict]]] = {"pending": [], "rejected": [], "approved": []}
    for user, patch in pairs:
        groups.setdefault(user.get("status"), []).append((user, patch))
    return groups


def person_label(user: dict) -> str:
    name = (user.get("full_name") or "").strip()
    username = (user.get("username") or "").lstrip("@")
    who = f"@{username}" if username else f"id {user['telegram_id']}"
    return f"{name} ({who})" if name else who


async def preview(since: str) -> dict:
    """Сводка без единой записи: сколько отклонится, сколько получит пометку, сколько одобренных
    не тронем, плюс примеры."""
    groups = split_targets(await collect(since))
    return {
        "pending": len(groups["pending"]),
        "rejected": len(groups["rejected"]),
        "approved": len(groups["approved"]),
        "examples": [person_label(u) for u, _ in groups["pending"][:EXAMPLES_LIMIT]],
    }


async def apply(bot, since: str, pause: float = 0.1) -> dict:
    """Применяет к тем, кто есть в предпросмотре на момент запуска. Возвращает счётчики."""
    from database.db import set_user_status, update_user_answers
    from services.application_effects import apply_decision_effects
    from services.applications import record_decision
    from services.i18n import context as i18n_context, tr as i18n_tr
    from services.reject_journal import AUTO_DECIDED_BY, record_auto_reject

    groups = split_targets(await collect(since))
    done = {"pending": 0, "rejected": 0, "failed": 0}
    for status in ("pending", "rejected"):
        for user, patch in groups[status]:
            tid = user["telegram_id"]
            try:
                column_patch = {k: patch[k] for k in AUTO_COLUMNS if k in patch}
                if status == "rejected":
                    column_patch.pop("rejected_at", None)
                await update_user_answers(tid, column_patch, allowed_columns=list(column_patch))
                await record_auto_reject(tid, patch["reject_rule_ids"], patch["reject_texts"])
                if status == "pending":
                    await set_user_status(tid, "rejected")
                    lang, tr_map = await i18n_context(tid)
                    reason = "\n\n".join(i18n_tr(t, lang, tr_map) for t in patch["reject_texts"]) or None
                    await apply_decision_effects(bot, tid, "rejected", reason, notify=True, sheet=True)
                    await record_decision(
                        tid, "rejected", reason, AUTO_DECIDED_BY,
                        datetime.strptime(patch["auto_rejected_at"], "%Y-%m-%d %H:%M:%S"),
                        effects_already_sent=True,
                    )
                    if pause:
                        await asyncio.sleep(pause)
                done[status] += 1
            except Exception:
                done["failed"] += 1
                logger.exception("reject_retro: не обработан tid=%s", tid)
    return done
