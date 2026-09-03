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
from handlers import admin_settings  # Phase 13 (13-06): settings moved out of admin.py
from handlers.admin_caps import required_capability


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
    grouped_keys = [k for _, __, keys in admin_settings.SETTINGS_GROUPS for k in keys]
    all_keys = [k for k, _, _ in admin_settings.SETTINGS_FIELDS]
    leftover = admin_settings._settings_group_keys("misc")

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

    text = asyncio.run(admin_settings.render_settings_text())

    assert ("x" * 61) not in text
    assert "…" not in text


def test_landing_keyboard_emits_group_nav_not_per_field(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_settings.build_settings_keyboard())
    flat = _flat_callback_data(kb)

    # Phase 20 (20-01): состав рядов-групп зафиксирован целиком, а не «хотя бы один» —
    # разрез «📝 Регистрация» на анкету и «📋 Заявки» обязан быть виден этому сторожу.
    assert [cd for cd in flat if cd and cd.startswith("settings_group:")] == [
        "settings_group:event", "settings_group:reg", "settings_group:apps",
        "settings_group:sheets", "settings_group:pay", "settings_group:party",
        "settings_group:consent", "settings_group:game", "settings_group:system",
    ]
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

    text = asyncio.run(admin_settings.render_settings_group_text("pay"))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("pay"))
    flat = _flat_callback_data(kb)

    assert "✏️ задано" in text
    assert "не задано" in text
    assert "settings_edit:payment_options" in flat
    assert "admin_sec:pay" in flat  # Phase 20 (20-04): «Назад» ведёт в раздел-владелец группы, а не на исчезнувший плоский лендинг


def test_group_event_contains_photo_and_file_callbacks(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("event"))
    flat = _flat_callback_data(kb)

    assert any(cd.startswith("settings_photo:") for cd in flat)
    assert any(cd.startswith("settings_file:") for cd in flat)
    assert any(cd.startswith("settings_edit:") for cd in flat)


def test_group_keyboard_collapses_unconfigured_under_noop_header(tmp_path):
    _admin_ready(tmp_path)
    # pay group has 7 keys, none set -> all unconfigured -> noop header must appear.
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("pay"))
    flat = _flat_callback_data(kb)
    assert "settings_group_noop" in flat


