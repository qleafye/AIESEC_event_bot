"""Quick 260917 (перф-приёмка, горячий путь анкеты — Mini App часть): N+1 на `GET`/`PATCH
/app/api/reg/draft` (`miniapp/routers/form.py::_draft_response`, зовёт `reg_engine.form_spec`
-> `enabled_steps`/`form_v2_flags`/`step_spec` на каждый из ~43-51 шага REG_FLOW) и на
`GET /app/api/hub` (`miniapp/routers/hub.py::hub`).

Замер ДО фикса (эквивалент — `settings_snapshot` временно заменён на no-op в
`tests/manual`-скрипте замера, здесь не воспроизводится буквально): `_draft_response` — 109
SQLite-соединений, `hub` — 16. Фикс — тот же `database.db.settings_snapshot()`, что и у
admin_settings.py/Mini App settings.py: тонкая обёртка (`_impl`-хвост) вокруг
`reg_engine.enabled_steps`/`reg_engine.form_v2_flags`/`_draft_response`/`hub`/
`services.reg_finalize.finalize_data`/`handlers.registration._advance` — ни один не порождает
asyncio.create_task/ensure_future (что было бы опасно — снимок утёк бы в фоновую задачу
навсегда, см. `test_snapshot_does_not_leak_into_spawned_background_task` ниже).

Сторож — ЧИСЛО открытых SQLite-соединений (через `database.db._connect`), не время."""
from __future__ import annotations

import asyncio

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)

MAX_CONNECTS_DRAFT_READ = 12
MAX_CONNECTS_HUB_READ = 8


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


def _setup(tmp_path, name="reg_miniapp_perf.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    return _client(_cfg(db_path))


def test_draft_get_uses_bounded_connections_not_n_plus_one(tmp_path):
    client = _setup(tmp_path)
    with _ConnectCounter() as counter:
        resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert counter.n <= MAX_CONNECTS_DRAFT_READ, (
        f"{counter.n} SQLite-соединений на GET /app/api/reg/draft — снимок bot_settings не "
        f"работает (N+1 регрессия, было 109 на стенде-эквиваленте до фикса 260917)"
    )


def test_draft_patch_uses_bounded_connections(tmp_path):
    client = _setup(tmp_path)
    with _ConnectCounter() as counter:
        resp = client.patch(
            "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
            json={"version": 0, "answers": {"age": "25"}},
        )
    assert resp.status_code == 200, resp.text
    # PATCH пишет ответ (upsert_reg_draft, своё соединение — не настройки) и затем
    # перечитывает `_draft_response` целиком — бюджет чуть шире чистого GET.
    assert counter.n <= MAX_CONNECTS_DRAFT_READ + 4, (
        f"{counter.n} SQLite-соединений на PATCH /app/api/reg/draft — N+1 регрессия"
    )


def test_hub_uses_bounded_connections_not_n_plus_one(tmp_path):
    client = _setup(tmp_path)
    with _ConnectCounter() as counter:
        resp = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert counter.n <= MAX_CONNECTS_HUB_READ, (
        f"{counter.n} SQLite-соединений на GET /app/api/hub — было 16 на стенде-эквиваленте "
        f"до фикса 260917"
    )


def test_draft_get_response_unchanged_by_snapshot(tmp_path):
    """Снимок — оптимизация чтения, не смена контракта: два подряд GET (снимок пересоздаётся
    на каждый запрос) обязаны отдать один и тот же JSON."""
    client = _setup(tmp_path)
    first = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    second = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    assert first == second


# ── Снимок не утекает в фоновую задачу ──────────────────────────────────────────────────────

def test_snapshot_does_not_leak_into_spawned_background_task(tmp_path):
    """Задокументированный риск (CLAUDE.md/план): `asyncio.create_task` копирует ТЕКУЩИЙ
    контекст (contextvars) в момент создания — если бы `_advance`/`_draft_response`/`hub`/
    `finalize_data`/`enabled_steps`/`form_v2_flags` порождали задачу ВНУТРИ своего
    `settings_snapshot()`, тот бы пережил закрытие снимка В РОДИТЕЛЕ и читал устаревшие
    настройки всю жизнь задачи — ровно та ловушка, которую этот план обязан обходить
    (поэтому ни один из перечисленных выше не содержит create_task/ensure_future — см. их
    докстринги). Тест доказывает механизм на самом примитиве, не на конкретной функции."""
    _use_tmp_db(tmp_path, "reg_miniapp_perf_leak.db")

    async def _check():
        await bot_db.set_setting("leak_test_key", "before")
        task = None

        async with bot_db.settings_snapshot():
            await bot_db.set_setting("leak_test_key", "snapshot-value")

            async def _leaked_read():
                await asyncio.sleep(0)  # родительский снимок к этому моменту уже закрыт
                return await bot_db.get_setting("leak_test_key")

            task = asyncio.create_task(_leaked_read())

        # Родительский снимок закрыт; прямое (не-снимочное) чтение обязано увидеть свежую
        # запись — задача, унаследовавшая копию контекста снимка, увидит СТАРОЕ значение.
        await bot_db.set_setting("leak_test_key", "post-snapshot-value")
        leaked_value = await task
        direct_value = await bot_db.get_setting("leak_test_key")
        return leaked_value, direct_value

    leaked_value, direct_value = _run(_check())
    assert leaked_value == "snapshot-value", (
        "задача, созданная внутри settings_snapshot(), обязана унаследовать снимок момента "
        "создания (contextvars copy-on-task) — если это утверждение перестало быть верным, "
        "пересмотреть все docstring'и «снимок безопасен, create_task не порождается»"
    )
    assert direct_value == "post-snapshot-value", (
        "обычное (вне снимка) чтение обязано видеть актуальное значение — тумблер, "
        "переключённый ПОСЛЕ закрытия снимка, не должен быть невидим для нового запроса"
    )
