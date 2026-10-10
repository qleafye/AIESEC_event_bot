"""D-29 (FORUM-CHECKIN.md, «Решения владельца 24.09») — плитка делегата «📅 Программа» в
Mini App: `GET /app/api/program`.

Гейт — `delegate_gate` (тот же приём, что у `miniapp/routers/hub.py`/`coins.py`): у программы
нет своего раздела-чекбокса в `SECTIONS` — видимость плитки и доступ к ручке решает та же
пара проверок, что у кнопки меню бота (`keyboards.builders.get_main_menu_kb`): тумблер
`menu_program` по городу делегата + есть фото или хотя бы одна сессия
(`services.program.program_menu_visible`). В `/app/api/me` это `sections["program"]`.

Вид (таблица/фото) и сам город делегата резолвятся ОДИН раз общими функциями
`services/program.py` — вторая копия правила «что показываем» не заводится нигде (докстринг
модуля `services/program.py`, D-29). Фото отдаётся ссылкой на `GET /app/api/file/{file_id}`
(тот же прокси, что у любой другой картинки Mini App, T-19-19: клиент никогда не видит токен
бота); сам file_id должен быть в `is_public_asset` (`miniapp/routers/files.py`) — это
публичное оформление события, не персональные данные, тот же класс, что лого/обложка."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from domain.cities import cities_module_on, normalize_city
from database.db import get_user
from services.i18n import i18n
from services.program import (
    build_delegate_program,
    default_program_photo_path,
    program_menu_visible,
    resolve_program_content,
)

from miniapp.deps import Principal, delegate_gate

router = APIRouter()
logger = logging.getLogger(__name__)


async def _delegate_city(p: Principal) -> str | None:
    """Город из `users.event_city` делегата (не привязка сотрудника, `Principal.city` у
    делегата всегда `None`); `None` — модуль городов выключен. Ошибка чтения НЕ глотается:
    в отличие от FAQ, «общий» вид здесь — расписание чужого города, лучше честная ошибка
    с повтором, чем не та программа (ручка отвечает 503 `retry`)."""
    if not await cities_module_on():
        return None
    user = await get_user(p.telegram_id)
    return normalize_city(user.get("event_city") if user else None)


_RETRY_TEXT = "Не удалось загрузить программу. Обновите экран через минуту."


async def _retry_error(telegram_id: int) -> HTTPException:
    """503 `retry` с человеческим текстом (`ui.js::errorText` читает `payload.text`).
    Язык — best effort: БД только что отказала, её же сбой здесь не должен ронять ответ."""
    text = _RETRY_TEXT
    try:
        lang, tr_map = await i18n.context(telegram_id)
        text = i18n.tr(_RETRY_TEXT, lang if lang in ("ru", "en") else "ru", tr_map)
    except Exception:
        pass
    return HTTPException(503, {"reason": "retry", "text": text})


async def program_section_visible(p: Principal) -> bool:
    """Раздел «📅 Программа» Mini App — вычисляемый, без своего чекбокса `miniapp_section_*`:
    виден ровно тогда, когда делегату в чате видна кнопка программы
    (`services.program.program_menu_visible` — тумблер `menu_program` по городу + есть фото или
    сессии). Одобренность делегата сюда не входит — её проверяет `delegate_gate`/`is_delegate`.
    Сбой чтения — «раздела нет», экран не роняется."""
    try:
        return await program_menu_visible(await _delegate_city(p))
    except Exception:
        logger.error("program_section_visible: сбой чтения, раздел скрыт", exc_info=True)
        return False


def _texts(lang: str, tr_map: dict) -> dict:
    """Подписи экрана — те же литералы, что у текстового вида программы в чате
    (`handlers/forum/program.py`, корпус `services/i18n/i18n_sources.py` «lit:program.*»): перевод уже
    есть, второй словарь не заводим."""
    def tr(text: str) -> str:
        return i18n.tr(text, lang, tr_map)

    return {
        "now": f"🔴 {tr('Идёт сейчас')}",
        "next": f"⏭ {tr('Следующая')}",
        "hall": tr("Зал:"),
        "speaker": tr("Спикер:"),
        "parallel": tr("параллельно"),
    }


@router.get("/app/api/program")
async def program_screen(p: Principal = Depends(delegate_gate)) -> dict:
    # Гейт — тот же, что у кнопки в чате: делегат одобрен (delegate_gate выше) И кнопка
    # программы ему видна (тумблер + есть что показать). Иначе 403 — как у выключенного
    # раздела (`require_section`), ядро app.js рисует свой экран «нет доступа».
    try:
        city = await _delegate_city(p)
        visible = await program_menu_visible(city)
    except Exception:
        logger.error("program_screen: сбой чтения города/гейта", exc_info=True)
        raise await _retry_error(p.telegram_id)
    if not visible:
        raise HTTPException(403, {"reason": "section_off", "section": "program"})
    view, photo = await resolve_program_content(city)
    lang, tr_map = await i18n.context(p.telegram_id)
    lang = lang if lang in ("ru", "en") else "ru"
    texts = _texts(lang, tr_map)

    if view == "photo":
        # Фото из настроек — через прокси getFile (как лого); файл на диске — своей публичной
        # ручкой ниже. Адреса Telegram и токена бота фронт не видит ни в одном случае.
        photo_url = (
            f"/app/api/file/{photo['file_id']}" if photo.get("file_id") else DEFAULT_PHOTO_URL
        )
        return {"view": "photo", "lang": lang, "texts": texts, "photo_url": photo_url,
                "days": [], "empty_text": None}

    days = await build_delegate_program(city) if view == "table" else []
    return {
        "view": "table",
        "lang": lang,
        "texts": texts,
        "photo_url": None,
        "days": days,
        "empty_text": None if days else await i18n.tr_setting("program_empty_text", lang, tr_map),
    }


DEFAULT_PHOTO_URL = "/app/api/program/photo-default"


@router.get(DEFAULT_PHOTO_URL)
async def program_default_photo() -> FileResponse:
    """Диск-фоллбэк `resources/program.jpg` — тот же файл, что чат шлёт, когда фото в
    настройках нет. Публично, без принципала (тег <img> не шлёт initData): это оформление
    события, как лого, и file_id здесь нет вовсе. Нет файла — 404."""
    path = default_program_photo_path()
    if not path:
        raise HTTPException(404, {"reason": "not_found"})
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=300"})
