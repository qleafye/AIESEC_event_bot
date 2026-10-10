"""Квалифицированная амбассадорка СкиллАп 5: экраны амбассадора.

- прогресс цифрами на «Моя ссылка» (бот) и в хабе Mini App;
- скрытие имён приглашённых: «Мои приглашённые» цифрами, «Приглашённый №N» в истории баллов
  (бот и Mini App), строки `coins` в БД не меняются;
- при выключенных тумблерах экраны прежние.

pytest-asyncio в окружении нет — async через `asyncio.run()`; хендлеры бота зовутся напрямую
с Fake-сообщением (приём `tests/test_bot_past_season_gate_260923.py`).
"""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db
from handlers.i18n import reg_i18n
from handlers import user_actions as ua_mod
from domain.settings.schema import get_setting_typed
from tests._dbtpl import fast_init_db
from tests.test_amb_tiers_core_su5 import seed_journal_row

SEASON = "SU26"
AMB = 700
NAMES = {801: "Иван Уникальный", 802: "Пётр Особый", 803: "Мария Третья"}


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_tiers_screens_su5.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    # Экраны проверяются на значениях СкиллАп (общие дефолты реестра нейтральные).
    import domain.regform.presets as reg_presets
    for key in ("amb_next_step_o2o_text", "amb_next_step_networking_text"):
        _run(db.set_setting(key, reg_presets.SKILLUP_TIER_SETTINGS[key]))


def _seed_user(tid, *, referrer_id=None, status="pending", full_name=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "referrer_id": referrer_id,
        "season": SEASON,
    }))
    _run(db.set_user_status(tid, status))
    if referrer_id and status == "approved":
        seed_journal_row(tid, referrer_id, season=SEASON)


def _make_ambassador(tid=AMB):
    _seed_user(tid, status="approved", full_name="Амбассадор Тестовый")
    _run(db.set_ambassador_flag(tid, active=True, at="2026-09-01 00:00:00"))


def _seed_invitees(n=3, amb=AMB):
    for tid in list(NAMES)[:n]:
        _seed_user(tid, referrer_id=amb, full_name=NAMES[tid])


def _approve(tid):
    _run(db.set_user_status(tid, "approved"))
    rows = _sql("SELECT referrer_id FROM users WHERE telegram_id = ?", (tid,))
    if rows and rows[0][0]:
        seed_journal_row(tid, rows[0][0], season=SEASON)


def _credit(invitee, amb=AMB):
    _sql("DELETE FROM referral_credits WHERE invitee_id = ?", (invitee,))  # строку заведёт начисление
    _run(db.claim_referral_credit_atomic(
        invitee, amb, 5, None, reason=f"Приглашённый: {NAMES[invitee]}", changed_by=None,
    ))


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None
        self.username = None


class FakeMessage:
    def __init__(self, user_id=AMB):
        self.text = None
        self.from_user = FakeUser(user_id)
        self.answers_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)


class FakeBotUser:
    username = "AIESEC_test_bot"


class FakeBot:
    async def get_me(self):
        return FakeBotUser()


def _screen(uid=AMB):
    text, _kb = _run(ua_mod._referral_screen(uid, FakeBot()))
    return text


def _old_screen_text(uid, *, ambassador=True):
    """Текст «Моя ссылка» ровно как до изменений: ссылка + (амбассадору) подсказка пути."""
    async def build():
        link = ua_mod.build_referral_link(FakeBotUser.username, uid)
        text = reg_i18n.tr_fmt(await get_setting_typed("referral_link_prompt_text"), "ru", {}, link=link)
        if ambassador:
            text += "\n\n" + reg_i18n.tr_text(await get_setting_typed("ambassador_path_prompt_text"), "ru", {})
        return text
    return _run(build())


def _en_map():
    """Карта перевода `src_hash -> en` из ручного словаря — как после сида на старте бота."""
    from services.i18n import src_hash
    from services.i18n_form_manual import FORM_DEFAULT_EN
    return {src_hash(ru): en for ru, en in FORM_DEFAULT_EN.items()}


