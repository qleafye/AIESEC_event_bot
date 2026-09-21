"""Авто-баллы амбассадору за одобренного приглашённого (Phase 32, план 32-05, D-20/D-21/D-22/D-37).

32-RESEARCH.md (Pitfall 2) нашёл главную ловушку фазы: единого шва «заявку одобрили» в проекте
не существует. Одиночное одобрение идёт через `services.applications.claim_approve`, массовое —
через `database.db.approve_all_pending` (у которого раньше было ДВА независимых вызывающих — бот
напрямую и веб через `services.applications.claim_approve_all`), а авто-одобрение на финале
анкеты вообще пишет статус в `services/reg_finalize.py`, минуя оба. Побочный эффект, повешенный
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
`handlers/uat_seed.py` (команда `/uat`, сидер состояний на стенде) и `tools/shoot_screens.py`
(генератор скриншотов для документации). Начисление туда НЕ врезано — это осознанное решение
(T-32-05-07), не пропуск: это dev-инструменты, а не боевой путь одобрения — сидер стенда раздавал
бы амбассадорам реальные баллы за фиктивные тестовые аккаунты, а генератор скриншотов пачкал бы
журнал начислений тестовыми строками. Список закреплён тестом-сторожем швов в
`tests/test_referral_credit_32.py` (обход исходников на предмет новых мест, где `status`
становится `'approved'`), не устной договорённостью — приёмка (план 32-13) явно требует проверять
начисление боевым одобрением, а не `/uat`.

Зависимости — ТОЛЬКО `database.db`, `services.ambassador_waves`, `settings_schema` (плюс
стандартная библиотека). Телеграм-фреймворк и `handlers.*` на уровне модуля не импортируются —
три врезки (`services/applications.py`, `services/reg_finalize.py`) тянут этот модуль ЛЕНИВЫМ
импортом внутри функции именно поэтому: не тащить новый модуль в цепочку импортов веб-процесса
Mini App.
"""
from __future__ import annotations

import logging

from database.db import (
    claim_referral_credit_atomic,
    count_referral_credits,
    get_referral_credit,
    get_user,
    list_applications_page,
)
from services.ambassador_waves import current_wave_for_city_raw, wave_eligible
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)


def _invitee_reason(invitee: dict) -> str:
    """«Приглашённый: Имя Фамилия» для строки истории монет амбассадора (T-32-05-02 —
    каждое начисление обязано быть видимо человеку, не голой суммой без объяснения)."""
    name = (invitee.get("full_name") or "").strip() or "Без имени"
    return f"Приглашённый: {name}"


async def credit_for_approved(invitee_id: int, *, changed_by: int | None = None) -> dict | None:
    """Единственная точка начисления. Безопасна при случайном вызове (заявка ещё не
    `approved`, приглашённого не существует, пригласившего не существует) — во всех этих
    случаях просто возвращает `None`, ничего не пишет.

    Порядок проверок (все обязательны, D-20/D-37):
    1. Заявка приглашённого ДЕЙСТВИТЕЛЬНО в статусе `approved` прямо сейчас (не «поданная»,
       не «на модерации» — за поданную заявку не начисляется ничего).
    2. У приглашённого непустой `referrer_id`.
    3. Пригласивший существует и `is_ambassador == 1` ПРЯМО СЕЙЧАС (D-37: обычный делегат со
       старой реф-ссылкой и человек, вышедший из амбассадоров, ничего не получают; старое поле
       анкеты «хочу быть амбассадором» к текущему статусу отношения не имеет и здесь НЕ
       проверяется вовсе — читается только актуальный флаг членства).
    4. `ambassador_referral_coins` больше нуля (дефолт 0 на каждом живом событии — Rule «фича
       выключена по умолчанию»).

    Волна — `current_wave_for_city_raw(город пригласившего)` (WR-03, 32-REVIEW.md:
    нормализует сырой `event_city`, иначе легаси-амбассадор дефолтного города теряет волну
    своего города), но только если пригласивший в ней участвует (`wave_eligible`, D-31/D-38:
    вступивший посреди волны в неё не попадает) — иначе `wave_id = None`, баллы идут только в
    общий зачёт.

    Запись — `claim_referral_credit_atomic` (WR-01, 32-REVIEW.md): квитанция
    `referral_credits` и начисление в `coins` пишутся ОДНОЙ транзакцией, не двумя отдельными
    соединениями — сбой между ними раньше навсегда терял начисление (квитанция есть, монет
    нет, повтор её видит и молча пропускает). При проигранной гонке (`False`) вторая половина
    не выполняется вовсе, эта функция тихо возвращает `None`.

    Вся функция fail-soft (T-32-05-05): любое исключение логируется, возвращается `None` —
    сбой начисления не имеет права отменить уже состоявшееся одобрение заявки, статус
    приглашённого уже записан отдельной атомарной операцией ДО вызова этой функции."""
    try:
        invitee = await get_user(invitee_id)
        if not invitee or invitee.get("status") != "approved":
            return None

        referrer_id_raw = invitee.get("referrer_id")
        if not referrer_id_raw:
            return None
        referrer_id = int(referrer_id_raw)

        referrer = await get_user(referrer_id)
        if not referrer or int(referrer.get("is_ambassador") or 0) != 1:
            return None

        coins = int(await get_setting_typed("ambassador_referral_coins") or 0)
        if coins <= 0:
            return None

        wave_id: int | None = None
        wave = await current_wave_for_city_raw(referrer.get("event_city"))
        if wave and wave_eligible(referrer, wave):
            wave_id = int(wave["id"])

        invitee_name = (invitee.get("full_name") or "").strip() or "Без имени"
        won = await claim_referral_credit_atomic(
            int(invitee_id), referrer_id, coins, wave_id,
            reason=_invitee_reason(invitee), changed_by=changed_by, source="approval",
        )
        if not won:
            return None

        return {
            "referrer_id": referrer_id, "coins": coins, "wave_id": wave_id,
            "invitee_name": invitee_name,
        }
    except Exception:
        logger.exception("credit_for_approved failed for invitee_id=%s", invitee_id)
        return None


