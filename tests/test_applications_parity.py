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

import reg_engine
import services.application_effects as application_effects
import services.applications as applications
from config import config
from database import db
from tests.test_miniapp_labels_drift import _loaded_aiogram


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