def test_show_settings_group_handler_renders_subscreen(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_group:pay")
    asyncio.run(admin_settings.show_settings_group(cb))

    assert cb.message.edit_calls == 1
    assert "Оплата" in cb.message.text
    flat = _flat_callback_data(cb.message.markup)
    assert "settings_edit:payment_options" in flat


def test_show_settings_group_is_capability_guarded():
    # Phase 8 / D-01: the old per-handler `config.ADMIN_IDS` check (and the direct-call test
    # that exercised it) is gone (08-04, one-shot migration, D-03) -- CapabilityMiddleware is
    # now the ONLY enforcement point, and it only wraps events dispatched through the real
    # router, not direct handler calls. The structural guarantee survives with a new carrier:
    # the handler stays registered, and its callback_data resolves to a real capability.
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "show_settings_group" in names
    assert required_capability(callback_data="settings_group:pay") == "settings"


def test_settings_group_noop_just_answers(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_group_noop")
    asyncio.run(admin_settings.settings_group_noop(cb))
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
    # "roles" added 08-01 (D-09/D-10): role -> capability matrix + per-role kill switch.
    # "sheets" added quick 260815-3hw: Google Sheets tab names ("📄 Вкладки таблицы" screen).
    # "game" added Phase 09.1 (A): free-form submission flow texts ("🎮 Геймификация" screen).
    allowed_groups = {
        "event", "reg", "reg_questions", "pay", "party", "consent", "toggles", "roles",
        "sheets", "game", "misc",
        # "menu" added Phase 09.2 (B): тумблеры кнопок главного меню, экран «🔘 Кнопки
        # главного меню»
        "menu",
        # "system" added Phase 14 (CFG-01): proxy timings that used to live only in .env,
        # экран «🔧 Система».
        "system",
        # "dashboard" added Phase 15 (15-02, D-19): восемь тумблеров блоков веб-дашборда,
        # свой экран «📊 Дашборд» (handlers/admin_dashboard.py) — вне SETTINGS_FIELDS.
        "dashboard",
        # "miniapp" added Phase 19 (19-01, D-06): тумблеры/оформление/разделы/тексты Mini App,
        # свой экран «🎨 Оформление» (план 19-08) — вне SETTINGS_FIELDS, как и dashboard.
        "miniapp",
        # "apps" added quick 260902-tzh: modcard_fields/modcard_answer_limit — набор вопросов
        # и лимит длины ответа для карточки заявки, свой экран «🧾 Поля карточки заявки»
        # (handlers/admin_modcard.py) — вне SETTINGS_FIELDS, как dashboard/miniapp.
        "apps",
    }
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

    text = asyncio.run(admin_settings.render_settings_group_text("event"))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("event"))
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
    assert "admin_sec:event" in flat


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
            # party_sheet_tab moved to the "sheets" group (quick 260815-3hw) -- covered by
            # tests/test_sheet_tabs_settings_260815.py instead.
            "party_closed_text": "text", "approve_text__party": "text",
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
    text = asyncio.run(admin_settings.render_settings_group_text("reg"))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("reg"))
    flat = _flat_callback_data(kb)

    # Phase 20 (20-01): в группе остались ТОЛЬКО настройки анкеты — тексты «после
    # регистрации / одобрения / отклонения / на рассмотрении», предотбор и догонялка
    # переехали в «📋 Заявки» (см. test_render_snapshot_apps ниже).
    expected_keys = [
        "source_options", "city_options", "study_field_options",
        "goal_options", "formats_options", "university_options",
        # Phase 17.1 (17.1-03, schema-completeness): экран выбора города при /start —
        # в хвосте группы.
        "city_fork_text",
    ]
    expected_labels = [
        "📢 Источники", "🏙 Города (варианты)", "🎯 Направления обучения (варианты)",
        "🎯 Цель участия (варианты)", "📋 Форматы форума (варианты)", "🏫 Список ВУЗов",
        "🏙 Выбор города: вопрос",
    ]
    # Fresh DB -> nothing configured. Phase 17.1 (17.1-01): «⏳ Заявка на рассмотрении» —
    # первый ключ этой группы с непустым дефолтом в реестре, поэтому у него флаг
    # «по умолчанию» (менеджер видит: текст участнику уходит, просто стандартный), а не
    # «— не задано». У остальных ключей группы дефолта нет — флаг прежний.
    # Phase 17.1 (17.1-03): у трёх новых текстов есть дефолт (тот же флаг «по умолчанию»);
    # «🎯 Предотбор: ссылка» дефолта не имеет — «— не задано».
    # Quick 260819: «⏰ Догонялка: текст напоминания» -- текст с дефолтом (флаг «по умолчанию»);
    # «⏰ Догонялка: через сколько минут» -- int, как pending_reminder_interval: функциональный
    # parse-дефолт НЕ показывается как display-дефолт (_SETTINGS_DISPLAY_DEFAULTS только text).
    defaulted_labels = {"🏙 Выбор города: вопрос"}
    for label in defaulted_labels:
        assert f"{label}: <i>по умолчанию</i>" in text, f"missing/wrong flag for {label}"
    for label in expected_labels:
        if label in defaulted_labels:
            continue
        assert f"{label}: <i>— не задано</i>" in text, f"missing/wrong flag for {label}"
    # Quick 260815-3hw: short_sheet_tab moved to the new "sheets" group screen -- no longer
    # rendered here at all (see tests/test_sheet_tabs_settings_260815.py::test_render_snapshot_sheets).
    positions = [text.index(label) for label in expected_labels]
    assert positions == sorted(positions), "label order drifted"

    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "settings_group_noop" in flat
    assert "admin_sec:form" in flat


