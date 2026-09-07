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
