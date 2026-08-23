"""Phase 19 Plan 01: служебные маршруты — `/app/health` и `/app/api/me`.

`/app/health` открыт и не трогает БД (HEALTHCHECK контейнера) — единственный маршрут,
живущий и при `miniapp_enabled = off` (гасящий middleware — в `miniapp.main`).

`/app/api/me` — первый запрос фронта: кто я, какие разделы включены, оформление. Оболочка
по нему решает, какие экраны рисовать (D-06 чекбоксы) и как себя вести без прав.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from dashboard.db import read_conn

from miniapp.deps import SECTIONS, Principal, delegate_denial, principal, read_setting

router = APIRouter()


@router.get("/app/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/app/api/me")
def me(request: Request, p: Principal = Depends(principal)) -> dict:
    cfg = request.app.state.cfg
    with read_conn(cfg.db_path) as conn:
        sections = {s: read_setting(conn, f"miniapp_section_{s}") == "on" for s in SECTIONS}
        accent = read_setting(conn, "miniapp_accent")
        event_name = read_setting(conn, "event_name")
        logo_file_id = read_setting(conn, "miniapp_logo")
        is_delegate = delegate_denial(conn, p) is None
    return {
        "telegram_id": p.telegram_id,
        "via": p.via,
        "caps": sorted(p.caps),
        "city": p.city,
        "is_delegate": is_delegate,
        "is_staff": p.is_staff,
        "sections": sections,
        "accent": accent,
        "event_name": event_name,
        "logo_file_id": logo_file_id,
        "bot_username": cfg.bot_username,
    }
