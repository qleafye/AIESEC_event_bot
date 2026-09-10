"""Квик 260911-0zu: поиск делегата по юзернейму терпим к «@» в любом направлении.

Прод-баг: `/delete_user @um0l1shenn1` и `/delete_user um0l1shenn1` оба отвечают «Не нашёл
пользователя», хотя делегат зарегистрирован. Причина — оба искателя (`get_user_by_username`,
`find_user_id_by_username`) нормализовали только ВВОД (дописывали «@»), а сравнивали с сырым
хранимым значением. Три пути записи расходятся по формату: анкета в чате пишет «@user», анкета
Mini App и брошенная анкета (`reg_started`) — без «@». На проде это оставляло вторую ветку
`find_user_id_by_username` (поиск среди бросивших анкету, 947 строк `reg_started`, ВСЕ без
собаки) мёртвым кодом.

Фикс — двусторонняя нормализация в SQL (`ltrim(username, '@') = ? COLLATE NOCASE`) плюс канон
записи «с @» на обоих писателях (`add_user`, `mark_reg_started`). Старые строки без «@» НЕ
мигрируются — их находит именно двусторонний поиск, поэтому часть тестов ниже сеет «легаси»-
строки в обход канона записи напрямую через SQL (`_force_raw_username`), имитируя прод-данные,
заведённые до этого фикса.

pytest-asyncio недоступен в этом окружении (см. `tests/test_db_phase5.py`) — каждый async-вызов
обёрнут в `asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path`, как в
`tests/test_delete_user_260910.py`.
"""
import ast
import asyncio
import re
from datetime import datetime
from pathlib import Path

from config import config
from database import db

USERS_A = 910101
USERS_B = 910102
REG_A = 910201
REG_B = 910202
UNKNOWN_ID = 910301


def _ready(tmp_path, name="test_username_lookup_at_sign_260911.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def _add_user(tid: int, username, full_name: str = "Т"):
    await db.add_user({
        "telegram_id": tid,
        "username": username,
        "full_name": full_name,
        "registration_date": _now(),
    })


async def _force_raw_username(table: str, tid: int, raw):
    """Пишет username В ОБХОД канона записи — симулирует прод-строку, заведённую ДО этого
    фикса (Mini App писала без «@», брошенная анкета — тоже). Без этого хелпера засеять
    «легаси»-формат нельзя: оба писателя теперь всегда приводят к «@»."""
    async with db._connect() as conn:
        await conn.execute(
            f"UPDATE {table} SET username = ? WHERE telegram_id = ?", (raw, tid)
        )
        await conn.commit()


async def _raw_username(table: str, tid: int):
    async with db._connect() as conn:
        async with conn.execute(
            f"SELECT username FROM {table} WHERE telegram_id = ?", (tid,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


# ── Матрица поиска: get_user_by_username (таблица users) ───────────────────────────────────

def test_get_user_by_username_finds_at_stored_record_any_input_format(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "@SeededUser"))
    for needle in ("seededuser", "@seededuser", "SEEDEDUSER", "@SeededUser"):
        found = asyncio.run(db.get_user_by_username(needle))
        assert found is not None, f"не нашёл по вводу {needle!r}"
        assert found["telegram_id"] == USERS_A


def test_get_user_by_username_finds_legacy_record_stored_without_at(tmp_path):
    """Симулирует прод-строку из Mini App (пишет username без «@», см. CONTEXT квика)."""
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, None))
    asyncio.run(_force_raw_username("users", USERS_A, "MiniAppUser"))
    for needle in ("miniappuser", "@miniappuser"):
        found = asyncio.run(db.get_user_by_username(needle))
        assert found is not None, f"не нашёл легаси-строку по вводу {needle!r}"
        assert found["telegram_id"] == USERS_A


def test_get_user_by_username_unknown_returns_none(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "@SeededUser"))
    assert asyncio.run(db.get_user_by_username("nobody")) is None


# ── Матрица поиска: find_user_id_by_username (users + reg_started) ─────────────────────────

def test_find_user_id_by_username_finds_at_stored_record_in_users(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "@SeededUser"))
    for needle in ("seededuser", "@seededuser", "SEEDEDUSER", "@SeededUser"):
        found = asyncio.run(db.find_user_id_by_username(needle))
        assert found == USERS_A, f"не нашёл по вводу {needle!r}"


def test_find_user_id_by_username_finds_legacy_dropout_without_at_in_reg_started(tmp_path):
    """Прод-случай: 947 строк `reg_started` — ВСЕ без собаки. Эта ветка искателя была мёртвым
    кодом до фикса — совпадений не находила НИКОГДА."""
    _ready(tmp_path)
    asyncio.run(db.mark_reg_started(REG_A, None))
    asyncio.run(_force_raw_username("reg_started", REG_A, "dropout"))
    for needle in ("dropout", "@dropout"):
        found = asyncio.run(db.find_user_id_by_username(needle))
        assert found == REG_A, f"не нашёл легаси-строку reg_started по вводу {needle!r}"


def test_find_user_id_by_username_finds_reg_started_stored_with_at(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.mark_reg_started(REG_A, "Dropout"))  # канон записи добавит «@»
    for needle in ("dropout", "@dropout", "Dropout", "@Dropout"):
        found = asyncio.run(db.find_user_id_by_username(needle))
        assert found == REG_A, f"не нашёл по вводу {needle!r}"


