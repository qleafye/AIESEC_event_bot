"""Форум-ночь п.8 (идея №19 бэклога чек-ина): менеджерская сторона «🆘 SOS» — экран
(пункт 5 плана), привязка чата SOS (пункт 2), реплай-ответ на карточку (пункт 3, «Беру»/
«✅ Решено» — тоже пункт 3).

Форма шва — Phase 13 (REFAC-01): своего `Router()` нет, хендлеры декорируют ОБЩИЙ
`handlers.admin.router`; модуль импортируется ХВОСТОМ `handlers/admin.py` (тот же приём, что
`handlers/admin_questions.py`, у которого позаимствован и сам экран-журнал — screen-рендер
«(text, kb)», фильтр-чипы, пагинация PAGE=6).

Домен (счётчики, статус, карточка, привязка, эскалация) — целиком в `services/sos.py`; здесь
только хендлеры и рендер экрана."""
import html as html_module
import re

from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import ALL_CITIES, admin_selected_city, cities_module_on, get_setting_typed_for_city
from database.db import (
    claim_sos_report,
    count_sos_by_status,
    get_sos_report,
    get_user,
    list_sos_reports_page,
    resolve_sos_report,
)
from handlers.admin import router
from handlers.admin_caps import has_capability, required_capability
from handlers.admin_core import _admin_city_view
from handlers.states import SosChatBind
from keyboards.builders import get_cancel_kb
from services import sos as sos_service
from services.questions import format_stamp
from services.timeutil import msk_now

PAGE = 6
FILTER_LABELS = {
    "all": "Все", "open": "Открыто", "claimed": "Взято", "resolved": "Решено сегодня",
}
_FILTER_ORDER = ("all", "open", "claimed", "resolved")


async def _row_text(row: dict) -> str:
    status = sos_service.report_status(row)
    who = row.get("user_full_name") or row.get("user_username") or "—"
    category = sos_service.CATEGORY_LABELS.get(row.get("category"), str(row.get("category")))
    # CLAUDE.md: «Кодовые значения ... человеку не показываем» — код города резолвится в
    # подпись (тот же приём, что services/sos.py::render_card_text/resolve_city_label);
    # экран уже отфильтрован ПО городу (`_admin_city_view`), но при выбранном «Все города»
    # строки смешивают разные города — код в списке был бы нарушением.
    city_label = await sos_service.resolve_city_label(row.get("city"))
    lines = [
        f"#{row['id']} · {sos_service.STATUS_LABELS[status]} · {category}",
        f"🆔 <code>{row['telegram_id']}</code> {html_module.escape(str(who))}"
        + (f" · {html_module.escape(str(city_label))}" if city_label else ""),
        f"🕓 {format_stamp(row.get('created_at'), stored_utc=False)}",
    ]
    if status == "claimed":
        lines.append(f"✍️ взял(а) {html_module.escape(str(row.get('claimed_by_name') or '—'))}")
    elif status == "resolved":
        lines.append(f"✅ {html_module.escape(str(row.get('resolved_by_name') or '—'))}")
    # Ревью 24.09 (находка 1): карточка не дошла НИКУДА (ни в чат, ни фоллбэком в личку) —
    # у неё физически нет ни `chat_id`, ни личных копий с общим треадом, поэтому единственное
    # место, где менеджер вообще узнаёт об этом SOS, — этот список.
    if row.get("delivery_failed_at"):
        lines.append("🔴 не доставлен оргкомитету — повтор запланирован")
    return "\n".join(lines)


