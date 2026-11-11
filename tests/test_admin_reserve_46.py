"""Экран «Позвать резерв»: только живой автоотказ города шапки, без смены status."""
from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError

from config import config
from database import db
from domain.cities import ALL_CITIES, city_scope, set_admin_city
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.forum import admin_reserve as rsv
from services.forum.reserve import today_msk
from tests._dbtpl import fast_init_db

ADMIN = 461100
DAY = "2026-01-01 12:00:00"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def _restore_cities_cache():
    import domain.cities as cities
    snapshot = list(cities.CITIES)
    yield
    cities.CITIES.clear()
    cities.CITIES.extend(snapshot)


def _env(tmp_path):
    config.DB_PATH = str(tmp_path / "reserve_admin.db")
    config.GOOGLE_SHEET_ID = ""
    fast_init_db()


async def _status(tid: int, status: str) -> None:
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, tid))
        await conn.commit()


async def _return_log(tid: int) -> None:
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE auto_reject_log SET returned_to_moderation_at = ? WHERE telegram_id = ?",
            ("2026-01-02 00:00:00", tid),
        )
        await conn.commit()


async def _invite_rows() -> int:
    async with db._connect() as conn:
        async with conn.execute("SELECT COUNT(*) FROM reserve_invites") as cur:
            row = await cur.fetchone()
            return int(row[0])


def _person(tid: int, name: str, city: str, *, status: str = "rejected", rules: str | None = None):
    _run(db.add_user({
        "telegram_id": tid, "full_name": name, "username": f"u{tid}",
        "event_city": city, "registration_date": "2026-01-01",
    }))
    _run(_status(tid, status))
    if rules:
        _run(db.upsert_auto_reject_log(tid, rules, '["текст"]', DAY))


def _cities():
    _run(db.insert_city("msk", "Москва", "msk", 0))
    _run(db.insert_city("spb", "Санкт-Петербург", "spb", 1))
    import domain.cities as cities
    _run(cities.reload_cities())
    _run(db.set_setting("event_city_enabled", "on"))


class _Bot:
    def __init__(self, blocked=()):
        self.sent = []
        self.blocked = set(blocked)

    async def send_message(self, chat_id, text, parse_mode=None):
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        self.sent.append(("msg", chat_id, text))

    async def send_photo(self, chat_id, photo, caption=None):
        self.sent.append(("photo", chat_id, caption, photo))

    async def send_document(self, chat_id, document, caption=None):
        self.sent.append(("document", chat_id, document))

    async def send_video(self, chat_id, video, caption=None):
        self.sent.append(("video", chat_id))

    async def send_audio(self, chat_id, audio, caption=None):
        self.sent.append(("audio", chat_id))

    async def send_animation(self, chat_id, animation, caption=None):
        self.sent.append(("animation", chat_id))


def test_header_city_keeps_only_live_auto_reject(tmp_path, _restore_cities_cache):
    _env(tmp_path)
    _cities()
    assert _run(set_admin_city(ADMIN, "msk")) is True
    _person(461201, "Живой Мск", "msk", rules="[1]")
    _person(461202, "Ручной Мск", "msk")
    _person(461203, "Живой Спб", "spb", rules="[1]")
    _person(461204, "Вернули Мск", "msk", rules="[1]")
    _run(_return_log(461204))
    _person(461205, "Одобрен Мск", "msk", status="approved", rules="[1]")
    rows = _run(rsv.rows_for_header(ADMIN))
    assert [row["telegram_id"] for row in rows] == [461201]


def test_all_follows_current_rule_filter(tmp_path, _restore_cities_cache):
    _env(tmp_path)
    _cities()
    _run(set_admin_city(ADMIN, "msk"))
    _person(461211, "Первое правило", "msk", rules="[1]")
    _person(461212, "Второе правило", "msk", rules="[2]")
    ids = _run(rsv.ids_for_action(ADMIN, rule_id=1, mode="all", selected=[461211, 461212]))
    assert ids == [461211]
    everyone = _run(rsv.ids_for_action(ADMIN, rule_id=None, mode="all", selected=[]))
    assert set(everyone) == {461211, 461212}


def test_all_cities_header_sends_nothing(tmp_path, _restore_cities_cache):
    _env(tmp_path)
    _cities()
    _person(461221, "Живой Мск", "msk", rules="[1]")
    _run(set_admin_city(ADMIN, ALL_CITIES))
    assert _run(rsv.rows_for_header(ADMIN)) is None
    bot = _Bot()
    result = _run(rsv.deliver(bot, ADMIN, [461221], city_code=ALL_CITIES, rule_id=None))
    assert bot.sent == []
    assert result["new"] == 0
    assert _run(_invite_rows()) == 0
    assert "город" in rsv.PICK_CITY.lower()


