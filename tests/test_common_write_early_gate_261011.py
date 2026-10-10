"""Ранний гейт: если хендлер делает внешний эффект (Google, выгрузка) и потом пишет общий ключ,
менеджеру с городом отказ приходит ДО эффекта, а не воронкой записи после него.

- «🏷 Приписка к вкладкам» и переименование вкладки: Google не трогается вовсе.
- «🏫 Делегации»: экран показывается без действий, любая старая кнопка или ввод — объяснение;
  выгрузка прежней формы снимается только после записи новой формы.
- Отказ в группе — только лог, в чат не пишем.
- Строка outbox от приложения прежней версии: название берётся у самого задания."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import domain.settings.ops as settings_ops
from database import db
from handlers.delegations import admin_delegations as dlg_mod
from handlers.settings import admin_settings_global as gscope
from handlers.sheets import admin_sheet_tabs as tabs
from handlers.states import DelegationEdit
from services.delegations import delegations as dlg
from services.infra import miniapp_outbox
from services.settings import audit
from tests.test_admin_sections_ia20 import FakeCallback
from tests.test_common_settings_bound_manager_261010 import _bound_manager, _state
from tests.test_delegations_admin import _form, _select
from tests.test_game_submit_digest_260822 import _capture_notify
from tests.test_gamification_delegate_phase9 import FakeBot
from tests.test_miniapp_outbox_job import _enqueue
from tests.test_roles_phase8 import ADMIN_ID, MANAGER_ID

DENIED = settings_ops.COMMON_DENIED_TEXT
LOCKED = dlg_mod.CITY_LOCKED_TEXT


def _no_google(monkeypatch):
    calls = []

    async def _titles():
        calls.append("list")
        return None  # «таблица недоступна» — дальше суперадмин не идёт, Google не нужен

    async def _rename(old, new):
        calls.append(("rename", old, new))
        return "ok"

    monkeypatch.setattr(tabs, "list_worksheet_titles", _titles)
    monkeypatch.setattr(tabs, "rename_worksheet", _rename)
    return calls


# ── 1. вкладки таблицы ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("handler", [
    tabs.sheet_tabs_prefix_add, tabs.sheet_tabs_prefix_del,
    tabs.sheet_tabs_prefix_add_go, tabs.sheet_tabs_prefix_del_go,
])
def test_prefix_plan_is_refused_before_google(tmp_path, monkeypatch, handler):
    _bound_manager(tmp_path)
    calls = _no_google(monkeypatch)
    cb = FakeCallback("sheet_tabs_prefix", user_id=MANAGER_ID)
    asyncio.run(handler(cb))
    assert cb.answers == [(DENIED, True)]
    assert calls == [] and cb.message.edit_calls == 0


def test_superadmin_prefix_plan_still_reaches_google(tmp_path, monkeypatch):
    _bound_manager(tmp_path)
    calls = _no_google(monkeypatch)
    cb = FakeCallback("sheet_tabs_prefix_add_go", user_id=ADMIN_ID)
    asyncio.run(tabs.sheet_tabs_prefix_add_go(cb))
    assert calls == ["list"]
    assert cb.answers and cb.answers[0][0] != DENIED


def test_tab_rename_is_refused_before_google(tmp_path, monkeypatch):
    _bound_manager(tmp_path)
    calls = _no_google(monkeypatch)
    state = _state(MANAGER_ID)
    asyncio.run(state.set_data({
        "pending_tab_key": "polls_sheet_tab", "pending_tab_value": "Опросы 2", "pending_tab_old": "Опросы",
    }))
    cb = FakeCallback("sheet_tab_rename_go", user_id=MANAGER_ID)
    asyncio.run(tabs.sheet_tab_rename_go(cb, state))
    assert cb.answers == [(DENIED, True)] and calls == []
    assert asyncio.run(db.get_setting("polls_sheet_tab")) is None


def test_city_tab_rename_result_refuses_before_city_change(tmp_path, monkeypatch):
    code = _bound_manager(tmp_path)
    changed = []

    async def _update_city(*a, **k):
        changed.append((a, k))

    monkeypatch.setattr(tabs, "update_city", _update_city)
    target = SimpleNamespace(kind="main", city_code=code)
    with pytest.raises(audit.CommonSettingDenied):
        asyncio.run(tabs._apply_city_rename_result(MANAGER_ID, target, "Новая база"))
    assert changed == []


# ── 2. делегации ───────────────────────────────────────────────────────────────────────────

def _buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_bound_manager_sees_delegations_screen_without_actions(tmp_path):
    _bound_manager(tmp_path)
    _select(_form())
    cb = FakeCallback("admin_delegations", user_id=MANAGER_ID)
    asyncio.run(dlg_mod.admin_delegations(cb))
    assert LOCKED in cb.message.text
    assert not [d for d in _buttons(cb.message.markup) if d.startswith("dlg_")]


def test_bound_manager_without_form_sees_only_the_lock(tmp_path):
    _bound_manager(tmp_path)
    cb = FakeCallback("admin_delegations", user_id=MANAGER_ID)
    asyncio.run(dlg_mod.admin_delegations(cb))
    assert LOCKED in cb.message.text
    assert not [d for d in _buttons(cb.message.markup) if d.startswith("dlg_")]


def test_bound_manager_old_form_button_changes_nothing(tmp_path, monkeypatch):
    _bound_manager(tmp_path)
    prev, new = _form("Старая"), _form("Новая")
    _select(prev)
    released = []

    async def _release(*a):
        released.append(a)

    monkeypatch.setattr(dlg_mod, "_release_previous_export", _release)
    cb = FakeCallback(f"dlg_form:{new}", user_id=MANAGER_ID)
    asyncio.run(dlg_mod.dlg_form(cb))
    assert cb.answers == [(LOCKED, True)]
    assert released == [] and asyncio.run(dlg.delegation_form_id()) == prev


def test_bound_manager_text_input_is_refused_and_input_closed(tmp_path):
    _bound_manager(tmp_path)
    state = _state(MANAGER_ID)
    asyncio.run(state.set_state(DelegationEdit.waiting_cutoff))
    msg = SimpleNamespace(from_user=SimpleNamespace(id=MANAGER_ID), text="01.09.2026", answers=[])

    async def _answer(text=None, **kw):
        msg.answers.append(text)

    msg.answer = _answer
    asyncio.run(dlg_mod.dlg_cutoff_input(msg, state))
    assert msg.answers == [LOCKED]
    assert asyncio.run(state.get_state()) is None
    assert asyncio.run(db.get_setting("delegation_ta_cutoff")) is None


def test_previous_export_released_only_after_new_form_is_saved(tmp_path, monkeypatch):
    _bound_manager(tmp_path, bound=False)
    prev, new = _form("Старая"), _form("Новая")
    _select(prev)
    seen = []

    async def _release(prev_id, new_id):
        seen.append(await dlg.delegation_form_id())

    monkeypatch.setattr(dlg_mod, "_release_previous_export", _release)
    asyncio.run(dlg_mod.dlg_form(FakeCallback(f"dlg_form:{new}", user_id=ADMIN_ID)))
    assert seen == [new]


_DLG_MODULES = [
    "handlers/delegations/admin_delegations.py",
    "handlers/delegations/admin_delegations_review.py",
    "handlers/delegations/admin_delegations_sheet.py",
]


@pytest.mark.parametrize("path", _DLG_MODULES)
def test_every_delegations_handler_starts_with_the_gate(path):
    """Новый хендлер экрана не сможет забыть гейт: каждый, кроме входа (рисует экран сам) и
    отмены, первым делом спрашивает `deny_locked`."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    missing = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or not node.decorator_list:
            continue
        if not ast.unparse(node.decorator_list[0]).startswith("router."):
            continue
        if node.name in {"admin_delegations", "dlg_cancel"}:
            continue
        body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        if "deny_locked(" not in ast.unparse(body[0]):
            missing.append(node.name)
    assert missing == []


