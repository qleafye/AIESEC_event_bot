"""Приёмка 19.09 (review-260919, раздел «Модерация», находки №2/№3): развилка резюме
(`reg_resume_mode = fork`), ветка «мини-профиль» — три её шага (`mini_projects`/
`mini_portfolio`/`mini_direction`) обязаны работать без второго тумблера `reg_q_mini_*`
(дефолт `off`, развилка о нём не знает). Один общий предикат
(`reg_engine.mini_resume_branch_active`) управляет и очерёдностью шагов анкеты
(`reg_engine.enabled_steps`, тем же вызовом пользуется Mini App — `miniapp/routers/form.py`),
и шапкой/строкой Google-листа (`handlers/reg_schema.active_sheet_headers`).

pytest-asyncio недоступен в этом окружении — асинхронщина через `asyncio.run()`, БД —
временная (`config.DB_PATH = tmp_path / "..."` + `database.db.init_db()`), тот же приём, что
`tests/test_percity_sheets_25.py`/`tests/test_skillup_resume_fork_28.py`.
"""
import asyncio

from config import config
from database import db
import reg_engine
from handlers import reg_schema
from handlers import admin_reg_config
from handlers import admin_reg_percity
import services.sheets as sheets_mod


def _ready(tmp_path, name="test_resume_mini_fork_260919.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _run(coro):
    return asyncio.run(coro)


def _drain():
    """`_refresh_sheet_header` зовёт `ensure_sheet_header`/`ensure_named_sheet_header` через
    `services.background.spawn` (fire-and-forget) — без gather тест проверил бы вызовы до
    того, как задача успела выполниться (тот же приём, что `tests/test_percity_sheets_25.py`)."""
    async def _g():
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
    return _g


# ══════════════════════════════════════════════════════════════════════════════════════════
# reg_engine.mini_resume_branch_active — единый предикат
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_mini_resume_branch_active_true_only_in_fork_mode(tmp_path):
    _ready(tmp_path)

    async def go():
        before = await reg_engine.mini_resume_branch_active()
        await db.set_setting("reg_resume_mode", "fork")
        during = await reg_engine.mini_resume_branch_active()
        await db.set_setting("reg_resume_mode", "text_only")
        after = await reg_engine.mini_resume_branch_active()
        return before, during, after

    before, during, after = _run(go())
    assert before is False
    assert during is True
    assert after is False


# ══════════════════════════════════════════════════════════════════════════════════════════
# enabled_steps: мини-профиль — три шага подряд, без второго тумблера
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_enabled_steps_includes_all_three_mini_steps_in_fork_mini_branch(tmp_path):
    """Находки №2/№3: раньше `_advance` не находил `mini_projects` в `enabled` (личный
    тумблер выключен) и заканчивал анкету сразу после первого подшага — `mini_portfolio`/
    `mini_direction` не задавались вовсе."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        data = {"participant_type": "full", "resume_type": "mini"}
        return await reg_engine.enabled_steps(data)

    steps = _run(go())
    assert "mini_projects" in steps
    assert "mini_portfolio" in steps
    assert "mini_direction" in steps
    # Порядок — как в REG_FLOW (тот же порядок, что печатает лист/карточка/сводка).
    idx = [steps.index(s) for s in ("mini_projects", "mini_portfolio", "mini_direction")]
    assert idx == sorted(idx)


def test_enabled_steps_excludes_mini_steps_when_branch_not_chosen(tmp_path):
    """Развилка в режиме fork, но делегат выбрал другую ветку — мини-шаги не появляются."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        data = {"participant_type": "full", "resume_type": "file"}
        return await reg_engine.enabled_steps(data)

    steps = _run(go())
    assert "mini_projects" not in steps
    assert "mini_portfolio" not in steps
    assert "mini_direction" not in steps


def test_enabled_steps_excludes_mini_steps_when_mode_not_fork(tmp_path):
    """`resume_type == "mini"` физически недостижим вне fork-режима (пишет только развилка),
    но сторож на будущее: bypass не срабатывает без `mode == "fork"`."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "file_or_text")
        data = {"participant_type": "full", "resume_type": "mini"}
        return await reg_engine.enabled_steps(data)

    steps = _run(go())
    assert "mini_projects" not in steps
    assert "mini_portfolio" not in steps
    assert "mini_direction" not in steps


def test_enabled_steps_respects_manual_toggle_outside_fork_branch(tmp_path):
    """Личный тумблер `reg_q_mini_projects` включён вручную, но делегат НЕ в ветке «мини»
    (`resume_type` пуст/другой) — старое поведение не ломается: шаг всё равно выключен, пока
    не выбрана ветка."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_q_mini_projects", "on")
        data = {"participant_type": "full", "resume_type": None}
        return await reg_engine.enabled_steps(data)

    steps = _run(go())
    assert "mini_projects" not in steps


