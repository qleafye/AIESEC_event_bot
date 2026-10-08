"""Личная Яндекс Форма (без организации) в карточке: загрузка старых ответов файлом выгрузки
и выбор вопросов ника и телефона кнопками — когда первые ответы уже пришли.

Шов на общий `handlers.admin.router` (своего Router нет, декораторы в одну строку). Права — в
handlers/admin_caps.py: `extf_*` и состояние ExtFormImport — `settings`.
"""
import logging

from aiogram import Bot, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database import ext_forms_db as xdb
from handlers.admin import router
from handlers.admin_ext_forms import _btn, _e, _show, _tail_id
from handlers.states import ExtFormImport
from keyboards.builders import get_cancel_kb
from services.background import spawn
from services.ext_forms_import import (
    ImportFileError, export_to_answers, import_answers, read_export_rows,
)
from services.ext_forms_match import rematch_unmatched

logger = logging.getLogger(__name__)

IMPORT_MAX_BYTES = 10 * 1024 * 1024
_LABEL_LIMIT = 40
_STALE = "Список вопросов устарел — откройте выбор ещё раз"
_NOT_FOUND = "Форма не найдена — обновите список."


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cut(text: str) -> str:
    return text if len(text) <= _LABEL_LIMIT else text[:_LABEL_LIMIT - 1] + "…"


async def _push_form(form_id: int | None) -> dict | None:
    f = await xdb.get_form(form_id) if form_id is not None else None
    return f if f and f.get("ingest_mode") == "push" else None


