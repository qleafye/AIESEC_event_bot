"""Мастер «➕ Новый опрос» (раздел «📊 Опросы»): вопрос → варианты → тумблеры → аудитория →
превью → отправить сейчас / запланировать.

Шов на общий `admin.router`; импортируется из хвоста handlers/admin_polls.py. Право —
`broadcast` ("state:PollCreate:*" + poll_* ключи в handlers/admin_caps.py).

По правилу «бот для людей»: текстом вводятся только вопрос, варианты и дата; всё остальное —
кнопки. Ошибка всегда говорит, что сделать. Лимиты Bot API (вопрос ≤300, вариант ≤100,
2–10 вариантов) проверяются здесь, до отправки.

INVARIANT (13-01 cap-test): каждый декоратор `@router.*` — в ОДНУ строку.
"""
import html as html_module
import logging

from aiogram import F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.db import create_poll, count_and_list_filtered, get_distinct_filter_values
from services.polls import (
    POLL_QUESTION_MAX,
    POLL_OPTION_MAX,
    POLL_OPTIONS_MIN,
    POLL_OPTIONS_MAX,
    deliver_poll,
    audience_label,
)
from services.scheduler import (
    _parse_schedule_dt,
    _fmt_dt,
    _now_moscow_naive,
    schedule_poll_job,
)
from services.background import spawn as _spawn
from keyboards.builders import get_cancel_kb
from cities import cities_module_on, city_label, city_scope, enabled_cities
from handlers.states import PollCreate
from handlers.admin_core import _admin_city_view
from handlers.admin_broadcasts import _TRACK_LABELS
from handlers.admin import router

logger = logging.getLogger(__name__)

_OPTIONS_SEP = ";"  # «Enter = отправить» на телефоне: варианты можно прислать одной строкой


def split_options(text: str) -> list[str]:
    """Одно сообщение → список вариантов: по строкам И по «;», пустые выбрасываются."""
    parts = []
    for line in (text or "").splitlines():
        for piece in line.split(_OPTIONS_SEP):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return parts


def _options_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово", callback_data="poll_opts_done")],
    ])


def _options_status(options: list[str]) -> str:
    listed = "\n".join(f"{i + 1}. {html_module.escape(o)}" for i, o in enumerate(options))
    return (
        f"Вариантов: {len(options)} из {POLL_OPTIONS_MAX}.\n{listed}\n\n"
        "Пришлите следующий вариант или нажмите «✅ Готово»."
    )


def _settings_kb(anon: bool, multi: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'🟢' if anon else '⚪'} Анонимный опрос", callback_data="poll_tg_anon")],
        [InlineKeyboardButton(text=f"{'🟢' if multi else '⚪'} Несколько вариантов", callback_data="poll_tg_multi")],
        [InlineKeyboardButton(text="Дальше ➡️", callback_data="poll_settings_next")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="poll_cancel")],
    ])


def _settings_text(anon: bool) -> str:
    text = (
        "⚙️ <b>Настройки опроса</b>\n\n"
        "• <b>Анонимный</b> — делегаты не видят, кто как голосовал.\n"
        "• <b>Несколько вариантов</b> — можно выбрать больше одного.\n"
    )
    if anon:
        text += (
            "\n⚠️ В анонимном опросе Telegram не сообщает боту, кто голосовал: "
            "ответы по людям будут недоступны, в карточке и таблице — только итоги по вариантам."
        )
    return text


async def _audience_kb(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    scope, city_text = await _admin_city_view(admin_id)
    rows = [
        [InlineKeyboardButton(text="👥 Все делегаты", callback_data="poll_aud:all")],
        [InlineKeyboardButton(text="✅ Только одобренные", callback_data="poll_aud:approved")],
    ]
    if scope is None and await cities_module_on():
        for c in await enabled_cities():
            rows.append([InlineKeyboardButton(
                text=f"🏙 {await city_label(c['code'])}", callback_data=f"poll_aud:city:{c['code']}",
            )])
    for code in await get_distinct_filter_values("participant_type"):
        rows.append([InlineKeyboardButton(
            text=f"🎟 Трек: {_TRACK_LABELS.get(code, code)}", callback_data=f"poll_aud:track:{code}",
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="poll_cancel")])
    text = "👥 <b>Кому отправить опрос?</b>"
    if scope is not None:
        text += f"\nГород из шапки: {html_module.escape(city_text or scope[0])} — опрос уйдёт только ему."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _city_filter(code: str) -> dict:
    scope = city_scope(code)
    return {
        "field": "event_city", "value": code,
        "exclude": list(scope[1]) if scope else [],
        "label": await city_label(code),
    }


async def build_audience_spec(choice: str, admin_id: int) -> tuple[list[dict], str | None]:
    """(filter_spec, city_code) по кнопке аудитории с учётом города из шапки."""
    scope, _ = await _admin_city_view(admin_id)
    spec: list[dict] = []
    city: str | None = None
    if choice == "approved":
        spec.append({"field": "status", "value": "approved"})
    elif choice.startswith("track:"):
        code = choice[len("track:"):]
        spec.append({"field": "participant_type", "value": code, "label": _TRACK_LABELS.get(code, code)})
    elif choice.startswith("city:"):
        city = choice[len("city:"):]
    if scope is not None:
        city = scope[0]
    if city:
        spec.append(await _city_filter(city))
    return spec, city


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Отправить сейчас", callback_data="poll_send_now")],
        [InlineKeyboardButton(text="🕒 Запланировать", callback_data="poll_schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="poll_cancel")],
    ])


# ── шаги ─────────────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "poll_new")
async def poll_new(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PollCreate.question)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"📊 <b>Новый опрос</b>\n\nШаг 1 из 4. Напишите вопрос (до {POLL_QUESTION_MAX} символов).\n"
        "Например: <i>Во сколько тебе удобнее начать первый день?</i>",
        parse_mode="HTML", reply_markup=get_cancel_kb(),
    )


