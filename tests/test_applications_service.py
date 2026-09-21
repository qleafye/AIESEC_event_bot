"""Phase 23 План 02 (APP-TINDER-01) — юнит-покрытие `services/applications.py`: ядро отбора
заявок без aiogram (очередь, карточка, атомарные решения, журнал отмены).

pytest-asyncio не используется — асинхронщина через `asyncio.run()`, БД — временная
(`config.DB_PATH = tmp_path / "..."` + `database.db.init_db()`), как в `tests/test_applications_db.py`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import moderation_card
import services.applications as applications
from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "applications_service.db")


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


def _set_resume_type(tid, value):
    """`resume_type` — одна из ПЯТИ колонок резервной цепочки (`database/db.py:818`), НЕ
    участвующих в INSERT `add_user` (пишет её узкий `update_user_answers`, план 28-04/28-05) —
    сеять её через `_seed_user(**fields)` молча не сработает (колонка останется NULL)."""
    _run(db.update_user_answers(tid, {"resume_type": value}, allowed_columns=["resume_type"]))


# ── TRACK_FILTERS ────────────────────────────────────────────────────────────────────────

def test_track_filters_shape():
    assert applications.TRACK_FILTERS["full"] == ("full",)
    assert set(applications.TRACK_FILTERS["party"]) == {"party_overnight", "party_noovernight"}
    assert applications.TRACK_FILTERS["short"] == ("short",)


# ── queue_page: счётчик и выборка по ОДНОМУ набору фильтров ─────────────────────────────────

def test_queue_page_returns_row_and_matching_total(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1001, participant_type="full")
    _seed_user(1002, participant_type="short")

    row, total = _run(applications.queue_page(scope=None, offset=0))
    assert total == 2
    assert row["telegram_id"] == 1001

    row2, total2 = _run(applications.queue_page(scope=None, offset=1))
    assert total2 == 2
    assert row2["telegram_id"] == 1002


def test_queue_page_track_filter_narrows_both_count_and_row(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1101, participant_type="full")
    _seed_user(1102, participant_type="short")

    row, total = _run(applications.queue_page(scope=None, offset=0, track="short"))
    assert total == 1
    assert row["telegram_id"] == 1102


def test_queue_page_offset_past_total_is_empty(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1201, participant_type="full")

    row, total = _run(applications.queue_page(scope=None, offset=5))
    assert row is None
    assert total == 1


def test_queue_page_empty_queue(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())

    row, total = _run(applications.queue_page(scope=None, offset=0))
    assert row is None
    assert total == 0


# ── manager_scope / out_of_scope ─────────────────────────────────────────────────────────

def test_manager_scope_none_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    assert _run(applications.manager_scope("msk")) is None


def test_manager_scope_none_when_city_none(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("event_city_enabled", "on"))
    assert _run(applications.manager_scope(None)) is None


def test_out_of_scope_false_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1301, event_city="spb")
    assert _run(applications.out_of_scope("msk", 1301)) is False


def test_out_of_scope_true_for_mismatched_city(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("event_city_enabled", "on"))
    _seed_user(1401, event_city="spb")
    assert _run(applications.out_of_scope("msk", 1401)) is True


def test_out_of_scope_false_for_matching_city(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("event_city_enabled", "on"))
    _seed_user(1402, event_city="msk")
    assert _run(applications.out_of_scope("msk", 1402)) is False


# ── card_payload ──────────────────────────────────────────────────────────────────────────

def test_card_payload_main_fields_respect_answer_limit(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    long_goal = "о" * 500
    _seed_user(1501, goal=long_goal)
    _run(db.set_setting("modcard_fields", "goal"))
    _run(db.set_setting("modcard_answer_limit", "50"))

    payload = _run(applications.card_payload(_run(db.get_user(1501))))
    main = dict(payload["main_fields"])
    assert len(main) == 1
    value = next(iter(main.values()))
    assert len(value) <= 51  # 50 символов + «…»


def test_card_payload_extra_fields_not_truncated(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    long_goal = "о" * 500
    _seed_user(1502, goal=long_goal)
    _run(db.set_setting("modcard_fields", "age"))  # goal НЕ включён -> extra
    _run(db.set_setting("modcard_answer_limit", "10"))

    payload = _run(applications.card_payload(_run(db.get_user(1502))))
    extra = dict(payload["extra_fields"])
    values = list(extra.values())
    assert any(len(v) == 500 for v in values)


def test_card_payload_resume_kinds(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1601, resume_file_id="file-abc")
    _seed_user(1602, resume_text="текстовое резюме")
    _seed_user(1603)

    p1 = _run(applications.card_payload(_run(db.get_user(1601))))
    p2 = _run(applications.card_payload(_run(db.get_user(1602))))
    p3 = _run(applications.card_payload(_run(db.get_user(1603))))

    assert p1["resume"] == {
        "kind": "file", "file_id": "file-abc", "text": None, "url": None,
        "mini": [], "warning": False,
    }
    assert p2["resume"] == {
        "kind": "text", "file_id": None, "text": "текстовое резюме", "url": None,
        "mini": [], "warning": False,
    }
    assert p3["resume"] == {
        "kind": "none", "file_id": None, "text": None, "url": None,
        "mini": [], "warning": False,
    }


def test_card_payload_resume_link_kind(tmp_path):
    """Приёмка 17.09 (п.2): развилка резюме R2b (СкиллАп 5) — делегат дал ссылку вместо файла,
    карточка обязана показать «kind: link», а не молча падать в «нет резюме»."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1604, resume_link="https://example.com/cv")

    payload = _run(applications.card_payload(_run(db.get_user(1604))))
    assert payload["resume"] == {
        "kind": "link", "file_id": None, "text": None, "url": "https://example.com/cv",
        "mini": [], "warning": False,
    }