def test_render_snapshot_apps(tmp_path):
    """Phase 20 (20-01, ADMIN-IA-01): «📋 Заявки» — всё, что делегат видит ПОСЛЕ подачи
    анкеты. Ключи те же, что были в «📝 Регистрация», порядок — путь делегата: подтверждение
    -> решение -> ожидание -> предотбор -> догонялка."""
    _admin_ready(tmp_path)
    text = asyncio.run(admin_settings.render_settings_group_text("apps"))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("apps"))
    flat = _flat_callback_data(kb)

    expected_keys = [
        "reg_complete_text", "approve_text", "reject_text", "pending_gate_text",
        "pending_reminder_interval",
        "preselect_no_username_text", "preselect_fail_text", "preselect_link",
        "nudge_after_minutes", "nudge_text",
        # Phase 23-01 (APP-TINDER-01, D-05): шаблоны причин отказа шторки Mini App.
        "reject_reason_templates",
    ]
    expected_labels = [
        "✅ После регистрации", "🎉 После одобрения", "🚫 При отклонении",
        "⏳ Заявка на рассмотрении", "🕒 Тайминг батчей заявок",
        "🎯 Предотбор: нет @username", "🎯 Предотбор: не прошёл", "🎯 Предотбор: ссылка",
        "⏰ Догонялка: через сколько минут", "⏰ Догонялка: текст напоминания",
        "✍️ Причины отказа",
    ]
    defaulted_labels = {
        "⏳ Заявка на рассмотрении", "🎯 Предотбор: нет @username",
        "🎯 Предотбор: не прошёл", "⏰ Догонялка: текст напоминания",
    }
    for label in defaulted_labels:
        assert f"{label}: <i>по умолчанию</i>" in text, f"missing/wrong flag for {label}"
    for label in expected_labels:
        if label in defaulted_labels:
            continue
        assert f"{label}: <i>— не задано</i>" in text, f"missing/wrong flag for {label}"
    positions = [text.index(label) for label in expected_labels]
    assert positions == sorted(positions), "label order drifted"

    assert "Заявки" in text
    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "admin_sec:apps" in flat


def test_render_snapshot_pay(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_settings.render_settings_group_text("pay"))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("pay"))
    flat = _flat_callback_data(kb)

    expected_keys = [
        "payment_options", "payment_requisites", "payment_requisites_by_lc",
        "payment_deadline", "payment_reminder_text", "payment_overdue_text", "penalty_schedule",
        # Phase 17.1 (17.1-02): делегатские экраны платёжного потока — в хвосте группы.
        "payment_option_picker_header_text", "payment_details_template_text",
        "payment_pay_later_text", "payment_pay_later_menu_hint_text",
        "payment_receipt_received_text",
    ]
    expected_labels = [
        "💳 Варианты оплаты", "💰 Реквизиты оплаты", "💳 Реквизиты по ЛК",
        "📅 Дедлайн оплаты", "⏰ Текст напоминания об оплате", "⌛ Текст «оплата просрочена»",
        "⚠️ Штрафы за отмену",
    ]
    # Phase 17.1 (17.1-02): у новых ключей есть дефолт в реестре -> флаг «по умолчанию»
    # (текст делегату уходит, просто стандартный), а не «— не задано».
    defaulted_labels = [
        "💳 Выбор варианта: заголовок", "💰 Экран оплаты: шаблон",
        "⏭ «Оплачу позже»: ответ", "⏭ «Оплачу позже»: подсказка про меню",
        "✅ Чек получен: подтверждение",
    ]
    for label in expected_labels:
        assert f"{label}: <i>— не задано</i>" in text, f"missing/wrong flag for {label}"
    for label in defaulted_labels:
        assert f"{label}: <i>по умолчанию</i>" in text, f"missing/wrong flag for {label}"
    positions = [text.index(label) for label in expected_labels + defaulted_labels]
    assert positions == sorted(positions), "label order drifted"

    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "settings_group_noop" in flat
    assert "admin_sec:pay" in flat


