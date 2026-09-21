"""Quick 260921 (найдено при исполнении фазы 31): сторож для бага «пресет типа события
молча выключает модуль-рубильники».

`reg_engine.REG_DEFAULTS` раньше собирался по одному критерию — `type == "toggle"` — и
случайно подхватывал ДВА модуль-рубильника (`reg_scoring_enabled` фазы 28, `reject_rules_enabled`
фазы 31) наравне с полем-вопросами `reg_q_*`. `reg_presets.apply_reg_preset` детерминированно
сметает КАЖДЫЙ ключ REG_DEFAULTS на каждый тап пресета «🏛 Форум»/«🎤 Конференция» — пишет "off"
всему, что не в списке "on" пресета. Для вопросов анкеты это ровно то, что нужно; для
модуль-рубильника это тихо выключает скоринг/автоотказ без единого сообщения менеджеру.

Фикс — REG_DEFAULTS сузился до `type == "toggle" and group == "reg_questions"`
(тот же дискриминатор, что `settings_ops.reg_question_track_base`), а `reg_engine.
MODULE_SWITCH_TOGGLES` — явный allowlist модуль-рубильников, который `apply_reg_preset`
проверяет отдельно (пишет "on", если пресет явно перечислил ключ в своём "on", никогда не
пишет "off" за отсутствие).

Async — через `asyncio.run()`, фикстура временной БД — тот же приём, что
`tests/test_skillup_preset_28.py::_ready`."""
import asyncio

import reg_presets
from config import config
from database import db
from database.db import get_setting, set_setting
from reg_engine import REG_DEFAULTS, MODULE_SWITCH_TOGGLES
from settings_schema import SETTINGS_SCHEMA

from handlers.reg_schema import _apply_party_preset, _apply_short_preset


def _ready(tmp_path, name="test_preset_module_switches_260921.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── (a) presets never turn a module switch off ─────────────────────────────────────────────

def test_event_type_presets_never_turn_off_module_switches(tmp_path):
    """Каждый пресет, который реально проходит через `apply_reg_preset` (`forum`/`conf`/
    `skillup` — `party`/`short` в проде маршрутизируются мимо, см. следующий тест) —
    рубильники были ON до тапа, остаются ON после. Плюс контрольный вопрос анкеты, которого
    нет в списке "on" пресета, гарантированно OFF — доказывает, что смётка вопросов ещё
    работает, а не просто ничего не пишет.

    `party`/`short` не входят сюда намеренно: у них нет ключа "payment_enabled" (D-07 —
    пресет party/short не должен трогать модуль оплаты вовсе), поэтому прямой вызов
    `apply_reg_preset("party"|"short")` падает на KeyError ДО этого фикса и после него
    одинаково — `admin_reg_config.preset_confirm` поэтому и не зовёт для них эту функцию, а
    маршрутизирует в изолированные `_apply_party_preset`/`_apply_short_preset`."""
    for preset_key, preset in reg_presets.REG_PRESETS.items():
        if preset_key in ("party", "short"):
            continue
        _ready(tmp_path, name=f"test_preset_{preset_key}_260921.db")

        async def _run():
            await set_setting("reject_rules_enabled", "on")
            await set_setting("reg_scoring_enabled", "on")
            await reg_presets.apply_reg_preset(preset_key)
            assert await get_setting("reject_rules_enabled") == "on", preset_key
            assert await get_setting("reg_scoring_enabled") == "on", preset_key

            on_set = set(preset["on"])
            off_candidates = [k for k in REG_DEFAULTS if k not in on_set]
            if off_candidates:
                assert await get_setting(off_candidates[0]) == "off", (
                    preset_key, off_candidates[0]
                )

        asyncio.run(_run())


def test_party_and_short_track_presets_never_touch_module_switches(tmp_path):
    """`_apply_party_preset`/`_apply_short_preset` пишут ТОЛЬКО `__party`/`__short`-суффиксные
    вопросные ключи (D-07/SHORT-03) — отдельный путь от `apply_reg_preset`, но тот же риск,
    если бы он когда-нибудь начал перебирать REG_DEFAULTS вместо REG_FLOW."""
    _ready(tmp_path, name="test_preset_party_track_260921.db")

    async def _run():
        await set_setting("reject_rules_enabled", "on")
        await set_setting("reg_scoring_enabled", "on")
        await _apply_party_preset(admin_id=None)
        await _apply_short_preset(admin_id=None)
        assert await get_setting("reject_rules_enabled") == "on"
        assert await get_setting("reg_scoring_enabled") == "on"

    asyncio.run(_run())


# ── (b) structural guard: every toggle is either a question or an allowlisted module switch ─

def test_every_toggle_is_a_question_or_an_allowlisted_module_switch():
    toggle_keys = {k for k, v in SETTINGS_SCHEMA.items() if v["type"] == "toggle"}
    question_toggles = {
        k for k in toggle_keys if SETTINGS_SCHEMA[k].get("group") == "reg_questions"
    }
    unaccounted = toggle_keys - question_toggles - MODULE_SWITCH_TOGGLES
    assert not unaccounted, (
        f"новый тумблер модуля: {sorted(unaccounted)} — либо сделайте его enum on/off, как "
        "payment_enabled, либо добавьте в MODULE_SWITCH_TOGGLES — иначе пресет анкеты будет "
        "молча его выключать"
    )


# ── (c) REG_DEFAULTS contains none of the module switches ──────────────────────────────────

def test_reg_defaults_excludes_module_switches():
    assert not (set(REG_DEFAULTS) & MODULE_SWITCH_TOGGLES)


# ── (d) question-key identity in REG_DEFAULTS is unchanged apart from the two removed keys ──

def test_reg_defaults_identity_unchanged_apart_from_module_switches():
    """До фикса REG_DEFAULTS = {k: v["default"] for k, v in SETTINGS_SCHEMA.items() if
    v["type"] == "toggle"}. После фикса — тот же набор МИНУС MODULE_SWITCH_TOGGLES (проверено
    группой "reg_questions" — единственный дискриминатор, отделяющий вопросы анкеты).
    Ни один reg_q_* ключ не пропал и не появился лишний."""
    pre_fix_all_toggles = {
        k: v["default"] for k, v in SETTINGS_SCHEMA.items() if v["type"] == "toggle"
    }
    expected = {
        k: v for k, v in pre_fix_all_toggles.items() if k not in MODULE_SWITCH_TOGGLES
    }
    assert REG_DEFAULTS == expected
    assert set(pre_fix_all_toggles) - set(REG_DEFAULTS) == MODULE_SWITCH_TOGGLES
