"""Phase 31 (31-09, D-20/D-23): бейджи пометки правилом и автоотказа на трёх поверхностях
менеджера — карточка заявки в боте, карточка Mini App, список заявок — плюс фильтр очереди
«только помеченные» и человеческая подпись решения правила «🤖 Автоправило».

Пока правило не сработало ни разу, все три поверхности выглядят ровно как до этой волны — это
проверяют «baseline»-тесты в каждом блоке.

async через asyncio.run() (pytest-asyncio недоступен в этом окружении), временная БД
config.DB_PATH — та же модель, что у tests/test_reject_rules_finalize.py/
tests/test_applications_parity.py.
"""
from __future__ import annotations

import asyncio
import html as html_module
import json

from config import config
from database import db
import services.applications as applications

UID = 920900100


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="reject_rules_badges.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(db.init_db())


# add_user's fixed INSERT column list doesn't include the Phase 31 columns — узкий UPDATE
# после add_user, тот же приём, что services/reg_finalize.py и tests/test_reject_rules_finalize.py.
_NARROW_UPDATE_ONLY = ("flagged_rule_ids", "auto_reject_rule_ids")


async def _seed_user(uid, status="pending", **overrides):
    row = {
        "telegram_id": uid,
        "full_name": "Иван Иванов",
        "username": "ivan",
        "registration_date": "2026-09-01 10:00:00",
        "event_city": None,
        "participant_type": "full",
        "course": "1",
    }
    narrow = {k: overrides.pop(k) for k in list(overrides) if k in _NARROW_UPDATE_ONLY}
    row.update(overrides)
    await db.add_user(row)
    await db.set_user_status(uid, status)
    if narrow:
        await db.update_user_answers(uid, narrow, allowed_columns=list(narrow.keys()))
    return row


async def _seed_rule(*, name=None, reject_text="Правило сработало.", action="flag", enabled=1):
    return await db.create_reject_rule(
        name=name, city=None, tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1"]}]]),
        action=action, reject_text=reject_text, enabled=enabled, created_by=1,
    )


# ═══════════════════════════════════════════════════════════════════════════════════════
# Задача 1: services.applications — rule_badge_lines/auto_reject_cleared_line/queue_page
# ═══════════════════════════════════════════════════════════════════════════════════════

# ── rule_badge_lines (D-20) ───────────────────────────────────────────────────────────────

def test_rule_badge_lines_flagged_rule_shows_name_no_id(tmp_path):
    async def go():
        rid = await _seed_rule(name="Отбор по курсу")
        await _seed_user(UID, flagged_rule_ids=json.dumps([rid]))
        user = await db.get_user(UID)
        return await applications.rule_badge_lines(user), rid

    _ready(tmp_path)
    lines, rid = _run(go())
    assert lines == ["⚠️ Помечена правилом: Отбор по курсу"]
    assert str(rid) not in lines[0]


def test_rule_badge_lines_deleted_rule_shows_removed_label_not_crash(tmp_path):
    async def go():
        rid = await _seed_rule(name="Временное правило")
        await _seed_user(UID, flagged_rule_ids=json.dumps([rid]))
        await db.delete_reject_rule(rid)
        user = await db.get_user(UID)
        return await applications.rule_badge_lines(user)

    _ready(tmp_path)
    lines = _run(go())
    assert lines == ["⚠️ Помечена правилом: правило удалено"]


def test_rule_badge_lines_auto_reject_without_name_uses_text_words(tmp_path):
    async def go():
        rid = await _seed_rule(
            name=None,
            reject_text="Слишком много слов подряд без имени явно длиннее шести слов подряд",
            action="reject",
        )
        await _seed_user(UID, auto_reject_rule_ids=json.dumps([rid]))
        user = await db.get_user(UID)
        return await applications.rule_badge_lines(user)

    _ready(tmp_path)
    lines = _run(go())
    assert len(lines) == 1
    assert lines[0].startswith("🤖 Автоотказ по правилу: Слишком много слов подряд без имени")
    assert lines[0].endswith("…")


def test_rule_badge_lines_both_flag_and_reject_present_in_order(tmp_path):
    async def go():
        flag_id = await _seed_rule(name="Мягкое правило", action="flag")
        reject_id = await _seed_rule(name="Жёсткое правило", action="reject")
        await _seed_user(
            UID, flagged_rule_ids=json.dumps([flag_id]),
            auto_reject_rule_ids=json.dumps([reject_id]),
        )
        user = await db.get_user(UID)
        return await applications.rule_badge_lines(user)

    _ready(tmp_path)
    lines = _run(go())
    assert lines == [
        "⚠️ Помечена правилом: Мягкое правило",
        "🤖 Автоотказ по правилу: Жёсткое правило",
    ]


