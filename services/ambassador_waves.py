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
import settings_ops
from database.db import (
    NO_DEADLINE_AT,
    announce_wave_atomic,
    create_task,
    create_wave,
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
    waves_overlapping,
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


async def current_wave_for_city_raw(event_city_raw: str | None, *,
                                     now: datetime | None = None) -> dict | None:
    """WR-03 (32-REVIEW.md): та же волна, что `current_wave_for`, но НОРМАЛИЗУЕТ сырой
    `users.event_city` ПЕРЕД поиском — легаси-амбассадор дефолтного города (`event_city IS
    NULL`, таких ~590 на проде) иначе матчит только волны «все города» (`wave_at(ts, None)`
    отбирает лишь `event_city IS NULL`), теряя волну своего дефолтного города, хотя видит её
    задания и стоит в её рейтинге (та же `normalize_city`, что везде в проекте). Когда модуль
    городов выключен, семантика — прежняя (`None` = без городов вовсе, нормализация не
    применяется) — эта функция сама решает, нужна ли она, вызывающей стороне (`services.
    referrals`, которая намеренно не заводит зависимость от `cities`, см. её докстринг) думать
    об этом не нужно."""
    city = cities.normalize_city(event_city_raw) if await cities.cities_module_on() else None
    return await current_wave_for(city, now=now)


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
    # CR-07: «спортивное» место (1-2-2-4), не срез по позиции в списке — при ничьей на границе
    # призовых мест срез рисовал бы менеджеру топ, где один из двух равных по баллам участников
    # не попал в список, хотя после объявления итогов попадут ОБА (см. `announce_results` ниже).
    top = [r for r in rating if r["place"] <= prize_places]

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


async def prize_places_for(wave: dict) -> int:
    """D-18: своё число призовых мест волны, если задано, иначе общая настройка
    `wave_prize_places`; меньше единицы (кривые данные — 0 или отрицательное) приводится к
    единице — волна без единого призового места не имеет смысла."""
    raw = (wave or {}).get("prize_places")
    if not raw:
        raw = await get_setting_typed("wave_prize_places")
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n >= 1 else 1


async def announce_results(wave_id: int) -> dict | None:
    """D-16/D-17: объявление итогов волны — ровно один раз.

    CR-06: рейтинг читается ДО открытия транзакции (`wave_rating` — read-only), а сам переход
    состояния `closing -> announced` и запись снимка ВСЕХ участников волны идут ОДНОЙ
    транзакцией (`announce_wave_atomic`) — тем же арбитром гонки, что раньше был отдельный
    `set_wave_state`. До этой правки переход коммитился первым отдельным вызовом, а снимок —
    вторым: падение между ними (`database is locked`, рестарт при деплое) оставляло волну
    `announced` БЕЗ единой строки снимка и без кнопки восстановления в UI. `rowcount != 1`
    (волна уже объявлена, ещё идёт, или её вовсе нет) — функция ничего не пишет и возвращает
    `None`, двойной тап по кнопке «Объявить итоги» не создаёт второго объявления (T-32-11-02).

    CR-07: призёр — КАЖДЫЙ, чьё «спортивное» место (1-2-2-4, см. `wave_rating`) не превышает
    число призовых мест, а не первые N позиций списка — при равенстве баллов на границе срез по
    позиции отдавал бы приз только одному из разделивших место, хотя оба физически заняли его
    (`is_winner` в снимке отражает именно это правило, а не длину списка).

    В БД (`announce_wave_atomic`) пишутся ВСЕ участники волны — не только призёры (D-17: личное
    место невыигравшего тоже нельзя пересчитать заново задним числом); полные `standings` (место,
    баллы КАЖДОГО участника) возвращаются вызывающей стороне для обратной совместимости
    вызывающего кода, рассылка итогов (план 32-11) читает снимок из БД напрямую, а не через
    этот аргумент — сдача, одобренная уже ПОСЛЕ этой секунды, не имеет права задним числом
    поменять то, что увидят участники в сообщении об итогах (T-32-11-01), хотя в общий зачёт
    она пойдёт (D-17).

    Коинов здесь не начисляется вовсе (D-19) — приз («счастливый билет») только текст в
    шаблоне итогов, бот ничего не выдаёт и не начисляет сам.

    Возвращает `{wave, winners: [...], standings: {user_id: (place, points)}, total}` либо
    `None`, если переход не выигран."""
    wave = await get_wave(wave_id)
    if not wave or wave.get("state") != "closing":
        return None

    rating = await wave_rating(wave_id)
    places = await prize_places_for(wave)
    winners = [r for r in rating if r["place"] <= places]
    winner_ids = {int(r["user_id"]) for r in winners}

    announced_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (int(r["user_id"]), int(r["place"]), int(r["points"]), int(r["user_id"]) in winner_ids)
        for r in rating
    ]
    if not await announce_wave_atomic(wave_id, rows, announced_at):
        return None  # проиграли гонку (или волну откатили между чтением и записью) — молчим

    wave = await get_wave(wave_id)
    standings = {int(r["user_id"]): (int(r["place"]), int(r["points"])) for r in rating}
    return {"wave": wave, "winners": winners, "standings": standings, "total": len(rating)}