def test_render_snapshot_party(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_settings.render_settings_group_text("party"))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("party"))
    flat = _flat_callback_data(kb)

    # party_closed_text carries a display default (_SETTINGS_DISPLAY_DEFAULTS pre-migration ->
    # registry `default` post-migration, T-06-06) -> «по умолчанию» flag. party_sheet_tab moved
    # to the new "sheets" group screen (quick 260815-3hw) -- no longer rendered here.
    assert "🎉 Текст «вечеринка закрыта»: <i>по умолчанию</i>" in text
    # approve_text__party has no display default -> plain unconfigured flag.
    assert "🎉 После одобрения (Party): <i>— не задано</i>" in text
    # Phase 17.1 (17.1-03, schema-completeness): party_fork_text carries the old
    # DEFAULT_PARTY_FORK_TEXT literal as its registry default -> «по умолчанию».
    assert "🔀 Развилка формата: вопрос: <i>по умолчанию</i>" in text

    expected_keys = ["party_closed_text", "approve_text__party", "party_fork_text"]
    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "admin_sec:form" in flat


def test_render_snapshot_consent(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_settings.render_settings_group_text("consent"))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("consent"))
    flat = _flat_callback_data(kb)

    assert "✅ Текст кнопки согласия: <i>— не задано</i>" in text
    assert "📋 Список согласий: <i>— не задано</i>" in text

    # quick 260822: +consent_version / +consent_recollect_text (версионирование согласий)
    expected_keys = ["consent_button_text", "consent_list", "consent_version", "consent_recollect_text"]
    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]
    assert "settings_group_noop" in flat
    assert "admin_sec:form" in flat


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
    from handlers.reg_schema import REG_DEFAULTS

    assert REG_DEFAULTS == _FROZEN_REG_DEFAULTS_ORACLE


def test_toggle_keys_coverage():
    from handlers.reg_schema import REG_DEFAULTS

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


# ── Phase 6 plan 06-05 (REG-02, D-12, T-06-19/T-06-20): admin feature-switch READ-SITE
# regression net. Captures the CURRENT (pre-migration) render_settings_text +
# build_settings_keyboard output byte-for-byte, over both the all-default (fresh DB) case
# and a mixed/non-default case, so Task 2's get_setting_typed read-migration cannot drift
# the landing text or the bespoke toggle-button callback_data/text. These tests PASS
# against the pre-migration code (regression lock, not RED-first) and MUST still pass,
# unchanged, after Task 2.

def test_settings_landing_text_snapshot(tmp_path):
    _admin_ready(tmp_path)

    # ── all-default case (fresh DB, every feature-switch unset) ──
    text = asyncio.run(admin_settings.render_settings_text())

    assert "📝 Форма регистрации: <b>⚡ Краткая</b>" in text
    assert "🎁 Бонус за регистрацию: <b>❌ Выкл</b>" in text
    assert "✅ Модерация полной формы: <b>👮 Ручная</b>" in text
    assert "✅ Модерация краткой формы: <b>⚡ Авто</b>" in text
    assert "🔔 Уведомление о заявке: <b>🕒 Пачкой (напоминалка)</b>" in text
    assert "💳 Модуль оплаты: <b>❌ Выкл</b>" in text
    assert "📋 Модуль согласий: <b>❌ Выкл</b>" in text
    assert "⏰ Автонапоминания об оплате: <b>✅ Вкл</b>" in text
    assert "🎉 Трек вечеринки: <b>❌ Выкл</b>" in text
    assert "🔀 Вопрос-развилка формата: <b>❌ Выкл</b>" in text
    assert "✅ Модерация вечеринки: <b>👮 Ручная</b>" in text
    assert "🎯 Предотбор по таблице: <b>❌ Выкл</b>" in text
    assert "📋 Сводка о заявках в ожидании: <b>✅ Вкл</b>" in text
    assert "⏰ Догонялка брошенных анкет: <b>✅ Вкл</b>" in text

    # ── mixed/non-default case: party_enabled="on", full_approval="auto" flips two lines ──
    asyncio.run(db.set_setting("party_enabled", "on"))
    asyncio.run(db.set_setting("full_approval", "auto"))
    text2 = asyncio.run(admin_settings.render_settings_text())

    assert "🎉 Трек вечеринки: <b>✅ Вкл</b>" in text2
    assert "✅ Модерация полной формы: <b>⚡ Авто</b>" in text2
    # Untouched lines stay at their default rendering.
    assert "✅ Модерация краткой формы: <b>⚡ Авто</b>" in text2


