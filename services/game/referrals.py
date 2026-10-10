"""Авто-баллы амбассадору за одобренного приглашённого (Phase 32, план 32-05, D-20/D-21/D-22/D-37).

32-RESEARCH.md (Pitfall 2) нашёл главную ловушку фазы: единого шва «заявку одобрили» в проекте
не существует. Одиночное одобрение идёт через `services.applications.applications.claim_approve`, массовое —
через `database.db.approve_all_pending` (у которого раньше было ДВА независимых вызывающих — бот
напрямую и веб через `services.applications.applications.claim_approve_all`), а авто-одобрение на финале
анкеты вообще пишет статус в `services/registration/reg_finalize.py`, минуя оба. Побочный эффект, повешенный
только на один из этих путей, — ровно класс инцидента 06.09 (см.
`.planning/.../auto-approve-incident-260906.md`): молчаливое массовое действие без видимого следа
для менеджера. Поэтому начисление — ОДНА функция, врезанная во ВСЕ три пути одинаково, а не три
независимые копии одного правила.

Идемпотентность держит БАЗА ДАННЫХ, а не Python: `database.db.claim_referral_credit_atomic` —
`INSERT OR IGNORE` против `PRIMARY KEY (invitee_id)` у `referral_credits` + `rowcount == 1`,
затем в ТОЙ ЖЕ транзакции `INSERT INTO coins` (фикс находки WR-01, 32-REVIEW.md: раньше это
были два отдельных соединения/коммита — сбой между ними навсегда терял начисление). Повторный
вызов (устаревшая кнопка «Принять всех», два менеджера, бот и веб одновременно, «отклонили и
одобрили снова») физически не может создать вторую строку — проверки «а не начисляли ли мы
уже» в этом модуле нет вовсе (T-32-05-01).

Две служебные точки, которые пишут `users.status = 'approved'` НАПРЯМУЮ, минуя все три шва выше, —
`handlers/access/uat_seed.py` (команда `/uat`, сидер состояний на стенде) и `tools/shoot_screens.py`
(генератор скриншотов для документации). Начисление туда НЕ врезано — это осознанное решение
(T-32-05-07), не пропуск: это dev-инструменты, а не боевой путь одобрения — сидер стенда раздавал
бы амбассадорам реальные баллы за фиктивные тестовые аккаунты, а генератор скриншотов пачкал бы
журнал начислений тестовыми строками. Список закреплён тестом-сторожем швов в
`tests/test_referral_credit_32.py` (обход исходников на предмет новых мест, где `status`
становится `'approved'`), не устной договорённостью — приёмка (план 32-13) явно требует проверять
начисление боевым одобрением, а не `/uat`.

Зависимости — ТОЛЬКО `database.db`, `services.amb.ambassador_waves`, `settings_schema` (плюс
стандартная библиотека). Телеграм-фреймворк и `handlers.*` на уровне модуля не импортируются —
три врезки (`services/applications/applications.py`, `services/registration/reg_finalize.py`) тянут этот модуль ЛЕНИВЫМ
импортом внутри функции именно поэтому: не тащить новый модуль в цепочку импортов веб-процесса
Mini App.
"""
from __future__ import annotations

import logging

from database.db import (
    claim_referral_credit_atomic,
    count_referral_credits,
    get_referral_credit,
    get_setting,
    get_user,
    list_applications_page,
)
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)


def _invitee_reason(invitee: dict) -> str:
    """«Приглашённый: Имя Фамилия» для строки истории монет амбассадора (T-32-05-02 —
    каждое начисление обязано быть видимо человеку, не голой суммой без объяснения)."""
    name = (invitee.get("full_name") or "").strip() or "Без имени"
    return f"Приглашённый: {name}"


async def credit_for_approved(invitee_id: int, *, changed_by: int | None = None) -> dict | None:
    """Тонкая обёртка над журналом зачётов (`services.amb.amb_journal`): `None`, если баллы не
    начислены (не одобрен, нет пригласившего, не амбассадор, исключён, настройка 0, повтор)."""
    from services.amb.amb_journal import _record_one
    return await _record_one(int(invitee_id), changed_by=changed_by, source="approval")


async def credit_for_approved_bulk(invitee_ids) -> dict:
    """Сводка `{"credited", "coins", "ambassadors"}` — см. `services.amb.amb_journal`."""
    from services.amb.amb_journal import on_invitees_approved
    return await on_invitees_approved(invitee_ids)


