"""HTTP редактора правил автоотказа.

Логика живёт в `services.applications.reject_rules_editor`. Роутер только переводит
ответ редактора в код HTTP: город и слепок он сам не сравнивает. Общий рубильник
пишет `reject_rules_enabled` напрямую — журнал настроек тянет aiogram, а этот
процесс его не грузит.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database.db import set_setting
from domain.cities import city_label
from domain.settings.ops import per_city_visible_codes
from miniapp.deps import Principal, require_cap, require_section
from services.applications import reject_rules_editor
from services.applications.reject_rules import rules_for_admin

logger = logging.getLogger(__name__)

router = APIRouter()

TEXT_NOT_FOUND = "Это правило уже удалили"
TEXT_OUT_OF_SCOPE = (
    "Это правило относится к другому городу. Править его может руководитель без привязки к городу."
)
TEXT_STALE = (
    "Пока вы правили, кто-то поменял это правило — в боте, в другом окне или другой менеджер. "
    "Выберите, что оставить."
)
EMPTY_TEXT = (
    "Правило само отклоняет заявки, которые вам точно не подходят, — например, школьников "
    "или заявки без нужного курса. Начните с готовой заготовки: правило создаётся выключенным, "
    "пока вы его не включите."
)
HINT = "Журнал отклонённых и возврат на модерацию — в боте: Заявки → 🤖 Автоотказы"
COMMON_GREY_TEXT = "Для всех городов. Править может только руководитель без привязки к городу."

_ACTIONS = {"reject", "flag"}
_STATUS_RANK = {"paused": 0, "on": 1, "off": 2}
_EMPTY_TRACKS = "Выберите хотя бы один тип заявки."
_BAD_ACTION = "Неизвестное действие правила."
_NEED_DRAFT = "Пришлите заготовку или черновик правила."


class CondIn(BaseModel):
    step: str
    op: str
    values: list[str] = []


class RuleIn(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    city: str | None = None
    tracks: list[str] = []
    conditions: list[list[CondIn]] = []
    action: str = "reject"
    reject_text: str | None = Field(default=None, max_length=1000)
    base_rev: str | None = None


class CreateIn(BaseModel):
    draft: RuleIn | None = None
    preset: int | None = None
    city: str | None = None


class EnableIn(BaseModel):
    confirm: bool = False
    base_rev: str | None = None


class RevIn(BaseModel):
    base_rev: str | None = None


class CopyIn(BaseModel):
    city: str | None = None


class MasterIn(BaseModel):
    enabled: bool
    confirm: bool = False


def _blank(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _as_draft(body: RuleIn) -> dict:
    tracks = [track.strip() for track in body.tracks if (track or "").strip()]
    if not tracks:
        raise HTTPException(400, {"reason": "invalid", "text": _EMPTY_TRACKS})
    action = (body.action or "").strip()
    if action not in _ACTIONS:
        raise HTTPException(400, {"reason": "invalid", "text": _BAD_ACTION})
    return {
        "name": _blank(body.name),
        "city": _blank(body.city),
        "tracks": tracks,
        "conditions": [[cond.model_dump() for cond in group] for group in body.conditions],
        "action": action,
        "reject_text": _blank(body.reject_text),
    }


def _raise_for(result) -> None:
    if result.not_found:
        raise HTTPException(404, {"reason": "not_found", "text": TEXT_NOT_FOUND})
    if result.forbidden:
        raise HTTPException(403, {"reason": "out_of_scope", "text": TEXT_OUT_OF_SCOPE})
    if result.stale:
        raise HTTPException(409, {"reason": "stale", "text": TEXT_STALE, "rule": result.fresh})
    if result.error:
        raise HTTPException(400, {"reason": "invalid", "text": result.error})


def _ok(result, *, rule_id: int, admin_id: int, action: str) -> dict:
    _raise_for(result)
    logger.info("admin=%s rule_id=%s %s", admin_id, rule_id, action)
    return {"ok": True, "rule": result.view}


@router.get("/app/api/admin/reject-rules")
async def list_rules(
    city: str | None = None,
    offset: int = 0,
    limit: int = 100,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    state = await reject_rules_editor.master_state(p.telegram_id)
    items = []
    for row in await rules_for_admin(p.telegram_id):
        view = await reject_rules_editor.rule_view(p.telegram_id, row)
        if view.get("city") is None and not view.get("can_edit"):
            view["locked_text"] = COMMON_GREY_TEXT
        items.append(view)
    if city and state["can_toggle"]:
        wanted = city.strip()
        items = [item for item in items if item.get("city") == wanted]
    items.sort(key=lambda item: (
        _STATUS_RANK.get(item.get("status"), 9),
        (item.get("name") or "").casefold(),
        item.get("id") or 0,
    ))
    paused_count = sum(1 for item in items if item.get("status") == "paused")
    total = len(items)
    offset = max(offset, 0)
    limit = max(limit, 0)
    cities = []
    if state["can_toggle"]:
        for code in await per_city_visible_codes(p.telegram_id):
            cities.append({"code": code, "label": await city_label(code)})
    return {
        "items": items[offset:offset + limit],
        "total": total,
        "offset": offset,
        "limit": limit,
        "empty_text": EMPTY_TEXT,
        "hint": HINT,
        "master": {
            "enabled": state["enabled"],
            "can_toggle": state["can_toggle"],
            "status_text": state["status_text"],
        },
        "can_toggle_master": state["can_toggle"],
        "cities": cities,
        "paused_count": paused_count,
    }


@router.get("/app/api/admin/reject-rules/catalog")
async def catalog(
    city: str | None = None,
    tracks: str | None = None,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    chosen = [part.strip() for part in (tracks or "").split(",") if part.strip()] or ["full"]
    return await reject_rules_editor.catalog(p.telegram_id, _blank(city), chosen)


@router.get("/app/api/admin/reject-rules/{rule_id}")
async def get_rule(
    rule_id: int,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    rule = await reject_rules_editor.load_rule(rule_id)
    if rule is None:
        raise HTTPException(404, {"reason": "not_found", "text": TEXT_NOT_FOUND})
    visible = {row["id"] for row in await rules_for_admin(p.telegram_id)}
    if rule_id not in visible:
        raise HTTPException(403, {"reason": "out_of_scope", "text": TEXT_OUT_OF_SCOPE})
    return {"rule": await reject_rules_editor.rule_view(p.telegram_id, rule)}


@router.post("/app/api/admin/reject-rules")
async def create_rule(
    body: CreateIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    if body.preset is not None:
        result = await reject_rules_editor.create_from_preset(
            p.telegram_id, body.preset, _blank(body.city),
        )
    elif body.draft is not None:
        draft = _as_draft(body.draft)
        if body.city is not None:
            draft["city"] = _blank(body.city)
        result = await reject_rules_editor.create_rule(p.telegram_id, draft)
    else:
        raise HTTPException(400, {"reason": "invalid", "text": _NEED_DRAFT})
    _raise_for(result)
    logger.info("admin=%s rule_id=%s created", p.telegram_id, result.view["id"])
    return {"ok": True, "rule": result.view}


@router.put("/app/api/admin/reject-rules/{rule_id}")
async def save_rule(
    rule_id: int,
    body: RuleIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    result = await reject_rules_editor.save_draft(
        p.telegram_id, rule_id, _as_draft(body), body.base_rev,
    )
    return _ok(result, rule_id=rule_id, admin_id=p.telegram_id, action="saved")


@router.post("/app/api/admin/reject-rules/preview")
async def preview_rule(
    body: RuleIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    return await reject_rules_editor.preview(p.telegram_id, _as_draft(body))


@router.post("/app/api/admin/reject-rules/{rule_id}/enable")
async def enable_rule(
    rule_id: int,
    body: EnableIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    result = await reject_rules_editor.set_enabled(
        p.telegram_id, rule_id, True, confirm=body.confirm, base_rev=body.base_rev,
    )
    if result.needs_confirm:
        return {"needs_confirm": result.needs_confirm}
    return _ok(result, rule_id=rule_id, admin_id=p.telegram_id, action="enabled")


@router.post("/app/api/admin/reject-rules/{rule_id}/disable")
async def disable_rule(
    rule_id: int,
    body: RevIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    result = await reject_rules_editor.set_enabled(
        p.telegram_id, rule_id, False, confirm=False, base_rev=body.base_rev,
    )
    return _ok(result, rule_id=rule_id, admin_id=p.telegram_id, action="disabled")


@router.post("/app/api/admin/reject-rules/{rule_id}/copy")
async def copy_rule(
    rule_id: int,
    body: CopyIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    result = await reject_rules_editor.copy_to_city(
        p.telegram_id, rule_id, _blank(body.city),
    )
    _raise_for(result)
    logger.info(
        "admin=%s rule_id=%s copied_to=%s", p.telegram_id, rule_id, result.view["id"],
    )
    return {"ok": True, "rule": result.view}


@router.delete("/app/api/admin/reject-rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    confirm: bool = False,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    result = await reject_rules_editor.delete(
        p.telegram_id, rule_id, confirm=confirm, base_rev=None,
    )
    if result.needs_confirm:
        return {"needs_confirm": result.needs_confirm}
    _raise_for(result)
    logger.info("admin=%s rule_id=%s deleted", p.telegram_id, rule_id)
    return {"ok": True}


@router.post("/app/api/admin/reject-rules/master")
async def master_switch(
    body: MasterIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    state = await reject_rules_editor.master_state(p.telegram_id)
    if not state["can_toggle"]:
        raise HTTPException(403, {"reason": "out_of_scope", "text": state["status_text"]})
    if not body.enabled and not body.confirm:
        return {"needs_confirm": {
            "text": state["confirm_text"],
            "enabled_count": state["enabled_count"],
        }}
    value = "on" if body.enabled else "off"
    await set_setting("reject_rules_enabled", value)
    logger.info(f"admin={p.telegram_id} reject_rules_enabled -> {value}")
    return {"ok": True, "enabled": body.enabled}
