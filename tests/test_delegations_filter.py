"""Делегации вузов (D-07, D-21): «🏫 Делегации» как поле фильтра рассылки.

Две стороны одной фичи:
- SQL (`database/db.py`): виртуальное поле `delegation_any` (любая делегация / не делегация,
  fail closed на незнакомом значении) и обычная колонка `delegation` (вуз из формы, равенство);
- экран (`handlers/comms/admin_broadcasts.py`): подписи, двойная регистрация в `_PICKER_FIELDS`,
  кнопка меню только когда в базе есть обе стороны, пикер с человеческими подписями.

Образец — `tests/test_broadcast_auto_reject_filter.py` (тот же приём двойной регистрации, тот же
харнес FakeCallback/FakeState). pytest-asyncio не установлен — `asyncio.run()`,
`config.DB_PATH` указывает на файл в `tmp_path`.
"""
import asyncio

from config import config
from database import db
from database.db import _build_filter_clause
from tests._dbtpl import fast_init_db


# ── двойная регистрация + подписи ────────────────────────────────────────────────────────────

def test_delegation_fields_double_registration():
    from handlers.comms import admin_broadcasts
    assert "delegation_any" in admin_broadcasts._PICKER_FIELDS
    assert "delegation" in admin_broadcasts._PICKER_FIELDS
    assert "delegation_any" in db._FILTER_COLUMNS
    assert "delegation" in db._FILTER_COLUMNS


def test_delegation_any_is_virtual_and_delegation_is_a_real_column():
    assert "delegation_any" in db._FILTER_VIRTUAL_FIELDS
    assert "delegation" not in db._FILTER_VIRTUAL_FIELDS


def test_filter_field_labels():
    from handlers.comms import admin_broadcasts
    assert admin_broadcasts._FILTER_FIELD_LABELS["delegation_any"] == "Делегация вуза"
    assert admin_broadcasts._FILTER_FIELD_LABELS["delegation"] == "Вуз делегации"


def test_sentinels():
    assert db.DELEGATION_YES == "yes"
    assert db.DELEGATION_NO == "no"


# ── SQL-ветка ────────────────────────────────────────────────────────────────────────────────

def test_clause_delegation_any_yes_no_and_fail_closed():
    where, params = _build_filter_clause([{"field": "delegation_any", "value": db.DELEGATION_YES}])
    assert "delegation IS NOT NULL AND TRIM(delegation) != ''" in where
    assert params == []

    where, params = _build_filter_clause([{"field": "delegation_any", "value": db.DELEGATION_NO}])
    assert "delegation IS NULL OR TRIM(delegation) = ''" in where
    assert params == []

    where, params = _build_filter_clause([{"field": "delegation_any", "value": "weird"}])
    assert where == " WHERE 0"
    assert params == []

    where, _ = _build_filter_clause([{"field": "delegation_any"}])
    assert where == " WHERE 0"


def test_clause_delegation_is_generic_equality():
    where, params = _build_filter_clause([{"field": "delegation", "value": "Тестовый университет"}])
    assert where == " WHERE delegation = ?"
    assert params == ["Тестовый университет"]


# ── сквозной сценарий на засеянной базе ─────────────────────────────────────────────────────

def _seed(tmp_path, dbname, rows):
    """rows: список (telegram_id, delegation|None). `delegation` — не колонка анкеты, `add_user`
    её не знает; пишем прямым UPDATE (та же дисциплина, что у `auto_reject_rule_ids`)."""
    config.DB_PATH = str(tmp_path / dbname)

    async def go():
        fast_init_db()
        for tid, delegation in rows:
            await db.add_user({
                "telegram_id": tid,
                "full_name": f"User {tid}",
                "registration_date": f"2026-01-01 09:{tid:02d}:00",
            })
            if delegation is not None:
                async with db._connect() as conn:
                    await conn.execute(
                        "UPDATE users SET delegation = ? WHERE telegram_id = ?", (delegation, tid),
                    )
                    await conn.commit()

    asyncio.run(go())


def test_count_and_list_filtered_delegation_any(tmp_path):
    _seed(tmp_path, "dlg_any.db", [(1, "МГУ"), (2, None), (3, ""), (4, "ВШЭ")])
    yes = asyncio.run(db.count_and_list_filtered([{"field": "delegation_any", "value": db.DELEGATION_YES}]))
    assert set(yes) == {1, 4}
    no = asyncio.run(db.count_and_list_filtered([{"field": "delegation_any", "value": db.DELEGATION_NO}]))
    assert set(no) == {2, 3}
    nobody = asyncio.run(db.count_and_list_filtered([{"field": "delegation_any", "value": "weird"}]))
    assert list(nobody) == []


