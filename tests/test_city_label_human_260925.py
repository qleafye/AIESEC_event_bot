"""Город человеку — названием, не кодом, даже при выключенном модуле городов.

Баг: на плашке сканера Mini App город выводился сырым кодом «msk», когда `event_city_enabled`
выключен (ответ отдавал `users.event_city` как есть, JS рисовал `res.city`). Правило CLAUDE.md:
кодовые значения человеку не показываем. Модуль городов выключен — выбор города спрятан, но
код в `users.event_city`/в QR остаётся, и доходить до человека он не должен.

Сторожа: функция подписи для каждого кода реестра, ответы сканера (новый/дубль/отказ чужой
город, поиск), статический скан JS сканера/заявок, CSV «Статистики прихода» и отчёт загрузки
CSV-отметок в боте."""
from __future__ import annotations
from tests._paths import REPO_ROOT

import asyncio
import csv
import io
import re
from pathlib import Path

import pytest

import domain.cities as cities_mod
from config import config as bot_config
from database import db as bot_db
from handlers import admin_checkin, admin_checkin_stats
from services.checkin import ENTRY_POINT, build_payload, current_event_tag

from tests.test_miniapp_checkin_260924 import (
    BASE,
    GAME_MANAGER_ID,
    TAG,
    _grant_checkin_to_game_manager,
    _hdr,
    _insert_user,
    client_with,
    _token,
)

ROOT = REPO_ROOT
_REGISTRY = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
    {"code": "tyumen", "label": "Тюмень", "tab_base": "Тюмень", "enabled": 1, "sort_order": 2},
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _known_registry():
    saved = cities_mod.all_cities()
    cities_mod.set_cities_for_test([dict(c) for c in _REGISTRY])
    yield
    cities_mod.set_cities_for_test(saved)


def _module_off():
    assert _run(cities_mod.cities_module_on()) is False


def _no_code_leaks(body: dict, code: str = "msk") -> None:
    """Кроме поля `city` (код для логики) ни одна строка ответа не несёт код города."""
    for key, value in body.items():
        if key == "city":
            continue
        if isinstance(value, str):
            assert not re.search(rf"\b{code}\b", value), (key, value)


# ── (а) функция подписи ──────────────────────────────────────────────────────────────────────

def test_label_for_every_registry_code_is_not_the_code_with_module_off(tmp_path):
    client_with(tmp_path)  # свежая база, модуль городов выключен (дефолт реестра)
    _module_off()
    for c in cities_mod.all_cities():
        label = _run(cities_mod.city_label_or_none(c["code"]))
        assert label and label != c["code"], c
        assert _run(cities_mod.city_label(c["code"])) == label
    assert _run(cities_mod.city_labels_map())["msk"] == "Москва"


def test_label_empty_and_unknown_code():
    assert _run(cities_mod.city_label_or_none(None)) is None
    assert _run(cities_mod.city_label_or_none("")) is None
    assert _run(cities_mod.city_label_or_none("—")) is None  # плейсхолдер пустого города в QR


def test_label_setting_override_wins_with_module_off(tmp_path):
    client_with(tmp_path)
    _run(bot_db.set_setting("city_label__msk", "Москва (ВДНХ)"))
    assert _run(cities_mod.city_label_or_none("msk")) == "Москва (ВДНХ)"


# ── (б) ответы сканера Mini App при выключенном модуле ──────────────────────────────────────

def _qr_with_code(uid, code="msk") -> str:
    # Настоящий QR несёт КОД города (`services.checkin.build_qr_payload` -> users.event_city).
    return build_payload(TAG, "Иванов Иван", code, _token(uid))


