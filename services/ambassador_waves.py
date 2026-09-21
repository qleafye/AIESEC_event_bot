"""Правила амбассадорской волны — БЕЗ aiogram, тот же разрез, что `services/applications.py`
против `handlers/admin_moderation.py` (Phase 23, D-06/D-08): экраны (бот сейчас, Mini App
следующей фазой, D-36) не имеют права нести бизнес-правило дважды.

Рейтинг волны считается НА ЧТЕНИИ, а не хранится меткой на строке `coins` — D-14 прямо требует,
чтобы сдача, проверенная после конца волны, всё равно попала в свою волну; единственная
привязка, которая это гарантирует, — `coins.task_id -> game_tasks.wave_id` (см.
`database.db.sum_task_coins_for_wave`, комментарий там же). Участие в волне (кто вообще в ней
считается) определяется ТЕКУЩИМ состоянием `users.is_ambassador`/`ambassador_since` в момент
чтения, а не историческим снимком — D-31/D-32/D-38 читаются буквально так: «стал посреди волны —
не участвует», «вышел — пропал из текущей волны, но не из общего зачёта», «вернулся — снова
новичок посреди волны».

Зависимости — `database.db`, `settings_schema`, `cities` (плюс стандартная библиотека); `cities`
сама не импортирует `aiogram`, так что правило ниже не нарушается. Ни `aiogram`, ни
`handlers.*` на уровне модуля не импортируются — веб-процесс Mini App будущей фазы (D-36)
сможет позвать те же функции напрямую, без второй копии этих правил.
"""
from __future__ import annotations

import statistics
from datetime import datetime

import cities
from database.db import (
    get_display_names,
    get_pending_submissions,
    get_pending_submissions_count,
    get_wave,
    list_active_tasks,
    list_ambassadors,
    list_wave_tasks,
    set_wave_state,
    sum_referral_coins_for_wave,
    sum_task_coins_for_wave,
    wave_at,
)
from services.timeutil import msk_now
from settings_schema import get_setting_typed


# ── Задача 1 (D-14/D-29/D-31/D-32/D-38): участие в волне и её рейтинг ──────────────────────

def wave_eligible(user: dict, wave: dict) -> bool:
    """True тогда и только тогда, когда `user["is_ambassador"] == 1` И (`ambassador_since`
    пусто ИЛИ `ambassador_since <= wave["starts_at"]`). Пустой `ambassador_since` значит «был
    амбассадором ещё до этой фазы» — такой человек участвует в первой же волне (иначе
    обновление молча выкинуло бы из волны всех нынешних амбассадоров). Строковое сравнение
    ISO-меток — тот же приём, что уже используется для «сдано после дедлайна» (game_review_render.py).
    Чистая функция: ни БД, ни реестра."""
    if not user or int(user.get("is_ambassador") or 0) != 1:
        return False
    since = user.get("ambassador_since")
    if not since:
        return True
    return str(since) <= str((wave or {}).get("starts_at") or "")


def eligible_wave_ids(user: dict, waves: list[dict]) -> set[int]:
    """Чистая обёртка над `wave_eligible` — какие из перечисленных волн этому пользователю
    доступны прямо сейчас."""
    return {int(w["id"]) for w in waves if wave_eligible(user, w)}


async def current_wave_for(event_city: str | None, *, now: datetime | None = None) -> dict | None:
    """Волна, идущая прямо сейчас для этого города (`db.wave_at`). `now` — только для тестов;
    по умолчанию московское «сейчас» (`services.timeutil.msk_now`)."""
    ts = (now or msk_now()).strftime("%Y-%m-%d %H:%M:%S")
    return await wave_at(ts, event_city)


