"""Форум-ночь п.9 (идея №15 бэклога чек-ина, D-24 `.planning/FORUM-CHECKIN.md`): «⭐ Отзыв о
сессии одним тапом» — делегатская сторона (оценка 1–5 + необязательный комментарий, только
отмеченным на сессии) И менеджерская сторона (статистика в карточке сессии, экран «📊 Оценки
сессий» дня, список комментариев).

Форма шва — своего `Router()` нет, но, В ОТЛИЧИЕ от соседних одно-адресных швов (program.py —
только делегат, admin_program.py — только менеджер), этот модуль декорирует ОБА общих роутера:
`handlers.admin.router` (статистика/комментарии менеджеру, callback-пространство `prog_fb*` —
переиспользует УЖЕ существующую capability-запись `"prog_*": "settings"`, handlers/access/admin_caps.py,
второй записи не заводится) И `handlers.user_actions.router` (делегатская оценка). Импортирован
как голое имя `router` (не `admin_router`) — `tests/test_roles_phase8.py::_decorator_lines`
ищет декораторы ПО ТЕКСТУ `"@router."`, псевдоним сделал бы менеджерские хендлеры невидимыми
для сторожа полноты капы; делегатский роутер поэтому — `delegate_router` (обратный псевдоним,
не наоборот, capability-скан НЕ смотрит на `user_actions.router` вовсе). Домен (планирование
джоб, идемпотентная рассылка, агрегаты) — целиком в `services/session_feedback.py`, здесь
только FSM-шаги/callback'и и точки отправки.

Импортирован ХВОСТОМ `handlers/user_actions.py` (сразу после `sos_handlers`, перед фолбэк-
хендлером `reg_handoff_idle_fallback`) — тот же приём, что `program`/`sos` выше; этого же
импорта достаточно, чтобы зарегистрировать И admin-часть (Python кеширует модуль после первого
импорта, `handlers.admin.router` к этому моменту уже существует — тот же порядок, что доказан
`admin_program.py`).

Тексты делегатской стороны — через `reg_i18n.say`/`reg_i18n.tr_text` (тот же перевод, что
остальные экраны делегатского чата); литерал алерта «Эта оценка тебе недоступна.»
зарегистрирован в `services/i18n/i18n_sources.py::code_literals()` (сторож
`tests/test_i18n_literal_corpus_guard_260906.py`, SCANNED_FILES дополнен этим модулем)."""
import html as html_module
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import cities_module_on, city_label, get_setting_typed_for_city, per_city_key
from database.db import get_program_session
from handlers.i18n import reg_i18n
from handlers.admin import router
from handlers.forum.admin_program import _CITY_FORBIDDEN_ALERT, _city_allowed
from handlers.states import EditSetting, SessionFeedbackComment
from handlers.user_actions import router as delegate_router
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import get_setting_typed
from services import session_feedback as sf

logger = logging.getLogger(__name__)

_UNAVAILABLE_ALERT = "Эта оценка тебе недоступна."


# ══════════════════════════════════════════════════════════════════════════════════════════
# Делегатская сторона — оценка + комментарий
# ══════════════════════════════════════════════════════════════════════════════════════════

@delegate_router.callback_query(F.data.startswith("sfb:r:"))
async def sfb_rate(callback: types.CallbackQuery):
    _, _, session_id_s, rating_s = callback.data.split(":", 3)
    session_id, rating = int(session_id_s), int(rating_s)
    lang, tr_map = await reg_i18n.ctx_for(callback)
    ok = await sf.record_rating(callback.from_user.id, session_id, rating)
    if not ok:
        await callback.answer(reg_i18n.tr_text(_UNAVAILABLE_ALERT, lang, tr_map), show_alert=True)
        return
    await callback.answer()
    thanks = reg_i18n.tr_text(
        await _setting_or("session_feedback_thanks_text", "Спасибо! Хочешь добавить пару слов?"),
        lang, tr_map,
    )
    try:
        await callback.message.edit_text(
            thanks, reply_markup=sf.comment_offer_keyboard(session_id, lang, tr_map),
        )
    except Exception as e:
        logger.info("sfb_rate: edit_text failed for session=%s: %s", session_id, e)


