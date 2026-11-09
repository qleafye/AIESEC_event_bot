"""Превью текста делегата до сохранения: дата и сумма остаются, город чипом не предлагается."""
from __future__ import annotations

import asyncio

from config import config
from domain.settings.ops import (
    PREVIEW_SAMPLES,
    item_spec,
    needs_delegate_preview,
    preview_samples,
    preview_text,
)
from domain.settings.placeholders import expected_placeholders, hint
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

PAY = "payment_details_template_text"
SCHEDULE = "session_enroll_confirmed_text"
CLOSED = "city_reg_closed_text"
RESERVE = "reserve_invite_text"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "preview.db")
    fast_init_db()


def test_delegate_letters_wait_service_settings_do_not():
    assert needs_delegate_preview(PAY)
    assert needs_delegate_preview(SCHEDULE)
    assert needs_delegate_preview(f"{SCHEDULE}__city__msk")
    assert not needs_delegate_preview("main_sheet_tab")
    assert not needs_delegate_preview("sheet_tab_bot_prefix")
    assert not needs_delegate_preview("city_tab_suffix__short")
    assert not needs_delegate_preview("payment_enabled")
    assert not needs_delegate_preview("nudge_after_minutes")


def test_payment_and_schedule_keep_deadline_and_amount():
    pay = SETTINGS_SCHEMA[PAY]["default"]
    assert "{amount}" in pay and "{deadline}" in pay
    assert "{amount}" in SETTINGS_SCHEMA[PAY]["prompt"]
    assert "{deadline}" in SETTINGS_SCHEMA[PAY]["prompt"]
    assert "{deadline}" in SETTINGS_SCHEMA[SCHEDULE]["default"]
    assert "{deadline}" in hint(SCHEDULE)
    shown = preview_text(PAY, pay, samples=dict(PREVIEW_SAMPLES))
    assert "3500" in shown and "{amount}" not in shown
    assert PREVIEW_SAMPLES["deadline"] in preview_text(
        SCHEDULE, SETTINGS_SCHEMA[SCHEDULE]["default"], samples=dict(PREVIEW_SAMPLES),
    )


def test_city_chip_hidden_defaults_kept():
    assert "{city}" in SETTINGS_SCHEMA[CLOSED]["default"]
    assert "{city}" in SETTINGS_SCHEMA[RESERVE]["default"]
    assert "{city}" not in SETTINGS_SCHEMA[CLOSED]["prompt"]
    assert "city" in expected_placeholders(RESERVE)
    tokens = [chip["token"] for chip in item_spec(RESERVE, raw=None, value="", is_default=True)["placeholders"]]
    assert "name" in tokens and "city" not in tokens
    pay_tokens = [chip["token"] for chip in item_spec(PAY, raw=None, value="", is_default=True)["placeholders"]]
    assert "amount" in pay_tokens and "deadline" in pay_tokens and "city" not in pay_tokens
    assert "{city}" not in hint(CLOSED)
    assert hint(CLOSED) == ""


def test_header_city_fills_sample_name_stays_ivan(tmp_path):
    _ready(tmp_path)

    async def go():
        named = await preview_samples("msk")
        blank = await preview_samples(None)
        every = await preview_samples("*")
        return named, blank, every

    named, blank, every = _run(go())
    assert named["name"] == "Иван"
    assert named["city"] != "msk" and "Москва" in named["city"]
    assert blank["city"] == PREVIEW_SAMPLES["city"]
    assert every["city"] == PREVIEW_SAMPLES["city"]
    assert named["amount"] == PREVIEW_SAMPLES["amount"]
