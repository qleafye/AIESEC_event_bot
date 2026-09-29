"""Квик 260927: экран «📣 Публикация рейтинга в чат» (из «🏆 Рейтинг чата»).

Менеджер города включает еженедельный пост рейтинга в чат делегатов, выбирает день кнопками,
время и число мест — текстом с примером, тексты заголовков и подписи — текстом с подсказкой
плейсхолдеров. «👁 Отправить сейчас (проверка)» присылает пост менеджеру в личку; оттуда —
«📣 Опубликовать в чат» с подтверждением. Сам пост и расписание — services/chat_rating_post.py.

Город — шапка админки, зашит в каждую кнопку и перепроверяется на тапе (тот же
`_checked_city`, что у экрана рейтинга). Модуль городов включён, а в шапке «все города» —
публикация не настраивается: у поста всегда один конкретный чат города.
Регистрируется на общий `admin.router` хвостовым импортом `handlers/admin.py`.
"""
import html as html_module
import logging
from datetime import datetime

from aiogram import Bot, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.cities import get_setting_typed_for_city, per_city_key
from handlers.admin import router
from handlers.admin_chat_rating import _GLOBAL, _checked_city, _raw, _screen_city
from handlers.settings_validation import is_command_like, validate_setting_value
from handlers.states import ChatRatingPostEdit
from services import chat_rating_post as crp
from services.chat_tracking import chat_for_city
from core.settings_audit import delete_setting_by_admin, set_setting_by_admin
from core.settings_schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test): every `@router.*` decorator below MUST fit on ONE line.

_WEEKDAY_FULL = {
    "mon": "каждый понедельник", "tue": "каждый вторник", "wed": "каждую среду",
    "thu": "каждый четверг", "fri": "каждую пятницу", "sat": "каждую субботу",
    "sun": "каждое воскресенье",
}
# Поля, которые вводятся текстом: имя в callback -> ключ реестра.
_EDITABLE = {
    "time": crp.KEY_TIME,
    "top": crp.KEY_TOP,
    "title_rules": crp.KEY_TITLE_RULES,
    "title_formula": crp.KEY_TITLE_FORMULA,
    "total": crp.KEY_TOTAL_TITLE,
    "footer": crp.KEY_FOOTER,
}
_EXAMPLES = {
    "time": "Например <code>19:00</code> — время московское.",
    "top": f"Число от {crp.TOP_MIN} до {crp.TOP_MAX}, например <code>10</code>.",
}


def _key(base: str, code: str | None) -> str | None:
    return per_city_key(base, code) if code else base


def _token(code: str | None) -> str:
    return code or _GLOBAL


async def toggle_row(code: str | None, header: str | None) -> list[list[InlineKeyboardButton]]:
    """Строки для экрана «🏆 Рейтинг чата»: тумблер и вход на экран публикации. При «все
    города» (модуль включён, город не выбран) строк нет — пост всегда про один чат."""
    if header and code is None:
        return []
    on = await crp.enabled_for(code)
    token = _token(code)
    return [
        [InlineKeyboardButton(
            text=f"{'✅' if on else '⬜'} 📣 Публиковать рейтинг в чат",
            callback_data=f"chpost:toggle:{token}",
        )],
        [InlineKeyboardButton(text="📣 Когда и что публиковать", callback_data=f"chpost:open:{token}")],
    ]


