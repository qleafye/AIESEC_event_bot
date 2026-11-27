"""Превью текста делегата до сохранения: дата и сумма остаются, город чипом не предлагается."""
from __future__ import annotations

import asyncio
from pathlib import Path

from config import config
from database.db import delete_setting, get_setting, set_setting
from domain.cities import ADMIN_CITY_KEY_PREFIX, PER_CITY_SEP
from domain.settings.ops import (
    PREVIEW_SAMPLES,
    item_spec,
    needs_delegate_preview,
    preview_samples,
    preview_text,
)
from miniapp.deps import Principal
from miniapp.routers.settings import settings_preview
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
    wrong = "regional_noshow_wrong_city_text"
    assert "{city}" in SETTINGS_SCHEMA[wrong]["default"]
    assert "{city} — город" not in SETTINGS_SCHEMA[wrong]["prompt"]


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


def test_dialog_data_shows_payment_the_way_a_delegate_reads_it():
    spec = item_spec(PAY, raw=None, value="", is_default=True)
    assert spec["delegate_preview"] is True
    shown = preview_text(PAY, SETTINGS_SCHEMA[PAY]["default"], samples=dict(PREVIEW_SAMPLES))
    assert PREVIEW_SAMPLES["amount"] in shown and "{amount}" not in shown
    assert PREVIEW_SAMPLES["deadline"] in shown and "{deadline}" not in shown
    tokens = [chip["token"] for chip in spec["placeholders"]]
    assert "amount" in tokens and "deadline" in tokens and "city" not in tokens
    number = item_spec("nudge_after_minutes", raw="120", value="120", is_default=False)
    assert number["delegate_preview"] is False
    js = (Path(__file__).resolve().parents[1] / "miniapp" / "static" / "js" / "screens" / "settings.js").read_text(encoding="utf-8")
    assert "loadDelegatePreview" in js and "item.delegate_preview" in js
    assert "/admin/settings/preview" in js and "function buildPreviewButton" in js
    will_fn = js.split("function delegateWillQuery", 1)[1].split("function loadDelegatePreview", 1)[0]
    assert "after_reset=1" in will_fn and "encodeURIComponent(item.key)" in will_fn
    assert "api(delegateWillQuery(item, value))" in js
    was_fn = js.split("function loadDelegatePreview", 1)[1].split("function fillDelegatePreviews", 1)[0]
    assert "const oldSource = item.raw != null ? item.raw : item.value;" in was_fn
    row = js.split("function buildOrdinaryDiffRow", 1)[1].split("function buildNeedsConfirmRow", 1)[0]
    assert "preview.will" in row
    eye = js.split("function buildPreviewButton", 1)[1].split("function applyActions", 1)[0]
    assert "after_reset=1" in eye and "encodeURIComponent(item.key)" in eye


def test_city_reset_preview_shows_shared_text_of_that_city(tmp_path):
    """«Станет» после сброса города — общий текст этого города, не дефолт и не шапка."""
    _ready(tmp_path)
    uid = 910049
    city_key = f"{PAY}{PER_CITY_SEP}spb"
    own = "Свой текст города"
    shared = "Общий счёт {amount} до {deadline}, город {city}"
    planted = "ОБРАЗЕЦ {amount}"
    principal = Principal(telegram_id=uid, via="cookie", caps=frozenset({"settings"}), city=None)

    async def go():
        await set_setting("event_city_enabled", "on")
        await set_setting(f"{ADMIN_CITY_KEY_PREFIX}{uid}", "msk")
        await set_setting(f"payment_deadline{PER_CITY_SEP}spb", "03.10.2026 18:00")
        await set_setting(f"payment_deadline{PER_CITY_SEP}msk", "15.01.2099 10:00")
        await set_setting(city_key, own)
        await set_setting(PAY, shared)
        spb = await preview_samples("spb")
        msk = await preview_samples("msk")
        shown = await settings_preview(city_key, planted, True, principal, principal)
        await delete_setting(PAY)
        missing = await settings_preview(city_key, planted, True, principal, principal)
        await set_setting(PAY, "")
        blank = await settings_preview(city_key, planted, True, principal, principal)
        return spb, msk, shown, missing, blank, await get_setting(city_key), await get_setting(PAY)

    spb, msk, shown, missing, blank, kept_own, kept_shared = _run(go())
    text = shown["text"]
    assert "Общий счёт" in text and "3500" in text
    assert spb["deadline"] in text and spb["city"] in text
    assert msk["deadline"] not in text and msk["city"] not in text
    assert "Оплата участия" not in text and "ОБРАЗЕЦ" not in text
    assert "Оплата участия" in missing["text"] and "ОБРАЗЕЦ" not in missing["text"]
    assert spb["deadline"] in missing["text"] and msk["deadline"] not in missing["text"]
    assert blank["text"] == ""
    assert kept_own == own and kept_shared == ""


