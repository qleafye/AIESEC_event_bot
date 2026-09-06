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
BOUND_REG_MANAGER_ID = 900751
DELEGATE_ID = 900752
KZN_DELEGATE_ID = 900753


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_faq.db")
    _seed(
        staff=[
            (REG_MANAGER_ID, "reg_manager", None),
            (BOUND_REG_MANAGER_ID, "reg_manager", "kzn"),
        ],
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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 6: POST /app/api/faq — кнопка «В FAQ» менеджера в приложении
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_faq_post_requires_moderate_reg_cap(client):
    resp = client.post("/app/api/faq", headers=_hdr(DELEGATE_ID), json={"question": "Q?", "answer": "A"})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


def test_faq_post_section_off_403(client):
    _set("miniapp_section_questions", "off")
    resp = client.post("/app/api/faq", headers=_hdr(REG_MANAGER_ID), json={"question": "Q?", "answer": "A"})
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "questions"}


def test_faq_post_empty_question_400(client):
    resp = client.post("/app/api/faq", headers=_hdr(REG_MANAGER_ID), json={"question": "  ", "answer": "A"})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "empty"


def test_faq_post_empty_answer_400(client):
    resp = client.post("/app/api/faq", headers=_hdr(REG_MANAGER_ID), json={"question": "Q?", "answer": " "})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "empty"


def test_faq_post_too_long_question_400(client):
    resp = client.post("/app/api/faq", headers=_hdr(REG_MANAGER_ID), json={"question": "Q" * 301, "answer": "A"})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "too_long"


def test_faq_post_creates_general_item_for_unbound_manager(client):
    resp = client.post("/app/api/faq", headers=_hdr(REG_MANAGER_ID), json={"question": "Где проходит форум?", "answer": "В кампусе."})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    row = _run(bot_db.get_faq_item(body["id"]))
    assert row["city"] is None
    assert row["question"] == "Где проходит форум?"
    assert row["answer"] == "В кампусе."


def test_faq_post_creates_city_item_for_bound_manager(client):
    resp = client.post(
        "/app/api/faq", headers=_hdr(BOUND_REG_MANAGER_ID),
        json={"question": "Где регистрация?", "answer": "У входа."},
    )
    assert resp.status_code == 200
    row = _run(bot_db.get_faq_item(resp.json()["id"]))
    assert row["city"] == "kzn"


