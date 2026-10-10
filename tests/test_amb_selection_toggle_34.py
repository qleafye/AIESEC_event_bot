"""Тумблер «🤝 Отбор амбассадоров» (`amb_team_selection_enabled`, по умолчанию выключен).

Требование владельца 30.09: всё, что сделано под РилТолк (отбор, лимит мест, кандидаты,
раздел «🤝 Амбассадоры»), на других событиях выключено, и Юлид после выката ведёт себя ровно
как до модуля (коммит ab2e7f1): вступление сразу по кнопке, прежние тексты, без «заявка
рассматривается», «места заняты» и подтверждения кандидату.

Все проверки «выключено» идут при худших для Юлида значениях настроек модуля: режим отбора,
лимит 1 и место уже занято, у делегата статус «кандидат» (как после переноса ответа анкеты).

pytest-asyncio нет — async через `asyncio.run()`, харнессы — соседних тестов фазы.
"""
from __future__ import annotations

import asyncio

import pytest

from config import config
from database import amb_status_db as sdb
from database import db
from services import amb_status, amb_tiers
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

SEASON = "YL 26/2"
AT = "2026-09-30 12:00:00"
BOT = "TestBot"
KEY = "amb_team_selection_enabled"


def _run(coro):
    return asyncio.run(coro)


def _default(key):
    return SETTINGS_SCHEMA[key]["default"]


def _worst_case_module_settings():
    """Настройки модуля, которые на выключенном тумблере не должны читаться вовсе."""
    _run(db.set_setting("amb_join_mode", "selection"))
    _run(db.set_setting("amb_slots_limit", "1"))


def _seed(tid, *, status="approved", amb=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "season": SEASON,
    }))
    if status:
        _run(db.set_user_status(tid, status))
    if amb:
        _run(sdb.set_status(tid, amb, at=AT))


def _fill_slot(tid=5000):
    _seed(tid)
    _run(sdb.set_status(tid, "active", at=AT))
    assert _run(sdb.try_claim_slot(tid, limit=0, season=SEASON, at=AT))


def _st(tid):
    return _run(sdb.get_status(tid))


@pytest.fixture
def off(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "test_amb_selection_toggle_34.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    # Как на проде Юлида: вопрос анкеты и предложение ссылки включены.
    _run(db.set_setting("reg_q_ambassador", "on"))
    _run(db.set_setting("reg_offer_ref_link", "on"))
    _worst_case_module_settings()
    _fill_slot()
    monkeypatch.setattr(config, "ADMIN_IDS", [])
    calls: list[int] = []

    async def _fake_tiers(tid):
        calls.append(int(tid))

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _fake_tiers)
    return calls


# ── реестр ───────────────────────────────────────────────────────────────────────────────────

def test_registry_key_is_human_toggle_off_by_default():
    meta = SETTINGS_SCHEMA[KEY]
    assert meta["type"] == "enum" and meta["options"] == ["on", "off"]
    assert meta["default"] == "off"
    assert meta["group"] == SETTINGS_SCHEMA["amb_join_mode"]["group"]
    assert meta["label"] == "🤝 Отбор амбассадоров (раздел, лимит мест, кандидаты)"
    assert "Выключено" in meta["prompt"] and "Включено" in meta["prompt"]


def test_off_reads_as_instant_without_limit(off):
    assert _run(amb_status.selection_enabled()) is False
    assert _run(amb_status.join_mode()) == amb_status.MODE_INSTANT
    assert _run(amb_status.slots_limit()) == 0
    assert _run(amb_status.slots_full()) is False
    _run(db.set_setting(KEY, "on"))
    assert _run(amb_status.join_mode()) == amb_status.MODE_SELECTION
    assert _run(amb_status.slots_full()) is True


def test_skillup_tiers_toggle_is_independent(off):
    _run(db.set_setting("amb_qualified_program", "on"))
    assert _run(amb_status.selection_enabled()) is False
    assert _run(amb_tiers.program_on()) is True
    _run(db.set_setting("amb_qualified_program", "off"))
    _run(db.set_setting(KEY, "on"))
    assert _run(amb_status.selection_enabled()) is True
    assert _run(amb_tiers.program_on()) is False


# ── «Моя ссылка» в боте ──────────────────────────────────────────────────────────────────────

class _Me:
    username = BOT


class _Bot:
    async def get_me(self):
        return _Me()


class _Msg:
    def __init__(self):
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))