def test_global_reset_preview_uses_free_text_when_payment_is_off(tmp_path):
    _ready(tmp_path)
    body = "reg_status_approved_step1_body_text"
    paid = SETTINGS_SCHEMA[body]["default"]
    free = SETTINGS_SCHEMA[body]["default_free"]
    saved = "Свой общий шаг"
    principal = Principal(telegram_id=910050, via="cookie", caps=frozenset({"settings"}), city=None)

    async def go():
        await set_setting("payment_enabled", "off")
        await set_setting(body, saved)
        shown = await settings_preview(body, "ОБРАЗЕЦ", True, principal, principal)
        return shown, await get_setting(body)

    shown, kept = _run(go())
    assert shown["text"] == free
    assert paid not in shown["text"] and "ОБРАЗЕЦ" not in shown["text"]
    assert kept == saved


def test_save_bar_waits_and_city_reset_names_delegate_text():
    """«Сохранить N» не раньше превью. «Как везде» подтверждает и оставляет текст делегата."""
    import json
    import shutil
    import subprocess

    js = (Path(__file__).resolve().parents[1] / "miniapp" / "static" / "js" / "screens" / "settings.js").read_text(encoding="utf-8")
    actions = js.split("function applyActions", 1)[1].split("function updateBatchBar", 1)[0]
    assert 'openResetConfirm(item, "city", el)' in actions
    assert 'openResetConfirm(item, "default", el)' in actions
    assert "queueCityReset(item)" not in actions
    city = js.split("function queueCityReset", 1)[1].split("function hasPlaceholder", 1)[0]
    assert 'display: ""' not in city
    assert "display: text" in city
    resetBox = js.split("async function openResetConfirm", 1)[1].split("function revealPreview", 1)[0]
    assert "after_reset=1" in resetBox
    assert "anchor.after(resetConfirm)" in resetBox
    assert 'scrollIntoView({ block: "center" })' in resetBox
    bar = js.split("function updateBatchBar", 1)[1].split("function matrixCellValue", 1)[0]
    assert "saveBarState" in bar
    assert 'batchBtn.classList.toggle("hidden", !bar.visible)' in bar
    busy = js.split("function setDiffBusy", 1)[1].split("function buildOrdinaryDiffRow", 1)[0]
    assert "delegatePreviewMissing()" in busy

    start = js.index("function delegateStamp")
    end = js.index("function markerFor")
    node = shutil.which("node")
    assert node, "node нужен, чтобы проверить панель сохранения"
    script = js[start:end] + """
const items = {
  letter: { delegate_preview: true },
  tab: { delegate_preview: false },
};
const hidden = saveBarState([{ key: "letter", value: "pay {amount}" }], items, {});
const shown = saveBarState(
  [{ key: "letter", value: "pay {amount}" }],
  items,
  { letter: delegateStamp("pay {amount}") },
);
const other = saveBarState([{ key: "tab", value: "x" }], items, {});
const resetHidden = saveBarState([{ key: "letter", value: null }], items, {});
const resetShown = saveBarState(
  [{ key: "letter", value: null }],
  items,
  { letter: delegateStamp(null) },
);
console.log(JSON.stringify({ hidden, shown, other, resetHidden, resetShown }));
"""
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["hidden"] == {"visible": False, "waiting": True}
    assert got["shown"] == {"visible": True, "waiting": False}
    assert got["other"] == {"visible": True, "waiting": False}
    assert got["resetHidden"]["visible"] is False
    assert got["resetShown"] == {"visible": True, "waiting": False}