def test_confirmation_says_approved_status_will_not_appear():
    text = rsv.confirm_body(4, "09.11.2026", attached=False)
    assert text.startswith(rsv.confirm_line(4, "09.11.2026"))
    assert "Статус „одобрен“ не появится" in text
    assert "09.11.2026" in text
    assert rsv.today_label() == __import__("services.infra.timeutil", fromlist=["msk_now"]).msk_now().strftime("%d.%m.%Y")


def test_same_day_repeat_is_silent_and_status_stays(tmp_path, _restore_cities_cache, monkeypatch):
    _env(tmp_path)
    _cities()
    _person(461231, "Анна Резерв", "msk", rules="[1]")
    monkeypatch.setattr(rsv, "build_checkin_qr", _qr_ok)
    bot = _Bot()
    scope = city_scope("msk")
    assert rsv.user_in_city_scope("msk", scope) is True
    assert rsv.user_in_city_scope("spb", scope) is False
    first = _run(rsv.deliver(bot, ADMIN, [461231], city_code="msk", rule_id=None))
    assert first["new"] == 1
    assert len([item for item in bot.sent if item[0] == "msg"]) == 1
    assert "09." in bot.sent[0][2] or rsv.today_label() in bot.sent[0][2]
    assert any(item[0] == "photo" for item in bot.sent)
    user = _run(db.get_user(461231))
    assert user["status"] == "rejected"
    assert _run(_invite_rows()) == 1
    again = _run(rsv.deliver(bot, ADMIN, [461231], city_code="msk", rule_id=1))
    assert again["new"] == 0 and again["already"] == 1
    assert len([item for item in bot.sent if item[0] == "msg"]) == 1
    assert _run(_invite_rows()) == 1
    assert _run(db.get_user(461231))["status"] == "rejected"
    assert _run(db.is_invited_reserve(461231, today_msk(), city="msk")) is True


def test_blocked_bot_and_missing_qr_are_named(tmp_path, _restore_cities_cache, monkeypatch):
    _env(tmp_path)
    _cities()
    _person(461241, "Борис Блок", "msk", rules="[7]")
    _person(461242, "Вера Кьюар", "msk", rules="[7]")
    monkeypatch.setattr(rsv, "build_checkin_qr", _qr_fail)
    bot = _Bot(blocked={461241})
    result = _run(rsv.deliver(
        bot, ADMIN, [461241, 461242], city_code="msk", rule_id=7,
    ))
    assert result["new"] == 1
    assert result["failed"] == 1 and result["blocked"] == 1
    assert result["qr_miss"] == ["Вера Кьюар"]
    texts = [item[2] for item in bot.sent if item[0] == "msg"]
    assert texts == [rsv.letter_body(
        SETTINGS_SCHEMA["reserve_invite_text"]["default"],
        name="Вера Кьюар", date=rsv.today_label(), city="Москва",
    )]
    assert _run(db.get_user(461241))["status"] == "rejected"
    assert _run(db.get_user(461242))["status"] == "rejected"
    report = rsv.report_text(
        new=result["new"], already=result["already"], failed=result["failed"],
        blocked=result["blocked"], qr_miss=result["qr_miss"],
    )
    assert "Новым позвали 1." in report
    assert "Не доставлено 1." in report
    assert "заблокировал бота" in report
    assert "Вера Кьюар" in report
    assert letter_blank_is_ready()


def test_other_city_and_manual_reject_are_not_delivered(tmp_path, _restore_cities_cache, monkeypatch):
    _env(tmp_path)
    _cities()
    _person(461251, "Чужой город", "spb", rules="[1]")
    _person(461252, "Ручной отказ", "msk")
    monkeypatch.setattr(rsv, "build_checkin_qr", _qr_ok)
    bot = _Bot()
    result = _run(rsv.deliver(bot, ADMIN, [461251, 461252], city_code="msk", rule_id=None))
    assert result["new"] == 0
    assert bot.sent == []
    assert _run(_invite_rows()) == 0


def test_callback_data_stays_short():
    sample = f"rsv:t:{10 ** 15}"
    assert len(sample.encode()) <= 64
    assert SETTINGS_SCHEMA["reserve_invite_text"]["default"] == rsv.READY_LETTER
    assert "{name}" in SETTINGS_SCHEMA["reserve_invite_text"]["prompt"]
    assert "{date}" in SETTINGS_SCHEMA["reserve_invite_text"]["prompt"]
    assert "{city}" in SETTINGS_SCHEMA["reserve_invite_text"]["prompt"]


