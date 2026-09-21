"""Phase 28 (28-10, SU-11): пресеты события — корневой aiogram-free реестр + единый
bulk-writer, общий для бота (`handlers/admin_reg_config._apply_event_preset`) и веб-слоя
(`settings_ops.apply_event_type_preset`).

REG_PRESETS переехал сюда ДОСЛОВНО из `handlers/reg_schema.py` (Phase 13/5/7) — байт-в-байт,
без единой правки значений четырёх существующих пресетов (forum/conf/party/short).
`handlers/reg_schema.py` реэкспортирует то же имя (`from reg_presets import REG_PRESETS`),
поэтому семь существующих мест импорта (`handlers/admin.py`, `admin_reg_config.py`,
`registration.py` и соседи) продолжают работать без правок.

Причина выноса — `settings_ops.py` (импортируется веб-процессом Mini App, FastAPI) не имеет
права тянуть `aiogram`/`handlers.*` (та же причина, что у `reg_engine.py`/`reg_labels.py`), а
веб-путь применения пресета «СкиллАп» обязан звать ТОТ ЖЕ bulk-writer, что кнопка в боте
(T-28-10-01/03: один детерминированный писатель настроек — не две копии правила).

Зависимости — ТОЛЬКО `database.db` (`set_setting`) и `reg_engine` (`REG_DEFAULTS`,
`MODULE_SWITCH_TOGGLES`), ни одного
импорта `aiogram`/`handlers.*` (сторож tests/test_skillup_preset_28.py::
test_reg_presets_module_is_aiogram_free).
"""
from database.db import set_setting
from reg_engine import REG_DEFAULTS, MODULE_SWITCH_TOGGLES

# --- Event-type presets (admin one-tap bulk toggle) ---
# A preset lists the reg_q_* keys to turn ON (everything else in REG_DEFAULTS is turned
# OFF) plus the payment module flag. Applying a preset is an explicit admin action that
# writes the same settings the per-question toggles write — it changes NOTHING until
# tapped, so live bots keep their current flow. Extra questions can still be flipped on
# individually afterwards (see REG_CATEGORIES «➕ Экстра»).
#
# Phase 28 (28-10, SU-11): optional "settings" key — {registry_key: value}, written by
# apply_reg_preset() AFTER the reg_q_*/payment_enabled toggles below, via the same
# set_setting. Only "skillup" carries it; the four presets below have no "settings" key and
# are therefore untouched byte-for-byte by this extension
# (tests/test_skillup_preset_28.py::test_existing_presets_byte_identical, D-06).
REG_PRESETS = {
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
        # Phase 5 (D-07): NO "payment_enabled" key here — the party preset must never touch
        # the payment module (party pricing is D-16/D-17 in plan 05-05, a separate concern).
        # setting_key spellings (not step_keys) — the shared confirm dialog in admin.py
        # renders REG_LABELS.get(k, k) for k in preset["on"], and REG_LABELS is keyed by
        # reg_q_*; matches the "forum"/"conf" entries above.
        "on": [
            "reg_q_age", "reg_q_phone", "reg_q_alumni_status", "reg_q_vk", "reg_q_city",
            "reg_q_allergies", "reg_q_food",
        ],
    },
    "short": {
        "label": "⚡ Акция: 6 вопросов",
        # Phase 7 (D-07 pattern): NO "payment_enabled" key here either — the promo preset
        # must never touch the payment module, same reasoning as the party preset above.
        # preset_apply already tolerates its absence via preset.get("payment_enabled").
        # Five setting_keys below + ФИО = six questions: ФИО is asked unconditionally by
        # _ask_full_name and is NOT a REG_FLOW key, so it can never appear in an "on" list —
        # it is not missing, it just isn't a toggle.
        "on": [
            "reg_q_phone", "reg_q_vk", "reg_q_city", "reg_q_education", "reg_q_course",
        ],
    },
    # Phase 28 (28-10, SU-11): один пресет настраивает весь форум СкиллАп 5 — восемь новых
    # вопросов анкеты, скоринг, развилку резюме, догонялку и рефералку — тем, что менеджеру
    # иначе пришлось бы щёлкать тремя десятками отдельных тумблеров и списков (D-01).
    "skillup": {
        "label": "🎓 Форум СкиллАп",
        "payment_enabled": "off",
        "on": [
            "reg_q_phone", "reg_q_vk", "reg_q_city", "reg_q_education", "reg_q_course",
            "reg_q_university", "reg_q_study_field", "reg_q_stack", "reg_q_experience",
            "reg_q_readiness", "reg_q_goal", "reg_q_source", "reg_q_resume",
            "reg_q_resume_link", "reg_q_mini_projects", "reg_q_mini_portfolio",
            "reg_q_mini_direction", "reg_q_case_optin", "reg_scoring_enabled",
        ],
        # Не reg_q_*/toggle ключи — REG_DEFAULTS их не трогает (не toggle-типа), поэтому
        # пресет пишет их явным вторым проходом. Множества скоринга (score_it_fields и
        # соседи) сюда НЕ входят намеренно: их значения зависят от списков вариантов
        # конкретного события — менеджер отмечает галочками на экране «🧮 Правила балла»
        # (план 28-08), пресет угадывать за него не должен (см. текст
        # skillup_preset_confirm_text в settings_schema.py, который это же говорит
        # менеджеру прямо).
        "settings": {
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
        },
    },
}


async def apply_reg_preset(preset_key: str) -> None:
    """Bulk-write reg_q_* + payment_enabled for the chosen preset (byte-for-byte body of the
    former `handlers.admin_reg_config._apply_event_preset`), then (Phase 28, 28-10) any
    arbitrary registry keys listed in preset["settings"]. Every REG_DEFAULTS key (every
    reg_q_* toggle — NOT module switches, see `reg_engine.MODULE_SWITCH_TOGGLES`) is set
    explicitly — on if it's in the preset's "on" list, off otherwise — so the result is
    deterministic regardless of prior per-question overrides (T-28-10-03). Order matters:
    question toggles first, the "settings" block second — a preset value in "settings" must
    win over whatever the REG_DEFAULTS pass just wrote for that same key (not applicable to
    today's presets, since "settings" keys are never toggle-typed, but keeps the contract
    explicit).

    Quick 260921 (found during phase 31 execution): `reg_scoring_enabled`/`reject_rules_enabled`
    are toggle-typed but excluded from REG_DEFAULTS on purpose — a preset that doesn't mention
    them must leave the manager's current choice untouched, not silently force it off. Only
    the "skillup" preset opts a module switch IN today (`reg_scoring_enabled` in its "on"
    list) — honoured below by writing "on" for exactly the module switches a preset lists,
    never "off" for the ones it omits."""
    preset = REG_PRESETS[preset_key]
    on_set = set(preset["on"])
    for key in REG_DEFAULTS:
        await set_setting(key, "on" if key in on_set else "off")
    for key in MODULE_SWITCH_TOGGLES:
        if key in on_set:
            await set_setting(key, "on")
    await set_setting("payment_enabled", preset["payment_enabled"])
    for key, value in preset.get("settings", {}).items():
        await set_setting(key, value)
