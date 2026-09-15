"""Квик 260916: «📊 Итоги дня» — вечерняя сводка менеджерам.

Покрывает: ключи реестра и подписи, тумблер в разделе «🔧 Управление», разбор и валидацию
времени, сборку текста из засеянных строк (кто сколько одобрил / проверил), схлопывание
пустого раздела в одну строку, «оба раздела пусты -> не шлём вовсе», дедупликацию получателя
с двумя правами, городской срез и регистрацию cron-джобы на старте.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio

import cities
from config import config
from database import db
from services import daily_digest as dd
from services.timeutil import msk_now

ADMIN_ID = 944401
MANAGER_A = 944402
MANAGER_B = 944403
DELEGATE_MSK = 944410
DELEGATE_SPB = 944411

# «Сегодня» по Москве: `add_coins`/`claim_submission` ставят время сами (`msk_now`), подсунуть
# им прошлое нельзя — значит и остальные «сегодняшние» строки сеем сегодняшним днём, а
# «вчерашние» — заведомо прошлой датой.
DAY = msk_now().strftime("%Y-%m-%d")
STAMP = f"{DAY} 12:00:00"
OTHER_DAY_STAMP = "2026-01-15 12:00:00"


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_daily_digest.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _add_delegate(telegram_id, event_city, full_name, *, status="pending",
                  registration_date=STAMP):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "event_city": event_city,
        "full_name": full_name,
        "registration_date": registration_date,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))


def _decide(telegram_id, decision, by, at=STAMP, *, undoable=False):
    """`undoable=True` — живая строка (`effects_sent_at IS NULL`), которую можно отменить:
    ровно так пишет решения веб-путь Mini App."""
    return asyncio.run(db.record_application_decision(
        telegram_id, decision, None, by, at, at,
        effects_sent_at=None if undoable else at,
    ))


class _Bot:
    def __init__(self, broken=()):
        self.sent = []
        self.broken = set(broken)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id in self.broken:
            raise RuntimeError("bot was blocked")
        self.sent.append((chat_id, text))


def _stats(**overrides):
    base = {
        "apps_new": 0, "apps_approved": 0, "apps_rejected": 0, "apps_pending": 0,
        "app_managers": [], "game_submissions": 0, "game_reviewed": 0,
        "coins_awarded": 0, "game_managers": [],
    }
    base.update(overrides)
    return base


# ── Реестр и UI ───────────────────────────────────────────────────────────────

def test_schema_keys_present_with_human_labels():
    from settings_schema import SETTINGS_SCHEMA
    enabled = SETTINGS_SCHEMA["daily_digest_enabled"]
    assert enabled["type"] == "enum" and enabled["options"] == ["on", "off"]
    assert enabled["default"] == "off" and enabled["group"] == "toggles"
    at = SETTINGS_SCHEMA["daily_digest_time"]
    assert at["type"] == "text" and at["group"] == "system" and at["default"] == "21:00"
    assert at["format"] == "time"
    # Честная приписка про перезапуск — как у остальных таймингов фоновых джоб.
    assert "после перезапуска" in at["prompt"]


def test_time_key_is_editable_from_the_system_group():
    from handlers import admin_settings
    assert "daily_digest_time" in admin_settings._SYSTEM_FIELD_ORDER


def test_toggle_row_and_section_placement(tmp_path):
    _db_ready(tmp_path)
    from handlers import admin_settings
    from handlers import admin_sections as sec

    rows = asyncio.run(admin_settings.settings_toggle_rows(ADMIN_ID))
    btn = rows["toggle_daily_digest"][0][0]
    assert btn.callback_data == "toggle_daily_digest"
    assert "❌ Выкл → ✅ Вкл" in btn.text  # дефолт OFF
    assert "on" not in btn.text.split(":")[0]

    callbacks = [sec.row_callback(r) for r in sec._declared_rows("manage")]
    assert callbacks.count("toggle_daily_digest") == 1
    assert callbacks.index("toggle_daily_digest") == callbacks.index("toggle_chat_tracking_enabled") + 1
    for token, _label, rows_ in sec.SECTIONS:
        if token == "manage":
            continue
        assert "toggle_daily_digest" not in [sec.row_callback(r) for r in rows_]


def test_toggle_callback_registered_under_settings_capability():
    from handlers.admin_caps import required_capability
    assert required_capability(callback_data="toggle_daily_digest") == "settings"


def test_toggle_handler_flips_the_switch(tmp_path):
    _db_ready(tmp_path)
    from handlers import admin_settings

    class _Msg:
        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            self.text = text

    class _Cb:
        def __init__(self):
            self.data = "toggle_daily_digest"
            self.message = _Msg()
            self.from_user = type("U", (), {"id": ADMIN_ID})()
            self.answers = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append(text)

    cb = _Cb()
    asyncio.run(admin_settings.toggle_daily_digest(cb))
    assert asyncio.run(db.get_setting("daily_digest_enabled")) == "on"
    assert "📊 Итоги дня менеджерам: ✅ Вкл" == cb.answers[0]


# ── Время ─────────────────────────────────────────────────────────────────────

def test_parse_time_reads_human_input_and_falls_back_on_garbage():
    assert dd.parse_time("21:00") == (21, 0)
    assert dd.parse_time(" 9:05 ") == (9, 5)
    assert dd.parse_time("00:00") == (0, 0)
    for bad in (None, "", "абв", "25:00", "21:60", "21", "21-00"):
        assert dd.parse_time(bad) == (21, 0), bad


def test_time_input_is_validated_before_it_reaches_the_registry():
    from settings_validation import validate_setting_value
    value, error = validate_setting_value("daily_digest_time", "21:30")
    assert (value, error) == ("21:30", None)
    value, error = validate_setting_value("daily_digest_time", "9:00")
    assert (value, error) == ("09:00", None)  # ведущий ноль менеджер помнить не обязан
    value, error = validate_setting_value("daily_digest_time", "вечером")
    assert value is None and "ЧЧ:ММ" in error
    value, error = validate_setting_value("daily_digest_time", "21:70")
    assert value is None and "минуты" in error


# ── Текст сводки ──────────────────────────────────────────────────────────────

def test_build_digest_text_full_day():
    stats = _stats(
        apps_new=23, apps_approved=18, apps_rejected=2, apps_pending=41,
        app_managers=[(MANAGER_A, 11, 1), (MANAGER_B, 7, 1)],
        game_submissions=14, game_reviewed=12, coins_awarded=340,
        game_managers=[(MANAGER_A, 9), (MANAGER_B, 3)],
    )
    names = {MANAGER_A: "Марина Иванова", MANAGER_B: "Пётр Сидоров"}
    text = dd.build_digest_text(stats, names, day_label="16.09", city_title="Москва")
    assert text == (
        "📊 Итоги дня, 16.09 — Москва\n"
        "📋 Заявки: новых 23 · одобрено 18 · отклонено 2 · ждут 41\n"
        "   Марина Иванова — 11 ✅ / 1 ❌\n"
        "   Пётр Сидоров — 7 ✅ / 1 ❌\n"
        "🎮 Геймификация: сдач 14 · проверено 12 · монет начислено 340\n"
        "   Марина Иванова — 9\n"
        "   Пётр Сидоров — 3"
    )


def test_quiet_section_collapses_to_one_line():
    stats = _stats(apps_new=3, apps_pending=7, app_managers=[])
    text = dd.build_digest_text(stats, {}, day_label="16.09")
    assert "🎮 Геймификация: сегодня тихо" in text
    assert "📊 Итоги дня, 16.09" == text.splitlines()[0]  # без модуля городов заголовок голый

    stats = _stats(apps_pending=7, game_submissions=2, game_reviewed=1, coins_awarded=10)
    text = dd.build_digest_text(stats, {}, day_label="16.09")
    assert "📋 Заявки: сегодня тихо, ждут 7" in text


def test_empty_day_is_not_sent_at_all():
    assert dd.build_digest_text(_stats(apps_pending=41), {}, day_label="16.09") is None


def test_unknown_manager_reads_as_a_person_not_an_id():
    stats = _stats(apps_new=1, app_managers=[(MANAGER_A, 1, 0)])
    text = dd.build_digest_text(stats, {}, day_label="16.09")
    assert f"менеджер #{MANAGER_A} — 1 ✅ / 0 ❌" in text


def test_manager_name_is_escaped():
    stats = _stats(apps_new=1, app_managers=[(MANAGER_A, 1, 0)])
    text = dd.build_digest_text(stats, {MANAGER_A: "<b>Ло</b>"}, day_label="16.09")
    assert "&lt;b&gt;Ло&lt;/b&gt;" in text and "<b>Ло</b>" not in text


# ── Цифры из БД ───────────────────────────────────────────────────────────────

def test_stats_count_the_day_and_split_by_manager(tmp_path):
    _db_ready(tmp_path)
    _add_delegate(DELEGATE_MSK, "msk", "Делегат Раз", status="approved")
    _add_delegate(DELEGATE_SPB, "spb", "Делегат Два", status="pending")
    _add_delegate(944412, "msk", "Вчерашний", status="pending",
                  registration_date=OTHER_DAY_STAMP)
    _decide(DELEGATE_MSK, "approved", MANAGER_A)
    _decide(DELEGATE_SPB, "rejected", MANAGER_A)
    _decide(944412, "approved", MANAGER_B)
    _decide(944412, "rejected", MANAGER_B, at=OTHER_DAY_STAMP)  # вчерашнее решение не в счёт

    stats = asyncio.run(db.daily_digest_stats(DAY))

    assert stats["apps_new"] == 2  # вчерашняя регистрация не считается
    assert stats["apps_approved"] == 2 and stats["apps_rejected"] == 1
    assert stats["apps_pending"] == 2
    assert stats["app_managers"] == [(MANAGER_A, 1, 1), (MANAGER_B, 1, 0)]


def test_undone_decision_is_not_counted(tmp_path):
    _db_ready(tmp_path)
    _add_delegate(DELEGATE_MSK, "msk", "Делегат Раз")
    did = _decide(DELEGATE_MSK, "approved", MANAGER_A, undoable=True)
    assert asyncio.run(db.claim_application_undo(did))

    stats = asyncio.run(db.daily_digest_stats(DAY))
    assert stats["apps_approved"] == 0 and stats["app_managers"] == []


def test_stats_count_game_and_coins(tmp_path):
    _db_ready(tmp_path)
    _add_delegate(DELEGATE_MSK, "msk", "Делегат Раз")
    _add_delegate(DELEGATE_SPB, "spb", "Делегат Два")
    sid_a = asyncio.run(db.create_submission(1, DELEGATE_MSK, "text", "раз", STAMP))
    sid_b = asyncio.run(db.create_submission(2, DELEGATE_SPB, "text", "два", STAMP))
    asyncio.run(db.create_submission(3, DELEGATE_MSK, "text", "три", OTHER_DAY_STAMP))
    asyncio.run(db.claim_submission(sid_a, MANAGER_A, "approved", coins_awarded=40))
    asyncio.run(db.claim_submission(sid_b, MANAGER_B, "rejected"))
    asyncio.run(db.add_coins(DELEGATE_MSK, 40, "за задание", MANAGER_A))
    asyncio.run(db.add_coins(DELEGATE_SPB, 300, "за активность", MANAGER_A))
    asyncio.run(db.add_coins(DELEGATE_SPB, -50, "штраф", MANAGER_A))

    stats = asyncio.run(db.daily_digest_stats(DAY))

    assert stats["game_submissions"] == 2
    assert stats["game_reviewed"] == 2
    assert stats["coins_awarded"] == 340  # минусовые дельты цифру не уменьшают
    assert stats["game_managers"] == [(MANAGER_A, 1), (MANAGER_B, 1)]


def test_stats_are_scoped_by_the_delegate_city(tmp_path):
    _db_ready(tmp_path)
    _add_delegate(DELEGATE_MSK, "msk", "Делегат Раз")
    _add_delegate(DELEGATE_SPB, "spb", "Делегат Два")
    _decide(DELEGATE_MSK, "approved", MANAGER_A)
    _decide(DELEGATE_SPB, "approved", MANAGER_A)

    spb = asyncio.run(db.daily_digest_stats(DAY, city_scope=cities.city_scope("spb")))
    assert spb["apps_new"] == 1 and spb["apps_approved"] == 1
    assert spb["app_managers"] == [(MANAGER_A, 1, 0)]

    everything = asyncio.run(db.daily_digest_stats(DAY, city_scope=None))
    assert everything["apps_approved"] == 2


def test_display_names_resolve_in_one_query(tmp_path):
    _db_ready(tmp_path)
    _add_delegate(MANAGER_A, "msk", "Марина Иванова")
    names = asyncio.run(db.get_display_names([MANAGER_A, MANAGER_B, MANAGER_A, None]))
    assert names == {MANAGER_A: "Марина Иванова"}
    assert asyncio.run(db.get_display_names([])) == {}


# ── Получатели ────────────────────────────────────────────────────────────────

def _grant(uid, role):
    asyncio.run(db.add_staff(uid, role, ADMIN_ID))


def test_recipient_with_both_capabilities_gets_one_copy(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = []  # иначе суперадмин держит оба права и закрывает собой картину
    from handlers.admin_caps import role_caps_key
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg"))
    asyncio.run(db.set_setting(role_caps_key("game_manager"), "moderate_game"))
    _grant(MANAGER_A, "reg_manager")
    _grant(MANAGER_A, "game_manager")
    _grant(MANAGER_B, "game_manager")

    recipients = asyncio.run(dd.digest_recipients(None))

    assert recipients.count(MANAGER_A) == 1
    assert set(recipients) == {MANAGER_A, MANAGER_B}
    config.ADMIN_IDS = [ADMIN_ID]


def test_send_city_digest_delivers_once_per_recipient_and_survives_a_blocked_chat(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _add_delegate(DELEGATE_MSK, "msk", "Делегат Раз")
    _decide(DELEGATE_MSK, "approved", MANAGER_A)
    # Час отправки фиксируем, ДЕНЬ — сегодняшний московский: строки выше засеяны им же.
    fixed = msk_now().replace(hour=21, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(dd, "msk_now", lambda: fixed)

    async def fake_recipients(city):
        return [ADMIN_ID, MANAGER_B]
    monkeypatch.setattr(dd, "digest_recipients", fake_recipients)

    bot = _Bot(broken={MANAGER_B})
    sent = asyncio.run(dd.send_city_digest(bot, None))

    assert sent == 1  # сломанный чат не отменяет доставку остальным
    assert [uid for uid, _ in bot.sent] == [ADMIN_ID]
    assert bot.sent[0][1].startswith(f"📊 Итоги дня, {fixed.strftime('%d.%m')}")
    assert "одобрено 1" in bot.sent[0][1]


def test_send_city_digest_stays_silent_on_an_empty_day(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def fake_recipients(city):
        return [ADMIN_ID]
    monkeypatch.setattr(dd, "digest_recipients", fake_recipients)

    bot = _Bot()
    assert asyncio.run(dd.send_city_digest(bot, None)) == 0
    assert bot.sent == []


# ── Джоба ─────────────────────────────────────────────────────────────────────

def test_job_is_a_no_op_while_the_switch_is_off(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = []

    async def fake_send(bot, city):
        calls.append(city)
        return 1
    monkeypatch.setattr(dd, "send_city_digest", fake_send)

    assert asyncio.run(dd.daily_digest_job()) == 0
    assert calls == []


def test_job_sends_one_digest_per_enabled_city(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("daily_digest_enabled", "on"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    calls = []

    async def fake_send(bot, city):
        calls.append(city)
        return 1
    monkeypatch.setattr(dd, "send_city_digest", fake_send)

    total = asyncio.run(dd.daily_digest_job())

    expected = [c["code"] for c in asyncio.run(cities.enabled_cities())]
    assert calls == expected and total == len(expected)


def test_job_sends_one_global_digest_when_the_cities_module_is_off(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("daily_digest_enabled", "on"))
    calls = []

    async def fake_send(bot, city):
        calls.append(city)
        return 1
    monkeypatch.setattr(dd, "send_city_digest", fake_send)

    assert asyncio.run(dd.daily_digest_job()) == 1
    assert calls == [None]


def test_init_scheduler_registers_the_cron_job_with_replace_existing():
    """Структурно: одна cron-джоба, заводится заново на каждом старте (поэтому смена времени
    применяется после перезапуска — так и написано менеджеру в подсказке ключа)."""
    import inspect
    from services import scheduler as sched
    src = inspect.getsource(sched.init_scheduler)
    assert "daily_digest_job" in src
    assert '"cron"' in src
    assert 'get_setting_typed("daily_digest_time")' in src
    assert src.index("daily_digest_job") < src.index("_scheduler.resume()")
