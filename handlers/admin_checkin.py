"""Phase 12 (FORUM-CHECKIN.md): раздел «✅ Отметки на форуме» — загрузка выгрузки офлайн-
приложения-сканера (D-09/D-10) и счётчик пришедших (D-17: статус «отмечен»/«пришёл», не
«зарегистрирован»). Сканер внутри Mini App (D-08) и ленивая выдача самого QR (D-01/D-03) —
отдельные задачи Phase 12, не эта (выдача QR уже сделана Квиком 260923 —
`services/checkin.py::build_checkin_qr`/`handlers/user_actions.py::show_my_checkin_qr`).

Форма шва — эталон `handlers/admin_purge.py`/`handlers/admin_cities.py`: своего `Router()`
нет, `from handlers.admin import router`, каждый декоратор — в одну строку со строковым
литералом (инвариант cap-теста `tests/test_roles_phase8.py`). Право — `checkin`
(`handlers/admin_caps.py` ADMIN_CAPS/`_ADMIN_MENU_ROWS`, `handlers/admin_sections.py`
SECTIONS «apps»).

T-12-01 (Tampering): найденный в файле QR-код НЕ доверенный ввод — строки отчёта «не найден»/
«не одобрен»/«прошлый сезон» показывают ФИО/город ИЗ САМОГО QR (см. FORUM-CHECKIN.md «Грубая
форма реализации»: они видны в строке, D-04), а не из БД — для «не найден» строки в БД
попросту нет. Это делает их таким же недоверенным текстом, как любой пользовательский ввод —
экранируем `html.escape` перед склейкой в HTML-сообщение (иначе фальшивый QR с «<a href=...>»
внутри ФИО отрисовался бы менеджеру как живая разметка)."""
import html
import logging

from aiogram import F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.db import (
    count_approved_current_season,
    count_checkins_by_point,
    get_user_by_checkin_token,
    record_checkin,
)
from handlers.admin import router
from handlers.states import CheckinImport
from keyboards.builders import get_cancel_kb
from services.checkin import (
    ENTRY_POINT,
    ENTRY_POINT_LABEL,
    checkin_denial,
    current_event_tag,
    decode_scan_export,
    find_checkin_records,
    parse_qr_payload,
)

logger = logging.getLogger(__name__)

# T-12-02 (DoS): тот же потолок, что у «📥 Импорт прошлого события» (admin_cities.py) — гейт
# ДО bot.download, не после.
_IMPORT_MAX_BYTES = 20 * 1024 * 1024
_REPORT_ROW_LIMIT = 20

# `services.checkin.checkin_denial` возвращает машинный код допуска D-02 ("no_user" |
# "not_approved" | "past_season") -- строка отчёта нужна человеческая, эта таблица -- единая
# точка перевода кода в текст, чтобы не завести два места, знающих коды денайла.
_DENIAL_LABELS = {
    "no_user": "не найден",
    "not_approved": "не одобрен",
    "past_season": "прошлый сезон",
}


async def _counter_line() -> str:
    """«Пришли: N из M одобренных» (задача 4) — считает по точке «Вход»; строкой, не отдельной
    кнопкой (менеджер видит её при каждом заходе в раздел, без лишнего нажатия)."""
    approved = await count_approved_current_season()
    arrived = await count_checkins_by_point(ENTRY_POINT)
    return f"Пришли: {arrived} из {approved} одобренных"


@router.callback_query(F.data == "admin_checkin")
async def show_admin_checkin(callback: types.CallbackQuery):
    text = (
        "✅ <b>Отметки на форуме</b>\n\n"
        f"{await _counter_line()}\n\n"
        "Выгрузите историю сканов из приложения-сканера в CSV и пришлите сюда файлом — "
        "отмечу всех, кого найду."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Загрузить файл сканера", callback_data="checkin_upload_start")],
    ])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "checkin_upload_start")