def test_card_payload_resume_file_prefers_nextcloud_url(tmp_path):
    """Приёмка 17.09 (п.2): файл резюме есть в Nextcloud (`resume_url`) — карточка отдаёт
    прямую ссылку, а не заставляет фронт идти за токен-эндпоинтом файла."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1605, resume_file_id="file-xyz", resume_url="https://cloud.example.com/s/tok/file.pdf")

    payload = _run(applications.card_payload(_run(db.get_user(1605))))
    assert payload["resume"] == {
        "kind": "file", "file_id": "file-xyz", "text": None,
        "url": "https://cloud.example.com/s/tok/file.pdf",
        "mini": [], "warning": False,
    }


def test_card_payload_resume_priority_file_over_link_over_text(tmp_path):
    """Файл — самый информативный артефакт, поэтому побеждает даже если заодно заполнены
    ссылка/текст (в проде это не встречается — развилка отвечает одной веткой, но карточка
    не должна зависеть от этого предположения)."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(
        1606, resume_file_id="file-priority", resume_link="https://example.com/cv",
        resume_text="текст",
    )

    payload = _run(applications.card_payload(_run(db.get_user(1606))))
    assert payload["resume"]["kind"] == "file"


def test_card_payload_resume_mini_kind(tmp_path):
    """Приёмка 19.09 (review-260919, находки №2/№3 «Модерация»): развилка резюме, ветка
    «мини-профиль» — карточка обязана показать три подполя, а не «нет резюме»."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(
        1608, mini_projects="Бот для АЙСЕК",
        mini_portfolio='[{"title": "GitHub", "description": "github.com/x"}]',
        mini_direction="Бэкенд",
    )
    _set_resume_type(1608, "mini")

    payload = _run(applications.card_payload(_run(db.get_user(1608))))
    resume = payload["resume"]
    assert resume["kind"] == "mini"
    assert resume["warning"] is False
    values = {f["label"]: f["value"] for f in resume["mini"]}
    assert values[moderation_card.CARD_STEPS["mini_projects"]] == "Бот для АЙСЕК"
    assert values[moderation_card.CARD_STEPS["mini_portfolio"]] == "GitHub — github.com/x"
    assert values[moderation_card.CARD_STEPS["mini_direction"]] == "Бэкенд"


def test_card_payload_resume_warning_when_type_set_but_empty(tmp_path):
    """`resume_type` задан (делегат прошёл развилку), но ни один карман не заполнен — маркер
    потери данных, а не тихое «резюме не приложено»."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1609)
    _set_resume_type(1609, "mini")

    payload = _run(applications.card_payload(_run(db.get_user(1609))))
    assert payload["resume"] == {
        "kind": "none", "file_id": None, "text": None, "url": None,
        "mini": [], "warning": True,
    }


