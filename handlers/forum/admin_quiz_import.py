"""Тест компетенций: загрузка вопросов из таблицы (CSV) — шаблон, файл, предпросмотр, замена.

Продолжение `admin_quiz.py` (его хвостовой импорт). Файл читается как данные: размер проверяется
до скачивания, формулы не исполняются, применение — одна транзакция после подтверждения."""
import html as html_module

from aiogram import Bot, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, ReplyKeyboardRemove

from database import quiz_db as qdb
from database import session_enroll_db as edb
from handlers.admin import router
from handlers.forum.admin_program import _CITY_FORBIDDEN_ALERT, _city_allowed
from handlers.forum.admin_quiz import _btn, deny, points_max, quiz_by_code, render_quiz
from handlers.states import QuizImport
from services.forum import quiz_import

_MAX_BYTES = 2 * 1024 * 1024
_NOT_FILE = "Пришлите файл CSV — тот, что получили по кнопке «📥 Шаблон»."


def _import_kb(code: str, *, can_apply: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_apply:
        rows.append([_btn("✅ Применить", f"prog_qzapply:{code}")])
    rows.append([_btn("← Отмена", f"prog_qzimpno:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("prog_qzimp:"))
async def prog_qzimp(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    top = await points_max(code)
    await state.set_state(QuizImport.waiting_file)
    await state.set_data({"city": code})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📥 Шаблон", f"prog_qztpl:{code}")], [_btn("← Отмена", f"prog_qzimpno:{code}")],
    ])
    await callback.message.edit_text(
        "📥 <b>Загрузить тест из таблицы</b>\n\n"
        "Пришлите файл CSV (до 2 МБ) с колонками: вопрос; вариант; компетенция; баллы. "
        f"Баллы — от 0 до {top}. Один вариант с несколькими компетенциями — несколько строк, "
        "пустая ячейка вопроса — продолжение прежнего вопроса.\n\n"
        "Сначала можно скачать шаблон: «📥 Шаблон». Перед заменой я покажу, что изменится.",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qztpl:"))
async def prog_qztpl(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    raw = await quiz_import.build_template(code)
    await callback.message.answer_document(
        BufferedInputFile(raw, filename="quiz_template.csv"),
        caption="Заполните и пришлите файлом.",
    )
    await callback.answer()


@router.message(StateFilter(QuizImport.waiting_file), Command("cancel"))
@router.message(StateFilter(QuizImport.waiting_file), F.text == "Отмена")
async def prog_qzimp_cancel_word(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Тест не изменился.", reply_markup=ReplyKeyboardRemove())


@router.message(QuizImport.waiting_file, F.document)
async def prog_qzimp_file(message: types.Message, state: FSMContext, bot: Bot):
    code = (await state.get_data()).get("city")
    if not code or not await _city_allowed(message.from_user.id, code):
        await state.clear()
        await message.answer(_CITY_FORBIDDEN_ALERT)
        return
    if (message.document.file_size or 0) > _MAX_BYTES:  # до скачивания
        await message.answer("Файл больше 2 МБ — в таблице не может быть столько вопросов. "
                             "Пришлите файл поменьше.")
        return
    buf = await bot.download(message.document.file_id)
    buf.seek(0)
    raw = buf.read(_MAX_BYTES + 1)  # размер в сообщении мог быть неизвестен — сверяем по факту
    if len(raw) > _MAX_BYTES:
        await message.answer("Файл больше 2 МБ — в таблице не может быть столько вопросов. "
                             "Пришлите файл поменьше.")
        return
    names = {c["name"].lower(): c["id"] for c in await edb.list_competencies(code)}
    parsed = quiz_import.parse_csv(raw, names, await points_max(code))
    quiz = await quiz_by_code(code)
    options_now = sum(len(v) for v in (await qdb.list_options_for_quiz(quiz["id"])).values())
    preview = quiz_import.preview_text(
        parsed, current_questions=await qdb.count_questions(quiz["id"]), current_options=options_now,
        open_attempts=await qdb.count_open_attempts(quiz["id"]),
    )
    if parsed.errors:
        await message.answer(
            html_module.escape(preview) + "\n\nИсправьте файл и пришлите его снова.",
            parse_mode="HTML", reply_markup=_import_kb(code, can_apply=False),
        )
        return
    await state.update_data(questions=parsed.questions, unknown=parsed.unknown_competencies)
    await message.answer(
        html_module.escape(preview) + "\n\nЗаменить вопросы теста? Старые вопросы и варианты пропадут, "
        "уровни останутся.", parse_mode="HTML", reply_markup=_import_kb(code, can_apply=True),
    )


@router.message(QuizImport.waiting_file)
async def prog_qzimp_not_file(message: types.Message):
    await message.answer(_NOT_FILE)


@router.callback_query(F.data.startswith("prog_qzapply:"))
async def prog_qzapply(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    data = await state.get_data()
    questions = data.get("questions")
    if data.get("city") != code or not questions:
        await state.clear()
        await callback.answer("Файл не найден — загрузите его заново.", show_alert=True)
        return
    quiz = await quiz_by_code(code)
    await quiz_import.apply(quiz["id"], quiz_import.ParsedQuiz(questions=questions))
    await state.clear()
    text, kb = await render_quiz(code)
    await callback.message.edit_text(
        f"✅ Тест обновлён: {quiz_import._count(len(questions), 'вопрос', 'вопроса', 'вопросов')}\n\n{text}", parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzimpno:"))
async def prog_qzimpno(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    await state.clear()
    text, kb = await render_quiz(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Ничего не изменилось.")
