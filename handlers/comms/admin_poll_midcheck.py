"""Аудитории мидчека в общем шаге «Кому отправить» мастера опросов.

Кнопки «пришли сегодня» и «отмечены на этой сессии» собирают уже существующую
спеку фильтра. Пустая спека в опросах значит «все делегаты», поэтому ноль людей
здесь не записывается как []: мастер остаётся на шаге выбора.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import (
    CHECKIN_DAY_TODAY,
    CHECKIN_YES,
    SESSION_ATTENDED,
    count_and_list_filtered,
    get_all_users_ids,
    get_program_session,
    list_program_days_for_city,
)
from domain.cities import cities_module_on, city_label, default_city_code, enabled_cities
from services.forum.program import day_label, session_point_label, sessions_for_city_day
from services.infra.timeutil import msk_now

_NONE_TODAY = (
    "Сегодня на входе ещё никого нет — отправлять некому. "
    "Всем подряд опрос не уйдёт."
)
_NONE_SESSION = (
    "На этой сессии пока никто не отмечен — отправлять некому. "
    "Всем подряд опрос не уйдёт."
)
_NO_PROGRAM = (
    "В программе этого города сессий нет — выбрать некого. "
    "Всем подряд опрос не уйдёт."
)
_OTHER_CITY = "Эта сессия другого города — выбрать её нельзя."
_NO_SUCH_CITY = "Такого города нет — выберите город кнопкой."
_UNKNOWN = "Не понял выбор — нажмите кнопку ещё раз."
_GONE = "Этой сессии уже нет — выберите другую."
_PICK_CITY = "Выберите город — опрос уйдёт только ему."
_PICK_DAY = "На сегодня сессий нет. Выберите день."
_PICK_SESSION = "Какая сессия? Опрос получат те, кто на ней отмечен."


def recount_note(spec) -> str:
    fields = {item.get("field") for item in (spec or [])}
    if fields & {"checkin_entry", "checkin_session"}:
        return (
            "К моменту отправки список пересчитается: "
            "попадут те, кто будет отмечен к этому времени.\n"
        )
    return ""


def _result(kind, *, spec=None, city=None, ids=None, text="", markup=None):
    return {
        "kind": kind, "spec": spec, "city": city, "ids": ids, "text": text, "markup": markup,
    }


def _with_back(rows):
    rows = [*rows, [InlineKeyboardButton(text="⬅️ Назад", callback_data="poll_aud:back")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _survey_markup(markup, draft_id):
    rows = []
    for row in markup.inline_keyboard:
        kept = [button for button in row if button.callback_data != "poll_cancel"]
        if kept:
            rows.append(kept)
    rows.append([InlineKeyboardButton(text="⬅️ К черновику", callback_data=f"psv_draft:{draft_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _present(raw, surface, draft_id):
    text, markup = raw["text"], raw["markup"]
    if surface == "survey":
        text = (text or "").replace("опрос", "анкету")
        if markup is not None:
            markup = _survey_markup(markup, draft_id)
    return {**raw, "text": text, "markup": markup}


async def _wizard():
    from handlers.comms import admin_poll_wizard as wiz
    return wiz


async def _main(admin_id):
    wiz = await _wizard()
    _text, markup = await wiz._audience_kb(admin_id)
    return markup


async def _refuse(admin_id, text):
    return _result("refuse", text=text, markup=await _main(admin_id))


async def _lock_city(scope, explicit, state):
    """Город шапки важнее кода в кнопке. Без шапки и при включённых городах город
    обязателен: иначе спека без event_city ушла бы всем городам сразу."""
    if scope is not None:
        return "ok", scope[0]
    if not await cities_module_on():
        return "ok", None
    data = await state.get_data()
    code = explicit or data.get("mid_city") or None
    if not code:
        return "ask", None
    enabled = {city["code"] for city in await enabled_cities()}
    if code not in enabled:
        return "bad", None
    await state.update_data(mid_city=code)
    return "ok", code


async def _ask_city(prefix):
    rows = []
    for city in await enabled_cities():
        code = city["code"]
        rows.append([InlineKeyboardButton(
            text=await city_label(code), callback_data=f"poll_aud:{prefix}:{code}",
        )])
    return _result("screen", text=_PICK_CITY, markup=_with_back(rows))


async def _apply_city(spec, city):
    if not city:
        return spec
    wiz = await _wizard()
    return [*spec, await wiz._city_filter(city)]


def _came_today():
    return {
        "field": "checkin_entry",
        "value": CHECKIN_YES,
        "day": CHECKIN_DAY_TODAY,
        "label": "пришли сегодня",
    }


async def _people_or_refuse(admin_id, spec, city, empty_text):
    ids = await count_and_list_filtered(spec)
    if not ids:
        return await _refuse(admin_id, empty_text)
    return _result("ready", spec=spec, city=city, ids=ids)


async def _program_city(city):
    return city or default_city_code()


def _session_rows(sessions):
    return [[InlineKeyboardButton(
        text=session_point_label(session),
        callback_data=f"poll_aud:sess:{int(session['id'])}",
    )] for session in sessions]


async def _show_program(city, day, *, other_days):
    program_city = await _program_city(city)
    sessions = await sessions_for_city_day(program_city, day)
    if sessions:
        return _result(
            "screen", city=city, text=_PICK_SESSION,
            markup=_with_back(_session_rows(sessions)),
        )
    if other_days:
        days = [item for item in await list_program_days_for_city(program_city) if item != day]
        if days:
            rows = [[InlineKeyboardButton(
                text=day_label(item), callback_data=f"poll_aud:sessday:{item}",
            )] for item in days]
            return _result("screen", city=city, text=_PICK_DAY, markup=_with_back(rows))
    return None


async def _today(admin_id, state, scope, choice):
    explicit = choice.split(":", 1)[1] if choice.startswith("today:") else None
    status, city = await _lock_city(scope, explicit, state)
    if status == "ask":
        return await _ask_city("today")
    if status == "bad":
        return await _refuse(admin_id, _NO_SUCH_CITY)
    spec = await _apply_city([_came_today()], city)
    return await _people_or_refuse(admin_id, spec, city, _NONE_TODAY)


async def _open_sessions(admin_id, state, scope, explicit):
    status, city = await _lock_city(scope, explicit, state)
    if status == "ask":
        return await _ask_city("sesscity")
    if status == "bad":
        return await _refuse(admin_id, _NO_SUCH_CITY)
    shown = await _show_program(city, msk_now().strftime("%Y-%m-%d"), other_days=True)
    if shown is None:
        return await _refuse(admin_id, _NO_PROGRAM)
    return shown


async def _open_day(admin_id, state, scope, day):
    status, city = await _lock_city(scope, None, state)
    if status == "ask":
        return await _ask_city("sesscity")
    if status == "bad":
        return await _refuse(admin_id, _NO_SUCH_CITY)
    shown = await _show_program(city, day, other_days=False)
    if shown is None:
        return await _refuse(admin_id, _NO_PROGRAM)
    return shown


async def _pick_session(admin_id, state, scope, raw_id):
    if not raw_id.isdigit():
        return await _refuse(admin_id, _GONE)
    session = await get_program_session(int(raw_id))
    if not session:
        return await _refuse(admin_id, _GONE)
    status, city = await _lock_city(scope, None, state)
    if status == "ask":
        return await _ask_city("sesscity")
    if status == "bad":
        return await _refuse(admin_id, _NO_SUCH_CITY)
    if city and session.get("city") != city:
        return await _refuse(admin_id, _OTHER_CITY)
    title = (session.get("title") or "").strip() or "сессия"
    spec = [{
        "field": "checkin_session",
        "value": SESSION_ATTENDED,
        "session_id": int(session["id"]),
        "label": f"отмечены на «{title}»",
    }]
    spec = await _apply_city(spec, city)
    return await _people_or_refuse(admin_id, spec, city, _NONE_SESSION)


async def _legacy(choice, admin_id):
    wiz = await _wizard()
    spec, city = await wiz.build_audience_spec(choice, admin_id)
    if spec:
        ids = await count_and_list_filtered(spec)
    else:
        ids = await get_all_users_ids()
    return _result("ready", spec=spec, city=city, ids=ids)


async def resolve_audience_tap(choice, admin_id, state, *, surface, draft_id=None) -> dict:
    """Единственная развилка новых аудиторий. choice уже без префикса poll_aud:.

    В FSM пишется только mid_city. Ключ audience ставит вызывающий хендлер,
    и только когда kind == ready.
    """
    wiz = await _wizard()
    scope, _label = await wiz._admin_city_view(admin_id)
    choice = choice or ""
    if choice == "back":
        await state.update_data(mid_city=None)
        text, markup = await wiz._audience_kb(admin_id)
        raw = _result("screen", text=text, markup=markup)
    elif choice == "today" or choice.startswith("today:"):
        raw = await _today(admin_id, state, scope, choice)
    elif choice == "session" or choice.startswith("sesscity:"):
        explicit = choice.split(":", 1)[1] if choice.startswith("sesscity:") else None
        raw = await _open_sessions(admin_id, state, scope, explicit)
    elif choice.startswith("sessday:"):
        raw = await _open_day(admin_id, state, scope, choice.split(":", 1)[1])
    elif choice.startswith("sess:"):
        raw = await _pick_session(admin_id, state, scope, choice.split(":", 1)[1])
    elif choice in {"all", "approved"} or choice.startswith(("track:", "city:")):
        raw = await _legacy(choice, admin_id)
    else:
        raw = await _refuse(admin_id, _UNKNOWN)
    return _present(raw, surface, draft_id)