async def credit_for_approved_bulk(invitee_ids) -> dict:
    """Цикл по списку id, только что одобренных «Принять всех» (D-20/D-22: массовый побочный
    эффект не имеет права быть молчаливым) — сводка для текста подтверждения менеджеру:
    `{"credited": сколько приглашённых реально начислило, "coins": сумма баллов,
    "ambassadors": скольким РАЗНЫМ амбассадорам начислено}`. Каждый элемент идёт через ту же
    `credit_for_approved` — устаревшая повторная «Принять всех» (пустой список `ids`) просто
    не создаёт итераций, сводка нулевая."""
    credited = 0
    coins_total = 0
    ambassadors: set[int] = set()
    for invitee_id in invitee_ids:
        result = await credit_for_approved(invitee_id)
        if result:
            credited += 1
            coins_total += result["coins"]
            ambassadors.add(result["referrer_id"])
    return {"credited": credited, "coins": coins_total, "ambassadors": len(ambassadors)}


async def approved_referrals_in_wave(referrer_id: int, wave_id: int | None) -> int:
    """Тонкая обёртка над `database.db.count_referral_credits` — подсказка модератору на
    карточке заявки ДО одобрения («уже N одобренных в этой волне»). Единственная защита
    против накрутки альт-аккаунтами (T-32-05-04, `accept + mitigate` — баллы после начисления
    не отзываются вовсе, D-22, поэтому единственный рычаг стоит строго до одобрения)."""
    return await count_referral_credits(referrer_id, wave_id)


async def backfill_approved(*, dry_run: bool) -> dict:
    """Разовое начисление задним числом (D-23) — по всем `users` со `status = 'approved'` и
    непустым `referrer_id`, у кого ЕЩЁ НЕТ строки в `referral_credits` и чей пригласивший
    ПРЯМО СЕЙЧАС амбассадор. `wave_id` ВСЕГДА `None`, `source = 'backfill'` — задним числом
    баллы идут ТОЛЬКО в общий зачёт, ни в одну волну (правило волны применимо только к
    «живому» одобрению, где волна пригласившего резолвится в момент самого события).

    `dry_run=True` (по умолчанию у CLI-обёртки `tools/backfill_referral_credits.py`) ничего не
    пишет в базу — но возвращает те же поля, что и реальный запуск, спроецированные из числа
    кандидатов («что БЫ произошло»), для предпоказа перед `--apply` (менеджер должен увидеть
    цифры ДО необратимой записи, а не после).

    Уже начисленный «живым» путём приглашённый (одиночное/массовое/авто-одобрение, source
    `'approval'`) в кандидаты не попадает — `get_referral_credit` уже нашёл строку.
    Повторный запуск бэкафилла идемпотентен по той же причине: второй проход видит те же
    строки `referral_credits`, что первый уже создал, и пропускает их."""
    coins = int(await get_setting_typed("ambassador_referral_coins") or 0)
    rows = await list_applications_page(status="approved", limit=100000, offset=0)

    candidates = 0
    credited = 0
    coins_total = 0
    ambassadors: set[int] = set()

    for row in rows:
        invitee_id = int(row["telegram_id"])
        if await get_referral_credit(invitee_id):
            continue
        invitee = await get_user(invitee_id)
        if not invitee:
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
        "ambassadors": len(ambassadors),
    }