def test_count_and_list_filtered_by_university(tmp_path):
    _seed(tmp_path, "dlg_uni.db", [(1, "МГУ"), (2, None), (3, "ВШЭ"), (4, "МГУ")])
    ids = asyncio.run(db.count_and_list_filtered([{"field": "delegation", "value": "МГУ"}]))
    assert set(ids) == {1, 4}


def test_get_delegation_filter_options_threshold(tmp_path):
    _seed(tmp_path, "dlg_opts_empty.db", [])
    assert asyncio.run(db.get_delegation_filter_options()) == []

    _seed(tmp_path, "dlg_opts_all.db", [(1, "МГУ"), (2, "ВШЭ")])
    assert asyncio.run(db.get_delegation_filter_options()) == [db.DELEGATION_YES]

    _seed(tmp_path, "dlg_opts_none.db", [(1, None), (2, "")])
    assert asyncio.run(db.get_delegation_filter_options()) == [db.DELEGATION_NO]

    _seed(tmp_path, "dlg_opts_both.db", [(1, "МГУ"), (2, None)])
    assert asyncio.run(db.get_delegation_filter_options()) == [db.DELEGATION_YES, db.DELEGATION_NO]


def test_get_distinct_filter_values(tmp_path):
    _seed(tmp_path, "dlg_distinct.db", [(1, "МГУ"), (2, None), (3, "ВШЭ"), (4, "МГУ")])
    assert asyncio.run(db.get_distinct_filter_values("delegation_any")) == []
    assert asyncio.run(db.get_distinct_filter_values("delegation")) == ["ВШЭ", "МГУ"]


# ── экран: меню и пикер ─────────────────────────────────────────────────────────────────────

def _flat(kb):
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def test_filter_menu_kb_delegations_button_only_when_asked():
    from handlers.comms.admin_broadcasts import _filter_menu_kb
    before = _filter_menu_kb([])
    assert ("🏫 Делегации", "filter_f_delegation_any") not in _flat(before)
    with_flag = _filter_menu_kb([], show_delegations=True)
    assert ("🏫 Делегации", "filter_f_delegation_any") in _flat(with_flag)
    # дефолт False — клавиатура байт-в-байт прежняя
    assert _filter_menu_kb([]) == before


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = _FakeUser(1)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _FakeState:
    def __init__(self):
        self.data = {}

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)


def test_value_picker_delegation_any_gate_when_one_side(tmp_path):
    from handlers.comms.admin_broadcasts import _show_value_picker
    _seed(tmp_path, "dlg_picker_gate.db", [(1, None), (2, None)])
    cb, st = _FakeCallback("filter_f_delegation_any"), _FakeState()
    asyncio.run(_show_value_picker(cb, st, "delegation_any", "Выберите значение"))
    assert cb.answers == [("Делегаций в базе пока нет — фильтровать не по чему.", True)]
    assert cb.message.edits == []


def test_value_picker_delegation_any_human_labels(tmp_path):
    from handlers.comms.admin_broadcasts import _show_value_picker
    _seed(tmp_path, "dlg_picker.db", [(1, "МГУ"), (2, None)])
    cb, st = _FakeCallback("filter_f_delegation_any"), _FakeState()
    asyncio.run(_show_value_picker(cb, st, "delegation_any", "Выберите значение"))
    assert cb.message.edits, "picker not rendered"
    texts = [t for t, _ in _flat(cb.message.edits[0][1])]
    assert "Делегация вуза" in texts
    assert "Не делегация" in texts
    assert "yes" not in texts and "no" not in texts
    assert st.data["filter_options"] == [db.DELEGATION_YES, db.DELEGATION_NO]
    assert st.data["filter_option_labels"] == {db.DELEGATION_YES: "Делегация вуза", db.DELEGATION_NO: "Не делегация"}


def test_value_picker_delegation_lists_universities(tmp_path):
    from handlers.comms.admin_broadcasts import _show_value_picker
    _seed(tmp_path, "dlg_picker_uni.db", [(1, "МГУ"), (2, None), (3, "ВШЭ")])
    cb, st = _FakeCallback("filter_f_delegation"), _FakeState()
    asyncio.run(_show_value_picker(cb, st, "delegation", "Выберите значение"))
    texts = [t for t, _ in _flat(cb.message.edits[0][1])]
    assert "МГУ" in texts and "ВШЭ" in texts
