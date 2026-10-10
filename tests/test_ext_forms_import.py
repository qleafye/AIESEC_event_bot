"""Импорт выгрузки личной Яндекс Формы (XLSX/CSV) и экраны карточки push-формы."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from database import ext_forms_db as ef
from handlers.ext_forms import admin_ext_forms_push as push
from handlers.states import ExtFormImport
from services.ext_forms import ext_forms_import as imp
from services.ext_forms import ext_forms_yandex_sync as S
from tests.test_roles_phase8 import (
    ADMIN_ID, FakeMessage, _fresh_state, _roles_ready, dispatch_callback,
)


def _run(coro):
    return asyncio.run(coro)


def make_xlsx(rows: list[list], shared: bool = True) -> bytes:
    """Минимальный XLSX: строки — через sharedStrings, числа — как есть, None — пропуск."""
    sst: list[str] = []
    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, v in enumerate(row):
            if v is None:
                continue
            ref = f"{chr(65 + c)}{r}"
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            elif shared:
                sst.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(sst) - 1}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml",
                   f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'
                   f'<sheet name="Ответы" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="{ns}">' + "".join(f"<si><t>{s}</t></si>" for s in sst) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    return buf.getvalue()


HEADER = ["ID ответа", "Время создания", "Ник в Telegram", "Телефон"]
ROWS = [
    HEADER,
    [1001, "2026-10-01 10:00:00", "@ann", "+7 900 111"],
    [1002, "2026-10-02 11:30:00", "@bob", "+7 900 222"],
]


# ---------- чтение файла ----------

def test_read_xlsx_shared_and_inline_and_numbers():
    rows = imp.read_export_rows(make_xlsx(ROWS), "a.xlsx")
    assert rows[0] == HEADER
    assert rows[1][0] == "1001"  # число-ID без «.0»
    rows = imp.read_export_rows(make_xlsx(ROWS, shared=False), "a.xlsx")
    assert rows[2][2] == "@bob"


def test_read_xlsx_gaps_are_filled():
    rows = imp.read_export_rows(make_xlsx([["a", "b", "c"], ["x", None, "z"]]), "a.xlsx")
    assert rows[1] == ["x", "", "z"]


def test_read_xlsx_float_id_and_excel_date():
    # 46000.5 = дата-серийник Excel; ID, записанный как 1001.0, остаётся «1001»
    rows = imp.read_export_rows(make_xlsx([HEADER, [1001.0, 46000.5, "@a", "1"]]), "a.xlsx")
    answers, _ = imp.export_to_answers(rows)
    assert answers[0][0] == "1001"
    assert answers[0][1].startswith("2025-12-")


def test_read_csv_semicolon_utf8_and_cp1251():
    text = "ID ответа;Время создания;Ник\n5;01.10.2026 10:00:00;@ann\n"
    for enc in ("utf-8-sig", "cp1251"):
        rows = imp.read_export_rows(text.encode(enc), "a.csv")
        assert rows[1] == ["5", "01.10.2026 10:00:00", "@ann"]
    rows = imp.read_export_rows(text.replace(";", ",").encode("utf-8"), "a.csv")
    assert rows[0][2] == "Ник"


def test_broken_file_gives_human_error():
    with pytest.raises(imp.ImportFileError):
        imp.read_export_rows(b"PK\x03\x04 not a zip", "a.xlsx")
    with pytest.raises(imp.ImportFileError):
        imp.read_export_rows(b"", "a.csv")
    with pytest.raises(imp.ImportFileError):
        imp.read_export_rows(b"\x00\x01\x02" * 10, "a.bin")


def test_xlsx_with_doctype_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", '<!DOCTYPE x [<!ENTITY a "b">]><worksheet/>')
    with pytest.raises(imp.ImportFileError):
        imp.read_export_rows(buf.getvalue(), "a.xlsx")


# ---------- строки -> анкеты ----------

def test_export_to_answers_columns_dups_and_no_id():
    rows = [["id", "Время создания", "Вопрос", "Вопрос", ""],
            ["7", "2026-10-01 10:00:00", "a", "b", "c"],
            ["", "2026-10-01 10:00:00", "no id", "", ""],
            ["", "", "", "", ""]]
    answers, no_id = imp.export_to_answers(rows)
    assert no_id == 1 and len(answers) == 1
    aid, at, items = answers[0]
    assert (aid, at) == ("7", "2026-10-01 10:00:00")
    assert [i["label"] for i in items] == ["Вопрос", "Вопрос (2)", "Колонка 5"]


def test_no_id_column_and_row_limit():
    with pytest.raises(imp.ImportFileError) as e:
        imp.export_to_answers([["Имя"], ["x"]])
    assert "нет колонки с ID ответа" in e.value.text
    with pytest.raises(imp.ImportFileError):
        imp.export_to_answers([["ID"]] + [[str(i)] for i in range(imp.MAX_ROWS + 1)])


# ---------- сохранение и дедуп ----------

@pytest.fixture
def form(tmp_path):
    _roles_ready(tmp_path)
    fid = _run(ef.create_form(platform="yandex", external_id="6aa022f0068ff027eaba0bcb",
                              title="Личная", secret="s", ingest_mode="push"))
    return _run(ef.get_form(fid))


def test_import_dedup_and_repeat(form):
    rep = _run(imp.import_answers(form, ROWS))
    assert rep == {"added": 2, "existed": 0, "no_id": 0}
    rep = _run(imp.import_answers(form, ROWS))
    assert rep == {"added": 0, "existed": 2, "no_id": 0}
    assert _run(ef.count_answers(form["id"])) == 2


def test_import_does_not_duplicate_webhook_answer(form):
    body = {"params": {"answer_id": "1001", "answers": {"Ник в Telegram": "@ann"}}}
    _run(ef.enqueue_pending(form["id"], "1001", None, "2020-01-01 00:00:00",
                            payload=json.dumps(body)))
    _run(S.drain_pending())
    rep = _run(imp.import_answers(form, ROWS))
    assert rep["added"] == 1 and rep["existed"] == 1
    assert _run(ef.count_answers(form["id"])) == 2


# ---------- хендлеры ----------

class _Doc:
    def __init__(self, size=100, name="o.xlsx"):
        self.file_size, self.file_name, self.file_id = size, name, "fid"


class _Msg:
    def __init__(self, doc=None, text=None):
        self.document, self.text = doc, text
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))


class _Bot:
    def __init__(self, data: bytes):
        self.data, self.downloads = data, 0

    async def download(self, file_id):
        self.downloads += 1
        return io.BytesIO(self.data)


def _state_for(form):
    st = _fresh_state(ADMIN_ID)
    _run(st.set_state(ExtFormImport.waiting_file))
    _run(st.update_data(extf_import_form=form["id"]))
    return st


def test_import_start_sets_state(form):
    st = _fresh_state(ADMIN_ID)
    res, ev = dispatch_callback(f"extf_import:{form['id']}", ADMIN_ID, state=st)
    assert _run(st.get_state()) == ExtFormImport.waiting_file.state
    assert "XLSX" in ev.message.answers[0][0]


def test_file_import_report_and_buttons(form):
    st = _state_for(form)
    msg = _Msg(_Doc())
    _run(push.extf_import_file(msg, st, _Bot(make_xlsx(ROWS))))
    texts = " ".join(a[0] for a in msg.answers)
    assert "добавлено 2, уже были 0, без ID ответа 0" in texts
    assert _run(st.get_state()) is None
    kb = msg.answers[-1][1]
    assert f"extf_card:{form['id']}" in [b.callback_data for r in kb.inline_keyboard for b in r]


def test_big_file_rejected_before_download(form):
    st = _state_for(form)
    bot = _Bot(b"")
    msg = _Msg(_Doc(size=push.IMPORT_MAX_BYTES + 1))
    _run(push.extf_import_file(msg, st, bot))
    assert bot.downloads == 0 and "10 МБ" in msg.answers[0][0]


def test_bad_file_keeps_state(form):
    st = _state_for(form)
    msg = _Msg(_Doc())
    _run(push.extf_import_file(msg, st, _Bot(make_xlsx([["Имя"], ["x"]]))))
    assert "нет колонки с ID ответа" in msg.answers[-1][0]
    assert _run(st.get_state()) == ExtFormImport.waiting_file.state


def test_text_instead_of_file_and_cancel(form):
    msg = _Msg(text="привет")
    _run(push.extf_import_not_file(msg))
    assert "XLSX или CSV" in msg.answers[0][0]
    st = _state_for(form)
    _run(push.extf_import_cancel(_Msg(text="Отмена"), st))
    assert _run(st.get_state()) is None


# ---------- вопросы ника и телефона ----------

def _datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_pkeys_without_columns_alerts(form):
    res, ev = dispatch_callback(f"extf_pkeys:{form['id']}", ADMIN_ID)
    assert ev.answers[-1][1] is True and "первый ответ" in ev.answers[-1][0]


def test_pkeys_pick_sets_keys(form):
    _run(imp.import_answers(form, ROWS))
    res, ev = dispatch_callback(f"extf_pkeys:{form['id']}", ADMIN_ID)
    assert f"extf_pkey:{form['id']}:u" in _datas(ev.message.markup)
    res, ev = dispatch_callback(f"extf_pkey:{form['id']}:u", ADMIN_ID)
    assert f"extf_pkeyset:{form['id']}:u:0" in _datas(ev.message.markup)
    cols = _run(ef.list_columns(form["id"]))
    idx = next(i for i, c in enumerate(cols) if c["qkey"] == "Ник в Telegram")
    res, ev = dispatch_callback(f"extf_pkeyset:{form['id']}:u:{idx}", ADMIN_ID)
    assert _run(ef.get_form(form["id"]))["key_username_q"] == "Ник в Telegram"
    assert "Сохранено" in ev.answers[-1][0]
    res, ev = dispatch_callback(f"extf_pkeyset:{form['id']}:p:99", ADMIN_ID)
    assert ev.answers[-1][1] is True and "устарел" in ev.answers[-1][0]


def test_card_of_push_form_has_buttons_and_waiting_line(form):
    from handlers.ext_forms.admin_ext_forms import _card_kb, _card_text, _form_with_stats
    f = _run(_form_with_stats(form["id"]))
    assert "Ждёт первый ответ" in _card_text(f)
    assert "Последняя сверка" not in _card_text(f)
    assert {f"extf_import:{form['id']}", f"extf_pkeys:{form['id']}"} <= set(_datas(_card_kb(f)))
    _run(ef.set_form_push_warning(form["id"], "пришёл без ответов"))
    f = _run(_form_with_stats(form["id"]))
    assert "⚠️ пришёл без ответов" in _card_text(f)
