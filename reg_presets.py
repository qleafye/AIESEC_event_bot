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

Зависимости — ТОЛЬКО `database.db` (`set_setting`) и `reg_engine` (`REG_DEFAULTS`), ни одного
импорта `aiogram`/`handlers.*` (сторож tests/test_skillup_preset_28.py::
test_reg_presets_module_is_aiogram_free).
"""
from database.db import set_setting
from reg_engine import REG_DEFAULTS

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
}
# Задача 2 (28-10, SU-11) добавит сюда пятый пресет "skillup" с блоком "settings" — bulk-writer
# ниже уже поддерживает это поле, само наполнение приходит следующим коммитом.


async def apply_reg_preset(preset_key: str) -> None:
    """Bulk-write reg_q_* + payment_enabled for the chosen preset (byte-for-byte body of the
    former `handlers.admin_reg_config._apply_event_preset`), then (Phase 28, 28-10) any
    arbitrary registry keys listed in preset["settings"]. Every REG_DEFAULTS key (every
    toggle-type key in SETTINGS_SCHEMA, not only reg_q_*) is set explicitly — on if it's in
    the preset's "on" list, off otherwise — so the result is deterministic regardless of
    prior per-question overrides (T-28-10-03). Order matters: question toggles first, the
    "settings" block second — a preset value in "settings" must win over whatever the
    REG_DEFAULTS pass just wrote for that same key (not applicable to today's presets, since
    "settings" keys are never toggle-typed, but keeps the contract explicit)."""
    preset = REG_PRESETS[preset_key]
    on_set = set(preset["on"])
    for key in REG_DEFAULTS:
        await set_setting(key, "on" if key in on_set else "off")
    await set_setting("payment_enabled", preset["payment_enabled"])
    for key, value in preset.get("settings", {}).items():
        await set_setting(key, value)