class _Cb:
    def __init__(self, uid, data):
        self.data = data
        self.from_user = type("U", (), {"id": uid, "language_code": "ru"})()
        self.message = _Msg()
        self.alerts: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append((text, show_alert))


def _datas(kb):
    return [b.callback_data for row in (kb.inline_keyboard if kb else []) for b in row]


def _screen(uid):
    from handlers import user_actions as ua
    return _run(ua._referral_screen(uid, _Bot()))


def _base_link_text(uid):
    """Текст «Моя ссылка» до модуля: шаблон реестра с подставленной ссылкой, и всё."""
    from handlers.i18n import reg_i18n
    link = f"https://t.me/{BOT}?start=amb_{uid}"
    return reg_i18n.tr_fmt(_default("referral_link_prompt_text"), "ru", {}, link=link)


@pytest.mark.parametrize("amb", [None, "candidate", "declined", "left"])
def test_my_link_non_ambassador_sees_join_button_as_before(off, amb):
    _seed(10, status="pending", amb=amb)
    text, kb = _screen(10)
    assert text == _base_link_text(10)
    assert _datas(kb) == ["ambjoin"]


def test_my_link_active_sees_path_and_leave_as_before(off):
    _seed(11, amb="active")
    text, kb = _screen(11)
    assert text == _base_link_text(11) + "\n\n" + _default("ambassador_path_prompt_text")
    datas = _datas(kb)
    assert datas == ["ambpath:invite", "ambpath:content", "ambpath:none", "ambleave"]


def test_my_link_candidate_joins_instantly_despite_full_limit(off):
    from handlers import user_actions as ua
    _seed(12, status="pending", amb="candidate")
    cb = _Cb(12, "ambjoin")
    _run(ua.ambassador_join(cb, _Bot()))
    st = _st(12)
    assert st["status"] == "active" and st["slot_at"] is None
    assert _run(db.get_user(12))["is_ambassador"]
    assert off == [12]  # ступени СкиллАп проверяются как всегда (свой тумблер)
    text, kw = cb.message.edits[0]
    assert "ambleave" in _datas(kw["reply_markup"])
    assert cb.alerts == [(None, False)]


def test_my_link_declined_joins_too(off):
    from handlers import user_actions as ua
    _seed(13, status="pending", amb="declined")
    cb = _Cb(13, "ambjoin")
    _run(ua.ambassador_join(cb, _Bot()))
    assert _st(13)["status"] == "active"
    assert cb.alerts == [(None, False)]


# ── анкета: вопрос, ответ «да», предложение после анкеты ────────────────────────────────────

def _fakes():
    from tests.test_skillup_referral_28 import _FakeCallback, _FakeMessage
    return _FakeCallback, _FakeMessage


def _finalize(tid, *, yes):
    from services import reg_finalize as rf
    draft = {"telegram_id": tid, "kind": "new",
             "answers": {"full_name": f"Делегат {tid}", "is_ambassador_candidate": yes}}
    return _run(rf.finalize_data(tid, "@d", draft))


def _offer(tid):
    from handlers.reg import reg_ambassador
    _cb, msg_cls = _fakes()
    msg = msg_cls(tid)
    _run(reg_ambassador.offer_ref_link(msg, tid))
    return msg


def test_question_shown_despite_full_limit(off):
    import domain.regform.engine as reg_engine
    assert "ambassador" in _run(reg_engine.enabled_steps({}, None))


def test_form_yes_writes_only_the_answer(off):
    _finalize(20, yes=True)
    assert _st(20)["status"] == "none"
    user = _run(db.get_user(20))
    assert user["is_ambassador_candidate"] and not user["is_ambassador"]


@pytest.mark.parametrize("amb", [None, "candidate", "declined"])
def test_offer_after_form_is_plain_offer_buttons(off, amb):
    _finalize(21, yes=True)
    if amb:
        _run(sdb.set_status(21, amb, at=AT))
    msg = _offer(21)
    assert len(msg.sent) == 1
    text, kb, _pm = msg.sent[0]
    assert _default("amb_candidate_ack_text") not in (text or "")
    assert _datas(kb) == ["regamb:want", "regamb:later"]


def test_offer_toggle_off_sends_nothing_even_to_candidate(off):
    _run(db.set_setting("reg_offer_ref_link", "off"))
    _finalize(22, yes=True)
    _run(sdb.set_status(22, "candidate", at=AT))
    assert not _offer(22).sent


