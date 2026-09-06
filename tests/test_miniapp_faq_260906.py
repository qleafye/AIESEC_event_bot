"""Quick 260906-8uq (FAQ-01..06), задача 5: API делегатского раздела «❓ Частые вопросы» в
Mini App — `GET /app/api/faq`. Правило видимости (перекрытие городского пункта над общим) —
ОДНО место, `services/faq.py`, здесь второй копии нет, только контракт HTTP поверх него.

Харнесс — `tests/test_miniapp_routes.py` (тот же процесс/БД, что у соседних роутеров).
Задача 6 дописывает тесты `POST /app/api/faq` (кнопка «В FAQ» менеджера) отдельным блоком в
конце этого же файла.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _use_tmp_db,
)

REG_MANAGER_ID = 900750
DELEGATE_ID = 900752
KZN_DELEGATE_ID = 900753


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_faq.db")
    _seed(
        staff=[(REG_MANAGER_ID, "reg_manager", None)],
        users=[(DELEGATE_ID, "approved"), (KZN_DELEGATE_ID, "approved")],
        settings={"miniapp_enabled": "on", "event_name": "форума YouLead"},
    )
    return _client(_cfg(db_path))


def _set_user_city(tid, city):
    """Форма `tests/test_miniapp_questions_260904.py::_set_user_city` — `add_user` ON
    CONFLICT перезаписывает `event_city` даже для уже засеянного `_seed()` делегата."""
    _run(bot_db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}", "event_city": city,
        "registration_date": "2026-01-01 00:00:00",
    }))


@pytest.fixture
def _restore_cities_cache():
    """Форма `tests/test_faq_260906.py::_restore_cities_cache` — `cities.reload_cities()`
    мутирует `cities.CITIES` НА МЕСТЕ, conftest.py проекта не сбрасывает состояние между
    тестами/файлами."""
    import cities
    snapshot = list(cities.CITIES)
    yield
    cities.CITIES.clear()
    cities.CITIES.extend(snapshot)


def _seed_kzn_city():
    # "msk" первым (sort_order 0) — тот же приём, что tests/test_faq_260906.py::_seed_cities:
    # DELEGATE_ID без явного event_city обязан нормализоваться в «Москву», а не молча
    # унаследовать единственный зарегистрированный город (default_city_code() падает на
    # CITIES[0], когда config.EVENT_CITY_DEFAULT не зарегистрирован).
    _run(bot_db.insert_city("msk", "Москва", "", 0))
    _run(bot_db.insert_city("kzn", "Казань", "", 1))
    import cities
    _run(cities.reload_cities())


# ── права / раздел ───────────────────────────────────────────────────────────────────────

def test_faq_no_auth_401(client):
    resp = client.get("/app/api/faq")
    assert resp.status_code == 401
    assert resp.json() == {"reason": "no_auth"}


def test_faq_section_off_403(client):
    _set("miniapp_section_faq", "off")
    resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "faq"}


def test_faq_staff_only_mode_blocks_delegate(client):
    _set("miniapp_staff_only", "on")
    resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "delegate_gate"


def test_faq_unregistered_user_403_delegate_gate(client):
    resp = client.get("/app/api/faq", headers=_hdr(900999))
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "delegate_gate" and body["kind"] == "unregistered"


# ── GET /app/api/faq — список, пустое состояние, город ──────────────────────────────────

def test_faq_empty_returns_empty_text_not_empty_screen(client):
    resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["empty_text"]  # текст из реестра, не пустая строка


def test_faq_lists_general_item(client):
    _run(bot_db.create_faq_item(city=None, question="Где проходит форум?", answer="В кампусе.", created_by=REG_MANAGER_ID))
    resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["question"] == "Где проходит форум?"
    assert body["items"][0]["answer"] == "В кампусе."


def test_faq_disabled_item_never_appears(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Скрытый?", answer="a", created_by=REG_MANAGER_ID))
    _run(bot_db.update_faq_item(item_id, enabled=0))
    resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    assert resp.json()["items"] == []


def test_faq_city_module_off_shows_only_general(client):
    _run(bot_db.create_faq_item(city=None, question="Общий вопрос?", answer="a", created_by=REG_MANAGER_ID))
    _run(bot_db.create_faq_item(city="kzn", question="Казанский вопрос?", answer="b", created_by=REG_MANAGER_ID))
    resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    questions = [i["question"] for i in resp.json()["items"]]
    assert questions == ["Общий вопрос?"]


def test_faq_city_override_shows_only_city_answer(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    _set_user_city(KZN_DELEGATE_ID, "kzn")
    general = _run(bot_db.create_faq_item(city=None, question="Где проходит форум?", answer="Общий ответ.", created_by=REG_MANAGER_ID))
    kzn = _run(bot_db.create_faq_item(city="kzn", question="где проходит форум??", answer="Казанский ответ.", created_by=REG_MANAGER_ID))

    resp = client.get("/app/api/faq", headers=_hdr(KZN_DELEGATE_ID))
    items = resp.json()["items"]
    ids = [i["id"] for i in items]
    assert kzn in ids and general not in ids
    assert items[0]["answer"] == "Казанский ответ."


def test_faq_other_city_delegate_does_not_see_kzn_item(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    _set_user_city(KZN_DELEGATE_ID, "kzn")
    # DELEGATE_ID остаётся без города (msk, дефолт) — казанский пункт ему не виден.
    _run(bot_db.create_faq_item(city="kzn", question="Только Казань?", answer="k", created_by=REG_MANAGER_ID))

    resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    assert resp.json()["items"] == []


# ── проводка (SECTIONS/SECTION_KEYS/навигация) ───────────────────────────────────────────

def test_faq_wired_into_sections_and_nav():
    from miniapp.deps import SECTIONS
    from handlers.admin_miniapp import SECTION_KEYS
    from settings_schema import SETTINGS_SCHEMA

    assert "faq" in SECTIONS
    assert "miniapp_section_faq" in SECTION_KEYS
    assert set(SECTION_KEYS) == {f"miniapp_section_{s}" for s in SECTIONS}
    assert SETTINGS_SCHEMA["miniapp_section_faq"]["default"] == "on"


def test_faq_router_registered_in_all_routers():
    from miniapp.routers import ALL_ROUTERS
    from miniapp.routers.faq import router as faq_router

    assert faq_router in ALL_ROUTERS
