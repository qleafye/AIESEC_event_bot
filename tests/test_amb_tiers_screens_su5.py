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
from handlers import reg_i18n
from handlers import user_actions as ua_mod
from settings_schema import get_setting_typed
from tests._dbtpl import fast_init_db

SEASON = "SU26"
AMB = 700
NAMES = {801: "Иван Уникальный", 802: "Пётр Особый", 803: "Мария Третья"}


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_tiers_screens_su5.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))


def _seed_user(tid, *, referrer_id=None, status="pending", full_name=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "referrer_id": referrer_id,
        "season": SEASON,
    }))
    _run(db.set_user_status(tid, status))


def _make_ambassador(tid=AMB):
    _seed_user(tid, status="approved", full_name="Амбассадор Тестовый")
    _run(db.set_ambassador_flag(tid, active=True, at="2026-09-01 00:00:00"))


def _seed_invitees(n=3, amb=AMB):
    for tid in list(NAMES)[:n]:
        _seed_user(tid, referrer_id=amb, full_name=NAMES[tid])


def _approve(tid):
    _run(db.set_user_status(tid, "approved"))


def _credit(invitee, amb=AMB):
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
