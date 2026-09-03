"""Phase 22 Plan 06 (WEB-SET-01..04, D-13): сторожа паритета «реестр ↔ веб-экран».

Пять сторожей проверяют, что `GET /app/api/admin/settings/all` не может тихо разойтись с
ботом: компонент на тип, единственный источник «что опасно», подписи разделов/групп из
`handlers.admin_sections`/`handlers.admin_settings`, полное покрытие поиска и отсутствие
группы `roles` на обеих операциях (чтение/запись).

Харнесс — `tests/test_miniapp_routes.py` (тот же, что у планов 22-04/22-05); форма покрытия —
`tests/test_settings_groups_c0x.py`/`tests/test_settings_synonyms.py`.
"""
from __future__ import annotations

import asyncio

import handlers.admin_sections as admin_sections
import settings_ops
from database import db as bot_db
from handlers.admin_settings import SETTINGS_GROUPS, _settings_group_label
from miniapp.routers import settings as settings_router
from settings_schema import SETTINGS_SCHEMA

from tests.test_miniapp_routes import (
    ADMIN_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)
from tests.test_settings_synonyms import SEARCH_SELF_DESCRIBING

SETTINGS_MANAGER_SPB = 900610  # reg_manager + право settings через реестр, привязан к spb


def _run(coro):
    return asyncio.run(coro)


def _raw(key: str):
    return _run(bot_db.get_setting(key))