# ── 3. группа ──────────────────────────────────────────────────────────────────────────────

class _GroupMsg:
    def __init__(self, chat_type):
        self.chat = SimpleNamespace(type=chat_type)
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


@pytest.mark.parametrize("chat_type, expected", [("group", []), ("supergroup", []), ("private", [DENIED])])
def test_denial_is_not_posted_into_a_group(chat_type, expected):
    msg = _GroupMsg(chat_type)
    event = SimpleNamespace(update=SimpleNamespace(callback_query=None, message=msg))
    assert asyncio.run(gscope.on_common_setting_denied(event)) is True
    assert msg.answers == expected


# ── 4. старая строка outbox ────────────────────────────────────────────────────────────────

def test_old_outbox_row_takes_title_from_the_task(tmp_path, monkeypatch):
    _bound_manager(tmp_path)
    calls = _capture_notify(monkeypatch)
    task_id = asyncio.run(db.create_task(
        "Выложи пост про форум и пришли скрин", "Light", 10, "text", "2026-12-31 23:59:00",
        ADMIN_ID, title="Пост про форум",
    ))
    _enqueue("submission_created", {
        "submission_id": 1, "user_id": 2, "task_id": task_id,
        "task_text": "Выложи пост про форум и пришли скрин", "submitter_name": "Ира",
    })
    assert asyncio.run(miniapp_outbox.drain(FakeBot())) == 1
    assert "«Пост про форум»" in calls[0]["text"]