async def render_post_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    code, header = await _screen_city(admin_id)
    token = _token(code)
    title = "📣 <b>Публикация рейтинга в чат</b>"
    if header:
        title = f"📣 <b>Публикация рейтинга в чат — {html_module.escape(header)}</b>"
    if header and code is None:
        text = (f"{title}\n\nПост уходит в чат одного города — выберите город в шапке админки "
                "(«🏙 Сменить город»).")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏙 Сменить город", callback_data="admin_city_switch:manage")],
            [InlineKeyboardButton(text="⬅️ К рейтингу чата", callback_data="chrate:back")],
        ])
        return text, kb

    on = await crp.enabled_for(code)
    cfg = await crp.post_settings(code)
    chat = await chat_for_city(code)
    hh, mm = cfg["time"]
    chat_line = (
        f"Чат: «{html_module.escape(chat['title'] or 'без названия')}»" if chat
        else "Чат: <b>не привязан</b> — пост не уйдёт, пока бота не добавят в чат делегатов города."
    )
    what = f"топ-{cfg['top']} за прошедшую неделю (пн–вс)"
    if cfg["cumulative"]:
        what += " и таблица «с начала»"
    lines = [
        title, "",
        f"Статус: <b>{'включена' if on else 'выключена'}</b>",
        chat_line,
        f"Когда: {_WEEKDAY_FULL[cfg['weekday']]} в {hh:02d}:{mm:02d} (МСК)",
        f"Что: {what}",
        "",
        "<i>В посте — @ники из Telegram; упомянутые получат уведомление. Кто без ника — в пост "
        "не попадает. Команда (сотрудники и админы чата) не участвует. Тихие часы и «🔕» не "
        "действуют: это одно сообщение в группу, а не личная рассылка.</i>",
    ]
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if on else '⬜'} 📣 Публиковать рейтинг в чат", callback_data=f"chpost:toggle:{token}",
    )]]
    rows.append([
        InlineKeyboardButton(
            text=f"✅ {label}" if day == cfg["weekday"] else label,
            callback_data=f"chpost:day:{token}:{day}",
        )
        for day, label in crp.WEEKDAYS.items()
    ])
    rows.append([InlineKeyboardButton(text=f"🕐 Время: {hh:02d}:{mm:02d}", callback_data=f"chpost:edit:{token}:time")])
    rows.append([InlineKeyboardButton(text=f"🔢 Сколько человек: {cfg['top']}", callback_data=f"chpost:edit:{token}:top")])
    rows.append([InlineKeyboardButton(
        text=f"{'✅' if cfg['cumulative'] else '⬜'} Добавлять таблицу «с начала»",
        callback_data=f"chpost:cum:{token}",
    )])
    rows.append([InlineKeyboardButton(text="✏️ Заголовок (правила города)", callback_data=f"chpost:edit:{token}:title_rules")])
    rows.append([InlineKeyboardButton(text="✏️ Заголовок (формула активности)", callback_data=f"chpost:edit:{token}:title_formula")])
    rows.append([InlineKeyboardButton(text="✏️ Заголовок «с начала»", callback_data=f"chpost:edit:{token}:total")])
    rows.append([InlineKeyboardButton(text="✏️ Подпись внизу", callback_data=f"chpost:edit:{token}:footer")])
    rows.append([InlineKeyboardButton(text="👁 Отправить сейчас (проверка)", callback_data=f"chpost:preview:{token}")])
    rows.append([InlineKeyboardButton(text="⬅️ К рейтингу чата", callback_data="chrate:back")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: types.CallbackQuery) -> None:
    text, kb = await render_post_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("chpost:open:"))
async def chpost_open(callback: types.CallbackQuery, state: FSMContext):
    ok, _code = await _checked_city(callback, callback.data.split(":", 2)[2])
    if not ok:
        return
    await state.clear()
    await _show(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("chpost:toggle:"))
