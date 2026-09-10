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
  - держателю `settings` — `file_id`, который прямо сейчас является значением одного из
    photo/file-ключей реестра (`settings_ops.file_setting_keys`, Phase 22 T-22-12): превью
    обложки программы/спикеров/старта в веб-настройках, без права на любой файл бота;
  - владельцу сдачи, в частях которой встречается `file_id`;
  - держателю `moderate_game` — в пределах городского скоупа сдачи (тот же критерий, что
    `_submission_out_of_scope` в боте: модуль городов выключен или привязки нет -> всё;
    иначе город делегата должен совпадать с привязкой менеджера);
  - владельцу — его собственный аватар (`users.avatar_file_id`, UAT 07.09, T-d6t-01): его же
    резюме и чек этой веткой НЕ открываются — сверяется ИМЕННО колонка аватара, тот же
    принцип, что у ветки `moderate_reg` ниже;
  - держателю `moderate_reg` (Phase 23, 23-03, D-02) — `avatar_file_id` делегата в пределах
    того же городского скоупа, что и очередь заявок; резюме, чек и любой другой `file_id`
    того же делегата этой веткой НЕ открываются — сверяется ИМЕННО колонка `avatar_file_id`
    обратным поиском по ней (не любой файл известного пользователя).
Иначе 403; неизвестный `file_id` — тоже 403 (ни одной сдачи/обложки с ним нет) — расширение
allow-list не ослабляет это правило: список конечен и явен, а не «любой file_id из настроек»
регэкспом.
Недоступный upstream — 404, не 500: картинка в приложении просто не покажется.

Quick 260910-w3j (IMG-01..06): у маршрута — ТРЕТЬЯ ветка аутентификации (`file_principal`),
и публичные ассеты (лого, оформление, PDF согласий — три ветки `can_read_file`, уже открытые
ЛЮБОМУ принципалу) отдаются АНОНИМНО, вовсе без принципала. Это не дыра: `is_public_asset`
перечисляет ровно тот же конечный список, что и раньше был открыт любому авторизованному —
требование аутентификации ничего не защищало на этих трёх ветках, а тег `<img>` физически не
может пройти существующую аутентификацию (нет ни заголовка, ни куки). Обложка активной задачи
(`is_active_task_cover`) НЕ публична — она остаётся за принципалом (initData/cookie/токен
`t`, см. `miniapp.file_tokens`), это контент задания, а не оформление приложения.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import reg_engine
import tg_media
from cities import cities_module_on, normalize_city
from dashboard.access import resolve_capabilities, staff_city
from dashboard.db import read_conn
from database.db import (
    find_submissions_by_file_id,
    find_user_by_avatar_file_id,
    get_setting,
    get_user,
    is_active_task_cover,
)
from settings_schema import get_setting_typed

import settings_ops
import web_theme
from miniapp import telegram_api
from miniapp.deps import Principal, principal
from miniapp.file_tokens import verify_file_token
from miniapp.telegram_api import TelegramApiError

router = APIRouter()

FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
CACHE_CONTROL = "private, max-age=3000"
_SAFE_EXT = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


async def _city_matches(p: Principal, user: dict | None) -> bool:
    """Единственная в файле реализация правила городского скоупа (D-14): модуль городов
    выключен или у менеджера нет привязки -> видит всех; иначе город делегата должен совпасть
    с привязкой менеджера. Переиспользуется обеими ветками allow-list — сдачи геймификации
    (`moderate_game`) и аватар заявок (`moderate_reg`) — вместо второй копии правила."""
    if not await cities_module_on() or p.city is None:
        return True
    return normalize_city((user or {}).get("event_city")) == normalize_city(p.city)


async def _manager_in_scope(p: Principal, submissions: list[dict]) -> bool:
    """`moderate_game` видит файл, если хотя бы одна сдача с ним — в его городском скоупе."""
    if "moderate_game" not in p.caps or not submissions:
        return False
    for sub in submissions:
        user = await get_user(sub["user_id"])
        if await _city_matches(p, user):
            return True
    return False


async def is_public_asset(file_id: str) -> bool:
    """Слой A (quick 260910-w3j): три ветки, уже открытые ЛЮБОМУ принципалу ДО этого квика —
    вынесены сюда, чтобы `proxy_file` мог отдать их вовсе без аутентификации (лого рендерится
    в шапке `app.html` сервером ДО загрузки JS, принципала в этот момент нет физически)."""
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
    return False


async def can_read_file(p: Principal, file_id: str) -> bool:
    if await is_public_asset(file_id):
        return True
    if await is_active_task_cover(file_id):
        return True
    # Phase 22 (22-04, T-22-12): держателю `settings` — только file_id, который прямо сейчас
    # является значением photo/file-ключа реестра (settings_ops.file_setting_keys), не любой.
    if "settings" in p.caps and await settings_ops.is_current_file_value(file_id):
        return True
    # Phase 23 (23-03, D-02) + UAT 07.09 (T-d6t-01): сверяется КОЛОНКА users.avatar_file_id,
    # а не «любой file_id известного пользователя» — резюме и чек того же делегата этой
    # веткой НЕ открываются. Обратный поиск по колонке — один раз на обе ветки:
    #   владельцу — его собственный аватар (равенство telegram_id принципалу);
    #   держателю moderate_reg — чужой аватар в его городском скоупе (прежнее поведение).
    avatar_owner = await find_user_by_avatar_file_id(file_id)
    if avatar_owner and avatar_owner["telegram_id"] == p.telegram_id:
        return True
    if avatar_owner and "moderate_reg" in p.caps and await _city_matches(p, avatar_owner):
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


def file_principal(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
    t: str | None = Query(default=None),
) -> Principal | None:
    """Третья ветка аутентификации маршрута файлов (Слой B, quick 260910-w3j). Заголовок
    initData ИЛИ живая cookie-сессия — как у остальных маршрутов, `principal` целиком (коды
    ошибок 401 `bad_initdata`/403 `csrf`/403 `staff_only` не меняются, требование 6). Иначе —
    короткоживущий токен `t` (`miniapp.file_tokens`): тег `<img>` не может послать заголовок и
    во встроенном браузере Телеграма нет куки. Права и город читаются из БД ТЕМ ЖЕ способом,
    что и `principal` — БЕЗ КАКОГО-ЛИБО КЭША (D-09/T-19-05): снятое право видно на следующем
    же запросе. `None` — ни одна ветка не сработала, маршрут ответит 401 `no_auth`."""
    if x_telegram_init_data or ("session" in request.scope and request.session.get("telegram_id")):
        return principal(request, x_telegram_init_data)
    if not t:
        return None
    cfg = request.app.state.cfg
    telegram_id = verify_file_token(t, cfg.bot_token)
    if telegram_id is None:
        return None
    with read_conn(cfg.db_path) as conn:
        caps = frozenset(resolve_capabilities(conn, telegram_id, cfg.admin_ids))
        city = staff_city(conn, telegram_id)
    return Principal(telegram_id=telegram_id, via="token", caps=caps, city=city)


@router.get("/app/api/file/{file_id}")
async def proxy_file(file_id: str, request: Request, p: Principal | None = Depends(file_principal)):
    if not FILE_ID_RE.match(file_id):
        raise HTTPException(404, {"reason": "not_found"})
    if p is None:
        if not await is_public_asset(file_id):
            raise HTTPException(401, {"reason": "no_auth"})
    elif not await can_read_file(p, file_id):
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
    media_type = tg_media.media_type_for(stream.content_type, file_path)
    return StreamingResponse(stream.chunks(), media_type=media_type, headers=headers)
