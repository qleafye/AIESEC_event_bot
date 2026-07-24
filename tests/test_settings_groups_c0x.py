"""Quick 260724-c0x tests: settings landing screen no longer dumps ~40 fields inline;
fields are grouped into per-group sub-screens (settings_group:{token}) with status flags
(«задано»/«не задано»/«по умолчанию») instead of raw values. Existing edit/photo/file/toggle
callbacks stay byte-identical — only render/navigation changed.

pytest-asyncio is unavailable in this env (see tests/test_db_phase5.py) — every async
helper is driven via asyncio.run() and config.DB_PATH points at a tmp_path file.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod


ADMIN_ID = 900002


def _admin_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_groups_c0x.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ── Task 1: coverage — every SETTINGS_FIELDS key lands in exactly one group (or leftover) ──

def test_settings_groups_cover_every_field_key():
    grouped_keys = [k for _, __, keys in admin_mod.SETTINGS_GROUPS for k in keys]
    all_keys = [k for k, _, _ in admin_mod.SETTINGS_FIELDS]
    leftover = admin_mod._settings_group_keys("misc")

    # No duplicates within declared groups.
    assert len(grouped_keys) == len(set(grouped_keys))

    for key in all_keys:
        assert key in grouped_keys or key in leftover, f"{key} missing from all groups"

    # Nothing invented: every grouped/leftover key must be a real SETTINGS_FIELDS key.
    for key in grouped_keys + leftover:
        assert key in all_keys


# ── Task 1: landing no longer dumps values inline ───────────────────────────────────

def test_landing_text_has_no_inline_value_dump(tmp_path):
    _admin_ready(tmp_path)
    long_value = "x" * 200
    asyncio.run(db.set_setting("start_text", long_value))

    text = asyncio.run(admin_mod.render_settings_text())

    assert ("x" * 61) not in text
    assert "…" not in text


def test_landing_keyboard_emits_group_nav_not_per_field(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_settings_keyboard())
    flat = _flat_callback_data(kb)

    assert any(cd.startswith("settings_group:") for cd in flat)
    assert not any(cd and cd.startswith("settings_edit:") for cd in flat)
    assert not any(cd and cd.startswith("settings_photo:") for cd in flat)
    assert not any(cd and cd.startswith("settings_file:") for cd in flat)

    # Prior toggle/back callbacks must still be present, untouched.
    assert "toggle_payment_enabled" in flat
    assert "settings_toggle_reg" in flat
    assert "settings_back" in flat


# ── Task 2: per-group sub-screen — flags, collapse, callback integrity ─────────────

def test_group_pay_shows_configured_and_unconfigured_flags(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("payment_options", "Полный билет|5000"))

    text = asyncio.run(admin_mod.render_settings_group_text("pay"))
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("pay"))
    flat = _flat_callback_data(kb)

    assert "✏️ задано" in text
    assert "не задано" in text
    assert "settings_edit:payment_options" in flat
    assert "admin_settings" in flat  # back button reuses existing landing handler


def test_group_event_contains_photo_and_file_callbacks(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("event"))
    flat = _flat_callback_data(kb)

    assert any(cd.startswith("settings_photo:") for cd in flat)
    assert any(cd.startswith("settings_file:") for cd in flat)
    assert any(cd.startswith("settings_edit:") for cd in flat)


def test_group_keyboard_collapses_unconfigured_under_noop_header(tmp_path):
    _admin_ready(tmp_path)
    # pay group has 7 keys, none set -> all unconfigured -> noop header must appear.
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("pay"))
    flat = _flat_callback_data(kb)
    assert "settings_group_noop" in flat


def test_show_settings_group_handler_renders_subscreen(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_group:pay")
    asyncio.run(admin_mod.show_settings_group(cb))

    assert cb.message.edit_calls == 1
    assert "Оплата" in cb.message.text
    flat = _flat_callback_data(cb.message.markup)
    assert "settings_edit:payment_options" in flat


def test_show_settings_group_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_group:pay", user_id=1)
    asyncio.run(admin_mod.show_settings_group(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True


def test_settings_group_noop_just_answers(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_group_noop")
    asyncio.run(admin_mod.settings_group_noop(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers


# ── Phase 6 plan 06-01 (REG-01/02/03): settings_schema registry — event-group pilot ──
# The five tests below drive the registry module into existence (RED first — settings_schema
# does not exist yet, this file will fail to collect with ModuleNotFoundError). See
# .planning/phases/06-settings-schema-registry/06-01-PLAN.md / 06-CONTEXT.md (D-15/D-16/D-17).

from settings_schema import SETTINGS_SCHEMA, _parse_setting, get_setting_typed  # noqa: E402


def test_parse_setting_text_passthrough():
    # Non-empty raw passes through unchanged.
    assert _parse_setting("start_text", "Привет, форум!") == "Привет, форум!"
    # None raw resolves to the registry default (None for event text keys).
    assert _parse_setting("start_text", None) == SETTINGS_SCHEMA["start_text"]["default"]


def test_parse_setting_enum_falsy_to_default():
    default = SETTINGS_SCHEMA["event_type"]["default"]
    # Non-empty raw passes through unchanged.
    assert _parse_setting("event_type", "forum") == "forum"
    # BOTH None AND empty-string resolve to default — byte-for-byte with the live
    # `get_setting(k) or "<default>"` idiom (D-15/CRITICAL enum contract).
    assert _parse_setting("event_type", None) == default
    assert _parse_setting("event_type", "") == default


def test_parse_setting_photo_file_passthrough():
    # photo/file entries are passthrough — the raw file_id is returned unchanged (D-10).
    assert _parse_setting("program", "AgACAgIAAxkBAAI_fake_photo_id") == "AgACAgIAAxkBAAI_fake_photo_id"
    assert _parse_setting("reg_bonus", "BQACAgIAAxkBAAI_fake_file_id") == "BQACAgIAAxkBAAI_fake_file_id"
    # None raw resolves to the registry default (None) for both.
    assert _parse_setting("program", None) == SETTINGS_SCHEMA["program"]["default"]
    assert _parse_setting("reg_bonus", None) == SETTINGS_SCHEMA["reg_bonus"]["default"]


def test_registry_coverage_event():
    # "toggles" added 06-04 (D-12): the feature-switch enum group.
    allowed_groups = {"event", "reg", "reg_questions", "pay", "party", "consent", "toggles", "misc"}
    allowed_types = {"toggle", "int", "list", "date", "text", "enum", "photo", "file"}

    event_keys_seen = set()
    for key, entry in SETTINGS_SCHEMA.items():
        assert entry["group"] in allowed_groups, f"{key} has unknown group {entry['group']!r}"
        assert entry["type"] in allowed_types, f"{key} has unknown type {entry['type']!r}"
        # Registry default must parse cleanly through _parse_setting without raising.
        _parse_setting(key, entry["default"])
        if entry["group"] == "event":
            assert key not in event_keys_seen, f"{key} duplicated in event group"
            event_keys_seen.add(key)

    expected_event_keys = {
        "event_date", "event_time", "event_place_name", "event_place_address",
        "contact_person", "contact_vk", "contact_tg", "start_text", "event_name", "event_type",
    }
    assert expected_event_keys <= event_keys_seen


def test_event_render_snapshot(tmp_path):
    _admin_ready(tmp_path)

    text = asyncio.run(admin_mod.render_settings_group_text("event"))
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("event"))
    flat = _flat_callback_data(kb)

    # Byte-for-byte render invariant (D-16): the concrete event field labels must still
    # render exactly as before the registry became the source of this group.
    for label in [
        "🗓 Дата", "⌚ Время", "📍 Место", "📫 Адрес", "👤 Контакт",
        "🔵 VK", "🔹 TG", "💬 Приветствие", "🎪 Название меро", "🎭 Тип события",
    ]:
        assert label in text, f"missing event label: {label}"

    # settings_edit/photo/file callbacks stay byte-identical (D-14, no call-site rewrites).
    assert "settings_edit:event_date" in flat
    assert any(cd.startswith("settings_photo:") for cd in flat)
    assert any(cd.startswith("settings_file:") for cd in flat)
    # Fresh DB -> nothing configured yet -> the unconfigured collapse header must appear.
    assert "settings_group_noop" in flat
    # Back button unchanged.
    assert "admin_settings" in flat


# ── Phase 6 plan 06-02 (REG-01/REG-03): reg/pay/party/consent groups migrated into the
# registry. Parse-equivalence tests prove int/date/list types parse byte-for-byte identical
# to the pre-migration ad-hoc parse helpers (D-15); render-snapshot tests capture the CURRENT
# (pre-Task-2) group render/keyboard output so they fail if Task 2's registry-driven
# generation drifts from it (D-16).

def test_parse_equivalence_int():
    from services.reminders import _reminder_interval

    for raw in [None, "", "abc", "0", "-5", "900", "1800"]:
        assert _parse_setting("pending_reminder_interval", raw) == _reminder_interval(raw), (
            f"mismatch for raw={raw!r}"
        )
    # Registry default MUST be 1800 (matches services.reminders.DEFAULT_INTERVAL).
    assert SETTINGS_SCHEMA["pending_reminder_interval"]["default"] == 1800


def test_parse_equivalence_date():
    from services.scheduler import _parse_schedule_dt

    for raw in [None, "", "garbage", "15.08.2026 23:59"]:
        assert _parse_setting("payment_deadline", raw) == _parse_schedule_dt(raw), (
            f"mismatch for raw={raw!r}"
        )


def test_parse_equivalence_list():
    def _expected_options(raw):
        # splitlines/strip (registration.py::_get_options) extended with the `;` inline
        # separator (Telegram Enter=send trap convention already used elsewhere).
        if raw:
            items = [
                segment.strip()
                for line in raw.splitlines()
                for segment in line.split(";")
                if segment.strip()
            ]
            if items:
                return items
        return []

    for raw in [None, "", "a\nb\n\n c ", "a; b ;c"]:
        assert _parse_setting("source_options", raw) == _expected_options(raw), (
            f"mismatch for raw={raw!r}"
        )


def test_registry_coverage_all_text_groups():
    """Every reg/pay/party/consent key exists in SETTINGS_SCHEMA with the correct group/type
    (D-04 taxonomy), and its registry `default` parses cleanly through `_parse_setting`
    without raising. (Source-level "no key in both a literal tuple AND the registry" check is
    a grep-based Task 2 acceptance criterion, not re-implemented here.)"""
    expected = {
        "reg": {
            "source_options": "list", "reg_complete_text": "text", "approve_text": "text",
            "reject_text": "text", "pending_reminder_interval": "int", "city_options": "list",
            "study_field_options": "list", "goal_options": "list", "formats_options": "list",
            "university_options": "list",
        },
        "pay": {
            "payment_options": "list", "payment_requisites": "text",
            "payment_requisites_by_lc": "list", "payment_deadline": "date",
            "payment_reminder_text": "text", "payment_overdue_text": "text",
            "penalty_schedule": "list",
        },
        "party": {
            "party_closed_text": "text", "party_sheet_tab": "text",
            "approve_text__party": "text",
        },
        "consent": {
            "consent_button_text": "text", "consent_list": "list",
        },
    }
    for group, keys in expected.items():
        for key, expected_type in keys.items():
            assert key in SETTINGS_SCHEMA, f"{key} missing from SETTINGS_SCHEMA"
            entry = SETTINGS_SCHEMA[key]
            assert entry["group"] == group, f"{key} group {entry['group']!r} != {group!r}"
            assert entry["type"] == expected_type, f"{key} type {entry['type']!r} != {expected_type!r}"
            _parse_setting(key, entry.get("default"))  # must not raise


def test_render_snapshot_reg(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_mod.render_settings_group_text("reg"))
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("reg"))
    flat = _flat_callback_data(kb)

    expected_keys = [
        "source_options", "reg_complete_text", "approve_text", "reject_text",
        "pending_reminder_interval", "city_options", "study_field_options",
        "goal_options", "formats_options", "university_options",
    ]
    expected_labels = [
        "📢 Источники", "✅ После регистрации", "🎉 После одобрения", "🚫 При отклонении",
        "🕒 Тайминг батчей заявок", "🏙 Города (варианты)", "🎯 Направления обучения (варианты)",
        "🎯 Цель участия (варианты)", "📋 Форматы форума (варианты)", "🏫 Список ВУЗов",
    ]
    # Fresh DB -> every key unconfigured, no display-default fallback for this group.
    for label in expected_labels:
        assert f"{label}: <i>— не задано</i>" in text, f"missing/wrong flag for {label}"
    positions = [text.index(label) for label in expected_labels]
    assert positions == sorted(positions), "label order drifted"

    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "settings_group_noop" in flat
    assert "admin_settings" in flat


def test_render_snapshot_pay(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_mod.render_settings_group_text("pay"))
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("pay"))
    flat = _flat_callback_data(kb)

    expected_keys = [
        "payment_options", "payment_requisites", "payment_requisites_by_lc",
        "payment_deadline", "payment_reminder_text", "payment_overdue_text", "penalty_schedule",
    ]
    expected_labels = [
        "💳 Варианты оплаты", "💰 Реквизиты оплаты", "💳 Реквизиты по ЛК",
        "📅 Дедлайн оплаты", "⏰ Текст напоминания об оплате", "⌛ Текст «оплата просрочена»",
        "⚠️ Штрафы за отмену",
    ]
    for label in expected_labels:
        assert f"{label}: <i>— не задано</i>" in text, f"missing/wrong flag for {label}"
    positions = [text.index(label) for label in expected_labels]
    assert positions == sorted(positions), "label order drifted"

    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "settings_group_noop" in flat
    assert "admin_settings" in flat


def test_render_snapshot_party(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_mod.render_settings_group_text("party"))
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("party"))
    flat = _flat_callback_data(kb)

    # party_closed_text/party_sheet_tab carry a display default (_SETTINGS_DISPLAY_DEFAULTS
    # pre-migration -> registry `default` post-migration, T-06-06) -> «по умолчанию» flag.
    assert "🎉 Текст «вечеринка закрыта»: <i>по умолчанию</i>" in text
    assert "📄 Вкладка Google-таблицы (Party): <i>по умолчанию</i>" in text
    # approve_text__party has no display default -> plain unconfigured flag.
    assert "🎉 После одобрения (Party): <i>— не задано</i>" in text

    expected_keys = ["party_closed_text", "party_sheet_tab", "approve_text__party"]
    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "admin_settings" in flat


def test_render_snapshot_consent(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_mod.render_settings_group_text("consent"))
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("consent"))
    flat = _flat_callback_data(kb)

    assert "✅ Текст кнопки согласия: <i>— не задано</i>" in text
    assert "📋 Список согласий: <i>— не задано</i>" in text

    expected_keys = ["consent_button_text", "consent_list"]
    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "settings_group_noop" in flat
    assert "admin_settings" in flat


# ── Phase 6 plan 06-04 (REG-01/REG-02, D-06/D-12): toggle wave — reg_q_* toggles +
# feature-switch enums registered; REG_DEFAULTS becomes a computed re-export; the three
# duplicated reg_q on/off read-sites resolve their default through the registry. The
# 44-key REG_DEFAULTS oracle below is FROZEN (copied verbatim from the pre-migration
# handlers/registration.py:197-241 literal) so this test file is independent of the
# migrated derivation — it is the byte-for-byte parity oracle (T-06-12/T-06-13).

_FROZEN_REG_DEFAULTS_ORACLE = {
    "reg_q_age": "on",
    "reg_q_vk": "on",
    "reg_q_email": "off",
    "reg_q_phone": "off",
    "reg_q_city": "off",
    "reg_q_source": "on",
    "reg_q_lc": "off",
    "reg_q_position": "off",
    "reg_q_education": "on",
    "reg_q_university": "on",
    "reg_q_course": "on",
    "reg_q_study_field": "on",
    "reg_q_specialty": "off",
    "reg_q_work": "on",
    "reg_q_work_sphere": "on",
    "reg_q_skills": "on",
    "reg_q_expectations": "on",
    "reg_q_attendance": "off",
    "reg_q_informal_day": "off",
    "reg_q_comments": "off",
    "reg_q_department": "off",
    "reg_q_aiesec_role": "off",
    "reg_q_certificate": "off",
    "reg_q_alumni_status": "off",
    "reg_q_english": "off",
    "reg_q_allergies": "off",
    "reg_q_food": "off",
    "reg_q_arrival": "off",
    "reg_q_housing": "off",
    "reg_q_bed_sharing": "off",
    "reg_q_bed_partner": "off",
    "reg_q_transport": "off",
    "reg_q_payment_date": "off",
    "reg_q_cc_shop": "off",
    "reg_q_exp_organizers": "off",
    "reg_q_exp_content": "off",
    "reg_q_volunteer": "off",
    "reg_q_arrival_date": "off",
    "reg_q_birth_date": "off",
    "reg_q_goal": "off",
    "reg_q_formats": "off",
    "reg_q_ambassador": "off",
    "reg_q_resume": "off",
}

# NOTE (deviation, Rule 1): 06-04-PLAN.md's interfaces table labels this a "44-key" oracle,
# but handlers/registration.py:197-241's actual REG_DEFAULTS literal has 43 keys (verified by
# direct count of the source dict) — the plan's count label was off-by-one. This assertion
# pins the VERIFIED source count (43), not the plan's stated count, per the "byte-for-byte
# matches registration.py:197-241 exactly" acceptance criterion (source is the ground truth).
assert len(_FROZEN_REG_DEFAULTS_ORACLE) == 43  # sanity — must match the live table (source-verified)

# Feature-switch (enum) defaults verified byte-for-byte from the live call sites
# (06-04-PLAN.md interfaces table) — DO NOT guess, DO NOT edit without re-checking the
# call sites (admin.py/registration.py/payment.py/scheduler.py).
_FROZEN_ENUM_DEFAULTS_ORACLE = {
    "party_enabled": "off",
    "party_fork_question": "off",
    "reg_bonus_enabled": "off",
    "payment_enabled": "off",
    "consent_enabled": "off",
    "payment_reminders_enabled": "on",
    "edu_conditional": "on",
    "reg_show_progress": "off",
    "reg_university_mode": "text",
    "registration_mode": "short",
    "pending_notify_mode": "batched",
    "full_approval": "manual",
    "short_approval": "auto",
    "party_approval": "manual",
}


def test_toggle_parse_equivalence_all_keys():
    for key, default in _FROZEN_REG_DEFAULTS_ORACLE.items():
        for raw in [None, "on", "off", "", "garbage"]:
            expected = (raw == "on") if raw is not None else (default == "on")
            assert _parse_setting(key, raw) == expected, f"mismatch for {key} raw={raw!r}"


def test_reg_defaults_parity():
    from handlers.registration import REG_DEFAULTS

    assert REG_DEFAULTS == _FROZEN_REG_DEFAULTS_ORACLE


def test_toggle_keys_coverage():
    from handlers.registration import REG_DEFAULTS

    toggle_keys_in_schema = {k for k, v in SETTINGS_SCHEMA.items() if v["type"] == "toggle"}
    assert set(REG_DEFAULTS.keys()) <= toggle_keys_in_schema, (
        "every REG_DEFAULTS key must exist in SETTINGS_SCHEMA with type toggle"
    )
    assert toggle_keys_in_schema <= set(REG_DEFAULTS.keys()), (
        "every SETTINGS_SCHEMA type-toggle key must be in REG_DEFAULTS"
    )


def test_enum_feature_switch_defaults():
    for key, default in _FROZEN_ENUM_DEFAULTS_ORACLE.items():
        assert key in SETTINGS_SCHEMA, f"{key} missing from SETTINGS_SCHEMA"
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "enum", f"{key} type {entry['type']!r} != 'enum'"
        assert entry["default"] == default, f"{key} default {entry['default']!r} != {default!r}"

        # `or`-idiom byte-for-byte (D-15): both None and "" resolve to default; a real
        # value passes through unchanged.
        assert _parse_setting(key, "") == default
        assert _parse_setting(key, None) == default
        assert _parse_setting(key, "on") == "on"


def test_full_registry_coverage():
    """D-17/WARNING-2: iterate ALL SETTINGS_SCHEMA entries unconditionally — the
    catch-all coverage gate across every type (text/int/list/date/enum/toggle/photo/file),
    not just the migrated-so-far groups."""
    allowed_types = {"toggle", "int", "list", "date", "text", "enum", "photo", "file"}
    seen_keys = set()

    for key, entry in SETTINGS_SCHEMA.items():
        assert isinstance(entry["group"], str) and entry["group"], f"{key} has empty group"
        assert entry["type"] in allowed_types, f"{key} has unknown type {entry['type']!r}"
        assert key not in seen_keys, f"{key} duplicated in registry"
        seen_keys.add(key)
        _parse_setting(key, entry["default"])  # must not raise
