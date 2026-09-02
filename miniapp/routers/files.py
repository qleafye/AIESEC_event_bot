"""Phase 19 (D-03, RESEARCH Pattern 4), расширено 19.1-02 (D-08/D-15/D-16):
`GET /app/api/file/{file_id}` — прокси getFile.

Прямая ссылка на файл Telegram содержит токен бота (`/file/bot<token>/<file_path>`),
поэтому наружу отдаётся только этот маршрут: сервер сам делает getFile, скачивает и
отдаёт байты потоком. `file_path` и URL с токеном клиенту не возвращаются и не логируются
(T-19-19).

Доступ (T-19-20, IDOR) — allow-list, а не «всё, что знает бот» (у бота есть и чеки, и
резюме делегатов — их `file_id` менеджер геймы видеть не должен):
  - обложка неархивного задания или лого приложения (`miniapp_logo`) — любому принципалу;
  - любой ассет оформления из `web_theme.ASSET_KEYS` (лого/обложка тёмной темы, 4 стикера,
    иконка монеты, T-19.1-06) — любому принципалу: это графика мероприятия, а не персональные
    данные, менеджер загружает её осознанно как публичное оформление (accept, threat_model);
  - владельцу сдачи, в частях которой встречается `file_id`;
  - держателю `moderate_game` — в пределах городского скоупа сдачи (тот же критерий, что
    `_submission_out_of_scope` в боте: модуль городов выключен или привязки нет -> всё;
    иначе город делегата должен совпадать с привязкой менеджера).
Иначе 403; неизвестный `file_id` — тоже 403 (ни одной сдачи/обложки с ним нет) — расширение
allow-list не ослабляет это правило: список конечен и явен, а не «любой file_id из настроек»
регэкспом.
Недоступный upstream — 404, не 500: картинка в приложении просто не покажется.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import reg_engine
from cities import cities_module_on, normalize_city
from database.db import find_submissions_by_file_id, get_setting, get_user, is_active_task_cover
from settings_schema import get_setting_typed

import web_theme
from miniapp import telegram_api
from miniapp.deps import Principal, principal
from miniapp.telegram_api import TelegramApiError

router = APIRouter()

FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
CACHE_CONTROL = "private, max-age=3000"
_SAFE_EXT = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


async def _manager_in_scope(p: Principal, submissions: list[dict]) -> bool:
    """`moderate_game` видит файл, если хотя бы одна сдача с ним — в его городском скоупе."""
    if "moderate_game" not in p.caps or not submissions:
        return False
    if not await cities_module_on() or p.city is None:
        return True
    scope = normalize_city(p.city)
    for sub in submissions:
        user = await get_user(sub["user_id"])
        if normalize_city((user or {}).get("event_city")) == scope:
            return True
    return False


async def can_read_file(p: Principal, file_id: str) -> bool:
    if await is_active_task_cover(file_id):
        return True
    if (await get_setting_typed("miniapp_logo") or "") == file_id:
        return True
    for key in web_theme.ASSET_KEYS.values():
        if (await get_setting_typed(key) or "") == file_id:
            return True
    # PDF согласий (`consent_pdf_{key}`, не ключ SETTINGS_SCHEMA): публичный документ анкеты,
    # который делегат обязан прочитать до подписи — открыт любому принципалу, как логотип.
    for _label, consent_key in await reg_engine.consent_entries():
        if (await get_setting(f"consent_pdf_{consent_key}") or "") == file_id:
            return True
    submissions = await find_submissions_by_file_id(file_id)
    if any(sub["user_id"] == p.telegram_id for sub in submissions):
        return True
    return await _manager_in_scope(p, submissions)


def _download_name(file_id: str, file_path: str | None) -> str:
    """Имя для Content-Disposition: префикс file_id + только расширение из file_path —
    сам путь наружу не уходит."""
    ext = PurePosixPath(file_path or "").suffix
    if not _SAFE_EXT.match(ext):
        ext = ""
    return f"{file_id[:16]}{ext}"


@router.get("/app/api/file/{file_id}")
async def proxy_file(file_id: str, request: Request, p: Principal = Depends(principal)):
    if not FILE_ID_RE.match(file_id):
        raise HTTPException(404, {"reason": "not_found"})
    if not await can_read_file(p, file_id):
        raise HTTPException(403, {"reason": "forbidden"})

    cfg = request.app.state.cfg
    try:
        info = await telegram_api.get_file(cfg, file_id)
        file_path = info.get("file_path")
        if not file_path:
            raise TelegramApiError("not_found")
        stream = await telegram_api.download_file(cfg, file_path)
    except TelegramApiError:
        raise HTTPException(404, {"reason": "not_found"})

    headers = {
        "Content-Disposition": f'inline; filename="{_download_name(file_id, file_path)}"',
        "Cache-Control": CACHE_CONTROL,
        "X-Content-Type-Options": "nosniff",
    }
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(stream.chunks(), media_type=stream.content_type, headers=headers)