async def approved_referrals_in_wave(referrer_id: int, wave_id: int | None) -> int:
    """Тонкая обёртка над `database.db.count_referral_credits` — подсказка модератору на
    карточке заявки ДО одобрения («уже N одобренных в этой волне»). Единственная защита
    против накрутки альт-аккаунтами (T-32-05-04, `accept + mitigate` — баллы после начисления
    не отзываются вовсе, D-22, поэтому единственный рычаг стоит строго до одобрения)."""
    return await count_referral_credits(referrer_id, wave_id)


async def backfill_approved(*, dry_run: bool, season: str | None = None) -> dict:
    """Разовое начисление задним числом (D-23) — по всем `users` со `status = 'approved'` и
    непустым `referrer_id`, у кого ЕЩЁ НЕТ строки в `referral_credits` и чей пригласивший
    ПРЯМО СЕЙЧАС амбассадор. `wave_id` ВСЕГДА `None`, `source = 'backfill'` — задним числом
    баллы идут ТОЛЬКО в общий зачёт, ни в одну волну (правило волны применимо только к
    «живому» одобрению, где волна пригласившего резолвится в момент самого события).

    WR-17 (32-REVIEW.md): кандидаты фильтруются по `users.season` — БЕЗ этого фильтра под
    бэкафилл попадали и легаси-строки (season пуст, дефолт миграции — таких около 590), и
    482 делегата, импортированных из прошлого сезона (`season` = прошлый сезон явно, задача
    была именно НЕ дать им упереться в тупик на `/start`, а не выдать баллы задним числом).
    `season=None` (по умолчанию, как у CLI-обёртки без `--season`) резолвит ТЕКУЩИЙ
    `bot_settings.event_season`; явный `season=""`/строка сравнивается буквально —
    `invitee.get("season")` и цель сравниваются как есть (`None == None` у события без
    настроенного сезона — единственный случай, где фильтр остаётся «пропускающим», как и до
    этой находки, если сезон вообще не сконфигурирован).

    `dry_run=True` (по умолчанию у CLI-обёртки `tools/backfill_referral_credits.py`) ничего не
    пишет в базу — но возвращает те же поля, что и реальный запуск, спроецированные из числа
    кандидатов («что БЫ произошло»), плюс `breakdown` — список «амбассадор — сколько
    приглашённых — сколько баллов» для предпоказа перед `--apply` (операция необратима, D-22
    — менеджер обязан увидеть, КОМУ и СКОЛЬКО начислится, а не только три голых числа).

    Уже начисленный «живым» путём приглашённый (одиночное/массовое/авто-одобрение, source
    `'approval'`) в кандидаты не попадает — `get_referral_credit` уже нашёл строку.
    Повторный запуск бэкафилла идемпотентен по той же причине: второй проход видит те же
    строки `referral_credits`, что первый уже создал, и пропускает их."""
    coins = int(await get_setting_typed("ambassador_referral_coins") or 0)
    resolved_season = season if season is not None else (
        (await get_setting("event_season") or "").strip() or None
    )
    rows = await list_applications_page(status="approved", limit=100000, offset=0)

    candidates = 0
    credited = 0
    coins_total = 0
    ambassadors: set[int] = set()
    breakdown: dict[int, dict] = {}

    for row in rows:
        invitee_id = int(row["telegram_id"])
        if await get_referral_credit(invitee_id):
            continue
        invitee = await get_user(invitee_id)
        if not invitee:
            continue
        if (invitee.get("season") or None) != resolved_season:
            continue
        referrer_id_raw = invitee.get("referrer_id")
        if not referrer_id_raw:
            continue
        referrer_id = int(referrer_id_raw)
        referrer = await get_user(referrer_id)
        if not referrer or int(referrer.get("is_ambassador") or 0) != 1:
            continue
        if coins <= 0:
            continue

        candidates += 1
        ambassadors.add(referrer_id)
        entry = breakdown.setdefault(referrer_id, {
            "referrer_id": referrer_id,
            "referrer_name": (referrer.get("full_name") or "").strip() or f"#{referrer_id}",
            "invitees": 0,
            "coins": 0,
        })
        entry["invitees"] += 1
        entry["coins"] += coins
        if dry_run:
            continue

        won = await claim_referral_credit_atomic(
            invitee_id, referrer_id, coins, None,
            reason=_invitee_reason(invitee), changed_by=None, source="backfill",
        )
        if won:
            credited += 1
            coins_total += coins

    if dry_run:
        credited = candidates
        coins_total = coins * candidates

    return {
        "candidates": candidates, "credited": credited, "coins": coins_total,
        "ambassadors": len(ambassadors), "season": resolved_season,
        "breakdown": sorted(breakdown.values(), key=lambda e: (-e["coins"], e["referrer_id"])),
    }
