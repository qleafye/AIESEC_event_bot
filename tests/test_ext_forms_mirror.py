"""Зеркало анкет внешних форм в таблицу: шапка, дописывание, обновление по ID ответа."""
import asyncio

import pytest

from config import config
from database import db
from database import ext_forms_db as ef
from services import ext_forms_mirror as mir
from tests._dbtpl import fast_init_db


class FakeWS:
    def __init__(self):
        self.rows = []  # включая шапку
        self.calls = []

    def row_values(self, n):
        return list(self.rows[n - 1]) if len(self.rows) >= n else []

    def update(self, rng, values, value_input_option=None):
        self.calls.append(("update", rng, value_input_option))
        self.rows = [list(values[0])] + self.rows[1:]

    def update_cell(self, r, c, v):
        self.calls.append(("update_cell", r, c, v))
        hdr = self.rows[0]
        hdr.extend([""] * (c - len(hdr)))
        hdr[c - 1] = v

    def append_rows(self, rows, value_input_option=None):
        self.calls.append(("append_rows", len(rows), value_input_option))
        self.rows.extend([list(r) for r in rows])

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.rows]

    def batch_update(self, data, value_input_option=None):
        self.calls.append(("batch_update", value_input_option))
        for d in data:
            r = int(d["range"].split(":")[0][1:])
            self.rows[r - 1][1:3] = d["values"][0]

    def insert_cols(self, *a, **k):
        raise AssertionError("вставка колонок запрещена")


@pytest.fixture
def env(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "mirror.db")
    fast_init_db()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sid")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    ws = FakeWS()
    state = {"ws": ws, "exists": True, "created": 0}

    def open_tab(tab):
        return state["ws"] if state["exists"] else None

    def create_tab(title, cols):
        state["created"] += 1
        state["exists"] = True
        return state["ws"]

    monkeypatch.setattr(mir, "_open_tab_sync", open_tab)
    monkeypatch.setattr(mir, "_create_tab_sync", create_tab)
    return state


def _form(tab="Вкладка"):
    fid = asyncio.run(ef.create_form(platform="yandex", external_id="f", title="Ф", mirror_tab=tab))
    return fid


def _answer(fid, aid, items, tid=None):
    asyncio.run(ef.upsert_columns(fid, [(i["q"], i["label"]) for i in items]))
    asyncio.run(ef.insert_answer(
        form_id=fid, answer_id=aid, answered_at="2026-10-01 12:00:00",
        received_at="2026-10-01 12:00:00", payload=items, raw=None,
        matched_telegram_id=tid, match_how="x" if tid else None,
    ))


def _state(aid_ids=None):
    async def go():
        async with db._connect() as c:
            async with c.execute("SELECT answer_id, sheet_state FROM external_form_answers") as cur:
                return {r[0]: r[1] for r in await cur.fetchall()}
    return asyncio.run(go())


def _user(tid, status="approved"):
    async def go():
        async with db._connect() as c:
            await c.execute(
                "INSERT INTO users (telegram_id, username, full_name, status) VALUES (?,?,?,?)",
                (tid, "ivan", "Иван Петров", status))
            await c.commit()
    asyncio.run(go())


def test_row_matched_and_unmatched():
    cols = [{"qkey": "a", "position": 1}, {"qkey": "c", "position": 3}]
    ans = {"answered_at": "T", "answer_id": "9",
           "payload": [{"q": "a", "value": "x"}, {"q": "c", "value": "z"}]}
    user = {"full_name": "Иван Петров", "username": "ivan", "status": "approved"}
    assert mir.build_row(ans, cols, user) == ["T", "Иван Петров @ivan", "✅ Одобрена", "9", "x", "", "z"]
    row = mir.build_row(ans, cols, None)
    assert row[1:3] == ["не найден", "—"]
    assert mir.build_row(ans, cols, {"full_name": "И", "username": "-", "status": "pending"})[1] == "И"


def test_create_tab(env):
    env["exists"] = False
    fid = _form()
    asyncio.run(ef.upsert_columns(fid, [("a", "Вопрос А")]))
    assert asyncio.run(mir.create_mirror_tab(fid, "Вкладка")) == "ok"
    assert env["created"] == 1
    assert env["ws"].rows[0] == mir.FIXED_HEADERS + ["Вопрос А"]
    assert asyncio.run(mir.create_mirror_tab(fid, "Вкладка")) == "exists"
    assert env["created"] == 1


