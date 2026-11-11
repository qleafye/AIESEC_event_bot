"""Доли рамок сертификата рядом с файлом. Нет долей — прежние полосы."""
from __future__ import annotations

import asyncio
import inspect
import io

from pypdf import PdfReader

from config import config
from database import certificates_db as cdb
from database import db
from services.forum.certificate_render import (
    NAME_BOX,
    dump_markup,
    markup_error,
    parse_markup,
    render_certificate,
)
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
