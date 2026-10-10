"""Личные Яндекс Формы (ingest_mode='push'): разбор тела, приёмник, очередь без API."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from database import db as bot_db
from database import ext_forms_db as ef
from services.ext_forms import ext_forms_yandex as Y
from services.ext_forms import ext_forms_yandex_sync as S
from services.ext_forms.ext_forms_parse import parse_push_body

from tests.test_miniapp_routes import _cfg, _client, _use_tmp_db

SECRET = "PUSHSECRET123abc"
FORM_EXT = "6aa022f0068ff027eaba0bcb"


# ---------- parse_push_body ----------

def test_parse_jsonrpc_params():
    body = {"jsonrpc": "2.0", "method": "x", "id": 1,
            "params": {"answer_id": 555, "answers": {"Имя": "Аня", "Телефон": "+7 900"}}}
    r = parse_push_body(body)
    assert r["answer_id"] == "555"
    assert r["items"] == [
        {"q": "Имя", "label": "Имя", "value": "Аня"},
        {"q": "Телефон", "label": "Телефон", "value": "+7 900"},
    ]


def test_parse_flat_json():
    r = parse_push_body({"answer_id": "a-1", "answers": {"Q": "v"}})
    assert r["answer_id"] == "a-1" and r["items"][0]["value"] == "v"


def test_parse_answers_as_json_string():
    r = parse_push_body({"answer_id": "7", "answers": json.dumps({"Q": "v"}, ensure_ascii=False)})
    assert r["items"] == [{"q": "Q", "label": "Q", "value": "v"}]


def test_parse_list_and_dict_values():
    r = parse_push_body({"answer_id": "7", "answers": {"A": ["a", "b"], "B": {"text": "t"}}})
    assert [i["value"] for i in r["items"]] == ["a, b", "t"]


def test_parse_answers_list_of_dicts_and_api_format():
    r = parse_push_body({"answer_id": "7", "answers": [
        {"id": "q1", "label": "Имя", "value": "Аня"}, {"key": "q2", "question": "Город", "answer": ["М"]}]})
    assert r["items"] == [
        {"q": "q1", "label": "Имя", "value": "Аня"},
        {"q": "q2", "label": "Город", "value": "М"},
    ]
    r = parse_push_body({"answer_id": "7", "answers": {"data": [{"id": "x", "label": "L", "value": "v"}]}})
    assert r["items"] == [{"q": "x", "label": "L", "value": "v"}]


@pytest.mark.parametrize("body", [
    {"answer_id": "1"}, {"answer_id": "1", "answers": "не json {"}, {"answer_id": "1", "answers": {}},
    [], "str", 5, None,
])
def test_parse_no_answers_no_exception(body):
    assert parse_push_body(body)["items"] is None


def test_parse_header_answer_id_and_created():
    r = parse_push_body({"answers": {"Q": "v"}, "created": "2026-10-09T10:00:00Z"},
                        header_answer_id="42")
    assert r["answer_id"] == "42"
    assert r["created"] == "2026-10-09 13:00:00"
    assert parse_push_body({"answer_id": "bad id!"})["answer_id"] is None


# ---------- приёмник ----------

@pytest.fixture
def env(tmp_path):
    path = _use_tmp_db(tmp_path)
    form_id = asyncio.run(ef.create_form(
        platform="yandex", external_id=FORM_EXT, title="T", secret=SECRET, ingest_mode="push"))
    return path, form_id


def _rows():
    async def run():
        async with bot_db._connect() as db:
            cur = await db.execute("SELECT form_id, answer_id, payload FROM external_form_pending")
            return [tuple(r) for r in await cur.fetchall()]
    return asyncio.run(run())


def _post(client, body, headers=None, secret=SECRET):
    return client.post(f"/app/hooks/yform/{secret}", json=body, headers=headers or {})


GOOD = {"jsonrpc": "2.0", "method": "m", "id": 3,
        "params": {"answer_id": "9001", "answers": {"Ник": "@ann"}}}


def test_push_ok_writes_pending_with_payload(env):
    path, form_id = env
    r = _post(_client(_cfg(path)), GOOD)
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "result": "ok", "id": 3}
    rows = _rows()
    assert len(rows) == 1 and rows[0][:2] == (form_id, "9001")
    assert json.loads(rows[0][2])["params"]["answers"] == {"Ник": "@ann"}


def test_push_repeat_single_row(env):
    path, _ = env
    c = _client(_cfg(path))
    _post(c, GOOD)
    _post(c, GOOD)
    assert len(_rows()) == 1


def test_push_answer_id_from_header(env):
    path, _ = env
    body = {"answers": {"Q": "v"}}
    r = _post(_client(_cfg(path)), body, headers={"X-Form-Answer-Id": "77"})
    assert r.status_code == 200
    assert [x[1] for x in _rows()] == ["77"]


def test_push_no_answer_id_nothing_written(env):
    path, _ = env
    r = _post(_client(_cfg(path)), {"answers": {"Q": "v"}})
    assert r.status_code == 200 and _rows() == []


def test_push_without_answers_sets_warning_and_does_not_log_values(env, caplog):
    path, form_id = env
    caplog.set_level(logging.INFO)
    r = _post(_client(_cfg(path)), {"params": {"answer_id": "5", "secret_value": "ТАЙНА"}})
    assert r.status_code == 200
    assert len(_rows()) == 1  # тело сохранено в очередь — разбор можно поправить позже
    form = asyncio.run(ef.get_form(form_id))
    assert "без ответов" in form["push_warning"]
    assert "ТАЙНА" not in caplog.text
    assert "params.secret_value" in caplog.text


def test_push_form_id_checks(env):
    path, _ = env
    c = _client(_cfg(path))
    assert _post(c, GOOD, headers={"X-Form-Id": "f" * 24}).status_code == 403
    bad_param = {"params": {"answer_id": "1", "form_id": "f" * 24, "answers": {"Q": "v"}}}
    assert _post(c, bad_param).status_code == 403
    assert _post(c, GOOD, secret="nope").status_code == 403
    assert _rows() == []
    assert _post(c, GOOD, headers={"X-Form-Id": FORM_EXT}).status_code == 200
    assert len(_rows()) == 1


# ---------- drain_pending / джобы ----------

def _enqueue_push(form_id, answer_id, body):
    asyncio.run(ef.enqueue_pending(form_id, answer_id, None, "2020-01-01 00:00:00",
                                   payload=json.dumps(body, ensure_ascii=False)))


def _no_api(monkeypatch):
    calls = []

    async def boom(*a, **k):
        calls.append(1)
        raise AssertionError("API Яндекса не должно быть")

    monkeypatch.setattr(Y, "get_answer", boom)
    monkeypatch.setattr(Y, "list_answer_ids", boom)
    monkeypatch.setattr(S, "ensure_fresh_token", boom)
    return calls


def test_drain_push_saves_without_api(env, monkeypatch):
    _, form_id = env
    calls = _no_api(monkeypatch)
    asyncio.run(ef.set_form_push_warning(form_id, "старая проблема"))
    body = {"params": {"answer_id": "9001", "created": "2026-10-09T10:00:00Z",
                       "answers": {"Ник": "@ann"}}}
    _enqueue_push(form_id, "9001", body)
    res = asyncio.run(S.drain_pending())
    assert res["done"] == 1 and calls == []
    row = asyncio.run(ef.get_answer(form_id, "9001"))
    assert row["payload"] == [{"q": "Ник", "label": "Ник", "value": "@ann"}]
    assert row["answered_at"] == "2026-10-09 13:00:00"
    assert json.loads(row["raw"])["params"]["answer_id"] == "9001"
    assert _rows() == []
    assert asyncio.run(ef.get_form(form_id))["push_warning"] is None


def test_drain_push_duplicate_of_saved_answer(env, monkeypatch):
    _, form_id = env
    _no_api(monkeypatch)
    body = {"params": {"answer_id": "1", "answers": {"Q": "v"}}}
    _enqueue_push(form_id, "1", body)
    asyncio.run(S.drain_pending())
    _enqueue_push(form_id, "1", body)  # вебхук повторился после сохранения
    asyncio.run(S.drain_pending())
    assert asyncio.run(ef.count_answers(form_id)) == 1
    assert _rows() == []


def test_drain_push_without_payload_dropped(env, monkeypatch):
    _, form_id = env
    _no_api(monkeypatch)
    asyncio.run(ef.enqueue_pending(form_id, "3", None, "2020-01-01 00:00:00"))
    res = asyncio.run(S.drain_pending())
    assert res["dropped"] == 1 and _rows() == []


def test_reconcile_and_backfill_skip_push(env, monkeypatch):
    _, form_id = env
    calls = _no_api(monkeypatch)
    assert asyncio.run(S.backfill_form(form_id)) == 0
    assert asyncio.run(S.reconcile_all())["enqueued"] == 0
    assert calls == []
    assert asyncio.run(ef.get_form(form_id))["sync_error"] is None


def test_push_answers_double_escaped_string_from_prod():
    # Прод 09.10: интеграция «JSON-RPC POST» прислала answers строкой с \" и \uXXXX внутри.
    import json as _json
    from services.ext_forms.ext_forms_parse import parse_push_body

    inner = r'{\"ФИО\": \"фвфы\", \"Ник в телеграмме (через @)\": \"awdaw\"}'
    body = _json.loads(_json.dumps({"jsonrpc": "2.0", "method": "answer", "id": 1,
                                    "params": {"answer_id": "2549315454", "answers": inner}}))
    parsed = parse_push_body(body)
    assert parsed["answer_id"] == "2549315454"
    got = {i["q"]: i["value"] for i in parsed["items"]}
    assert got == {"ФИО": "фвфы", "Ник в телеграмме (через @)": "awdaw"}


def test_push_answers_single_encoded_string_still_works():
    from services.ext_forms.ext_forms_parse import parse_push_body
    parsed = parse_push_body({"params": {"answer_id": "1", "answers": '{"\u0424\u0418\u041e": "x"}'}})
    assert [(i["q"], i["value"]) for i in parsed["items"]] == [("ФИО", "x")]


def test_push_keys_trimmed_like_export_header():
    from services.ext_forms.ext_forms_parse import parse_push_body
    parsed = parse_push_body({"params": {"answer_id": "1", "answers": {"Название университета ": "МГУ"}}})
    assert [(i["q"], i["label"]) for i in parsed["items"]] == [("Название университета", "Название университета")]