async def prize_tie_note(wave_id: int) -> str | None:
    """CR-07: человеческая строка для экрана подтверждения «Объявить итоги», когда на границе
    призовых мест ничья и призёров окажется больше, чем призовых мест (например, `prize_places
    = 3`, а два участника делят 3-е место — призёров будет 4). `None`, если ничьей на границе
    нет — обычный случай, экран не меняется."""
    wave = await get_wave(wave_id)
    if not wave:
        return None
    rating = await wave_rating(wave_id)
    places = await prize_places_for(wave)
    winners_count = len([r for r in rating if r["place"] <= places])
    if winners_count <= places:
        return None
    return f"Из-за равенства баллов призёров {winners_count}, а не {places}."


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


# ── Plan 32-10: правила админки волн — даты, права, копия, редактируемые поля ─────────────

def _fmt_wave_date(raw: str | None) -> str:
    """ISO «%Y-%m-%d %H:%M:%S» -> человеческое «ДД.ММ.ГГГГ»; мусор — как есть (fail-soft, та
    же идиома, что `game_labels.task_deadline_admin`)."""
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(raw or "—")


def wave_editable_fields(wave: dict) -> set[str]:
    """Что можно править в каждом состоянии волны (32-CONTEXT.md, «Claude's Discretion»):
    `draft` — даты, вводный текст, число призовых мест, город, состав заданий (волна ещё не
    разослана, менять нечего опасаться); `active` ДО отправки стартовых сообщений
    (`started_notified_at` пуст) — то же самое; `active` ПОСЛЕ отправки — ТОЛЬКО вводный текст
    и число призовых мест: амбассадоры уже получили список заданий с дедлайнами, менять его
    задним числом нечестно, поэтому даты и состав заперты; `closing` — только число призовых
    мест (даты события уже наступили, двигать их бессмысленно); `announced` — ничего (D-17:
    снимок призёров неизменяем)."""
    state = (wave or {}).get("state")
    full = {"dates", "intro_text", "prize_places", "event_city", "tasks"}
    if state == "draft":
        return full
    if state == "active":
        if not (wave or {}).get("started_notified_at"):
            return full
        return {"intro_text", "prize_places"}
    if state == "closing":
        return {"prize_places"}
    return set()  # announced и любое незнакомое состояние — ничего


