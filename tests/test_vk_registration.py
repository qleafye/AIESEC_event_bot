"""Анкета ВК: тот же движок, пустой telegram_id, до сдачи не одобрена."""
import asyncio

import aiosqlite

from config import config
from database import db
from handlers.vk.vk_bot import handle_vk_message
from handlers.vk.vk_registration import process_vk_registration_step, start_vk_registration
from services.vk.vk_fsm import get_vk_fsm_state, set_vk_fsm_data, set_vk_fsm_state


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "vk_reg.db")


def _capture(monkeypatch):
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text, keyboard_json))

    monkeypatch.setattr("handlers.vk.vk_registration.send_vk_message", _send)
    return sent


def _gate(monkeypatch, kind="go", code=None):
    async def _city_gate(_event_city):
        return kind, code

    monkeypatch.setattr("handlers.vk.vk_registration.city_gate", _city_gate)


def _steps(monkeypatch, steps):
    async def _enabled(_answers, _city):
        return list(steps)

    monkeypatch.setattr("handlers.vk.vk_registration.enabled_steps", _enabled)


def _flow(monkeypatch, screens):
    async def _pre(_answers, _meta):
        return list(screens)

    monkeypatch.setattr("handlers.vk.vk_registration.pre_flow", _pre)


def test_start_asks_full_name_not_age(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    _gate(monkeypatch)
    _flow(monkeypatch, [])
    _steps(monkeypatch, ["age"])

    asyncio.run(start_vk_registration(7))

    assert asyncio.run(get_vk_fsm_state(7)) == "full_name"
    assert "ФИО" in sent[-1][1]
    assert asyncio.run(db.get_vk_user(7)) is None


def test_short_name_stays_on_full_name(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    _gate(monkeypatch)
    _flow(monkeypatch, [])
    _steps(monkeypatch, ["age"])
    asyncio.run(start_vk_registration(7))

    asyncio.run(process_vk_registration_step(7, "Иван"))

    assert asyncio.run(get_vk_fsm_state(7)) == "full_name"
    assert "ФИО" in sent[-1][1]
    assert asyncio.run(db.get_vk_user(7)) is None


def test_name_then_next_engine_step_is_not_approved(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _capture(monkeypatch)
    _gate(monkeypatch)
    _flow(monkeypatch, [])
    _steps(monkeypatch, ["age"])
    asyncio.run(start_vk_registration(7))

    asyncio.run(process_vk_registration_step(7, "Иван Иванов"))

    row = asyncio.run(db.get_vk_user(7))
    assert asyncio.run(get_vk_fsm_state(7)) == "age"
    assert row["full_name"] == "Иван Иванов"
    assert row["telegram_id"] is None
    assert row["status"] is None
    assert row["vk_id"] == 7


def test_empty_form_submits_pending(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    _gate(monkeypatch)
    _flow(monkeypatch, [])
    _steps(monkeypatch, [])
    asyncio.run(start_vk_registration(7))

    asyncio.run(process_vk_registration_step(7, "Иван Иванов"))

    row = asyncio.run(db.get_vk_user(7))
    assert row["status"] == "pending"
    assert row["telegram_id"] is None
    assert row["registration_date"]
    assert asyncio.run(get_vk_fsm_state(7)) is None
    assert "отправлена" in sent[-1][1]


def test_pending_start_does_not_open_second_card(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    asyncio.run(db.create_vk_user(7, {"status": "pending", "full_name": "Иван Иванов"}))

    asyncio.run(handle_vk_message(7, "/start ref"))

    assert "уже есть" in sent[-1][1]
    assert asyncio.run(get_vk_fsm_state(7)) is None
    assert asyncio.run(db.get_vk_user(7))["status"] == "pending"


def test_no_state_tells_how_to_start(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)

    asyncio.run(process_vk_registration_step(7, "просто текст"))

    assert sent == [(7, "Напишите /start, чтобы заполнить анкету.", None)]


def test_city_fork_stores_chosen_city(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    _gate(monkeypatch, "fork", None)
    _flow(monkeypatch, [])
    _steps(monkeypatch, [])

    async def _options():
        return [{"code": "msk", "label": "Москва"}]

    async def _valid(code):
        return ("msk", None) if code == "msk" else (None, "Некорректный выбор.")

    monkeypatch.setattr("handlers.vk.vk_registration.city_fork_options", _options)
    monkeypatch.setattr("handlers.vk.vk_registration.validate_city_choice", _valid)

    asyncio.run(start_vk_registration(7))
    assert "Москва" in sent[-1][1]
    assert asyncio.run(get_vk_fsm_state(7)) == "city_fork"

    asyncio.run(process_vk_registration_step(7, "Москва"))

    row = asyncio.run(db.get_vk_user(7))
    assert row["event_city"] == "msk"
    assert row["status"] is None
    assert row["telegram_id"] is None
    assert asyncio.run(get_vk_fsm_state(7)) == "full_name"


def test_closed_city_does_not_create_card(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    _gate(monkeypatch, "all_closed", None)

    asyncio.run(start_vk_registration(7))

    assert "закрыт" in sent[-1][1].lower()
    assert asyncio.run(db.get_vk_user(7)) is None
    assert asyncio.run(get_vk_fsm_state(7)) is None


def test_party_fork_only_when_module_asks(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    _gate(monkeypatch)
    _flow(monkeypatch, ["consent:pd", "party_fork"])
    _steps(monkeypatch, [])

    async def _tracks():
        return [{"code": "full", "label": "Форум"}]

    async def _valid(code, _city):
        return ("full", None) if code == "full" else (None, "Выбери формат участия.")

    monkeypatch.setattr("handlers.vk.vk_registration.party_track_options", _tracks)
    monkeypatch.setattr("handlers.vk.vk_registration.validate_track_choice", _valid)

    asyncio.run(start_vk_registration(7))
    assert asyncio.run(get_vk_fsm_state(7)) == "party_fork"
    assert "Форум" in sent[-1][1]
    assert "consent" not in sent[-1][1]

    asyncio.run(process_vk_registration_step(7, "Форум"))

    row = asyncio.run(db.get_vk_user(7))
    assert row["participant_type"] == "full"
    assert row["status"] is None
    assert asyncio.run(get_vk_fsm_state(7)) == "full_name"

    async def _consents():
        async with aiosqlite.connect(config.DB_PATH) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM user_consents")
            return (await cursor.fetchone())[0]

    assert asyncio.run(_consents()) == 0


def test_multi_answer_joins_variants(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    _gate(monkeypatch)
    _flow(monkeypatch, [])
    _steps(monkeypatch, ["goal"])
    asyncio.run(start_vk_registration(7))
    asyncio.run(process_vk_registration_step(7, "Иван Иванов"))
    assert "запятую" in sent[-1][1]

    asyncio.run(process_vk_registration_step(7, "а, б"))

    row = asyncio.run(db.get_vk_user(7))
    assert row["goal"] == "а, б"
    assert row["status"] == "pending"


def test_vk_nick_is_not_the_account_link(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _capture(monkeypatch)
    _gate(monkeypatch)
    _flow(monkeypatch, [])
    _steps(monkeypatch, ["vk"])
    asyncio.run(start_vk_registration(7))
    asyncio.run(process_vk_registration_step(7, "Иван Иванов"))

    asyncio.run(process_vk_registration_step(7, "@ivanova"))

    row = asyncio.run(db.get_vk_user(7))
    assert row["vk_id"] == 7
    assert row["vk_username"] == "@ivanova"
    assert row["telegram_id"] is None


def test_restart_reasks_current_step(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    _gate(monkeypatch)
    _flow(monkeypatch, [])
    asyncio.run(set_vk_fsm_state(7, "full_name"))
    asyncio.run(set_vk_fsm_data(7, {"answers": {}}))

    asyncio.run(start_vk_registration(7))

    assert asyncio.run(get_vk_fsm_state(7)) == "full_name"
    assert "ФИО" in sent[-1][1]