def test_scan_new_and_duplicate_carry_human_city(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _module_off()
    uid = 951001
    _run(_insert_user(uid, city="msk"))
    for expected in ("new", "duplicate"):
        body = client.post(f"{BASE}/scan", json={"payload": _qr_with_code(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
        assert body["status"] == expected
        assert body["city"] == "msk"  # код остаётся для логики
        assert body["city_label"] == "Москва"
        _no_code_leaks(body)


def test_scan_denied_and_foreign_event_carry_human_city(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _module_off()
    uid = 951002
    _run(_insert_user(uid, city="msk", status="pending"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr_with_code(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "denied" and body["city_label"] == "Москва"
    _no_code_leaks(body)

    foreign = build_payload("CHUZHOY", "Иванов Иван", "msk", "tok")
    body = client.post(f"{BASE}/scan", json={"payload": foreign}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "foreign_event" and body["city_label"] == "Москва"
    _no_code_leaks(body)


def test_scan_other_city_session_denial_carries_human_city(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _module_off()
    uid = 951003
    _run(_insert_user(uid, city="msk"))
    sid = _run(bot_db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    body = client.post(
        f"{BASE}/scan", json={"payload": _qr_with_code(uid), "point": f"session:{sid}"},
        headers=_hdr(GAME_MANAGER_ID),
    ).json()
    assert body["status"] == "wrong_city"
    assert body["city_label"] == "Москва"
    _no_code_leaks(body)
    _no_code_leaks(body, "spb")


def test_manual_and_search_carry_human_city(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _module_off()
    uid = 951004
    _run(_insert_user(uid, full_name="Кузнецова Кира", city="msk"))
    items = client.get(f"{BASE}/search", params={"q": "Кузнецова"}, headers=_hdr(GAME_MANAGER_ID)).json()["items"]
    assert items[0]["city_label"] == "Москва"
    body = client.post(f"{BASE}/manual", json={"telegram_id": uid}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new" and body["city_label"] == "Москва"
    _no_code_leaks(body)


# ── (в) JS показывает подпись, не код ────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "miniapp/static/js/screens/scanner.js",
    "miniapp/static/js/screens/applications.js",
])
def test_js_renders_city_label_not_city_code(rel):
    """В тексте экрана — только `*.city_label`. `*.city` допустим лишь для логики (список ниже)."""
    src = (ROOT / rel).read_text(encoding="utf-8")
    allowed = (
        "citySelect.value = data.city",  # значение <select>, не текст
        "city: res.city,",  # проброс поля в плашку отмены
        "me.city != null ? me.city",  # привязка менеджера уходит в запрос, не на экран
    )
    for line in src.splitlines():
        if re.search(r"\b\w+\.city\b(?!_)", line) and not any(a in line for a in allowed):
            pytest.fail(f"{rel}: город выводится кодом — `{line.strip()}` (нужен .city_label)")


# ── бот: CSV «Статистики прихода» и отчёт загрузки отметок ───────────────────────────────────

class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self):
        self.sent = []
        self.docs = []
        self.edits = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **k):
        self.sent.append((text, reply_markup))

    async def answer_document(self, document, caption=None, **k):
        self.docs.append((document, caption))


class _Cb:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg()
        self.bot = None

    async def answer(self, *a, **k):
        return None


class _State:
    def __init__(self, data):
        self.data = dict(data)

    async def get_data(self):
        return dict(self.data)

    async def set_state(self, value):
        return None

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_data(self, data):
        self.data = dict(data)


ADMIN = 951900


def _bot_ready(tmp_path):
    bot_config.DB_PATH = str(tmp_path / "city_label_bot.db")
    from tests._dbtpl import fast_init_db
    fast_init_db()
    bot_config.ADMIN_IDS = [ADMIN]
    _run(bot_db.set_setting("event_season", "YL'26"))


def test_stats_csv_session_city_is_human_with_module_off(tmp_path):
    _bot_ready(tmp_path)
    _module_off()
    _run(_insert_user(951010, city="msk", season="YL'26"))
    sid = _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    _run(bot_db.record_checkin(951010, "entry", source="miniapp", scanned_at="2026-10-30 09:00:00"))
    _run(bot_db.record_checkin(951010, f"session:{sid}", source="miniapp", scanned_at="2026-10-30 10:05:00"))
    cb = _Cb("checkin_stats_csv", ADMIN)
    _run(admin_checkin_stats.checkin_stats_csv(cb))
    doc, _caption = cb.message.docs[0]
    rows = list(csv.reader(io.StringIO(doc.data.decode("utf-8-sig")), delimiter=";"))
    session_rows = [r for r in rows if "Открытие" in r]
    assert session_rows and session_rows[0][0] == "Москва"
    assert not any(cell == "msk" for r in rows for cell in r)


def test_csv_upload_report_names_city_not_code(tmp_path):
    _bot_ready(tmp_path)
    _module_off()
    uid = 951011
    _run(_insert_user(uid, full_name="Орлов Олег", city="msk", status="pending"))
    qr = build_payload(_run(current_event_tag()), "Орлов Олег", "msk", _token(uid))
    state = _State({"checkin_records": [{"qr": qr, "scanned_at": "2026-10-30 09:10:00"}]})
    cb = _Cb(f"checkin_point:{ENTRY_POINT}", ADMIN)
    _run(admin_checkin.checkin_point_pick(cb, state))
    report = next(t for t, _kb in cb.message.sent if t and "Отметки загружены" in t)
    assert "Орлов Олег · Москва" in report
    assert not re.search(r"\bmsk\b", report)
