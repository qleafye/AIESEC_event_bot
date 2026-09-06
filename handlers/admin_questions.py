"""Quick 260904-2cj (QJRN-01..04): раздел «❓ Вопросы делегатов» — журнал ВСЕХ вопросов из
кнопки «Задать вопрос» со статусом каждого (без ответа / в работе / отвечен) и ответом прямо
из экрана. Правило статуса — ОДНО место, `services/questions.py` (чистый модуль, эту логику
здесь не дублируем).

Заменяет собой односостояние «🔒 Залипшие вопросы» (T-08-33): старый callback
`admin_stuck_questions` (см. `handlers/admin.py::show_stuck_questions`) теперь редиректит на
этот же экран с фильтром «в работе» — клавиатуры, живущие в старых чатах, продолжают
работать, а кнопки на главном экране больше нет (заменена на «❓ Вопросы делегатов»).

Форма шва — Phase 13 (REFAC-01): своего `Router()` нет, хендлеры декорируют ОБЩИЙ
`handlers.admin.router`; модуль подключается ХВОСТОМ `handlers/admin.py`. Импорты
`admin_core`/`admin_sections` — см. комментарии у самих импортов ниже (admin_core на уровне
модуля безопасен, admin_sections даёт цикл и потому лениво внутри функций — тот же приём, что
у каждого другого шва этого файла)."""
import html as html_module

from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.db import (
    claim_question,
    count_questions_by_status,
    get_question,
    list_questions_page,
)
from handlers.admin import router
from handlers.admin_core import _admin_city_view
from handlers.states import QuestionAnswer
from keyboards.builders import get_cancel_kb
from services.questions import (
    FILTER_LABELS,
    format_stamp,
    is_stuck,
    question_status,
    status_label,
)

PAGE = 6
QUESTION_TEXT_LIMIT = 160
ANSWER_TEXT_LIMIT = 120

# Порядок чипов фильтра на экране — тот же, что в FILTER_LABELS (services/questions.py).
_FILTER_ORDER = ("all", "new", "in_work", "answered")


def _display_delegate(row: dict) -> str:
    name = row.get("user_full_name")
    username = row.get("user_username")
    if name:
        return html_module.escape(str(name))
    if username:
        return html_module.escape(str(username))
    return "—"


def _row_text(row: dict) -> str:
    """Одна строка журнала. Для "in_work" сырой `user_id` и `answered_by_name` — КОНТРАКТ с
    живым тестом `tests/test_roles_phase8.py::test_stuck_questions_screen_shows_claimed_undelivered`
    (дёргает алиас `admin_stuck_questions`) — менять их формат нельзя без правки того теста."""
    status = question_status(row)
    lines = [
        f"#{row['id']} · {status_label(row)} · "
        f"{html_module.escape(format_stamp(row.get('asked_at')))}",
        f"🆔 <code>{row['user_id']}</code> {_display_delegate(row)}",
        f"«{html_module.escape(str(row.get('question_text') or '')[:QUESTION_TEXT_LIMIT])}»",
    ]
    if status == "in_work":
        who = html_module.escape(str(row.get("answered_by_name") or "—"))
        lines.append(f"✍️ взял(а) {who}")
        if is_stuck(row):
            lines.append("🔒 залип")
    elif status == "answered":
        who = html_module.escape(str(row.get("answered_by_name") or "—"))
        when = html_module.escape(format_stamp(row.get("delivered_at")))
        lines.append(f"✅ {who} · {when}")
        answer = html_module.escape(str(row.get("answer_text") or "")[:ANSWER_TEXT_LIMIT])
        lines.append(f"↩️ {answer}")
    return "\n".join(lines)


