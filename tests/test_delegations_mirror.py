"""Зеркало формы делегаций в `UR REGS` «как выгрузка Яндекса»: обновление по ID, дописывание
после последней непустой строки, колонка M «В боте», серый/белый, P+ нетронуты, сухая сверка."""
import logging

import pytest

from services import delegations_mirror as dm
from services import ext_forms_mirror as mir
from tests._fake_ws import GREEN, GREY, PROBE_HEADER, WHITE, FakeWS, probe_sheet

TAB = "UR REGS"


def _columns(labels=PROBE_HEADER[3:12]):
    return [{"qkey": f"q{i + 1}", "label": lab, "position": i + 1} for i, lab in enumerate(labels)]


def _answer(aid, values=None, *, answered_at="2026-10-05 12:00:00", columns=None):
    columns = columns or _columns()
    values = values or {}
    payload = [{"q": c["qkey"], "label": c["label"], "value": values.get(c["qkey"], f"v{c['qkey']}")}
               for c in columns]
    return {"answer_id": aid, "answered_at": answered_at, "payload": payload}


def _st(ta="ok", linked=False, arrived=False):
    return {"ta_status": ta, "linked": linked, "arrived": arrived, "decided_by": None}


@pytest.fixture
def ws(monkeypatch):
    sheet = probe_sheet()
    monkeypatch.setattr(mir, "_open_tab_sync", lambda tab: sheet if tab == TAB else None)
    return sheet


def _ranges(ws):
    out = []
    for c in ws.calls:
        if c[0] == "batch_update":
            out.extend(c[1])
    return out


def _formats(ws):
    out = []
    for c in ws.calls:
        if c[0] == "batch_format":
            out.extend(c[1])
    return out


# ---------- запись ----------

def test_update_existing_by_id(ws):
    before = [list(r) for r in ws.rows]
    res = dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200002", {"q1": "Новое ФИО"}), _st())])
    assert res == (0, 1, [])
    row = ws.rows[2]
    assert row[:3] == before[2][:3]  # A/B/C не тронуты
    assert row[3] == "Новое ФИО"
    assert row[12] == "⏳ не заходил"
    assert len(ws.rows) == len(before)
    assert not any(c[0] == "append_rows" for c in ws.calls)
    assert all(r.startswith(("D3:", "M1")) for r in _ranges(ws))


def test_new_row_after_last_nonempty(ws):
    res = dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200099"), _st())])
    assert res == (1, 0, [])
    assert ws.rows[8][0] == "2516200099"  # строка 9 — после строки 8, не в пустые 4–6
    assert ws.rows[8][1] == ""
    assert ws.rows[8][2] == "2026-10-05 12:00:00"
    assert ws.rows[8][3] == "vq1" and ws.rows[8][11] == "vq9"
    assert ws.rows[8][12] == "⏳ не заходил"
    assert ws.rows[3] == [] and ws.rows[4] == [] and ws.rows[5] == []
    assert "A9:M9" in _ranges(ws)


def test_grid_grown_when_rows_run_out(ws):
    ws.row_count = 8
    dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200097"), _st()),
                                           (2, _answer("2516200098"), _st())])
    assert ("add_rows", 2) in ws.calls
    assert [r[0] for r in ws.rows[8:10]] == ["2516200097", "2516200098"]


def test_m_header_written_only_when_empty(ws):
    dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200001"), _st())])
    assert ws.rows[0][12] == "В боте"
    assert "M1" in _ranges(ws)
    ws.calls.clear()
    dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200001"), _st())])
    assert "M1" not in _ranges(ws)
    ws.rows[0][12] = "Чужое"
    ws.calls.clear()
    with pytest.raises(dm.ColumnOccupiedError):
        dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200001"), _st())])
    assert ws.calls == []
    assert ws.rows[0][12] == "Чужое"


