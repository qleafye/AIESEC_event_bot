"""Доли рамок сертификата рядом с файлом. Нет долей — прежние полосы."""
from __future__ import annotations

import asyncio
import inspect
import io

from pypdf import PdfReader, PdfWriter

from config import config
from database import certificates_db as cdb
from database import db
from dashboard.certificate_markup import MARKUP_HREF, markup_page
from services.forum.certificate_render import (
    NAME_BOX,
    dump_markup,
    markup_error,
    parse_markup,
    pdf_error,
    preview_pdf,
    render_certificate,
)
from services.forum.certificates import percents_for_scales
from tests.test_certificates_261028 import _template
from tests.test_forum_review_261028 import _name_cm


def _run(coro):
    return asyncio.run(coro)


def test_without_markup_name_stays_on_the_old_box():
    w, h, x, y = _name_cm(render_certificate(_template(), "Анна Иванова"))
    assert abs(w - 842 * NAME_BOX[2]) < 1
    assert abs((x + w / 2) - 421) < 1
    assert abs((y + h / 2) - (595 - 595 * NAME_BOX[1])) < 1


def test_saved_fractions_move_the_name():
    markup = {"name": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.1}}
    w, h, x, y = _name_cm(render_certificate(_template(), "Анна Иванова", markup=markup))
    assert abs(w - 842 * 0.4) < 1
    assert abs(x - 842 * 0.1) < 1
    assert abs(h - 595 * 0.1) < 1
    assert abs(y - (595 - 595 * 0.2 - 595 * 0.1)) < 1


def test_scales_date_and_city_follow_saved_boxes():
    markup = {
        "date": {"x": 0.05, "y": 0.8, "w": 0.3, "h": 0.06},
        "city": {"x": 0.5, "y": 0.8, "w": 0.3, "h": 0.06},
        "scales": [
            {"x": 0.55, "y": 0.2, "w": 0.4, "h": 0.06},
            {"x": 0.55, "y": 0.3, "w": 0.4, "h": 0.06},
        ],
    }
    pdf = render_certificate(
        _template(), "Анна Иванова", percents=[80, 0], markup=markup,
        date_text="30.10.2026", city_text="Москва",
    )
    data = PdfReader(io.BytesIO(pdf)).pages[0].get_contents().get_data().decode("latin-1")
    assert "/CertDate" in data and "/CertCity" in data
    assert "/CertBar0" in data and "/CertBar1" not in data
    assert "/CertBars" not in data


def test_plain_pdf_does_not_need_a_name_layer():
    pdf = render_certificate(_template(), "Анна Иванова", markup={"name": {"x": 0.2, "y": 0.4, "w": 0.5, "h": 0.08}})
    assert "/CertName" in PdfReader(io.BytesIO(pdf)).pages[0]["/Resources"]["/XObject"]
    body = inspect.getsource(render_certificate).split('"""', 2)[-1]
    assert "{name}" not in body


def test_box_outside_the_page_is_rejected():
    assert markup_error({"name": {"x": 0.8, "y": 0.1, "w": 0.4, "h": 0.1}})
    assert markup_error({"name": {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.1}}) is None
    assert parse_markup('{"name": {"x": 2, "y": 0, "w": 0.1, "h": 0.1}}') is None


def test_markup_is_stored_next_to_the_file(tmp_path):
    config.DB_PATH = str(tmp_path / "markup.db")
    _run(db.init_db())
    _run(cdb.set_template("participant", "all", "FILE_A", 1))
    raw = dump_markup({"name": {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.1}, "scales": []})
    assert _run(cdb.set_markup("participant", "all", raw)) is True
    assert parse_markup(_run(cdb.markup_for_file("FILE_A")))["name"]["x"] == 0.1
    assert _run(cdb.set_template("participant", "all", "FILE_A", 1)) is False
    assert _run(cdb.markup_for_file("FILE_A"))
    assert _run(cdb.set_template("participant", "all", "FILE_B", 1)) is True
    assert _run(cdb.markup_for_file("FILE_B")) is None


def test_scale_follows_the_competency_button_not_the_row():
    percents = [100, 75, 50, 25, 0]
    by_name = {
        "Критическое и аналитическое мышление": 100,
        "Коммуникация и самопрезентация": 50,
    }
    markup = {"scales": [
        {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.05, "competency": "Коммуникация и самопрезентация"},
        {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05, "competency": "Критическое и аналитическое мышление"},
    ]}
    assert percents_for_scales(percents, markup, by_name) == [50, 100]
    plain = {"scales": [{"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.05}]}
    assert percents_for_scales(percents, plain, by_name) == percents


def test_ordinary_pdf_is_accepted_and_a_picture_is_not():
    assert pdf_error(_template()) is None
    assert pdf_error(b"not a pdf") == "Это не PDF. Пришлите файл в формате PDF."


def test_preview_keeps_only_the_first_page():
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_blank_page(width=200, height=400)
    raw = io.BytesIO()
    writer.write(raw)
    shown = preview_pdf(raw.getvalue())
    assert len(PdfReader(io.BytesIO(shown)).pages) == 1
    assert preview_pdf(_template()) == _template()


def test_pdf_upload_is_not_cut_at_one_megabyte():
    text = open("miniapp/main.py", encoding="utf-8").read()
    assert '"/app/api/certificates/markup/file": MAX_BODY_BYTES' in text


def test_site_opens_the_markup_screen_and_chat_has_no_item():
    page = markup_page()
    assert MARKUP_HREF == "/app/#/certificate-markup"
    assert MARKUP_HREF in page
    screen = open("miniapp/static/js/screens/certificate_markup.js", encoding="utf-8").read()
    chat = open("handlers/forum/admin_certificates.py", encoding="utf-8").read()
    assert "box.competency = name" in screen
    assert "dataTransfer" not in screen
    assert "innerHTML" not in screen
    assert "certadm_markup" not in chat
    assert "certificate-markup" not in chat
