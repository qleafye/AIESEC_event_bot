"""Разовое применение действующих правил автоотказа к уже поданным заявкам.

Решение владельца 23.09: правило «Курс из списка» завели 22.09 вечером, а заявки под него шли с
понедельника. Всем, кто подал анкету с даты `--since` и подходит под ВКЛЮЧЁННЫЕ сейчас правила
(своего города и трека), — тот же исход, что дал бы движок на подаче:

- статус «на рассмотрении» -> автоотказ целиком: колонки автоотказа, статус `rejected`, строка
  журнала автоотказов, письмо делегату с текстом правила (через `apply_decision_effects` —
  тихие часы соблюдаются: ночью письмо встаёт в очередь), статус в листе, решение
  `application_decisions` от имени автоотказа;
- уже отклонён вручную -> только пометка (колонки + журнал), чтобы статистика «какое правило
  сколько отсеяло» и «реальные заявки» считались верно; второго письма нет, ручное решение
  в `application_decisions` не трогается;
- одобрен -> не трогается.

Уже помеченные автоотказом пропускаются — повторный запуск ничего не удвоит.

Запуск на сервере (по умолчанию — только отчёт, без единой записи):
    docker exec -w /app -e PYTHONPATH=/app youlead26-bot-1 python tools/retro_auto_reject.py --since 2026-09-21
    ... --apply     # применить
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from tools.send_resume_recovery import build_bot  # noqa: E402 — тот же Bot/прокси, что у main.py

AUTO_COLUMNS = ["auto_reject_rule_ids", "auto_rejected_at", "flagged_rule_ids", "auto_rule_note", "rejected_at"]


async def collect(since: str) -> list[tuple[dict, dict]]:
    from database.db import get_all_users_dicts, get_setting
    from services.reg_finalize import _auto_reject_patch

    season = await get_setting("event_season")
    out = []
    for user in await get_all_users_dicts():
        if str(user.get("registration_date") or "")[:10] < since:
            continue
        if user.get("season") not in (season, None):
            continue
        if user.get("auto_reject_rule_ids"):
            continue
        patch = await _auto_reject_patch(user["telegram_id"], user)
        if patch.get("status_override") == "rejected":
            out.append((user, patch))
    return out


async def apply(pairs: list[tuple[dict, dict]]) -> None:
    from database.db import set_user_status, update_user_answers
    from services.application_effects import apply_decision_effects
    from services.applications import record_decision
    from services.i18n import context as i18n_context, tr as i18n_tr
    from services.reject_journal import AUTO_DECIDED_BY, record_auto_reject

    bot = await build_bot()
    done = {"pending": 0, "rejected": 0}
    try:
        for user, patch in pairs:
            tid = user["telegram_id"]
            status = user.get("status")
            column_patch = {k: patch[k] for k in AUTO_COLUMNS if k in patch}
            if status == "rejected":
                # Ручной отказ уже со своей датой — её не переписываем.
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
                await asyncio.sleep(0.1)
            done[status] += 1
            print(f"  ok {tid} ({status})")
    finally:
        await bot.session.close()
    print(f"Готово: автоотказ {done['pending']}, пометка у отклонённых вручную {done['rejected']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", required=True, help="дата подачи анкеты, с которой применять (ГГГГ-ММ-ДД)")
    parser.add_argument("--apply", action="store_true", help="применить (без флага — только отчёт)")
    args = parser.parse_args()

    pairs = await collect(args.since)
    by_status: dict[str, list] = {}
    for user, _ in pairs:
        by_status.setdefault(user.get("status"), []).append(user["telegram_id"])
    print(f"Под правила с {args.since}: " + ", ".join(f"{k}={len(v)}" for k, v in by_status.items()))
    targets = [(u, p) for u, p in pairs if u.get("status") in ("pending", "rejected")]
    print(f"Будет обработано: {len(targets)} (одобренные не трогаются: {len(by_status.get('approved', []))})")
    if not args.apply:
        print("Отчёт без изменений. Для применения добавьте --apply.")
        return
    await apply(targets)


if __name__ == "__main__":
    asyncio.run(main())
