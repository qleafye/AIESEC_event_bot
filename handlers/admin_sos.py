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
from secret_redact import redact_secrets

from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import (
    ALL_CITIES,
    admin_selected_city,
    cities_module_on,
    city_label,
    get_setting_typed_for_city,
    per_city_key,
)
from database.db import (
    claim_sos_report,
    count_sos_by_status,
    get_sos_report,
    list_sos_reports_page,
    resolve_sos_report,
)
from handlers.admin import router
from handlers.admin_caps import has_capability, required_capability
from handlers.admin_core import _admin_city_view
from handlers.states import EditSetting, SosChatBind
from keyboards.builders import get_cancel_kb
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed
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
    # CLAUDE.md: «Кодовые значения ... человеку не показываем» — код города резолвится в
    # подпись (тот же приём, что services/sos.py::render_card_text/resolve_city_label);
    # экран уже отфильтрован ПО городу (`_admin_city_view`), но при выбранном «Все города»
    # строки смешивают разные города — код в списке был бы нарушением.
    city_label = await sos_service.resolve_city_label(row.get("city"))
    lines = [
        f"#{row['id']} · {sos_service.STATUS_LABELS[status]}",
        f"🆔 <code>{row['telegram_id']}</code> {html_module.escape(str(who))}"
        + (f" · {html_module.escape(str(city_label))}" if city_label else ""),
        f"🕓 {format_stamp(row.get('created_at'), stored_utc=False)}",
    ]
    # D-31: карточка (и этот список) публикуется/держится МГНОВЕННО, без вопроса «что
    # случилось» — пока делегат ничего не дописал, менеджер должен видеть это в списке тем же
    # приёмом, что и сама карточка (`services.sos.render_card_text`).
    if not row.get("details_text") and not row.get("details_photo_file_id"):
        lines.append("🆘 подробности ещё не прислали")
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
    # Ревью 24.09 (находка 1/3, аудит ключей после 8c0d8af): тексты/тайминги SOS жили ТОЛЬКО
    # в Mini App (на проде выключен) — менеджер их менять не мог вовсе. Отдельный подэкран,
    # не список кнопок здесь, — список заявок и так длинный (пагинация PAGE=6).
    buttons.append([InlineKeyboardButton(text="⚙️ Тексты и тайминги", callback_data="asos_settings")])

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
    """Тонкая обёртка над `services.sos.refresh_card` (домен карточки целиком в этом модуле,
    см. докстринг файла) — оставлена под старым именем ради минимального диффа у вызывающих
    ниже (`sos_claim`/`sos_resolve`/`admin_reply_to_sos`)."""
    await sos_service.refresh_card(bot, report_id)


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
    sos_service.schedule_claimed_reminder(report_id, minutes, callback.from_user.id)
    await callback.answer("Взято.")
    await _refresh_card(bot, report_id)


@router.callback_query(F.data.startswith("sos_resolve:"))
async def sos_resolve(callback: types.CallbackQuery, bot: Bot, fsm_storage=None):
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
    # `fsm_storage` — хранилище диспетчера, aiogram кладёт его в данные хендлера.
    await sos_service.close_delegate_collecting(bot, fsm_storage, report)


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
        await message.reply(f"❌ Не удалось отправить ответ пользователю: {redact_secrets(e)}")
        return

    sos_service.cancel_escalation(report_id)
    await message.reply("✅ Ответ отправлен пользователю.")
    await _refresh_card(bot, report_id)


