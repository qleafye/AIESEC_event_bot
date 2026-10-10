"""Ретро-применение правил автоотказа к уже поданным заявкам (предпросмотр + применение).

Одна реализация на кнопку «Применить к уже поданным» (handlers/applications/admin_reject_retro.py) и на
`tools/retro_auto_reject.py`. Решение по каждому делегату берёт тот же движок, что на подаче
анкеты (`services.reg_finalize._auto_reject_patch`):

- «на рассмотрении» -> автоотказ целиком (колонки, статус `rejected`, журнал, письмо с текстом
  правила через `apply_decision_effects` — тихие часы соблюдаются, решение
  `application_decisions` от имени автоотказа);
- уже отклонён вручную -> только пометка (колонки + журнал), второго письма нет;
- отклонён НАШИМ прошлым прогоном, оборвавшимся до письма (есть живая строка журнала, но нет
  решения в `application_decisions`) -> дошлём письмо и решение: статус `rejected` уже стоит,
  а метка не поставлена как раз потому, что письмо не дошло;
- одобрен -> не трогается.

Уже помеченные автоотказом пропускаются (метка пишется последней), поэтому повторное применение
ничего не удвоит, а прерванное — дообработается.
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


async def resolve_unfinished(groups: dict[str, list[tuple[dict, dict]]]) -> dict[str, list[tuple[dict, dict]]]:
    """Переносит из «rejected» в «pending» тех, кого оборвал наш же прошлый прогон: журнал
    автоотказа есть, а решения человека или автоотказа в `application_decisions` нет. Ручной отказ
    всегда оставляет там строку (бот и веб пишут её при любом решении), автоотказ пишет её
    последней, после письма, — значит, «журнал есть, решения нет» = письмо не дошло. Таким
    записываем `_resume`: статус менять не надо, надо дослать письмо."""
    from database.db import get_last_application_decision, get_live_auto_reject_log_entry

    stay = []
    for user, patch in groups["rejected"]:
        tid = user["telegram_id"]
        if await get_live_auto_reject_log_entry(tid) and not await get_last_application_decision(tid):
            groups["pending"].append(({**user, "_resume": True}, patch))
        else:
            stay.append((user, patch))
    groups["rejected"] = stay
    return groups


def person_label(user: dict) -> str:
    name = (user.get("full_name") or "").strip()
    username = (user.get("username") or "").lstrip("@")
    who = f"@{username}" if username else f"id {user['telegram_id']}"
    return f"{name} ({who})" if name else who


async def preview(since: str) -> dict:
    """Сводка без единой записи: сколько отклонится, сколько получит пометку, сколько одобренных
    не тронем, плюс примеры."""
    groups = await resolve_unfinished(split_targets(await collect(since)))
    return {
        "pending": len(groups["pending"]),
        "rejected": len(groups["rejected"]),
        "approved": len(groups["approved"]),
        "digest": ids_digest(groups["pending"] + groups["rejected"]),
        "examples": [person_label(u) for u, _ in groups["pending"][:EXAMPLES_LIMIT]],
    }


def ids_digest(pairs: list[tuple[dict, dict]]) -> str:
    """Короткий отпечаток списка из предпросмотра: кнопка «Применить» несёт его, и применение
    идёт, только если список за это время не изменился."""
    import hashlib

    ids = sorted(int(u["telegram_id"]) for u, _ in pairs)
    return hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:8]


async def apply(bot, since: str, pause: float = 0.1, ids: set[int] | None = None) -> dict:
    """Применяет к тем, кто есть в предпросмотре (`ids` — список из него; без `ids` — свежая
    выборка). Возвращает счётчики. Порядок на человека: статус «отклонён» проверяемой записью
    (`reject_user`, только из «на рассмотрении»), журнал, письмо, и только потом метка
    `auto_reject_rule_ids` — она «закрывает» строку для повторного запуска, поэтому на сбое
    посередине строка остаётся видна и дообрабатывается, а не теряется. Если сбой пришёлся на
    письмо, повтор шлёт его заново (`resolve_unfinished`); письмо может уйти дважды, если
    оборвалась только запись решения, — это осознанно лучше потерянного."""
    from database.db import get_live_auto_reject_log_entry, reject_user, update_user_answers
    from domain.regform.labels import STATUS_LABELS
    from services.application_effects import apply_decision_effects
    from services.applications import record_decision
    from services.i18n import context as i18n_context, tr as i18n_tr
    from services.reject_journal import AUTO_DECIDED_BY, record_auto_reject

    pairs = await collect(since)
    if ids is not None:
        pairs = [(u, p) for u, p in pairs if int(u["telegram_id"]) in ids]
    groups = await resolve_unfinished(split_targets(pairs))
    done = {"pending": 0, "rejected": 0, "failed": 0, "skipped": 0, "sheet_failed": False}
    sheet_ids: list[int] = []
    for status in ("pending", "rejected"):
        for user, patch in groups[status]:
            tid = user["telegram_id"]
            try:
                column_patch = {k: patch[k] for k in AUTO_COLUMNS if k in patch}
                resume = bool(user.get("_resume"))
                if status == "pending" and not resume:
                    if not await reject_user(tid):
                        done["skipped"] += 1  # успели одобрить/отклонить вручную — не трогаем
                        continue
                elif not resume:
                    column_patch.pop("rejected_at", None)
                if status == "pending":
                    sheet_ids.append(tid)
                if not await get_live_auto_reject_log_entry(tid):  # повтор не растит attempt_count
                    await record_auto_reject(tid, patch["reject_rule_ids"], patch["reject_texts"])
                if status == "pending":
                    lang, tr_map = await i18n_context(tid)
                    reason = "\n\n".join(i18n_tr(t, lang, tr_map) for t in patch["reject_texts"]) or None
                    await apply_decision_effects(bot, tid, "rejected", reason, notify=True, sheet=False)
                    await record_decision(
                        tid, "rejected", reason, AUTO_DECIDED_BY,
                        datetime.strptime(patch["auto_rejected_at"], "%Y-%m-%d %H:%M:%S"),
                        effects_already_sent=True,
                    )
                    if pause:
                        await asyncio.sleep(pause)
                await update_user_answers(tid, column_patch, allowed_columns=list(column_patch))
                done[status] += 1
            except Exception:
                done["failed"] += 1
                logger.exception("reject_retro: не обработан tid=%s", tid)
    if sheet_ids:
        try:
            from services import sheet_target as _sheet_target
            from services.sheets import bulk_update_status_in_sheet

            res = await bulk_update_status_in_sheet({str(t): STATUS_LABELS["rejected"] for t in sheet_ids})
            if res == -1 and _sheet_target.sheets_enabled():
                done["sheet_failed"] = True
                logger.warning("reject_retro: лист не обновлён для %s строк", len(sheet_ids))
        except Exception:
            done["sheet_failed"] = True
            logger.exception("reject_retro: лист не обновлён для %s строк", len(sheet_ids))
    return done