async def render_sos_screen(admin_id: int, status: str | None = None, offset: int = 0
                             ) -> tuple[str, InlineKeyboardMarkup]:
    scope, label = await _admin_city_view(admin_id)
    today = msk_now().strftime("%Y-%m-%d")
    counts = await count_sos_by_status(city_scope=scope, today=today)

    active = status if status in FILTER_LABELS else "all"
    fetch_status = None if active == "all" else active
    rows = await list_sos_reports_page(
        status=fetch_status, city_scope=scope, today=today if active == "resolved" else None,
        limit=PAGE, offset=offset,
    )
    total = counts.get(active, counts["open"] + counts["claimed"] + counts["resolved"]) \
        if active != "all" else counts["open"] + counts["claimed"] + counts["resolved"]

    lines = ["🆘 <b>SOS</b>"]
    lines.append(f"открыто: {counts['open']} · взято: {counts['claimed']} · решено сегодня: {counts['resolved']}")
    if label:
        lines.append(html_module.escape(str(label)))

    # Пункт 2 плана: предупреждение, если чат SOS не привязан (город чекается тем же скоупом,
    # что и остальной экран — конкретный код, если модуль городов включён и выбран не «Все»).
    bind_city = None
    if await cities_module_on():
        code = await admin_selected_city(admin_id)
        bind_city = code if code not in (None, ALL_CITIES) else None
    chat = await sos_service.sos_chat_for_city(bind_city)
    if chat is not None:
        lines.append(f"💬 Чат SOS: «{html_module.escape(chat['title'] or str(chat['chat_id']))}»")
        # Ревью 24.09 (находка 2): последняя отправка карточки в этот чат упала (кик,
        # неизвестная ошибка — не миграция, та перепривязывает автоматически) — красная
        # строка, тот же процесс держит `services.sos.chat_is_unhealthy` и шлёт алерт.
        if sos_service.chat_is_unhealthy(chat["chat_id"]):
            lines.append("🔴 Чат SOS не отвечает — SOS идут в личку. Перепривяжите чат.")
    else:
        lines.append("⚠️ Чат SOS не привязан — SOS идут в личку менеджерам.")

    if status is not None:
        total_pages = max(1, (total + PAGE - 1) // PAGE) if total else 1
        current_page = offset // PAGE + 1
        lines.append(f"Показаны: {FILTER_LABELS[active]}")
        lines.append(f"Страница {current_page} из {total_pages}")

    if not rows:
        lines.append("")
        lines.append("SOS пока нет." if active == "all" else "В этом состоянии SOS нет — попробуйте фильтр «Все».")
    else:
        for row in rows:
            lines.append("")
            lines.append(await _row_text(row))

    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(
            text=("• " if opt == active else "") + FILTER_LABELS[opt],
            callback_data=f"asos:{opt}:0",
        )
        for opt in _FILTER_ORDER
    ]]
    # Кнопка видна ВСЕГДА, не только когда чат не привязан — иначе менеджер, привязавший
    # чат по ошибке, не смог бы перепривязать его без прямого лазания в БД (бот для людей:
    # разрушительного шага здесь нет — привязка просто перезаписывается, старый чат при этом
    # не отвязывается автоматически, поэтому подпись отличается словом «Перепривязать»).
    bind_text = "🔗 Перепривязать чат SOS" if chat is not None else "🔗 Привязать чат SOS"
    buttons.append([InlineKeyboardButton(text=bind_text, callback_data="asos_bind")])

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"asos:{active}:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"asos:{active}:{offset + PAGE}"))
    if nav_row:
        buttons.append(nav_row)

    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    buttons.append([back_button("admin_sos")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_sos")
async def admin_sos(callback: types.CallbackQuery):
    text, kb = await render_sos_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("asos:"))
async def asos_page(callback: types.CallbackQuery):
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
    text, kb = await render_sos_screen(callback.from_user.id, status=status, offset=offset)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Пункт 2 плана: «🔗 Привязать чат SOS» — бот просит добавить его в группу и прислать оттуда
# подтверждение (пересылка сообщения ИЗ группы, эта DM-ветка; ИЛИ команда `/sos_id`, набранная
# прямо в группе, `handlers/group_chat.py`). Одна и та же цель —
# `services.sos.complete_chat_bind`, потребляет ту же заявку `sos_chat_bind_pending`.

_BIND_INSTRUCTIONS = (
    "Добавьте бота в чат оргов этого города (правами администратора можно не наделять) и "
    "подтвердите одним из двух способов:\n\n"
    "1. Перешлите сюда любое сообщение ИЗ этого чата — бот определит чат по пересылке.\n"
    "2. Если пересылка в чате выключена (защита контента) — отправьте прямо в чате команду "
    "/sos_id.\n\n"
    "Заявка активна 15 минут."
)


@router.callback_query(F.data == "asos_bind")
async def asos_bind_start(callback: types.CallbackQuery, state: FSMContext):
    bind_city = None
    if await cities_module_on():
        code = await admin_selected_city(callback.from_user.id)
        if code == ALL_CITIES:
            await callback.answer(
                "Выберите конкретный город вверху раздела «🔧 Управление» — чат SOS "
                "привязывается к одному городу, не ко «Всем».",
                show_alert=True,
            )
            return
        bind_city = code
    await sos_service.set_pending_bind(callback.from_user.id, bind_city)
    await state.set_state(SosChatBind.waiting)
    await callback.answer()
    await callback.message.answer(_BIND_INSTRUCTIONS, reply_markup=get_cancel_kb())


@router.message(SosChatBind.waiting, F.text.in_({"Отмена", "/cancel"}))
async def asos_bind_cancel(message: types.Message, state: FSMContext):
    await sos_service.clear_pending_bind(message.from_user.id)
    await state.clear()
    await message.answer("Привязка отменена.", reply_markup=ReplyKeyboardRemove())


@router.message(SosChatBind.waiting)
async def asos_bind_step(message: types.Message, state: FSMContext, bot: Bot):
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin is not None else None
    if chat is None:
        await message.answer(
            "Не понял — перешлите сюда сообщение ИЗ чата оргов (не от человека), либо "
            "отправьте команду /sos_id прямо в этом чате."
        )
        return
    result = await sos_service.complete_chat_bind(
        bot, message.from_user.id, chat.id, chat.title or chat.full_name or "",
    )
    # Ревью 24.09 (находка 2): бота ещё нет в целевом чате — заявка НЕ потреблена
    # (`complete_chat_bind`), состояние `SosChatBind.waiting` НЕ снимается: менеджер добавляет
    # бота в группу и пересылает то же сообщение ещё раз, без похода в «🔗 Привязать чат SOS».
    if result == sos_service.BIND_NOT_MEMBER:
        await message.answer(
            "Сначала добавьте бота в эту группу, потом перешлите сообщение ещё раз."
        )
        return
    await state.clear()
    await message.answer(
        "Готово." if result == sos_service.BIND_OK
        else "Заявка устарела — откройте «🔗 Привязать чат SOS» заново.",
        reply_markup=ReplyKeyboardRemove(),
    )
    if result == sos_service.BIND_OK:
        text, kb = await render_sos_screen(message.from_user.id)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Пункт 3 плана: «🙋 Беру» / «✅ Решено» под карточкой ─────────────────────────────────────

async def _refresh_card(bot: Bot, report_id: int) -> None:
    """Перерисовывает карточку в чате (если она там есть) после захвата/решения — fail-soft:
    карточка могла быть удалена/устареть, это не должно ронять сам захват/ответ."""
    report = await get_sos_report(report_id)
    if report is None or not report.get("chat_id") or not report.get("card_message_id"):
        return
    user = await get_user(report["telegram_id"])
    city_label = await sos_service.resolve_city_label(report.get("city"))
    text = sos_service.render_card_text(report, user, city_label=city_label)
    # Ревью 24.09 (находка 4): «✅ Решено» убирает кнопки под карточкой — сам текст «Решено:
    # … в HH:MM» остаётся (уже несёт `render_card_text` по статусу выше).
    kb = (
        None if sos_service.report_status(report) == sos_service.STATUS_RESOLVED
        else sos_service.build_card_kb(report_id)
    )
    try:
        await bot.edit_message_text(
            text, chat_id=report["chat_id"], message_id=report["card_message_id"],
            parse_mode="HTML", reply_markup=kb,
        )
    except Exception:
        pass


def _card_origin_ok(callback: types.CallbackQuery, report: dict) -> bool:
    """Ревью 24.09 (находка 5): любой участник ПРИВЯЗАННОГО чата SOS может нажать «Беру»/
    «Решено» (это чат оргов — так и задумано, пункт 3 плана), но карточку могли переслать в
    ДРУГУЮ группу (другой чат оргов, случайный чат, чат другого города) — там кнопки не должны
    срабатывать вовсе, иначе первый нажавший в чужой группе перехватывает чужой SOS.

    Личка (фоллбэк-веер, `report["chat_id"] is None`) — уже под капой `moderate_reg`
    (`ADMIN_CAPS["sos_claim:*"]`/`"sos_resolve:*"` на `handlers.admin.router`), эта проверка её
    не сужает: `chat_type == "private"` всегда проходит."""
    chat_type = getattr(callback.message.chat, "type", None) or "private"
    if chat_type in ("group", "supergroup") and callback.message.chat.id != report.get("chat_id"):
        return False
    return True


@router.callback_query(F.data.startswith("sos_claim:"))
async def sos_claim(callback: types.CallbackQuery, bot: Bot):
    try:
        report_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректная карточка", show_alert=True)
        return
    report = await get_sos_report(report_id)
    if report is None:
        await callback.answer("Некорректная карточка", show_alert=True)
        return
    if not _card_origin_ok(callback, report):
        await callback.answer("Эта карточка не из чата SOS", show_alert=True)
        return
    admin_name = callback.from_user.full_name or callback.from_user.username or "Орг"
    claimed = await claim_sos_report(report_id, callback.from_user.id, admin_name)
    if not claimed:
        row = await get_sos_report(report_id)
        winner = (row or {}).get("claimed_by_name") or "коллега"
        await callback.answer(f"Уже взял(а) {winner}.", show_alert=True)
        return
    sos_service.cancel_escalation(report_id)
    # Ревью 24.09 (находка 3): напоминание взявшему, если за N минут не отметил «✅ Решено».
    try:
        minutes_raw = await get_setting_typed_for_city("sos_claimed_remind_minutes", report.get("city"))
        minutes = int(minutes_raw) if minutes_raw else sos_service.DEFAULT_CLAIMED_REMIND_MINUTES
    except (TypeError, ValueError):
        minutes = sos_service.DEFAULT_CLAIMED_REMIND_MINUTES
    sos_service.schedule_claimed_reminder(report_id, minutes)
    await callback.answer("Взято.")
    await _refresh_card(bot, report_id)


@router.callback_query(F.data.startswith("sos_resolve:"))
async def sos_resolve(callback: types.CallbackQuery, bot: Bot):
    try:
        report_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректная карточка", show_alert=True)
        return
    report = await get_sos_report(report_id)
    if report is None:
        await callback.answer("Некорректная карточка", show_alert=True)
        return
    if not _card_origin_ok(callback, report):
        await callback.answer("Эта карточка не из чата SOS", show_alert=True)
        return
    admin_name = callback.from_user.full_name or callback.from_user.username or "Орг"
    resolved = await resolve_sos_report(report_id, callback.from_user.id, admin_name)
    if not resolved:
        await callback.answer("Уже решено.", show_alert=True)
        return
    sos_service.cancel_escalation(report_id)
    sos_service.cancel_claimed_reminder(report_id)
    await callback.answer("Отмечено решённым.")
    await _refresh_card(bot, report_id)


# ── Пункт 3 плана: ответ орга РЕПЛАЕМ на карточку (мимо тихих часов — «это срочное») ────────

async def is_sos_reply(message: types.Message) -> bool:
    """Та же форма, что `handlers.admin.is_question_reply` — предикат по ФОРМЕ сообщения
    (реплай на карточку с маркерами "🆔"+"🆘"), право перепроверяется ВНУТРИ (та же капа,
    что несёт `ADMIN_CAPS["special:sos_reply"]`) — без этой проверки делегатский ответ на
    похожую по форме карточку тоже совпал бы с предикатом."""
    cap = required_capability(special="sos_reply")
    if not cap or not await has_capability(message.from_user.id, cap):
        return False
    replied = message.reply_to_message
    if not replied or not replied.text:
        return False
    return "🆔" in replied.text and "🆘" in replied.text


@router.message(is_sos_reply)
async def admin_reply_to_sos(message: types.Message, bot: Bot):
    replied = message.reply_to_message
    id_match = re.search(r"🆔\s*(\d+)", replied.text)
    report_match = re.search(r"SOS #([0-9]+)", replied.text)
    if not id_match or not report_match:
        return
    user_id = int(id_match.group(1))
    report_id = int(report_match.group(1))
    report = await get_sos_report(report_id)
    if report is None:
        return

    admin_name = message.from_user.full_name or message.from_user.username or "Орг"
    claimed = await claim_sos_report(report_id, message.from_user.id, admin_name)
    if not claimed:
        row = await get_sos_report(report_id)
        same_claimant = (
            row and row.get("claimed_by") == message.from_user.id and not row.get("resolved_at")
        )
        if not same_claimant:
            winner = (row or {}).get("claimed_by_name") or "коллега"
            await message.reply(f"⚠️ SOS #{report_id} уже взял(а) {winner}.")
            return

    # Пункт 3 (правка исполнения): ответ по SOS — срочный, тихие часы (services/quiet_hours.py)
    # здесь НЕ применяются в отличие от «❓ Задать вопрос» — доставка идёт напрямую bot.send_
    # message/copy_to, без очереди на утро.
    header = f"🆘 <b>Ответ по SOS #{report_id}:</b>"
    try:
        if message.text:
            await bot.send_message(user_id, f"{header}\n\n{message.html_text}", parse_mode="HTML")
        else:
            await bot.send_message(user_id, header, parse_mode="HTML")
            await message.copy_to(user_id)
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить ответ пользователю: {e}")
        return

    sos_service.cancel_escalation(report_id)
    await message.reply("✅ Ответ отправлен пользователю.")
    await _refresh_card(bot, report_id)
