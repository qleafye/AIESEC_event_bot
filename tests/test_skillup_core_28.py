"""Phase 28 (28-01, СкиллАп 5 P1): сторожа базового слоя анкеты СкиллАпа — тринадцать новых
колонок `users`, INSERT `add_user` и (задачи 2/3) восемь новых шагов REG_FLOW, настраиваемые
списки вариантов, множество «учусь», условность новых шагов и неизменная ширина листа YL/
РилТолка. pytest-asyncio недоступен в этом окружении — async через asyncio.run(), фикстура
временной БД — тот же приём, что `tests/test_reg_drafts.py::_ready(tmp_path)`.

Задача 1 (Pitfall 1, RESEARCH): `test_every_answer_column_survives_add_user` читает список
колонок из `reg_engine.answer_columns()` ДИНАМИЧЕСКИ — когда задача 2 добавит восемь новых
шагов в REG_FLOW, этот же тест начнёт покрывать и их без единой правки здесь.
"""
import asyncio
import sqlite3

from config import config
from database import db
import reg_engine


def _ready(tmp_path, name="test_skillup_core_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _table_columns(tmp_path, name="test_skillup_core_28.db"):
    conn = sqlite3.connect(str(tmp_path / name))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    return cols


NEW_USERS_COLUMNS = (
    "stack", "experience", "readiness", "resume_link",
    "mini_projects", "mini_portfolio", "mini_direction", "case_optin",
    "resume_type", "link_verified", "is_ambassador", "score", "is_it_3plus",
)

# Колонки анкеты, ЗАВЕДОМО исключённые из round-trip сторожа `add_user` — не потому что что-то
# сломано, а потому что у них нет прямого пути через add_user (документируем ПОЧЕМУ, Pitfall 1):
_NOT_IN_ADD_USER = {
    # FSM-only поле анкеты резюме — колонки в `users` нет вовсе (см. reg_engine._EXTRA_ANSWER_COLUMNS
    # и комментарий Quick 260904-aup), это не регрессия, а изначальный дизайн.
    "resume_file_name",
}


# ── Задача 1: тринадцать колонок users + идемпотентность миграции ──────────────────────────

def test_new_columns_exist_after_init(tmp_path):
    _ready(tmp_path)
    cols = _table_columns(tmp_path)
    for col in NEW_USERS_COLUMNS:
        assert col in cols, f"missing column {col}"


def test_init_db_is_idempotent_on_existing_base(tmp_path):
    _ready(tmp_path)
    uid = 900280001

    async def seed():
        await db.add_user({
            "telegram_id": uid, "full_name": "Иванова Мария",
            "registration_date": "2026-09-07", "stack": "Python",
        })

    asyncio.run(seed())

    # Второй init_db() на той же самой (уже заполненной) базе не должен падать и не должен
    # трогать ранее вставленную строку.
    asyncio.run(db.init_db())

    row = asyncio.run(db.get_user(uid))
    assert row is not None
    assert row["full_name"] == "Иванова Мария"
    assert row["stack"] == "Python"

    cols = _table_columns(tmp_path)
    for col in NEW_USERS_COLUMNS:
        assert col in cols, f"missing column {col} after second init_db()"


def test_every_answer_column_survives_add_user(tmp_path):
    """Сторож Pitfall 1: каждая колонка из `reg_engine.answer_columns()` (за вычетом
    документированного `_NOT_IN_ADD_USER`) обязана пережить round-trip через `add_user` ->
    `get_user`. Тест обязан падать, если завтра кто-то добавит шаг в REG_FLOW и забудет
    добавить его колонку в INSERT (или уберёт колонку оттуда «между делом»)."""
    _ready(tmp_path)
    uid = 900280002

    columns = [c for c in reg_engine.answer_columns() if c not in _NOT_IN_ADD_USER]
    assert columns, "answer_columns() вернул пусто — сторож бессмысленен"

    data = {
        "telegram_id": uid,
        "registration_date": "2026-09-07",
    }
    for col in columns:
        data[col] = f"ANSWER::{col}"

    async def go():
        await db.add_user(data)
        return await db.get_user(uid)

    row = asyncio.run(go())
    assert row is not None
    for col in columns:
        assert row.get(col) == f"ANSWER::{col}", (
            f"колонка {col!r} не пережила round-trip через add_user — "
            f"проверь список колонок/? /VALUES/ON CONFLICT SET в database/db.py::add_user"
        )


# ── Задача 3: условность шагов, множество «учусь», ширина листа ────────────────────────────

# Заголовки листа, которые появляются ТОЛЬКО когда включён соответствующий reg_q_* (все
# восемь — default OFF, D-06). На чистой базе (без единого override) ни один из них не
# должен попасть в active_sheet_headers() — иначе ширина листа живых событий (YL/РилТолк)
# изменилась бы молча (Pitfall 5, RESEARCH).
_NEW_SHEET_HEADERS = {
    "Стек", "Опыт работы", "Готовность",
    "Резюме (ссылка на профиль)", "Проекты", "Портфолио",
    "Направление развития", "Кейс-чемпионат",
}


def test_new_steps_hidden_by_default(tmp_path):
    _ready(tmp_path)

    enabled = asyncio.run(reg_engine.enabled_steps({}))
    new_steps = {
        "stack", "experience", "readiness", "resume_link",
        "mini_projects", "mini_portfolio", "mini_direction", "case_optin",
    }
    assert not (set(enabled) & new_steps), (
        f"новые шаги СкиллАпа включены по умолчанию: {set(enabled) & new_steps}"
    )


def test_sheet_width_unchanged_when_new_questions_off(tmp_path):
    _ready(tmp_path)
    from handlers import reg_schema

    headers = asyncio.run(reg_schema.active_sheet_headers())
    leaked = set(headers) & _NEW_SHEET_HEADERS
    assert not leaked, f"новые колонки СкиллАпа просочились в дефолтную ширину листа: {leaked}"


def test_edu_studying_set_drives_course_question(tmp_path):
    """При настроенном `edu_studying_statuses` шаг `course` включается по точному вхождению
    статуса в список (а не по `startswith("Да")`); при пустом множестве — прежнее правило
    байт-в-байт (D-06)."""
    _ready(tmp_path)

    async def with_configured_set():
        await db.set_setting("edu_studying_statuses", "Магистратура")
        return await reg_engine.enabled_steps(
            {"education_status": "Магистратура", "participant_type": "full"}
        )

    async def with_empty_set():
        await db.set_setting("edu_studying_statuses", "")
        return await reg_engine.enabled_steps(
            {"education_status": "Магистратура", "participant_type": "full"}
        )

    enabled_configured = asyncio.run(with_configured_set())
    enabled_default_rule = asyncio.run(with_empty_set())

    assert "course" in enabled_configured, (
        "edu_studying_statuses=['Магистратура'] должен включать course для этого статуса"
    )
    assert "course" not in enabled_default_rule, (
        "пустое множество должно откатываться на startswith('Да'), 'Магистратура' ему не "
        "удовлетворяет"
    )


def test_is_studying_default_rule_unchanged():
    """Сторож byte-for-byte (D-06): без настроенного множества — прежнее правило
    `startswith('Да')`."""
    assert reg_engine.is_studying("Да, в ВУЗе или колледже") is True
    assert reg_engine.is_studying("Нет, завершил(а) обучение") is False
    assert reg_engine.is_studying("Магистратура") is False
    assert reg_engine.is_studying("Магистратура", ["Магистратура"]) is True
    assert reg_engine.is_studying("Да, в ВУЗе или колледже", ["Магистратура"]) is False