# ══════════════════════════════════════════════════════════════════════════════════════════
# active_sheet_headers: колонки мини-профиля появляются вместе с режимом fork
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_active_sheet_headers_gains_mini_columns_in_fork_mode(tmp_path):
    _ready(tmp_path)

    async def go():
        off_headers = await reg_schema.active_sheet_headers()
        await db.set_setting("reg_resume_mode", "fork")
        on_headers = await reg_schema.active_sheet_headers()
        return off_headers, on_headers

    off_headers, on_headers = _run(go())
    for col in ("Проекты", "Портфолио", "Направление развития"):
        assert col not in off_headers
        assert col in on_headers


def test_active_sheet_headers_mini_columns_also_via_personal_toggle(tmp_path):
    """Личный тумблер `reg_q_mini_projects` продолжает работать сам по себе (событие вне
    развилки, вопрос включён напрямую) — предикат fork НЕ единственный путь появления колонки."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_q_mini_projects", "on")
        return await reg_schema.active_sheet_headers()

    headers = _run(go())
    assert "Проекты" in headers
    assert "Портфолио" not in headers  # свой тумблер не трогали


# ══════════════════════════════════════════════════════════════════════════════════════════
# reg_resume_mode_toggle: та же синхронизация шапки листа, что у обычных вопросных тумблеров
# ══════════════════════════════════════════════════════════════════════════════════════════

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edit_calls += 1


class _FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


ADMIN_ID = 920803


def test_resume_mode_toggle_global_refreshes_main_sheet_header(tmp_path, monkeypatch):
    _ready(tmp_path, "test_resume_mini_fork_toggle_global.db")
    config.ADMIN_IDS = [ADMIN_ID]

    calls = []

    async def fake_ensure_main(headers):
        calls.append(("main", headers))

    monkeypatch.setattr(admin_reg_config, "ensure_sheet_header", fake_ensure_main)

    async def go():
        cb = _FakeCallback("reg_resume_mode_toggle", ADMIN_ID)
        await admin_reg_percity.reg_resume_mode_toggle(cb)
        await _drain()()

    _run(go())
    assert calls, "тумблер режима резюме обязан позвать _refresh_sheet_header (main tab)"


def test_resume_mode_toggle_percity_refreshes_that_citys_tab(tmp_path, monkeypatch):
    _ready(tmp_path, "test_resume_mini_fork_toggle_city.db")
    config.ADMIN_IDS = [ADMIN_ID]

    async def prepare():
        await db.set_setting("event_city_enabled", "on")
        import cities
        await cities.set_admin_city(ADMIN_ID, "spb")

    _run(prepare())

    named_calls = []

    async def fake_ensure_named(tab, headers):
        named_calls.append((tab, headers))

    monkeypatch.setattr(sheets_mod, "ensure_named_sheet_header", fake_ensure_named)

    async def go():
        cb = _FakeCallback("reg_resume_mode_toggle", ADMIN_ID)
        await admin_reg_percity.reg_resume_mode_toggle(cb)
        await _drain()()

    _run(go())
    assert named_calls, "городской тумблер режима резюме обязан обновить СВОЮ вкладку"
