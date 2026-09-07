"""Phase 28 Plan 10 (SU-11, СкиллАп 5): пресет события «СкиллАп» + корневой aiogram-free
`reg_presets.py`.

Задача 1: `REG_PRESETS`/`apply_reg_preset` переехали в `reg_presets.py` дословно — старые
четыре пресета не изменились ни на байт, старый путь импорта (`handlers.reg_schema`)
продолжает работать, модуль не тянет aiogram, bulk-writer умеет писать произвольные ключи
реестра из необязательного поля пресета `"settings"`.

Задача 2 (этот коммит): пресет «skillup» включает нужные вопросы + скоринг, пишет
произвольные ключи реестра ПОСЛЕ тумблеров, не трогает скоринговые множества, `event_type` —
четыре варианта, бот и веб дают одинаковое состояние `bot_settings`.

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


SKILLUP_ON = [
    "reg_q_phone", "reg_q_vk", "reg_q_city", "reg_q_education", "reg_q_course",
    "reg_q_university", "reg_q_study_field", "reg_q_stack", "reg_q_experience",
    "reg_q_readiness", "reg_q_goal", "reg_q_source", "reg_q_resume", "reg_q_resume_link",
    "reg_q_mini_projects", "reg_q_mini_portfolio", "reg_q_mini_direction", "reg_q_case_optin",
    "reg_scoring_enabled",
]

SKILLUP_SETTINGS = {
    "reg_resume_mode": "fork",
    "edu_conditional": "on",
    "reg_skip_source_for_referred": "on",
    "reg_offer_ref_link": "on",
    "nudge_enabled": "on",
    "nudge_after_minutes": "1440",
    "reg_multi_max_stack": "5",
    "reg_multi_max_goal": "2",
    "score_course_from": "3",
    "score_stack_from": "2",
}

SCORING_SET_KEYS = {
    "score_it_fields", "score_senior_statuses", "score_readiness_counts",
    "score_experience_counts",
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


# ── Задача 2: пресет «skillup» ─────────────────────────────────────────────────────────────

def test_preset_turns_on_expected_questions(tmp_path):
    _ready(tmp_path)

    async def _run():
        await reg_presets.apply_reg_preset("skillup")
        for key in SKILLUP_ON:
            assert await get_setting(key) == "on", key

    asyncio.run(_run())


def test_preset_turns_off_everything_else(tmp_path):
    _ready(tmp_path)

    async def _run():
        await reg_presets.apply_reg_preset("skillup")
        on_set = set(SKILLUP_ON)
        for key in reg_presets.REG_DEFAULTS:
            if key in on_set:
                continue
            assert await get_setting(key) == "off", key
        assert await get_setting("payment_enabled") == "off"

    asyncio.run(_run())


def test_preset_writes_extra_settings(tmp_path):
    _ready(tmp_path)

    async def _run():
        await reg_presets.apply_reg_preset("skillup")
        for key, value in SKILLUP_SETTINGS.items():
            assert await get_setting(key) == value, key

    asyncio.run(_run())


def test_preset_does_not_touch_scoring_sets(tmp_path):
    _ready(tmp_path)
    on_list = set(reg_presets.REG_PRESETS["skillup"]["on"])
    settings_block = set(reg_presets.REG_PRESETS["skillup"].get("settings", {}))
    assert not (SCORING_SET_KEYS & on_list)
    assert not (SCORING_SET_KEYS & settings_block)

    async def _run():
        from database.db import set_setting
        await set_setting("score_it_fields", "Бэкенд\nФронтенд")
        await reg_presets.apply_reg_preset("skillup")
        assert await get_setting("score_it_fields") == "Бэкенд\nФронтенд"

    asyncio.run(_run())


def test_event_type_has_four_options():
    from settings_schema import SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["event_type"]["options"] == [
        "forum", "conference", "custom", "skillup",
    ]


def test_web_and_bot_apply_same_preset(tmp_path):
    import settings_ops
    from handlers.admin_reg_config import _apply_event_preset

    async def _snapshot():
        keys = list(reg_presets.REG_DEFAULTS) + ["payment_enabled"] + list(SKILLUP_SETTINGS)
        return {key: await get_setting(key) for key in keys}

    config.DB_PATH = str(tmp_path / "web.db")
    asyncio.run(db.init_db())
    asyncio.run(settings_ops.apply_event_type_preset("skillup"))
    web_snapshot = asyncio.run(_snapshot())

    config.DB_PATH = str(tmp_path / "bot.db")
    asyncio.run(db.init_db())
    asyncio.run(_apply_event_preset("skillup"))
    bot_snapshot = asyncio.run(_snapshot())

    assert web_snapshot == bot_snapshot


def test_settings_ops_still_aiogram_free():
    loaded = _loaded_aiogram("import settings_ops")
    assert loaded == [], f"settings_ops потянул aiogram: {loaded}"