def test_m_labels():
    assert dm.m_label(_st("ok")) == "⏳ не заходил"
    assert dm.m_label(_st("ok", linked=True)) == "✅ зашёл"
    assert dm.m_label(_st("ok", linked=True, arrived=True)) == "🎟 пришёл"
    assert dm.m_label(_st("no")) == "— не ЦА"
    assert dm.m_label(_st("check")) == "❔ проверить курс"
    assert dm.m_label({}) == "❔ проверить курс"  # ответ без оценки
    assert dm.M_HEADER == "В боте"


def test_grey_and_white(ws):
    rows = [
        (1, _answer("2516200001"), _st("no")),               # белая -> серая
        (2, _answer("2516200002"), _st("ok")),               # серая (строка 3) -> белая
        (3, _answer("2516200003"), _st("ok")),               # белая, ok -> без формата
        (4, _answer("2516200004"), _st("no")),               # зелёная (строка 8) -> не трогаем
        (5, _answer("2516200050"), _st("no")),               # новая строка 9 -> серая
    ]
    dm.write_export_sync(TAB, _columns(), rows)
    fmts = dict(_formats(ws))
    assert fmts["A2:M2"] == {"backgroundColor": dm.GREY}
    assert fmts["A3:M3"] == {"backgroundColor": dm.WHITE}
    assert fmts["A9:M9"] == {"backgroundColor": dm.GREY}
    assert "A7:M7" not in fmts and "A8:M8" not in fmts
    assert ws.formats[8] == GREEN
    assert sum(1 for c in ws.calls if c[0] == "batch_format") == 1
    assert ws.rows[7][12] == "— не ЦА"  # значение M у зелёной строки всё же пишем


def test_grey_constants_match_probe():
    assert dm.GREY == {"red": 0.851, "green": 0.851, "blue": 0.851} == GREY
    assert dm.GREEN == {"red": 0.714, "green": 0.843, "blue": 0.659} == GREEN
    assert dm.WHITE == WHITE


def test_colour_read_failure_recolours_only_new_rows(ws):
    def boom(params=None):
        raise RuntimeError("quota")
    ws.spreadsheet.fetch_sheet_metadata = boom
    dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200001"), _st("no")),
                                           (2, _answer("2516200077"), _st("no"))])
    fmts = dict(_formats(ws))
    assert "A2:M2" not in fmts
    assert fmts["A9:M9"] == {"backgroundColor": dm.GREY}


def test_no_format_call_when_nothing_changes(ws):
    dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200001"), _st("ok")),
                                           (2, _answer("2516200002"), _st("no"))])
    assert not any(c[0] == "batch_format" for c in ws.calls)


def test_p_and_right_untouched(ws):
    keep = {(r, c): ws.cell(r, c) for r in range(1, 9) for c in range(16, 18)}
    dm.write_export_sync(TAB, _columns(), [
        (1, _answer("2516200001"), _st("no")),
        (2, _answer("2516200002"), _st("ok", linked=True)),
        (3, _answer("2516200055"), _st("check")),
    ])
    assert {(r, c): ws.cell(r, c) for r in range(1, 9) for c in range(16, 18)} == keep
    assert ws.cell(1, 16) == "всего (ЦА):" and ws.cell(1, 17) == "=SUM(Q2:Q16)"
    from tests._fake_ws import parse_range
    for rng in _ranges(ws):
        _, _, _, c2 = parse_range(rng)
        assert c2 <= dm.M_COL, rng
    for rng, _ in _formats(ws):
        _, _, _, c2 = parse_range(rng)
        assert c2 <= dm.M_COL, rng


def test_column_map_by_header_labels(ws):
    # вопросы формы в другом порядке: «Ник в телеграмме» на позиции 3, неизвестный вопрос на 10
    labels = PROBE_HEADER[3:12]
    shuffled = [labels[0], labels[1], labels[6], labels[2], labels[3], labels[4], labels[5],
                labels[7], labels[8]]
    columns = _columns(shuffled) + [{"qkey": "q10", "label": "Любимый цвет", "position": 10}]
    cmap, extra = dm.column_map(columns, ws.row_values(1))
    assert cmap["q3"] == 10  # J по подписи, не по позиции
    assert cmap["q1"] == 4 and cmap["q9"] == 12
    assert "q10" not in cmap and extra == ["Любимый цвет"]
    ans = _answer("2516200001", {"q3": "@nick", "q10": "синий"}, columns=columns)
    res = dm.write_export_sync(TAB, columns, [(1, ans, _st())])
    assert res == (0, 1, ["Любимый цвет"])
    assert ws.rows[1][9] == "@nick"
    assert "синий" not in ws.rows[1]
    # все вопросы с колонкой -> третий элемент пуст
    assert dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200001"), _st())])[2] == []