async def chpost_toggle(callback: types.CallbackQuery):
    """Тумблер живёт и на экране рейтинга, и на экране публикации — перерисовываем тот, где
    нажали (у экрана публикации есть кнопка проверки)."""
    ok, code = await _checked_city(callback, callback.data.split(":", 2)[2])
    if not ok:
        return
    on = not await crp.enabled_for(code)
    await set_setting_by_admin(callback.from_user.id, _key(crp.KEY_ENABLED, code), "on" if on else "off")
    await crp.reschedule_soft(code)
    on_post_screen = any(
        (b.callback_data or "").startswith("chpost:preview:")
        for row in getattr(getattr(callback.message, "reply_markup", None), "inline_keyboard", None) or []
        for b in row
    )
    if on_post_screen:
        await _show(callback)
    else:
        from handlers.admin_chat_rating import render_chat_rating_screen
        text, kb = await render_chat_rating_screen(callback.from_user.id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    note = "Публикация включена" if on else "Публикация выключена"
    if on and await chat_for_city(code) is None:
        await callback.answer(
            f"{note}, но чат делегатов города не привязан — пост не уйдёт, пока бота не добавят в чат.",
            show_alert=True,
        )
        return
    await callback.answer(note)


@router.callback_query(F.data.startswith("chpost:day:"))
async def chpost_day(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[3] not in crp.WEEKDAYS:
        await callback.answer("Кнопка устарела — откройте экран заново.", show_alert=True)
        return
    ok, code = await _checked_city(callback, parts[2])
    if not ok:
        return
    await set_setting_by_admin(callback.from_user.id, _key(crp.KEY_WEEKDAY, code), parts[3])
    await crp.reschedule_soft(code)
    await _show(callback)
    await callback.answer(f"Публикуем {_WEEKDAY_FULL[parts[3]]}")


@router.callback_query(F.data.startswith("chpost:cum:"))
async def chpost_cumulative(callback: types.CallbackQuery):
    ok, code = await _checked_city(callback, callback.data.split(":", 2)[2])
    if not ok:
        return
    on = await get_setting_typed_for_city(crp.KEY_CUMULATIVE, code) != "on"
    await set_setting_by_admin(callback.from_user.id, _key(crp.KEY_CUMULATIVE, code), "on" if on else "off")
    await _show(callback)
    await callback.answer("Таблица «с начала» добавлена" if on else "Таблица «с начала» убрана")


@router.callback_query(F.data.startswith("chpost:edit:"))
async def chpost_edit(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[3] not in _EDITABLE:
        await callback.answer("Кнопка устарела — откройте экран заново.", show_alert=True)
        return
    ok, code = await _checked_city(callback, parts[2])
    if not ok:
        return
    name = parts[3]
    base = _EDITABLE[name]
    entry = SETTINGS_SCHEMA[base]
    value, _own = await _raw(base, code)
    if value is None:
        value = entry["default"]
    lines = [
        f"✏️ <b>{html_module.escape(entry['label'])}</b>", "",
        f"Сейчас: <b>{html_module.escape(str(value))}</b>", "",
        _EXAMPLES.get(name) or html_module.escape(entry["prompt"]), "",
        "<i>Пришлите новое значение сообщением. «-» — вернуть значение по умолчанию.</i>",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"chpost:open:{parts[2]}")],
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await state.set_state(ChatRatingPostEdit.waiting_for_value)
    await state.set_data({"chpost_name": name, "chpost_city": code})
    await callback.answer()


@router.message(StateFilter(ChatRatingPostEdit.waiting_for_value))
async def chpost_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name, code = data.get("chpost_name"), data.get("chpost_city")
    if name not in _EDITABLE:
        await state.clear()
        return
    value = (message.text or "").strip()
    if not value or is_command_like(value):
        await message.answer(
            "Не понял — пришлите значение текстом одним сообщением. "
            f"{_EXAMPLES.get(name, '')}\n\nПередумали — «❌ Отмена» под сообщением выше.",
            parse_mode="HTML",
        )
        return
    current, _header = await _screen_city(message.from_user.id)
    if current != code:
        await state.clear()
        await message.answer("Город админки изменился — начните правку заново.")
        return
    key = _key(_EDITABLE[name], code)
    if value == "-":
        await delete_setting_by_admin(message.from_user.id, key)
    else:
        value, error = validate_setting_value(key, value)
        if not error and name == "top" and not (crp.TOP_MIN <= int(value) <= crp.TOP_MAX):
            error = (f"Нужно число от {crp.TOP_MIN} до {crp.TOP_MAX}, например <code>10</code> — "
                     "длиннее пост в чате читать не станут.")
        if error:
            await message.answer(error, parse_mode="HTML")
            return
        await set_setting_by_admin(message.from_user.id, key, value)
    logger.info(f"admin {message.from_user.id} правит настройку {key}")
    await state.clear()
    if name == "time":
        await crp.reschedule_soft(code)
    text, kb = await render_post_screen(message.from_user.id)
    await message.answer("✅ Сохранено\n\n" + text, parse_mode="HTML", reply_markup=kb)


# ── Проверка и ручная публикация ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("chpost:preview:"))
async def chpost_preview(callback: types.CallbackQuery, now: datetime | None = None):
    """Пост приходит менеджеру в личку в том виде, в каком уйдёт в чат. @ники в личке с ботом
    никого не уведомляют. Кнопка публикации — только если чат привязан."""
    token = callback.data.split(":", 2)[2]
    ok, code = await _checked_city(callback, token)
    if not ok:
        return
    text, chat = await crp.build_post(code, now=now)
    if text is None:
        await callback.answer(
            "За прошедшую неделю в рейтинге никого нет (или ни у кого нет @ника) — "
            "публиковать нечего.",
            show_alert=True,
        )
        return
    rows = []
    if chat is not None:
        rows.append([InlineKeyboardButton(text="📣 Опубликовать в чат", callback_data=f"chpost:pub:{token}")])
    await callback.message.answer(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )
    await callback.answer(
        "Так пост будет выглядеть в чате." if chat is not None
        else "Так пост будет выглядеть. Чат города не привязан — опубликовать пока некуда.",
        show_alert=chat is None,
    )


@router.callback_query(F.data.startswith("chpost:pub:"))
async def chpost_publish_ask(callback: types.CallbackQuery):
    token = callback.data.split(":", 2)[2]
    ok, code = await _checked_city(callback, token)
    if not ok:
        return
    chat = await chat_for_city(code)
    if chat is None:
        await callback.answer("Чат делегатов города не привязан — публиковать некуда.", show_alert=True)
        return
    title = html_module.escape(chat["title"] or "без названия")
    text = (
        f"Опубликовать рейтинг в чат «{title}»?\n\n"
        "Пост увидят все участники чата, упомянутые по @нику получат уведомление. Убрать его "
        "потом можно только вручную, удалив сообщение в чате. Данные пересчитаются в момент "
        "публикации."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, опубликовать", callback_data=f"chpost:go:{token}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="chpost:no")],
    ])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "chpost:no")
