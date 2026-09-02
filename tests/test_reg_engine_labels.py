"""Phase 21 (gap closure, FORM-SYNC-01): сторож паритета подписей шагов анкеты.

Подпись шага в мастере Mini App и в профиле — та же строка, которой бот подписывает этот
шаг в админке (`handlers/admin_reg_config.py`: `REG_LABELS.get(setting_key, setting_key)`).
Ключ подписи в `reg_labels.REG_LABELS` — это `setting_key` из тройки `REG_FLOW`
(`reg_q_education`, `reg_q_lc`, ...), а НЕ `reg_q_{step_key}`: для девяти шагов
(`education_status`, `local_committee`, `work_status`, `missing_skills`, `attendance_format`,
`needs_certificate`, `english_level`, `food_pref`, `payment_plan_date`) эти два ключа
расходятся, и движок, искавший подпись по префиксу, откатывался на сырой `step_key`
(`deferred-items.md` § 21-11). Единственный источник подписи теперь — `reg_engine.label_for`,
локальной таблицы алиасов в `miniapp/routers/profile.py` больше нет.

БД поднимается как в `tests/test_reg_engine_parity.py::_ready` (pytest-asyncio недоступен —
async через `asyncio.run()`).
"""
from __future__ import annotations

import asyncio

import pytest

import reg_engine
from config import config
from database.db import init_db
from reg_labels import REG_LABELS

_DRIFTED = [
    ("education_status", "reg_q_education"),
    ("local_committee", "reg_q_lc"),
    ("work_status", "reg_q_work"),
    ("missing_skills", "reg_q_skills"),
    ("attendance_format", "reg_q_attendance"),
    ("needs_certificate", "reg_q_certificate"),
    ("english_level", "reg_q_english"),
    ("food_pref", "reg_q_food"),
    ("payment_plan_date", "reg_q_payment_date"),
]


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reg_engine_labels.db")
    asyncio.run(init_db())


def test_every_reg_flow_step_label_matches_bot_dictionary(tmp_path):
    _ready(tmp_path)
    for step_key, setting_key, _t in reg_engine.REG_FLOW:
        spec = asyncio.run(reg_engine.step_spec(step_key))
        assert spec["label"] == REG_LABELS[setting_key], step_key
        assert spec["label"] != step_key, step_key


@pytest.mark.parametrize("step_key,label_key", _DRIFTED)
def test_nine_drifted_steps_have_human_labels(step_key, label_key):
    assert reg_engine.label_for(step_key) == REG_LABELS[label_key]
    assert reg_engine.label_for(step_key) != step_key


def test_label_key_for_maps_step_to_reg_flow_setting_key():
    assert reg_engine.label_key_for("education_status") == "reg_q_education"
    assert reg_engine.label_key_for("age") == "reg_q_age"
    # Не в REG_FLOW (ФИО спрашивается до движка шагов) — префиксный фоллбэк.
    assert reg_engine.label_key_for("full_name") == "reg_q_full_name"
    assert reg_engine.SETTING_KEY_BY_STEP == {s: k for s, k, _ in reg_engine.REG_FLOW}


def test_form_spec_never_falls_back_to_step_key(tmp_path):
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.form_spec({}, "full"))
    assert spec["steps"]
    for step in spec["steps"]:
        assert step["label"] != step["key"], step["key"]


def test_profile_has_no_alias_table_and_uses_engine_keys():
    import miniapp.routers.profile as profile

    assert not hasattr(profile, "_LABEL_KEY_ALIASES")
    expected = {reg_engine.label_key_for(s) for s in reg_engine.STEP_TO_COLUMN} - {"reg_q_full_name"}
    assert set(profile._profile_columns()) == expected
    assert "reg_q_education" in profile._profile_columns()
