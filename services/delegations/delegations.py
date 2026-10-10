"""Делегации вузов на Москву — ядро: оценка ответа формы, поиск делегата по @нику и
превращение его в одобренного участника без анкеты.

Источник — Яндекс Форма делегаций, подключённая через внешние формы (`services/ext_forms_*`).
Каждый новый ответ формы из настройки `delegation_form_id` проходит три шага
(одобряет и пишет людям модуль только после «✅ Включить делегации» — `is_armed()`):

1. **Вердикт ЦА** (`evaluate`): курс из свободного текста разбирает
   `services.delegations.delegations_course`, отсечка и список курсов «не ЦА» берутся из реестра. Исходов
   три: `ok` — целевая аудитория, `no` — нет, `check` — бот не берётся решать, менеджер нажимает
   «ЦА / не ЦА» сам. Ручное решение менеджера переоценка не перетирает (`upsert_eval`).
2. **Поиск человека** (`find_person`): только по нику из формы — сначала `users`, потом
   `reg_started`. Самопоиска «по ФИО» нет: чужое имя стало бы чужим одобрением.
3. **Решение о привязке** (`decide_link`) и **превращение** (`convert_to_delegate`): делегат ЦА
   становится `approved` текущего сезона в городе по умолчанию (Москва), без анкеты и без шага
   оплаты — `approve_user` из `handlers.reg.reg_schema` намеренно не вызывается: он открывает оплату
   при включённом модуле и шлёт чужой текст. Делегат получает одно сообщение и обычное меню
   одобренного участника; QR, статистика прихода и рассылки работают по `users.status`,
   как у всех.

Отклонённый ранее в боте (в т.ч. автоотказом по курсу) молча не одобряется: автоматические
пути — синк формы (`on_answer_available`) и поздний вход через /start (`try_delegate_start`) —
переводят его ответ в «❔ проверить» с пометкой «в боте отказ». Превращение происходит только
когда менеджер нажал «✅ ЦА» (`decided_by` заполнен) или привязал вручную (`how="manual"`):
тогда строка сначала возвращается в `pending`, потому что `approve_user_atomic` переворачивает
только `pending`, и журнал решений получает менеджера как автора.

В лог — только id формы, ответа и человека: вуз, ФИО и ник из чужой формы — ПД делегата.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from database import delegations_db as ddb
from database import ext_forms_db as ef
from database.db import (
    add_user, approve_user_atomic, clear_reg_started, delete_reg_draft, get_reg_started_by_id,
    get_reg_started_by_username,
    get_setting, get_user, get_user_by_username, record_reg_event, set_user_status, store_username,
    update_user_answers, username_needle,
)
from services.delegations.delegations_course import _cutoff_dt, evaluate_ta, parse_course
from services.ext_forms.ext_forms_match import username_from_value
from services.reg_stuck_reset import _is_registration_state
from services.reject_journal import AUTO_DECIDED_BY
from services.infra.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

SOURCE_TAG = "delegation"
LINK_HOW_USERNAME = "username"
LINK_HOW_MANUAL = "manual"
LINK_HOW_DUPLICATE = "duplicate"
NOTE_REJECTED_IN_BOT = ddb.NOTE_REJECTED_IN_BOT
NOTE_AMBIGUOUS_NICK = ddb.NOTE_AMBIGUOUS_NICK
# Что пишем в `users.delegation`, когда вуз в ответе пуст: все гейты геймы, меню и фильтр рассылки
# проверяют «delegation не пусто» — пустое значение сделало бы делегата обычным участником.
UNIVERSITY_UNKNOWN = "вуз не указан"
# Автор журнала решений при автоматическом одобрении — тот же сентинел, что у автоотказа
# анкеты (`services.reject_journal.AUTO_DECIDED_BY`): списки заявок уже умеют показывать его
# как «автоматически». Когда одобрение авторизовал менеджер, автором становится он.
DELEGATION_DECIDED_BY = AUTO_DECIDED_BY

_ALLOWED_DELEGATION_COLUMNS = ["delegation", "delegation_answer_id"]
_FMT = "%Y-%m-%d %H:%M:%S"

# Угадывание ключевых вопросов формы по подписи — тем же приёмом, что
# `services.ext_forms.ext_forms_match.guess_key_questions`. Подписи про ник/телеграм/ВК пропускаются:
# «Ник в телеграмме» содержит «ник», а не «имя», но лишняя страховка дешевле ложного ФИО.
_FULLNAME = re.compile(r"фио|имя")
_UNIVERSITY = re.compile(r"универ|вуз")
_COURSE = re.compile(r"курс|бакалавр|магистр")
_EMAIL = re.compile(r"почт|e-?mail")
_SKIP_LABEL = re.compile(r"ник|telegram|телеграм|vk|вконтакте")
_GUESS_ORDER = (("fullname", _FULLNAME), ("university", _UNIVERSITY),
                ("course", _COURSE), ("email", _EMAIL))

_FIELD_SETTING_KEYS = {
    "fullname": "delegation_q_fullname",
    "university": "delegation_q_university",
    "course": "delegation_q_course",
    "email": "delegation_q_email",
}

# Бот и хранилище FSM — задаются один раз при старте (`main.py` -> `init(bot, dp.storage)`),
# тот же приём, что `services.infra.miniapp_outbox.init_fsm_storage`. Внутри джоб, если `init` не
# звали, бот берётся из `services.scheduler.get_bot()`.
_bot = None
_storage = None


def init(bot, storage) -> None:
    global _bot, _storage
    _bot = bot
    _storage = storage


# ---------- настройки модуля ----------

async def delegation_form_id() -> int | None:
    """id подключённой формы делегаций из реестра; не выбрана или мусор — None."""
    try:
        raw = await get_setting_typed("delegation_form_id")
        if raw is None or str(raw).strip() == "":
            return None
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def armed_form_id() -> int | None:
    """id формы, для которой менеджер нажал «✅ Включить делегации»; пусто или мусор — None."""
    try:
        raw = await get_setting_typed("delegation_armed_form_id")
        if raw is None or str(raw).strip() == "":
            return None
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def is_armed() -> bool:
    """Модуль включён: форма выбрана и включение нажато именно для неё (смена формы выключает)."""
    dfid = await delegation_form_id()
    return dfid is not None and await armed_form_id() == dfid


def cutoff_dt(value) -> datetime | None:
    """Отсечка ЦА: datetime (сохранённое значение), date, строка «ДД.ММ.ГГГГ» (дефолт ключа)
    или None; непонятное — None (правило действует для всех)."""
    return _cutoff_dt(value)


async def field_keys() -> dict:
    """{fullname, university, course, email} -> qkey вопроса формы из настроек `delegation_q_*`
    (None, если менеджер ещё не подтвердил вопрос)."""
    out: dict = {}
    for name, key in _FIELD_SETTING_KEYS.items():
        raw = await get_setting_typed(key)
        out[name] = str(raw).strip() if raw not in (None, "") else None
    return out


# ---------- вопросы и поля ----------

def guess_delegation_questions(questions: list[tuple[str, str]]) -> dict[str, str | None]:
    """По подписям вопросов предлагает qkey для ФИО, вуза, курса и почты. Первый подходящий
    побеждает; один вопрос — не больше чем в одной роли; подписи про ник пропускаются."""
    guess: dict[str, str | None] = {name: None for name, _ in _GUESS_ORDER}
    for qkey, label in questions:
        low = (label or "").lower()
        if not low or _SKIP_LABEL.search(low):
            continue
        for name, rx in _GUESS_ORDER:
            if guess[name] is None and rx.search(low):
                guess[name] = qkey
                break
    return guess


def _value_of(payload: list[dict], qkey: str | None) -> str | None:
    if not qkey:
        return None
    for it in payload or ():
        if it.get("q") == qkey:
            value = it.get("value")
            if value is None:
                return None
            text = str(value).strip()
            return text or None
    return None


def extract_fields(form: dict, payload: list[dict], keys: dict) -> dict:
    """Поля делегата из ответа: ФИО, вуз, курс (как написан), почта и ключ поиска по нику
    (`username_from_value` понимает «@name», «t.me/name» и голый ник)."""
    return {
        "full_name": _value_of(payload, keys.get("fullname")),
        "university": _value_of(payload, keys.get("university")),
        "course_raw": _value_of(payload, keys.get("course")),
        "email": _value_of(payload, keys.get("email")),
        "username_needle": username_from_value(_value_of(payload, form.get("key_username_q"))),
    }


# ---------- поиск человека ----------

async def find_person(needle: str | None) -> tuple[int | None, str | None]:
    """(telegram_id, "users" | "reg_started" | None) по нику: сначала поданные анкеты, потом те,
    кто только нажал /start. Пустой ник — (None, None) без запроса."""
    if not needle:
        return None, None
    if len(await ddb.people_by_username(needle)) > 1:
        # Ник числится за несколькими людьми (прежний владелец ещё лежит в базе): кого
        # одобрять, бот не знает — решает менеджер.
        return None, "ambiguous"
    user = await get_user_by_username(needle)
    if user:
        return user["telegram_id"], "users"
    started = await get_reg_started_by_username(needle)
    if started:
        return started["telegram_id"], "reg_started"
    return None, None


# ---------- вердикт ЦА ----------

async def evaluate(answer_row: dict, form: dict, keys: dict) -> tuple[dict, dict, str]:
    """Оценить ответ формы и записать оценку. Возвращает (строка `delegation_answers`,
    перечитанная после записи, поля ответа + `course_canonical`, действующий ta_status).
    Перечитывание нужно, чтобы ручное решение менеджера (`decided_by`) победило парсер."""
    fields = extract_fields(form, answer_row.get("payload") or [], keys)
    parsed = parse_course(fields["course_raw"])
    fields["course_canonical"] = parsed.canonical
    cutoff = cutoff_dt(await get_setting_typed("delegation_ta_cutoff"))
    not_ta = await get_setting_typed("delegation_not_ta_courses") or []
    ta = evaluate_ta(parsed, answer_row.get("answered_at"), cutoff, not_ta)
    row_id = await ddb.upsert_eval(
        int(answer_row["form_id"]), str(answer_row["answer_id"]), ta_status=ta,
        university=fields["university"], course_raw=fields["course_raw"],
        course_canonical=parsed.canonical, username_needle=fields["username_needle"],
        answered_at=answer_row.get("answered_at"),
    )
    row = await ddb.get_by_id(row_id)
    return row, fields, row["ta_status"]


# ---------- решение о привязке ----------

def decide_link(row: dict, user: dict | None, tid: int, *, how: str) -> str:
    """Чистое решение, общее для синка, /start и экранов менеджера:

    - "already"        — ответ уже привязан к этому же человеку;
    - "conflict"       — ответ привязан к другому человеку, ничего не трогаем;
    - "check_rejected" — человек отклонён в боте, а решения менеджера нет и привязка не ручная:
                         автоматически не одобряем, ответ уходит в «❔ проверить»;
    - "convert"        — превращать: нет анкеты, pending, approved, либо отклонённый с
                         авторизацией (ручная привязка или «✅ ЦА» менеджера).
    """
    linked = row.get("linked_telegram_id")
    if linked is not None:
        return "already" if int(linked) == int(tid) else "conflict"
    if (user and user.get("status") == "rejected" and how != LINK_HOW_MANUAL
            and row.get("decided_by") is None):
        return "check_rejected"
    return "convert"


# ---------- колонка «В боте» листа UR REGS ----------

async def _mark_answer_update(form_id: int, answer_id: str) -> bool:
    """Поставить строку ответа в очередь перезаписи листа (`sheet_state` 'synced' -> 'update'),
    чтобы колонка «В боте» показала новое состояние. Строка, которую лист ещё не видел
    ('append'), не трогается — первая запись и так принесёт актуальную отметку."""
    answer = await ef.get_answer(int(form_id), str(answer_id))
    if answer and answer.get("sheet_state") == "synced":
        await ef.mark_sheet_state([answer["id"]], "update")
        return True
    return False


async def _mark_update_fail_soft(form_id: int, answer_id: str) -> None:
    try:
        await _mark_answer_update(form_id, answer_id)
    except Exception:
        logger.exception("delegations: отметка листа не поставлена (form=%s, answer=%s)",
                         form_id, answer_id)


# ---------- превращение в делегата ----------

def _resolve_bot(bot=None):
    if bot is not None:
        return bot
    if _bot is not None:
        return _bot
    from services.scheduler import get_bot
    return get_bot()


async def _reset_reg_fsm(bot, tid: int) -> None:
    """Снять незаконченную анкету в чате — только регистрационные состояния (оплата и прочие
    группы FSM не трогаются). Без `init` хранилища — тихий пропуск."""
    if _storage is None:
        return
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    ctx = FSMContext(storage=_storage, key=StorageKey(bot_id=bot.id, chat_id=tid, user_id=tid))
    current = await ctx.get_state()
    if _is_registration_state(current):
        await ctx.clear()


async def _send_welcome(bot, tid: int, university: str | None, *, existing: bool) -> None:
    """Одно сообщение делегату с обычным меню одобренного. Сбой доставки — в учёт решения, чтобы
    «Сверить с БД» показал одобрение без письма."""
    import html as html_module

    from keyboards.builders import get_main_menu_kb
    from services.application_effects import _record_delivery_fail_soft
    from services.i18n import context as i18n_context
    from services.i18n import tr
    from services.infra.telegram_send import send_with_retry

    key = "delegation_welcome_existing_text" if existing else "delegation_welcome_text"
    template = await get_setting_typed(key) or ""
    lang, tr_map = "ru", {}
    try:
        lang, tr_map = await i18n_context(tid)
    except Exception:
        logger.exception("delegations: язык делегата не определён (tid=%s)", tid)
    from services.text_fill import event_name, fill_event

    text = tr(template, lang, tr_map).replace("{university}", html_module.escape(university or ""))
    text = fill_event(text, await event_name(), lang, escape=True)
    kb = await get_main_menu_kb(tid)
    err = await send_with_retry(
        lambda: bot.send_message(tid, text, reply_markup=kb, parse_mode="HTML")
    )
    if err is not None:
        logger.error("delegations: сообщение делегату не доставлено (tid=%s): %s",
                     tid, type(err).__name__)
        await _record_delivery_fail_soft(tid, "approved", "failed", err)
    else:
        await _record_delivery_fail_soft(tid, "approved", "delivered")


async def convert_to_delegate(
    tid: int, row: dict, fields: dict, *, how: str, by: int | None, bot=None,
    tg_username: str | None = None,
) -> dict:
    """Превратить человека `tid` в одобренного делегата по ответу `row` (строка
    `delegation_answers`) с полями `fields` (см. `extract_fields` + `course_canonical`).

    Идемпотентно: уже привязан к этому ответу — {"already": True}. Новая строка `users`
    создаётся только у того, кто анкету не подавал; существующая анкета получает лишь поля
    делегации (ФИО/почта/вуз делегата не перетираются). Отклонённый превращается только с
    авторизацией (ручная привязка или решение менеджера) — иначе {"refused": ...}.
    Шага оплаты нет: `approve_user` не вызывается, напоминания не ставятся. В лист города
    строка не пишется; у бывшего pending/rejected обновляется статус существующей строки.
    """
    import domain.regform.engine as reg_engine
    from domain.cities import default_city_code

    answer_id = str(row["answer_id"])
    user = await get_user(tid)
    if user and str(user.get("delegation_answer_id") or "") == answer_id:
        return {"already": True}
    bound = str((user or {}).get("delegation_answer_id") or "")
    if bound and bound != answer_id:
        # Второй ответ того же человека: делегат уже в боте по первому. Привязываем ответ тихо —
        # ни второго письма, ни перезаписи вуза, ни второго зачёта в сводках.
        if not await ddb.link(row["id"], tid, LINK_HOW_DUPLICATE):
            return {"conflict": True}
        await _mark_update_fail_soft(row["form_id"], answer_id)
        return {"duplicate": True}
    prev_status = user.get("status") if user else None
    authorised = how == LINK_HOW_MANUAL or row.get("decided_by") is not None
    if prev_status == "rejected" and not authorised:
        # Защита: decide_link обязан был отправить такого в «проверить».
        return {"refused": "rejected_in_bot"}

    # Сначала заявляем ответ за этим человеком: два параллельных пути (синк, /start, ручная
    # привязка) не должны оба одобрить и оба написать — проигравший уходит с конфликтом.
    if not await ddb.link(row["id"], tid, how):
        logger.warning("delegations: ответ %s уже привязан к другому человеку (tid=%s)",
                       row["id"], tid)
        return {"conflict": True}

    season = (await get_setting("event_season") or "").strip() or None
    city = default_city_code()
    university = fields.get("university")
    try:
        if user is None:
            # Настоящий ник человека — из его же /start (или переданный вызывающим), а не из
            # текста чужой формы: форму заполнял кто угодно.
            started = await get_reg_started_by_id(tid)
            real = (started or {}).get("username") or tg_username
            username = (store_username("@" + username_needle(real))
                        if username_needle(real) else "-")
            data = reg_engine.with_defaults({
                "full_name": fields.get("full_name") or "-",
                "email": fields.get("email") or "-",
                "university": university or "-",
                "course": fields.get("course_canonical"),
                "source": SOURCE_TAG,
                "event_city": city,
                "season": season,
                "participant_type": "full",
            })
            data["telegram_id"] = tid
            data["username"] = username
            data["registration_date"] = msk_now().strftime(_FMT)
            await add_user(data)
            await update_user_answers(tid, {"source_from_tag": 1},
                                      allowed_columns=["source_from_tag"])
            await set_user_status(tid, "pending")
            current = "pending"
        elif prev_status == "rejected":
            # approve_user_atomic переворачивает только pending — отклонённого иначе не одобрить.
            await set_user_status(tid, "pending")
            current = "pending"
        else:
            current = prev_status

        flipped = False
        if current != "approved":
            flipped = await approve_user_atomic(tid)

        await update_user_answers(
            tid, {"delegation": (university or "").strip() or UNIVERSITY_UNKNOWN,
                  "delegation_answer_id": answer_id},
            allowed_columns=_ALLOWED_DELEGATION_COLUMNS,
        )
        if user is not None:
            # Существующая анкета едет на форум Москвы текущего сезона: иначе возвращенец
            # прошлого сезона остаётся «прошлым» (нет игры, монет, QR), а делегат из другого
            # города — в чужом городе. Старый сезон уходит в prev_season, как при одобрении
            # у стойки.
            patch: dict = {"event_city": city}
            if season:
                patch["season"] = season
                old_season = (user.get("season") or "").strip()
                if old_season and old_season != season:
                    patch["prev_season"] = old_season
            await update_user_answers(tid, patch,
                                      allowed_columns=["event_city", "season", "prev_season"])
    except Exception:
        # Не оставляем ответ «занятым» за человеком, которого так и не одобрили.
        try:
            await ddb.unlink(row["id"], tid)
        except Exception:
            logger.exception("delegations: привязка не снята после сбоя (tid=%s)", tid)
        raise
    # Колонка «В боте» листа: единственная точка для всех путей (синк, /start, вручную).
    await _mark_update_fail_soft(row["form_id"], answer_id)

    resolved_bot = None
    try:
        resolved_bot = _resolve_bot(bot)
    except Exception:
        logger.exception("delegations: бот не инициализирован (tid=%s)", tid)

    for step, coro in (
        ("черновик анкеты", delete_reg_draft(tid)),
        ("отметка старта", clear_reg_started(tid)),
    ):
        try:
            await coro
        except Exception:
            logger.exception("delegations: %s не снят(а) (tid=%s)", step, tid)
    if resolved_bot is not None:
        try:
            await _reset_reg_fsm(resolved_bot, tid)
        except Exception:
            logger.exception("delegations: FSM анкеты не сброшен (tid=%s)", tid)

    if flipped:
        from services.applications import record_decision
        try:
            await record_decision(
                tid, "approved", None, by if by is not None else DELEGATION_DECIDED_BY,
                msk_now(), effects_already_sent=True,
            )
        except Exception:
            logger.exception("delegations: журнал решений не записан (tid=%s)", tid)
        try:
            await record_reg_event(tid, "form_completed", event_city=city, season=season,
                                   source_tag=SOURCE_TAG)
        except Exception:
            logger.exception("delegations: событие воронки не записано (tid=%s)", tid)
        if prev_status in ("pending", "rejected"):
            try:
                from domain.regform.labels import STATUS_LABELS
                from services.sheets import update_status_in_sheet
                await update_status_in_sheet(tid, STATUS_LABELS["approved"])
            except Exception:
                logger.exception("delegations: статус в листе не обновлён (tid=%s)", tid)

    if resolved_bot is not None:
        try:
            await _send_welcome(resolved_bot, tid, university, existing=(prev_status == "approved"))
        except Exception:
            logger.exception("delegations: сообщение делегату не отправлено (tid=%s)", tid)

    return {"converted": True, "flipped": flipped, "prev_status": prev_status}


# ---------- точки входа ----------

async def on_answer_available(form_id: int, answer_id: str, *, reason: str = "ingest") -> dict:
    """Хук «ответ формы доступен» (приём, поздняя привязка, sweep, решение менеджера): оценить,
    найти человека, привязать/превратить или отправить в «проверить». Для чужой формы — no-op.
    Возвращает только коды и id."""
    dfid = await delegation_form_id()
    if dfid is None or int(form_id) != dfid:
        return {"skipped": "not_delegation_form"}
    form = await ef.get_form(int(form_id))
    answer = await ef.get_answer(int(form_id), str(answer_id))
    if not form or not answer:
        return {"skipped": "no_answer"}

    before = await ddb.get_by_answer(int(form_id), str(answer_id))
    row, fields, ta = await evaluate(answer, form, await field_keys())
    result: dict = {"ta": ta, "row_id": row["id"], "reason": reason}
    changed = before is None or before.get("ta_status") != ta
    converted = False
    armed = await is_armed()

    if ta == "ok" and row.get("linked_telegram_id") is None and not armed:
        # Делегации не включены: ответ оценён, но человека не ищем, не одобряем, не пишем.
        result["waiting"] = "not_armed"
    elif ta == "ok" and row.get("linked_telegram_id") is None:
        # Только по нику из формы, свежим поиском: `matched_telegram_id` мог встать по
        # телефону (кто владеет номером, тот и «делегат») или устареть после смены ника.
        tid, _where = await find_person(fields["username_needle"])
        if _where == "ambiguous":
            result["waiting"] = "ambiguous_nick"
            if row.get("decided_by") is None:
                await ddb.set_decision(row["id"], "check", None, note=NOTE_AMBIGUOUS_NICK)
                result["ta"] = "check"
                changed = True
        elif tid is None:
            result["waiting"] = "no_person"
        else:
            user = await get_user(tid)
            verdict = decide_link(row, user, tid, how=LINK_HOW_USERNAME)
            result["verdict"] = verdict
            if verdict == "check_rejected":
                await ddb.set_decision(row["id"], "check", None, note=NOTE_REJECTED_IN_BOT)
                result["ta"] = "check"
                changed = True
            elif verdict == "convert":
                conv = await convert_to_delegate(
                    tid, row, fields, how=LINK_HOW_USERNAME, by=row.get("decided_by"),
                )
                if conv.get("conflict"):
                    result["verdict"] = "conflict"
                else:
                    result["converted"] = conv
                    converted = True
            elif verdict == "conflict":
                logger.warning("delegations: ответ %s формы %s привязан к другому (tid=%s)",
                               answer_id, form_id, tid)
    elif ta == "ok" and row.get("linked_telegram_id") is not None:
        result["verdict"] = "already"

    if changed and not converted:
        await _mark_update_fail_soft(int(form_id), str(answer_id))
    return result


async def try_delegate_start(message, state, bot) -> bool:
    """Поздний вход из `cmd_start`: ответ делегата уже пришёл, человек только что нажал /start.
    Один SELECT по нику; промах — False без побочных эффектов. Решение — через `decide_link`,
    как у синка: отклонённого в боте не превращаем молча и не отбрасываем без следа."""
    if not await is_armed():
        return False
    from_user = getattr(message, "from_user", None)
    needle = username_needle(getattr(from_user, "username", None))
    if needle is None:
        return False
    rows = await ddb.find_pending_by_username(needle)
    if not rows:
        return False
    row = rows[0]
    if int(row["form_id"]) != await delegation_form_id():
        return False
    tid = from_user.id
    user = await get_user(tid)
    verdict = decide_link(row, user, tid, how=LINK_HOW_USERNAME)
    if verdict == "check_rejected":
        await ddb.set_decision(row["id"], "check", None, note=NOTE_REJECTED_IN_BOT)
        await _mark_update_fail_soft(row["form_id"], row["answer_id"])
        return False
    if verdict != "convert":
        logger.info("delegations: /start — ответ %s не привязан (%s, tid=%s)",
                    row["id"], verdict, tid)
        return False
    form = await ef.get_form(int(row["form_id"]))
    answer = await ef.get_answer(int(row["form_id"]), str(row["answer_id"]))
    if not form or not answer:
        return False
    fields = extract_fields(form, answer.get("payload") or [], await field_keys())
    fields["course_canonical"] = row.get("course_canonical")
    res = await convert_to_delegate(tid, row, fields, how=LINK_HOW_USERNAME,
                                    by=row.get("decided_by"), bot=bot,
                                    tg_username=getattr(from_user, "username", None))
    return bool(res.get("converted"))


async def sweep_pending(limit: int = 200, *, reevaluate: bool = False) -> dict:
    """Страховка хука приёма (ретрай очереди второй раз хук не зовёт): оценить ответы формы
    делегаций без оценки и ещё раз поискать людей для ЦА-ответов без привязки. `reevaluate` —
    прогнать все ответы формы (ручные решения переживают переоценку). Идемпотентно."""
    dfid = await delegation_form_id()
    if dfid is None:
        return {"skipped": "no_form", "evaluated": 0, "linked": 0}
    evaluated = linked = rechecked = 0
    for item in await ddb.list_unevaluated(dfid, limit):
        res = await on_answer_available(dfid, item["answer_id"], reason="sweep")
        evaluated += 1
        if res.get("converted"):
            linked += 1
    if await is_armed():
        for r in await ddb.list_by_status(dfid, "ok", linked=False, offset=0, limit=limit):
            res = await on_answer_available(dfid, r["answer_id"], reason="sweep")
            if res.get("converted"):
                linked += 1
    if reevaluate:
        for aid in sorted(await ef.known_answer_ids(dfid)):
            res = await on_answer_available(dfid, aid, reason="reevaluate")
            rechecked += 1
            if res.get("converted"):
                linked += 1
    return {"evaluated": evaluated, "linked": linked, "rechecked": rechecked}


async def preview_reevaluate() -> int:
    """Сухой прогон `sweep_pending(reevaluate=True)`: сколько человек станут делегатами —
    будут одобрены и получат сообщение, — если пересчитать все ответы формы по действующим
    настройкам. Ничего не пишет и никому не отправляет."""
    dfid = await delegation_form_id()
    if dfid is None:
        return 0
    form = await ef.get_form(dfid)
    if not form:
        return 0
    keys = await field_keys()
    cutoff = cutoff_dt(await get_setting_typed("delegation_ta_cutoff"))
    not_ta = await get_setting_typed("delegation_not_ta_courses") or []
    would = 0
    for aid in sorted(await ef.known_answer_ids(dfid)):
        answer = await ef.get_answer(dfid, aid)
        if not answer:
            continue
        row = await ddb.get_by_answer(dfid, aid)
        if row is not None and row.get("linked_telegram_id") is not None:
            continue
        fields = extract_fields(form, answer.get("payload") or [], keys)
        if row is not None and row.get("decided_by") is not None:
            ta = row["ta_status"]  # решение менеджера переоценка не трогает
        else:
            ta = evaluate_ta(parse_course(fields["course_raw"]), answer.get("answered_at"),
                             cutoff, not_ta)
        if ta != "ok":
            continue
        tid, _where = await find_person(fields["username_needle"])
        if tid is None:
            continue
        user = await get_user(tid)
        if user and user.get("delegation_answer_id"):
            continue
        probe = row or {"linked_telegram_id": None, "decided_by": None}
        if decide_link(probe, user, tid, how=LINK_HOW_USERNAME) == "convert":
            would += 1
    return would


async def on_first_entry(bot, user_id: int, city, day, **kwargs) -> None:
    """Слушатель первой отметки входа (`services.checkin.register_first_entry_listener`):
    у делегата из формы строка UR REGS уходит на перезапись — колонка «В боте» покажет «пришёл»."""
    user = await get_user(user_id)
    answer_id = (user or {}).get("delegation_answer_id")
    if not answer_id:
        return
    row = await ddb.get_by_telegram_id(user_id)
    form_id = row["form_id"] if row else await delegation_form_id()
    if form_id is None:
        return
    await _mark_update_fail_soft(int(form_id), str(answer_id))