async def checkin_upload_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_data({})
    await callback.message.answer(
        "Выгрузите историю из приложения-сканера в CSV и пришлите сюда файлом.\n\n"
        "Размер — до 20 МБ.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CheckinImport.waiting_file)
    await callback.answer()


@router.message(StateFilter(CheckinImport), Command("cancel"))
@router.message(StateFilter(CheckinImport), F.text == "Отмена")
async def cancel_checkin_import(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено. Ничего не отмечено.", reply_markup=ReplyKeyboardRemove())


@router.message(CheckinImport.waiting_file, F.document)
async def checkin_import_file_step(message: types.Message, state: FSMContext, bot: Bot):
    if (message.document.file_size or 0) > _IMPORT_MAX_BYTES:
        await message.answer("Файл больше 20 МБ — столько я принять не могу.")
        return

    buf = await bot.download(message.document.file_id)
    buf.seek(0)
    text = decode_scan_export(buf.read())

    tag = await current_event_tag()
    records = find_checkin_records(text, tag)
    if not records:
        await message.answer(
            "В файле не нашёл ни одного QR форума — проверьте, что выгружали историю сканов "
            "именно этого форума.",
            reply_markup=get_cancel_kb(),
        )
        return  # остаёмся в waiting_file -- можно сразу прислать другой файл

    await state.update_data(checkin_records=records)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ENTRY_POINT_LABEL, callback_data=f"checkin_point:{ENTRY_POINT}")],
    ])
    await message.answer(f"Нашёл кодов: {len(records)}.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Отметить точкой:", reply_markup=kb)


@router.message(CheckinImport.waiting_file)
async def checkin_import_file_invalid(message: types.Message):
    await message.answer("Пришли файл выгрузки документом (не фото и не архив).")


@router.callback_query(StateFilter(CheckinImport), F.data.startswith("checkin_point:"))
async def checkin_point_pick(callback: types.CallbackQuery, state: FSMContext):
    point = callback.data.split(":", 1)[1]
    data = await state.get_data()
    records = data.get("checkin_records") or []
    await state.set_state(None)

    new_n = 0
    dup_n = 0
    # (reason, row) -- reason — человеческая причина из _DENIAL_LABELS, а не жёстко
    # закодированные категории: денайл-правило (services.checkin.checkin_denial) само решает
    # допуск, отчёт только переводит код в текст (не дублирует логику допуска D-02).
    flagged: list[tuple[str, dict]] = []

    for rec in records:
        parsed = parse_qr_payload(rec["qr"])
        token = parsed["token"]
        user = await get_user_by_checkin_token(token) if token else None
        denial_code = await checkin_denial(user)
        if denial_code is not None:
            flagged.append((_DENIAL_LABELS.get(denial_code, denial_code), parsed))
            continue
        approx = rec["scanned_at"] is None
        status, _ts = await record_checkin(
            user["telegram_id"], point, source="csv",
            scanned_at=rec["scanned_at"], approx=approx,
            by_staff_id=callback.from_user.id,
        )
        if status == "new":
            new_n += 1
        else:
            dup_n += 1

    not_found_n = sum(1 for reason, _row in flagged if reason == _DENIAL_LABELS["no_user"])
    not_approved_n = len(flagged) - not_found_n

    lines = [
        "✅ <b>Отметки загружены</b>",
        "",
        f"Отмечено новых: {new_n} · уже были: {dup_n} · "
        f"не найдено: {not_found_n} · не одобрены: {not_approved_n}",
    ]
    shown = flagged[:_REPORT_ROW_LIMIT]
    if shown:
        lines.append("")
        lines.append("<b>Требуют внимания:</b>")
        for reason, row in shown:
            name = html.escape(row["full_name"] or "(без имени)")
            city = html.escape(row["city"] or "—")
            lines.append(f"❔ {name} · {city} — {reason}")
    remaining = len(flagged) - len(shown)
    if remaining > 0:
        lines.append(f"…и ещё {remaining}")
    lines.append("")
    lines.append(await _counter_line())

    from handlers.admin_sections import op_return_keyboard  # ленивый шов (см. docstring модуля)
    await callback.message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=await op_return_keyboard(callback.from_user.id, "admin_checkin"),
    )
    await callback.answer()
