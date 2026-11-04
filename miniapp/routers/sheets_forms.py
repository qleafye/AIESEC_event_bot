"""Карточка «Таблица и формы».

Вкладки и книга только читаются. Id формы пишется, если он есть в list_forms.
Вопрос делегаций берётся из свежего list_columns по индексу, не из строки клиента.
Включение: нет трёх вопросов — ничего не пишем и sweep не зовём; иначе просмотр числа,
затем delegation_armed_form_id, затем sweep. Сезон эта ручка не закрывает.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

import database.ext_forms_db as ext_forms_db
import domain.settings.ops as settings_ops
from domain.settings.schema import get_setting_typed
from miniapp.deps import Principal, require_cap, require_section
from services.delegations import delegations
from services.settings.audit import set_setting_by_admin
from services.sheets.sheet_target import sheet_id

router = APIRouter()

_TAB_KEYS = (
    ("main", "main_sheet_tab"),
    ("short", "short_sheet_tab"),
    ("party", "party_sheet_tab"),
    ("incomplete", "incomplete_sheet_tab"),
)

_SLOTS = (
    ("registration", "registration_form_id"),
    ("delegation", "delegation_form_id"),
    ("ambassador", "ambassador_form_id"),
)

_QUESTIONS = (
    ("fullname", "Имя", "delegation_q_fullname"),
    ("university", "Вуз", "delegation_q_university"),
    ("course", "Курс", "delegation_q_course"),
)

_REQUIRED = ("fullname", "university", "course")

_KEYS_MISSING = (
    "Сначала выберите вопросы формы: имя, вуз и курс. Без этого делегации не включаются."
)
_NO_FORM = "Форма не выбрана"

_SLOT_KEY = dict(_SLOTS)
_QUESTION_KEY = {which: key for which, _label, key in _QUESTIONS}


class SlotIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slot: str
    index: int | None = None
    form_id: int | None = None


class QuestionIn(BaseModel):
    # qkey клиента сюда не входит: лишнее поле отбрасывается, колонка берётся по index.
    model_config = ConfigDict(extra="ignore")

    which: str
    index: int


class DelegationsIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str


def _public_name(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(raw) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def _deny_city(admin_id: int) -> None:
    if not await settings_ops.can_write_common(admin_id):
        raise HTTPException(403, {"reason": "common_locked"})


async def _card() -> dict:
    forms = await ext_forms_db.list_forms()
    by_id = {int(form["id"]): form for form in forms}
    tabs = []
    for key, setting_key in _TAB_KEYS:
        tabs.append({"key": key, "name": _public_name(await get_setting_typed(setting_key))})
    slots = []
    for slot, setting_key in _SLOTS:
        title = None
        fid = _int_or_none(await get_setting_typed(setting_key))
        if fid is not None and fid in by_id:
            title = _public_name(by_id[fid].get("title"))
        slots.append({"slot": slot, "title": title})
    dfid = await delegations.delegation_form_id()
    columns = await ext_forms_db.list_columns(dfid) if dfid is not None else []
    label_by_qkey = {col["qkey"]: _public_name(col.get("label")) for col in columns}
    keys = await delegations.field_keys()
    questions = []
    for which, label, _setting_key in _QUESTIONS:
        qkey = keys.get(which)
        questions.append({
            "which": which,
            "label": label,
            "text": label_by_qkey.get(qkey) if qkey else None,
        })
    return {
        "tabs": tabs,
        "book_connected": bool(sheet_id()),
        "slots": slots,
        "delegations_on": await delegations.is_armed(),
        "questions_ready": all(keys.get(which) for which in _REQUIRED),
        "questions": questions,
        "forms": [_public_name(form.get("title")) for form in forms],
        "question_choices": [_public_name(col.get("label")) for col in columns],
    }


def _chosen_form(forms: list[dict], body: SlotIn) -> dict | None:
    # Присланный id не подменяется индексом: нет такого id в свежем списке — не пишем.
    if body.form_id is not None:
        for form in forms:
            if int(form["id"]) == int(body.form_id):
                return form
        return None
    if body.index is None or body.index < 0 or body.index >= len(forms):
        return None
    return forms[body.index]


async def _arm_block() -> str | None:
    keys = await delegations.field_keys()
    if any(not keys.get(which) for which in _REQUIRED):
        return _KEYS_MISSING
    fid = await delegations.delegation_form_id()
    if fid is None or await ext_forms_db.get_form(fid) is None:
        return _NO_FORM
    return None


@router.get("/app/api/admin/sheets-forms")
async def sheets_forms_card(
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    del p
    return await _card()


@router.post("/app/api/admin/sheets-forms/slot")
async def sheets_forms_slot(
    body: SlotIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    await _deny_city(p.telegram_id)
    key = _SLOT_KEY.get(body.slot)
    if key is None:
        raise HTTPException(400, {"reason": "bad_slot"})
    form = _chosen_form(await ext_forms_db.list_forms(), body)
    if form is None:
        raise HTTPException(400, {"reason": "unknown_form"})
    await set_setting_by_admin(p.telegram_id, key, str(int(form["id"])))
    if body.slot == "delegation":
        # Пустая строка, не удаление ключа: старт базы не включит модуль сам.
        await set_setting_by_admin(p.telegram_id, "delegation_armed_form_id", "")
    return await _card()


@router.post("/app/api/admin/sheets-forms/question")
async def sheets_forms_question(
    body: QuestionIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    await _deny_city(p.telegram_id)
    key = _QUESTION_KEY.get(body.which)
    if key is None:
        raise HTTPException(400, {"reason": "bad_question"})
    fid = await delegations.delegation_form_id()
    columns = await ext_forms_db.list_columns(fid) if fid is not None else []
    if body.index < 0 or body.index >= len(columns):
        raise HTTPException(400, {"reason": "bad_index"})
    await set_setting_by_admin(p.telegram_id, key, str(columns[body.index]["qkey"]))
    return await _card()


@router.post("/app/api/admin/sheets-forms/delegations")
async def sheets_forms_delegations(
    body: DelegationsIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    if body.action not in ("preview", "on", "off"):
        raise HTTPException(400, {"reason": "bad_action"})
    if body.action != "preview":
        await _deny_city(p.telegram_id)
    if body.action == "off":
        await set_setting_by_admin(p.telegram_id, "delegation_armed_form_id", "")
        return {"ok": True, "delegations_on": False}
    block = await _arm_block()
    if block is not None:
        return {"ok": False, "text": block}
    if body.action == "preview":
        return {"ok": True, "count": await delegations.preview_reevaluate()}
    fid = await delegations.delegation_form_id()
    await set_setting_by_admin(p.telegram_id, "delegation_armed_form_id", str(fid))
    await delegations.sweep_pending(reevaluate=True)
    return {"ok": True, "delegations_on": True}
