"""Уведомление амбассадору о новой ступени СкиллАп: событие `amb_tier_reached` в
`miniapp_outbox` разбирает бот (`services/infra/miniapp_outbox.py` ->
`services/amb/amb_tiers_notify.py`). Одно сообщение на ступень даже при повторной доставке,
тексты без данных приглашённых, выбор текста «слот есть / лист ожидания» по статусу строки.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from config import config
from database import amb_tiers_db as tdb
from database import db
from miniapp import outbox as web_outbox
from services.amb import amb_tiers_notify
from services import applications
from services.infra import miniapp_outbox
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db
from tests.test_amb_tiers_core_su5 import seed_journal_row

SEASON = "SU26"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_tiers_notify_su5.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("amb_qualified_program", "on"))
    _run(db.set_setting("amb_count_deadline", "2099-01-01 00:00"))


def _seed_user(tid, *, referrer_id=None, status="pending", full_name=None, username=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "username": username,
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "referrer_id": referrer_id,
        "season": SEASON,
    }))
    _run(db.set_user_status(tid, status))
    if referrer_id and status == "approved":
        seed_journal_row(tid, referrer_id, season=SEASON)


def _make_ambassador(tid):
    _seed_user(tid, status="approved")
    _run(db.set_ambassador_flag(tid, active=True, at="2026-01-01 00:00:00"))


class _FakeBot:
    id = 42

    def __init__(self, error: Exception | None = None):
        self.sent: list[tuple[int, str]] = []
        self.error = error

    async def send_message(self, chat_id, text, **_kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))


def _tier_row(tid, tier, o2o_status=None):
    _run(tdb.claim_new_tiers(tid, [tier], "2026-09-29 12:00:00", 15))
    if o2o_status is not None:
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute(
            "UPDATE ambassador_tiers SET o2o_status = ? WHERE telegram_id = ? AND tier = ?",
            (o2o_status, tid, tier),
        )
        conn.commit()
        conn.close()


def _default(key):
    return SETTINGS_SCHEMA[key]["default"]


def test_tier1_text_with_left_and_no_second_message(tmp_path):
    _ready(tmp_path)
    _make_ambassador(100)
    _tier_row(100, 1)
    bot = _FakeBot()
    payload = {"telegram_id": 100, "tier": 1, "left": 2}
    _run(miniapp_outbox._handle_row(bot, "amb_tier_reached", payload))
    assert bot.sent == [(100, _default("amb_tier1_text").replace("{left}", "2"))]
    _run(miniapp_outbox._handle_row(bot, "amb_tier_reached", payload))
    assert len(bot.sent) == 1


@pytest.mark.parametrize("tier,status,key", [
    (2, "granted", "amb_tier2_granted_text"),
    (2, "waitlist", "amb_tier2_waitlist_text"),
    (3, None, "amb_tier3_text"),
])
def test_text_chosen_by_tier_and_o2o_status(tmp_path, tier, status, key):
    _ready(tmp_path)
    _make_ambassador(100)
    _tier_row(100, tier, status)
    bot = _FakeBot()
    assert _run(amb_tiers_notify.deliver_tier_notification(bot, 100, tier, 0)) is True
    assert bot.sent == [(100, _default(key))]


def test_temporary_failure_releases_mark_and_raises(tmp_path):
    _ready(tmp_path)
    _make_ambassador(100)
    _tier_row(100, 1)
    bot = _FakeBot(error=RuntimeError("сеть"))
    with pytest.raises(RuntimeError):
        _run(amb_tiers_notify.deliver_tier_notification(bot, 100, 1, 2))
    assert _run(tdb.list_tiers(100))[0]["notified_at"] is None
    ok_bot = _FakeBot()
    assert _run(amb_tiers_notify.deliver_tier_notification(ok_bot, 100, 1, 2)) is True
    assert len(ok_bot.sent) == 1


def test_blocked_bot_no_retry(tmp_path):
    from aiogram.exceptions import TelegramForbiddenError

    _ready(tmp_path)
    _make_ambassador(100)
    _tier_row(100, 1)
    bot = _FakeBot(error=TelegramForbiddenError(method=None, message="bot was blocked by the user"))
    assert _run(amb_tiers_notify.deliver_tier_notification(bot, 100, 1, 2)) is False
    assert _run(tdb.list_tiers(100))[0]["notified_at"] is not None


def test_english_delegate_gets_english_text(tmp_path):
    from services.i18n_form_manual import FORM_DEFAULT_EN, seed

    _ready(tmp_path)
    _run(seed())
    _run(db.set_setting("delegate_lang_enabled", "on"))
    _make_ambassador(100)
    _run(db.set_user_lang(100, "en"))
    _tier_row(100, 1)
    bot = _FakeBot()
    _run(amb_tiers_notify.deliver_tier_notification(bot, 100, 1, 2))
    expected = FORM_DEFAULT_EN[_default("amb_tier1_text")].replace("{left}", "2")
    assert bot.sent == [(100, expected)]


def test_outbox_kind_registered():
    assert "amb_tier_reached" in web_outbox.OUTBOX_KINDS
    assert "amb_tier_reached" in (web_outbox.__doc__ or "")


def test_end_to_end_approval_to_single_message_without_invitee_names(tmp_path):
    """Одобрение -> событие в очереди -> drain -> ровно одно сообщение; второй drain — ноль.
    В тексте нет имени и ника ни одного приглашённого."""
    _ready(tmp_path)
    _make_ambassador(100)
    names = {201: ("Зюзюкин Авдотий", "uniq_first"), 202: ("Хрумкина Евлампия", "uniq_second"),
             203: ("Брыксин Фрол", "uniq_third")}
    for tid, (full_name, username) in names.items():
        _seed_user(tid, referrer_id=100, full_name=full_name, username=username)
    for tid in names:
        _run(applications.claim_approve(tid))
        _run(applications.record_decision(
            tid, "approved", None, 1, datetime.now(), effects_already_sent=True,
        ))

    conn = sqlite3.connect(config.DB_PATH)
    kinds = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM miniapp_outbox WHERE kind = 'amb_tier_reached'"
    )]
    conn.close()
    assert [k["tier"] for k in kinds] == [1, 2]

    bot = _FakeBot()
    _run(miniapp_outbox.drain(bot))
    assert len(bot.sent) == 2
    assert [chat for chat, _ in bot.sent] == [100, 100]
    _run(miniapp_outbox.drain(bot))
    assert len(bot.sent) == 2
    for _chat, text in bot.sent:
        for full_name, username in names.values():
            for part in full_name.split() + [username]:
                assert part not in text


class _MarkupBot:
    """Отвечает «can't parse entities» на HTML, принимает простой текст."""
    id = 42

    def __init__(self):
        self.calls: list[tuple[str | None, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None, **_kwargs):
        from aiogram.exceptions import TelegramBadRequest

        self.calls.append((parse_mode, text))
        if parse_mode == "HTML":
            raise TelegramBadRequest(
                method=None, message="Bad Request: can't parse entities: unclosed tag at byte 5",
            )


def test_broken_html_sent_once_without_formatting(tmp_path):
    """Битый HTML в тексте ступени — не пять ретраев и тишина, а сразу простой текст."""
    _ready(tmp_path)
    _make_ambassador(100)
    _tier_row(100, 3)
    _run(db.set_setting("amb_tier3_text", "<b>Семеро прошли отбор &amp; зовём на нетворкинг"))
    bot = _MarkupBot()
    assert _run(amb_tiers_notify.deliver_tier_notification(bot, 100, 3, 0)) is True
    assert bot.calls == [
        ("HTML", "<b>Семеро прошли отбор &amp; зовём на нетворкинг"),
        (None, "Семеро прошли отбор & зовём на нетворкинг"),
    ]
    assert _run(tdb.list_tiers(100))[0]["notified_at"] is not None
    assert _run(amb_tiers_notify.deliver_tier_notification(bot, 100, 3, 0)) is False
    assert len(bot.calls) == 2