@router.message(StateFilter(PollCreate), Command("cancel"))
@router.message(StateFilter(PollCreate), F.text == "Отмена")
async def poll_wizard_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Создание опроса отменено.", reply_markup=ReplyKeyboardRemove())


@router.callback_query(F.data == "poll_cancel")
async def poll_cancel_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text("Создание опроса отменено. Открыть раздел: /admin → 📊 Опросы")
    except Exception:
        await callback.message.answer("Создание опроса отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(PollCreate.question)
async def poll_question_step(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст вопроса — пришлите его сообщением.")
        return
    if len(text) > POLL_QUESTION_MAX:
        await message.answer(
            f"Слишком длинно: {len(text)} символов, Telegram разрешает до {POLL_QUESTION_MAX}. "
            "Сократите и пришлите ещё раз."
        )
        return
    await state.update_data(question=text, options=[], anon=False, multi=False)
    await state.set_state(PollCreate.options)
    await message.answer(
        f"Шаг 2 из 4. Пришлите варианты ответа — по одному сообщением или сразу несколько "
        f"через «{_OPTIONS_SEP}» (например: <i>10:00; 11:00; 12:00</i>).\n"
        f"От {POLL_OPTIONS_MIN} до {POLL_OPTIONS_MAX} вариантов, каждый до {POLL_OPTION_MAX} символов. "
        "Когда закончите — «✅ Готово».",
        parse_mode="HTML", reply_markup=get_cancel_kb(),
    )


@router.message(PollCreate.options)
async def poll_options_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    options: list[str] = list(data.get("options") or [])
    new = split_options(message.text or "")
    if not new:
        await message.answer("Нужен текст варианта — пришлите его сообщением.")
        return
    too_long = [o for o in new if len(o) > POLL_OPTION_MAX]
    if too_long:
        await message.answer(
            f"Вариант «{html_module.escape(too_long[0][:40])}…» длиннее {POLL_OPTION_MAX} символов — "
            "сократите и пришлите ещё раз.", parse_mode="HTML",
        )
        return
    if len(options) + len(new) > POLL_OPTIONS_MAX:
        await message.answer(
            f"Получится {len(options) + len(new)} вариантов, а Telegram разрешает максимум "
            f"{POLL_OPTIONS_MAX}. Уже есть {len(options)} — добавьте не больше "
            f"{POLL_OPTIONS_MAX - len(options)} или нажмите «✅ Готово».",
            reply_markup=_options_kb(),
        )
        return
    options.extend(new)
    await state.update_data(options=options)
    await message.answer(_options_status(options), parse_mode="HTML", reply_markup=_options_kb())


@router.callback_query(F.data == "poll_opts_done", PollCreate.options)
async def poll_options_done(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    options = data.get("options") or []
    if len(options) < POLL_OPTIONS_MIN:
        await callback.answer(
            f"Нужно хотя бы {POLL_OPTIONS_MIN} варианта — пришлите ещё {POLL_OPTIONS_MIN - len(options)}.",
            show_alert=True,
        )
        return
    await state.set_state(PollCreate.settings)
    await callback.answer()
    await callback.message.edit_text(
        _settings_text(False), parse_mode="HTML", reply_markup=_settings_kb(False, False)
    )


@router.callback_query(F.data.in_({"poll_tg_anon", "poll_tg_multi"}), PollCreate.settings)
async def poll_toggle_setting(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    anon, multi = bool(data.get("anon")), bool(data.get("multi"))
    if callback.data == "poll_tg_anon":
        anon = not anon
    else:
        multi = not multi
    await state.update_data(anon=anon, multi=multi)
    await callback.answer()
    await callback.message.edit_text(
        _settings_text(anon), parse_mode="HTML", reply_markup=_settings_kb(anon, multi)
    )


@router.callback_query(F.data == "poll_settings_next", PollCreate.settings)
async def poll_settings_next(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PollCreate.audience)
    text, kb = await _audience_kb(callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text("Шаг 3 из 4. " + text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("poll_aud:"), PollCreate.audience)
async def poll_audience_pick(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    choice = callback.data[len("poll_aud:"):]
    spec, city = await build_audience_spec(choice, callback.from_user.id)
    ids = await count_and_list_filtered(spec) if spec else None
    count = len(ids) if ids is not None else None
    data = await state.get_data()
    await state.update_data(audience=spec, city=city)
    await state.set_state(PollCreate.confirm)
    await callback.answer()
    # Реальное превью: закрытый опрос в чат менеджера — ровно то, что увидит делегат.
    await bot.send_poll(
        callback.from_user.id,
        question=data["question"],
        options=list(data["options"]),
        is_anonymous=bool(data.get("anon")),
        allows_multiple_answers=bool(data.get("multi")),
        is_closed=True,
    )
    if count is None:
        from database.db import get_all_users_ids
        count = len(await get_all_users_ids())
    await callback.message.answer(
        "Шаг 4 из 4. Выше — как опрос увидит делегат (превью закрыто, голосовать нельзя).\n\n"
        f"Кому: <b>{html_module.escape(audience_label(spec))}</b> — {count} чел.\n"
        "Отправить сейчас или запланировать?",
        parse_mode="HTML", reply_markup=_confirm_kb(),
    )


async def _create_from_state(data: dict, admin_id: int, when_str: str) -> int:
    return await create_poll(
        data["question"], list(data["options"]),
        is_anonymous=bool(data.get("anon")), allows_multiple=bool(data.get("multi")),
        created_by=admin_id, city=data.get("city"), audience=data.get("audience") or [],
        scheduled_at=when_str,
    )


async def _deliver_and_report(bot, poll_id: int, admin_id: int):
    stats = await deliver_poll(bot, poll_id)
    if stats is None:
        text = f"⚠️ Опрос #{poll_id}: отправка прервалась — бот дошлёт остаток после перезапуска."
    else:
        text = (
            f"📊 Опрос #{poll_id} отправлен.\n✅ Доставлено: {stats['sent']}\n"
            f"❌ Недоступно: {stats['failed']}\nИтоги — в «📊 Опросы»."
        )
    try:
        await bot.send_message(admin_id, text)
    except Exception as e:
        logger.warning("poll %s report to %s failed: %s", poll_id, admin_id, e)


@router.callback_query(F.data == "poll_send_now", PollCreate.confirm)
async def poll_send_now(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("question"):
        await callback.answer("Сессия истекла — начните заново из «📊 Опросы».", show_alert=True)
        await state.clear()
        return
    pid = await _create_from_state(data, callback.from_user.id, _fmt_dt(_now_moscow_naive()))
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(f"🚀 Отправляю опрос #{pid}… Сообщу, когда закончу.")
    _spawn(_deliver_and_report(bot, pid, callback.from_user.id))


@router.callback_query(F.data == "poll_schedule", PollCreate.confirm)
async def poll_schedule_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PollCreate.schedule_when)
    await callback.answer()
    await callback.message.edit_text(
        "🕒 Когда отправить? Введите дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (по Москве)\n"
        "Например: 01.07.2026 14:30",
        parse_mode="HTML",
    )


@router.message(PollCreate.schedule_when)
async def poll_schedule_when(message: types.Message, state: FSMContext):
    when = _parse_schedule_dt(message.text)
    if when is None:
        await message.answer("❌ Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30)")
        return
    if when <= _now_moscow_naive():
        await message.answer("❌ Это время уже прошло. Введите будущую дату.")
        return
    data = await state.get_data()
    if not data.get("question"):
        await message.answer("Сессия истекла — начните заново из «📊 Опросы».")
        await state.clear()
        return
    pid = await _create_from_state(data, message.from_user.id, _fmt_dt(when))
    schedule_poll_job(pid, when)
    await state.clear()
    await message.answer(
        f"✅ Опрос #{pid} запланирован на {when.strftime('%d.%m.%Y %H:%M')} "
        f"({audience_label(data.get('audience'))}).\nОтменить можно в карточке опроса: «📊 Опросы».",
        reply_markup=ReplyKeyboardRemove(),
    )