async def render_questions_screen(
    admin_id: int, status: str | None = None, offset: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    """«Функция возвращает (text, kb)» idiom (форма `_coins_journal_screen`) — каждая точка
    перерисовки (открытие, фильтр, страница, возврат после ответа) остаётся байт-в-байт
    одинаковой по формату. `status=None` — самый первый вход в раздел (кнопка/старый алиас),
    ДО того как менеджер тронул фильтр; отображается без строк «Показаны»/«Страница N из M»
    (T-20-10 idiom: экран не навязывает контекст, которого менеджер ещё не выбирал). Любой
    вызов через `aq:*` всегда несёт явный статус (в т.ч. "all"), поэтому обе строки после
    первого тапа появляются всегда."""
    # WR-05: одно чтение города на экран — тот же scope уходит и в счётчики, и в выборку,
    # иначе «без ответа: N» разойдётся со списком под ним.
    scope, label = await _admin_city_view(admin_id)
    counts = await count_questions_by_status(city_scope=scope)
    rows = await list_questions_page(status=status, city_scope=scope, limit=PAGE, offset=offset)

    active = status if status in FILTER_LABELS else "all"
    total = counts.get(active, counts["all"]) if active != "all" else counts["all"]

    lines = ["❓ <b>Вопросы делегатов</b>"]
    lines.append(
        f"без ответа: {counts['new']} · в работе: {counts['in_work']} · "
        f"отвечено: {counts['answered']}"
    )
    if label:
        lines.append(html_module.escape(str(label)))
    if status is not None:
        total_pages = max(1, (total + PAGE - 1) // PAGE) if total else 1
        current_page = offset // PAGE + 1
        lines.append(f"Показаны: {FILTER_LABELS[active]}")
        lines.append(f"Страница {current_page} из {total_pages}")

    if not rows:
        lines.append("")
        if active == "all":
            lines.append("Вопросов пока нет.")
        else:
            lines.append("В этом состоянии вопросов нет — попробуйте фильтр «Все».")
    else:
        for row in rows:
            lines.append("")
            lines.append(_row_text(row))

    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    filter_row = [
        InlineKeyboardButton(
            text=("• " if opt == active else "") + FILTER_LABELS[opt],
            callback_data=f"aq:{opt}:0",
        )
        for opt in _FILTER_ORDER
    ]
    buttons.append(filter_row)

    for row in rows:
        if question_status(row) != "answered":
            buttons.append([InlineKeyboardButton(
                text=f"✉️ Ответить #{row['id']}", callback_data=f"aq_answer:{row['id']}",
            )])
        else:
            # Quick 260906-8uq (FAQ-04): под ОТВЕЧЕННЫМ вопросом — кнопка «В FAQ», ведёт в
            # handlers/admin_faq.py::afaq_from_question (черновик правится ДО сохранения).
            buttons.append([InlineKeyboardButton(
                text=f"❓ В FAQ #{row['id']}", callback_data=f"afaq_from:{row['id']}",
            )])

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"aq:{active}:{max(0, offset - PAGE)}",
        ))
    if offset + PAGE < total:
        nav_row.append(InlineKeyboardButton(
            text="➡️", callback_data=f"aq:{active}:{offset + PAGE}",
        ))
    if nav_row:
        buttons.append(nav_row)

    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    buttons.append([back_button("admin_questions")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_questions")
async def admin_questions(callback: types.CallbackQuery):
    text, kb = await render_questions_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("aq:"))
async def aq_page(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    _, status_raw, offset_raw = parts
    try:
        offset = int(offset_raw)
    except ValueError:
        offset = -1
    if offset < 0:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    status = status_raw if status_raw in FILTER_LABELS else "all"
    text, kb = await render_questions_screen(callback.from_user.id, status=status, offset=offset)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("aq_answer:"))
async def aq_answer_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        qid = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный вопрос", show_alert=True)
        return
    question = await get_question(qid)
    if question is None:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return
    await state.update_data(aq_qid=qid, aq_user_id=question["user_id"])
    await state.set_state(QuestionAnswer.text)
    await callback.message.answer(
        "Напишите ответ делегату одним сообщением.", reply_markup=get_cancel_kb(),
    )
    await callback.answer()


# WR-03-class guard (форма `grev_step_cancel`): зарегистрирован ПЕРВЫМ (admin.router: первый
# совпавший фильтр побеждает) — «Отмена» или набранная посреди ввода команда не должны уехать
# делегату как ответ (T-09-14/WR-03 lesson).
@router.message(QuestionAnswer.text, F.text.in_({"Отмена"}) | F.text.startswith("/"))
async def aq_answer_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(QuestionAnswer.text)
async def aq_answer_step(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    qid = data.get("aq_qid")
    user_id = data.get("aq_user_id")
    await state.set_state(None)

    if qid is None or user_id is None:
        await message.answer(
            "Что-то пошло не так — откройте вопрос заново.", reply_markup=ReplyKeyboardRemove(),
        )
        return

    admin_name = message.from_user.full_name or message.from_user.username or "Админ"
    row = await get_question(qid)

    if row and row.get("delivered_at"):
        winner_name = row.get("answered_by_name") or "коллега"
        await message.answer(f"⚠️ На этот вопрос уже ответил(а) {winner_name}.")
    else:
        claimed = await claim_question(qid, message.from_user.id, admin_name)
        # Ленивый импорт (цикл на уровне модуля: admin.py импортирует этот шов хвостом).
        from handlers.admin import _attempt_question_delivery

        if not claimed:
            # T-08-33, часть C: та же развилка, что у `admin_reply_to_question` — проигравший
            # захват МОЖЕТ оказаться тем же человеком, чья предыдущая попытка доставки упала
            # (claim_question переворачивает строку только из answered_by IS NULL, второй
            # вызов от того же claimant тоже вернёт False). Перечитываем строку заново — не
            # переиспользуем снимок `row` до попытки захвата.
            row2 = await get_question(qid)
            if (
                row2
                and row2.get("answered_by") == message.from_user.id
                and not row2.get("delivered_at")
            ):
                await _attempt_question_delivery(message, bot, user_id, admin_name, qid)
            else:
                winner_name = (row2 or {}).get("answered_by_name") or "коллега"
                await message.answer(f"⚠️ На этот вопрос уже ответил(а) {winner_name}.")
        else:
            await _attempt_question_delivery(message, bot, user_id, admin_name, qid)

    # `_attempt_question_delivery`/`_deliver_question_reply` шлют свой собственный текст
    # (успех/ошибка) БЕЗ reply_markup — клавиатура «Отмена» без этого осталась бы висеть на
    # экране делегата после завершения диалога (Rule 2: правило «бот для людей» — лишний
    # контрол без действия сбивает с толку). Одно короткое сообщение снимает клавиатуру, потом
    # экран журнала перерисовывается заново.
    await message.answer("Готово.", reply_markup=ReplyKeyboardRemove())
    text, kb = await render_questions_screen(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
