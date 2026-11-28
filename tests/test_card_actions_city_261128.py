"""Перевыпуск QR и чужая анкета отказывают так же, как личное письмо."""
import asyncio

import pytest

import domain.cities as cities
from config import config
from database import db
from database import ext_forms_db as xdb
from domain.cities import set_admin_city
from handlers.ext_forms import admin_ext_forms
from handlers.forum import admin_checkin
from services.applications.personal_letter import OTHER_CITY
from tests._dbtpl import fast_init_db
from tests.test_checkin_reissue_260924 import _FakeCallback

ADMIN_ID = 26112861
BOUND_SPB = 26112862
MSK_ID = 26112863
SPB_ID = 26112864
SECRET = "Секрет чужой анкеты"

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


@pytest.fixture(autouse=True)
def _city_registry():
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    yield
    cities.set_cities_for_test(saved)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "card_actions_city.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(BOUND_SPB, "reg_manager", ADMIN_ID, city="spb"))


def _user(telegram_id, event_city, username):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "username": username,
        "full_name": username,
        "registration_date": "2026-11-28 12:00:00",
        "event_city": event_city,
    }))


def _token(telegram_id):
    return asyncio.run(db.get_user(telegram_id))["checkin_token"]


def _form_for(telegram_id):
    async def go():
        fid = await xdb.create_form(
            platform="yandex", external_id=f"x-{telegram_id}", title="Анкета",
            secret="s3cret",
        )
        await xdb.insert_answer(
            form_id=fid, answer_id="a1", answered_at="2026-11-28 10:00:00",
            received_at="2026-11-28 10:00:01",
            payload=[{"q": "q1", "label": "Ответ", "value": SECRET}],
            raw=None, matched_telegram_id=telegram_id, match_how="username",
        )
    asyncio.run(go())


def test_reissue_confirm_refuses_foreign_city(tmp_path):
    _ready(tmp_path)
    _user(MSK_ID, "msk", "msk_person")
    old = asyncio.run(db.get_or_create_checkin_token(MSK_ID))

    cb = _FakeCallback(f"checkin_reissue:{MSK_ID}", BOUND_SPB)
    asyncio.run(admin_checkin.checkin_reissue_confirm(cb))

    assert cb.answers == [(OTHER_CITY, True)]
    assert cb.message.sent == []
    assert "другого города" in OTHER_CITY
    assert _token(MSK_ID) == old


def test_reissue_go_refuses_foreign_city_without_new_qr(tmp_path):
    _ready(tmp_path)
    _user(MSK_ID, "msk", "msk_person")
    old = asyncio.run(db.get_or_create_checkin_token(MSK_ID))

    cb = _FakeCallback(f"checkin_reissue_yes:{MSK_ID}", BOUND_SPB)
    asyncio.run(admin_checkin.checkin_reissue_go(cb))

    assert cb.message.edits == [OTHER_CITY]
    assert cb.bot.sent_photos == []
    assert _token(MSK_ID) == old


def test_reissue_own_city_still_sends_new_qr(tmp_path):
    _ready(tmp_path)
    _user(SPB_ID, "spb", "spb_person")
    old = asyncio.run(db.get_or_create_checkin_token(SPB_ID))

    cb = _FakeCallback(f"checkin_reissue_yes:{SPB_ID}", BOUND_SPB)
    asyncio.run(admin_checkin.checkin_reissue_go(cb))

    assert cb.message.edits and cb.message.edits[0].startswith("✅ QR перевыпущен.")
    assert len(cb.bot.sent_photos) == 1
    assert _token(SPB_ID) != old


def test_owner_still_reissues_another_selected_city(tmp_path):
    _ready(tmp_path)
    _user(MSK_ID, "msk", "msk_person")
    asyncio.run(set_admin_city(ADMIN_ID, "spb"))
    old = asyncio.run(db.get_or_create_checkin_token(MSK_ID))

    cb = _FakeCallback(f"checkin_reissue_yes:{MSK_ID}", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_reissue_go(cb))

    assert _token(MSK_ID) != old
    assert OTHER_CITY not in "".join(cb.message.edits)
    assert len(cb.bot.sent_photos) == 1


def test_extf_view_hides_foreign_form(tmp_path):
    _ready(tmp_path)
    _user(MSK_ID, "msk", "msk_person")
    _form_for(MSK_ID)

    cb = _FakeCallback(f"extf_view:{MSK_ID}", BOUND_SPB)
    asyncio.run(admin_ext_forms.extf_view(cb))
    page = _FakeCallback(f"extf_view:{MSK_ID}:0", BOUND_SPB)
    asyncio.run(admin_ext_forms.extf_view(page))

    assert cb.answers == [(OTHER_CITY, True)]
    assert page.answers == [(OTHER_CITY, True)]
    assert cb.message.sent == []
    assert page.message.edits == []
    shown = " ".join(str(item) for item in (cb.message.sent, page.message.edits, cb.answers))
    assert SECRET not in shown


def test_extf_view_shows_own_city_form(tmp_path):
    _ready(tmp_path)
    _user(SPB_ID, "spb", "spb_person")
    _form_for(SPB_ID)

    cb = _FakeCallback(f"extf_view:{SPB_ID}", BOUND_SPB)
    asyncio.run(admin_ext_forms.extf_view(cb))

    assert cb.message.sent
    assert SECRET in cb.message.sent[0][0]
    assert OTHER_CITY not in cb.message.sent[0][0]