def test_find_user_id_by_username_prefers_users_over_reg_started(tmp_path):
    """Порядок ветвей сохранён: один и тот же юзернейм есть в обеих таблицах под разными
    telegram_id — искатель обязан отдать telegram_id из `users`, а не `reg_started`."""
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "@shared"))
    asyncio.run(db.mark_reg_started(REG_A, "shared"))
    found = asyncio.run(db.find_user_id_by_username("shared"))
    assert found == USERS_A


def test_find_user_id_by_username_unknown_returns_none(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "@SeededUser"))
    assert asyncio.run(db.find_user_id_by_username("nobody")) is None


# ── Сторож пустого ввода (T-0zu-01/02: не найти карточку случайного делегата) ───────────────

EMPTY_INPUTS = ("", "   ", "@", "-", "@-", None)


def test_empty_input_never_matches_placeholder_rows_in_users(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "-"))
    asyncio.run(_add_user(USERS_B, ""))
    asyncio.run(_add_user(UNKNOWN_ID, None))
    for needle in EMPTY_INPUTS:
        assert asyncio.run(db.get_user_by_username(needle)) is None, (
            f"get_user_by_username({needle!r}) не должен находить плейсхолдерную строку"
        )
        assert asyncio.run(db.find_user_id_by_username(needle)) is None, (
            f"find_user_id_by_username({needle!r}) не должен находить плейсхолдерную строку"
        )


def test_empty_input_never_matches_placeholder_rows_in_reg_started(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.mark_reg_started(REG_A, "-"))
    asyncio.run(db.mark_reg_started(REG_B, ""))
    asyncio.run(db.mark_reg_started(UNKNOWN_ID, None))
    for needle in EMPTY_INPUTS:
        assert asyncio.run(db.find_user_id_by_username(needle)) is None, (
            f"find_user_id_by_username({needle!r}) не должен находить плейсхолдерную строку reg_started"
        )


# ── Канон записи ─────────────────────────────────────────────────────────────────────────

def test_add_user_stores_username_with_leading_at(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "plainuser"))
    assert asyncio.run(_raw_username("users", USERS_A)) == "@plainuser"


def test_add_user_username_with_at_is_idempotent(tmp_path):
    """Повторная регистрация прод-делегата (username уже «@plainuser») не портит строку."""
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "@plainuser"))
    asyncio.run(_add_user(USERS_A, "@plainuser"))
    assert asyncio.run(_raw_username("users", USERS_A)) == "@plainuser"


def test_add_user_placeholder_username_left_untouched(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(USERS_A, "-"))
    asyncio.run(_add_user(USERS_B, ""))
    asyncio.run(_add_user(UNKNOWN_ID, None))
    assert asyncio.run(_raw_username("users", USERS_A)) == "-"
    assert asyncio.run(_raw_username("users", USERS_B)) == ""
    assert asyncio.run(_raw_username("users", UNKNOWN_ID)) is None


def test_mark_reg_started_stores_username_with_leading_at(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.mark_reg_started(REG_A, "dropout"))
    assert asyncio.run(_raw_username("reg_started", REG_A)) == "@dropout"


def test_mark_reg_started_none_username_stays_null(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.mark_reg_started(REG_A, None))
    assert asyncio.run(_raw_username("reg_started", REG_A)) is None


# ── Юнит-таблица store_username / username_needle ───────────────────────────────────────────

STORE_USERNAME_CASES = [
    (None, None),
    ("", ""),
    ("-", "-"),
    ("@", "@"),
    ("user", "@user"),
    ("@user", "@user"),
    ("@@user", "@user"),
]


def test_store_username_unit_table():
    for raw, expected in STORE_USERNAME_CASES:
        assert db.store_username(raw) == expected, f"store_username({raw!r})"


USERNAME_NEEDLE_CASES = [
    (None, None),
    ("", None),
    ("   ", None),
    ("@", None),
    ("-", None),
    ("@-", None),
    ("user", "user"),
    ("@user", "user"),
    ("@User", "User"),
    ("@@user", "user"),
]


def test_username_needle_unit_table():
    for raw, expected in USERNAME_NEEDLE_CASES:
        assert db.username_needle(raw) == expected, f"username_needle({raw!r})"


# ── Структурный сторож дрейфа ────────────────────────────────────────────────────────────

DB_SRC_PATH = Path(__file__).resolve().parent.parent / "database" / "db.py"


def test_no_bare_username_equality_left_in_sql_strings():
    """Новый искатель обязан сравнивать через ltrim(username, '@') — иначе баг 260911-0zu
    вернётся. Смотрим только на строковые литералы — АРГУМЕНТЫ ВЫЗОВОВ (SQL, переданный в
    db.execute(...)); докстринги и комментарии — это либо не строки вызовов (docstring — это
    отдельный Expr-стейтмент, а не аргумент), либо не часть AST вовсе (комментарии Python не
    парсит), поэтому они не красят сторож."""
    src = DB_SRC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    pattern = re.compile(r"username\s*=\s*\?")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                s = arg.value
                if pattern.search(s) and "ltrim(username" not in s:
                    bad.append(s)
    assert not bad, (
        "новый искатель обязан сравнивать через ltrim(username, '@') — иначе баг 260911-0zu "
        f"вернётся. Найдены сырые сравнения: {bad}"
    )