async def wave_rating(wave_id: int) -> list[dict]:
    """Рейтинг волны: сумма баллов за задания этой волны (по привязке задания, не по дате
    начисления — D-14а) + авто-баллы за приглашённых, начисленные в датах волны (D-14б),
    оставляя только тех, кто ПРЯМО СЕЙЧАС проходит `wave_eligible` (вышедший из амбассадоров
    исчезает — D-32; вступивший посреди волны не появляется — D-31/D-38). Тот, у кого 0
    баллов, в списке остаётся — он участник волны, просто без очков. Сортировка: баллы по
    убыванию, при равенстве — раньше вступивший выше (`ambassador_since`), затем меньший
    `telegram_id`. Место — «спортивное»: равные баллы делят одно место, следующее место
    сдвигается на число разделивших (классическое 1-2-2-4 ранжирование)."""
    wave = await get_wave(wave_id)
    if not wave:
        return []

    task_sums = await sum_task_coins_for_wave(wave_id)
    ref_sums = await sum_referral_coins_for_wave(wave_id)
    totals: dict[int, int] = {}
    for uid, pts in task_sums.items():
        totals[uid] = totals.get(uid, 0) + int(pts)
    for uid, pts in ref_sums.items():
        totals[uid] = totals.get(uid, 0) + int(pts)

    # Волна со своим городом рейтингует амбассадоров этого города (плюс без города — как и
    # везде в проекте, city_scope=None у волны «все города» не фильтрует вовсе).
    # T-091-08/CITY-02: `list_ambassadors(city_scope=...)` ждёт дескриптор
    # `cities.city_scope(...)`, а не сырой код города — `wave["event_city"]` без обёртки
    # роняет `database.db._city_clause` (`code, exclude = scope` на голой строке).
    ambassadors = await list_ambassadors(city_scope=cities.city_scope(wave.get("event_city")))
    eligible: list[tuple[int, int, str]] = []
    for a in ambassadors:
        user = dict(a)
        user["is_ambassador"] = 1  # list_ambassadors уже отфильтровал WHERE is_ambassador = 1
        if not wave_eligible(user, wave):
            continue
        uid = int(a["telegram_id"])
        eligible.append((uid, totals.get(uid, 0), a.get("ambassador_since") or ""))

    eligible.sort(key=lambda row: (-row[1], row[2], row[0]))
    names = await get_display_names([row[0] for row in eligible])

    rows: list[dict] = []
    place = 0
    prev_points = None
    for idx, (uid, points, _since) in enumerate(eligible, start=1):
        if points != prev_points:
            place = idx
            prev_points = points
        rows.append({
            "user_id": uid,
            "name": names.get(uid, ""),
            "points": points,
            "place": place,
        })
    return rows


async def wave_rating_view(wave_id: int, viewer_id: int) -> dict:
    """То, что видит амбассадор (D-29 — граница раскрытия данных, а не украшение экрана):
    `own` (`place`/`points`/`total`/`gap_to_prize`) и `prize_places`. `rows` заполняется
    ТОЛЬКО при `wave_rating_show_names == "on"`; при `"off"` — пустой список, ни одно чужое
    имя в ответе не встречается вовсе. `gap_to_prize` — сколько баллов не хватает до
    последнего призового места, 0 если уже там, `None` если участников меньше числа призовых
    мест (тогда «отсечки» ещё нет). `viewer_id` не участник волны -> `own = None`."""
    wave = await get_wave(wave_id)
    rating = await wave_rating(wave_id)
    prize_places = int((wave or {}).get("prize_places") or await get_setting_typed("wave_prize_places"))
    total = len(rating)

    own = None
    for row in rating:
        if row["user_id"] != viewer_id:
            continue
        if total < prize_places:
            gap = None
        elif row["place"] <= prize_places:
            gap = 0
        else:
            cutoff_points = rating[prize_places - 1]["points"]
            gap = max(cutoff_points - row["points"], 0)
        own = {
            "place": row["place"],
            "points": row["points"],
            "total": total,
            "gap_to_prize": gap,
        }
        break

    show_names = await get_setting_typed("wave_rating_show_names")
    return {
        "own": own,
        "prize_places": prize_places,
        "rows": rating if show_names == "on" else [],
    }


