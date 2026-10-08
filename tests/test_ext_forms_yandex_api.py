"""Парсер ответов Яндекс Форм и httpx-клиент API (на MockTransport, без сети)."""
import asyncio
import glob
import json
import os

import httpx
import pytest

from services import ext_forms_parse as P

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ext_forms")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


# ---------------- парсер ----------------

def test_parse_synthetic_values_flattened():
    answered_at, items = P.parse_yandex_answer(_load("synthetic_answer_single.json"))
    assert answered_at == "2026-10-01 12:30:15"  # 09:30 UTC -> МСК
    assert [i["q"] for i in items] == ["111", "112", "113", "114", "115"]
    vals = {i["q"]: i["value"] for i in items}
    assert vals["111"] == "Аня Петрова"
    assert vals["112"] == "Спорт, Музыка"
    assert vals["113"] == "cv.pdf"
    assert vals["114"] == "21"
    assert vals["115"] == "True"
    assert items[0]["label"] == "Ваше имя"


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(FIX, "*answer_single*.json"))))
def test_parse_answer_fixtures(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    _, items = P.parse_yandex_answer(raw)
    assert items
    for it in items:
        assert isinstance(it["q"], str) and isinstance(it["label"], str) and isinstance(it["value"], str)


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(FIX, "*questions*.json"))))
def test_parse_questions_fixtures(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    pairs = P.parse_yandex_questions(raw)
    assert pairs
    assert all(isinstance(k, str) and isinstance(lbl, str) for k, lbl in pairs)


def test_parse_questions_order():
    assert P.parse_yandex_questions(_load("synthetic_questions.json")) == [
        ("111", "Ваше имя"), ("112", "Интересы"), ("113", "Резюме"), ("114", "Возраст")]


def test_parse_unknown_value_json():
    _, items = P.parse_yandex_answer({"data": [{"id": 1, "label": "x", "value": {"a": 1}}]})
    assert items[0]["value"] == '{"a": 1}'


# ---------------- клиент ----------------

from services import ext_forms_yandex as Y  # noqa: E402

CONN = {"access_token": "tok-" + "a" * 20, "org_id": None, "org_header": None}


def _patch(monkeypatch, handler):
    monkeypatch.setattr(
        Y, "_make_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))


def test_parse_form_id():
    assert Y.parse_form_id("https://forms.yandex.ru/cloud/6A94ad5c1f1eb5c9649c12bd/") == "6a94ad5c1f1eb5c9649c12bd"
    assert Y.parse_form_id("мусор") is None
    assert Y.parse_form_id("") is None


def test_get_answer_ok_and_headers(monkeypatch):
    seen = {}

    def handler(req):
        seen["h"] = req.headers
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"id": 5})

    _patch(monkeypatch, handler)
    assert asyncio.run(Y.get_answer(CONN, "5")) == {"id": 5}
    assert seen["h"]["authorization"] == "OAuth " + CONN["access_token"]
    assert "x-org-id" not in seen["h"]
    assert "answer_id=5" in seen["url"]

    conn = dict(CONN, org_id="777")
    asyncio.run(Y.get_answer(conn, "5"))
    assert seen["h"]["x-org-id"] == "777"
    conn = dict(CONN, org_id="777", org_header="X-Cloud-Org-Id")
    asyncio.run(Y.get_answer(conn, "5"))
    assert seen["h"]["x-cloud-org-id"] == "777"


@pytest.mark.parametrize("status,reason", [
    (401, "unauthorized"), (403, "forbidden"), (404, "not_found"),
    (429, "rate_limited"), (500, "upstream_unavailable"), (503, "upstream_unavailable"),
])
def test_get_answer_errors(monkeypatch, status, reason):
    _patch(monkeypatch, lambda req: httpx.Response(status, json={}))
    with pytest.raises(Y.YandexApiError) as ei:
        asyncio.run(Y.get_answer(CONN, "5"))
    assert ei.value.reason == reason
    assert ei.value.status == status
    assert "api.forms.yandex.net" not in str(ei.value) and CONN["access_token"] not in str(ei.value)


