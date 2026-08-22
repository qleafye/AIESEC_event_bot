"""Quick 260822 — версионирование согласий + напоминание менеджеру о целях обработки.

(A) `consent_version` в реестре и в аудит-таблице `user_consents`; строка «Согласие: v…»
в карточке заявки с маркером старой редакции; гейт пересогласия (дефолт off).
(B) После смены пресета типа события и после ВКЛЮЧЕНИЯ расширяющих модулей (оплата, резюме)
менеджеру приходит напоминание с кнопкой «📋 Открыть согласия».

Без pytest-asyncio (как везде): asyncio.run + config.DB_PATH на tmp_path.
"""
import asyncio
import sqlite3

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA, _parse_setting
from services import consent as consent_svc
from handlers import admin_settings, admin_reg_config, admin_consent, admin_moderation, reg_consent
from handlers.admin_caps import required_capability

ADMIN_ID = 900822


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "consent_versioning.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "tester"


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.sent = []        # (text, reply_markup) через answer
        self.documents = []   # (file_id, caption, reply_markup)
        self.markup_edits = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup

    async def answer(self, text=None, reply_markup=None, parse_mode=None, **kw):
        self.sent.append((text, reply_markup))

    async def answer_document(self, file_id, caption=None, reply_markup=None, parse_mode=None):
        self.documents.append((file_id, caption, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_edits += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _reminders(msg):
    return [(t, kb) for t, kb in msg.sent if t and "Цели обработки данных изменились" in t]


# ── (A) реестр ──────────────────────────────────────────────────────────────────────────

def test_schema_keys():
    v = SETTINGS_SCHEMA["consent_version"]
    assert (v["type"], v["group"], v["default"]) == ("text", "consent", db.DEFAULT_CONSENT_VERSION)
    assert db.DEFAULT_CONSENT_VERSION == "1"
    assert "2026-08" in v["prompt"] and "v2" in v["prompt"]
    r = SETTINGS_SCHEMA["consent_recollect_enabled"]
    assert (r["type"], r["group"], r["default"]) == ("enum", "toggles", "off")
    assert "пересогласие" in r["label"].lower()
    assert _parse_setting("consent_recollect_enabled", None) == "off"
    assert _parse_setting("consent_recollect_enabled", "") == "off"
    t = SETTINGS_SCHEMA["consent_recollect_text"]
    assert (t["type"], t["group"]) == ("text", "consent") and t["default"]
    # экран «📋 Согласия» показывает оба новых текстовых ключа
    keys = admin_settings._settings_group_keys("consent")
    assert "consent_version" in keys and "consent_recollect_text" in keys


def test_consent_group_screen_has_recollect_toggle(tmp_path):
    _ready(tmp_path)
    text = asyncio.run(admin_settings.render_settings_group_text("consent"))
    assert "🔖 Версия согласия: <i>по умолчанию</i>" in text
    assert "🔁 Просить пересогласие при новой редакции: <b>❌ Выкл</b>" in text
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("consent"))
    assert "toggle_consent_recollect" in _flat(kb)

    cb = FakeCallback("toggle_consent_recollect")
    asyncio.run(admin_consent.toggle_consent_recollect(cb))
    assert asyncio.run(db.get_setting("consent_recollect_enabled")) == "on"
    assert "✅ Вкл" in cb.answers[0][0] and cb.answers[0][1] is True
    assert "🔁 Просить пересогласие при новой редакции: <b>✅ Вкл</b>" in cb.message.text
    assert "toggle_consent_recollect" in _flat(cb.message.markup)
    asyncio.run(admin_consent.toggle_consent_recollect(FakeCallback("toggle_consent_recollect")))
    assert asyncio.run(db.get_setting("consent_recollect_enabled")) == "off"


def test_caps_mapping():
    assert required_capability(callback_data="toggle_consent_recollect") == "settings"
    # кнопка «📋 Открыть согласия» ведёт на существующий экран группы
    assert consent_svc.PURPOSE_REMINDER_CALLBACK == "settings_group:consent"
    assert required_capability(callback_data="settings_group:consent") == "settings"


# ── (A) БД: миграция существующей таблицы + версия при записи ────────────────────────────

def test_migration_on_existing_db_keeps_rows_and_allows_new_version(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE user_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            consent_key TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            UNIQUE(user_id, consent_key)
        );
        CREATE INDEX idx_consents_user ON user_consents(user_id);
        INSERT INTO user_consents (user_id, consent_key, accepted_at) VALUES (7, 'data', '2026-07-01T10:00:00');
        INSERT INTO user_consents (user_id, consent_key, accepted_at) VALUES (7, 'policy', '2026-07-01T10:01:00');
        INSERT INTO user_consents (user_id, consent_key, accepted_at) VALUES (8, 'data', '2026-07-02T10:00:00');
        """
    )
    con.commit(); con.close()

    config.DB_PATH = str(path)
    asyncio.run(db.init_db())
    asyncio.run(db.init_db())  # идемпотентно

    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(user_consents)")}
    assert "consent_version" in cols
    rows = con.execute("SELECT id, user_id, consent_key, accepted_at, consent_version FROM user_consents ORDER BY id").fetchall()
    assert rows == [
        (1, 7, "data", "2026-07-01T10:00:00", None),
        (2, 7, "policy", "2026-07-01T10:01:00", None),
        (3, 8, "data", "2026-07-02T10:00:00", None),
    ]
    assert con.execute("SELECT name FROM sqlite_master WHERE name='user_consents_v1'").fetchone() is None
    assert con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_consents_user'").fetchone()
    con.close()

    # старые строки = «до версионирования»; новая редакция — НОВАЯ строка, старая не затёрта
    assert asyncio.run(db.get_user_consent_versions(7)) == [("data", None), ("policy", None)]
    asyncio.run(db.set_setting("consent_version", "2026-08"))
    asyncio.run(db.record_user_consent(7, "data"))
    asyncio.run(db.record_user_consent(7, "data"))  # повтор той же редакции дедупится
    assert asyncio.run(db.get_user_consent_versions(7)) == [
        ("data", None), ("policy", None), ("data", "2026-08"),
    ]
    assert asyncio.run(db.get_user_consents(7)) == ["data", "policy", "data"]


def test_record_stores_current_version(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.record_user_consent(1, "data"))
    assert asyncio.run(db.get_user_consent_versions(1)) == [("data", "1")]  # дефолт реестра
    asyncio.run(db.set_setting("consent_version", " v2 "))
    asyncio.run(db.record_user_consent(1, "data"))
    asyncio.run(db.record_user_consent(2, "data", consent_version="explicit"))
    assert asyncio.run(db.get_user_consent_versions(1)) == [("data", "1"), ("data", "v2")]
    assert asyncio.run(db.get_user_consent_versions(2)) == [("data", "explicit")]
    assert asyncio.run(db.current_consent_version()) == "v2"


# ── (A) карточка заявки ─────────────────────────────────────────────────────────────────

def test_card_line_old_vs_new(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(consent_svc.consent_card_line(5)) is None  # подписей нет — строки нет

    asyncio.run(db.record_user_consent(5, "data"))
    assert asyncio.run(consent_svc.consent_card_line(5)) == "📝 Согласие: v1"

    asyncio.run(db.set_setting("consent_version", "2026-08"))
    line = asyncio.run(consent_svc.consent_card_line(5))
    assert line == "📝 Согласие: v1 ⚠️ подписано старой редакцией"

    asyncio.run(db.record_user_consent(5, "data"))
    assert asyncio.run(consent_svc.consent_card_line(5)) == "📝 Согласие: v2026-08"

    # строка до версионирования (NULL) — тоже «старая редакция», код «legacy» не показываем
    con = sqlite3.connect(config.DB_PATH)
    con.execute("INSERT INTO user_consents (user_id, consent_key, accepted_at) VALUES (6, 'data', 'x')")
    con.commit(); con.close()
    assert asyncio.run(consent_svc.consent_card_line(6)) == (
        "📝 Согласие: до версионирования ⚠️ подписано старой редакцией"
    )

    # версия экранируется — менеджерский текст не должен ломать HTML карточки
    asyncio.run(db.set_setting("consent_version", "<b>"))
    asyncio.run(db.record_user_consent(9, "data"))
    assert asyncio.run(consent_svc.consent_card_line(9)) == "📝 Согласие: v&lt;b&gt;"


def test_application_card_renders_consent_line():
    user = {"telegram_id": 1, "full_name": "Иван", "resume_text": None}
    base = admin_moderation._render_application_card(user, 1, 1)
    assert "Согласие" not in base
    with_line = admin_moderation._render_application_card(
        user, 1, 1, consent_line="📝 Согласие: v1 ⚠️ подписано старой редакцией"
    )
    assert with_line.endswith("📎 Резюме: нет\n📝 Согласие: v1 ⚠️ подписано старой редакцией")
    assert with_line.count("\n") == base.count("\n") + 1  # ровно одна строка


# ── (A) пересогласие делегата ──────────────────────────────────────────────────────────

def _consents_configured():
    asyncio.run(db.set_setting("consent_enabled", "on"))
    asyncio.run(db.set_setting("consent_list", "Согласие на обработку данных|data; Политика|policy"))


def test_recollect_gate_default_off(tmp_path):
    _ready(tmp_path)
    _consents_configured()
    asyncio.run(db.record_user_consent(11, "data"))
    asyncio.run(db.record_user_consent(11, "policy"))
    asyncio.run(db.set_setting("consent_version", "v2"))  # редакция поднята, гейт выключен
    msg = FakeMessage()
    assert asyncio.run(reg_consent.maybe_offer_consent_recollect(msg, 11)) is False
    assert msg.sent == [] and msg.documents == []


def test_recollect_flow_when_enabled(tmp_path):
    _ready(tmp_path)
    _consents_configured()
    asyncio.run(db.set_setting("consent_recollect_enabled", "on"))
    asyncio.run(db.set_setting("consent_pdf_data", "PDF_FILE_ID"))
    asyncio.run(db.record_user_consent(12, "data"))
    asyncio.run(db.record_user_consent(12, "policy"))

    # всё подписано текущей редакцией — ничего не показываем
    msg = FakeMessage()
    assert asyncio.run(reg_consent.maybe_offer_consent_recollect(msg, 12)) is False
    assert msg.sent == [] and msg.documents == []

    asyncio.run(db.set_setting("consent_version", "v2"))
    msg = FakeMessage()
    assert asyncio.run(reg_consent.maybe_offer_consent_recollect(msg, 12)) is True
    assert msg.sent[0][0] == SETTINGS_SCHEMA["consent_recollect_text"]["default"]
    file_id, caption, kb = msg.documents[0]  # первое согласие — с PDF, той же карточкой
    assert file_id == "PDF_FILE_ID" and "Согласие на обработку данных" in caption
    assert _flat(kb) == ["consent_renew:data"]

    # тап по старой/чужой карточке — ничего не пишем
    cb = FakeCallback("consent_renew:unknown", user_id=12)
    asyncio.run(reg_consent.consent_renew_accept(cb))
    assert asyncio.run(db.get_user_consent_versions(12)) == [("data", "1"), ("policy", "1")]

    cb = FakeCallback("consent_renew:data", user_id=12)
    asyncio.run(reg_consent.consent_renew_accept(cb))
    assert cb.answers == [("✅ Принято", False)] and cb.message.markup_edits == 1
    assert asyncio.run(db.get_user_consent_versions(12))[-1] == ("data", "v2")
    # второе согласие (без PDF) — текстом, с той же кнопкой
    text, kb = cb.message.sent[-1]
    assert "Политика" in text and _flat(kb) == ["consent_renew:policy"]

    cb2 = FakeCallback("consent_renew:policy", user_id=12)
    asyncio.run(reg_consent.consent_renew_accept(cb2))
    assert cb2.message.sent[-1][0] == "✅ Спасибо! Согласие обновлено."
    assert asyncio.run(db.get_user_consent_versions(12)) == [
        ("data", "1"), ("policy", "1"), ("data", "v2"), ("policy", "v2"),
    ]
    # больше не просим
    msg = FakeMessage()
    assert asyncio.run(reg_consent.maybe_offer_consent_recollect(msg, 12)) is False

    # гейт без модуля согласий — молчит
    asyncio.run(db.set_setting("consent_enabled", "off"))
    asyncio.run(db.set_setting("consent_version", "v3"))
    assert asyncio.run(reg_consent.maybe_offer_consent_recollect(FakeMessage(), 12)) is False


# ── (B) напоминание менеджеру ──────────────────────────────────────────────────────────

def _assert_reminder(msg, what):
    rem = _reminders(msg)
    assert len(rem) == 1, msg.sent
    text, kb = rem[0]
    assert what in text and "«📋 Согласия»" in text and "поднимите версию" in text
    assert [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row] == [
        ("📋 Открыть согласия", "settings_group:consent")
    ]


def test_reminder_on_widening_toggle_only_when_turned_on(tmp_path):
    _ready(tmp_path)
    cb = FakeCallback("toggle_payment_enabled")
    asyncio.run(admin_settings.toggle_payment_enabled(cb))
    assert asyncio.run(db.get_setting("payment_enabled")) == "on"
    _assert_reminder(cb.message, "оплата")

    cb = FakeCallback("toggle_payment_enabled")  # выключение не расширяет обработку
    asyncio.run(admin_settings.toggle_payment_enabled(cb))
    assert _reminders(cb.message) == []

    cb = FakeCallback("toggle_nudge_enabled")  # не расширяющий модуль
    asyncio.run(admin_settings.toggle_nudge_enabled(cb))
    assert _reminders(cb.message) == []
    cb = FakeCallback("toggle_consent_enabled")
    asyncio.run(admin_settings.toggle_consent_enabled(cb))
    assert _reminders(cb.message) == []


def test_reminder_on_resume_question_toggle(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def _noop():
        return None
    monkeypatch.setattr(admin_reg_config, "_refresh_sheet_header", _noop)
    asyncio.run(db.set_setting("reg_q_resume", "off"))
    cb = FakeCallback("reg_q_toggle:reg_q_resume")
    asyncio.run(admin_reg_config.toggle_reg_question(cb))
    assert asyncio.run(db.get_setting("reg_q_resume")) == "on"
    _assert_reminder(cb.message, "Nextcloud")
    cb = FakeCallback("reg_q_toggle:reg_q_city")
    asyncio.run(admin_reg_config.toggle_reg_question(cb))
    assert _reminders(cb.message) == []


def test_reminder_on_event_preset(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def _noop():
        return None
    monkeypatch.setattr(admin_reg_config, "_refresh_sheet_header", _noop)
    cb = FakeCallback("preset_confirm:forum")
    asyncio.run(admin_reg_config.preset_confirm(cb))
    assert cb.answers[0][0].startswith("Пресет применён")
    _assert_reminder(cb.message, "тип события")

    # правка event_type текстом — тот же хелпер, что и пресет
    msg = FakeMessage()
    asyncio.run(admin_settings.remind_consent_purposes_after_preset(msg, "conference"))
    _assert_reminder(msg, "conference")


def test_reminder_helper_is_fail_soft():
    class Broken:
        async def answer(self, *a, **kw):
            raise RuntimeError("net down")
    # исключение в отправке подсказки не должно ронять тумблер/пресет
    asyncio.run(admin_consent.remind_consent_purposes_after_preset(Broken(), "forum"))
    assert asyncio.run(admin_consent.remind_consent_purposes_if_widened(Broken(), "payment_enabled", "on")) is True