def test_settings_toggle_button_snapshot(tmp_path):
    _admin_ready(tmp_path)

    # ── all-default case: exact button texts + callback_data, in position order ──
    kb = asyncio.run(admin_settings.build_settings_keyboard())
    flat_buttons = [btn for row in kb.inline_keyboard for btn in row]
    texts = [btn.text for btn in flat_buttons]
    cbs = [btn.callback_data for btn in flat_buttons]

    expected_default = [
        ("📝 Регистрация: ⚡ Краткая → 📋 Полная", "settings_toggle_reg"),
        ("🎁 Бонус: ❌ Выкл → ✅ Вкл", "settings_toggle_bonus"),
        ("✅ Полная форма: 👮 Ручная → ⚡ Авто", "settings_toggle_full_approval"),
        ("✅ Краткая форма: ⚡ Авто → 👮 Ручная", "settings_toggle_short_approval"),
        ("🔔 Уведомление: 🕒 Пачкой → 📨 Сразу", "settings_toggle_notify"),
        ("💳 Оплата: ❌ Выкл → ✅ Вкл", "toggle_payment_enabled"),
        ("⏰ Автонапоминания оплаты: ✅ Вкл → ❌ Выкл", "toggle_payment_reminders"),
        ("📋 Согласия: ❌ Выкл → ✅ Вкл", "toggle_consent_enabled"),
        ("🧾 PDF согласий", "admin_consent_pdfs"),
        ("🏫 ВУЗ: свободный ввод → выбор из списка", "toggle_uni_mode"),
        ("🎓 ВУЗ/курс только у студентов: ✅ Вкл → ❌ Выкл", "toggle_edu_conditional"),
        ("🔢 Нумерация вопросов: ❌ Выкл → ✅ Вкл", "toggle_show_progress"),
        ("🎉 Трек вечеринки: ❌ Выкл → ✅ Вкл", "toggle_party_enabled"),
        ("🔀 Вопрос-развилка формата: ❌ Выкл → ✅ Вкл", "toggle_party_fork_question"),
        ("✅ Модерация вечеринки: 👮 Ручная → ⚡ Авто", "settings_toggle_party_approval"),
        ("🎯 Предотбор по таблице: ❌ Выкл → ✅ Вкл", "toggle_preselect_enabled"),
        ("📋 Сводка о заявках: ✅ Вкл → ❌ Выкл", "toggle_pending_reminder"),
        ("⏰ Догонялка анкет: ✅ Вкл → ❌ Выкл", "toggle_nudge_enabled"),
        ("🎛 Тип события (пресет)", "admin_event_preset"),
        ("📋 Вопросы регистрации", "admin_reg_questions"),
        ("✏️ Тексты вопросов", "admin_reg_prompts"),
        ("🔘 Кнопки меню", "admin_menu_buttons"),
    ]
    for i, (expected_text, expected_cb) in enumerate(expected_default):
        assert texts[i] == expected_text, f"button {i} text drifted: {texts[i]!r} != {expected_text!r}"
        assert cbs[i] == expected_cb, f"button {i} callback_data drifted: {cbs[i]!r} != {expected_cb!r}"

    # ── mixed case: party_enabled="on", payment_enabled="on", pending_notify_mode="instant" ──
    asyncio.run(db.set_setting("party_enabled", "on"))
    asyncio.run(db.set_setting("payment_enabled", "on"))
    asyncio.run(db.set_setting("pending_notify_mode", "instant"))
    kb2 = asyncio.run(admin_settings.build_settings_keyboard())
    flat_buttons2 = [btn for row in kb2.inline_keyboard for btn in row]
    texts2 = [btn.text for btn in flat_buttons2]
    cbs2 = [btn.callback_data for btn in flat_buttons2]

    assert texts2[4] == "🔔 Уведомление: 📨 Сразу → 🕒 Пачкой"
    assert cbs2[4] == "settings_toggle_notify"
    assert texts2[5] == "💳 Оплата: ✅ Вкл → ❌ Выкл"
    assert cbs2[5] == "toggle_payment_enabled"
    assert texts2[12] == "🎉 Трек вечеринки: ✅ Вкл → ❌ Выкл"
    assert cbs2[12] == "toggle_party_enabled"
    # Untouched buttons keep their default text/position.
    assert texts2[0] == expected_default[0][0]
    assert cbs2[0] == "settings_toggle_reg"