def test_rule_badge_lines_empty_without_rules_baseline(tmp_path):
    """Пока правила не использовались — ничего не изменилось (сторож baseline)."""
    async def go():
        await _seed_user(UID)
        user = await db.get_user(UID)
        return await applications.rule_badge_lines(user)

    _ready(tmp_path)
    assert _run(go()) == []


def test_no_rule_badge_text_contains_numeric_rule_id(tmp_path):
    async def go():
        rid = await _seed_rule(name=None, reject_text="Просто текст без имени.")
        await _seed_user(UID, flagged_rule_ids=json.dumps([rid]))
        user = await db.get_user(UID)
        lines = await applications.rule_badge_lines(user)
        return lines, rid

    _ready(tmp_path)
    lines, rid = _run(go())
    for line in lines:
        assert str(rid) not in line


# ── auto_reject_cleared_line (D-23) ───────────────────────────────────────────────────────

def test_auto_reject_cleared_line_with_rule_field(tmp_path):
    async def go():
        await _seed_user(UID)
        await db.record_answer_history(
            UID,
            [{
                "column": "status", "old": "rejected", "new": "pending",
                "auto_reject_cleared": True,
                "rule_field": "course", "rule_field_old": "1", "rule_field_new": "3",
            }],
            "bot",
        )
        user = await db.get_user(UID)
        return await applications.auto_reject_cleared_line(user)

    _ready(tmp_path)
    line = _run(go())
    assert line == f"⚠️ Сменил ответ после автоотказа: {applications.COLUMN_TO_LABEL['course']} 1 → 3"


def test_auto_reject_cleared_line_without_rule_field_still_returns_generic_line(tmp_path):
    async def go():
        await _seed_user(UID)
        await db.record_answer_history(
            UID,
            [{"column": "status", "old": "rejected", "new": "pending", "auto_reject_cleared": True}],
            "bot",
        )
        user = await db.get_user(UID)
        return await applications.auto_reject_cleared_line(user)

    _ready(tmp_path)
    assert _run(go()) == "⚠️ Сменил ответ после автоотказа"


def test_auto_reject_cleared_line_none_when_marker_absent(tmp_path):
    """Обычная повторная подача (ручной отказ) — маркер без ключа auto_reject_cleared."""
    async def go():
        await _seed_user(UID)
        await db.record_answer_history(
            UID, [{"column": "status", "old": "rejected", "new": "pending"}], "bot",
        )
        user = await db.get_user(UID)
        return await applications.auto_reject_cleared_line(user)

    _ready(tmp_path)
    assert _run(go()) is None


def test_auto_reject_cleared_line_none_without_history(tmp_path):
    async def go():
        await _seed_user(UID)
        user = await db.get_user(UID)
        return await applications.auto_reject_cleared_line(user)

    _ready(tmp_path)
    assert _run(go()) is None


# ── card_payload (веб, D-01) ──────────────────────────────────────────────────────────────

def test_card_payload_shows_cleared_badge_and_not_resubmit(tmp_path):
    async def go():
        await _seed_user(UID, status="pending")
        # edited_at тоже проставлен (mark_user_edited) — иначе resubmit_line был бы None и
        # сам по себе, без развилки auto_reject_cleared, что ослабило бы проверку подавления.
        await db.mark_user_edited(UID, "bot")
        await db.record_answer_history(
            UID,
            [{
                "column": "status", "old": "rejected", "new": "pending",
                "auto_reject_cleared": True, "rule_field": "course",
                "rule_field_old": "1", "rule_field_new": "3",
            }],
            "bot",
        )
        user = await db.get_user(UID)
        return await applications.card_payload(user)

    _ready(tmp_path)
    card = _run(go())
    kinds = [b["kind"] for b in card["badges"]]
    assert "auto_reject_cleared" in kinds
    assert "resubmit" not in kinds


def test_card_payload_shows_resubmit_for_ordinary_resubmission(tmp_path):
    async def go():
        await _seed_user(UID, status="pending")
        # resubmit_line требует непустой edited_at (edit_badges_for) — та же правка, что
        # реальный маршрут пишет вместе (mark_user_edited + record_answer_history).
        await db.mark_user_edited(UID, "bot")
        await db.record_answer_history(
            UID, [{"column": "status", "old": "rejected", "new": "pending"}], "bot",
        )
        user = await db.get_user(UID)
        return await applications.card_payload(user)

    _ready(tmp_path)
    card = _run(go())
    kinds = [b["kind"] for b in card["badges"]]
    assert "resubmit" in kinds
    assert "auto_reject_cleared" not in kinds