# ── загрузка старых ответов ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("extf_import:"))
async def extf_import_start(callback: types.CallbackQuery, state: FSMContext):
    form_id = _tail_id(callback.data)
    if await _push_form(form_id) is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    await state.set_state(ExtFormImport.waiting_file)
    await state.update_data(extf_import_form=form_id)
    await callback.message.answer(
        "Пришлите файл выгрузки ответов: в Яндекс Формах «Ответы» → «Выгрузить» → XLSX "
        "(или CSV). До 10 МБ, до 5000 ответов.", reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(StateFilter(ExtFormImport.waiting_file), F.text.in_({"Отмена", "/cancel"}))
async def extf_import_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Ничего не загружено.", reply_markup=ReplyKeyboardRemove())


@router.message(StateFilter(ExtFormImport.waiting_file), F.document)
async def extf_import_file(message: types.Message, state: FSMContext, bot: Bot):
    doc = message.document
    if (doc.file_size or 0) > IMPORT_MAX_BYTES:
        await message.answer("Файл больше 10 МБ — столько я принять не могу. "
                             "Выгрузите ответы частями или в CSV.")
        return
    form_id = (await state.get_data()).get("extf_import_form")
    form = await _push_form(form_id)
    if form is None:
        await state.clear()
        await message.answer("Форма не найдена — откройте её карточку и начните заново.",
                             reply_markup=ReplyKeyboardRemove())
        return
    await message.answer("Загружаю…")
    try:
        buf = await bot.download(doc.file_id)
        buf.seek(0)
        rows = read_export_rows(buf.read(), doc.file_name or "")
        export_to_answers(rows)  # проверка заголовков и лимита до записи
        report = await import_answers(form, rows)
    except ImportFileError as e:
        await message.answer(e.text)  # состояние не сбрасываем: можно прислать другой файл
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("ext_forms: импорт файла формы %s: %s", form_id, type(e).__name__)
        await message.answer("Не получилось загрузить файл. Проверьте, что это выгрузка ответов "
                             "из Яндекс Форм, и пришлите ещё раз.")
        return
    await state.clear()
    await message.answer(
        f"📥 Готово: добавлено {report['added']}, уже были {report['existed']}, "
        f"без ID ответа {report['no_id']}.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Что дальше?", reply_markup=_kb(
        [[_btn("📄 Карточка формы", f"extf_card:{form_id}")]]))


@router.message(StateFilter(ExtFormImport.waiting_file))
async def extf_import_not_file(message: types.Message):
    await message.answer("Пришлите файл выгрузки (XLSX или CSV) документом. "
                         "Передумали — нажмите «Отмена».")


# ── вопросы ника и телефона ──────────────────────────────────────────────────────────────

def _key_label(columns: list[dict], qkey: str | None) -> str:
    for c in columns:
        if c["qkey"] == qkey:
            return f"вопрос «{_e(c['label'] or c['qkey'])}»"
    return "не выбран"


async def _show_pkeys(callback: types.CallbackQuery, form: dict) -> None:
    columns = await xdb.list_columns(form["id"])
    text = (f"🔑 <b>{_e(form['title'])}</b>\n\nКак найти человека среди делегатов:\n"
            f"Ник в Telegram — {_key_label(columns, form.get('key_username_q'))}\n"
            f"Телефон — {_key_label(columns, form.get('key_phone_q'))}")
    fid = form["id"]
    kb = _kb([
        [_btn("✏️ Вопрос для ника", f"extf_pkey:{fid}:u")],
        [_btn("✏️ Вопрос для телефона", f"extf_pkey:{fid}:p")],
        [_btn("⬅️ К форме", f"extf_card:{fid}")],
    ])
    await _show(callback, text, kb, edit=True)


@router.callback_query(F.data.startswith("extf_pkeys:"))
async def extf_pkeys(callback: types.CallbackQuery):
    form = await _push_form(_tail_id(callback.data))
    if form is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    if not await xdb.list_columns(form["id"]):
        await callback.answer("Сначала нужен первый ответ или загрузка файла — тогда я узнаю "
                              "вопросы формы.", show_alert=True)
        return
    await _show_pkeys(callback, form)
    await callback.answer()


def _parse_pkey(data: str, size: int) -> tuple[int, str, str] | None:
    parts = str(data).split(":")
    if len(parts) != size:
        return None
    try:
        return int(parts[1]), parts[2], (parts[3] if size == 4 else "")
    except ValueError:
        return None


@router.callback_query(F.data.startswith("extf_pkey:"))
async def extf_pkey(callback: types.CallbackQuery):
    parsed = _parse_pkey(callback.data, 3)
    form = await _push_form(parsed[0]) if parsed else None
    if form is None or parsed[1] not in ("u", "p"):
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    columns = await xdb.list_columns(form["id"])
    rows = [[_btn(_cut(c["label"] or c["qkey"]), f"extf_pkeyset:{form['id']}:{parsed[1]}:{i}")]
            for i, c in enumerate(columns)]
    rows.append([_btn("Такого вопроса нет", f"extf_pkeyset:{form['id']}:{parsed[1]}:none")])
    what = "ник в Telegram" if parsed[1] == "u" else "телефон"
    await _show(callback, f"Какой вопрос формы — {what}?", _kb(rows), edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_pkeyset:"))
async def extf_pkeyset(callback: types.CallbackQuery):
    parsed = _parse_pkey(callback.data, 4)
    form = await _push_form(parsed[0]) if parsed else None
    if form is None or parsed[1] not in ("u", "p"):
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    if parsed[2] == "none":
        qkey = None
    else:
        columns = await xdb.list_columns(form["id"])
        try:
            qkey = columns[int(parsed[2])]["qkey"]
        except (ValueError, IndexError):
            await callback.answer(_STALE, show_alert=True)
            return
    uq, pq = form.get("key_username_q"), form.get("key_phone_q")
    if parsed[1] == "u":
        uq = qkey
    else:
        pq = qkey
    await xdb.set_form_keys(form["id"], uq, pq)
    spawn(rematch_unmatched())
    form = await xdb.get_form(form["id"])
    await _show_pkeys(callback, form)
    await callback.answer("Сохранено — уже пришедшие ответы бот привяжет заново")