def test_faq_post_duplicate_does_not_create_second_row(client):
    existing_id = _run(bot_db.create_faq_item(
        city=None, question="Где проходит форум??", answer="В кампусе.", created_by=REG_MANAGER_ID,
    ))
    resp = client.post(
        "/app/api/faq", headers=_hdr(REG_MANAGER_ID),
        json={"question": "где ПРОХОДИТ форум", "answer": "Другой ответ."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": False, "reason": "already", "id": existing_id}
    items = _run(bot_db.list_faq_items())
    assert len(items) == 1


def test_faq_post_duplicate_scoped_to_same_city_bucket(client):
    """Дубль ищем СРЕДИ пунктов ТОГО ЖЕ городского ведра — общий пункт с тем же вопросом не
    мешает менеджеру Казани завести свой городской пункт с другим ответом."""
    _run(bot_db.create_faq_item(city=None, question="Где проходит форум?", answer="Общий ответ.", created_by=REG_MANAGER_ID))
    resp = client.post(
        "/app/api/faq", headers=_hdr(BOUND_REG_MANAGER_ID),
        json={"question": "Где проходит форум?", "answer": "Казанский ответ."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    items = _run(bot_db.list_faq_items())
    assert len(items) == 2


def test_questions_list_exposes_can_add_to_faq_and_labels(client):
    qid = _run(bot_db.create_question(DELEGATE_ID, "Когда дедлайн?"))
    _run(bot_db.claim_question(qid, REG_MANAGER_ID, "Менеджер"))
    _run(bot_db.set_question_answer(qid, "Завтра в полдень."))
    new_qid = _run(bot_db.create_question(DELEGATE_ID, "Ещё вопрос?"))

    resp = client.get("/app/api/questions", headers=_hdr(REG_MANAGER_ID))
    body = resp.json()
    assert body["to_faq_button"]
    assert body["to_faq_saved_toast"]
    by_id = {i["id"]: i for i in body["items"]}
    assert by_id[qid]["can_add_to_faq"] is True
    assert by_id[new_qid]["can_add_to_faq"] is False


# ══════════════════════════════════════════════════════════════════════════════════════════
# Quick 260906-nxp, задача 1: менеджерский API /app/api/admin/faq
# ══════════════════════════════════════════════════════════════════════════════════════════

# ── права / раздел ───────────────────────────────────────────────────────────────────────

def test_admin_faq_list_no_auth_401(client):
    resp = client.get("/app/api/admin/faq")
    assert resp.status_code == 401
    assert resp.json() == {"reason": "no_auth"}


def test_admin_faq_list_requires_moderate_reg_cap(client):
    resp = client.get("/app/api/admin/faq", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


def test_admin_faq_list_section_off_403(client):
    _set("miniapp_section_faq", "off")
    resp = client.get("/app/api/admin/faq", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "faq"}


# ── GET — пустой список / город / номера / стрелки ──────────────────────────────────────

def test_admin_faq_list_empty_has_empty_text_and_no_city_choice_for_unbound(client):
    resp = client.get("/app/api/admin/faq", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["empty_text"]
    assert body["city_choice"] is False
    assert body["bound_city_label"] is None
    assert body["city_hint"]


def test_admin_faq_list_rows_carry_number_badge_status_and_edge_arrows(client):
    first = _run(bot_db.create_faq_item(city=None, question="Первый?", answer="a", created_by=REG_MANAGER_ID))
    second = _run(bot_db.create_faq_item(city=None, question="Второй?", answer="b", created_by=REG_MANAGER_ID))

    resp = client.get("/app/api/admin/faq", headers=_hdr(REG_MANAGER_ID))
    items = resp.json()["items"]
    assert [i["id"] for i in items] == [first, second]
    assert [i["number"] for i in items] == [1, 2]
    assert items[0]["city_badge"] == "🌍 все города"
    assert items[0]["is_general"] is True
    assert items[0]["status_text"] == "показывается делегатам"
    assert items[0]["toggle_label"] == "Скрыть"
    assert items[0]["can_move_up"] is False and items[0]["can_move_down"] is True
    assert items[1]["can_move_up"] is True and items[1]["can_move_down"] is False


def test_admin_faq_list_bound_manager_sees_own_city_and_general_not_other_city(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    general = _run(bot_db.create_faq_item(city=None, question="Общий?", answer="a", created_by=REG_MANAGER_ID))
    kzn = _run(bot_db.create_faq_item(city="kzn", question="Казанский?", answer="b", created_by=REG_MANAGER_ID))
    msk = _run(bot_db.create_faq_item(city="msk", question="Московский?", answer="c", created_by=REG_MANAGER_ID))

    resp = client.get("/app/api/admin/faq", headers=_hdr(BOUND_REG_MANAGER_ID))
    body = resp.json()
    ids = [i["id"] for i in body["items"]]
    assert general in ids and kzn in ids
    assert msk not in ids
    assert body["city_choice"] is True
    assert body["bound_city_label"] == "Казань"
    kzn_row = next(i for i in body["items"] if i["id"] == kzn)
    assert kzn_row["city_toggle_label"] == "Для всех городов"
    general_row = next(i for i in body["items"] if i["id"] == general)
    assert general_row["city_toggle_label"] == "Только Казань"


# ── POST — создание / дубли ──────────────────────────────────────────────────────────────

def test_admin_faq_create_general_item_for_unbound_manager_returns_item(client):
    resp = client.post(
        "/app/api/admin/faq", headers=_hdr(REG_MANAGER_ID),
        json={"question": "Где кампус?", "answer": "На набережной."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["item"]["question"] == "Где кампус?"
    assert body["item"]["is_general"] is True
    row = _run(bot_db.get_faq_item(body["id"]))
    assert row["city"] is None


def test_admin_faq_create_city_item_for_bound_manager(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    resp = client.post(
        "/app/api/admin/faq", headers=_hdr(BOUND_REG_MANAGER_ID),
        json={"question": "Где стойка регистрации?", "answer": "У входа."},
    )
    assert resp.status_code == 200
    row = _run(bot_db.get_faq_item(resp.json()["id"]))
    assert row["city"] == "kzn"


def test_admin_faq_create_duplicate_does_not_create_second_row(client):
    existing_id = _run(bot_db.create_faq_item(
        city=None, question="Где кампус??", answer="a", created_by=REG_MANAGER_ID,
    ))
    resp = client.post(
        "/app/api/admin/faq", headers=_hdr(REG_MANAGER_ID),
        json={"question": "где КАМПУС", "answer": "Другой ответ."},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "already", "id": existing_id}
    assert len(_run(bot_db.list_faq_items())) == 1


# ── PATCH — правка одним полем ───────────────────────────────────────────────────────────

def test_admin_faq_patch_question(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Старый вопрос?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.patch(f"/app/api/admin/faq/{item_id}", headers=_hdr(REG_MANAGER_ID), json={"question": "Новый вопрос?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["field"] == "question"
    assert body["item"]["question"] == "Новый вопрос?"
    row = _run(bot_db.get_faq_item(item_id))
    assert row["question"] == "Новый вопрос?"


def test_admin_faq_patch_answer(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Q?", answer="Старый ответ.", created_by=REG_MANAGER_ID))
    resp = client.patch(f"/app/api/admin/faq/{item_id}", headers=_hdr(REG_MANAGER_ID), json={"answer": "Новый ответ."})
    assert resp.status_code == 200
    assert resp.json()["item"]["answer"] == "Новый ответ."


def test_admin_faq_patch_enabled_hides_from_delegate_get(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.patch(f"/app/api/admin/faq/{item_id}", headers=_hdr(REG_MANAGER_ID), json={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["item"]["enabled"] is False
    assert body["item"]["status_text"] == "скрыт от делегатов"
    # Сквозная проверка: делегатский GET /app/api/faq больше не отдаёт скрытый пункт.
    delegate_resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    assert delegate_resp.json()["items"] == []


def test_admin_faq_patch_city_mine_for_bound_manager_writes_kzn(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    item_id = _run(bot_db.create_faq_item(city=None, question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.patch(f"/app/api/admin/faq/{item_id}", headers=_hdr(BOUND_REG_MANAGER_ID), json={"city": "mine"})
    assert resp.status_code == 200
    row = _run(bot_db.get_faq_item(item_id))
    assert row["city"] == "kzn"


def test_admin_faq_patch_city_all_writes_null(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    item_id = _run(bot_db.create_faq_item(city="kzn", question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.patch(f"/app/api/admin/faq/{item_id}", headers=_hdr(BOUND_REG_MANAGER_ID), json={"city": "all"})
    assert resp.status_code == 200
    row = _run(bot_db.get_faq_item(item_id))
    assert row["city"] is None


def test_admin_faq_patch_city_mine_for_unbound_manager_400_no_city_binding(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.patch(f"/app/api/admin/faq/{item_id}", headers=_hdr(REG_MANAGER_ID), json={"city": "mine"})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "no_city_binding"


def test_admin_faq_patch_two_fields_at_once_400_one_field(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.patch(
        f"/app/api/admin/faq/{item_id}", headers=_hdr(REG_MANAGER_ID),
        json={"question": "Другой?", "answer": "Другой."},
    )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "one_field"


def test_admin_faq_patch_empty_question_400(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.patch(f"/app/api/admin/faq/{item_id}", headers=_hdr(REG_MANAGER_ID), json={"question": "   "})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "empty"


# ── скоуп на мутациях — 403/404 ──────────────────────────────────────────────────────────

def test_admin_faq_patch_other_city_item_403_out_of_scope(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    msk_item = _run(bot_db.create_faq_item(city="msk", question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.patch(f"/app/api/admin/faq/{msk_item}", headers=_hdr(BOUND_REG_MANAGER_ID), json={"enabled": False})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "out_of_scope"


def test_admin_faq_move_other_city_item_403_out_of_scope(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    msk_item = _run(bot_db.create_faq_item(city="msk", question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.post(f"/app/api/admin/faq/{msk_item}/move", headers=_hdr(BOUND_REG_MANAGER_ID), json={"direction": "up"})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "out_of_scope"


def test_admin_faq_delete_other_city_item_403_out_of_scope(client, _restore_cities_cache):
    _seed_kzn_city()
    _set("event_city_enabled", "on")
    msk_item = _run(bot_db.create_faq_item(city="msk", question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.delete(f"/app/api/admin/faq/{msk_item}", headers=_hdr(BOUND_REG_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "out_of_scope"


def test_admin_faq_patch_nonexistent_404(client):
    resp = client.patch("/app/api/admin/faq/999999", headers=_hdr(REG_MANAGER_ID), json={"enabled": False})
    assert resp.status_code == 404
    assert resp.json()["reason"] == "not_found"


def test_admin_faq_move_nonexistent_404(client):
    resp = client.post("/app/api/admin/faq/999999/move", headers=_hdr(REG_MANAGER_ID), json={"direction": "up"})
    assert resp.status_code == 404


def test_admin_faq_delete_nonexistent_404(client):
    resp = client.delete("/app/api/admin/faq/999999", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 404


# ── move — порядок / край ────────────────────────────────────────────────────────────────

def test_admin_faq_move_up_swaps_actual_order(client):
    first = _run(bot_db.create_faq_item(city=None, question="Первый?", answer="a", created_by=REG_MANAGER_ID))
    second = _run(bot_db.create_faq_item(city=None, question="Второй?", answer="b", created_by=REG_MANAGER_ID))

    resp = client.post(f"/app/api/admin/faq/{second}/move", headers=_hdr(REG_MANAGER_ID), json={"direction": "up"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "moved": True}

    ordered_ids = [r["id"] for r in _run(bot_db.list_faq_items())]
    assert ordered_ids == [second, first]


def test_admin_faq_move_at_edge_returns_moved_false_without_error(client):
    only = _run(bot_db.create_faq_item(city=None, question="Единственный?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.post(f"/app/api/admin/faq/{only}/move", headers=_hdr(REG_MANAGER_ID), json={"direction": "up"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "moved": False}


def test_admin_faq_move_bad_direction_400(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.post(f"/app/api/admin/faq/{item_id}/move", headers=_hdr(REG_MANAGER_ID), json={"direction": "sideways"})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "bad_direction"


# ── delete ────────────────────────────────────────────────────────────────────────────────

def test_admin_faq_delete_removes_row_second_delete_404(client):
    item_id = _run(bot_db.create_faq_item(city=None, question="Q?", answer="a", created_by=REG_MANAGER_ID))
    resp = client.delete(f"/app/api/admin/faq/{item_id}", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": True}
    assert _run(bot_db.get_faq_item(item_id)) is None

    resp2 = client.delete(f"/app/api/admin/faq/{item_id}", headers=_hdr(REG_MANAGER_ID))
    assert resp2.status_code == 404
    assert resp2.json()["reason"] == "not_found"