@delegate_router.callback_query(F.data.startswith("sfb:c:"))
async def sfb_offer_comment(callback: types.CallbackQuery, state: FSMContext):
    session_id = int(callback.data.split(":", 2)[2])
    lang, tr_map = await reg_i18n.ctx_for(callback)
    if not await _is_marked(callback.from_user.id, session_id):
        await callback.answer(reg_i18n.tr_text(_UNAVAILABLE_ALERT, lang, tr_map), show_alert=True)
        return
    await state.set_state(SessionFeedbackComment.waiting)
    await state.update_data(sfb_session_id=session_id)
    await callback.answer()
    hint = reg_i18n.tr_text(
        await _setting_or(
            "session_feedback_comment_hint_text", "✍️ Напиши комментарий следующим сообщением.",
        ),
        lang, tr_map,
    )
    try:
        await callback.message.edit_text(hint)
    except Exception as e:
        logger.info("sfb_offer_comment: edit_text failed for session=%s: %s", session_id, e)


@delegate_router.message(SessionFeedbackComment.waiting)
async def sfb_comment_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    session_id = data.get("sfb_session_id")
    text = (message.text or message.caption or "").strip()
    if session_id is None or not text:
        return
    ok = await sf.record_comment(message.from_user.id, int(session_id), text)
    if not ok:
        return  # заявка устарела/делегат больше не отмечен — тихо, без ошибки на пустом месте
    await reg_i18n.say(
        message, await _setting_or("session_feedback_comment_saved_text", "Спасибо, записал!"),
    )


async def _is_marked(telegram_id: int, session_id: int) -> bool:
    from database.db import is_marked_for_session
    return await is_marked_for_session(telegram_id, session_id)


async def _setting_or(key: str, default: str) -> str:
    from domain.settings.schema import get_setting_typed
    return await get_setting_typed(key) or default


# ══════════════════════════════════════════════════════════════════════════════════════════
# Менеджерская сторона — «📊 Оценки сессий» дня + «💬 Комментарии» сессии
# ══════════════════════════════════════════════════════════════════════════════════════════

_COMMENTS_PAGE_SIZE = 10


