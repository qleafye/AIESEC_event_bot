"""Квик 260912 (QUICK-260912-LWY): «Вопрос не задавался» вместо «Самостоятельно» в
`users.source`. `with_defaults` подставляет эту подпись, когда шага «Откуда узнал» в треке
делегата не было и реф-ссылки нет — раньше подстановка называлась «Самостоятельно» и
менеджер на дашборде читал её как осознанный ответ делегата.

Секции:
    with_defaults — новая константа побеждает старый литерал, ответ делегата и
    реферальная ветка не задеты, сторож TRANSPORT_OPTIONS (другая колонка, не трогаем).
    Миграция БД — задача 2 дописывает сюда тест `init_db`, приводящий старые строки
    к новой подписи (приём tmp_path + config.DB_PATH, как в test_admin_percity_ui.py).
"""
import asyncio

import reg_options
from config import config
from database import db
from reg_engine import with_defaults


# ── with_defaults ────────────────────────────────────────────────────────────────────────────

def test_with_defaults_uses_source_not_asked_when_nothing_known():
    result = with_defaults({})
    assert result["source"] == reg_options.SOURCE_NOT_ASKED
    assert result["source"] == "Вопрос не задавался"


def test_with_defaults_referrer_wins_over_source_not_asked():
    result = with_defaults({"referrer_id": 7})
    assert result["source"] == "Реферальная ссылка"


def test_with_defaults_real_answer_wins_over_substitution():
    result = with_defaults({"source": "Соцсети Юлид"})
    assert result["source"] == "Соцсети Юлид"


def test_with_defaults_source_details_unaffected():
    assert with_defaults({})["source_details"] == "Referrer ID: -"


def test_transport_options_untouched_by_source_rename():
    """Сторож: «Самостоятельно» остаётся настоящим вариантом ответа шага «Трансфер»
    (другая колонка, users.transport) — этот квик её не трогает."""
    assert "Самостоятельно" in reg_options.TRANSPORT_OPTIONS


# ── Миграция старых строк (Задача 2) ────────────────────────────────────────────────────────

def _init_tmp_db(tmp_path, db_name="test_source_not_asked_260912.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())


def test_init_db_migrates_legacy_source_label_and_is_idempotent(tmp_path):
    _init_tmp_db(tmp_path)

    async def seed_and_check():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, source, transport) "
                "VALUES (?, ?, ?, ?)",
                (910100001, "Легаси Делегат", reg_options.SOURCE_NOT_ASKED_LEGACY, "Самостоятельно"),
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, source, transport) "
                "VALUES (?, ?, ?, ?)",
                (910100002, "Честный Ответ", "Соцсети Юлид", None),
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, source, transport) "
                "VALUES (?, ?, ?, ?)",
                (910100003, "Реферал", "Реферальная ссылка", None),
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, source, transport) "
                "VALUES (?, ?, ?, ?)",
                (910100004, "Пустышка", "-", None),
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, source, transport) "
                "VALUES (?, ?, ?, ?)",
                (910100005, "NULL-источник", None, None),
            )
            await conn.commit()

        # Повторный init_db — та самая точка, где живёт одноразовая нормализация.
        await db.init_db()

        rows = {}
        async with db._connect() as conn:
            cur = await conn.execute(
                "SELECT telegram_id, source, transport FROM users WHERE telegram_id >= 910100001"
            )
            for telegram_id, source, transport in await cur.fetchall():
                rows[telegram_id] = (source, transport)
        return rows

    rows = asyncio.run(seed_and_check())

    assert rows[910100001] == (reg_options.SOURCE_NOT_ASKED, "Самостоятельно"), (
        "старая подпись source приведена к новой; transport с тем же словом не тронут"
    )
    assert rows[910100002] == ("Соцсети Юлид", None)
    assert rows[910100003] == ("Реферальная ссылка", None)
    assert rows[910100004] == ("-", None)
    assert rows[910100005] == (None, None)


def test_init_db_migration_is_idempotent_on_second_run(tmp_path):
    _init_tmp_db(tmp_path)

    async def go():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, source) VALUES (?, ?, ?)",
                (910100006, "Легаси Делегат 2", reg_options.SOURCE_NOT_ASKED_LEGACY),
            )
            await conn.commit()

        await db.init_db()  # первый прогон после вставки — приводит подпись

        async with db._connect() as conn:
            cur = await conn.execute(
                "UPDATE users SET source = ? WHERE source = ?",
                (reg_options.SOURCE_NOT_ASKED, reg_options.SOURCE_NOT_ASKED_LEGACY),
            )
            second_run_rowcount = cur.rowcount
            await conn.commit()

        async with db._connect() as conn:
            cur = await conn.execute(
                "SELECT source FROM users WHERE telegram_id = ?", (910100006,)
            )
            row = await cur.fetchone()
        return second_run_rowcount, row[0]

    second_run_rowcount, source = asyncio.run(go())
    assert second_run_rowcount == 0, "повторный прогон не должен находить строк для обновления"
    assert source == reg_options.SOURCE_NOT_ASKED
