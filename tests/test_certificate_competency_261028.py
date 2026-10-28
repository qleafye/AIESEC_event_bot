"""Сертификат компетенций: шкалы из теста компетенций в порядке макета, кому положен,
рендер поверх шаблона, один вариант «все дни» в админке."""
from __future__ import annotations

import asyncio
import io
import sqlite3

from pypdf import PdfReader, PdfWriter

from config import config
from database import certificates_db as cdb
from database import db, quiz_db, session_enroll_db
from domain.cities import default_city_code
from services.forum import certificates as certs
from services.forum.certificate_render import COMPETENCY_NAME_BOX, render_certificate

UID = 261028301


def _run(coro):
    return asyncio.run(coro)


def _template() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _env(tmp_path, checked_in=True):
    config.DB_PATH = str(tmp_path / "comp.db")
    _run(db.init_db())
    _run(db.add_user({"telegram_id": UID, "full_name": "Иванова Анна", "registration_date": "2026-01-01",
                      "event_city": "msk"}))
    if checked_in:
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
                     "VALUES (?, 'entry', '2026-10-30 10:00:00', 'scan', '2026-10-30 10:00:00', '2026-10-30')", (UID,))
        conn.commit()
        conn.close()


def _finish_test(points: dict[str, int]):
    """Тест с компетенциями в порядке прода (не в порядке макета) и законченная попытка."""
    city = default_city_code()
    quiz = _run(quiz_db.get_or_create_quiz(city))
    scores = {}
    for name in ("Критическое и аналитическое мышление", "Командная работа и сотрудничество",
                 "Коммуникация и самопрезентация", "Эмоциональный интеллект",
                 "Адаптивность и устойчивость к изменениям"):
        cid = _run(session_enroll_db.create_competency(city, name))
        scores[cid] = {"points": points[name.split()[0]], "max": 4, "level_id": None}
    attempt = _run(quiz_db.create_attempt(UID, quiz["id"], quiz.get("content_version") or 1))
    _run(quiz_db.finish_attempt(attempt, scores))


def test_percents_follow_template_order(tmp_path):
    _env(tmp_path)
    assert _run(certs.competency_percents(UID)) is None
    _finish_test({"Критическое": 4, "Командная": 1, "Коммуникация": 2, "Эмоциональный": 0, "Адаптивность": 3})
    # макет: критическое, адаптивность, коммуникация, командная работа, эмоциональный интеллект
    assert _run(certs.competency_percents(UID)) == [100, 75, 50, 25, 0]


def test_entitled_only_when_checked_in_and_test_done(tmp_path):
    _env(tmp_path)
    assert (cdb.KIND_COMPETENCY, cdb.VARIANT_ALL) not in _run(certs.entitlements(UID, None))
    _finish_test({"Критическое": 4, "Командная": 4, "Коммуникация": 4, "Эмоциональный": 4, "Адаптивность": 4})
    assert (cdb.KIND_COMPETENCY, cdb.VARIANT_ALL) in _run(certs.entitlements(UID, None))


def test_test_done_but_not_checked_in_gets_nothing(tmp_path):
    _env(tmp_path, checked_in=False)
    _finish_test({"Критическое": 4, "Командная": 4, "Коммуникация": 4, "Эмоциональный": 4, "Адаптивность": 4})
    assert _run(certs.entitlements(UID, None)) == []


def test_render_adds_bars_image():
    pdf = render_certificate(_template(), "Иванова Анна", COMPETENCY_NAME_BOX, [80, 0, 100, 5, 50])
    xobjects = PdfReader(io.BytesIO(pdf)).pages[0]["/Resources"]["/XObject"]
    assert "/CertName" in xobjects and "/CertBars" in xobjects
    plain = render_certificate(_template(), "Иванова Анна")
    assert "/CertBars" not in PdfReader(io.BytesIO(plain)).pages[0]["/Resources"]["/XObject"]


def test_competency_has_single_variant():
    assert certs.kind_variants(cdb.KIND_COMPETENCY, ["all", "2026-10-30"]) == ["all"]
    assert certs.kind_variants(cdb.KIND_PARTICIPANT, ["all", "2026-10-30"]) == ["all", "2026-10-30"]