# ── Ревью 24.09 (находка 1/3, аудит ключей после 8c0d8af): «⚙️ Тексты и тайминги» ───────────
#
# Пять ключей реестра жили ТОЛЬКО в Mini App (на проде выключен, CLAUDE.md — правится в самом
# боте): два глобальных текста (`sos_delivery_failed_text`, `sos_recent_followup_text`), один
# per_city текст (`sos_fallback_contact_text`, может быть пустым — «-» его чистит, тот же
# сентинел, что у всех text-ключей) и два per_city тайминга (`sos_reopen_window_minutes`,
# `sos_claimed_remind_minutes`) — оба перечитываются заново в момент события (следующий тап
# «Беру»/следующий повторный SOS), джобу переставлять не нужно, в отличие от `services.
# session_feedback` (там задержка уже зашита в run_date уже стоящей джобы).
#
# Правка текстов идёт через ОБЩИЙ `EditSetting.waiting_for_value` (handlers/admin_settings.py::
# settings_edit_value) — валидация/HTML/сброс «-» там уже есть, здесь только вход в FSM.
# Возврат после сохранения — известное ограничение `settings_return_screen` (нет карты
# «ключ -> подэкран», docstring `handlers/admin_sections.py`): менеджер приземляется в корне
# разделов, как и у `admin_reg_percity.py::reg_prompt_edit` (глобальная ветка) — тот же
# принятый компромисс, не новый.

_SOS_DELAY_PRESETS = (5, 10, 15, 30)

# D-31: третий тайминг — «сколько ждать дозапись» (режим «дописываю SOS», handlers/sos.py::
# SosReport.collecting) — тот же реестровый ключ `sos_collecting_timeout_minutes`, тот же
# пресет/«Другое» приём, что у reopen/claimed выше, поэтому вынесен в общий словарь field ->
# base_key вместо if/else-цепочки на два значения.
_SOS_DELAY_FIELDS = {
    "reopen": "sos_reopen_window_minutes",
    "claimed": "sos_claimed_remind_minutes",
    "collecting": "sos_collecting_timeout_minutes",
}

_SOS_TEXT_FIELDS = {
    "failed": ("sos_delivery_failed_text", "🆘 Не получилось передать"),
    "followup": ("sos_recent_followup_text", "🆘 Уже есть открытый — дополнить"),
    "contact": ("sos_fallback_contact_text", "📞 Экстренный контакт (если не доставлено)"),
    "done": ("sos_done_text", "🆘 Дописывание завершено («Готово»)"),
    "resolved": ("sos_resolved_notify_text", "✅ Делегату: вопрос решён"),
    "expired": ("sos_collecting_expired_text", "🆘 Сессия дозаписи истекла"),
    "remind": ("sos_claimed_remind_text", "⏰ Напоминание взявшему"),
    "stale": ("sos_claimed_escalation_text", "⏰ Взяли, но не решили — менеджерам"),
}


async def _sos_settings_city_scope(admin_id: int) -> tuple[bool, str | None]:
    """`(per_city_ctx, code)` — `code` резолвится ТОЛЬКО когда правка per_city ключа
    однозначна (конкретный город привязки/шапки) И право на него перепроверено ЗАНОВО
    (`settings_ops.per_city_visible_codes` — та же TOCTOU-перепроверка, что
    `admin_reg_percity.py::toggle_reg_question`: привязка менеджера к городу могла
    измениться между рендером экрана и тапом кнопки). `per_city_ctx=True, code=None` —
    модуль городов включён, но выбраны «Все города»/город не закреплён/право отозвано — per_city
    ключи не правятся отсюда (тот же отказ, что `asos_bind_start` уже даёт для привязки чата)."""
    if not await cities_module_on():
        return False, None
    code = await admin_selected_city(admin_id)
    if code in (None, ALL_CITIES):
        return True, None
    import settings_ops
    if code not in await settings_ops.per_city_visible_codes(admin_id):
        return True, None
    return True, code