# ── Задача 2 (D-16/D-17): сводка конца волны и переход состояния ──────────────────────────

async def wave_end_summary(wave_id: int) -> dict:
    """Данные для сообщения менеджеру в конце волны (D-16) — только числа, сама формулировка
    текста живёт в ключе реестра `wave_end_manager_text`. `top` — первые N строк
    `wave_rating` (N = своё число волны либо общая настройка `wave_prize_places`). `pending` —
    сколько сдач по заданиям ИМЕННО этой волны сейчас `pending` (задания чужой волны и «вне
    волн» не считаются). `pending_near_cutoff` — сколько из них принадлежат амбассадорам,
    чьё текущее место сейчас в пределах одного призового места от отсечки («эти проверить в
    первую очередь»); если посчитать невозможно — 0, а не исключение (fail-soft, того же духа,
    что и `referral_ratio_hint`, T-32-03-05)."""
    wave = await get_wave(wave_id)
    rating = await wave_rating(wave_id)
    prize_places = int((wave or {}).get("prize_places") or await get_setting_typed("wave_prize_places"))
    top = rating[:prize_places]

    pending = 0
    pending_near_cutoff = 0
    try:
        wave_tasks = await list_wave_tasks(wave_id, active_only=False)
        task_ids = {int(t["id"]) for t in wave_tasks}
        if task_ids:
            total_pending = await get_pending_submissions_count()
            pending_rows = await get_pending_submissions(limit=total_pending) if total_pending else []
            wave_pending = [r for r in pending_rows if int(r.get("task_id") or 0) in task_ids]
            pending = len(wave_pending)
            if pending and len(rating) >= prize_places > 0:
                cutoff_points = rating[prize_places - 1]["points"]
                near_users = set()
                for r in wave_pending:
                    uid = r.get("user_id")
                    place_row = next((row for row in rating if row["user_id"] == uid), None)
                    if place_row and abs(place_row["place"] - prize_places) <= 1:
                        near_users.add(uid)
                pending_near_cutoff = len(near_users)
    except Exception:
        pending = 0
        pending_near_cutoff = 0

    return {"wave": wave, "top": top, "pending": pending, "pending_near_cutoff": pending_near_cutoff}


async def close_wave(wave_id: int) -> bool:
    """Переход `active -> closing`, True только у того вызова, который реально перевёл волну —
    повторный тик джобы (или второй одновременный клик) не имеет права «закрыть» волну дважды
    и заново дёрнуть менеджера (T-32-03-03)."""
    return await set_wave_state(wave_id, "closing", expected_state="active")


def wave_number_label(wave: dict) -> str:
    """Единственное место, где номер волны превращается в человеческую строку «Волна N» —
    названия у волны нет (D-10); используют и экраны, и рассылки."""
    return f"Волна {(wave or {}).get('number', '?')}"


# ── Задача 3 (D-21): подсказка соотношения баллов на экране настройки ─────────────────────

async def referral_ratio_hint() -> str | None:
    """Подсказка менеджеру рядом с полем `ambassador_referral_coins` (D-21, риск в
    32-RESEARCH-DOMAIN.md «D-14 + D-21»): во сколько заданий средней категории превращается
    текущая сумма за приглашённого. Fail-soft — любое исключение внутри возвращает `None`,
    подсказка не имеет права уронить экран настроек (T-32-03-05)."""
    try:
        tasks = await list_active_tasks()
        coins_values = sorted(int(t["coins"]) for t in tasks if t.get("coins") is not None)
        if not coins_values:
            return None
        median = statistics.median(coins_values)
        referral_coins = int(await get_setting_typed("ambassador_referral_coins") or 0)
        if referral_coins == 0:
            return "Сейчас 0 — баллы за приглашённого не начисляются"
        if median <= 0:
            return None
        ratio = round(referral_coins / median, 1)
        return (
            f"Сейчас 1 приглашённый ≈ {ratio} заданий средней категории "
            f"(среднее задание — {median:g} баллов)"
        )
    except Exception:
        return None
