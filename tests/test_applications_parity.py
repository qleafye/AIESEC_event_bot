"""Phase 23 План 02 (APP-TINDER-01) — снимок поведения решений бота ДО переноса в
`services/applications.py`/`services/application_effects.py`.

До задачи 2 модуль `services.applications` не существует — импорт на уровне модуля падает
`ModuleNotFoundError`, это Wave 0 RED-снимок плана (как `tests/test_settings_ops.py` фазы 22).

Эталоны (тексты, порядок вызовов) сняты ДОСЛОВНО из `handlers/admin_moderation.py` (HEAD
до переноса): `appr_approve` (330-349), `appr_reject_reason` (382-405), `_welcome_flipped`/
`appr_all_yes` (457-532), `_render_application_card` (120-192, подписи треков строки 137-142).

pytest-asyncio не используется — асинхронщина через `asyncio.run()`, БД — временная
(`config.DB_PATH = tmp_path / "..."` + `database.db.init_db()`), как в `tests/test_applications_db.py`.
"""
from __future__ import annotations

import asyncio
import html as html_module
import inspect
import pathlib
from datetime import datetime, timedelta

import moderation_card
import reg_engine
import services.application_effects as application_effects
import services.applications as applications
from config import config
from database import db
from tests.test_miniapp_labels_drift import _loaded_aiogram

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "applications_parity.db")


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, **fields):
    row = {
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-01-01 00:00:{tid % 60:02d}",
    }
    row.update(fields)
    _run(db.add_user(row))
    status = fields.get("status", "pending")
    _run(db.set_user_status(tid, status))


# ── Ядро без aiogram (T-23-06 / D-... форма settings_ops.py) ────────────────────────────────

def test_applications_module_does_not_load_aiogram():
    loaded = _loaded_aiogram("import services.applications")
    assert loaded == [], f"services.applications потянул aiogram: {loaded}"


# ── Сторож дрейфа кодов трека (23-01, T-23-05: _track_clause литералы vs reg_engine) ────────

def test_track_filter_codes_match_reg_engine_canonical_set():
    all_codes = set()
    for codes in applications.TRACK_FILTERS.values():
        all_codes.update(codes)
    expected = set(reg_engine.PARTY_TRACK_CODES) | {reg_engine.SHORT_TRACK}
    assert all_codes == expected


# ── Подписи треков — литералы карточки бота ──────────────────────────────────────────────

def test_track_labels_are_the_bot_card_literals():
    assert applications.TRACK_LABELS["party_overnight"] == "🎉 Трек: вечеринка с ночёвкой"
    assert applications.TRACK_LABELS["party_noovernight"] == "🎉 Трек: вечеринка без ночёвки"
    assert applications.TRACK_LABELS["short"] == "⚡ Трек: краткая анкета (акция)"
    assert "full" not in applications.TRACK_LABELS  # full не печатает строку трека вовсе


# ── Одобрение: approve_user -> update_status_in_sheet, в этом порядке, ровно один раз ───────

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


def test_apply_decision_effects_approved_calls_welcome_then_sheet(monkeypatch):
    calls = []

    async def fake_approve_user(bot, tid):
        calls.append(("approve_user", tid))

    async def fake_update_status_in_sheet(tid, label):
        calls.append(("update_status_in_sheet", tid, label))

    import handlers.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)
    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    bot = _FakeBot()
    _run(application_effects.apply_decision_effects(bot, 501, "approved"))

    assert calls == [("approve_user", 501), ("update_status_in_sheet", 501, "Одобрена")]


def test_apply_decision_effects_approved_welcome_exactly_once(monkeypatch):
    count = {"n": 0}

    async def fake_approve_user(bot, tid):
        count["n"] += 1

    async def fake_update_status_in_sheet(tid, label):
        return None

    import handlers.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)
    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    _run(application_effects.apply_decision_effects(_FakeBot(), 502, "approved"))
    assert count["n"] == 1


