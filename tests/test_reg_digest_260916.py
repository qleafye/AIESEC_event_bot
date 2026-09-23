"""Квик 260916: уведомления менеджеру о новых заявках — каждую отдельно или пачкой.

Покрывает: ключи реестра и подписи, тумблер в разделе «📋 Заявки», поведение по режимам
(сразу / в очередь + джоба), перевзвод джобы на каждую заявку, текст дайджеста с
HTML-экранированием и сворачиванием хвоста, маршрутизацию по городу заявки в обоих режимах,
немедленную отправку правки анкеты мимо очереди, ре-арм на старте.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from services import reg_digest as rd
from services import scheduler as sched

ADMIN_ID = 933301
DELEGATE_MSK = 933305
DELEGATE_SPB = 933306


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reg_submit_digest.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _add_delegate(telegram_id, event_city, full_name):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "event_city": event_city,
        "full_name": full_name,
        "registration_date": "2026-09-16 00:00:00",
    }))


class _FakeScheduler:
    """add_job/get_job — ровно то, что трогает reg_digest."""

    def __init__(self, existing=()):
        self.jobs = {}
        for jid in existing:
            self.jobs[jid] = {"id": jid}
        self.add_calls = []

    def add_job(self, func, trigger, **kw):
        self.add_calls.append((func, trigger, kw))
        self.jobs[kw["id"]] = {"id": kw["id"], "func": func, **kw}

    def get_job(self, jid):
        return self.jobs.get(jid)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _capture_notify(monkeypatch):
    """Подменяет notify_by_capability в handlers.admin_caps (reg_digest импортирует его лениво)."""
    from handlers import admin_caps
    calls = []

    async def fake(bot, cap, text, *, parse_mode=None, city=None):
        calls.append({"cap": cap, "text": text, "city": city, "parse_mode": parse_mode})
        return 1
    monkeypatch.setattr(admin_caps, "notify_by_capability", fake)
    return calls


async def _submit(bot, telegram_id, city_raw=None, is_new=True):
    await rd.notify_application(
        bot, telegram_id=telegram_id, admin_text="📋 Новая заявка от <Иванов>",
        city_raw=city_raw, is_new=is_new,
    )


# ── Реестр и UI ───────────────────────────────────────────────────────────────

def test_schema_keys_present_with_human_labels():
    from settings_schema import SETTINGS_SCHEMA, REG_SUBMIT_NOTIFY_MODE_LABELS
    mode = SETTINGS_SCHEMA["reg_submit_notify_mode"]
    assert mode["type"] == "enum" and mode["group"] == "apps"
    assert mode["options"] == ["each", "digest"] and mode["default"] == "each"
    assert mode["option_labels"] == REG_SUBMIT_NOTIFY_MODE_LABELS
    assert REG_SUBMIT_NOTIFY_MODE_LABELS == {
        "each": "Каждую заявку отдельно", "digest": "Пачкой (дайджест)",
    }
    minutes = SETTINGS_SCHEMA["reg_submit_digest_minutes"]
    assert minutes["type"] == "int" and minutes["group"] == "apps" and minutes["default"] == 15
    assert "через сколько минут тишины" in minutes["prompt"].lower()
    assert "например 15" in minutes["prompt"]


def test_schema_key_max_minutes_defaults_to_zero_uncapped():
    """Квик 260923 (D-C): дефолт 0 = без потолка — поведение прежнее для остальных событий."""
    from settings_schema import SETTINGS_SCHEMA
    cap = SETTINGS_SCHEMA["reg_submit_digest_max_minutes"]
    assert cap["type"] == "int" and cap["group"] == "apps" and cap["default"] == 0
    assert "не ограничивать" in cap["prompt"].lower()


def test_minutes_is_a_plain_field_mode_is_toggle_only():
    """Минуты правятся обычным полем (int), режим — только тумблером, без ввода кода."""
    from handlers import admin_settings
    assert "reg_submit_digest_minutes" in admin_settings._APPS_FIELD_ORDER
    assert "reg_submit_notify_mode" not in {k for k, _, _ in admin_settings.SETTINGS_FIELDS}


def test_toggle_row_shows_human_labels_only(tmp_path):
    _db_ready(tmp_path)
    from handlers import admin_settings
    rows = asyncio.run(admin_settings.settings_toggle_rows(ADMIN_ID))
    btn = rows["toggle_reg_submit_notify"][0][0]
    assert btn.callback_data == "toggle_reg_submit_notify"
    assert "Каждую заявку отдельно" in btn.text and "Пачкой (дайджест)" in btn.text
    assert "each" not in btn.text and "digest" not in btn.text


def test_toggle_row_lives_in_the_applications_section_right_after_its_neighbour():
    from handlers import admin_sections as sec
    callbacks = [sec.row_callback(r) for r in sec._declared_rows("apps")]
    assert callbacks.count("toggle_reg_submit_notify") == 1
    assert callbacks.index("toggle_reg_submit_notify") == callbacks.index("settings_toggle_notify") + 1
    for token, _label, rows in sec.SECTIONS:
        if token == "apps":
            continue
        assert "toggle_reg_submit_notify" not in [sec.row_callback(r) for r in rows]


def test_toggle_callback_registered_under_settings_capability():
    from handlers.admin_caps import required_capability
    assert required_capability(callback_data="toggle_reg_submit_notify") == "settings"


def test_toggle_handler_flips_mode_and_answers_with_label(tmp_path):
    _db_ready(tmp_path)
    from handlers import admin_settings

    class _Msg:
        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            self.text, self.kb = text, reply_markup

    class _Cb:
        def __init__(self):
            self.data = "toggle_reg_submit_notify"
            self.message = _Msg()
            self.from_user = type("U", (), {"id": ADMIN_ID})()
            self.answers = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append(text)

    cb = _Cb()
    asyncio.run(admin_settings.toggle_reg_submit_notify(cb))
    assert asyncio.run(db.get_setting("reg_submit_notify_mode")) == "digest"
    assert "Пачкой (дайджест)" in cb.answers[0]
    assert "digest" not in cb.answers[0]
    cb2 = _Cb()
    asyncio.run(admin_settings.toggle_reg_submit_notify(cb2))
    assert asyncio.run(db.get_setting("reg_submit_notify_mode")) == "each"
    assert "Каждую заявку отдельно" in cb2.answers[0]


# ── Режим «каждую отдельно» (по умолчанию) ────────────────────────────────────

def test_each_mode_sends_immediately_and_queues_nothing(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)

    asyncio.run(_submit(_Bot(), DELEGATE_MSK))

    assert len(calls) == 1
    assert calls[0]["cap"] == "moderate_reg"
    assert calls[0]["text"] == "📋 Новая заявка от <Иванов>"  # текст карточки не переписываем
    assert calls[0]["city"] is None  # модуль городов выключен -> глобально, как раньше
    assert fake.add_calls == []
    assert asyncio.run(db.list_unsent_reg_digest(all_cities=True)) == []


def test_each_mode_routes_by_application_city_when_cities_on(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    asyncio.run(_submit(_Bot(), DELEGATE_SPB, city_raw="spb"))

    assert calls[0]["city"] == "spb"


def test_empty_city_never_narrows_the_fan_out(tmp_path, monkeypatch):
    """Пустой город в анкете при включённом модуле городов НЕ схлопывается в город по
    умолчанию — иначе режим уведомлений молча сузил бы круг получателей."""
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    asyncio.run(_submit(_Bot(), DELEGATE_MSK, city_raw=None))

    assert calls[0]["city"] is None


# ── Режим «пачкой» ────────────────────────────────────────────────────────────

def test_digest_mode_queues_and_arms_one_job_per_city(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    asyncio.run(db.set_setting("reg_submit_notify_mode", "digest"))

    asyncio.run(_submit(_Bot(), DELEGATE_MSK))

    assert calls == []  # ничего не ушло сразу
    rows = asyncio.run(db.list_unsent_reg_digest(None))
    assert [(r["telegram_id"], r["city"]) for r in rows] == [(DELEGATE_MSK, None)]
    assert len(fake.add_calls) == 1
    func, trigger, kw = fake.add_calls[0]
    assert func is rd.send_reg_digest and trigger == "date"
    assert kw["id"] == "reg_digest:all" and kw["replace_existing"] is True and kw["args"] == [None]


def test_digest_mode_rearms_same_job_on_every_application(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    asyncio.run(db.set_setting("reg_submit_notify_mode", "digest"))
    asyncio.run(db.set_setting("reg_submit_digest_minutes", "3"))

    asyncio.run(_submit(_Bot(), DELEGATE_MSK))
    asyncio.run(_submit(_Bot(), DELEGATE_SPB))

    assert [kw["id"] for _, _, kw in fake.add_calls] == ["reg_digest:all", "reg_digest:all"]
    first, second = (kw["run_date"] for _, _, kw in fake.add_calls)
    assert second >= first  # окно тишины сдвинулось вперёд
    assert len(asyncio.run(db.list_unsent_reg_digest(None))) == 2


def test_digest_mode_city_routing_separate_jobs(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    asyncio.run(db.set_setting("reg_submit_notify_mode", "digest"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    asyncio.run(_submit(_Bot(), DELEGATE_MSK, city_raw="msk"))
    asyncio.run(_submit(_Bot(), DELEGATE_SPB, city_raw="spb"))

    assert sorted(kw["id"] for _, _, kw in fake.add_calls) == ["reg_digest:msk", "reg_digest:spb"]
    assert [r["telegram_id"] for r in asyncio.run(db.list_unsent_reg_digest("spb"))] == [DELEGATE_SPB]


# ── Квик 260923 (D-C): потолок ожидания пачки ──────────────────────────────────

def test_arm_digest_job_cap_zero_is_byte_identical(tmp_path, monkeypatch):
    """cap_minutes=0 (дефолт) -> run_at ровно now+minutes, потолок не участвует вовсе."""
    _db_ready(tmp_path)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    fixed_now = datetime(2026, 9, 23, 10, 0, tzinfo=sched.MOSCOW_TZ)
    monkeypatch.setattr(rd, "datetime", _FrozenDatetime(fixed_now))

    rd.arm_digest_job(None, 15)

    run_date = fake.add_calls[0][2]["run_date"]
    assert run_date == fixed_now + timedelta(minutes=15)


def test_arm_digest_job_cap_shortens_run_at_when_queue_is_not_quiet(tmp_path, monkeypatch):
    """Первая заявка в очереди — 25 минут назад, окно тишины 15, потолок 30 -> сводка уходит
    через 5 минут от «сейчас» (потолок сработал раньше окна тишины)."""
    _db_ready(tmp_path)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    fixed_now = datetime(2026, 9, 23, 10, 0, tzinfo=sched.MOSCOW_TZ)
    monkeypatch.setattr(rd, "datetime", _FrozenDatetime(fixed_now))
    first_queued_at = (fixed_now - timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S")

    rd.arm_digest_job(None, 15, first_queued_at=first_queued_at, cap_minutes=30)

    run_date = fake.add_calls[0][2]["run_date"]
    assert run_date == fixed_now + timedelta(minutes=5)


def test_arm_digest_job_cap_never_goes_to_the_past(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    fixed_now = datetime(2026, 9, 23, 10, 0, tzinfo=sched.MOSCOW_TZ)
    monkeypatch.setattr(rd, "datetime", _FrozenDatetime(fixed_now))
    first_queued_at = (fixed_now - timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")

    rd.arm_digest_job(None, 15, first_queued_at=first_queued_at, cap_minutes=30)

    run_date = fake.add_calls[0][2]["run_date"]
    assert run_date == fixed_now  # потолок уже прошёл — не в прошлое, а «сейчас»


class _FrozenDatetime:
    """Подменяет ИМЯ `datetime` внутри `services.reg_digest` целиком (единственный
    потребитель — `arm_digest_job`, вызывает только `.now(tz)` и `.strptime(...)`)."""

    def __init__(self, frozen):
        self._frozen = frozen

    def now(self, tz=None):
        return self._frozen

    def strptime(self, *a, **kw):
        return datetime.strptime(*a, **kw)


def test_edit_is_never_batched(tmp_path, monkeypatch):
    """Правка/переподача уже поданной анкеты уходит менеджеру СРАЗУ даже в режиме «пачкой»:
    это не «новая заявка», и текст у неё свой — «что именно изменилось»."""
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    asyncio.run(db.set_setting("reg_submit_notify_mode", "digest"))

    asyncio.run(_submit(_Bot(), DELEGATE_MSK, is_new=False))

    assert len(calls) == 1 and calls[0]["cap"] == "moderate_reg"
    assert fake.add_calls == []
    assert asyncio.run(db.list_unsent_reg_digest(all_cities=True)) == []


# ── Текст дайджеста и отправка ────────────────────────────────────────────────

def test_build_digest_text_escapes_and_collapses_the_tail():
    assert rd.build_digest_text(["Иванова", "<b>Петров</b>"]) == (
        "📥 Новые заявки: 2 — Иванова, &lt;b&gt;Петров&lt;/b&gt; → 📋 Заявки"
    )
    long = rd.build_digest_text([f"Имя{i}" for i in range(18)])
    assert long.startswith("📥 Новые заявки: 18 — Имя0, ")
    assert "Имя14 и ещё 3 → 📋 Заявки" in long
    assert "Имя15" not in long


# ── Phase 31 (31-06, D-17): счётчик автоотказов в дайджесте ──────────────────

def test_build_digest_text_zero_auto_reject_is_byte_identical():
    """Событие без правил автоотказа — текст байт-в-байт прежний (дефолт `auto_reject_count=0`
    не добавляет хвост)."""
    assert rd.build_digest_text(["Иванова", "Петров"]) == rd.build_digest_text(
        ["Иванова", "Петров"], 0,
    )
    assert ", из них" not in rd.build_digest_text(["Иванова", "Петров"], 0)


def test_build_digest_text_appends_auto_reject_suffix_with_ru_plural():
    text1 = rd.build_digest_text(["Иванова"], 1)
    text2 = rd.build_digest_text(["Иванова", "Петров"], 2)
    text5 = rd.build_digest_text(["А", "Б", "В", "Г", "Д"], 5)
    assert text1.endswith(", из них 🤖 1 автоотказ")
    assert text2.endswith(", из них 🤖 2 автоотказа")
    assert text5.endswith(", из них 🤖 5 автоотказов")


# ── Квик 260923 (D-D): разбивка по правилам в тексте пачки ────────────────────

def test_build_digest_text_mixed_batch_appends_rule_breakdown_line():
    text = rd.build_digest_text(
        ["Иванова", "Петров"], 1, [("Младше 16", 1)],
    )
    lines = text.splitlines()
    assert lines[0] == "📥 Новые заявки: 2 — Иванова, Петров → 📋 Заявки, из них 🤖 1 автоотказ"
    assert lines[1] == "🤖 Автоотказ: 1 — «Младше 16» 1"


def test_build_digest_text_all_auto_swaps_header_and_cta():
    text = rd.build_digest_text(
        ["Иванова"], 1, [("Младше 16", 1)], all_auto=True,
    )
    lines = text.splitlines()
    assert lines[0] == "🤖 Автоотказ: 1 — Иванова → 🚫 Правила автоотказа → 🤖 Автоотказы"
    assert "📥 Новые заявки" not in text
    assert lines[1] == "🤖 Автоотказ: 1 — «Младше 16» 1"


def test_build_digest_text_rule_counts_none_is_byte_identical():
    assert rd.build_digest_text(["Иванова"], 1) == rd.build_digest_text(["Иванова"], 1, None, False)


def test_send_reg_digest_appends_rule_breakdown_and_all_auto_header(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    monkeypatch.setattr(sched, "_bot", _Bot())
    _add_delegate(DELEGATE_MSK, "msk", "Иванова")
    rid = asyncio.run(db.create_reject_rule(
        name="Младше 16", city=None, tracks="[]", conditions="{}", action="reject",
        reject_text=None, enabled=1, created_by=None,
    ))
    from services.reject_journal import record_auto_reject
    asyncio.run(record_auto_reject(DELEGATE_MSK, [rid], ["текст"]))
    now = "2026-09-16 12:00:00"
    asyncio.run(db.enqueue_reg_digest(DELEGATE_MSK, "msk", now, auto_rejected=1))

    asyncio.run(rd.send_reg_digest("msk"))

    assert calls[0]["text"] == (
        "🤖 Автоотказ: 1 — Иванова → 🚫 Правила автоотказа → 🤖 Автоотказы\n"
        "🤖 Автоотказ: 1 — «Младше 16» 1"
    )


def test_notify_application_digest_mode_stamps_auto_rejected_flag(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    asyncio.run(db.set_setting("reg_submit_notify_mode", "digest"))

    asyncio.run(rd.notify_application(
        _Bot(), telegram_id=DELEGATE_MSK, admin_text="🤖 Автоотказ: Иванова", auto_rejected=True,
    ))
    asyncio.run(rd.notify_application(
        _Bot(), telegram_id=DELEGATE_SPB, admin_text="📋 Новая заявка", auto_rejected=False,
    ))

    rows = asyncio.run(db.list_unsent_reg_digest(None))
    by_tid = {r["telegram_id"]: r["auto_rejected"] for r in rows}
    assert by_tid[DELEGATE_MSK] == 1
    assert by_tid[DELEGATE_SPB] == 0


def test_send_reg_digest_counts_auto_rejected_from_queue_not_live_status(tmp_path, monkeypatch):
    """D-17/Pitfall 3: счётчик считается по полю СТРОК очереди (штампуется на постановке), а
    не перечитыванием `users.status` — менеджер мог вернуть заявку из журнала автоотказов
    между постановкой в очередь и отправкой дайджеста, и статус уже сменился на `pending`."""
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    monkeypatch.setattr(sched, "_bot", _Bot())
    _add_delegate(DELEGATE_MSK, "msk", "Иванова")
    _add_delegate(DELEGATE_SPB, "msk", "Петров")
    now = "2026-09-16 12:00:00"
    asyncio.run(db.enqueue_reg_digest(DELEGATE_MSK, "msk", now, auto_rejected=1))
    asyncio.run(db.enqueue_reg_digest(DELEGATE_SPB, "msk", now, auto_rejected=0))
    # Статус делегата сменился (менеджер вернул на модерацию) ПОСЛЕ постановки в очередь — счётчик
    # обязан остаться 1 (по флагу строки очереди), а не 0 (по текущему статусу).
    asyncio.run(db.set_user_status(DELEGATE_MSK, "pending"))

    assert asyncio.run(rd.send_reg_digest("msk")) == 1

    assert len(calls) == 1
    assert calls[0]["text"] == (
        "📥 Новые заявки: 2 — Иванова, Петров → 📋 Заявки, из них 🤖 1 автоотказ"
    )


def test_send_reg_digest_sends_once_per_city_and_marks_rows(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    monkeypatch.setattr(sched, "_bot", _Bot())
    _add_delegate(DELEGATE_MSK, "msk", "Иванова")
    _add_delegate(DELEGATE_SPB, "spb", "<Петров>")
    now = "2026-09-16 12:00:00"
    asyncio.run(db.enqueue_reg_digest(DELEGATE_MSK, "msk", now))
    asyncio.run(db.enqueue_reg_digest(DELEGATE_SPB, "msk", now))
    asyncio.run(db.enqueue_reg_digest(DELEGATE_SPB, "spb", now))

    assert asyncio.run(rd.send_reg_digest("msk")) == 1

    assert len(calls) == 1
    assert calls[0]["city"] == "msk" and calls[0]["cap"] == "moderate_reg"
    assert calls[0]["text"] == "📥 Новые заявки: 2 — Иванова, &lt;Петров&gt; → 📋 Заявки"
    assert asyncio.run(db.list_unsent_reg_digest("msk")) == []
    assert len(asyncio.run(db.list_unsent_reg_digest("spb"))) == 1  # чужой город не тронут

    # Повторный запуск при пустой очереди — без сообщения.
    assert asyncio.run(rd.send_reg_digest("msk")) == 0
    assert len(calls) == 1


def test_send_reg_digest_empty_queue_sends_nothing(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    monkeypatch.setattr(sched, "_bot", _Bot())
    assert asyncio.run(rd.send_reg_digest(None)) == 0
    assert calls == []


def test_unknown_delegate_falls_back_to_id_not_a_crash(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    monkeypatch.setattr(sched, "_bot", _Bot())
    asyncio.run(db.enqueue_reg_digest(DELEGATE_MSK, None, "2026-09-16 12:00:00"))

    assert asyncio.run(rd.send_reg_digest(None)) == 1
    assert str(DELEGATE_MSK) in calls[0]["text"]


# ── Ре-арм на старте ──────────────────────────────────────────────────────────

def test_rearm_pending_digests_arms_missing_jobs_only(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    now = "2026-09-16 12:00:00"
    asyncio.run(db.enqueue_reg_digest(DELEGATE_MSK, "msk", now))
    spb_id = asyncio.run(db.enqueue_reg_digest(DELEGATE_SPB, "spb", now))
    asyncio.run(db.enqueue_reg_digest(DELEGATE_SPB, None, now))
    asyncio.run(db.mark_reg_digest_sent([spb_id], now))  # spb уже отправлен
    fake = _FakeScheduler(existing=["reg_digest:all"])  # джоба без города пережила рестарт
    monkeypatch.setattr(sched, "_scheduler", fake)

    armed = asyncio.run(rd.rearm_pending_digests())

    assert armed == ["msk"]
    assert [kw["id"] for _, _, kw in fake.add_calls] == ["reg_digest:msk"]


def test_rearm_pending_digests_noop_when_queue_empty(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    assert asyncio.run(rd.rearm_pending_digests()) == []
    assert fake.add_calls == []


def test_init_scheduler_calls_both_rearms_on_boot():
    """Структурно: init_scheduler дёргает ре-арм заявок ДО resume() — иначе накопленная
    очередь молчала бы до следующей заявки."""
    import inspect
    src = inspect.getsource(sched.init_scheduler)
    assert "rearm_pending_reg_digests()" in src
    assert src.index("rearm_pending_reg_digests()") < src.index("_scheduler.resume()")


def test_post_finalize_goes_through_reg_digest_only():
    """post_finalize больше не зовёт notify_by_capability для заявок напрямую — только через
    services.reg_digest (иначе настройка режима молча перестала бы действовать на одном из
    двух путей подачи: чат и Mini App)."""
    import inspect
    from services import reg_finalize
    src = inspect.getsource(reg_finalize.post_finalize)
    assert "notify_application(" in src
    assert "await notify_by_capability(" not in src