async def render_sos_settings_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    per_city_ctx, code = await _sos_settings_city_scope(admin_id)

    lines = ["⚙️ <b>SOS — тексты и тайминги</b>", ""]
    if per_city_ctx and code:
        lines.append(f"Тайминги и контакт — для города «{html_module.escape(await city_label(code))}».")
    elif per_city_ctx:
        lines.append(
            "Выберите конкретный город вверху раздела «🔧 Управление», чтобы менять тайминги "
            "и экстренный контакт — тексты ниже можно менять и без этого."
        )
    else:
        lines.append("Тайминги и контакт — общие (модуль городов выключен).")
    lines.append("")

    reopen_raw = await get_setting_typed_for_city("sos_reopen_window_minutes", code if per_city_ctx else None)
    try:
        reopen = int(reopen_raw) if reopen_raw else sos_service.DEFAULT_REOPEN_WINDOW_MINUTES
    except (TypeError, ValueError):
        reopen = sos_service.DEFAULT_REOPEN_WINDOW_MINUTES
    claimed_raw = await get_setting_typed_for_city("sos_claimed_remind_minutes", code if per_city_ctx else None)
    try:
        claimed = int(claimed_raw) if claimed_raw else sos_service.DEFAULT_CLAIMED_REMIND_MINUTES
    except (TypeError, ValueError):
        claimed = sos_service.DEFAULT_CLAIMED_REMIND_MINUTES
    collecting_raw = await get_setting_typed_for_city(
        "sos_collecting_timeout_minutes", code if per_city_ctx else None,
    )
    try:
        collecting = int(collecting_raw) if collecting_raw else sos_service.DEFAULT_COLLECTING_TIMEOUT_MINUTES
    except (TypeError, ValueError):
        collecting = sos_service.DEFAULT_COLLECTING_TIMEOUT_MINUTES
    contact = await get_setting_typed_for_city("sos_fallback_contact_text", code if per_city_ctx else None)

    lines.append(f"⏱ Окно повторного открытия: {reopen} мин")
    lines.append(
        f"⏱ Напоминание взявшему: через {claimed} мин, потом через "
        + " и ".join(str(m) for m in sos_service.CLAIMED_REMIND_DELAYS_MINUTES[1:])
        + " мин, после третьего — сообщение менеджерам"
    )
    lines.append(f"⏱ Сколько ждать дозапись: {collecting} мин")
    lines.append(f"📞 Экстренный контакт: {html_module.escape(contact) if contact else 'не задан'}")
    # Сколько дней видна кнопка SOS = длина форума; сама настройка живёт рядом с датой форума
    # («🎪 Событие/Медиа»), здесь — только ссылка на неё.
    days = await get_setting_typed_for_city("sos_active_days", code if per_city_ctx else None)
    lines.append(
        f"🗓 Кнопка SOS видна все дни форума ({days or sos_service.DEFAULT_ACTIVE_DAYS} дн.) — "
        "длина форума меняется рядом с датой форума"
    )

    buttons: list[list[InlineKeyboardButton]] = []
    can_edit_percity = (not per_city_ctx) or bool(code)

    def _delay_row(field: str, current: int, label: str) -> None:
        row = []
        for n in _SOS_DELAY_PRESETS:
            mark = "• " if current == n else ""
            row.append(InlineKeyboardButton(text=f"{mark}{n}", callback_data=f"asos_set_delay:{field}:{n}"))
        buttons.append([InlineKeyboardButton(text=label, callback_data="asos_noop")])
        buttons.append(row)
        buttons.append([InlineKeyboardButton(text="✏️ Другое", callback_data=f"asos_delay_custom:{field}")])

    _delay_row("reopen", reopen, "⏱ Окно повторного открытия:")
    _delay_row("claimed", claimed, "⏱ Напоминание взявшему:")
    _delay_row("collecting", collecting, "⏱ Сколько ждать дозапись:")

    if not can_edit_percity:
        lines.append("")
        lines.append("<i>Тайминги выше меняются после выбора конкретного города.</i>")

    from handlers.admin_caps import _holds, required_capability, resolve_capabilities
    length_cb = "settings_edit:sos_active_days"
    if _holds(await resolve_capabilities(admin_id), required_capability(callback_data=length_cb)):
        buttons.append([InlineKeyboardButton(text="🗓 Сколько дней идёт форум", callback_data=length_cb)])
    buttons.append([InlineKeyboardButton(text="✏️ Изменить: 📞 Экстренный контакт", callback_data="asos_settings_edit:contact")])
    buttons.append([InlineKeyboardButton(text="✏️ Изменить: 🆘 Не получилось передать", callback_data="asos_settings_edit:failed")])
    buttons.append([InlineKeyboardButton(text="✏️ Изменить: 🆘 Уже есть открытый — дополнить", callback_data="asos_settings_edit:followup")])
    buttons.append([InlineKeyboardButton(text="✏️ Изменить: 🆘 Дописывание завершено", callback_data="asos_settings_edit:done")])
    buttons.append([InlineKeyboardButton(text="✏️ Изменить: 🆘 Сессия дозаписи истекла", callback_data="asos_settings_edit:expired")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="admin_sos")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "asos_settings")
