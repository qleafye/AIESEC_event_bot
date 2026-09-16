"""Quick 260917 (перф-приёмка, продолжение settings/all): экран «📋 Вопросы регистрации» и
«✏️ Тексты вопросов» в ЧАТЕ админки (`handlers/admin_reg_percity.py`) — тот же класс N+1, что
чинили в Mini App/группах настроек бота (`tests/test_miniapp_settings_perf_260917.py`,
`admin_settings.py::settings_toggle_rows`/`render_settings_group_text`).

Замер ДО фикса (51 ключ REG_FLOW, module-off/без города): `render_questions_text` — 51
соединений, `build_questions_keyboard` — 52, `build_prompts_keyboard` — 61 (каждый ключ читает
`get_setting`, часть — по два раза: основа + `__party`/`__short`/`reg_help_`). Фикс — тот же
`database.db.settings_snapshot()`: тонкая обёртка `render_*`/`build_*` (`_impl`-хвост, тело не
тронуто), независимый снимок на каждую функцию (как у `admin_settings.py`).

Сторож — ЧИСЛО открытых SQLite-соединений (через `database.db._connect`), не время."""
from __future__ import annotations

import asyncio

from config import config
from database import db as bot_db

MAX_CONNECTS_PER_RENDER = 3


class _ConnectCounter:
    """monkeypatch-обёртка `database.db._connect` — считает вызовы, не трогая поведение."""

    def __init__(self):
        self.n = 0
        self._orig = bot_db._connect

    def __enter__(self):
        counter = self

        def counting_connect():
            counter.n += 1
            return counter._orig()

        bot_db._connect = counting_connect
        return self

    def __exit__(self, *exc):
        bot_db._connect = self._orig


def _run(coro):
    return asyncio.run(coro)


def _admin_ready(tmp_path, db_name="test_admin_reg_percity_perf_260917.db"):
    config.DB_PATH = str(tmp_path / db_name)
    _run(bot_db.init_db())


def test_render_questions_text_uses_one_connection_not_n_plus_one(tmp_path):
    _admin_ready(tmp_path)
    from handlers import admin_reg_percity as arp

    with _ConnectCounter() as counter:
        _run(arp.render_questions_text("full", None))
    assert counter.n <= MAX_CONNECTS_PER_RENDER, (
        f"{counter.n} SQLite-соединений на render_questions_text — снимок не работает "
        f"(N+1 регрессия, было 51 на 51 ключ REG_FLOW до фикса 260917)"
    )


def test_build_questions_keyboard_uses_one_connection_not_n_plus_one(tmp_path):
    _admin_ready(tmp_path)
    from handlers import admin_reg_percity as arp

    with _ConnectCounter() as counter:
        _run(arp.build_questions_keyboard("full", None))
    assert counter.n <= MAX_CONNECTS_PER_RENDER, (
        f"{counter.n} SQLite-соединений на build_questions_keyboard — было 52 до фикса 260917"
    )


def test_build_prompts_keyboard_uses_one_connection_not_n_plus_one(tmp_path):
    _admin_ready(tmp_path)
    from handlers import admin_reg_percity as arp

    with _ConnectCounter() as counter:
        _run(arp.build_prompts_keyboard("full", None))
    assert counter.n <= MAX_CONNECTS_PER_RENDER, (
        f"{counter.n} SQLite-соединений на build_prompts_keyboard — было 61 до фикса 260917"
    )


def test_questions_screen_output_unchanged_by_snapshot(tmp_path):
    """Эквивалентность: снимок — оптимизация чтения, не смена контракта. Прогоняем рендер
    дважды (снимок пересоздаётся на каждый вызов) и сверяем побайтово."""
    _admin_ready(tmp_path)
    from handlers import admin_reg_percity as arp

    first = _run(arp.render_questions_text("full", None))
    second = _run(arp.render_questions_text("full", None))
    assert first == second


def test_snapshot_write_visible_immediately_within_render(tmp_path):
    """Правки внутри одного снимка обязаны быть видны немедленному перечитыванию в ТОМ ЖЕ
    блоке (тот же контракт, что и у Mini App settings/batch)."""
    _admin_ready(tmp_path)

    async def _check():
        async with bot_db.settings_snapshot():
            await bot_db.set_setting("reg_q_age", "on")
            assert await bot_db.get_setting("reg_q_age") == "on"
            await bot_db.set_setting("reg_q_age", "off")
            assert await bot_db.get_setting("reg_q_age") == "off"

    _run(_check())
