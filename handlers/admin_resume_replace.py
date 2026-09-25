"""Phase 33 (delegate-card admin actions, задача 3): «📎 Заменить резюме» — кнопка на карточке
`/find` (`handlers/admin.py::cmd_find_user`), сама замена — `services/resume_replace.py`.

Не форумная функция (админ-действие модератора) — своей строки в хабе «🎪 Форум: функции» нет
и не нужно, тот же посыл, что у соседних швов фазы.

Шов той же формы, что соседние: своего `Router()` нет, хендлеры декорируют ОБЩИЙ
`admin.router`, модуль импортируется ХВОСТОМ `handlers/admin.py` (golden snapshot: чистая
вставка). `_city_allowed` — импорт из `handlers/admin_checkin.py` (не владеем файлом, только
вызываем).

В отличие от соседних карточных действий фазы (city_move/revert_pending/resubmit_grant/
edit_grant/reg_reset — экран подтверждения ЗАКАНЧИВАЕТСЯ тапом кнопки), здесь подтверждение
ОТКРЫВАЕТ ожидание файла: `resumerep_start` сразу переводит менеджера в FSM-состояние
`ResumeReplace.waiting_for_file` (telegram_id делегата несёт `state.get_data()`, тот же приём,
что у `StaffAdd.waiting_for_person`, `handlers/admin_roles.py`) — сама замена происходит в
приёмном хендлере документа, не в callback."""
import html as html_module
import logging

from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import normalize_city
from database.db import get_user
from handlers.admin import router
from handlers.admin_checkin import _city_allowed
from handlers.states import ResumeReplace
from services.resume_replace import preview_resume_replace, replace_resume, validate_resume_document

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN_ALERT = "Этот город вне вашей зоны ответственности."
_NOT_FOUND_ALERT = "Делегат не найден — возможно, карточка устарела."


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.lstrip("-").isdigit() else None


def _old_resume_line(preview: dict) -> str:
    if preview.get("had_old_file"):
        url = preview.get("old_resume_url")
        if url:
            return f"Текущее резюме: файл есть, ссылка в таблице — {html_module.escape(str(url))}"
        return "Текущее резюме: файл есть, ссылки в таблице пока нет."
    return "Текущее резюме: нет — делегат ещё не присылал файл."


@router.callback_query(F.data.startswith("resumerep_start:"))
async def resumerep_start(callback: types.CallbackQuery, state: FSMContext):
    tid = _parse_tid(callback.data.split(":", 1)[1])
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    preview = await preview_resume_replace(tid)
    if preview is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return

    name = html_module.escape(str(user.get("full_name") or tid))
    lines = [
        "📎 <b>Заменить резюме</b>\n",
        f"{name}",
        _old_resume_line(preview),
        "",
        "Пришлите новый файл резюме (PDF или DOCX) одним сообщением — заменю сразу, как "
        "получу файл. Передумали — /cancel.",
    ]
    await state.set_state(ResumeReplace.waiting_for_file)
    await state.update_data(resumerep_tid=tid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"resumerep_cancel:{tid}")],
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("resumerep_cancel:"))
async def resumerep_cancel(callback: types.CallbackQuery, state: FSMContext):
    if await state.get_state() == ResumeReplace.waiting_for_file.state:
        await state.clear()
    await callback.message.edit_text("✖️ Замена резюме отменена.")
    await callback.answer()


@router.message(ResumeReplace.waiting_for_file, F.text.in_({"Отмена", "/cancel"}))
async def resumerep_cancel_text(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("✖️ Замена резюме отменена.")


@router.message(ResumeReplace.waiting_for_file, F.document)
async def resumerep_receive_file(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    tid = data.get("resumerep_tid")
    if tid is None:
        await state.clear()
        await message.answer("Что-то пошло не так — начните заново с карточки /find.")
        return

    user = await get_user(tid)
    if user is None:
        await state.clear()
        await message.answer(_NOT_FOUND_ALERT)
        return
    if not await _city_allowed(message.from_user.id, normalize_city(user.get("event_city"))):
        await state.clear()
        await message.answer(_CITY_FORBIDDEN_ALERT)
        return

    doc = message.document
    error_text = validate_resume_document(doc.file_name, doc.file_size)
    if error_text:
        await message.answer(error_text)
        return  # состояние остаётся — менеджер присылает файл ещё раз или /cancel

    await state.clear()
    report = await replace_resume(
        bot, tid, doc.file_id, doc.file_name, by_admin=message.from_user.id,
    )
    if not report.get("ok"):
        await message.answer(f"❌ Не заменил(а): {report.get('error') or '-'}")
        return

    name = html_module.escape(str(user.get("full_name") or tid))
    lines = [f"✅ Резюме делегата <b>{name}</b> заменено."]
    if report.get("sheet_updated"):
        lines.append("Ссылка в таблице обновлена.")
    elif report.get("cloud_error"):
        lines.append(f"⚠️ Ссылка в таблице не обновлена: {html_module.escape(str(report['cloud_error']))}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(ResumeReplace.waiting_for_file)
async def resumerep_receive_other(message: types.Message):
    """Что угодно, кроме документа и /cancel, в этом состоянии — напоминание, не тихий
    проигрыш (правило продукта: ошибка объясняет, что сделать)."""
    await message.answer("Пришлите файл резюме (PDF или DOCX) одним сообщением — или /cancel.")