def test_want_button_joins_and_sends_link_without_ack(off):
    from handlers.reg import reg_ambassador
    cb_cls, _msg = _fakes()
    _finalize(23, yes=True)
    _run(sdb.set_status(23, "candidate", at=AT))
    cb = cb_cls("regamb:want", 23)
    _run(reg_ambassador.regamb_want(cb))
    assert _st(23)["status"] == "active"
    texts = [t for (t, _m, _p) in cb.message.sent]
    assert texts[0] == f"https://t.me/{BOT}?start=amb_23"
    assert len(texts) == 2  # ссылка + пояснение, где её найти
    assert _default("amb_candidate_ack_text") not in texts
    assert cb.answers == [(None, False)]


# ── швы одобрения ────────────────────────────────────────────────────────────────────────────

def test_approval_hooks_are_noops(off):
    _run(db.set_setting("amb_slots_limit", "5"))
    _seed(30, status="approved", amb="active")
    _run(amb_status.on_applications_approved([30]))
    assert _st(30)["slot_at"] is None
    _run(db.set_user_status(5000, "pending"))  # у держателя места заявка больше не одобрена
    _run(amb_status.on_applications_unapproved([5000]))
    assert _st(5000)["slot_at"]
    assert _run(amb_status.reconcile_slots()) == 0
    assert _st(5000)["slot_at"]


# ── Mini App: финал анкеты, «Хочу свою ссылку», хаб ──────────────────────────────────────────

from tests.test_miniapp_form import _seed_draft, bot_api  # noqa: E402,F401 — фикстура bot_api
from tests.test_miniapp_routes import (  # noqa: E402
    DELEGATE_ID, UNREGISTERED_ID, _cfg, _client, _hdr, _set, _standard_seed,
)
from tests.test_miniapp_routes import _use_tmp_db as _use_tmp_http_db  # noqa: E402

APP_BOT = "YouLead_test_bot"


@pytest.fixture
def http(tmp_path, monkeypatch):
    path = _use_tmp_http_db(tmp_path, "test_amb_selection_toggle_34_http.db")
    _standard_seed()
    _set("reg_offer_ref_link", "on")
    _worst_case_module_settings()
    _fill_slot()
    calls: list[int] = []

    async def _fake_tiers(tid):
        calls.append(int(tid))

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _fake_tiers)
    client = _client(_cfg(path))
    client.tier_calls = calls
    return client


@pytest.mark.usefixtures("bot_api")
def test_miniapp_finale_yes_gets_plain_offer(http):
    _seed_draft(UNREGISTERED_ID, kind="new",
                patch={"age": 22, "full_name": "Иван Иванов", "is_ambassador_candidate": True})
    resp = http.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ambassador"]["cta"] == _default("miniapp_form_ambassador_cta_text")
    assert "ambassador_note" not in body and "ambassador_link" not in body
    assert _st(UNREGISTERED_ID)["status"] == "none"


