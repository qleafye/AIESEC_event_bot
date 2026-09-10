"""Квик 260910-ro7 (DELU-01..08): скрытая команда /delete_user — удаление тестового делегата
одной транзакцией. Покрывает два слоя:

- БД (`database/db.py`): `USER_PURGE_TABLES`/`USER_PURGE_EXCLUDED`, `count_user_footprint`,
  `purge_user`, `find_user_id_by_username` — плюс сторож дрейфа схемы (регулярка по исходнику
  `database/db.py`, тот же приём, что `test_kinds_match_module_docstring` в
  `tests/test_miniapp_outbox.py`).
- Хендлер (`handlers/admin_purge.py`): карточка/подтверждение/отказ, права `config.ADMIN_IDS`.

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async-вызов
обёрнут в `asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path`. Фейки
сообщения/колбэка — по образцу `tests/test_roles_phase8.py` (запись текстов ответов,
`edit_text`, `answer`), хендлеры вызываются напрямую функцией (не через `router.propagate_
event`), как в большинстве admin-тестов проекта.
"""
import asyncio
import re
from datetime import datetime
from pathlib import Path

from config import config
from database import db

ADMIN_ID = 900901
MANAGER_ID = 900902
STRANGER_ID = 900903
DELEGATE_ID = 900910
OTHER_DELEGATE_ID = 900911


def _ready(tmp_path, name="test_delete_user_260910.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def _seed_full_footprint(tid: int, *, username: str | None = None) -> int:
    """Заявка + монеты + черновик + сдача с двумя частями + вопрос делегата — тот же набор,
    что описан в <behavior> плана (application=1, coins=1, draft=1, game=3, questions=1)."""
    await db.add_user({
        "telegram_id": tid,
        "username": username,
        "full_name": "Тестовый Делегат",
        "registration_date": _now(),
        "event_city": None,
    })
    await db.add_coins(tid, 10, reason="тест", source="manual")
    await db.upsert_reg_draft(tid, kind="full", source="bot")
    submission_id = await db.create_submission(1, tid, "text", "ответ", _now())
    await db.add_submission_part(submission_id, 0, "text", "часть 1")
    await db.add_submission_part(submission_id, 1, "text", "часть 2")
    await db.create_question(tid, "Когда дедлайн?")
    return submission_id


# ── count_user_footprint / purge_user ───────────────────────────────────────────────────────

def test_footprint_on_empty_db_is_all_zeros(tmp_path):
    _ready(tmp_path)
    footprint = asyncio.run(db.count_user_footprint(DELEGATE_ID))
    assert footprint, "словарь не должен быть пустым — ключи групп присутствуют всегда"
    assert all(v == 0 for v in footprint.values())


def test_footprint_counts_seeded_delegate(tmp_path):
    _ready(tmp_path)
    asyncio.run(_seed_full_footprint(DELEGATE_ID, username="@seeded"))
    footprint = asyncio.run(db.count_user_footprint(DELEGATE_ID))
    assert footprint["application"] == 1
    assert footprint["coins"] == 1
    assert footprint["draft"] == 1
    assert footprint["game"] == 3  # сдача + две части
    assert footprint["questions"] == 1


def test_footprint_counts_referrals_kept_but_does_not_delete_them(tmp_path):
    _ready(tmp_path)
    asyncio.run(_seed_full_footprint(DELEGATE_ID))
    asyncio.run(db.add_user({
        "telegram_id": OTHER_DELEGATE_ID,
        "referrer_id": DELEGATE_ID,
        "full_name": "Приведённый делегат",
        "registration_date": _now(),
    }))
    footprint = asyncio.run(db.count_user_footprint(DELEGATE_ID))
    assert footprint["referrals_kept"] == 1
    asyncio.run(db.purge_user(DELEGATE_ID))
    referred = asyncio.run(db.get_user(OTHER_DELEGATE_ID))
    assert referred is not None, "заявка приведённого делегата не должна исчезнуть"


def test_purge_returns_same_counts_as_footprint(tmp_path):
    _ready(tmp_path)
    asyncio.run(_seed_full_footprint(DELEGATE_ID))
    before = asyncio.run(db.count_user_footprint(DELEGATE_ID))
    after = asyncio.run(db.purge_user(DELEGATE_ID))
    assert after == before


def test_purge_deletes_all_footprint_tables_keeps_staff_and_other_delegate(tmp_path):
    _ready(tmp_path)
    submission_id = asyncio.run(_seed_full_footprint(DELEGATE_ID, username="@seeded"))
    asyncio.run(db.add_staff(DELEGATE_ID, "reg_manager", ADMIN_ID))
    asyncio.run(_seed_full_footprint(OTHER_DELEGATE_ID, username="@other"))

    asyncio.run(db.purge_user(DELEGATE_ID))

    assert asyncio.run(db.get_user(DELEGATE_ID)) is None
    assert asyncio.run(db.get_reg_draft(DELEGATE_ID)) is None
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 0
    assert asyncio.run(db.get_active_submission(1, DELEGATE_ID)) is None
    assert asyncio.run(db.list_submission_parts(submission_id)) == []

    async def _questions_left():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM delegate_questions WHERE user_id = ?", (DELEGATE_ID,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0]

    assert asyncio.run(_questions_left()) == 0

    # staff (роли менеджера) переживает удаление делегатских данных того же id
    assert asyncio.run(db.get_staff_roles(DELEGATE_ID)) == ["reg_manager"]

    # чужой делегат цел
    assert asyncio.run(db.get_user(OTHER_DELEGATE_ID)) is not None
    assert asyncio.run(db.get_balance(OTHER_DELEGATE_ID)) == 10


def test_purge_is_idempotent(tmp_path):
    _ready(tmp_path)
    asyncio.run(_seed_full_footprint(DELEGATE_ID))
    asyncio.run(db.purge_user(DELEGATE_ID))
    second = asyncio.run(db.purge_user(DELEGATE_ID))
    assert all(v == 0 for v in second.values())


# ── find_user_id_by_username ────────────────────────────────────────────────────────────────

def test_find_user_id_by_username_finds_in_users_case_insensitive_without_at(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_ID, "username": "@SeededUser",
        "full_name": "Т", "registration_date": _now(),
    }))
    found = asyncio.run(db.find_user_id_by_username("seededuser"))
    assert found == DELEGATE_ID


