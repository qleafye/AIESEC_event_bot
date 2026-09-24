"""D-29 (FORUM-CHECKIN.md, «Решения владельца 24.09») — плитка делегата «📅 Программа» в
Mini App: `GET /app/api/program`.

Гейт — `delegate_gate` (тот же приём, что у `miniapp/routers/hub.py`/`coins.py`): у программы
нет своего раздела-чекбокса в `SECTIONS` — видимость плитки решает ДАННЫЕ (есть фото или хотя
бы одна сессия), не тумблер, тем же способом, что кнопка меню бота
(`keyboards.builders.get_main_menu_kb`, `services.program.has_program_content`).

Вид (таблица/фото) и сам город делегата резолвятся ОДИН раз общими функциями
`services/program.py` — вторая копия правила «что показываем» не заводится нигде (докстринг
модуля `services/program.py`, D-29). Фото отдаётся ссылкой на `GET /app/api/file/{file_id}`
(тот же прокси, что у любой другой картинки Mini App, T-19-19: клиент никогда не видит токен
бота); сам file_id должен быть в `is_public_asset` (`miniapp/routers/files.py`) — это
публичное оформление события, не персональные данные, тот же класс, что лого/обложка."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from cities import cities_module_on, normalize_city
from database.db import get_user
from services.program import build_delegate_program, resolve_program_photo, resolve_program_view
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate

router = APIRouter()


async def _delegate_city(p: Principal) -> str | None:
    """Тот же fail-soft приём, что `miniapp.routers.faq._delegate_city` — город из
    `users.event_city` делегата (не привязка сотрудника, `Principal.city` у делегата всегда
    `None`); ошибка чтения не роняет экран, только сужает до общего (module-off) вида."""
    try:
        if not await cities_module_on():
            return None
        user = await get_user(p.telegram_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception:
        return None


@router.get("/app/api/program")
async def program_screen(p: Principal = Depends(delegate_gate)) -> dict:
    city = await _delegate_city(p)
    view = await resolve_program_view(city)

    if view == "photo":
        file_id = await resolve_program_photo(city)
        return {
            "view": "photo",
            "photo_url": f"/app/api/file/{file_id}" if file_id else None,
            "days": [],
            "empty_text": None if file_id else await get_setting_typed("program_empty_text"),
        }

    days = await build_delegate_program(city)
    return {
        "view": "table",
        "photo_url": None,
        "days": days,
        "empty_text": None if days else await get_setting_typed("program_empty_text"),
    }
