"""Phase 33 (delegate-card admin actions, задача 3): «📎 Заменить резюме» — менеджер шлёт файл
(PDF/DOCX) взамен резюме делегата. Кнопка на карточке `/find`
(`handlers/admin_resume_replace.py`), сама замена — здесь.

Nextcloud-загрузка (`services/nextcloud.py::upload_resume`) — тем же путём, что при обычной
подаче анкеты; `_resume_file_stem`/`_resume_filename_mode` переиспользуем из
`reg_finalize.py` (владелец файла — не владеем им, только вызываем, тот же приём, что
`admin_city_move.py`/`admin_revert_pending.py` берут `_city_allowed` из `admin_checkin.py`).

Ревью part2 (26.09): запись ячейки листа «Резюме (ссылка)» НЕ идёт через общий
`reg_finalize._apply_resume_url`/`services.sheets.update_row_by_id` — тот при промахе по строке
на ИМЕНОВАННОЙ вкладке откатывается на ГЛАВНЫЙ лист (`_update_row_by_id_sync`'s fallback) и
может переписать чужую строку чужими колонками (памятка `standalone-script-sheet-traps`,
находка 21.09: `update_row_by_id` откатывается на главный лист при промахе). Для админской
замены резюме это неприемлемо — своя функция `_write_resume_cell_safely` ниже: резолвит
вкладку (`reg_finalize._resolve_update_tab`, тот же маршрут), проверяет `services.sheets.
find_rows_by_id` (НИКОГДА не создаёт вкладку) и пишет `update_row_by_id` ТОЛЬКО когда строка
на этой вкладке найдена РОВНО один раз — иначе отчёт называет причину словами, лист не трогаем.
`_apply_resume_url` самой финализации анкеты (Mini App догрузка/джоба повтора/очистка
дропзоны) этим НЕ затронут — координатор рассматривает его отдельно.

`users.resume_file_id` обновляется узким UPDATE ДО похода в облако — то, что делегат получит
при следующем recall (`services/reg_finalize.py` RESUME_RECALL_COLUMNS), меняется на новый
файл сразу, даже если сама выгрузка в Nextcloud временно недоступна. Сбой облака НЕ рвёт саму
замену `file_id` (fail-soft, тот же приём, что `post_finalize`/`handle_resume_upload`) —
`report["cloud_error"]`/`report["sheet_error"]` несут причину, экран подтверждения печатает
её как «ссылка в таблице не обновлена: …», а не молчит."""
from __future__ import annotations

import asyncio
import logging
import os

from database.db import get_user, record_answer_history, update_user_answers
from reg_engine import RESUME_MAX_BYTES, is_allowed_resume, resume_too_large
from services.reg_finalize import _resolve_update_tab, _resume_file_stem, _resume_filename_mode
import services.sheets as sheets_service

logger = logging.getLogger(__name__)

_SHEET_RESULT_TEXT = {
    None: "таблица недоступна или вкладки нет",
    "empty": "строка делегата на вкладке не найдена",
    "duplicate": "на вкладке несколько строк делегата",
}


async def _write_resume_cell_safely(telegram_id: int, full: dict, url: str | None) -> str | None:
    """Пишет ячейку «Резюме (ссылка)» ТОЛЬКО когда строка делегата на резолвленной вкладке
    найдена ровно один раз (`find_rows_by_id` — no-create контракт). Возвращает `None` на
    успехе или человекочитаемую причину отказа — лист в этом случае НЕ трогаем вовсе (ни
    создания вкладки, ни отката на главный лист)."""
    from handlers.registration import _sheet_dispatch
    from handlers.reg_schema import sheet_city_code

    tab = await _resolve_update_tab(full.get("event_city"), full.get("participant_type"))
    rows = await sheets_service.find_rows_by_id(tab, telegram_id)
    if rows is None:
        return _SHEET_RESULT_TEXT[None]
    if not rows:
        return _SHEET_RESULT_TEXT["empty"]
    if len(rows) > 1:
        return _SHEET_RESULT_TEXT["duplicate"]

    row_fn, _append_fn = _sheet_dispatch(full.get("participant_type"))
    city = await sheet_city_code(full.get("event_city"))
    row = await row_fn({**full, "resume_url": url}, city)
    ok = await sheets_service.update_row_by_id(tab, telegram_id, row)
    return None if ok else "запись в таблицу не удалась"

_NOT_ALLOWED_TEXT = "Принимаются только PDF или DOCX. Пришлите файл ещё раз."
_TOO_LARGE_TEXT = f"❌ Файл слишком большой (максимум {RESUME_MAX_BYTES // (1024 * 1024)} МБ). Пришлите файл меньшего размера."


def validate_resume_document(file_name: str | None, file_size) -> str | None:
    """`None` — файл принимается; иначе текст отказа менеджеру (та же пара проверок, что
    `handlers/reg_flow.py::process_resume` у делегата, литералами — не через реестр, здесь
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
        "sheet_updated": False, "new_resume_url": None, "cloud_error": None, "sheet_error": None,
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
                await update_user_answers(telegram_id, {"resume_url": url}, allowed_columns=["resume_url"])
                report["new_resume_url"] = url
                sheet_error = await _write_resume_cell_safely(telegram_id, {**full, "resume_url": url}, url)
                if sheet_error is None:
                    report["sheet_updated"] = True
                else:
                    report["sheet_error"] = sheet_error
            else:
                report["cloud_error"] = "загрузка в облако не удалась"
    except Exception as e:
        logger.warning(f"replace_resume: nextcloud upload failed for {telegram_id}: {e}")
        report["cloud_error"] = str(e)

    return report