def test_column_map_label_normalisation_and_positional_fallback():
    header = PROBE_HEADER[:3] + ["  фио ", "возраст", "Другая подпись"] + PROBE_HEADER[6:12]
    columns = _columns()
    cmap, extra = dm.column_map(columns, header)
    assert cmap["q1"] == 4 and cmap["q2"] == 5  # регистр и пробелы не мешают
    assert cmap["q3"] == 6  # подпись не совпала -> по позиции (3 + 3)
    assert extra == []


def test_duplicate_ids_in_sheet(ws, caplog):
    ws.rows[6][0] = "2516200002"  # строка 7 дублирует ID строки 3
    with caplog.at_level(logging.WARNING, logger="services.delegations_mirror"):
        dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200002", {"q1": "X"}), _st())])
    assert ws.rows[2][3] == "X" and ws.rows[6][3] != "X"
    dup_logs = [r for r in caplog.records if "дубл" in r.getMessage().lower()]
    assert len(dup_logs) == 1
    assert "Тест Тестов" not in dup_logs[0].getMessage()


def test_cell_safety(ws):
    dm.write_export_sync(TAB, _columns(), [(1, _answer("2516200001", {"q1": "=1+1", "q7": "@nick"}), _st())])
    from database.db import _sheet_safe
    assert ws.rows[1][3] == _sheet_safe("=1+1")
    assert ws.rows[1][9] == _sheet_safe("@nick")
    for c in ws.calls:
        if c[0] == "batch_update":
            assert c[2] == mir._raw()
    assert mir._raw() != "USER_ENTERED"


# ---------- сухая сверка ----------

def test_dry_run_numbers(ws):
    ids = ["2516200001", "2516200002", "2516200003", "2516200088", "2516200089"]
    res = dm.dry_run_sync(TAB, _columns(), ids)
    assert res["sheet_rows"] == 4
    assert res["matched"] == 3 and res["new"] == 2 and res["unknown_sheet_ids"] == 1
    assert res["header_diff"] == [] and res["extra_questions"] == []
    assert res["m_free"] is True and res["m_header"] == ""
    assert res["grey_rows"] == 1 and res["green_rows"] == 1
    assert res["header"] == PROBE_HEADER
    assert all(c[0] not in ("batch_update", "batch_format", "update", "add_rows") for c in ws.calls)


def test_dry_run_header_diff_and_m(ws):
    ws.rows[0][3] = "Имя и фамилия"
    ws.rows[0][12] = "Чужое"
    res = dm.dry_run_sync(TAB, _columns(), ["2516200001"])
    assert res["header_diff"] == [("D", "Имя и фамилия", "ФИО")]
    assert res["m_free"] is False and res["m_header"] == "Чужое"
    ws.rows[0][12] = "В боте"
    assert dm.dry_run_sync(TAB, _columns(), [])["m_free"] is True


def test_dry_run_extra_question(ws):
    cols = _columns() + [{"qkey": "q10", "label": "Любимый цвет", "position": 10}]
    assert dm.dry_run_sync(TAB, cols, [])["extra_questions"] == ["Любимый цвет"]


def test_tab_missing(monkeypatch):
    monkeypatch.setattr(mir, "_open_tab_sync", lambda tab: None)
    assert dm.write_export_sync("Нет", _columns(), [(1, _answer("1"), _st())]) is None
    assert dm.dry_run_sync("Нет", _columns(), ["1"]) is None


def test_fake_ws_guards():
    sheet = FakeWS([["ID"]])
    with pytest.raises(AssertionError):
        sheet.append_rows([["x"]])
    with pytest.raises(AssertionError):
        sheet.insert_cols(1)