# ── Отказ: ровно одно сообщение, точный текст, порядок, лист ────────────────────────────────

def test_apply_decision_effects_rejected_sends_one_message_then_sheet(monkeypatch):
    calls = []

    async def fake_update_status_in_sheet(tid, label):
        calls.append(("update_status_in_sheet", tid, label))

    async def fake_get_setting(key):
        assert key == "reject_text"
        return None  # дефолт

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)
    monkeypatch.setattr(applications, "get_setting", fake_get_setting)

    class _TrackingBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(("send_message", chat_id, text, kwargs))

    _run(application_effects.apply_decision_effects(_TrackingBot(), 503, "rejected", reason="Не подходит"))

    expected_text = (
        html_module.escape("К сожалению, твоя заявка отклонена.")
        + "\n\n" + html_module.escape("Не подходит")
    )
    assert calls[0] == ("send_message", 503, expected_text, {"parse_mode": "HTML"})
    assert calls[1] == ("update_status_in_sheet", 503, "Отклонена")
    assert len([c for c in calls if c[0] == "send_message"]) == 1


def test_apply_decision_effects_rejected_send_failure_does_not_block_sheet(monkeypatch):
    calls = []

    async def fake_update_status_in_sheet(tid, label):
        calls.append(("update_status_in_sheet", tid, label))

    async def fake_get_setting(key):
        return None

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)
    monkeypatch.setattr(applications, "get_setting", fake_get_setting)

    class _RaisingBot:
        async def send_message(self, *a, **k):
            raise RuntimeError("blocked by user")

    # решение НЕ откатывается сбоем отправки — лист всё равно обновляется (паритет с ботом).
    _run(application_effects.apply_decision_effects(_RaisingBot(), 504, "rejected", reason="х"))
    assert calls == [("update_status_in_sheet", 504, "Отклонена")]


# ── Массовое одобрение: welcome-рассылка + один bulk-sync, паритет с _welcome_flipped ────────

def test_mass_approve_effects_empty_list_no_calls(monkeypatch):
    calls = []

    async def fake_approve_user(bot, tid):
        calls.append(tid)

    async def fake_bulk(mapping):
        calls.append(("bulk", mapping))

    import handlers.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)
    monkeypatch.setattr(application_effects, "bulk_update_status_in_sheet", fake_bulk)

    _run(application_effects.mass_approve_effects(_FakeBot(), []))
    assert calls == []


def test_mass_approve_effects_welcomes_all_then_one_bulk_sync(monkeypatch):
    calls = []

    async def fake_approve_user(bot, tid):
        calls.append(("welcome", tid))

    async def fake_bulk(mapping):
        calls.append(("bulk", mapping))

    async def fake_sleep(_seconds):
        return None

    import handlers.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)
    monkeypatch.setattr(application_effects, "bulk_update_status_in_sheet", fake_bulk)
    monkeypatch.setattr(application_effects.asyncio, "sleep", fake_sleep)

    _run(application_effects.mass_approve_effects(_FakeBot(), [601, 602, 603]))

    welcomes = [c for c in calls if c[0] == "welcome"]
    assert welcomes == [("welcome", 601), ("welcome", 602), ("welcome", 603)]
    assert calls[-1] == ("bulk", {"601": "Одобрена", "602": "Одобрена", "603": "Одобрена"})


def test_mass_approve_effects_retry_after_retries_once_others_continue(monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter

    attempts = {}
    calls = []

    async def fake_approve_user(bot, tid):
        attempts[tid] = attempts.get(tid, 0) + 1
        if tid == 701 and attempts[tid] == 1:
            raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
        calls.append(tid)

    async def fake_bulk(mapping):
        calls.append(("bulk", mapping))

    async def fake_sleep(_seconds):
        return None

    import handlers.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)
    monkeypatch.setattr(application_effects, "bulk_update_status_in_sheet", fake_bulk)
    monkeypatch.setattr(application_effects.asyncio, "sleep", fake_sleep)

    _run(application_effects.mass_approve_effects(_FakeBot(), [701, 702]))

    assert attempts[701] == 2  # одна повторная попытка
    assert 701 in calls and 702 in calls