def test_card_payload_excludes_mini_resume_steps_from_fields(tmp_path):
    """Три шага мини-профиля не дублируются построчно — у них свой блок `resume.mini`."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1610, mini_projects="Проект")
    _set_resume_type(1610, "mini")
    _run(db.set_setting("modcard_fields", "age\nmini_projects\nmini_portfolio\nmini_direction"))

    payload = _run(applications.card_payload(_run(db.get_user(1610))))
    labels = [label for label, _ in payload["main_fields"]] + [label for label, _ in payload["extra_fields"]]
    assert moderation_card.CARD_STEPS["mini_projects"] not in labels
    assert moderation_card.CARD_STEPS["mini_portfolio"] not in labels
    assert moderation_card.CARD_STEPS["mini_direction"] not in labels


def test_card_payload_age_birth_date_reciprocal_no_cross_section_duplicate(tmp_path):
    """Находка №1 «Модерация»: `birth_date` выбран в карточке, у делегата только `age` (старая
    схема) — main_fields печатает «Возраст», а extra_fields («Показать всё») не повторяет ту же
    строку вторым проходом через `age`."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1611, age=19)
    _run(db.set_setting("modcard_fields", "birth_date"))

    payload = _run(applications.card_payload(_run(db.get_user(1611))))
    main_labels = [label for label, _ in payload["main_fields"]]
    extra_labels = [label for label, _ in payload["extra_fields"]]
    assert moderation_card.CARD_STEPS["age"] in main_labels
    assert moderation_card.CARD_STEPS["age"] not in extra_labels
    assert moderation_card.CARD_STEPS["birth_date"] not in extra_labels