def test_card_payload_rule_badges_before_edited_badge_in_order(tmp_path):
    async def go():
        rid = await _seed_rule(name="Правило пометки")
        await _seed_user(UID, flagged_rule_ids=json.dumps([rid]))
        await db.mark_user_edited(UID, "bot")
        user = await db.get_user(UID)
        return await applications.card_payload(user)

    _ready(tmp_path)
    card = _run(go())
    kinds = [b["kind"] for b in card["badges"]]
    assert "rule_flag" in kinds and "edited" in kinds
    assert kinds.index("rule_flag") < kinds.index("edited")


def test_card_payload_badge_text_is_plain_not_html_escaped(tmp_path):
    """D-01: карточка веба несёт ЧИСТЫЙ текст — экранирование делает только карточка бота."""
    async def go():
        rid = await _seed_rule(name="Правило <b>")
        await _seed_user(UID, flagged_rule_ids=json.dumps([rid]))
        user = await db.get_user(UID)
        return await applications.card_payload(user)

    _ready(tmp_path)
    card = _run(go())
    rule_badge = next(b for b in card["badges"] if b["kind"] == "rule_flag")
    assert "<b>" in rule_badge["text"]
    assert "&lt;" not in rule_badge["text"]


def test_card_payload_baseline_unaffected_without_rules(tmp_path):
    """Пока правила не использовались — карточка выглядит ровно как до фазы."""
    async def go():
        await _seed_user(UID)
        user = await db.get_user(UID)
        return await applications.card_payload(user)

    _ready(tmp_path)
    card = _run(go())
    kinds = [b["kind"] for b in card["badges"]]
    assert "rule_flag" not in kinds
    assert "auto_reject" not in kinds
    assert "auto_reject_cleared" not in kinds


# ── queue_page(flagged_only=True) (D-20) ──────────────────────────────────────────────────

def test_queue_page_flagged_only_returns_only_flagged_with_matching_count(tmp_path):
    async def go():
        rid = await _seed_rule(name="Правило")
        await _seed_user(
            UID, flagged_rule_ids=json.dumps([rid]), registration_date="2026-09-01 10:00:00",
        )
        await _seed_user(UID + 1, registration_date="2026-09-01 10:00:01")
        return await applications.queue_page(scope=None, flagged_only=True)

    _ready(tmp_path)
    row, total = _run(go())
    assert total == 1
    assert row["telegram_id"] == UID


def test_queue_page_flagged_only_default_false_is_byte_compatible(tmp_path):
    async def go():
        await _seed_user(UID)
        return await applications.queue_page(scope=None)

    _ready(tmp_path)
    row, total = _run(go())
    assert total == 1
    assert row["telegram_id"] == UID


def test_queue_page_flagged_only_signature_present():
    import inspect
    assert "flagged_only" in inspect.signature(applications.queue_page).parameters


# ═══════════════════════════════════════════════════════════════════════════════════════
# Задача 2: карточка бота (handlers/admin_moderation.py) + список заявок (admin_app_list.py)
# ═══════════════════════════════════════════════════════════════════════════════════════

def test_render_application_card_prints_rule_lines_before_edited_line():
    from handlers.admin_moderation import _render_application_card

    out = _render_application_card(
        {"full_name": "Иван"}, 1, 1,
        edited_line="✏️ Изменена 02.09 14:00",
        rule_lines=["⚠️ Помечена правилом: Тест"],
    )
    assert out.index("⚠️ Помечена правилом: Тест") < out.index("✏️ Изменена 02.09 14:00")


def test_render_application_card_cleared_line_replaces_resubmit_line():
    from handlers.admin_moderation import _render_application_card

    out = _render_application_card(
        {"full_name": "Иван"}, 1, 1,
        resubmit_line="🔁 Повторная подача",
        cleared_line="⚠️ Сменил ответ после автоотказа: Курс 1 → 3",
    )
    assert "⚠️ Сменил ответ после автоотказа: Курс 1 → 3" in out
    assert "🔁 Повторная подача" not in out