# ── Карточка: единая схема вопросов, согласие последним, резюме гасится show_resume ─────────

def test_card_payload_main_and_extra_do_not_overlap(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(801, education_status="Студент", city="Москва", age="20", goal="цель")
    _run(db.set_setting("modcard_fields", "age\ncity"))

    payload = _run(applications.card_payload(_run(db.get_user(801))))

    main_labels = {label for label, _ in payload["main_fields"]}
    extra_labels = {label for label, _ in payload["extra_fields"]}
    assert main_labels & extra_labels == set()
    assert main_labels  # age/city answered -> оказались в main
    assert extra_labels  # goal ответили, но не входит в modcard_fields -> в extra


def test_card_payload_prev_season_badge_before_edited_and_track_badges_precede_it(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(804, participant_type="short", prev_season="legacy")
    _run(db.set_setting("modcard_fields", "age"))

    payload = _run(applications.card_payload(_run(db.get_user(804))))
    kinds = [b["kind"] for b in payload["badges"]]
    assert kinds.index("track") < kinds.index("prev_season")
    track_badge = next(b for b in payload["badges"] if b["kind"] == "track")
    assert track_badge["text"] == applications.TRACK_LABELS["short"]
    prev_badge = next(b for b in payload["badges"] if b["kind"] == "prev_season")
    assert prev_badge["text"] == "🔁 Повторный: был(а) на прошлом событии"


def test_card_payload_show_resume_false_when_step_disabled(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(802, resume_text="мой опыт")
    _run(db.set_setting("modcard_fields", "age"))  # resume не включён

    payload = _run(applications.card_payload(_run(db.get_user(802))))
    assert payload["show_resume"] is False
    assert payload["resume"]["kind"] == "text"  # данные есть, решение гасить — за клиентом


def test_card_payload_empty_resume_is_kind_none(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(803)

    payload = _run(applications.card_payload(_run(db.get_user(803))))
    assert payload["resume"] == {"kind": "none", "file_id": None, "text": None}


# ── prev_reject_line / карточка «Ранее отклонена: …» (quick 260904-liz) ─────────────────────

def test_card_payload_prev_reject_badge_after_resubmit_before_consent(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(810)
    assert _run(applications.claim_reject(810))
    _run(applications.record_decision(810, "rejected", "Мало опыта", 999, datetime(2026, 1, 1)))
    _run(db.mark_user_edited(810, "bot"))
    _run(db.record_answer_history(
        810, [{"column": "status", "old": "rejected", "new": "pending"}], "bot",
    ))
    _run(db.set_user_status(810, "pending"))

    payload = _run(applications.card_payload(_run(db.get_user(810))))
    kinds = [b["kind"] for b in payload["badges"]]
    assert "resubmit" in kinds and "prev_reject" in kinds
    assert kinds.index("resubmit") < kinds.index("prev_reject")
    prev_badge = next(b for b in payload["badges"] if b["kind"] == "prev_reject")
    assert prev_badge["text"] == "🚫 Ранее отклонена: Мало опыта"
    # согласие (если бы было) всегда последним — здесь его нет, но prev_reject не последний
    # элемент структуры вслепую: убеждаемся хотя бы, что не идёт ПЕРЕД resubmit.


def test_card_payload_no_prev_reject_badge_without_past_rejection(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(811)
    payload = _run(applications.card_payload(_run(db.get_user(811))))
    assert "prev_reject" not in [b["kind"] for b in payload["badges"]]


def test_card_payload_no_prev_reject_badge_when_reason_empty(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(812)
    assert _run(applications.claim_reject(812))
    _run(applications.record_decision(812, "rejected", "", 999, datetime(2026, 1, 1)))
    payload = _run(applications.card_payload(_run(db.get_user(812))))
    assert "prev_reject" not in [b["kind"] for b in payload["badges"]]


def test_card_payload_no_prev_reject_badge_when_last_decision_is_approval(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(813)
    assert _run(applications.claim_reject(813))
    _run(applications.record_decision(813, "rejected", "Не подходит", 999, datetime(2026, 1, 1)))
    _run(db.set_user_status(813, "pending"))  # имитация повторной подачи
    assert _run(applications.claim_approve(813))
    _run(applications.record_decision(813, "approved", None, 999, datetime(2026, 1, 2)))
    payload = _run(applications.card_payload(_run(db.get_user(813))))
    assert "prev_reject" not in [b["kind"] for b in payload["badges"]]


def test_card_payload_no_prev_reject_badge_when_rejection_undone(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(814)
    assert _run(applications.claim_reject(814))
    decision_id = _run(applications.record_decision(814, "rejected", "Не подходит", 999, datetime(2026, 1, 1)))
    result = _run(applications.undo_decision(decision_id))
    assert result["ok"] is True
    payload = _run(applications.card_payload(_run(db.get_user(814))))
    assert "prev_reject" not in [b["kind"] for b in payload["badges"]]


def test_prev_reject_line_none_without_telegram_id():
    assert _run(applications.prev_reject_line({})) is None


def test_prev_reject_line_escapes_reason_only_when_flagged(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(815)
    assert _run(applications.claim_reject(815))
    _run(applications.record_decision(815, "rejected", "<script>плохо</script>", 999, datetime(2026, 1, 1)))
    user = _run(db.get_user(815))

    plain = _run(applications.prev_reject_line(user))
    escaped = _run(applications.prev_reject_line(user, escape_reason=True))
    assert plain == "🚫 Ранее отклонена: <script>плохо</script>"
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


def test_render_application_card_prints_prev_reject_between_resubmit_and_consent():
    import handlers.admin_moderation as admin_moderation
    user = {"telegram_id": 1, "full_name": "Иван"}
    text = admin_moderation._render_application_card(
        user, 1, 1,
        consent_line="Согласие: v1",
        resubmit_line="🔁 Повторная подача",
        prev_reject_line="🚫 Ранее отклонена: Мало опыта",
    )
    lines = text.splitlines()
    i_resubmit = lines.index("🔁 Повторная подача")
    i_prev = lines.index("🚫 Ранее отклонена: Мало опыта")
    i_consent = lines.index("Согласие: v1")
    assert i_resubmit < i_prev < i_consent


# ── Сквозные сторожа паритета «бот ↔ приложение» (план 23-06, T-23-28) ──────────────────────
#
# Веб-путь здесь собран из тех же звеньев, что реально образуют production pipeline
# (miniapp/routers/applications.py::_decide -> record_decision -> miniapp/outbox.py::
# flush_application_decisions -> services/miniapp_outbox.py::drain -> apply_decision_effects/
# mass_approve_effects) — не второй, укороченный путь. `effects_due_at` в прошлом (вместо
# ожидания UNDO_WINDOW_SECONDS) — та же техника, что `_expire` в tests/test_miniapp_applications.py.

async def _outbox_row_count() -> int:
    async with db._connect() as conn:
        async with conn.execute("SELECT COUNT(*) FROM miniapp_outbox") as cur:
            row = await cur.fetchone()
            return row[0]


def _due_now() -> datetime:
    """`record_decision(..., now=...)` считает `effects_due_at = now + UNDO_WINDOW_SECONDS` —
    подставляя `now` в прошлом, получаем решение, уже просроченное к моменту flush."""
    return datetime.now() - timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 5)


def test_bot_and_web_reach_same_state(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1901, full_name="Bot Path")
    _seed_user(1902, full_name="Web Path")

    calls = []

    async def fake_approve_user(bot, tid):
        calls.append(("approve_user", tid))

    async def fake_update_status_in_sheet(tid, label):
        calls.append(("update_status_in_sheet", tid, label))

    import handlers.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)
    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    bot = _FakeBot()

    # ── бот: тот же путь, что appr_approve — claim -> apply_decision_effects СРАЗУ ──────────
    assert _run(applications.claim_approve(1901))
    _run(application_effects.apply_decision_effects(bot, 1901, "approved"))

    # ── веб: claim -> record_decision (окно уже истекло) -> miniapp_outbox drain ────────────
    assert _run(applications.claim_approve(1902))
    _run(applications.record_decision(1902, "approved", None, 999, _due_now()))

    import miniapp.outbox as web_outbox
    import services.miniapp_outbox as bot_outbox
    _run(web_outbox.flush_application_decisions(datetime.now()))
    _run(bot_outbox.drain(bot))

    welcome_calls = {c[1] for c in calls if c[0] == "approve_user"}
    sheet_calls = {tuple(c) for c in calls if c[0] == "update_status_in_sheet"}
    assert welcome_calls == {1901, 1902}
    assert sheet_calls == {
        ("update_status_in_sheet", 1901, "Одобрена"),
        ("update_status_in_sheet", 1902, "Одобрена"),
    }
    assert _run(db.get_user(1901))["status"] == "approved"
    assert _run(db.get_user(1902))["status"] == "approved"


def test_bot_and_web_reject_reach_same_state_with_identical_text(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1911, full_name="Bot Path")
    _seed_user(1912, full_name="Web Path")

    async def fake_update_status_in_sheet(tid, label):
        return None

    async def fake_get_setting(key):
        assert key == "reject_text"
        return None  # дефолт

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)
    monkeypatch.setattr(applications, "get_setting", fake_get_setting)

    sent: dict[int, tuple[str, dict]] = {}

    class _TrackingBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent[chat_id] = (text, kwargs)

    bot = _TrackingBot()
    reason = "Не хватает опыта"

    # ── бот ──────────────────────────────────────────────────────────────────────────────
    assert _run(applications.claim_reject(1911))
    _run(application_effects.apply_decision_effects(bot, 1911, "rejected", reason))

    # ── веб ──────────────────────────────────────────────────────────────────────────────
    assert _run(applications.claim_reject(1912))
    _run(applications.record_decision(1912, "rejected", reason, 999, _due_now()))

    import miniapp.outbox as web_outbox
    import services.miniapp_outbox as bot_outbox
    _run(web_outbox.flush_application_decisions(datetime.now()))
    _run(bot_outbox.drain(bot))

    text_bot, kwargs_bot = sent[1911]
    text_web, kwargs_web = sent[1912]
    # D-05: символ в символ один и тот же текст, кто бы ни принял решение.
    assert text_bot == text_web
    assert kwargs_bot == kwargs_web == {"parse_mode": "HTML"}


def test_web_reject_without_reason_has_no_hanging_separator_or_none_word():
    """Причина по желанию (D-05) — веб-роутер приводит пустую строку к `None`
    (`miniapp/routers/applications.py::applications_reject`), `reject_message_text` обязана
    отдать ТОЛЬКО префикс без висящего «\\n\\n» и без слова `None` в тексте делегату."""
    text = _run(applications.reject_message_text(None))
    assert not text.endswith("\n\n")
    assert "None" not in text
    assert text == html_module.escape("К сожалению, твоя заявка отклонена.")


def test_bot_reject_writes_journal_without_second_send(tmp_path, monkeypatch):
    """Quick 260904-liz (T-liz-04): бот-путь (claim_reject -> apply_decision_effects ->
    record_decision(..., effects_already_sent=True)) оставляет ровно одну строку журнала,
    заполненный effects_sent_at — и `flush_due_decisions` даже спустя окно отмены её НЕ
    забирает: 0 забранных, колбэк не вызван, ни одной новой строки в miniapp_outbox. Делегат
    получает сообщение об отказе РОВНО ОДИН раз — то, что уже отправил apply_decision_effects
    синхронно, ниже."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1971, full_name="Bot Reject Path")

    async def fake_update_status_in_sheet(tid, label):
        return None

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    bot = _FakeBot()
    reason = "Анкета неполная"

    assert _run(applications.claim_reject(1971))
    _run(application_effects.apply_decision_effects(bot, 1971, "rejected", reason))
    decision_id = _run(applications.record_decision(
        1971, "rejected", reason, 999, datetime.now(), effects_already_sent=True,
    ))

    row = _run(applications.get_decision(decision_id))
    assert row["reason"] == reason
    assert row["effects_sent_at"] is not None

    enqueued = []
    far_future = datetime.now() + timedelta(days=1)
    flushed = _run(applications.flush_due_decisions(far_future, lambda k, p: enqueued.append((k, p))))
    assert flushed == 0
    assert enqueued == []
    assert _run(_outbox_row_count()) == 0

    assert len(bot.sent) == 1  # ровно одна отправка делегату — от apply_decision_effects


def test_appr_reject_reason_records_journal_without_reason_in_log():
    """T-liz-01 (Info disclosure): сторож самого хендлера — `record_decision`/
    `effects_already_sent` в теле функции ЕСТЬ, а `reason={reason!r}` (утечка ПД в лог) НЕТ.
    Проверяет исходники, а не только примитивы (тест выше), чтобы правка не откатилась тихо
    следующим рефакторингом."""
    import handlers.admin_moderation as admin_moderation
    source = inspect.getsource(admin_moderation.appr_reject_reason)
    assert "record_decision" in source
    assert "effects_already_sent" in source
    assert "reason={reason!r}" not in source


def test_undo_leaves_no_trace(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1921)

    calls = []

    async def fake_update_status_in_sheet(tid, label):
        calls.append((tid, label))

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    assert _run(applications.claim_approve(1921))
    decision_id = _run(applications.record_decision(1921, "approved", None, 999, datetime.now()))

    result = _run(applications.undo_decision(decision_id))
    assert result == {"ok": True, "telegram_id": 1921}

    assert _run(db.get_user(1921))["status"] == "pending"
    row = _run(applications.get_decision(decision_id))
    assert row["undone_at"] is not None
    assert row["effects_sent_at"] is None

    # эффекты не ушли — ни один хвост не позвал лист, ни одной строки в outbox не появилось.
    import miniapp.outbox as web_outbox
    _run(web_outbox.flush_application_decisions(datetime.now()))
    assert calls == []
    assert _run(_outbox_row_count()) == 0


def test_mass_approve_parity(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    # Два захода по два делегата — вторая пара сидируется ПОСЛЕ первого одобрения, иначе
    # атомарный `approve_all_pending` (общий и для бота, и для веба) заберёт все четыре сразу
    # и второму заходу нечего будет одобрять.
    _seed_user(1931)
    _seed_user(1932)

    calls = []

    async def fake_approve_user(bot, tid):
        calls.append(("welcome", tid))

    async def fake_bulk(mapping):
        calls.append(("bulk", mapping))

    import handlers.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)
    monkeypatch.setattr(application_effects, "bulk_update_status_in_sheet", fake_bulk)

    # ── бот: appr_all_yes зовёт database.db.approve_all_pending напрямую ────────────────────
    ids_bot = _run(db.approve_all_pending(city_scope=None))
    _run(application_effects.mass_approve_effects(_FakeBot(), ids_bot))

    # ── веб: claim_approve_all — тонкая обёртка над ТОЙ ЖЕ approve_all_pending ──────────────
    _seed_user(1941)
    _seed_user(1942)
    ids_web = _run(applications.claim_approve_all(None))
    _run(application_effects.mass_approve_effects(_FakeBot(), ids_web))

    assert set(ids_bot) == {1931, 1932}
    assert set(ids_web) == {1941, 1942}

    welcomed = {tid for kind, tid in calls if kind == "welcome"}
    assert welcomed == {1931, 1932, 1941, 1942}

    bulk_calls = [payload for kind, payload in calls if kind == "bulk"]
    # один batch-вызов на КАЖДЫЙ заход (не построчно) — паритет с _welcome_flipped бота.
    assert len(bulk_calls) == 2
    assert bulk_calls[0] == {"1931": "Одобрена", "1932": "Одобрена"}
    assert bulk_calls[1] == {"1941": "Одобрена", "1942": "Одобрена"}


def test_no_second_source_of_truth():
    """T-23-28: единственный вызывающий `approve_user`/`update_status_in_sheet`/
    `bulk_update_status_in_sheet` — `services/application_effects.py`. Ни веб-роутер, ни
    бот-хендлер не держат собственной копии хвоста решения."""
    forbidden = ("approve_user(", "update_status_in_sheet(", "bulk_update_status_in_sheet(")
    for rel_path in ("miniapp/routers/applications.py", "handlers/admin_moderation.py"):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        clean = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
        for name in forbidden:
            assert name not in clean, f"{rel_path}: второй источник правды — {name}"


def test_card_fields_same_registry_key_drives_both_surfaces(tmp_path):
    """D-01: один ключ реестра (`modcard_fields`) задаёт набор вопросов И карточке бота
    (`handlers/admin_moderation.py::_show_current_card`/`appr_full`), И карточке веба
    (`services/applications.py::card_payload`) — обе стороны читают его через ОДНУ функцию
    `moderation_card.enabled_steps`, второго набора вопросов не существует."""
    for rel_path in ("handlers/admin_moderation.py",):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        assert 'moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))' in text

    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1951, age="20", city="Москва")

    _run(db.set_setting("modcard_fields", "age"))
    main_before = {label for label, _ in _run(applications.card_payload(_run(db.get_user(1951))))["main_fields"]}

    _run(db.set_setting("modcard_fields", "age\ncity"))
    main_after = {label for label, _ in _run(applications.card_payload(_run(db.get_user(1951))))["main_fields"]}

    assert main_after - main_before == {moderation_card.CARD_STEPS["city"]}


def test_file_scope_matches_service_scope(tmp_path):
    """T-23-05 (сторож дрейфа, оставленный планом 23-05): правило городского скоупа временно
    живёт в двух местах — `miniapp/routers/files.py::_city_matches` и
    `services/applications.py::out_of_scope` (D-14). На таблице случаев (модуль выключен /
    менеджер без привязки / города совпадают / расходятся) оба места обязаны давать
    согласованный ответ (`_city_matches == not out_of_scope`)."""
    from miniapp.deps import Principal
    from miniapp.routers.files import _city_matches

    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1961, event_city="spb")
    _seed_user(1962, event_city="msk")

    cases = [
        # (event_city_enabled, manager_city, delegate_tid)
        ("off", "spb", 1961),
        ("off", None, 1961),
        ("on", None, 1961),   # менеджер без привязки — модуль включён, но видит всех
        ("on", "spb", 1961),  # привязка совпадает с городом делегата
        ("on", "spb", 1962),  # привязка расходится с городом делегата
    ]
    for enabled, manager_city, delegate_tid in cases:
        _run(db.set_setting("event_city_enabled", enabled))
        p = Principal(telegram_id=1, via="cookie", caps=frozenset({"moderate_reg"}), city=manager_city)
        user = _run(db.get_user(delegate_tid))
        in_scope_files = _run(_city_matches(p, user))
        out_of_scope_service = _run(applications.out_of_scope(manager_city, delegate_tid))
        assert in_scope_files == (not out_of_scope_service), (enabled, manager_city, delegate_tid)
