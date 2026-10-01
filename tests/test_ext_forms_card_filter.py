"""Фильтр рассылки «📝 Внешняя форма» (заполнил / не заполнил)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from database import ext_forms_db as efd
from database.db import _build_filter_clause
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import FakeCallback, _fresh_state

ADMIN_ID = 900935


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "ext_form_filter.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


async def _seed():
    for tid in (1, 2, 3):
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status) VALUES (?, ?, 'approved')",
                (tid, f"Д{tid}"),
            )
            await conn.commit()
    f1 = await efd.create_form(platform="yandex", external_id="a", title="Анкета А")
    f2 = await efd.create_form(platform="yandex", external_id="b", title="Анкета Б")
    await efd.insert_answer(form_id=f1, answer_id="1", answered_at=None, received_at="2026-10-01",
                            payload=[], raw=None, matched_telegram_id=1, match_how="username")
    await efd.insert_answer(form_id=f2, answer_id="1", answered_at=None, received_at="2026-10-01",
                            payload=[], raw=None, matched_telegram_id=2, match_how="username")
    return f1, f2


def test_filter_filled_and_not_filled(tmp_path):
    _ready(tmp_path)
    f1, _ = _run(_seed())
    got = _run(db.count_and_list_filtered([{"field": "ext_form", "value": "filled", "form_id": f1}]))
    assert sorted(got) == [1]
    got = _run(db.count_and_list_filtered([{"field": "ext_form", "value": "not_filled", "form_id": f1}]))
    assert sorted(got) == [2, 3]


def test_filter_fail_closed():
    for spec in (
        {"field": "ext_form", "value": "bogus", "form_id": 1},
        {"field": "ext_form", "value": "filled"},
        {"field": "ext_form", "value": "filled", "form_id": "x'; DROP"},
    ):
        clause, params = _build_filter_clause([spec])
        assert "0" in clause and params == []


def test_filter_distinct_values_no_crash(tmp_path):
    _ready(tmp_path)
    assert _run(db.get_distinct_filter_values("ext_form")) == []


def test_filter_deleted_form_matches_nobody_for_filled(tmp_path):
    _ready(tmp_path)
    _run(_seed())
    got = _run(db.count_and_list_filtered([{"field": "ext_form", "value": "filled", "form_id": 999}]))
    assert got == [] or list(got) == []


def test_menu_button_only_with_forms(tmp_path):
    _ready(tmp_path)
    from handlers.admin_broadcasts import _filter_menu_kb, _FILTER_FIELD_LABELS, _PICKER_FIELDS

    def cbs(kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    assert "extff_start" not in cbs(_filter_menu_kb([]))
    assert "extff_start" in cbs(_filter_menu_kb([], show_ext_form=True))
    assert _FILTER_FIELD_LABELS["ext_form"] == "Внешняя форма"
    assert "ext_form" not in _PICKER_FIELDS


def test_seam_pick_and_cancel(tmp_path):
    _ready(tmp_path)
    from handlers import admin_broadcast_ext_form_filter as ff

    f1, _ = _run(_seed())
    state = _fresh_state(ADMIN_ID)
    _run(state.update_data(filters=[]))
    cb = FakeCallback("extff_start", ADMIN_ID)
    _run(ff.extff_start(cb, state))
    assert any(b.callback_data == f"extff_form:{f1}" for r in cb.message.markup.inline_keyboard for b in r)
    cb = FakeCallback(f"extff_form:{f1}", ADMIN_ID)
    _run(ff.extff_form(cb, state))
    cb = FakeCallback(f"extff_pick:{f1}:filled", ADMIN_ID)
    _run(ff.extff_pick(cb, state))
    flt = _run(state.get_data())["filters"]
    assert flt == [{"field": "ext_form", "value": "filled", "form_id": f1, "label": "Анкета А: заполнил"}]
    cb = FakeCallback("extff_cancel", ADMIN_ID)
    _run(ff.extff_cancel(cb, state))
    assert _run(state.get_data())["filters"] == flt