def test_toggle_preselect_enabled_flips_and_rerenders_landing(tmp_path):
    """Quick 260819: «🎯 Предотбор по таблице» — кнопка-переключатель на лендинге настроек.
    Тап переключает preselect_enabled off -> on -> off (дефолт из реестра = off), перерисовывает
    текст и клавиатуру лендинга, и тап защищён capability `settings`."""
    _admin_ready(tmp_path)
    assert required_capability(callback_data="toggle_preselect_enabled") == "settings"

    cb = FakeCallback("toggle_preselect_enabled")
    asyncio.run(admin_settings.toggle_preselect_enabled(cb))
    assert asyncio.run(db.get_setting("preselect_enabled")) == "on"
    assert cb.message.edit_calls == 1
    # Phase 20 (20-04): перерисовывается РАЗДЕЛ-владелец тумблера («📋 Заявки»), а не
    # исчезнувший плоский лендинг — новое состояние видно в подписи самой кнопки.
    assert "<b>📋 Заявки</b>" in cb.message.text
    assert "🎯 Предотбор по таблице: ✅ Вкл → ❌ Выкл" in [b.text for r in cb.message.markup.inline_keyboard for b in r]
    assert cb.answers and cb.answers[0][0] == "🎯 Предотбор по таблице: ✅ Вкл"

    cb2 = FakeCallback("toggle_preselect_enabled")
    asyncio.run(admin_settings.toggle_preselect_enabled(cb2))
    assert asyncio.run(db.get_setting("preselect_enabled")) == "off"
    assert "🎯 Предотбор по таблице: ❌ Выкл → ✅ Вкл" in [b.text for r in cb2.message.markup.inline_keyboard for b in r]