def test_miniapp_want_joins_candidate_instantly(http):
    _run(sdb.set_status(DELEGATE_ID, "candidate", at=AT))
    resp = http.post("/app/api/reg/ambassador", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "active"
    assert body["link"] == f"https://t.me/{APP_BOT}?start=amb_{DELEGATE_ID}"
    assert body["note"] and "status_note" not in body
    assert _run(db.get_user(DELEGATE_ID))["is_ambassador"]
    assert http.tier_calls == [DELEGATE_ID]


def test_miniapp_hub_referral_same_for_candidate_and_none(http):
    before = http.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()["referral"]
    _run(sdb.set_status(DELEGATE_ID, "candidate", at=AT))
    after = http.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()["referral"]
    assert before is not None and after == before
    blob = str(after)
    for key in ("amb_status_candidate_text", "amb_slots_full_text", "amb_candidate_ack_text"):
        assert _default(key) not in blob


# ── админка: корень /admin, устаревшие кнопки, тумблер, «🔄 Новый сезон» ─────────────────────

def _admin_ready(tmp_path):
    from tests.test_amb_candidates_34 import _ready
    _ready(tmp_path)  # включает модуль для тестов раздела — здесь выключаем обратно
    _run(db.delete_setting(KEY))


def test_admin_root_hides_amb_section_until_toggle_on(tmp_path):
    from handlers.settings import admin_core
    from handlers.settings.admin_sections import SECTIONS
    from tests.test_amb_candidates_34 import ADMIN_ID
    _admin_ready(tmp_path)
    flat = _datas(_run(admin_core.build_admin_keyboard(ADMIN_ID)))
    assert "admin_sec:amb" not in flat
    assert flat == [f"admin_sec:{t}" for t, _l, _r in SECTIONS if t != "amb"]
    _run(db.set_setting(KEY, "on"))
    flat = _datas(_run(admin_core.build_admin_keyboard(ADMIN_ID)))
    assert flat == [f"admin_sec:{t}" for t, _l, _r in SECTIONS]


def test_stale_section_buttons_explain_how_to_turn_on(tmp_path):
    from handlers.amb import admin_amb_section as s
    from handlers.settings import admin_sections
    from tests.test_amb_candidates_34 import _cb
    _admin_ready(tmp_path)
    cb = _cb("admin_sec:amb")
    _run(admin_sections.show_admin_section(cb))
    assert cb.answers == [(s.SECTION_OFF_ALERT, True)]
    assert len(s.SECTION_OFF_ALERT) <= 200
    assert "🎮 Геймификация" in s.SECTION_OFF_ALERT and "🤝 Отбор амбассадоров" in s.SECTION_OFF_ALERT

    for data in ("admin_amb_entry", "admin_amb_candidates", "ambs_mode", "ambc:candidates:0",
                 "ambc_take:5", "ambc_decl", "ambc_arch_csv", "ambp:5"):
        assert _run(s._section_off(_cb(data))), data
    # Кнопки делегата и ступеней СкиллАп — не раздел, их гейт не трогает.
    for data in ("ambjoin", "ambleave", "ambpath:invite", "ambt_toggle:program", "ambwave"):
        assert not _run(s._section_off(_cb(data))), data
    _run(db.set_setting(KEY, "on"))
    assert not _run(s._section_off(_cb("admin_amb_entry")))
    assert _run(admin_sections.section_screen(cb.from_user.id, "amb")) is not None


def test_toggle_button_in_game_settings_flips_key(tmp_path):
    from handlers.amb import admin_amb_section as s
    from handlers.settings import admin_settings
    from tests.test_amb_candidates_34 import ADMIN_ID, _buttons, _cb
    _admin_ready(tmp_path)
    kb = _run(admin_settings.build_settings_group_keyboard("game", ADMIN_ID))
    assert ("🤝 Отбор амбассадоров: ❌ Выкл → ✅ Вкл", "toggle_amb_team_selection") in _buttons(kb)
    cb = _cb("toggle_amb_team_selection")
    _run(s.toggle_amb_team_selection(cb))
    assert _run(db.get_setting(KEY)) == "on"
    text, alert = cb.answers[-1]
    assert alert and len(text) <= 200
    # 09.10: тумблер способ входа не меняет — алерт честно говорит, что вступают сразу.
    assert "Сейчас: ⚡ Сразу по кнопке, мест: без лимита" in text and "Вход и лимит" in text
    assert ("🤝 Отбор амбассадоров: ✅ Вкл → ❌ Выкл", "toggle_amb_team_selection") in _buttons(
        (cb.message.edits or cb.message.answers)[-1][1])
    _run(s.toggle_amb_team_selection(_cb("toggle_amb_team_selection")))
    assert _run(db.get_setting(KEY)) == "off"


def test_season_wizard_leaves_ambassadors_alone(tmp_path):
    from handlers.cities import admin_cities
    from handlers.states import SeasonReset
    from tests.test_amb_bulk_34 import SEASON_OLD, _msg, _season_wizard
    from tests.test_amb_candidates_34 import _sql
    state = _season_wizard(tmp_path)  # 17 статусов: половина в команде с местом, половина кандидаты
    _run(db.delete_setting(KEY))
    before = _sql("SELECT telegram_id, ambassador_status, is_ambassador, ambassador_slot_at "
                  "FROM users ORDER BY telegram_id")

    msg = _msg("RT27")
    _run(admin_cities.season_reset_name_step(msg, state))
    assert "амбассадоров" not in msg.answers[-1][0]

    _run(state.set_state(SeasonReset.passphrase))
    _run(state.update_data(season_new="RT27", season_phrase=SEASON_OLD))
    msg = _msg(SEASON_OLD)
    _run(admin_cities.season_reset_passphrase_step(msg, state))
    assert "амбассадоров" not in msg.answers[-1][0]
    assert _run(db.get_setting("event_season")) == "RT27"
    assert _sql("SELECT telegram_id, ambassador_status, is_ambassador, ambassador_slot_at "
                "FROM users ORDER BY telegram_id") == before
    assert _sql("SELECT COUNT(*) FROM ambassador_season_archive")[0][0] == 0
