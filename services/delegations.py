"""Делегации вузов на Москву — ядро: оценка ответа формы, поиск делегата по @нику и
превращение его в одобренного участника без анкеты.

Источник — Яндекс Форма делегаций, подключённая через внешние формы (`services/ext_forms_*`).
Каждый новый ответ формы из настройки `delegation_form_id` проходит три шага:

1. **Вердикт ЦА** (`evaluate`): курс из свободного текста разбирает
   `services.delegations_course`, отсечка и список курсов «не ЦА» берутся из реестра. Исходов
   три: `ok` — целевая аудитория, `no` — нет, `check` — бот не берётся решать, менеджер нажимает
   «ЦА / не ЦА» сам. Ручное решение менеджера переоценка не перетирает (`upsert_eval`).
2. **Поиск человека** (`find_person`): только по нику из формы — сначала `users`, потом
   `reg_started`. Самопоиска «по ФИО» нет: чужое имя стало бы чужим одобрением.
3. **Решение о привязке** (`decide_link`) и **превращение** (`convert_to_delegate`): делегат ЦА
   становится `approved` текущего сезона в городе по умолчанию (Москва), без анкеты и без шага
   оплаты — `approve_user` из `handlers.reg_schema` намеренно не вызывается: он открывает оплату
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
from database.db import get_reg_started_by_username, get_user_by_username
from services.delegations_course import _cutoff_dt, evaluate_ta, parse_course
from services.ext_forms_match import username_from_value
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

SOURCE_TAG = "delegation"
LINK_HOW_USERNAME = "username"
LINK_HOW_MANUAL = "manual"
NOTE_REJECTED_IN_BOT = "rejected_in_bot"

# Угадывание ключевых вопросов формы по подписи — тем же приёмом, что
# `services.ext_forms_match.guess_key_questions`. Подписи про ник/телеграм/ВК пропускаются:
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
# тот же приём, что `services.miniapp_outbox.init_fsm_storage`. Внутри джоб, если `init` не
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