def test_scheduler_and_reminder_keys_declared_with_code_defaults(tmp_path):
    """Quick 260819: ключи services/scheduler.py / services/reminders.py объявлены в реестре,
    дефолты байт-в-байт равны прежним литералам кода (`_int_or_default(..., N)`,
    `!= "off"`, DEFAULT_NUDGE_TEXT), генерический UI рендерит int/text как у соседей, а
    on/off — тумблеры на лендинге (дефолт ON: тап выключает)."""
    from services import scheduler as sched
    _admin_ready(tmp_path)
    expected = {
        "nudge_after_minutes": ("int", "reg", 120),
        "nudge_text": ("text", "reg", sched.DEFAULT_NUDGE_TEXT),
        "nudge_scan_minutes": ("int", "system", 15),
        "allowlist_refresh_minutes": ("int", "system", 60),
        "incomplete_sync_hours": ("int", "system", 2),
        "nudge_enabled": ("enum", "toggles", "on"),
        "pending_reminder_enabled": ("enum", "toggles", "on"),
    }
    for key, (typ, group, default) in expected.items():
        entry = SETTINGS_SCHEMA[key]
        assert (entry["type"], entry["group"], entry["default"]) == (typ, group, default), key
        assert _parse_setting(key, None) == default
        if typ != "text":  # text-ветка -- passthrough ("" остаётся ""), как у соседей
            assert _parse_setting(key, "") == default
    # int-ветка generic-парсера: мусор -> дефолт (та же терпимость, что _int_or_default)
    assert _parse_setting("nudge_scan_minutes", "abc") == 15
    assert _parse_setting("incomplete_sync_hours", "3") == 3
    assert sched._int_or_default("abc", 15) == 15
    # группы: system рендерит три новых int-поля, reg -- два поля догонялки
    assert admin_settings._settings_group_keys("system")[-3:] == [
        "nudge_scan_minutes", "allowlist_refresh_minutes", "incomplete_sync_hours"]
    # Phase 20 (20-01): поля догонялки переехали из «📝 Регистрация» в «📋 Заявки» вместе
    # с остальными послеподачными текстами — сама пара ключей и её порядок не менялись.
    # Phase 23-01 (APP-TINDER-01, D-05): reject_reason_templates добавлен хвостом _APPS_FIELD_ORDER.
    assert admin_settings._settings_group_keys("apps")[-3:] == [
        "nudge_after_minutes", "nudge_text", "reject_reason_templates"]
    text = asyncio.run(admin_settings.render_settings_group_text("system"))
    # int без значения в БД -- «— не задано» (как proxy_*: parse-дефолт не display-дефолт)
    assert "⏱ Догонялка: как часто проверять: <i>— не задано</i>" in text
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("system"))
    assert "settings_edit:incomplete_sync_hours" in _flat_callback_data(kb)
    # тумблеры: дефолт ON -> первый тап выключает; чтение консьюмеров (`!= "off"`) не меняется
    from services import reminders
    cb = FakeCallback("toggle_pending_reminder")
    asyncio.run(admin_settings.toggle_pending_reminder(cb))
    assert asyncio.run(db.get_setting("pending_reminder_enabled")) == "off"
    assert reminders._reminder_enabled(asyncio.run(db.get_setting("pending_reminder_enabled"))) is False
    cb2 = FakeCallback("toggle_nudge_enabled")
    asyncio.run(admin_settings.toggle_nudge_enabled(cb2))
    assert asyncio.run(db.get_setting("nudge_enabled")) == "off"
    assert sched._nudge_enabled(asyncio.run(db.get_setting("nudge_enabled"))) is False
    assert required_capability(callback_data="toggle_pending_reminder") == "settings"
    assert required_capability(callback_data="toggle_nudge_enabled") == "settings"


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


# ── Phase 6 plan 07 (final coverage sweep): close the 06-05/06-06-flagged raw-idiom
# boundary -- render_settings_text's own registration_mode read (:466) and the three
# generic multi-key toggle helpers (_toggle_approval_setting :716, _toggle_module_setting
# :748, _toggle_value_setting :793) must resolve their current-value read through
# get_setting_typed instead of `get_setting(key) or default`. ────────────────────────────