def _patch_gates(monkeypatch):
    async def _ok(_message):
        return True
    monkeypatch.setattr(ua_mod, "ensure_registered", _ok)
    monkeypatch.setattr(ua_mod, "ensure_current_season", _ok)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Бот: «Моя ссылка» — прогресс
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_progress_three_pending(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_qualified_program", "on"))
    _make_ambassador()
    _seed_invitees(3)
    assert "По твоей ссылке: 3. Прошли отбор: 0. До разбора резюме: 3" in _screen()


def test_progress_after_one_approval_acceptance_2(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_qualified_program", "on"))
    _make_ambassador()
    _seed_invitees(3)
    _approve(801)
    text = _screen()
    assert "По твоей ссылке: 3. Прошли отбор: 1. До разбора резюме: 2" in text
    # прогресс стоит перед подсказкой пути, оба на месте
    assert text.startswith(_old_screen_text(AMB, ambassador=False))


def test_progress_networking_and_done(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_qualified_program", "on"))
    _make_ambassador()
    for tid in range(901, 908):
        _seed_user(tid, referrer_id=AMB)
    for tid in range(901, 904):
        _approve(tid)
    assert "Прошли отбор: 3. До нетворкинга: 4" in _screen()
    for tid in range(904, 908):
        _approve(tid)
    assert "Прошли отбор: 7. Все ступени твои" in _screen()


def test_program_off_screen_byte_identical_acceptance_11(tmp_path):
    _ready(tmp_path)
    _make_ambassador()
    _seed_invitees(3)
    _approve(801)
    assert _screen() == _old_screen_text(AMB)


def test_non_ambassador_sees_no_progress(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_qualified_program", "on"))
    _seed_user(710, status="approved")
    _seed_user(811, referrer_id=710)
    text = _screen(710)
    assert "По твоей ссылке:" not in text
    assert text == _old_screen_text(710, ambassador=False)


def test_progress_english(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_qualified_program", "on"))
    _make_ambassador()
    _seed_invitees(3)
    _approve(801)
    text, _kb = _run(ua_mod._referral_screen(AMB, FakeBot(), "en", _en_map()))
    assert "Joined via your link: 3. Passed selection: 1. Until the resume review: 2" in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Бот: «Мои приглашённые» и история баллов — имена скрыты
# ══════════════════════════════════════════════════════════════════════════════════════════

def _my_referrals(monkeypatch):
    _patch_gates(monkeypatch)
    message = FakeMessage()
    _run(ua_mod.my_referrals(message, FakeBot()))
    assert len(message.answers_sent) == 1
    return message.answers_sent[0]


def test_my_referrals_hidden_shows_only_numbers_acceptance_9(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("amb_hide_invitee_names", "on"))
    _make_ambassador()
    _seed_invitees(3)
    _approve(801)
    _run(db.set_user_status(803, "rejected"))
    text = _my_referrals(monkeypatch)
    for name in NAMES.values():
        assert name not in text
    assert text == "Всего по твоей ссылке: 3\nНа рассмотрении: 1\nПрошли отбор: 1"


def test_my_referrals_off_lists_names(tmp_path, monkeypatch):
    _ready(tmp_path)
    _make_ambassador()
    _seed_invitees(2)
    text = _my_referrals(monkeypatch)
    assert "Иван Уникальный" in text and "Пётр Особый" in text
    assert "Всего по твоей ссылке" not in text


def _history_rows_seed():
    _make_ambassador()
    _seed_invitees(2)
    _approve(801)
    _approve(802)
    _credit(801)
    _run(db.add_coins(AMB, 10, "Бонус за активность", None))
    _credit(802)


def test_balance_history_masks_names_acceptance_9(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_hide_invitee_names", "on"))
    _history_rows_seed()
    text, _kb = _run(ua_mod._balance_history_screen(AMB))
    summary, _kb = _run(ua_mod._balance_screen(AMB))
    for screen in (text, summary):
        for name in NAMES.values():
            assert name not in screen
        assert "Приглашённый №1" in screen and "Приглашённый №2" in screen
        assert "Бонус за активность" in screen
    # в БД строки не переписаны
    reasons = [r[0] for r in _sql("SELECT reason FROM coins WHERE source = 'referral' ORDER BY id")]
    assert reasons == ["Приглашённый: Иван Уникальный", "Приглашённый: Пётр Особый"]


def test_masked_ordinal_stable_by_coin_id(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_hide_invitee_names", "on"))
    _history_rows_seed()
    text, _kb = _run(ua_mod._balance_history_screen(AMB))
    # история идёт от новых к старым: второй приглашённый выше первого
    assert text.index("Приглашённый №2") < text.index("Приглашённый №1")


def test_balance_screens_off_keep_names(tmp_path):
    _ready(tmp_path)
    _history_rows_seed()
    text, _kb = _run(ua_mod._balance_history_screen(AMB))
    assert "Приглашённый: Иван Уникальный" in text
    assert "Приглашённый №" not in text


def test_mask_off_returns_same_object(tmp_path):
    from services import amb_progress

    _ready(tmp_path)
    rows = [{"id": 1, "source": "referral", "reason": "Приглашённый: Иван"}]

    async def tr_key(key):
        return await get_setting_typed(key)

    assert _run(amb_progress.mask_referral_coin_rows(AMB, rows, tr_key)) is rows


# ══════════════════════════════════════════════════════════════════════════════════════════
# Mini App: хаб, история баллов, «Хочу свою ссылку», текст оффера
# ══════════════════════════════════════════════════════════════════════════════════════════

import pytest  # noqa: E402

from tests.test_miniapp_routes import (  # noqa: E402
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "amb_tiers_screens_miniapp.db")
    _standard_seed()
    import domain.regform.presets as reg_presets
    for key in ("amb_next_step_o2o_text", "amb_next_step_networking_text"):
        _set(key, reg_presets.SKILLUP_TIER_SETTINGS[key])
    return _client(_cfg(db_path))


def _seed_http_invitees(n=3, *, approve=0):
    """Приглашённые делегата DELEGATE_ID (сезон события не задан — считаются пустым сезоном)."""
    async def seed():
        for i in range(n):
            tid = DELEGATE_ID + 600 + i
            await db.add_user({
                "telegram_id": tid, "full_name": f"Скрытый Приглашённый {i}",
                "registration_date": "2026-09-20", "referrer_id": DELEGATE_ID,
            })
            await db.set_user_status(tid, "approved" if i < approve else "pending")
            if i < approve:
                seed_journal_row(tid, DELEGATE_ID, season="")
    _run(seed())


def _hub_invites(client):
    resp = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    return resp.json()["referral"]["invites_text"]


def test_hub_progress_for_ambassador_acceptance_2(client):
    _set("amb_qualified_program", "on")
    _run(db.set_ambassador_flag(DELEGATE_ID, active=True, at="2026-09-01 00:00:00"))
    _seed_http_invitees(3, approve=1)
    assert _hub_invites(client) == "По твоей ссылке: 3. Прошли отбор: 1. До разбора резюме: 2"


def test_hub_progress_ignores_menu_invites_toggle(client):
    _set("amb_qualified_program", "on")
    _set("menu_invites", "off")
    _run(db.set_ambassador_flag(DELEGATE_ID, active=True, at="2026-09-01 00:00:00"))
    _seed_http_invitees(1)
    assert _hub_invites(client).startswith("По твоей ссылке: 1.")


def test_hub_program_off_keeps_old_counter_acceptance_11(client):
    from domain.settings.schema import SETTINGS_SCHEMA

    _run(db.set_ambassador_flag(DELEGATE_ID, active=True, at="2026-09-01 00:00:00"))
    _seed_http_invitees(2)
    tpl = SETTINGS_SCHEMA["miniapp_hub_referral_invites_text"]["default"]
    assert _hub_invites(client) == tpl.format(count=2)
    _set("menu_invites", "off")
    assert _hub_invites(client) is None


def test_hub_non_ambassador_keeps_old_counter(client):
    from domain.settings.schema import SETTINGS_SCHEMA

    _set("amb_qualified_program", "on")
    _seed_http_invitees(2)
    tpl = SETTINGS_SCHEMA["miniapp_hub_referral_invites_text"]["default"]
    assert _hub_invites(client) == tpl.format(count=2)


def test_hub_progress_db_error_falls_back_to_old_counter(client, monkeypatch):
    """Сбой подсчёта прогресса (нет таблицы ступеней, БД занята) не роняет хаб — прежний
    счётчик приглашённых."""
    import sqlite3

    from services import amb_progress
    from domain.settings.schema import SETTINGS_SCHEMA

    _set("amb_qualified_program", "on")
    _run(db.set_ambassador_flag(DELEGATE_ID, active=True, at="2026-09-01 00:00:00"))
    _seed_http_invitees(2)

    async def boom(*_a, **_kw):
        raise sqlite3.OperationalError("no such table: ambassador_exclusions")

    monkeypatch.setattr(amb_progress, "render_progress", boom)
    tpl = SETTINGS_SCHEMA["miniapp_hub_referral_invites_text"]["default"]
    assert _hub_invites(client) == tpl.format(count=2)


def _seed_referral_coins():
    async def seed():
        for i, name in enumerate(("Иван Уникальный", "Пётр Особый")):
            tid = DELEGATE_ID + 700 + i
            await db.add_user({
                "telegram_id": tid, "full_name": name,
                "registration_date": "2026-09-20", "referrer_id": DELEGATE_ID,
            })
            await db.claim_referral_credit_atomic(
                tid, DELEGATE_ID, 5, None, reason=f"Приглашённый: {name}", changed_by=None,
            )
        await db.add_coins(DELEGATE_ID, 10, "Бонус за активность", None)
    _run(seed())


def test_coins_history_masks_names_acceptance_9(client):
    _set("amb_hide_invitee_names", "on")
    _seed_referral_coins()
    resp = client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Иван Уникальный" not in body and "Пётр Особый" not in body
    reasons = [item["reason"] for item in resp.json()["items"]]
    assert reasons == ["Бонус за активность", "Приглашённый №2", "Приглашённый №1"]
    stored = [r[0] for r in _sql("SELECT reason FROM coins WHERE source = 'referral' ORDER BY id")]
    assert stored == ["Приглашённый: Иван Уникальный", "Приглашённый: Пётр Особый"]


def test_coins_history_off_returns_stored_reason(client):
    _seed_referral_coins()
    reasons = [i["reason"] for i in client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID)).json()["items"]]
    assert reasons == ["Бонус за активность", "Приглашённый: Пётр Особый", "Приглашённый: Иван Уникальный"]


def test_miniapp_ambassador_endpoint_sets_since_once(client):
    resp = client.post("/app/api/reg/ambassador", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    user = _run(db.get_user(DELEGATE_ID))
    assert user["is_ambassador"] == 1
    assert user["ambassador_since"]
    first = user["ambassador_since"]
    _sql("UPDATE users SET ambassador_since = '2026-01-01 00:00:00' WHERE telegram_id = ?", (DELEGATE_ID,))
    assert client.post("/app/api/reg/ambassador", headers=_hdr(DELEGATE_ID)).status_code == 200
    assert _run(db.get_user(DELEGATE_ID))["ambassador_since"] == "2026-01-01 00:00:00"
    assert first != "2026-01-01 00:00:00"


def test_offer_body_default_unchanged_and_no_preset_writes_it():
    """Общий дефолт оффера прежний (иначе обещание разбора резюме утекло бы на YL/RT с
    включённой реф-ссылкой). Владелец 09.10: пресет «СкиллАп» тоже его не пишет — текст
    предложения менеджер задаёт сам."""
    import domain.regform.presets as reg_presets
    from services.i18n_miniapp_manual import MANUAL_EN
    from domain.settings.schema import SETTINGS_SCHEMA

    entry = SETTINGS_SCHEMA["miniapp_form_ambassador_offer_body_text"]
    old = "Каждый, кто зарегистрируется по твоей ссылке, будет засчитан тебе как приглашённый."
    assert entry["default"] == old
    assert entry["per_city"] is True
    for preset in reg_presets.REG_PRESETS.values():
        assert "miniapp_form_ambassador_offer_body_text" not in preset.get("settings", {})
    assert MANUAL_EN.get(old)