async def asos_settings_open(callback: types.CallbackQuery):
    text, kb = await render_sos_settings_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "asos_noop")
async def asos_noop(callback: types.CallbackQuery):
    # Строка-подпись группы пресетов — не кликабельна по смыслу.
    await callback.answer()


@router.callback_query(F.data.startswith("asos_set_delay:"))
async def asos_set_delay(callback: types.CallbackQuery):
    _, field, value_s = callback.data.split(":", 2)
    base_key = _SOS_DELAY_FIELDS.get(field)
    if base_key is None:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    try:
        value = int(value_s)
    except ValueError:
        await callback.answer("Некорректное значение", show_alert=True)
        return
    per_city_ctx, code = await _sos_settings_city_scope(callback.from_user.id)
    if per_city_ctx and not code:
        await callback.answer(
            "Выберите конкретный город вверху раздела «🔧 Управление».", show_alert=True,
        )
        return
    key = per_city_key(base_key, code) if per_city_ctx else base_key
    if per_city_ctx and key is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, key, str(value))
    text, kb = await render_sos_settings_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Сохранено.")


@router.callback_query(F.data.startswith("asos_delay_custom:"))
async def asos_delay_custom_start(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    base_key = _SOS_DELAY_FIELDS.get(field)
    if base_key is None:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    per_city_ctx, code = await _sos_settings_city_scope(callback.from_user.id)
    if per_city_ctx and not code:
        await callback.answer(
            "Выберите конкретный город вверху раздела «🔧 Управление».", show_alert=True,
        )
        return
    key = per_city_key(base_key, code) if per_city_ctx else base_key
    if per_city_ctx and key is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": key})
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]])
    await callback.message.answer(
        "Через сколько минут? Пришлите число, например <code>20</code>.", parse_mode="HTML", reply_markup=kb,
    )


@router.callback_query(F.data.startswith("asos_settings_edit:"))
async def asos_settings_edit_start(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    entry = _SOS_TEXT_FIELDS.get(field)
    if entry is None:
        await callback.answer("Неизвестный текст", show_alert=True)
        return
    base_key, label = entry
    is_percity = field == "contact"
    per_city_ctx, code = (await _sos_settings_city_scope(callback.from_user.id)) if is_percity else (False, None)
    if is_percity and per_city_ctx and not code:
        await callback.answer(
            "Выберите конкретный город вверху раздела «🔧 Управление».", show_alert=True,
        )
        return
    key = per_city_key(base_key, code) if (is_percity and per_city_ctx) else base_key
    if is_percity and per_city_ctx and key is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return

    current = await get_setting_typed_for_city(base_key, code if (is_percity and per_city_ctx) else None) \
        if is_percity else await get_setting_typed(base_key)
    text = f"✏️ <b>{label}</b>\n\n"
    if current:
        text += f"Сейчас: <b>{html_module.escape(str(current))}</b>\n\n"
    else:
        text += "Сейчас: <i>не задано</i>\n\n" if is_percity else "Сейчас: <i>стандартный</i>\n\n"
    text += "Пришли новый текст одним сообщением."
    if is_percity:
        text += "\n\n<i>«-» — очистить (без контакта).</i>"
    else:
        text += "\n\n<i>«-» — вернуть стандартный текст.</i>"

    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": key})
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()
