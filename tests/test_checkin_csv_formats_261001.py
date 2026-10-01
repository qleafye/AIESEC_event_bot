"""Выгрузка офлайн-сканера перед форумом 03.10: форматы реальных приложений.

`services.checkin.find_checkin_records`/`decode_scan_export`: время «03.10.2026, 10:15», дата и
время отдельными колонками, американское «10/3/2026 10:15 AM», UTF-16 (с BOM и без), «;» вместо
«,», пробел внутри кавычек, ФИО с запятой в некавыченном CSV, номер телефона не путается с
временем. Дубли сводятся по токену и дню. Загрузка в боте —
`tests/test_checkin_csv_upload_261001.py`."""
from __future__ import annotations

import pytest

from services.checkin import decode_scan_export, find_checkin_records, parse_qr_payload

TAG = "YL26/2"
Q1 = f"{TAG}·Иванов Иван·tmn·AbC-d_12345"
Q2 = f"{TAG}·Петрова Анна·spb·ZZZ-x_9876"


def _pairs(text: str) -> list[tuple[str, str | None]]:
    return [(parse_qr_payload(r["qr"])["token"], r["scanned_at"]) for r in find_checkin_records(text, TAG)]


# ── разбор ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    pytest.param(
        "﻿Дата;Содержимое\n03.10.2026, 10:15:23;" + Q1 + "\n03.10.2026, 10:16:00;" + Q2 + "\n",
        [("AbC-d_12345", "2026-10-03 10:15:23"), ("ZZZ-x_9876", "2026-10-03 10:16:00")],
        id="dmy_comma_semicolon_bom",
    ),
    pytest.param(
        f"date,time,text\n03.10.2026,10:15:23,{Q1}\n03.10.2026,10:16,{Q2}\n",
        [("AbC-d_12345", "2026-10-03 10:15:23"), ("ZZZ-x_9876", "2026-10-03 10:16:00")],
        id="separate_date_time_columns",
    ),
    pytest.param(
        f'Date,Content\n"10/3/2026 10:15 AM",{Q1}\n"10/3/2026 1:05 PM",{Q2}\n',
        [("AbC-d_12345", "2026-10-03 10:15:00"), ("ZZZ-x_9876", "2026-10-03 13:05:00")],
        id="us_am_pm",
    ),
    pytest.param(
        f"time;text\n03/10/2026 10:15;{Q1}\n",
        [("AbC-d_12345", "2026-10-03 10:15:00")],
        id="slash_dmy_24h",
    ),
    pytest.param(
        f"time,text\n2026-10-03, 10:15:23,{Q1}\n",
        [("AbC-d_12345", "2026-10-03 10:15:23")],
        id="iso_date_comma_time",
    ),
    pytest.param(
        f"{Q1},2026-10-03 10:15:23\n{Q2},2026-10-03 10:16:00\n",
        [("AbC-d_12345", "2026-10-03 10:15:23"), ("ZZZ-x_9876", "2026-10-03 10:16:00")],
        id="no_header_comma",
    ),
    pytest.param(
        f"id,phone,text,ts\n1,79161234567,{Q1},2026-10-03 10:15:23\n",
        [("AbC-d_12345", "2026-10-03 10:15:23")],
        id="phone_is_not_time",
    ),
    pytest.param(
        f"id,phone,text\n1,79161234567,{Q1}\n",
        [("AbC-d_12345", None)],
        id="phone_only_no_time",
    ),
])
def test_time_formats(text, expected):
    assert _pairs(text) == expected


def test_quoted_trailing_space_gives_one_record():
    assert _pairs(f'time;text\n2026-10-03 10:15:23;"{Q1} "\n') == [("AbC-d_12345", "2026-10-03 10:15:23")]


def test_name_with_comma_in_unquoted_csv_keeps_token_and_name():
    recs = find_checkin_records(f"time,text\n2026-10-03 10:15:23,{TAG}·Иванова, Анна·spb·QQQ-1_aaaa\n", TAG)
    assert len(recs) == 1
    parsed = parse_qr_payload(recs[0]["qr"])
    assert parsed["token"] == "QQQ-1_aaaa"
    assert parsed["full_name"] == "Иванова, Анна"
    assert recs[0]["scanned_at"] == "2026-10-03 10:15:23"


def test_same_token_same_day_is_one_record_with_earliest_time():
    text = f"time,text\n2026-10-03 10:20:00,{Q1}\n2026-10-03 10:15:00,{Q1}\n{Q1}\n"
    assert _pairs(text) == [("AbC-d_12345", "2026-10-03 10:15:00")]


def test_same_token_two_days_is_two_entries():
    text = f"time,text\n2026-10-30 10:15:23,{Q1}\n2026-10-31 09:00:00,{Q1}\n"
    assert _pairs(text) == [("AbC-d_12345", "2026-10-30 10:15:23"), ("AbC-d_12345", "2026-10-31 09:00:00")]


def test_many_codes_on_one_json_line_get_no_guessed_time():
    text = '[{"t":"2026-10-03 10:15","c":"' + Q1 + '"},{"t":"2026-10-03 10:16","c":"' + Q2 + '"}]'
    assert _pairs(text) == [("AbC-d_12345", None), ("ZZZ-x_9876", None)]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_export_is_read(encoding):
    data = f"time,text\n2026-10-03 10:15:23,{Q1}\n".encode(encoding)
    assert _pairs(decode_scan_export(data)) == [("AbC-d_12345", "2026-10-03 10:15:23")]


def test_cp1251_still_read():
    data = f"time;text\n03.10.2026 10:15;{Q1}\n".encode("cp1251", errors="replace")
    text = decode_scan_export(data)
    assert "Иванов" in text