def test_card_payload_excludes_resume_link_step_from_fields(tmp_path):
    """Приёмка 17.09 (п.2): `resume_link` — та же ось, что `resume`, у неё теперь свой блок —
    строкой в main_fields/extra_fields она больше не дублируется."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1607, resume_link="https://example.com/cv")
    _run(db.set_setting("modcard_fields", "age\nresume_link"))

    payload = _run(applications.card_payload(_run(db.get_user(1607))))
    labels = [label for label, _ in payload["main_fields"]] + [label for label, _ in payload["extra_fields"]]
    assert moderation_card.CARD_STEPS["resume_link"] not in labels


def test_card_payload_show_resume_reflects_enabled_steps(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1701)
    _run(db.set_setting("modcard_fields", "age\nresume"))
    assert _run(applications.card_payload(_run(db.get_user(1701))))["show_resume"] is True

    _run(db.set_setting("modcard_fields", "age"))
    assert _run(applications.card_payload(_run(db.get_user(1701))))["show_resume"] is False


def test_card_payload_history_reads_answer_history(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1801)
    _run(db.mark_user_edited(1801, "bot"))

    payload = _run(applications.card_payload(_run(db.get_user(1801))))
    assert isinstance(payload["history"], list)


# ── claim_approve / claim_reject / claim_approve_all: выигрывает ровно один ────────────────

def test_claim_approve_wins_once(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1901, participant_type="full")

    first = _run(applications.claim_approve(1901))
    second = _run(applications.claim_approve(1901))
    assert first is True
    assert second is False
    assert _run(db.get_user(1901))["status"] == "approved"


def test_claim_reject_wins_once(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1902, participant_type="full")

    first = _run(applications.claim_reject(1902))
    second = _run(applications.claim_reject(1902))
    assert first is True
    assert second is False


def test_claim_approve_all_returns_flipped_ids(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1903, participant_type="full")
    _seed_user(1904, participant_type="short")

    ids = _run(applications.claim_approve_all(None))
    assert set(ids) == {1903, 1904}


# ── D-10 (23.1-CONTEXT.md O-2, план 23.1-05): users.approved_at — общая точка правды для
# чата, веба и «Принять всех», старые строки остаются NULL ─────────────────────────────────

def test_claim_approve_stamps_approved_at_chat_and_web_single_path(tmp_path):
    # claim_approve — ОДНО имя для бота (appr_approve) и веба (miniapp/routers/applications.py)
    # — единственный путь одиночного одобрения; approved_at ставит approve_user_atomic.
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1905, participant_type="full")
    assert _run(db.get_user(1905))["approved_at"] is None

    won = _run(applications.claim_approve(1905))
    assert won is True
    row = _run(db.get_user(1905))
    assert row["approved_at"] is not None
    assert row["approved_at"].count("-") == 2  # "YYYY-MM-DD HH:MM:SS", тот же формат, что и registration_date


def test_claim_approve_all_stamps_approved_at_web_mass_path(tmp_path):
    # claim_approve_all — «Принять всех» веб-слоя.
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1906, participant_type="full")
    _seed_user(1907, participant_type="short")

    ids = _run(applications.claim_approve_all(None))
    assert set(ids) == {1906, 1907}
    for tid in (1906, 1907):
        assert _run(db.get_user(tid))["approved_at"] is not None


def test_bot_direct_approve_all_pending_also_stamps_approved_at(tmp_path):
    # Бот (handlers/admin_moderation.py::appr_all_yes) зовёт database.db.approve_all_pending
    # НАПРЯМУЮ, минуя services.applications.claim_approve_all — approved_at обязан приехать
    # и по этому пути (та же атомарная UPDATE, живёт в database.db, а не в этом сервисе).
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1908, participant_type="full")

    ids = _run(db.approve_all_pending(city_scope=None))
    assert ids == [1908]
    assert _run(db.get_user(1908))["approved_at"] is not None


def test_old_rows_approved_at_stays_null_until_approved(tmp_path):
    # Строка не одобрена (или ещё не тронута) — approved_at остаётся NULL, а не пустой строкой.
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1909, participant_type="full")

    ok = _run(applications.claim_reject(1909))
    assert ok is True
    assert _run(db.get_user(1909))["approved_at"] is None


# ── Журнал отмены: record -> undo внутри окна -> flush после окна ──────────────────────────

def test_record_decision_then_undo_inside_window_reverts_and_nothing_flushes(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2001, participant_type="full", status="pending")
    won = _run(applications.claim_approve(2001))
    assert won is True

    now = datetime(2026, 1, 1, 12, 0, 0)
    decision_id = _run(applications.record_decision(2001, "approved", None, 999, now))
    assert decision_id > 0

    enqueued = []
    flushed_before = _run(applications.flush_due_decisions(now, lambda k, p: enqueued.append((k, p))))
    assert flushed_before == 0
    assert enqueued == []

    result = _run(applications.undo_decision(decision_id))
    assert result == {"ok": True, "telegram_id": 2001}
    assert _run(db.get_user(2001))["status"] == "pending"


def test_undo_decision_after_flush_returns_already(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2002, participant_type="full", status="pending")
    _run(applications.claim_approve(2002))

    now = datetime(2026, 1, 1, 12, 0, 0)
    decision_id = _run(applications.record_decision(2002, "approved", None, 999, now))

    later = now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1)
    enqueued = []
    flushed = _run(applications.flush_due_decisions(later, lambda k, p: enqueued.append((k, p))))
    assert flushed == 1
    assert len(enqueued) == 1
    kind, payload = enqueued[0]
    assert kind == "approved"
    assert payload["telegram_id"] == 2002

    result = _run(applications.undo_decision(decision_id))
    assert result == {"ok": False, "reason": "already"}
    assert _run(db.get_user(2002))["status"] == "approved"  # эффекты состоялись, отката нет


def test_wr02_web_approve_undo_before_window_never_credits_referrer(tmp_path):
    """Фикс WR-02 (фаза 32): менеджер одобрил в Mini App, тут же нажал «Отменить» (в пределах
    5-секундного окна) — амбассадор, пригласивший делегата, НЕ должен получить баллы. До
    фикса `claim_approve` начисляло СРАЗУ, откат решения деньги не забирал (D-22 запрещает их
    снять) — приглашённый оставался неодобренным ни секунды, а баллы уже ушли."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("ambassador_referral_coins", "100"))
    _seed_user(3001, participant_type="full", status="approved")  # амбассадор
    _run(db.set_ambassador_flag(3001, active=True, at="2026-01-01 00:00:00"))
    _seed_user(3002, participant_type="full", status="pending", referrer_id=3001)

    won = _run(applications.claim_approve(3002))
    assert won is True
    now = datetime(2026, 1, 1, 12, 0, 0)
    decision_id = _run(applications.record_decision(3002, "approved", None, 999, now))

    result = _run(applications.undo_decision(decision_id))
    assert result == {"ok": True, "telegram_id": 3002}
    assert _run(db.get_user(3002))["status"] == "pending"

    # Ни одного начисления — ни в момент approve, ни после отмены.
    assert _run(db.get_referral_credit(3002)) is None
    assert _run(db.get_balance(3001)) == 0


