"""Приёмка 16.09: «Спрашивать язык: всем при первом /start» зацикливался у нового делегата —
`set_user_lang` делал UPDATE users, строки ещё не было, выбор терялся, /start спрашивал снова."""
import asyncio

from config import config
from database import db
from services import i18n

UID = 777001


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_lang_before_user_row_260916.db")
    asyncio.run(db.init_db())


def test_choice_is_kept_without_users_row(tmp_path):
    _use_tmp_db(tmp_path)

    async def scenario():
        assert await db.get_user(UID) is None
        await db.set_user_lang(UID, "en")
        assert await db.get_stored_lang(UID) == "en"
        # Повторный /start отмечает старт анкеты — выбор языка не затирается.
        await db.mark_reg_started(UID, "delegate")
        assert await db.get_stored_lang(UID) == "en"

    asyncio.run(scenario())


def test_delegate_lang_sees_choice_before_registration(tmp_path):
    _use_tmp_db(tmp_path)

    async def scenario():
        await db.set_setting("delegate_lang_enabled", "on")
        await db.set_user_lang(UID, "en")
        assert await i18n.delegate_lang(UID, "ru") == "en"

    asyncio.run(scenario())


def test_choice_moves_to_users_on_submit(tmp_path):
    _use_tmp_db(tmp_path)

    async def scenario():
        await db.set_user_lang(UID, "en")
        await db.add_user({"telegram_id": UID, "username": "@delegate", "full_name": "Иванов Иван", "registration_date": "2026-09-16 23:00:00"})
        await db.clear_reg_started(UID)
        user = await db.get_user(UID)
        assert user["lang"] == "en"
        assert await db.get_stored_lang(UID) == "en"

    asyncio.run(scenario())


def test_existing_users_row_still_updated_in_place(tmp_path):
    _use_tmp_db(tmp_path)

    async def scenario():
        await db.add_user({"telegram_id": UID, "username": "@delegate", "full_name": "Иванов Иван", "registration_date": "2026-09-16 23:00:00"})
        await db.set_user_lang(UID, "en")
        assert (await db.get_user(UID))["lang"] == "en"

    asyncio.run(scenario())
