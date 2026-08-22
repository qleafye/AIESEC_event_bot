"""Quick 260822: уведомления менеджеру о сданных заданиях — каждую отдельно или дайджестом.

Покрывает: ключи реестра и подписи, тумблер на экране «🎮 Геймификация», поведение по режимам
(сразу / в очередь + джоба), перевзвод джобы на каждую сдачу, текст дайджеста с HTML-экранированием,
маршрутизацию по городу делегата в обоих режимах, ре-арм на старте.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio

from config import config
from database import db
from services import game_digest as gd
from services import scheduler as sched

ADMIN_ID = 922201
DELEGATE_MSK = 922205
DELEGATE_SPB = 922206


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_submit_digest.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _add_delegate(telegram_id, event_city, full_name):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "event_city": event_city,
        "full_name": full_name,
        "registration_date": "2026-08-22 00:00:00",
    }))


class _FakeScheduler:
    """add_job/get_job/remove_job — ровно то, что трогает game_digest."""

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
    """Подменяет notify_by_capability в handlers.admin_caps (game_digest импортирует его лениво)."""
    from handlers import admin_caps
    calls = []

    async def fake(bot, cap, text, *, parse_mode=None, city=None):
        calls.append({"cap": cap, "text": text, "city": city, "parse_mode": parse_mode})
        return 1
    monkeypatch.setattr(admin_caps, "notify_by_capability", fake)
    return calls


async def _submit(bot, user_id, submission_id=1, task_id=7):
    await gd.notify_submission(
        bot, submission_id=submission_id, user_id=user_id, task_id=task_id,
        task_text="Сфоткай <кота>", submitter_name="Иван <Иванов>",
    )


# ── Реестр и UI ───────────────────────────────────────────────────────────────

def test_schema_keys_present_with_human_labels():
    from settings_schema import SETTINGS_SCHEMA, GAME_SUBMIT_NOTIFY_MODE_LABELS
    mode = SETTINGS_SCHEMA["game_submit_notify_mode"]
    assert mode["type"] == "enum" and mode["group"] == "game"
    assert mode["options"] == ["each", "digest"] and mode["default"] == "each"
    assert GAME_SUBMIT_NOTIFY_MODE_LABELS == {
        "each": "Каждую сдачу отдельно", "digest": "Пачкой (дайджест)",
    }
    minutes = SETTINGS_SCHEMA["game_submit_digest_minutes"]
    assert minutes["type"] == "int" and minutes["group"] == "game" and minutes["default"] == 15
    assert "через сколько минут тишины" in minutes["prompt"].lower()
    assert "например 15" in minutes["prompt"]


def test_minutes_in_game_group_mode_not_a_text_field():
    """Минуты правятся обычным полем (int), режим — только тумблером, без ввода кода."""
    from handlers import admin_settings
    assert "game_submit_digest_minutes" in admin_settings._GAME_FIELD_ORDER
    assert "game_submit_notify_mode" not in {k for k, _, _ in admin_settings.SETTINGS_FIELDS}


def test_game_group_keyboard_has_mode_toggle(tmp_path):
    _db_ready(tmp_path)
    from handlers import admin_settings
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("game"))
    btns = [b for row in kb.inline_keyboard for b in row if b.callback_data == "toggle_game_submit_notify"]
    assert len(btns) == 1
    assert "Каждую сдачу отдельно" in btns[0].text
    assert "each" not in btns[0].text and "digest" not in btns[0].text

    asyncio.run(db.set_setting("game_submit_notify_mode", "digest"))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("game"))
    btns = [b for row in kb.inline_keyboard for b in row if b.callback_data == "toggle_game_submit_notify"]
    assert "Пачкой (дайджест)" in btns[0].text


def test_toggle_callback_registered_under_settings_capability():
    from handlers.admin_caps import ADMIN_CAPS
    assert ADMIN_CAPS["toggle_game_submit_notify"] == "settings"


def test_toggle_handler_flips_mode_and_answers_with_label(tmp_path):
    _db_ready(tmp_path)
    from handlers import admin_gamification

    class _Msg:
        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            self.text, self.kb = text, reply_markup

    class _Cb:
        def __init__(self):
            self.message = _Msg()
            self.from_user = type("U", (), {"id": ADMIN_ID})()
            self.answers = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append(text)

    cb = _Cb()
    asyncio.run(admin_gamification.toggle_game_submit_notify(cb))
    assert asyncio.run(db.get_setting("game_submit_notify_mode")) == "digest"
    assert cb.answers == ["📥 Сдачи менеджеру: Пачкой (дайджест)"]
    assert "🎮 Геймификация" in cb.message.text
    cb2 = _Cb()
    asyncio.run(admin_gamification.toggle_game_submit_notify(cb2))
    assert asyncio.run(db.get_setting("game_submit_notify_mode")) == "each"
    assert cb2.answers == ["📥 Сдачи менеджеру: Каждую сдачу отдельно"]


# ── Режим «каждую отдельно» (по умолчанию) ────────────────────────────────────

def test_each_mode_sends_immediately_and_queues_nothing(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)

    asyncio.run(_submit(_Bot(), DELEGATE_MSK))

    assert len(calls) == 1
    assert calls[0]["cap"] == "moderate_game"
    assert "Сфоткай &lt;кота&gt;" in calls[0]["text"]
    assert "Иван &lt;Иванов&gt;" in calls[0]["text"]
    assert calls[0]["city"] is None  # модуль городов выключен -> глобально, как раньше
    assert fake.add_calls == []
    assert asyncio.run(db.list_unsent_game_digest(all_cities=True)) == []


def test_each_mode_routes_by_delegate_city_when_cities_on(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _add_delegate(DELEGATE_SPB, "spb", "Пётр")

    asyncio.run(_submit(_Bot(), DELEGATE_SPB))

    assert calls[0]["city"] == "spb"


# ── Режим «пачкой» ────────────────────────────────────────────────────────────

def test_digest_mode_queues_and_arms_one_job_per_city(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    asyncio.run(db.set_setting("game_submit_notify_mode", "digest"))

    asyncio.run(_submit(_Bot(), DELEGATE_MSK, submission_id=1))

    assert calls == []  # ничего не ушло сразу
    rows = asyncio.run(db.list_unsent_game_digest(None))
    assert [(r["submission_id"], r["user_id"], r["task_id"], r["city"]) for r in rows] == [(1, DELEGATE_MSK, 7, None)]
    assert len(fake.add_calls) == 1
    func, trigger, kw = fake.add_calls[0]
    assert func is gd.send_game_digest and trigger == "date"
    assert kw["id"] == "game_digest:all" and kw["replace_existing"] is True and kw["args"] == [None]


def test_digest_mode_rearms_same_job_on_every_submission(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    asyncio.run(db.set_setting("game_submit_notify_mode", "digest"))
    asyncio.run(db.set_setting("game_submit_digest_minutes", "3"))

    asyncio.run(_submit(_Bot(), DELEGATE_MSK, submission_id=1))
    asyncio.run(_submit(_Bot(), DELEGATE_SPB, submission_id=2))

    assert [kw["id"] for _, _, kw in fake.add_calls] == ["game_digest:all", "game_digest:all"]
    first, second = (kw["run_date"] for _, _, kw in fake.add_calls)
    assert second >= first  # окно тишины сдвинулось вперёд
    assert len(asyncio.run(db.list_unsent_game_digest(None))) == 2


def test_digest_mode_city_routing_separate_jobs(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _capture_notify(monkeypatch)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    asyncio.run(db.set_setting("game_submit_notify_mode", "digest"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _add_delegate(DELEGATE_MSK, "msk", "Иванов")
    _add_delegate(DELEGATE_SPB, "spb", "Петрова")

    asyncio.run(_submit(_Bot(), DELEGATE_MSK, submission_id=1))
    asyncio.run(_submit(_Bot(), DELEGATE_SPB, submission_id=2))

    assert sorted(kw["id"] for _, _, kw in fake.add_calls) == ["game_digest:msk", "game_digest:spb"]
    assert [r["user_id"] for r in asyncio.run(db.list_unsent_game_digest("spb"))] == [DELEGATE_SPB]


# ── Текст дайджеста и отправка ────────────────────────────────────────────────

def test_build_digest_text_aggregates_and_escapes():
    text = gd.build_digest_text([("Иванов", 3), ("<b>Петрова</b>", 2), ("Сидоров", 1)])
    assert text == "📥 Новые сдачи: 6 — Иванов ×3, &lt;b&gt;Петрова&lt;/b&gt; ×2, Сидоров → 🎮 Проверка"


def test_send_game_digest_sends_once_per_city_and_marks_rows(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    monkeypatch.setattr(sched, "_bot", _Bot())
    _add_delegate(DELEGATE_MSK, "msk", "Иванов")
    _add_delegate(DELEGATE_SPB, "spb", "<Петрова>")
    now = "2026-08-22 12:00:00"
    for sid in (1, 2, 3):
        asyncio.run(db.enqueue_game_digest(sid, DELEGATE_MSK, 7, "msk", now))
    asyncio.run(db.enqueue_game_digest(4, DELEGATE_SPB, 7, "msk", now))
    asyncio.run(db.enqueue_game_digest(5, DELEGATE_SPB, 8, "spb", now))

    assert asyncio.run(gd.send_game_digest("msk")) == 1

    assert len(calls) == 1
    assert calls[0]["city"] == "msk" and calls[0]["cap"] == "moderate_game"
    assert calls[0]["text"] == "📥 Новые сдачи: 4 — Иванов ×3, &lt;Петрова&gt; → 🎮 Проверка"
    assert asyncio.run(db.list_unsent_game_digest("msk")) == []
    assert len(asyncio.run(db.list_unsent_game_digest("spb"))) == 1  # чужой город не тронут

    # Повторный запуск при пустой очереди — без сообщения.
    assert asyncio.run(gd.send_game_digest("msk")) == 0
    assert len(calls) == 1


def test_send_game_digest_empty_queue_sends_nothing(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = _capture_notify(monkeypatch)
    monkeypatch.setattr(sched, "_bot", _Bot())
    assert asyncio.run(gd.send_game_digest(None)) == 0
    assert calls == []


# ── Ре-арм на старте ──────────────────────────────────────────────────────────

def test_rearm_pending_digests_arms_missing_jobs_only(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    now = "2026-08-22 12:00:00"
    asyncio.run(db.enqueue_game_digest(1, DELEGATE_MSK, 7, "msk", now))
    asyncio.run(db.enqueue_game_digest(2, DELEGATE_SPB, 7, "spb", now))
    asyncio.run(db.enqueue_game_digest(3, DELEGATE_SPB, 7, None, now))
    asyncio.run(db.mark_game_digest_sent([2], now))  # spb уже отправлен
    fake = _FakeScheduler(existing=["game_digest:all"])  # джоба без города пережила рестарт
    monkeypatch.setattr(sched, "_scheduler", fake)

    armed = asyncio.run(gd.rearm_pending_digests())

    assert armed == ["msk"]
    assert [kw["id"] for _, _, kw in fake.add_calls] == ["game_digest:msk"]


def test_rearm_pending_digests_noop_when_queue_empty(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    assert asyncio.run(gd.rearm_pending_digests()) == []
    assert fake.add_calls == []


def test_init_scheduler_calls_rearm_on_boot():
    """Структурно: init_scheduler дёргает rearm_pending_digests ДО resume()."""
    import inspect
    src = inspect.getsource(sched.init_scheduler)
    assert "rearm_pending_digests" in src
    assert src.index("rearm_pending_digests()") < src.index("_scheduler.resume()")


def test_finalize_hook_goes_through_game_digest():
    """user_actions больше не зовёт notify_by_capability для сдач напрямую — только через
    services.game_digest (иначе настройка режима и город молча перестанут действовать)."""
    import inspect
    from handlers import user_actions as ua
    src = inspect.getsource(ua.finalize_game_submission)
    assert "notify_game_submission(" in src
    assert "notify_by_capability(" not in src