def test_wr02_web_approve_flush_after_window_credits_referrer_once(tmp_path):
    """Симметричный случай: то же одобрение, но окно истекло БЕЗ отмены — начисление
    происходит в `flush_due_decisions`, ровно один раз."""
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("ambassador_referral_coins", "100"))
    _seed_user(3003, participant_type="full", status="approved")  # амбассадор
    _run(db.set_ambassador_flag(3003, active=True, at="2026-01-01 00:00:00"))
    _seed_user(3004, participant_type="full", status="pending", referrer_id=3003)

    assert _run(applications.claim_approve(3004)) is True
    now = datetime(2026, 1, 1, 12, 0, 0)
    _run(applications.record_decision(3004, "approved", None, 999, now))
    assert _run(db.get_referral_credit(3004)) is None  # ещё не пережило окно

    later = now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1)
    enqueued = []
    flushed = _run(applications.flush_due_decisions(later, lambda k, p: enqueued.append((k, p))))
    assert flushed == 1

    credit = _run(db.get_referral_credit(3004))
    assert credit is not None and credit["coins"] == 100
    assert _run(db.get_balance(3003)) == 100

    # Повторный сбор той же (уже неживой) строки ничего не начисляет дважды.
    flushed_again = _run(applications.flush_due_decisions(later, lambda k, p: enqueued.append((k, p))))
    assert flushed_again == 0
    assert _run(db.get_balance(3003)) == 100


def test_undo_decision_unknown_id_returns_already(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    result = _run(applications.undo_decision(999999))
    assert result == {"ok": False, "reason": "already"}


def test_flush_due_decisions_skips_not_yet_due(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2003, participant_type="full", status="pending")
    _run(applications.claim_approve(2003))

    now = datetime(2026, 1, 1, 12, 0, 0)
    _run(applications.record_decision(2003, "approved", None, 999, now))

    enqueued = []
    flushed = _run(applications.flush_due_decisions(now, lambda k, p: enqueued.append((k, p))))
    assert flushed == 0
    assert enqueued == []


# ── reject_message_text / reject_reason_templates ───────────────────────────────────────────

def test_reject_message_text_default_prefix_and_reason(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    text = _run(applications.reject_message_text("плохое качество"))
    assert text == "К сожалению, твоя заявка отклонена.\n\nплохое качество"


def test_reject_message_text_escapes_html(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("reject_text", "<b>Отказ</b>"))
    text = _run(applications.reject_message_text("<script>"))
    assert "<b>" not in text
    assert "&lt;b&gt;Отказ&lt;/b&gt;" in text
    assert "&lt;script&gt;" in text


def test_reject_message_text_no_reason_no_trailing_blank(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    text = _run(applications.reject_message_text(None))
    assert text == "К сожалению, твоя заявка отклонена."


def test_reject_reason_templates_default_has_four_items(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    templates = _run(applications.reject_reason_templates())
    assert len(templates) == 4
    assert all(isinstance(t, str) and t for t in templates)


# ── UNDO_WINDOW_SECONDS ──────────────────────────────────────────────────────────────────

def test_undo_window_seconds_is_five():
    assert applications.UNDO_WINDOW_SECONDS == 5


# ── last_rejection_reason (quick 260904-liz) ────────────────────────────────────────────────

def test_last_rejection_reason_none_when_no_decisions(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2101, status="pending")
    assert _run(applications.last_rejection_reason(2101)) is None


def test_last_rejection_reason_none_when_last_decision_is_approval(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2102, status="pending")
    assert _run(applications.claim_approve(2102))
    now = datetime(2026, 1, 1, 12, 0, 0)
    _run(applications.record_decision(2102, "approved", None, 999, now))
    assert _run(applications.last_rejection_reason(2102)) is None


def test_last_rejection_reason_none_when_reason_empty(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2103, status="pending")
    assert _run(applications.claim_reject(2103))
    now = datetime(2026, 1, 1, 12, 0, 0)
    _run(applications.record_decision(2103, "rejected", "  ", 999, now))
    assert _run(applications.last_rejection_reason(2103)) is None


def test_last_rejection_reason_none_when_rejection_undone(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2104, status="pending")
    assert _run(applications.claim_reject(2104))
    now = datetime(2026, 1, 1, 12, 0, 0)
    decision_id = _run(applications.record_decision(2104, "rejected", "Не подходит", 999, now))
    result = _run(applications.undo_decision(decision_id))
    assert result["ok"] is True
    assert _run(applications.last_rejection_reason(2104)) is None


def test_last_rejection_reason_returns_reason_of_live_rejection(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2105, status="pending")
    assert _run(applications.claim_reject(2105))
    now = datetime(2026, 1, 1, 12, 0, 0)
    _run(applications.record_decision(2105, "rejected", "Не хватает опыта", 999, now))
    assert _run(applications.last_rejection_reason(2105)) == "Не хватает опыта"
