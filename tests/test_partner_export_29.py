"""Партнёрский CSV: фиксированные колонки, только одобренные, кнопка в «📊 Данные»."""
from __future__ import annotations

import inspect
from pathlib import Path

from database import db
from database.db import _csv_safe
from database.partner_export import (
    HEADERS, PARTNER_SQL_COLUMNS, RESUME_TYPE_LABELS, export_partner_csv, partner_select_prefix,
)
from domain.regform.engine import parse_repeatable, repeatable_display
from handlers.access.admin_caps import required_capability
from handlers.reg.reg_schema import _RESUME_TYPE_SHEET_LABELS
from handlers.settings import admin_core, admin_partner_export
from handlers.settings.admin_sections import SECTIONS, section_rows, visible_rows
from tests._enroll38 import add_user, ready, run

_BANNED = {
    "score", "status", "referrer_id", "source", "source_details", "goal", "telegram_id",
    "username", "resume_file_id", "resume_text", "is_it_3plus", "case_optin", "checked_in_at",
    "major", "work_format", "mini_profile", "event_city",
}
_EXPECTED_HEADERS = [
    "ФИО", "Email", "Телефон", "Город", "Образование", "Курс", "ВУЗ", "Направление",
    "Стек", "Опыт", "Готовность", "Форматы", "Тип резюме", "Ссылка на резюме",
    "Мини-профиль: проекты", "Мини-профиль: портфолио", "Мини-профиль: направление",
]


def _src() -> str:
    return Path(inspect.getfile(export_partner_csv)).read_text(encoding="utf-8")


async def _fill(tid, **fields):
    cols = ", ".join(f"{name} = ?" for name in fields)
    async with db._connect() as conn:
        await conn.execute(
            f"UPDATE users SET {cols} WHERE telegram_id = ?",
            (*fields.values(), tid),
        )
        await conn.commit()


def _by_name(rows, name):
    return next(row for row in rows if row[0] == name)


def test_columns_are_exactly_the_partner_list(tmp_path):
    ready(tmp_path, "partner-headers.db")
    headers, rows = run(export_partner_csv())
    assert headers == _EXPECTED_HEADERS
    assert headers == HEADERS
    assert rows == []
    assert len(headers) == 17


def test_sql_columns_exclude_service_fields():
    assert not (set(PARTNER_SQL_COLUMNS) & _BANNED)
    assert not any("rating" in name or "score" in name for name in PARTNER_SQL_COLUMNS)


def test_select_is_built_only_from_the_tuple():
    head = partner_select_prefix()
    assert head.split(" FROM", 1)[0] == "SELECT " + ", ".join(PARTNER_SQL_COLUMNS)
    assert "*" not in head
    src = inspect.getsource(export_partner_csv)
    assert "SELECT *" not in src
    assert "partner_select_prefix()" in src


def test_only_approved_current_season(tmp_path):
    ready(tmp_path, "partner-season.db")

    async def scenario():
        await db.set_setting("event_season", "YL 26/2")
        await add_user(1, status="approved", season="YL 26/2")
        await add_user(2, status="pending", season="YL 26/2")
        await add_user(3, status="rejected", season="YL 26/2")
        await add_user(4, status="approved", season="YL 26/1")
        await add_user(5, status="approved", season=None)
        await _fill(1, full_name="Текущий")
        await _fill(2, full_name="Ждёт")
        await _fill(3, full_name="Отказ")
        await _fill(4, full_name="Прошлый")
        await _fill(5, full_name="Без сезона")
        _headers, rows = await export_partner_csv()
        names = {row[0] for row in rows}
        assert names == {"Текущий", "Без сезона"}
        await db.set_setting("event_season", "")
        _headers, rows = await export_partner_csv()
        names = {row[0] for row in rows}
        assert "Прошлый" in names and "Ждёт" not in names and "Отказ" not in names

    run(scenario())


def test_city_scope(tmp_path):
    ready(tmp_path, "partner-city.db")

    async def scenario():
        await add_user(10, city="msk")
        await add_user(11, city="spb")
        await _fill(10, full_name="Свой", city="Домашний МСК")
        await _fill(11, full_name="Чужой", city="Домашний СПБ")
        _headers, rows = await export_partner_csv(city_scope=("msk", ()))
        assert [row[0] for row in rows] == ["Свой"]
        assert rows[0][3] == "Домашний МСК"

    run(scenario())


def test_resume_link_falls_back_to_resume_url(tmp_path):
    ready(tmp_path, "partner-resume.db")

    async def scenario():
        await add_user(20)
        await _fill(20, full_name="Резюме", resume_link="  ", resume_url="https://cv.example")
        headers, rows = await export_partner_csv()
        row = _by_name(rows, "Резюме")
        assert headers.index("Ссылка на резюме") == 13
        assert row[13] == "https://cv.example"
        assert len(row) == 17

    run(scenario())


