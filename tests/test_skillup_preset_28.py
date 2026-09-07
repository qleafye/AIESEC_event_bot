"""Phase 28 Plan 10 (SU-11, СкиллАп 5): пресет события «СкиллАп» + корневой aiogram-free
`reg_presets.py`.

Задача 1 (этот коммит): `REG_PRESETS`/`apply_reg_preset` переехали в `reg_presets.py`
дословно — старые четыре пресета не изменились ни на байт, старый путь импорта
(`handlers.reg_schema`) продолжает работать, модуль не тянет aiogram, bulk-writer умеет
писать произвольные ключи реестра из необязательного поля пресета `"settings"` (пока ни один
реальный пресет им не пользуется — проверяется синтетическим пресетом).

Задача 2 (следующий коммит, дописывает этот же файл): пресет «skillup».

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной
БД — тот же приём, что `tests/test_skillup_core_28.py::_ready(tmp_path)`.
"""
import asyncio

import reg_presets
from config import config
from database import db
from database.db import get_setting

from tests.test_miniapp_labels_drift import _loaded_aiogram

EXISTING_PRESETS_SNAPSHOT = {
    "forum": {
        "label": "🏛 Форум (Юлид)",
        "payment_enabled": "off",
        "on": [
            "reg_q_age", "reg_q_vk", "reg_q_source", "reg_q_education",
            "reg_q_university", "reg_q_course", "reg_q_study_field", "reg_q_work",
            "reg_q_work_sphere", "reg_q_skills", "reg_q_expectations",
        ],
    },
    "conf": {
        "label": "🎤 Конференция (RusCo)",
        "payment_enabled": "on",
        "on": [
            "reg_q_age", "reg_q_vk", "reg_q_phone", "reg_q_lc", "reg_q_work",
            "reg_q_department", "reg_q_aiesec_role", "reg_q_english", "reg_q_allergies",
            "reg_q_food", "reg_q_arrival", "reg_q_bed_sharing", "reg_q_bed_partner",
            "reg_q_transport", "reg_q_payment_date",
            "reg_q_cc_shop", "reg_q_exp_organizers", "reg_q_volunteer",
        ],
    },
    "party": {
        "label": "🎉 Party",
        "on": [
            "reg_q_age", "reg_q_phone", "reg_q_alumni_status", "reg_q_vk", "reg_q_city",
            "reg_q_allergies", "reg_q_food",
        ],
    },
    "short": {
        "label": "⚡ Акция: 6 вопросов",
        "on": [
            "reg_q_phone", "reg_q_vk", "reg_q_city", "reg_q_education", "reg_q_course",
        ],
    },
}


def _ready(tmp_path, name="test_skillup_preset_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── Задача 1: перенос модуля ──────────────────────────────────────────────────────────────

def test_reg_presets_module_is_aiogram_free():
    loaded = _loaded_aiogram("import reg_presets")
    assert loaded == [], f"reg_presets потянул aiogram: {loaded}"


def test_reg_schema_reexports_presets():
    from handlers import reg_schema
    assert reg_schema.REG_PRESETS is reg_presets.REG_PRESETS


def test_existing_presets_byte_identical():
    for key, expected in EXISTING_PRESETS_SNAPSHOT.items():
        actual = reg_presets.REG_PRESETS[key]
        assert actual["label"] == expected["label"], key
        assert actual["on"] == expected["on"], key
        assert actual.get("payment_enabled") == expected.get("payment_enabled"), key
        assert "settings" not in actual, f"{key}: пресет неожиданно получил блок settings"


def test_apply_preset_is_deterministic(tmp_path):
    _ready(tmp_path)

    async def _run():
        # Прежде включаем всё подряд, чтобы доказать, что пресет явно выключит лишнее.
        from database.db import set_setting
        for key in reg_presets.REG_DEFAULTS:
            await set_setting(key, "on")
        await reg_presets.apply_reg_preset("forum")
        on_set = set(reg_presets.REG_PRESETS["forum"]["on"])
        for key in reg_presets.REG_DEFAULTS:
            expected = "on" if key in on_set else "off"
            assert await get_setting(key) == expected, key
        assert await get_setting("payment_enabled") == "off"

    asyncio.run(_run())


def test_settings_block_written_after_toggles(monkeypatch):
    """Bulk-writer поддерживает необязательное поле пресета "settings" (Задача 1) — ни один
    из четырёх реальных пресетов им ещё не пользуется (Задача 2 добавит «skillup»), поэтому
    порядок проверяется синтетическим пресетом, вставленным на время теста."""
    calls = []

    async def fake_set_setting(key, value):
        calls.append(key)

    fake_preset = {
        "label": "Тест",
        "payment_enabled": "off",
        "on": ["reg_q_age"],
        "settings": {"reg_resume_mode": "fork", "nudge_after_minutes": "1440"},
    }
    monkeypatch.setitem(reg_presets.REG_PRESETS, "__test_synthetic__", fake_preset)
    monkeypatch.setattr(reg_presets, "set_setting", fake_set_setting)
    asyncio.run(reg_presets.apply_reg_preset("__test_synthetic__"))

    toggle_and_payment = set(reg_presets.REG_DEFAULTS) | {"payment_enabled"}
    last_toggle_index = max(i for i, k in enumerate(calls) if k in toggle_and_payment)
    settings_keys = list(fake_preset["settings"])
    first_settings_index = min(calls.index(k) for k in settings_keys)
    assert first_settings_index > last_toggle_index