async def chpost_publish_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Публикация отменена — в чат ничего не ушло.")
    await callback.answer()


# Двойное нажатие «✅ Да, опубликовать» давало два поста с @-упоминаниями (убрать — только
# руками в чате). Процессная защита: город в работе и уже отработанные экраны подтверждения
# (chat_id, message_id) — запоздавший колбэк того же экрана второй раз не публикует.
_publishing: set = set()
_published_messages: set = set()


@router.callback_query(F.data.startswith("chpost:go:"))
async def chpost_publish_go(callback: types.CallbackQuery, bot: Bot, now: datetime | None = None):
    ok, code = await _checked_city(callback, callback.data.split(":", 2)[2])
    if not ok:
        return
    msg = callback.message
    msg_key = (getattr(getattr(msg, "chat", None), "id", None), getattr(msg, "message_id", None))
    if code in _publishing or (msg_key[1] is not None and msg_key in _published_messages):
        await callback.answer("Публикую — второй раз нажимать не нужно.")
        return
    _publishing.add(code)
    if msg_key[1] is not None:
        _published_messages.add(msg_key)
    try:
        await msg.edit_text("⏳ Публикую…")  # кнопки подтверждения исчезают сразу
    except Exception:
        pass
    try:
        status, chat = await crp.publish(code, bot, now=now)
    finally:
        _publishing.discard(code)
    messages = {
        "ok": "✅ Опубликовано в чат «{title}».",
        "not_bound": "Чат делегатов города не привязан — публиковать некуда. Добавьте бота в чат "
                     "делегатов и привяжите его к городу.",
        "empty": "За прошедшую неделю в рейтинге никого нет — публиковать нечего.",
        "send_failed": "Не получилось отправить в чат «{title}»: бот не может там писать. Проверьте, "
                       "что бот состоит в чате и ему разрешено отправлять сообщения.",
    }
    title = html_module.escape((chat or {}).get("title") or "без названия")
    await callback.message.edit_text(messages[status].format(title=title), parse_mode="HTML")
    if status == "ok":
        logger.info(f"admin {callback.from_user.id} опубликовал рейтинг в чат {chat['chat_id']}")
    await callback.answer()
