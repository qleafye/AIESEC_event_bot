"""Таблица ответов Google Формы: ссылка, ключи ts#n, синхронизация без дублей."""
import asyncio
import json

import gspread
import pytest

from config import config
from database import ext_forms_db as ef
from services.ext_forms import ext_forms_google as g
from tests._dbtpl import fast_init_db

HEAD = ["Отметка времени", "ФИО", "Ник"]


def test_url():
    assert g.parse_sheet_url("https://docs.google.com/spreadsheets/d/1AbC_xyz/edit#gid=123") == ("1AbC_xyz", 123)
    assert g.parse_sheet_url("https://docs.google.com/spreadsheets/d/1AbC_xyz/edit") == ("1AbC_xyz", None)
    assert g.parse_sheet_url("https://example.com/x") is None


def test_rows_keys_and_items():
    vals = [HEAD, ["01.10.2026 12:34:56", "Иван", "@iv"], ["02.10.2026 10:00:00", "Анна", "@an"]]
    res, has_ts = g.rows_to_answers(vals)
    assert has_ts
    assert [r[0] for r in res] == ["g:01.10.2026 12:34:56#0", "g:02.10.2026 10:00:00#0"]
    assert res[0][1] == "2026-10-01 12:34:56"
    assert [i["q"] for i in res[0][2]] == ["ФИО", "Ник"]
    assert res[0][2][0]["label"] == "ФИО"


def test_same_timestamp_counter():
    res, _ = g.rows_to_answers([HEAD, ["t", "a", "b"], ["t", "c", "d"]])
    assert [r[0] for r in res] == ["g:t#0", "g:t#1"]


def test_reorder_and_delete_keep_keys():
    a, b, c = ["t1", "a", "1"], ["t2", "b", "2"], ["t3", "c", "3"]
    keys = lambda rows: {r[0] for r in g.rows_to_answers([HEAD, *rows])[0]}
    assert keys([a, b, c]) == keys([c, a, b])
    assert keys([b, c]) <= keys([a, b, c])


def test_duplicate_header_and_blank_and_short():
    res, _ = g.rows_to_answers([["Отметка времени", "Ник", "Ник"], ["", "", ""], ["t", "x"]])
    assert len(res) == 1
    assert [i["q"] for i in res[0][2]] == ["Ник", "Ник (2)"]
    assert res[0][2][1]["value"] == ""


def test_no_timestamp_header():
    res, has_ts = g.rows_to_answers([["ФИО", "Ник"], ["Иван", "x"]])
    assert not has_ts
    assert res[0][0] == "g:Иван#0"
    assert res[0][1] is None


def test_date_formats():
    for v in ("01.10.2026 12:34:56", "2026-10-01 12:34:56", "10/1/2026 12:34:56"):
        assert g._parse_ts(v) == "2026-10-01 12:34:56"
    assert g._parse_ts("вчера") is None


# ---------- синхронизация ----------

class _WS:
    def __init__(self, values, gid=0, title="Ответы"):
        self.values, self.id, self.title = values, gid, title

    def get_all_values(self):
        return self.values


class _SH:
    def __init__(self, ws):
        self.ws, self.title, self.sheet1 = ws, "Табл", ws

    def worksheets(self):
        return [self.ws]

    def get_worksheet_by_id(self, gid):
        return self.ws


@pytest.fixture
def env(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "g.db")
    fast_init_db()
    cred = tmp_path / "cred.json"
    cred.write_text(json.dumps({"client_email": "bot@proj.iam.gserviceaccount.com"}))
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", str(cred))
    return monkeypatch


def _form(platform="google", ext="sid"):
    fid = asyncio.run(ef.create_form(platform=platform, external_id=ext, title="Ф"))
    return asyncio.run(ef.get_form(fid))


def test_sync_idempotent_and_incremental(env):
    rows = [HEAD, ["t1", "a", "1"], ["t2", "b", "2"], ["t3", "c", "3"]]
    env.setattr(g, "_open_spreadsheet_sync", lambda sid: _SH(_WS(rows)))
    form = _form()
    assert asyncio.run(g.sync_google_form(form)) == 3
    assert asyncio.run(g.sync_google_form(form)) == 0
    rows.append(["t4", "d", "4"])
    assert asyncio.run(g.sync_google_form(form)) == 1
    f = asyncio.run(ef.get_form(form["id"]))
    assert f["last_sync_at"] and f["sync_error"] is None


def test_no_credentials(env):
    env.setattr(config, "GOOGLE_CREDENTIALS_FILE", "/nonexistent.json")
    with pytest.raises(g.GoogleFormError) as ei:
        asyncio.run(g.list_tabs("x"))
    assert ei.value.reason == "no_credentials"


def test_no_access_message_and_sync_error(env):
    def boom(sid):
        raise gspread.exceptions.SpreadsheetNotFound(type("R", (), {"status_code": 404, "text": "", "json": lambda s: {}})())
    env.setattr(g, "_open_spreadsheet_sync", boom)
    with pytest.raises(g.GoogleFormError) as ei:
        asyncio.run(g.list_tabs("x"))
    assert ei.value.reason == "no_access"
    assert "bot@proj.iam.gserviceaccount.com" in ei.value.human and "Читатель" in ei.value.human
    form = _form()
    assert asyncio.run(g.sync_google_form(form)) == 0
    f = asyncio.run(ef.get_form(form["id"]))
    assert "Читатель" in f["sync_error"]


def test_list_tabs(env):
    env.setattr(g, "_open_spreadsheet_sync", lambda sid: _SH(_WS([HEAD], gid=7, title="Ответы")))
    assert asyncio.run(g.list_tabs("x")) == ("Табл", [(7, "Ответы")])


def test_poll_only_active_google(env):
    env.setattr(g, "_open_spreadsheet_sync", lambda sid: _SH(_WS([HEAD, ["t1", "a", "1"]])))
    active = _form(ext="a")
    paused = _form(ext="b")
    _form(platform="yandex", ext="c")
    asyncio.run(ef.set_form_status(paused["id"], "paused"))
    assert asyncio.run(g.poll_google_forms()) == 1
    assert asyncio.run(ef.count_answers(active["id"])) == 1
    assert asyncio.run(ef.count_answers(paused["id"])) == 0