async def validate_wave_dates(starts_at: str, ends_at: str, event_city: str | None, *,
                               exclude_id: int | None = None) -> str | None:
    """Готовое человеческое объяснение проблемы с датами волны, или `None`, если всё хорошо —
    ни одного кода/id в тексте (проверяется тестом). `starts_at`/`ends_at` — ISO-сортируемые
    строки «%Y-%m-%d %H:%M:%S» (та же форма, что хранит `database.db`). При принятом в этом
    слое соглашении «начало — 00:00:00 своего дня, конец — 23:59:59 своего дня» перепутанные
    местами даты и «волна длиной ноль дней» дают один и тот же признак `ends_at <= starts_at`
    — единая проверка, единое объяснение."""
    if ends_at <= starts_at:
        return (
            "Дата конца должна быть позже даты начала — проверьте, не перепутаны ли даты "
            "местами."
        )
    conflicts = await waves_overlapping(starts_at, ends_at, event_city, exclude_id=exclude_id)
    if conflicts:
        other = conflicts[0]
        return (
            f"Пересекается с «{wave_number_label(other)}» "
            f"({_fmt_wave_date(other.get('starts_at'))}–{_fmt_wave_date(other.get('ends_at'))}). "
            "Поправьте даты так, чтобы волны не пересекались."
        )
    return None


async def can_edit_wave(admin_id: int, wave: dict) -> bool:
    """Право на город (32-CONTEXT.md key_links: `settings_ops.per_city_visible_codes`) —
    `per_city_visible_codes(admin_id)` содержит город волны, либо волна «для всех городов»
    (`event_city` пуст) и админ видит ВСЕ города (то есть не привязан ровно к одному)."""
    codes = set(await settings_ops.per_city_visible_codes(admin_id))
    city = (wave or {}).get("event_city")
    if city is None:
        return codes == set(cities.city_codes())
    return city in codes


async def editable_city_codes(admin_id: int) -> list[str]:
    """Те же права, что `can_edit_wave`, но для экрана создания — волны ещё нет, спрашивать
    не у чего."""
    return await settings_ops.per_city_visible_codes(admin_id)


async def copy_wave(src_wave_id: int, starts_at: str, ends_at: str, *,
                     created_by: int | None) -> int:
    """D-13 «Скопировать прошлую»: новая волна-черновик с городом/вводным текстом/числом
    призовых мест исходной, плюс копия её АКТИВНЫХ заданий (текст/название/категория/баллы/
    типы подтверждения/город/аудитория те же), дедлайн каждого сдвинут на ту же величину, что
    и сама волна (разница между новым и старым `starts_at`). Задание исходной волны без
    собственного срока (`NO_DEADLINE_AT`) копируется тоже без срока — сдвигать служебную
    метку бессмысленно. Сдвинутый дедлайн, вылезший за конец НОВОЙ волны, подрезается до её
    конца (иначе задание могло бы «пережить» волну, в которую его скопировали). Архивные
    задания не копируются. Возвращает id новой волны."""
    src = await get_wave(src_wave_id)
    if not src:
        raise ValueError("Исходная волна не найдена")

    new_id = await create_wave(
        starts_at, ends_at,
        intro_text=src.get("intro_text"),
        prize_places=src.get("prize_places"),
        event_city=src.get("event_city"),
        created_by=created_by,
    )

    old_start = datetime.strptime(src["starts_at"], "%Y-%m-%d %H:%M:%S")
    new_start = datetime.strptime(starts_at, "%Y-%m-%d %H:%M:%S")
    shift = new_start - old_start
    new_end_dt = datetime.strptime(ends_at, "%Y-%m-%d %H:%M:%S")

    for t in await list_wave_tasks(src_wave_id, active_only=True):
        old_deadline = t.get("deadline_at")
        if old_deadline == NO_DEADLINE_AT:
            new_deadline = NO_DEADLINE_AT
        else:
            try:
                shifted = datetime.strptime(old_deadline, "%Y-%m-%d %H:%M:%S") + shift
            except (TypeError, ValueError):
                shifted = new_end_dt
            if shifted > new_end_dt:
                shifted = new_end_dt
            new_deadline = shifted.strftime("%Y-%m-%d %H:%M:%S")
        await create_task(
            t["text"], t["category"], t["coins"], t["proof_type"], new_deadline, created_by,
            event_city=t.get("event_city"), title=t.get("title"),
            photo_file_id=t.get("photo_file_id"), wave_id=new_id,
            audience=t.get("audience") or "all",
        )
    return new_id
