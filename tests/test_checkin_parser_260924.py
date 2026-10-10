"""Phase 12 (FORUM-CHECKIN.md, D-09/D-10/D-13): чистый разбор выгрузки офлайн-приложения-
сканера и `parse_qr_payload` — юнит-тестируется без БД.

Формат QR (`build_payload`, `_event_tag`, `checkin_denial`) уже покрыт
`tests/test_checkin_qr_260923.py` (Квик 260923) — здесь только новая половина модуля."""
from __future__ import annotations

import time

from services.forum.checkin import (
    build_payload,
    decode_scan_export,
    find_checkin_records,
    parse_qr_payload,
)


def test_parse_qr_payload_roundtrip_with_build_payload():
    qr = build_payload("YL26", "Иванов Иван", "Казань", "k7Qx9a1b")
    assert qr == "YL26·Иванов Иван·Казань·k7Qx9a1b"
    parsed = parse_qr_payload(qr)
    assert parsed == {"tag": "YL26", "full_name": "Иванов Иван", "city": "Казань", "token": "k7Qx9a1b"}


def test_parse_qr_payload_never_raises_on_malformed_code():
    assert parse_qr_payload("")["token"] == ""
    assert parse_qr_payload("garbage-no-separator")["token"] == "garbage-no-separator"
    assert parse_qr_payload("A·B")["token"] == "B"  # только 2 поля -- имя/город пустые
    assert parse_qr_payload("A·B")["full_name"] == ""


def test_decode_scan_export_utf8_with_bom():
    data = ("﻿a,b\n1,2\n").encode("utf-8")
    assert decode_scan_export(data).lstrip("﻿").startswith("a,b")


def test_decode_scan_export_cp1251():
    data = "город,время\nКазань,10:00\n".encode("cp1251")
    text = decode_scan_export(data)
    assert "Казань" in text


def test_decode_scan_export_never_raises_on_garbage_bytes():
    assert isinstance(decode_scan_export(b"\xff\xfe\x00\x01"), str)


def test_find_checkin_records_comma_csv_iso_time():
    qr = build_payload("YL26", "Иванов Иван", "Казань", "tok1")
    text = f"timestamp,content,format\n2026-10-03T09:15:00,{qr},QR_CODE\n"
    recs = find_checkin_records(text, "YL26")
    assert len(recs) == 1
    assert recs[0]["qr"] == qr
    assert recs[0]["scanned_at"] == "2026-10-03 09:15:00"


def test_find_checkin_records_semicolon_csv_dmy_time():
    qr = build_payload("YL26", "Петров Пётр", "Тюмень", "tok2")
    text = f"Время;Данные\n03.10.2026 09:20;{qr}\n"
    recs = find_checkin_records(text, "YL26")
    assert recs[0]["scanned_at"] == "2026-10-03 09:20:00"


def test_find_checkin_records_tab_csv_epoch_seconds():
    qr = build_payload("YL26", "Сидоров Сидор", "СПб", "tok3")
    epoch = int(time.mktime((2026, 10, 3, 9, 25, 0, 0, 0, 0)))
    text = f"ts\tqr\n{epoch}\t{qr}\n"
    recs = find_checkin_records(text, "YL26")
    assert recs[0]["scanned_at"] is not None


def test_find_checkin_records_no_time_column_is_approx():
    qr = build_payload("YL26", "Кузнецов Кузьма", "Москва", "tok4")
    text = f"a,b\nfoo,{qr}\n"
    recs = find_checkin_records(text, "YL26")
    assert recs[0]["scanned_at"] is None


def test_find_checkin_records_dedupes_repeated_code_in_file():
    qr = build_payload("YL26", "Иванов Иван", "Казань", "tok5")
    text = f"a,b\n{qr},x\n{qr},y\n"
    recs = find_checkin_records(text, "YL26")
    assert len(recs) == 1


def test_find_checkin_records_ignores_foreign_event_tag():
    ours = build_payload("YL26", "Наш Делегат", "Казань", "tok6")
    foreign = build_payload("OTHER25", "Чужой Делегат", "Москва", "tok7")
    text = f"a,b\n{ours},x\n{foreign},y\n"
    recs = find_checkin_records(text, "YL26")
    assert len(recs) == 1
    assert recs[0]["qr"] == ours


def test_find_checkin_records_json_export_fallback():
    qr = build_payload("YL26", "Иванов Иван", "Казань", "tok8")
    text = '{"items":[{"content":"' + qr + '","raw":true}]}'
    recs = find_checkin_records(text, "YL26")
    assert len(recs) == 1
    assert recs[0]["qr"] == qr


def test_find_checkin_records_empty_file_no_matches():
    assert find_checkin_records("", "YL26") == []
    assert find_checkin_records("some,unrelated,csv\n1,2,3\n", "YL26") == []


def test_find_checkin_records_no_tag_configured_returns_empty():
    qr = build_payload("YL26", "Иванов Иван", "Казань", "tok9")
    assert find_checkin_records(qr, "") == []


# F14: время выгрузки сканера — в МСК независимо от зоны процесса (контейнер живёт в UTC).
# 1791008700 = 2026-10-03 06:25:00 UTC = 09:25:00 МСК.
_EPOCH_0925_MSK = 1791008700


def test_epoch_seconds_are_moscow_time_regardless_of_process_tz():
    qr = build_payload("YL26", "Сидоров Сидор", "СПб", "tok6")
    recs = find_checkin_records(f"ts\tqr\n{_EPOCH_0925_MSK}\t{qr}\n", "YL26")
    assert recs[0]["scanned_at"] == "2026-10-03 09:25:00"


def test_epoch_milliseconds_are_moscow_time():
    qr = build_payload("YL26", "Сидоров Сидор", "СПб", "tok7")
    recs = find_checkin_records(f"ts,qr\n{_EPOCH_0925_MSK}123,{qr}\n", "YL26")
    assert recs[0]["scanned_at"] == "2026-10-03 09:25:00"


def test_iso_with_zone_is_converted_to_moscow():
    for cell in ("2026-10-03T06:25:00Z", "2026-10-03T06:25:00.500Z", "2026-10-03T08:25:00+02:00",
                 "2026-10-03T09:25:00+03:00"):
        qr = build_payload("YL26", "Иванов Иван", "Казань", "tok-" + cell[-6:])
        recs = find_checkin_records(f"ts,qr\n{cell},{qr}\n", "YL26")
        assert recs[0]["scanned_at"] == "2026-10-03 09:25:00", cell


def test_msk_from_timestamp_ignores_process_tz():
    from datetime import datetime
    from services.infra.timeutil import msk_from_timestamp
    assert msk_from_timestamp(_EPOCH_0925_MSK) == datetime(2026, 10, 3, 9, 25, 0)
