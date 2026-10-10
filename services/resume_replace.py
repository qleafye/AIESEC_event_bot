"""Phase 33 (delegate-card admin actions, задача 3): «📎 Заменить резюме» — менеджер шлёт файл
(PDF/DOCX) взамен резюме делегата. Кнопка на карточке `/find`
(`handlers/applications/admin_resume_replace.py`), сама замена — здесь.

Тем же путём, что при обычной подаче анкеты (`services/reg_finalize.py`): Nextcloud-загрузка
(`services/nextcloud.py::upload_resume`) + обновление ячейки листа «Резюме (ссылка)» той же
`update_row_by_id`. Переиспользуем `_apply_resume_url`/`_resume_file_stem`/
`_resume_filename_mode` — владелец файла `reg_finalize.py`, не владеем им, только вызываем
(тот же приём, что `admin_city_move.py`/`admin_revert_pending.py` берут `_city_allowed` из
`admin_checkin.py`).

`users.resume_file_id` обновляется узким UPDATE ДО похода в облако — то, что делегат получит
при следующем recall (`services/reg_finalize.py` RESUME_RECALL_COLUMNS), меняется на новый
файл сразу, даже если сама выгрузка в Nextcloud временно недоступна. Сбой облака НЕ рвёт саму
замену `file_id` (fail-soft, тот же приём, что `post_finalize`/`handle_resume_upload`) —
`report["cloud_error"]` несёт причину, экран подтверждения печатает её как «ссылка в таблице
не обновлена: …», а не молчит."""
from __future__ import annotations

import asyncio
import logging
import os

from database.db import get_user, record_answer_history, update_user_answers
from domain.regform.engine import RESUME_MAX_BYTES, is_allowed_resume, resume_too_large
from services.reg_finalize import _apply_resume_url, _resume_file_stem, _resume_filename_mode

logger = logging.getLogger(__name__)

_NOT_ALLOWED_TEXT = "Принимаются только PDF или DOCX. Пришлите файл ещё раз."
_TOO_LARGE_TEXT = f"❌ Файл слишком большой (максимум {RESUME_MAX_BYTES // (1024 * 1024)} МБ). Пришлите файл меньшего размера."


def validate_resume_document(file_name: str | None, file_size) -> str | None:
    """`None` — файл принимается; иначе текст отказа менеджеру (та же пара проверок, что
    `handlers/reg/reg_flow.py::process_resume` у делегата, литералами — не через реестр, здесь
    менеджерский экран)."""
    if not is_allowed_resume(file_name):
        return _NOT_ALLOWED_TEXT
    if resume_too_large(file_size):
        return _TOO_LARGE_TEXT
    return None


async def preview_resume_replace(telegram_id: int) -> dict | None:
    """Только чтение — экран подтверждения/запроса файла. `None` — делегата нет."""
    user = await get_user(telegram_id)
    if user is None:
        return None
    return {
        "ok": True,
        "old_resume_url": user.get("resume_url"),
        "had_old_file": bool(user.get("resume_file_id")),
    }


async def replace_resume(
    bot, telegram_id: int, file_id: str, file_name: str | None, *, by_admin: int,
) -> dict:
    """Фактическая замена. `report["ok"]=False` + `report["error"]` — делегата нет (карточка
    устарела) — ничего не трогаем."""
    user = await get_user(telegram_id)
    if user is None:
        return {"ok": False, "error": "Делегат не найден — возможно, карточка устарела."}

    old_file_id = user.get("resume_file_id")
    old_url = user.get("resume_url")

    await update_user_answers(
        telegram_id, {"resume_file_id": file_id}, allowed_columns=["resume_file_id"],
    )
    try:
        await record_answer_history(
            telegram_id,
            [{"column": "resume_file_id", "old": bool(old_file_id), "new": True}],
            source=f"admin:{by_admin}",
        )
    except Exception as e:
        logger.warning(f"replace_resume: record_answer_history({telegram_id}) failed: {e}")

    report: dict = {
        "ok": True, "error": None,
        "old_resume_url": old_url, "had_old_file": bool(old_file_id),
        "sheet_updated": False, "new_resume_url": None, "cloud_error": None,
    }

    try:
        from services.nextcloud import is_configured, upload_resume

        if not is_configured():
            report["cloud_error"] = "модуль Nextcloud не настроен"
        else:
            stem_mode = await _resume_filename_mode()
            full = await get_user(telegram_id) or {}
            stem = _resume_file_stem(full, telegram_id, mode=stem_mode)
            ext = os.path.splitext(file_name or "")[1]
            url = await asyncio.wait_for(upload_resume(bot, file_id, f"{stem}{ext}"), timeout=20)
            if url:
                await _apply_resume_url(telegram_id, full, url)
                report["sheet_updated"] = True
                report["new_resume_url"] = url
            else:
                report["cloud_error"] = "загрузка в облако не удалась"
    except Exception as e:
        logger.warning(f"replace_resume: nextcloud upload failed for {telegram_id}: {e}")
        report["cloud_error"] = str(e)

    return report