def _setup(tmp_path, name="web_settings_parity.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    _seed(staff=[(SETTINGS_MANAGER_SPB, "reg_manager", "spb")])
    _set("role_caps_reg_manager", "moderate_reg\nsettings")
    return _client(_cfg(db_path))


def _all(client, user=ADMIN_ID):
    return client.get("/app/api/admin/settings/all", headers=_hdr(user))


def _batch(client, changes, *, base=None, confirm=None, user=ADMIN_ID):
    body = {
        "changes": [{"key": k, "value": v} for k, v in changes],
        "base": base or {},
        "confirm": confirm or [],
    }
    return client.post("/app/api/admin/settings/batch", json=body, headers=_hdr(user))


def _items(body: dict) -> list[dict]:
    out: list[dict] = []
    for section in body["sections"]:
        out.extend(section["toggles"])
        for group in section["groups"]:
            out.extend(group["items"])
    return out


def _sections_body(client, tmp_path=None) -> dict:
    resp = _all(client)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Сторож 1: компонент на тип ────────────────────────────────────────────────────────────

def test_every_editable_key_renders_exactly_one_component_of_its_registry_type(tmp_path):
    """Каждый ключ settings_ops.editable_keys() встречается в ответе settings/all РОВНО один
    раз, и его "type" совпадает с SETTINGS_SCHEMA[base]["type"] — ни дублей, ни пропусков,
    ни чужого компонента на ключ."""
    client = _setup(tmp_path)
    body = _sections_body(client)
    items = _items(body)

    keys = [i["key"] for i in items]
    expected_keys = set(settings_ops.editable_keys())

    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert not duplicates, f"ключ отрисован больше одного раза: {duplicates}"

    missing = sorted(expected_keys - set(keys))
    assert not missing, f"ключ реестра выпал из веб-экрана: {missing}"

    extra = sorted(set(keys) - expected_keys)
    assert not extra, f"на веб-экране чужой ключ вне editable_keys(): {extra}"

    type_mismatches = []
    for item in items:
        base = item["base_key"]
        expected_type = SETTINGS_SCHEMA[base]["type"]
        if item["type"] != expected_type:
            type_mismatches.append(
                f"{item['key']}: веб рисует '{item['type']}', реестр требует '{expected_type}'"
            )
    assert not type_mismatches, "тип компонента разошёлся с реестром:\n" + "\n".join(type_mismatches)


# ── Сторож 2: опасное — один источник ────────────────────────────────────────────────────

def test_dangerous_keys_come_only_from_settings_ops_dangerous_keys(tmp_path):
    """Множество ключей с dangerous=true в ответе == settings_ops.DANGEROUS_KEYS; все ключи
    SHEET_TAB_WRITE_MODE и обе пары DANGER_CONFIRM миниаппа входят в него; у каждого опасного
    ключа кроме вкладок Sheets confirm_text непуст (вкладки считаются по месту при записи)."""
    client = _setup(tmp_path)
    body = _sections_body(client)
    items = _items(body)

    response_dangerous = {i["base_key"] for i in items if i["dangerous"]}
    assert response_dangerous == set(settings_ops.DANGEROUS_KEYS), (
        "набор опасных ключей веб-экрана разошёлся с settings_ops.DANGEROUS_KEYS:\n"
        f"  только в ответе: {sorted(response_dangerous - settings_ops.DANGEROUS_KEYS)}\n"
        f"  только в реестре: {sorted(settings_ops.DANGEROUS_KEYS - response_dangerous)}"
    )

    tab_keys = set(settings_ops.SHEET_TAB_WRITE_MODE)
    assert tab_keys <= settings_ops.DANGEROUS_KEYS, (
        f"ключи вкладок Sheets не входят в DANGEROUS_KEYS: {sorted(tab_keys - settings_ops.DANGEROUS_KEYS)}"
    )

    danger_confirm_bases = {key for key, _next in settings_router.DANGER_CONFIRM}
    assert danger_confirm_bases <= settings_ops.DANGEROUS_KEYS, (
        "ключи DANGER_CONFIRM миниаппа не входят в DANGEROUS_KEYS: "
        f"{sorted(danger_confirm_bases - settings_ops.DANGEROUS_KEYS)}"
    )

    missing_confirm_text = []
    for item in items:
        if not item["dangerous"]:
            continue
        if item["base_key"] in tab_keys:
            continue  # текст вкладки считается по числу строк на месте (D-06), не в item
        if not item.get("confirm_text"):
            missing_confirm_text.append(item["key"])
    assert not missing_confirm_text, (
        "у опасного ключа (не вкладки) пустой confirm_text: " + ", ".join(sorted(missing_confirm_text))
    )


# ── Сторож 3: подписи разделов и групп ───────────────────────────────────────────────────

def test_section_and_group_labels_match_bot_verbatim(tmp_path):
    """Порядок и подписи разделов в ответе — подпоследовательность
    handlers.admin_sections.SECTIONS (разделы без настроек — «comms» — отсутствуют, порядок
    остальных сохранён 1-в-1); подпись каждой группы, входящей в
    handlers.admin_settings.SETTINGS_GROUPS, совпадает с _settings_group_label(token) символ
    в символ."""
    client = _setup(tmp_path)
    body = _sections_body(client)

    # ── разделы ──
    bot_order = [(token, label) for token, label, _rows in admin_sections.SECTIONS]
    bot_tokens_in_order = [t for t, _ in bot_order]
    bot_labels = dict(bot_order)

    resp_sections = [s for s in body["sections"] if s["token"] != "misc"]

    # Подпоследовательность: каждый следующий токен ответа должен встретиться в bot_tokens_in_order
    # НЕ РАНЬШЕ предыдущего.
    cursor = 0
    for section in resp_sections:
        token = section["token"]
        assert token in bot_tokens_in_order, (
            f"раздел '{token}' веб-экрана не существует в handlers.admin_sections.SECTIONS"
        )
        pos = bot_tokens_in_order.index(token, cursor)
        assert pos >= cursor, (
            f"порядок разделов веба разошёлся с ботом на '{token}': ожидался порядок бота "
            f"{bot_tokens_in_order}, веб дал {[s['token'] for s in resp_sections]}"
        )
        cursor = pos + 1

        assert section["label"] == bot_labels[token], (
            f"подпись раздела '{token}' веба ('{section['label']}') != подписи бота "
            f"('{bot_labels[token]}')"
        )

    # Раздел без единой настройки («comms» — рассылки/опросы) на экране настроек не рисуется.
    assert "comms" not in {s["token"] for s in resp_sections}, (
        "раздел без настроек ('comms') не должен рисоваться на экране настроек"
    )

    # ── группы ──
    bot_group_tokens = {tok for _label, tok, _keys in SETTINGS_GROUPS}
    mismatches = []
    seen_tokens = set()
    for section in body["sections"]:
        for group in section["groups"]:
            token = group["token"]
            if token not in bot_group_tokens:
                continue  # группы вне SETTINGS_GROUPS бота (reg_questions/menu/dashboard/miniapp) — не сторожим здесь
            seen_tokens.add(token)
            expected = _settings_group_label(token)
            if group["label"] != expected:
                mismatches.append(f"{token}: веб '{group['label']}' != бот '{expected}'")

    assert not mismatches, "подпись группы разошлась с ботом:\n" + "\n".join(mismatches)
    # Каждая непустая группа бота должна была встретиться хотя бы раз (иначе сторож молчал бы
    # впустую, если веб вообще перестанет рисовать группы бота).
    missing_groups = sorted(bot_group_tokens - seen_tokens)
    assert not missing_groups, f"группа бота не встретилась на веб-экране вовсе: {missing_groups}"


# ── Сторож 4: поиск покрыт ────────────────────────────────────────────────────────────────

def test_every_key_has_search_terms_or_is_self_describing(tmp_path):
    """Каждый ключ ответа либо имеет ≥2 значения в search_terms, либо перечислен в
    SEARCH_SELF_DESCRIBING (из tests.test_settings_synonyms, не переобъявляется) — сумма
    множеств равна editable_keys()."""
    client = _setup(tmp_path)
    body = _sections_body(client)
    items = _items(body)

    without_terms = []
    for item in items:
        terms = item.get("search_terms") or []
        base = item["base_key"]
        if len(terms) >= 2:
            continue
        if base in SEARCH_SELF_DESCRIBING:
            continue
        without_terms.append(item["key"])

    assert not without_terms, (
        "ключ без синонимов поиска и не самоописательный (нет в SEARCH_SELF_DESCRIBING): "
        + ", ".join(sorted(without_terms))
    )

    response_bases = {item["base_key"] for item in items}
    with_terms = {item["base_key"] for item in items if len(item.get("search_terms") or []) >= 2}
    covered = with_terms | (SEARCH_SELF_DESCRIBING & response_bases)
    assert covered == response_bases, (
        "сумма ключей с синонимами и самоописательных не равна составу веб-экрана:\n"
        f"  без покрытия: {sorted(response_bases - covered)}\n"
        f"  покрыто, но не на экране: {sorted(covered - response_bases)}"
    )


# ── Сторож 5: права ────────────────────────────────────────────────────────────────────────

def test_settings_require_cap_and_roles_group_never_readable_or_writable(tmp_path):
    """Ответ settings/all и settings/batch без права settings — 403; ни один ключ группы
    roles не приходит в settings/all и не принимается на запись через settings/batch —
    попытка записать role_caps_reg_manager отклоняется, значение в БД не меняется."""
    client = _setup(tmp_path)

    resp = _all(client, GAME_MANAGER_ID)
    assert resp.status_code == 403, "settings/all без права settings обязан отдавать 403"

    resp = _batch(client, [("event_name", "x")], user=GAME_MANAGER_ID)
    assert resp.status_code == 403, "settings/batch без права settings обязан отдавать 403"

    body = _sections_body(client)
    role_keys_in_response = [
        i["key"] for i in _items(body)
        if SETTINGS_SCHEMA.get(i["base_key"], {}).get("group") == "roles"
    ]
    assert not role_keys_in_response, (
        f"ключ группы roles приехал в settings/all: {role_keys_in_response}"
    )

    before = _raw("role_caps_reg_manager")
    resp = _batch(client, [("role_caps_reg_manager", "settings")])
    assert resp.status_code == 403, (
        f"запись role_caps_reg_manager через batch должна отклоняться 403, получили {resp.status_code}"
    )
    assert resp.json().get("reason") == "not_editable"
    after = _raw("role_caps_reg_manager")
    assert after == before, "значение role_caps_reg_manager изменилось после отклонённой попытки записи"