def test_toggle_current_value_equiv_across_generic_helpers(tmp_path):
    """Value-equivalence: driving each public toggle handler across raw DB inputs
    (None/empty/each enum option) must produce the exact same NEW value the pre-migration
    `get_setting(key) or default` idiom would have flipped to. Established as PASS-first
    (byte-for-byte preserving swap of the read primitive only -- get_setting_typed's enum
    branch `raw if raw else default` is mathematically identical to `raw or default`),
    matching the 06-05 precedent for this class of migration."""
    _admin_ready(tmp_path)

    async def go():
        cases = [
            # (handler, key, raw_values, flip_fn(current) -> expected_new_val)
            (admin_settings.toggle_full_approval, "full_approval", [None, "", "manual", "auto"],
             lambda cur: "auto" if cur == "manual" else "manual"),
            (admin_settings.toggle_short_approval, "short_approval", [None, "", "manual", "auto"],
             lambda cur: "auto" if cur == "manual" else "manual"),
            (admin_settings.toggle_party_approval, "party_approval", [None, "", "manual", "auto"],
             lambda cur: "auto" if cur == "manual" else "manual"),
            (admin_settings.toggle_payment_enabled, "payment_enabled", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_consent_enabled, "consent_enabled", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_party_enabled, "party_enabled", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_party_fork_question, "party_fork_question", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_preselect_enabled, "preselect_enabled", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_pending_reminder, "pending_reminder_enabled", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_nudge_enabled, "nudge_enabled", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_payment_reminders, "payment_reminders_enabled", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_uni_mode, "reg_university_mode", [None, "", "text", "list"],
             lambda cur: "text" if cur == "list" else "list"),
            (admin_settings.toggle_edu_conditional, "edu_conditional", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
            (admin_settings.toggle_show_progress, "reg_show_progress", [None, "", "on", "off"],
             lambda cur: "off" if cur == "on" else "on"),
        ]
        for handler, key, raw_values, flip_fn in cases:
            default = SETTINGS_SCHEMA[key]["default"]
            for raw in raw_values:
                await db.delete_setting(key)
                if raw is not None:
                    await db.set_setting(key, raw)
                cb = FakeCallback("noop")
                await handler(cb)
                new_val = await db.get_setting(key)
                current_oracle = raw or default  # pre-migration idiom: get_setting(key) or default
                expected_new = flip_fn(current_oracle)
                assert new_val == expected_new, (
                    f"{key}: raw={raw!r} -> new_val={new_val!r}, expected {expected_new!r}"
                )
    asyncio.run(go())

    # Render's own registration_mode read (:466) must also be byte-equivalent.
    async def go_render():
        for raw in [None, "", "full", "short"]:
            await db.delete_setting("registration_mode")
            if raw is not None:
                await db.set_setting("registration_mode", raw)
            text = await admin_settings.render_settings_text()
            oracle_mode = raw or "short"
            expected_label = "📋 Полная" if oracle_mode == "full" else "⚡ Краткая"
            assert f"📝 Форма регистрации: <b>{expected_label}</b>" in text, (
                f"render_settings_text registration_mode label mismatch for raw={raw!r}"
            )
    asyncio.run(go_render())


def test_generic_toggle_helpers_wired_to_registry():
    """Wiring gate (RED before the migration, GREEN after): render_settings_text's own
    registration_mode read plus the three generic toggle helpers must call
    get_setting_typed -- not the raw get_setting(key) or default idiom -- for their
    current-value read. Source-inspection (inspect.getsource), matching the established
    pattern for reads embedded in shared/parameterized functions (06-06 precedent)."""
    import inspect

    render_src = inspect.getsource(admin_settings.render_settings_text)
    assert 'get_setting_typed("registration_mode")' in render_src, (
        "render_settings_text's own registration_mode read is not wired to get_setting_typed"
    )

    appr_src = inspect.getsource(admin_settings._toggle_approval_setting)
    assert "get_setting_typed(key)" in appr_src, (
        "_toggle_approval_setting is not wired to get_setting_typed"
    )

    mod_src = inspect.getsource(admin_settings._toggle_module_setting)
    assert "get_setting_typed(key)" in mod_src, (
        "_toggle_module_setting is not wired to get_setting_typed"
    )

    val_src = inspect.getsource(admin_settings._toggle_value_setting)
    assert "get_setting_typed(key)" in val_src, (
        "_toggle_value_setting is not wired to get_setting_typed"
    )