def letter_blank_is_ready() -> bool:
    body = rsv.letter_body("  ", name="Анна", date="09.11.2026", city="Москва")
    return body == rsv.letter_body(None, name="Анна", date="09.11.2026", city="Москва") and "09.11.2026" in body


async def _qr_ok(user):
    return b"png", "qr"


async def _qr_fail(user):
    raise RuntimeError("qr")


def test_app_list_matches_chat_header(tmp_path, _restore_cities_cache):
    from services.forum import reserve_invite

    _env(tmp_path)
    _cities()
    assert _run(set_admin_city(ADMIN, "msk")) is True
    _person(461261, "Живой Мск", "msk", rules="[1]")
    _person(461262, "Живой Спб", "spb", rules="[1]")
    rows = _run(rsv.rows_for_header(ADMIN))
    view = _run(reserve_invite.screen(ADMIN))
    assert [row["telegram_id"] for row in rows] == [person["id"] for person in view["people"]]
    assert view["pick_city"] is False
    assert "Москва" in view["text"]
    assert "forbidden" not in view["text"].lower()


def test_app_all_cities_sends_nobody(tmp_path, _restore_cities_cache):
    from services.forum import reserve_invite

    _env(tmp_path)
    _cities()
    _person(461271, "Живой Мск", "msk", rules="[1]")
    _run(set_admin_city(ADMIN, ALL_CITIES))
    view = _run(reserve_invite.screen(ADMIN))
    assert view["pick_city"] is True and view["people"] == []
    decision = _run(reserve_invite.invite_decision(
        ADMIN, mode="all", selected=[461271], confirm=True,
    ))
    assert decision.get("enqueue") is None
    assert "город" in decision["text"].lower()
    assert _run(_invite_rows()) == 0


def test_confirm_does_not_send_until_the_second_step(tmp_path, _restore_cities_cache):
    from services.forum import reserve_invite

    _env(tmp_path)
    _cities()
    _run(set_admin_city(ADMIN, "msk"))
    _person(461281, "Анна Резерв", "msk", rules="[1]")
    preview = _run(reserve_invite.invite_decision(
        ADMIN, mode="selected", selected=[461281], confirm=False,
    ))
    assert preview["need_confirm"] is True
    assert "одобрен" in preview["text"]
    assert "enqueue" not in preview
    assert _run(_invite_rows()) == 0
    ready = _run(reserve_invite.invite_decision(
        ADMIN, mode="selected", selected=[461281], confirm=True,
    ))
    assert ready["enqueue"]["selected"] == [461281]
    assert ready["enqueue"]["admin_id"] == ADMIN
    assert _run(_invite_rows()) == 0


def test_queued_invite_uses_chat_deliver(tmp_path, _restore_cities_cache, monkeypatch):
    from services.infra.miniapp_outbox import _handle_row

    _env(tmp_path)
    _cities()
    _run(set_admin_city(ADMIN, "msk"))
    _person(461291, "Анна Резерв", "msk", rules="[1]")
    monkeypatch.setattr(rsv, "build_checkin_qr", _qr_ok)
    bot = _Bot()
    _run(_handle_row(bot, "reserve_invite", {
        "admin_id": ADMIN, "mode": "selected", "selected": [461291],
    }))
    assert len([item for item in bot.sent if item[0] == "msg"]) == 1
    assert _run(db.get_user(461291))["status"] == "rejected"
    assert _run(_invite_rows()) == 1
    _run(_handle_row(bot, "reserve_invite", {
        "admin_id": ADMIN, "mode": "selected", "selected": [461291],
    }))
    assert len([item for item in bot.sent if item[0] == "msg"]) == 1
    assert _run(_invite_rows()) == 1


def test_chat_button_stays_and_missing_right_is_plain():
    from handlers.access.admin_caps import required_capability
    from services.forum.reserve_invite import NEED_CAP
    from dashboard.forum_day import RESERVE_HREF, reserve_page

    assert rsv.rsv_open is not None
    assert rsv.rsv_act is not None
    assert rsv.rsv_file is not None
    assert required_capability(callback_data="rsv") == "moderate_reg"
    assert required_capability(callback_data="rsv:go") == "moderate_reg"
    assert "forbidden" not in NEED_CAP.lower()
    assert "заяв" in NEED_CAP.lower()
    page = reserve_page()
    assert RESERVE_HREF in page
    assert "SELECT" not in page
    assert "list_live_reserve" not in page