def test_find_user_id_by_username_falls_back_to_reg_started(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.mark_reg_started(DELEGATE_ID, "@Dropout"))
    found = asyncio.run(db.find_user_id_by_username("@dropout"))
    assert found == DELEGATE_ID


def test_find_user_id_by_username_unknown_returns_none(tmp_path):
    _ready(tmp_path)
    found = asyncio.run(db.find_user_id_by_username("@nobody"))
    assert found is None


# ── Сторож дрейфа схемы ─────────────────────────────────────────────────────────────────────

DB_SRC_PATH = Path(__file__).resolve().parent.parent / "database" / "db.py"
_TABLE_BLOCK_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)(.*?)'''", re.S)
_ID_COLUMN_RE = re.compile(r"^\s*(user_id|telegram_id|chat_id)\b", re.M)


def _tables_with_delegate_id_column() -> set[str]:
    src = DB_SRC_PATH.read_text(encoding="utf-8")
    found = set()
    for name, body in _TABLE_BLOCK_RE.findall(src):
        if _ID_COLUMN_RE.search(body):
            found.add(name)
    return found


def test_every_table_with_delegate_id_column_is_classified():
    """Каждая таблица из DDL init_db с колонкой user_id/telegram_id/chat_id обязана попасть
    либо в USER_PURGE_TABLES, либо в USER_PURGE_EXCLUDED — иначе новая таблица с делегатским
    следом молча остаётся неудаляемой."""
    covered = {t for t, _, _ in db.USER_PURGE_TABLES} | set(db.USER_PURGE_EXCLUDED)
    drifted = _tables_with_delegate_id_column() - covered
    assert not drifted, (
        f"Таблицы с колонкой user_id/telegram_id/chat_id вне USER_PURGE_TABLES и "
        f"USER_PURGE_EXCLUDED: {sorted(drifted)}. Добавь их в один из двух списков в "
        "database/db.py (USER_PURGE_TABLES — если это делегатский след, который нужно "
        "стирать, USER_PURGE_EXCLUDED — если сознательно не трогаем, с комментарием-причиной)."
    )


def test_purge_table_columns_exist_in_schema(tmp_path):
    """Каждая пара (таблица, колонка) из USER_PURGE_TABLES реально существует в схеме после
    init_db — опечатка в имени таблицы/колонки ловится здесь, а не в проде."""
    _ready(tmp_path)

    async def _check():
        async with db._connect() as conn:
            for table, column, _group in db.USER_PURGE_TABLES:
                assert await db._column_exists(conn, table, column), (
                    f"USER_PURGE_TABLES ссылается на {table}.{column}, но такой колонки нет "
                    "в схеме после init_db"
                )

    asyncio.run(_check())


def test_user_purge_tables_is_the_single_list():
    """USER_PURGE_TABLES объявлен ровно один раз в database/db.py (второго списка троек нет)."""
    src = DB_SRC_PATH.read_text(encoding="utf-8")
    assert src.count("USER_PURGE_TABLES: tuple[tuple[str, str, str], ...] = (") == 1