async def render_day_feedback_screen(code: str, day: str) -> tuple[str, InlineKeyboardMarkup]:
    from services.program import day_label, format_time_range

    rows = await sf.day_stats(code, day)
    lines = [f"📊 <b>Оценки сессий</b> — {day_label(day)}", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if not rows:
        lines.append("Сессий пока нет.")
    for row in rows:
        time_part = format_time_range(row["start_time"], row["end_time"])
        marked = row["marked_count"]
        line = f"{time_part} — {row['title']} · {sf.stats_line(row['stats'])} · отмечено {marked}"
        lines.append(line)
        buttons.append([
            InlineKeyboardButton(text=f"{time_part} {row['title']}"[:60], callback_data=f"prog_v:{row['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text="← К дню", callback_data=f"prog_day:{code}:{day}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_fbday:"))
async def prog_fbday_open(callback: types.CallbackQuery):
    _, code, day = callback.data.split(":", 2)
    text, kb = await render_day_feedback_screen(code, day)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def render_comments_screen(session_id: int, page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    session = await get_program_session(session_id)
    if session is None:
        return None
    rows, total = await sf.comments_page(session_id, page=page, page_size=_COMMENTS_PAGE_SIZE)
    lines = [f"💬 <b>Комментарии</b> — {session['title']}", ""]
    if not rows:
        lines.append("Комментариев пока нет.")
    for row in rows:
        stars = "⭐" * (row.get("rating") or 0)
        lines.append(f"{stars}\n«{row['comment']}»")
        lines.append("")
    buttons: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Раньше", callback_data=f"prog_fbc:{session_id}:{page - 1}"))
    if (page + 1) * _COMMENTS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Позже →", callback_data=f"prog_fbc:{session_id}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="← К сессии", callback_data=f"prog_v:{session_id}")])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_fbc:"))
async def prog_fbc_open(callback: types.CallbackQuery):
    _, session_id_s, page_s = callback.data.split(":", 2)
    screen = await render_comments_screen(int(session_id_s), int(page_s))
    if screen is None:
        await callback.answer("Сессия больше недоступна.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревью 24.09 (аудит ключей после 8c0d8af): «⭐ Отзывы о сессиях» — тумблер/задержка/четыре
# текста жили ТОЛЬКО в Mini App (на проде выключен, CLAUDE.md — правится в самом боте). Право —
# то же, что у «🗓 Программа форума» (`_city_allowed` — TOCTOU-перепроверка города экрана,
# переиспользуем оттуда, не заводим второй копии). Тумблер и задержка — per_city (когда модуль
# городов включён); четыре текста — глобальные (та же группа «сосед», что и три текста SOS
# выше, D-24: одна формулировка на все города, экран лишь показывает, откуда её править).
# ══════════════════════════════════════════════════════════════════════════════════════════

_FEEDBACK_DELAY_PRESETS = (5, 10, 15, 30)

# (setting_key, подпись кнопки) — правка через ОБЩИЙ EditSetting.waiting_for_value.
_FEEDBACK_TEXT_FIELDS = {
    "prompt": ("session_feedback_prompt_text", "❓ Вопрос под оценкой"),
    "thanks": ("session_feedback_thanks_text", "🙏 Спасибо после оценки"),
    "hint": ("session_feedback_comment_hint_text", "✍️ Приглашение к комментарию"),
    "saved": ("session_feedback_comment_saved_text", "✅ Комментарий сохранён"),
}


async def render_feedback_settings_screen(code: str) -> tuple[str, InlineKeyboardMarkup]:
    label = await city_label(code)
    per_city_ctx = await cities_module_on()
    enabled = await sf.is_enabled_for_city(code)
    delay_raw = await get_setting_typed_for_city("session_feedback_delay_minutes", code)
    try:
        delay = int(delay_raw) if delay_raw else sf.DEFAULT_DELAY_MINUTES
    except (TypeError, ValueError):
        delay = sf.DEFAULT_DELAY_MINUTES

    lines = [f"⭐ <b>Отзывы о сессиях</b> — {html_module.escape(label)}", ""]
    lines.append("Через сколько минут после конца сессии делегатам, отмеченным на ней, уходит просьба оценить её.")
    lines.append("")
    lines.append(f"Присылать отзыв: {'✅ Вкл' if enabled else '❌ Выкл'}")
    lines.append(f"Задержка: {delay} мин")
    if not per_city_ctx:
        lines.append("")
        lines.append("<i>Модуль городов выключен — тумблер и задержка общие для события.</i>")
    lines.append("")
    lines.append("<i>Тексты ниже общие для всех городов.</i>")

    buttons: list[list[InlineKeyboardButton]] = []
    toggle_label = "✅ Вкл → ❌ Выкл" if enabled else "❌ Выкл → ✅ Вкл"
    buttons.append([InlineKeyboardButton(text=f"Присылать отзыв: {toggle_label}", callback_data=f"prog_fbtoggle:{code}")])

    delay_row = [
        InlineKeyboardButton(
            text=f"{'• ' if delay == n else ''}{n} мин", callback_data=f"prog_fbdelay:{code}:{n}",
        )
        for n in _FEEDBACK_DELAY_PRESETS
    ]
    buttons.append(delay_row)
    other_mark = "• " if delay not in _FEEDBACK_DELAY_PRESETS else ""
    buttons.append([InlineKeyboardButton(text=f"{other_mark}✏️ Другое", callback_data=f"prog_fbdelay_custom:{code}")])

    for field_key, (_setting_key, field_label) in _FEEDBACK_TEXT_FIELDS.items():
        buttons.append([InlineKeyboardButton(text=f"✏️ {field_label}", callback_data=f"prog_fbtext:{field_key}")])

    buttons.append([InlineKeyboardButton(text="← К программе", callback_data=f"prog_city:{code}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_fbset:"))
async def prog_fbset_open(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await render_feedback_settings_screen(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _feedback_settings_key(base_key: str, code: str) -> tuple[str | None, bool]:
    """`(ключ_для_записи, per_city_ctx)` — модуль городов выключен -> голый глобальный ключ
    (T-093-02: переопределение физически не может подействовать, пока модуль выключен —
    `cities.get_setting_typed_for_city` сам схлопывает чтение к глобальному значению, писать
    в композитный ключ в этом состоянии значило бы молча терять правку)."""
    if not await cities_module_on():
        return base_key, False
    return per_city_key(base_key, code), True


@router.callback_query(F.data.startswith("prog_fbtoggle:"))
async def prog_fbtoggle(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key, per_city_ctx = await _feedback_settings_key("session_feedback_enabled", code)
    if key is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    new_val = "off" if await sf.is_enabled_for_city(code) else "on"
    await set_setting_by_admin(callback.from_user.id, key, new_val)
    await (sf.reconcile_city(code) if per_city_ctx else sf.reconcile_all())
    text, kb = await render_feedback_settings_screen(code)
    from handlers.forum.admin_forum_hub_nav import keep_hub_back  # открыт из хаба форума — «Назад» в хаб
    kb = keep_hub_back(callback.message, kb)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Сохранено.")


@router.callback_query(F.data.startswith("prog_fbdelay:"))
async def prog_fbdelay(callback: types.CallbackQuery):
    _, code, value_s = callback.data.split(":", 2)
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    try:
        value = int(value_s)
    except ValueError:
        await callback.answer("Некорректное значение", show_alert=True)
        return
    key, per_city_ctx = await _feedback_settings_key("session_feedback_delay_minutes", code)
    if key is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, key, str(value))
    await (sf.reconcile_city(code) if per_city_ctx else sf.reconcile_all())
    text, kb = await render_feedback_settings_screen(code)
    from handlers.forum.admin_forum_hub_nav import keep_hub_back  # открыт из хаба форума — «Назад» в хаб
    kb = keep_hub_back(callback.message, kb)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Сохранено.")


@router.callback_query(F.data.startswith("prog_fbdelay_custom:"))
async def prog_fbdelay_custom_start(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key, _per_city_ctx = await _feedback_settings_key("session_feedback_delay_minutes", code)
    back = {"return_cb": f"prog_fbset:{code}", "return_label": "← К отзывам о сессиях"}
    if key is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": key, **back})
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]])
    await callback.answer()
    await callback.message.answer(
        "Через сколько минут после конца сессии присылать отзыв? Пришлите число, например "
        "<code>20</code>.",
        parse_mode="HTML", reply_markup=kb,
    )


@router.callback_query(F.data.startswith("prog_fbtext:"))
async def prog_fbtext_edit(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    entry = _FEEDBACK_TEXT_FIELDS.get(field)
    if entry is None:
        await callback.answer("Неизвестный текст", show_alert=True)
        return
    key, label = entry
    current = await get_setting_typed(key)
    text = f"✏️ <b>{label}</b>\n\n"
    if current:
        text += f"Сейчас: <b>{html_module.escape(str(current))}</b>\n\n"
    else:
        text += "Сейчас: <i>стандартный</i>\n\n"
    text += "Пришли новый текст одним сообщением — общий для всех городов."
    text += "\n\n<i>«-» — вернуть стандартный текст.</i>"

    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": key})
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()