def test_get_answer_network_and_bad_json(monkeypatch):
    def boom(req):
        raise httpx.ConnectError("x " + CONN["access_token"])

    _patch(monkeypatch, boom)
    with pytest.raises(Y.YandexApiError) as ei:
        asyncio.run(Y.get_answer(CONN, "5"))
    assert ei.value.reason == "upstream_unavailable"
    assert CONN["access_token"] not in str(ei.value)

    _patch(monkeypatch, lambda req: httpx.Response(200, content=b"<html>"))
    with pytest.raises(Y.YandexApiError) as ei:
        asyncio.run(Y.get_answer(CONN, "5"))
    assert ei.value.reason == "bad_response"


def test_list_answer_ids_two_pages(monkeypatch):
    p1, p2 = _load("synthetic_answers_list_page1.json"), _load("synthetic_answers_list_page2.json")
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json=p2 if "id=1002" in str(req.url) else p1)

    _patch(monkeypatch, handler)
    ids = asyncio.run(Y.list_answer_ids(CONN, "6a94ad5c1f1eb5c9649c12bd"))
    assert ids == ["1001", "1002", "1003"]
    assert "ordering=asc" in calls[0]


def test_list_answer_ids_foreign_host_stops(monkeypatch):
    page = {"answers": [{"id": 1}], "next": {"next_url": "https://evil.example.com/x"}}
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json=page)

    _patch(monkeypatch, handler)
    assert asyncio.run(Y.list_answer_ids(CONN, "6a94ad5c1f1eb5c9649c12bd")) == ["1"]
    assert len(calls) == 1


def test_list_answer_ids_max_pages(monkeypatch):
    p1 = _load("synthetic_answers_list_page1.json")
    _patch(monkeypatch, lambda req: httpx.Response(200, json=p1))
    ids = asyncio.run(Y.list_answer_ids(CONN, "6a94ad5c1f1eb5c9649c12bd", max_pages=1))
    assert ids == ["1001", "1002"]


def test_get_survey_and_questions(monkeypatch):
    urls = []

    def handler(req):
        urls.append(req.url.path)
        return httpx.Response(200, json={"ok": 1})

    _patch(monkeypatch, handler)
    asyncio.run(Y.get_survey(CONN, "6a94ad5c1f1eb5c9649c12bd"))
    asyncio.run(Y.get_questions(CONN, "6a94ad5c1f1eb5c9649c12bd"))
    assert urls == ["/v1/surveys/6a94ad5c1f1eb5c9649c12bd", "/v1/surveys/6a94ad5c1f1eb5c9649c12bd/questions"]


def test_org_required_reason(monkeypatch):
    # 09.10 прод: форма личного аккаунта / вход без организации -> 400 «Требуется организация»
    _patch(monkeypatch, lambda req: httpx.Response(400, json={"detail": "Требуется организация"}))
    with pytest.raises(Y.YandexApiError) as ei:
        asyncio.run(Y.get_survey(CONN, "6aa022f0068ff027eaba0bcb"))
    assert ei.value.reason == "org_required"


@pytest.mark.parametrize("accepts, expected", [
    ("X-Org-Id", "X-Org-Id"),
    ("X-Cloud-Org-Id", "X-Cloud-Org-Id"),
    (None, None),
])
def test_detect_org_header(monkeypatch, accepts, expected):
    def handler(req):
        if accepts and req.headers.get(accepts) == "bpf1a2b3c4d5e6f7g8h9":
            return httpx.Response(200, json={"surveys": []})
        return httpx.Response(400, json={"detail": "Требуется организация"})
    _patch(monkeypatch, handler)
    assert asyncio.run(Y.detect_org_header(CONN, "bpf1a2b3c4d5e6f7g8h9")) == expected
