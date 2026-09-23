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