def test_render_application_card_without_new_params_is_byte_compatible():
    """Baseline: старый вызов без rule_lines/cleared_line печатает resubmit_line как раньше."""
    from handlers.admin_moderation import _render_application_card

    out = _render_application_card({"full_name": "Иван"}, 1, 1, resubmit_line="🔁 Повторная подача")
    assert "🔁 Повторная подача" in out


def test_show_current_card_prints_escaped_rule_badge_end_to_end(tmp_path):
    """Карточка бота печатает бейдж пометки с экранированным именем правила — вызывающий
    (_show_current_card) прогоняет строку через html.escape перед рендером."""
    from handlers import admin_moderation
    from tests.test_city_admin_phase72 import FakeMessage, _new_state

    async def go():
        rid = await _seed_rule(name="Правило <b>")
        await _seed_user(UID, flagged_rule_ids=json.dumps([rid]))

    _ready(tmp_path)
    _run(go())
    state = _new_state(UID)
    target = FakeMessage()
    _run(admin_moderation._show_current_card(target, state))
    assert "⚠️ Помечена правилом: Правило &lt;b&gt;" in target.text
    assert "Правило <b>" not in target.text


def test_show_current_card_prints_cleared_badge_not_resubmit_end_to_end(tmp_path):
    from handlers import admin_moderation
    from tests.test_city_admin_phase72 import FakeMessage, _new_state

    async def go():
        await _seed_user(UID, status="pending")
        await db.mark_user_edited(UID, "bot")
        await db.record_answer_history(
            UID,
            [{
                "column": "status", "old": "rejected", "new": "pending",
                "auto_reject_cleared": True, "rule_field": "course",
                "rule_field_old": "1", "rule_field_new": "3",
            }],
            "bot",
        )

    _ready(tmp_path)
    _run(go())
    state = _new_state(UID)
    target = FakeMessage()
    _run(admin_moderation._show_current_card(target, state))
    assert "Сменил ответ после автоотказа" in target.text
    assert "Повторная подача" not in target.text


def test_decision_suffix_auto_decided_by_shows_auto_rule_label():
    from handlers.admin_app_list import _decision_suffix
    from services.reject_journal import AUTO_DECIDED_BY

    assert _decision_suffix("rejected", AUTO_DECIDED_BY, {}) == " · 🤖 Автоправило"


def test_decision_suffix_pending_status_empty_even_for_auto_sentinel():
    from handlers.admin_app_list import _decision_suffix
    from services.reject_journal import AUTO_DECIDED_BY

    assert _decision_suffix("pending", AUTO_DECIDED_BY, {}) == ""


def test_decision_suffix_none_still_automatically():
    from handlers.admin_app_list import _decision_suffix

    assert _decision_suffix("rejected", None, {}) == " · автоматически"


def test_decision_suffix_real_manager_uses_label_unchanged():
    from handlers.admin_app_list import _decision_suffix

    out = _decision_suffix("rejected", 12345, {12345: "Мария"})
    assert out == " · отклонил(а) Мария"


def test_decision_suffix_manager_label_lookup_fallback_unchanged():
    """Плановый литеральный вызов (12345 vs словарь со строковым ключом) — фолбэк
    «менеджер #<id>» не изменился этой волной."""
    from handlers.admin_app_list import _decision_suffix

    out = _decision_suffix("rejected", 12345, {"12345": "Мария"})
    assert out == " · отклонил(а) менеджер #12345"


def test_admin_app_list_no_new_hardcoded_minus_one_literal():
    """Сентинел импортирован из services.reject_journal, не записан вторым литералом -1."""
    import inspect

    from handlers import admin_app_list

    src = inspect.getsource(admin_app_list._decision_suffix)
    assert "-1" not in src


# ═══════════════════════════════════════════════════════════════════════════════════════
# Задача 3: Mini App — фильтр «flagged», чип, новые виды бейджей на фронте
# ═══════════════════════════════════════════════════════════════════════════════════════

def test_applications_next_flagged_param_filters_queue(tmp_path):
    from tests.test_miniapp_routes import _cfg, _client, _hdr, _seed, _use_tmp_db

    db_path = _use_tmp_db(tmp_path, "reject_rules_badges_miniapp1.db")
    reg_manager_id = 921100
    _seed(staff=[(reg_manager_id, "reg_manager", None)], settings={"miniapp_enabled": "on"})

    async def go():
        rid = await _seed_rule(name="Правило")
        await _seed_user(
            910001, flagged_rule_ids=json.dumps([rid]), registration_date="2026-09-01 10:00:00",
        )
        await _seed_user(910002, registration_date="2026-09-01 10:00:01")

    _run(go())
    client = _client(_cfg(db_path))
    body = client.get(
        "/app/api/applications/next", params={"flagged": "1"}, headers=_hdr(reg_manager_id),
    ).json()
    assert body["application"]["telegram_id"] == 910001
    assert body["remaining"] == 1