def test_resume_type_uses_sheet_labels(tmp_path):
    ready(tmp_path, "partner-type.db")
    assert RESUME_TYPE_LABELS == _RESUME_TYPE_SHEET_LABELS

    async def scenario():
        await add_user(30)
        await add_user(31)
        await _fill(30, full_name="Файл", resume_type="file")
        await _fill(31, full_name="Код", resume_type="weird")
        _headers, rows = await export_partner_csv()
        assert _by_name(rows, "Файл")[12] == "Файл"
        assert _by_name(rows, "Код")[12] == "weird"

    run(scenario())


def test_portfolio_is_human(tmp_path):
    ready(tmp_path, "partner-portfolio.db")
    raw = '[{"title": "Сайт", "description": "визитка"}]'

    async def scenario():
        await add_user(40)
        await _fill(40, full_name="Портфолио", mini_portfolio=raw)
        _headers, rows = await export_partner_csv()
        cell = _by_name(rows, "Портфолио")[15]
        assert cell == repeatable_display(parse_repeatable(raw))
        assert "[{" not in cell

    run(scenario())


def test_formula_cells_are_neutralized(tmp_path):
    ready(tmp_path, "partner-formula.db")

    async def scenario():
        await add_user(50)
        await _fill(50, full_name="=1+1", email="+evil")
        _headers, rows = await export_partner_csv()
        row = rows[0]
        assert row[0] == _csv_safe("=1+1")
        assert row[1] == _csv_safe("+evil")

    run(scenario())


def test_no_checked_in_and_no_checkins():
    src = _src()
    assert "checked_in_at" not in src
    assert "checkins" not in src
    assert "handlers" not in src
    handler = Path(inspect.getfile(admin_partner_export)).read_text(encoding="utf-8")
    assert "checked_in_at" not in handler


class _User:
    id = 1


class _Msg:
    def __init__(self):
        self.docs = []

    async def answer_document(self, document, caption=None):
        self.docs.append((document, caption))


class _Cb:
    def __init__(self):
        self.from_user = _User()
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_button_caption_and_scope(monkeypatch):
    seen = {}

    async def view(_admin_id):
        return (("msk", ()), "A<b>")

    async def fake_export(*, city_scope=None):
        seen["scope"] = city_scope
        return (list(HEADERS), [("Аня",) * 17])

    assert admin_partner_export._admin_city_view is admin_core._admin_city_view
    monkeypatch.setattr(admin_partner_export, "_admin_city_view", view)
    monkeypatch.setattr(admin_partner_export, "export_partner_csv", fake_export)
    cb = _Cb()
    run(admin_partner_export.show_partner_export(cb))
    assert seen["scope"] == ("msk", ())
    document, caption = cb.message.docs[0]
    assert document.filename == "partners_msk.csv"
    assert "Служебные поля в этот файл не входят." in caption
    assert "A&lt;b&gt;" in caption
    assert "1" in caption
    assert cb.answers == [(None, False)]


def test_cap_is_stats():
    assert required_capability(callback_data="admin_export_partner") == "stats"
    assert required_capability(callback_data="admin_export_csv") == "stats"


def test_row_is_in_data_section():
    rows = section_rows("data")
    at = rows.index(("op", "admin_export_csv"))
    assert rows[at + 1] == ("op", "admin_export_partner")
    labels = {callback: text for text, callback in admin_core._ADMIN_MENU_ROWS}
    assert labels["admin_export_partner"] == "🤝 Экспорт для партнёров"
    shown = visible_rows("data", {"stats"}, False)
    assert ("op", "admin_export_partner") in shown
    assert "🤝 Экспорт для партнёров" not in [label for _token, label, _rows in SECTIONS]


def test_empty_file_still_has_headers(monkeypatch):
    async def view(_admin_id):
        return (None, None)

    async def fake_export(*, city_scope=None):
        return (list(HEADERS), [])

    monkeypatch.setattr(admin_partner_export, "_admin_city_view", view)
    monkeypatch.setattr(admin_partner_export, "export_partner_csv", fake_export)
    cb = _Cb()
    run(admin_partner_export.show_partner_export(cb))
    document, caption = cb.message.docs[0]
    assert document.filename == "partners.csv"
    text = document.data.decode("utf-8-sig")
    assert text.splitlines() == [";".join(HEADERS)]
    assert caption.startswith("Одобренные текущего сезона: 0.")
    assert "Служебные поля в этот файл не входят." in caption
    assert all(answer[0] is None for answer in cb.answers)