def test_create_unavailable(env, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    assert asyncio.run(mir.create_mirror_tab(_form(), "В")) == "unavailable"


def test_append_batch_and_new_column_right(env):
    fid = _form()
    for i in range(3):
        _answer(fid, f"a{i}", [{"q": "q1", "label": "Q1", "value": f"v{i}"}])
    res = asyncio.run(mir.drain_mirror())
    assert res["appended"] == 3
    ws = env["ws"]
    assert [c for c in ws.calls if c[0] == "append_rows"] == [("append_rows", 3, mir._raw())]
    assert ws.rows[0] == mir.FIXED_HEADERS + ["Q1"]
    assert set(_state().values()) == {"synced"}
    # новый вопрос — ячейка шапки справа
    _answer(fid, "a9", [{"q": "q2", "label": "Q2", "value": "n"}])
    asyncio.run(mir.drain_mirror())
    assert ws.rows[0][5] == "Q2"
    assert ("update_cell", 1, 6, "Q2") in ws.calls


def test_update_by_answer_id_after_resort(env):
    fid = _form()
    _answer(fid, "a1", [{"q": "q1", "label": "Q1", "value": "v"}])
    _answer(fid, "a2", [{"q": "q1", "label": "Q1", "value": "w"}])
    asyncio.run(mir.drain_mirror())
    ws = env["ws"]
    ws.rows[1], ws.rows[2] = ws.rows[2], ws.rows[1]  # пересортировка
    _user(5)
    asyncio.run(ef.set_answer_match(1, 5, "username"))
    asyncio.run(ef.mark_sheet_state([1], "update"))
    res = asyncio.run(mir.drain_mirror())
    assert res["updated"] == 1
    row = [r for r in ws.rows if r[3] == "a1"][0]
    assert row[1:3] == ["Иван Петров @ivan", "✅ Одобрена"]
    other = [r for r in ws.rows if r[3] == "a2"][0]
    assert other[1] == "не найден"


def test_update_missing_row_appended(env):
    fid = _form()
    _answer(fid, "a1", [{"q": "q1", "label": "Q1", "value": "v"}])
    asyncio.run(mir.drain_mirror())
    env["ws"].rows = env["ws"].rows[:1]
    asyncio.run(ef.mark_sheet_state([1], "update"))
    asyncio.run(mir.drain_mirror())
    assert len(env["ws"].rows) == 2
    assert set(_state().values()) == {"synced"}


def test_tab_deleted(env):
    fid = _form()
    _answer(fid, "a1", [{"q": "q1", "label": "Q1", "value": "v"}])
    env["exists"] = False
    res = asyncio.run(mir.drain_mirror())
    assert res["not_found"] == 1
    assert env["created"] == 0
    form = asyncio.run(ef.get_form(fid))
    assert "выберите вкладку заново" in form["mirror_error"]
    assert _state() == {"a1": "append"}


def test_google_failure_backoff(env):
    fid = _form()
    _answer(fid, "a1", [{"q": "q1", "label": "Q1", "value": "v"}])

    def boom(*a, **k):
        raise RuntimeError("quota")
    env["ws"].append_rows = boom
    res = asyncio.run(mir.drain_mirror())
    assert res["failed"] == 1
    assert _state() == {"a1": "append"}

    async def attempts():
        async with db._connect() as c:
            async with c.execute("SELECT sheet_attempts, sheet_next_try_at FROM external_form_answers") as cur:
                return await cur.fetchone()
    n, nxt = asyncio.run(attempts())
    assert n == 1 and nxt


def test_sheets_off_skips(env, monkeypatch):
    fid = _form()
    _answer(fid, "a1", [{"q": "q1", "label": "Q1", "value": "v"}])
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    asyncio.run(mir.drain_mirror())
    assert _state() == {"a1": "skip"}
    assert env["ws"].calls == []


def test_narrow_existing_tab_gets_columns(env):
    """Узкая вкладка, выбранная менеджером: сетка расширяется до записи шапки."""
    ws = env["ws"]
    ws.col_count = 3
    added = []
    ws.add_cols = lambda n: added.append(n)
    fid = _form()
    _answer(fid, "1", [{"q": "a", "label": "А", "value": "x"}])
    asyncio.run(mir.drain_mirror())
    assert added == [len(mir.FIXED_HEADERS) + 1 - 3]


def test_foreign_header_tab_not_written(env):
    """Вкладка с чужой шапкой: ничего не пишем, форма получает понятную ошибку."""
    ws = env["ws"]
    ws.rows = [["ID", "Имя", "Город"], ["1", "Аня", "Москва"]]
    fid = _form("Регистрации")
    _answer(fid, "a1", [{"q": "q1", "label": "Q1", "value": "v"}])
    res = asyncio.run(mir.drain_mirror())
    assert res["not_found"] == 1
    assert ws.calls == []
    assert ws.rows == [["ID", "Имя", "Город"], ["1", "Аня", "Москва"]]
    err = asyncio.run(ef.get_form(fid))["mirror_error"]
    assert "«Регистрации»" in err and "чужие данные" in err
    assert _state() == {"a1": "append"}
