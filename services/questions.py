"""Quick 260904-2cj: единственное место, где живёт правило статуса вопроса делегата.

Чистый модуль (форма `services/applications.py`, только этот вовсе не ходит в базу — ни
одного импорта `database.db`, ни одного SQL): статус вопроса выводится из ТРЁХ колонок
строки `delegate_questions` (answered_by/delivered_at/answered_at), которые уже есть в
проекте (T-08-33/D-14) — здесь только чтение, схему не трогаем.

Три состояния (интерфейсный контракт квика, другой логики в проекте быть не должно):
    answered_by IS NULL                          -> "new"      (никто не взял)
    answered_by NOT NULL AND delivered_at IS NULL -> "in_work"  (захват без доставки)
    delivered_at IS NOT NULL                      -> "answered" (доставлен ответ)

`question_status` проверяет СНАЧАЛА `delivered_at`, потом `answered_by` — легаси-строка,
которой каким-то образом проставили ответ без захвата (до T-08-33 захвата не было вовсе),
обязана читаться как «отвечен», а не «в работе».

`database.db._QUESTION_STATUS_SQL` — SQL-зеркало этих же трёх правил (для
`list_questions_page`/`count_questions_by_status`, которые фильтруют/считают на стороне
БД); паритет между зеркалом и этой чистой функцией закреплён тестом
`tests/test_questions_journal_260904.py`.

`format_stamp` — ПЕРЕЕЗД `services/sheet_logs.py::_fmt_dt` (тот модуль держит алиас на эту
функцию под старым именем, чтобы не трогать вызовы и золотые тесты).
"""
from __future__ import annotations

from datetime import datetime

from services.timeutil import utc_naive_to_msk

STATUS_NEW = "new"
STATUS_IN_WORK = "in_work"
STATUS_ANSWERED = "answered"
STATUSES = (STATUS_NEW, STATUS_IN_WORK, STATUS_ANSWERED)

STATUS_LABELS = {
    STATUS_NEW: "🆕 без ответа",
    STATUS_IN_WORK: "✍️ в работе",
    STATUS_ANSWERED: "✅ отвечен",
}

FILTER_LABELS = {
    "all": "Все",
    STATUS_NEW: "Без ответа",
    STATUS_IN_WORK: "В работе",
    STATUS_ANSWERED: "Отвечены",
}

# TODO: порог не настройка — в реестре SETTINGS_SCHEMA ключа нет. Завести
# `question_stuck_minutes` при первой же просьбе менеджера сделать его настраиваемым.
STUCK_AFTER_MINUTES = 30


def question_status(row: dict) -> str:
    """`row` — строка `delegate_questions` (dict с ключами answered_by/delivered_at/…).
    Порядок проверок ФИКСИРОВАН: сначала delivered_at (легаси-строка с доставкой, но без
    захвата — всё равно «отвечен»), потом answered_by."""
    if row.get("delivered_at"):
        return STATUS_ANSWERED
    if row.get("answered_by") is not None:
        return STATUS_IN_WORK
    return STATUS_NEW


def status_label(row: dict) -> str:
    """Человеческая подпись с эмодзи. Статус вне STATUSES не существует — `question_status`
    всегда возвращает одно из трёх значений, поэтому лишнего fallback здесь нет."""
    return STATUS_LABELS[question_status(row)]


def _parse_stamp(raw: str) -> datetime | None:
    """Оба формата, что реально пишутся в БД: `claim_question`/`create_question` пишут
    `datetime.utcnow().isoformat()`, а старые записи (миграция T-08-33) могли нести
    "%Y-%m-%d %H:%M:%S". Неразобранное -> None (fail-soft, вызывающий код решает сам)."""
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            return parser(raw)
        except (TypeError, ValueError):
            continue
    return None


def is_stuck(row: dict, now: datetime | None = None) -> bool:
    """Истинно только для "in_work" И только если `answered_at` старше `STUCK_AFTER_MINUTES`.
    Любая ошибка разбора даты -> False (fail-soft, не исключение) — залипание сигнализирует
    менеджеру о задержке, а не роняет экран на битой строке."""
    if question_status(row) != STATUS_IN_WORK:
        return False
    stamp = _parse_stamp(str(row.get("answered_at") or ""))
    if stamp is None:
        return False
    moment = now if now is not None else datetime.utcnow()
    return (moment - stamp).total_seconds() > STUCK_AFTER_MINUTES * 60


def format_stamp(raw: str | None, *, stored_utc: bool = True) -> str:
    """ПЕРЕЕЗД `services/sheet_logs.py::_fmt_dt` — оба формата времени в проекте разобраны
    одинаково, что для листа «Вопросы», что для экранов бота/приложения. Неразобранное
    отдаётся как есть (fail-soft, форма `polls._fmt_date`); пустое -> ''.

    Quick 260904-kk6 (Q1): `stored_utc=True` (по умолчанию) переводит разобранную метку в
    МСК (`utc_naive_to_msk`) ПЕРЕД `strftime` — до этой правки метка печаталась как есть, и
    менеджер читал время вопроса в UTC вместо московского. Что реально пишется в БД (факт из
    `database/db.py`, а не догадка, обновлено квиком 260906-52m):

        UTC (`datetime.utcnow().isoformat()` / `.strftime(...)`)  -> stored_utc=True (по умолчанию):
            delegate_questions.asked_at        (create_question)
            delegate_questions.answered_at     (claim_question)
            delegate_questions.delivered_at    (set_question_answer)
            reg_answer_history.changed_at      (record_answer_history, квик 260906-52m)

        НЕ UTC (`datetime.now().strftime(...)`)   -> stored_utc=False:
            (вызывающих у этого режима больше нет — режим сохранён для меток, которые ещё
            пишутся локальным временем контейнера и печатаются другой функцией,
            `services/applications.py::format_edited_date`: edited_at, approved_at,
            registration_date)

    Долг «`reg_answer_history.changed_at` пишется локальным временем» закрыт квиком
    260906-52m: `record_answer_history` переведена на `datetime.utcnow()`, все три точки
    показа (`services/sheet_logs.py`, `services/applications.py::_history_entry`,
    `handlers/admin_moderation.py::appr_history`) переключены на сдвиг в МСК. Остаток —
    семья `edited_at`/`approved_at`/`registration_date` — по-прежнему пишется
    `datetime.now()` и на проде отстаёт от московского времени на 3 часа; это отдельный
    долг, см. `.planning/backlog.md`."""
    if not raw:
        return ""
    stamp = _parse_stamp(raw)
    if stamp is None:
        return str(raw)
    if stored_utc:
        stamp = utc_naive_to_msk(stamp)
    return stamp.strftime("%d.%m.%Y %H:%M")