def test_applications_next_garbage_flagged_value_is_ignored_not_400(tmp_path):
    from tests.test_miniapp_routes import _cfg, _client, _hdr, _seed, _use_tmp_db

    db_path = _use_tmp_db(tmp_path, "reject_rules_badges_miniapp2.db")
    reg_manager_id = 921101
    _seed(staff=[(reg_manager_id, "reg_manager", None)], settings={"miniapp_enabled": "on"})
    _run(_seed_user(910003, registration_date="2026-09-01 10:00:00"))

    client = _client(_cfg(db_path))
    resp = client.get(
        "/app/api/applications/next", params={"flagged": "garbage"}, headers=_hdr(reg_manager_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["application"]["telegram_id"] == 910003


def test_applications_next_filters_chips_has_flagged_key_with_human_label(tmp_path):
    from tests.test_miniapp_routes import _cfg, _client, _hdr, _seed, _use_tmp_db

    db_path = _use_tmp_db(tmp_path, "reject_rules_badges_miniapp3.db")
    reg_manager_id = 921102
    _seed(staff=[(reg_manager_id, "reg_manager", None)], settings={"miniapp_enabled": "on"})
    _run(_seed_user(910004, registration_date="2026-09-01 10:00:00"))

    client = _client(_cfg(db_path))
    body = client.get("/app/api/applications/next", headers=_hdr(reg_manager_id)).json()
    assert body["filters"]["chips"]["flagged"] == "Помечены правилом"


def test_applications_next_empty_queue_with_flagged_filter_active_uses_filtered_empty_state(tmp_path):
    from tests.test_miniapp_routes import _cfg, _client, _hdr, _seed, _use_tmp_db

    db_path = _use_tmp_db(tmp_path, "reject_rules_badges_miniapp4.db")
    reg_manager_id = 921103
    _seed(staff=[(reg_manager_id, "reg_manager", None)], settings={"miniapp_enabled": "on"})
    _run(_seed_user(910005, registration_date="2026-09-01 10:00:00"))  # не помечена

    client = _client(_cfg(db_path))
    body = client.get(
        "/app/api/applications/next", params={"flagged": "1"}, headers=_hdr(reg_manager_id),
    ).json()
    assert body["empty"] is True
    assert body["empty_text"] == "По этому фильтру заявок нет — снимите фильтр."


def test_settings_schema_flagged_filter_key_matches_neighbour_shape():
    from settings_schema import SETTINGS_SCHEMA

    changed = SETTINGS_SCHEMA["miniapp_applications_filter_changed"]
    flagged = SETTINGS_SCHEMA["miniapp_applications_filter_flagged"]
    assert flagged["type"] == changed["type"] == "text"
    assert flagged["group"] == changed["group"] == "miniapp"
    assert isinstance(flagged["default"], str) and flagged["default"].strip()


def test_applications_js_renders_new_rule_badge_kinds_via_existing_mechanism():
    """Новые виды бейджей отрисовываются существующим механизмом (EDITED_BADGE_KINDS), а не
    молча пропускаются — тот же приём JS-сторожей, что test_miniapp_frontend.py."""
    from tests.test_miniapp_frontend import APPLICATIONS_JS, _js_without_comments

    text = _js_without_comments(APPLICATIONS_JS)
    assert '"rule_flag"' in text
    assert '"auto_reject"' in text
    assert '"auto_reject_cleared"' in text
    assert "flagged" in text


def test_applications_js_flagged_query_param_and_chip_present():
    from tests.test_miniapp_frontend import APPLICATIONS_JS, _js_without_comments

    text = _js_without_comments(APPLICATIONS_JS)
    assert 'query.set("flagged", "1")' in text
    assert "chips.flagged" in text


# ── html.escape guard (T-31-09-01) ────────────────────────────────────────────────────────

def test_escaped_rule_line_contains_lt_entity_for_html_name():
    """Плановая проверка контракта эскейпа — прямой вызов html.escape на строке бота,
    без похода в БД/aiogram."""
    raw = "⚠️ Помечена правилом: Правило <b>"
    assert "&lt;" in html_module.escape(raw)
