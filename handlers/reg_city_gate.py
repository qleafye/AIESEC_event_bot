"""Квик 260923-p37 (CITY-REG-CLOSE): экран «город закрыт» — новый шов, а не врезка в
`handlers/registration.py` (тот файл уже стоит на потолке размера, см.
`tests/test_module_size_convention_260816.py`).

`reg_engine.city_gate(event_city)` — единственный судья «что делать с городом новой подачи»
(общий для бота и Mini App). Когда его ответ — `"closed"`/`"all_closed"`, бот показывает ЭТОТ
экран вместо развилки/начала анкеты: старая ссылка `?start=city_{code}` на закрытый (по дате
или выключенный) город не должна тихо начинать регистрацию на город, которого больше нет
(T-p37-01) — она предлагает открытые форумы вместо него.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import city_label, get_setting_typed_for_city, open_cities
from handlers import reg_i18n


async def open_city_kb() -> InlineKeyboardMarkup:
    """Одна кнопка на каждый ОТКРЫТЫЙ город (`cities.open_cities()`), в порядке CITIES —
    callback `city_pick:{code}` собран из закрытого словаря реестра, не из пользовательского
    ввода (тот же контракт, что у `_city_fork_kb`)."""
    rows = []
    for c in await open_cities():
        rows.append([InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"city_pick:{c['code']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_city_closed(message, closed_code: str | None) -> None:
    """D-05: экран, который видит человек по ссылке/тапу на закрытый (по дате или выключенный)
    город. Есть хотя бы один открытый город -> текст `city_reg_closed_text` (per-city, резолв
    по САМОМУ закрытому городу — `closed_code`, не по открытым) + кнопки открытых городов; ни
    одного открытого -> `city_reg_all_closed_text`, без клавиатуры. `closed_code` может быть
    `None` (гейт вызван без известного города, все города закрыты сразу) — тогда `{city}` в
    тексте не подставляется (`city_reg_closed_text` в этой ветке не читается вовсе, т.к. её
    вызывает только ветка с известным закрытым городом).

    Текст реестра уходит БЕЗ `html.escape` — тот же контракт, что у соседнего `city_fork_text`
    (`handlers.registration._city_fork_then_continue`): менеджер пишет обычный текст, не HTML
    для эскейпа."""
    open_ = await open_cities()
    text = await get_setting_typed_for_city(
        "city_reg_closed_text" if open_ else "city_reg_all_closed_text", closed_code,
    )
    lang, tr_map = await reg_i18n.ctx_for(message)
    text = reg_i18n.tr_text(text, lang, tr_map)
    if closed_code:
        text = text.replace("{city}", reg_i18n.tr_text(await city_label(closed_code), lang, tr_map))
    reply_markup = reg_i18n.tr_kb(await open_city_kb(), lang, tr_map) if open_ else None
    await message.answer(text, reply_markup=reply_markup)


# ── Квик 27.09: анкета не начинается без города ──────────────────────────────────────────────

async def _show_city_fork(message) -> None:
    """Тот же экран выбора города, что у /start (`city_fork_text` + кнопки открытых городов),
    с тем же переводом текста и кнопок."""
    from settings_schema import get_setting_typed
    text = await get_setting_typed("city_fork_text")
    lang, tr_map = await reg_i18n.ctx_for(message)
    await message.answer(
        reg_i18n.tr_text(text, lang, tr_map),
        reply_markup=reg_i18n.tr_kb(await open_city_kb(), lang, tr_map),
    )


async def form_city_or_ask(message, state, *, resume: bool = False,
                           referrer_id=None, source_tag=None, participant_type=None):
    """Квик 27.09: последний рубеж перед стартом/продолжением анкеты, когда город не пришёл
    ни параметром, ни из FSM. Возвращает `(go, city)`:

    - модуль городов выключен -> `(True, None)`: стек без городов живёт как раньше;
    - город известен (`services.known_city`) или открыт ровно один -> `(True, код)`;
    - иначе показан экран (выбор города или «город закрыт») -> `(False, None)`, вызывающий
      обязан выйти и ждать тапа `city_pick`.

    Правка уже поданной анкеты (D-07) закрытием города по дате не гейтится — известный город
    берётся как есть. `resume=True` — вызов из продолжения черновика: ставит в FSM маркер
    `_resume_after_city`, по которому `city_pick` продолжит ЭТОТ черновик, а не начнёт анкету
    заново. Атрибуция (реферер/метка/трек), пришедшая параметрами, кладётся в FSM до экрана —
    тот же приём, что `_persist_fork_attribution`."""
    import reg_engine
    from cities import cities_module_on
    from services.known_city import known_city

    uid = message.from_user.id
    try:
        if not await cities_module_on():
            return True, None
    except Exception:
        return True, None
    city = await known_city(uid)

    is_edit = False
    if city:
        try:
            from database.db import get_user
            from settings_schema import get_setting_typed
            season = (await get_setting_typed("event_season") or "").strip() or None
            is_edit = reg_engine.has_submitted_anketa(await get_user(uid), season)
        except Exception:
            is_edit = False
    if city and is_edit:
        return True, city
    try:
        kind, code = await reg_engine.city_gate(city)
    except Exception:
        return True, city
    if kind == "go":
        return True, code

    existing = await state.get_data()
    patch = {
        "referrer_id": referrer_id or existing.get("referrer_id"),
        "source": source_tag or existing.get("source"),
        "participant_type": participant_type or existing.get("participant_type"),
        "event_city": None,
    }
    if source_tag:
        patch["_source_from_tag"] = True
    if resume:
        patch["_resume_after_city"] = True
    await state.update_data(**patch)
    if kind in ("closed", "all_closed"):
        await send_city_closed(message, code)
    else:
        await _show_city_fork(message)
    return False, None


# ── Приёмка 09.10: делегат видит выбранный город форума ──────────────────────────────────────

FORUM_CITY_LABEL = "Город форума"


async def confirm_city_choice(message, code: str) -> None:
    """После тапа по городу кнопки пропадают — подтверждаем выбор строкой в чате, иначе
    делегат не знает, в какой город подаёт заявку."""
    import html
    lang, tr_map = await reg_i18n.ctx_for(message)
    label = reg_i18n.tr_text(FORUM_CITY_LABEL, lang, tr_map)
    # Подпись города пишет менеджер: «&»/«<» при parse_mode=HTML Telegram отклонил бы.
    city = html.escape(reg_i18n.tr_text(await city_label(code), lang, tr_map))
    await message.answer(f"✅ {label}: {city}")


async def summary_data(data: dict) -> dict:
    """Ответы для сводки + подпись города форума (`reg_engine.SUMMARY_EVENT_CITY_KEY`).
    Модуль городов выключен или город не выбран — подписи нет, строки в сводке тоже."""
    from cities import cities_module_on
    from reg_engine import SUMMARY_EVENT_CITY_KEY
    code = data.get("event_city")
    if not code or not await cities_module_on():
        return data
    return {**data, SUMMARY_EVENT_CITY_KEY: await city_label(code)}
