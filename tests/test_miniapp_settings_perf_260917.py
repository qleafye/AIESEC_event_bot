"""Quick 260917 (перф-приёмка «настройки долго грузятся»): `GET /app/api/admin/settings/all`
на стенде — 4.65 с при ответе 3.2 КБ (`miniapp/routers/settings.py::_sections`/`_city_ctx`).

Причина — N+1: `_item_for` читает `bot_settings` через `database.db.get_setting`, а тот до
фикса открывал ОТДЕЛЬНОЕ SQLite-соединение (`_connect()`) на каждый ключ; ~700 правимых ключей
(часть — per-city, "все города" обходит все города на ключ) дают тысячи соединений на один
HTTP-ответ. Фикс — `database.db.settings_snapshot()`: один снимок `bot_settings` на запрос
(`ContextVar`, реентерабельно), `get_setting`/`set_setting`/`delete_setting` читают/пишут его,
если он открыт.

Сторож — ЧИСЛО открытых SQLite-соединений на запрос (через `database.db._connect`), не время:
время флейкает от машины, число соединений — нет. Обёртка ставится на модуль `database.db`
целиком (симметрично `tests/test_db_wal_busy_timeout.py`), не на copy/paste счётчика в
каждом тесте.
"""
from __future__ import annotations

import asyncio

import settings_ops
from database import db as bot_db
from settings_schema import SETTINGS_SCHEMA

from tests.test_miniapp_routes import ADMIN_ID, _cfg, _client, _seed, _set, _standard_seed, _use_tmp_db, _hdr

# Верхняя граница соединений на запрос — есть небольшой запас над «1 снимок + служебные»
# (например /hints параллельно читает очередь тихих часов из другой таблицы своим соединением),
# но НИКАК не сотни/тысячи, которые были до фикса (стенд: 2239 на /settings/all).
MAX_CONNECTS_PER_REQUEST = 5

# Запись — отдельный бюджет: каждая настоящая правка (`set_setting`/аудит/очередь перевода)
# честно открывает своё соединение (это не N+1 над реестром, это N ЗАПИСЕЙ на N изменений в
# теле запроса), но всё ещё НА ПОРЯДКИ меньше, чем N+1 над всем реестром при чтении.
MAX_CONNECTS_PER_WRITE_REQUEST = 12


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


def _seed_realistic_settings():
    """~700 ключей реестра + per-city оверрайды на паре десятков — тот же порядок величины,
    что на стенде (349 настроек только у раздела «Анкета»), не игрушечная горстка значений."""
    keys = list(settings_ops.editable_keys())
    for i, key in enumerate(keys):
        if i % 3 == 0:
            _set(key, "on" if i % 2 == 0 else "текст-значение")
    per_city_keys = [k for k in keys if SETTINGS_SCHEMA[k].get("per_city")][:20]
    from cities import per_city_key
    for key in per_city_keys:
        for code in ("msk", "spb", "tyumen"):
            _set(per_city_key(key, code), "override")


def _setup(tmp_path, name="miniapp_settings_perf.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    _set("event_city_enabled", "on")
    _seed_realistic_settings()
    return _client(_cfg(db_path))


def test_settings_all_uses_one_connection_not_n_plus_one(tmp_path):
    client = _setup(tmp_path)
    with _ConnectCounter() as counter:
        resp = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    assert counter.n <= MAX_CONNECTS_PER_REQUEST, (
        f"{counter.n} SQLite-соединений на /settings/all — снимок bot_settings не работает "
        f"(N+1 регрессия, было 2239 на стенде до фикса 260917)"
    )


def test_setup_status_uses_one_connection_not_n_plus_one(tmp_path):
    client = _setup(tmp_path)
    with _ConnectCounter() as counter:
        resp = client.get("/app/api/admin/setup", headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    assert counter.n <= MAX_CONNECTS_PER_REQUEST


def test_settings_batch_uses_bounded_connections(tmp_path):
    client = _setup(tmp_path)
    with _ConnectCounter() as counter:
        resp = client.post(
            "/app/api/admin/settings/batch",
            json={"changes": [{"key": "event_name", "value": "Новое имя"}], "base": {}, "confirm": []},
            headers=_hdr(ADMIN_ID),
        )
    assert resp.status_code == 200, resp.text
    assert counter.n <= MAX_CONNECTS_PER_WRITE_REQUEST


def test_settings_all_response_equals_before_fix_shape(tmp_path):
    """Эквивалентность ответа: снимок — оптимизация чтения, не смена контракта. Прогоняем
    запрос ДВАЖДЫ (снимок пересоздаётся на каждый вызов) и сверяем побайтово — снимок не мог
    внести недетерминизм (порядок ключей словаря, отсутствующие поля)."""
    client = _setup(tmp_path)
    first = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    second = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    assert first == second


def test_batch_write_visible_immediately_within_same_request(tmp_path):
    """Снимок обязан быть самосогласован внутри ОДНОГО запроса: `settings/batch` пишет через
    `set_setting` и тут же перечитывает записанные ключи для ответа `items` — без обновления
    снимка при записи менеджер увидел бы старое значение в ответе на своё же сохранение."""
    _use_tmp_db(tmp_path, "settings_perf_batch_write.db")

    async def _check():
        async with bot_db.settings_snapshot():
            await bot_db.set_setting("start_text", "Привет!")
            assert await bot_db.get_setting("start_text") == "Привет!"
            await bot_db.set_setting("start_text", "Другой текст")
            assert await bot_db.get_setting("start_text") == "Другой текст"

    _run(_check())


def test_snapshot_scope_does_not_leak_to_next_call(tmp_path):
    """Выход из `settings_snapshot()` обязан прекращать резолвинг из снимка — иначе правка
    следующего HTTP-запроса не увидела бы менеджер (правило CLAUDE.md: тумблер переключили —
    следующий запрос обязан увидеть новое значение)."""
    _use_tmp_db(tmp_path, "settings_perf_leak.db")

    async def _check():
        async with bot_db.settings_snapshot():
            await bot_db.set_setting("some_key", "внутри снимка")
        # снимок закрыт — следующее чтение обязано снова идти напрямую в БД
        await bot_db.set_setting("some_key", "после снимка")
        assert await bot_db.get_setting("some_key") == "после снимка"

    _run(_check())
