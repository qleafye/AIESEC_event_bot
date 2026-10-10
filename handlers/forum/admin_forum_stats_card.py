"""Идея №29 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): экран рассылки
«Твой Юлид в цифрах» — картинка-итог делегату после форума.

Форма — та же, что `handlers.forum.admin_lost_found`/`handlers.forum.admin_volunteer_invite`: тумблер
per_city `forum_stats_card_enabled` (дефолт off), собственный экран (`forumstats_cfg:*`),
статусная СТРОКА в хабе «🎪 Форум: функции» (`handlers/forum/admin_forum_functions.py`, правка
аддитивная — см. его докстринг). Своего `Router()` нет — декорирует `handlers.admin.router`,
импортирован в ХВОСТЕ `handlers/admin.py`, ПОСЛЕДНИМ (золотой снапшот — чистый аппенд).

Фон карточки — общий генерический `settings_photo:forum_stats_card` (handlers/settings/admin_settings.py
PHOTO_FIELDS), второй копии хендлера загрузки не заводим. Подпись к фото —
`forum_stats_card_caption_text`, редактор — общий текстовый экран «📋 Заявки»
(`handlers/settings/admin_settings.py::_APPS_FIELD_ORDER`), тот же приём, что у соседних форумных
текстов (см. докстринг `settings_schema.SETTINGS_SCHEMA["forum_stats_card_caption_text"]`) —
прямой ссылки на `settings_edit:` с этого экрана НЕТ намеренно (генерический хендлер стартует
FSM per-city правки только через «✏️ Изменить для …» на самом экране группы, см. докстринг
`handlers.settings.admin_settings.settings_edit_start`).

Рассылка — двухшаговое подтверждение (выбор аудитории кнопкой с готовым числом -> «✅ Да,
отправить»), тот же приём двойного барьера, что «📤 Разослать QR сейчас»
(`handlers/forum/admin_checkin.py::checkinqr_send_go`): колбэк отвечает СРАЗУ, клавиатура убирается
ДО вызова `services.forum_stats_card.send_broadcast` (может занять минуты — сотни фото), сама
рассылка вдобавок блокируется `asyncio.Lock` на город.

Капа — `moderate_reg` на ВСЕХ callback этого экрана (массовая рассылка + правка настроек, тот
же довод, что у остального хаба «🎪 Форум: функции»)."""
import html

from aiogram import F, types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import cities_module_on, city_label, get_setting_typed_for_city, per_city_key
from database.db import get_setting
from handlers.admin import router
from handlers.forum.admin_checkin import (
    _CITY_FORBIDDEN_ALERT,
    _city_allowed,
    _decode_city,
    _encode_city,
)
from services import forum_stats_card as fsc
from services.settings.audit import set_setting_by_admin


async def _cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    label = await city_label(code) if code else None
    on = await fsc.enabled_for(code)
    bg_set = bool(await get_setting(fsc.BACKGROUND_SETTING_KEY))
    caption_set = bool((await get_setting_typed_for_city("forum_stats_card_caption_text", code) or "").strip())
    counts = await fsc.audience_counts(code)
    sent = await fsc.sent_summary(code)

    lines = ["📊 <b>Карточка «Мы в цифрах»</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}")
    lines.append(f"Фон: {'✅ загружен' if bg_set else '— однотонный фон бренда'}")
    if not caption_set:
        lines.append("⚠️ Подпись к фото пуста — рассылка НЕ уйдёт, даже если включена здесь.")
    lines.append(f"\nОдобрены текущего сезона: {counts['all']} · пришли хотя бы раз: {counts['arrived']}")
    lines.append(f"Уже отправлено: {sent['sent']}")
    lines.append(
        "\nПодпись правится в «⚙️ Настройки» → «📋 Заявки» → "
        "«📊 Карточка «Мы в цифрах»: подпись»."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}",
            callback_data=f"forumstats_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="🖼 Фон", callback_data="settings_photo:forum_stats_card")],
        [InlineKeyboardButton(text="👁 Превью себе", callback_data=f"forumstats_preview:{_encode_city(code)}")],
        [InlineKeyboardButton(
            text=f"📤 Разослать пришедшим хотя бы раз ({counts['arrived']})",
            callback_data=f"forumstats_pick:{_encode_city(code)}:arrived",
        )],
        [InlineKeyboardButton(
            text=f"📤 Разослать всем одобренным ({counts['all']})",
            callback_data=f"forumstats_pick:{_encode_city(code)}:all",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("forumstats_cfg:"))
async def forumstats_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forumstats_toggle:"))
async def forumstats_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "forum_stats_card_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current == "on" else "on"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    text, kb = await _cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


@router.callback_query(F.data.startswith("forumstats_preview:"))
async def forumstats_preview_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await callback.answer("Строю превью…")
    png = await fsc.render_preview("ru", code)
    await callback.message.answer_photo(
        BufferedInputFile(png, filename="yulead_stats_preview.png"),
        caption="👁 Превью с представительными данными — не твоя личная статистика.",
    )


@router.callback_query(F.data.startswith("forumstats_pick:"))
async def forumstats_pick_go(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    code = _decode_city(parts[1])
    mode = parts[2]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    counts = await fsc.audience_counts(code)
    n = counts["arrived"] if mode == "arrived" else counts["all"]
    who = "пришедшим хотя бы раз" if mode == "arrived" else "всем одобренным текущего сезона"
    label = await city_label(code) if code else None
    where = f" города {label}" if label else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Да, отправить",
            callback_data=f"forumstats_send_go:{_encode_city(code)}:{mode}",
        ),
        InlineKeyboardButton(text="Отмена", callback_data=f"forumstats_cfg:{_encode_city(code)}"),
    ]])
    await callback.message.answer(f"Уйдёт {n} делегатам{where} ({who}). Отправить?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forumstats_send_go:"))
async def forumstats_send_go(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    code = _decode_city(parts[1])
    mode = parts[2]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    # Тот же двойной барьер, что checkinqr_send_go (handlers/forum/admin_checkin.py): отвечаем на
    # колбэк СРАЗУ и убираем клавиатуру ДО вызова — рассылка может занять минуты, повторный тап
    # уже физически не по чему нажимать; send_broadcast вдобавок сама блокируется per-city
    # локом.
    await callback.answer("Рассылка началась…")
    await callback.message.edit_text("⏳ Рассылаю карточки…", reply_markup=None)
    result = await fsc.send_broadcast(code, only_arrived=(mode == "arrived"))
    if result.get("already_running"):
        await callback.message.answer("⏳ Рассылка уже идёт — дождитесь её завершения.")
        return
    if result.get("disabled"):
        await callback.message.answer("❌ Рассылка сейчас выключена — включите тумблер и повторите.")
        return
    if result.get("empty_caption"):
        await callback.message.answer(
            "❌ Ничего не отправлено: подпись к карточке пуста. Заполните её в «⚙️ Настройки» → "
            "«📋 Заявки» → «📊 Карточка «Мы в цифрах»: подпись» и запустите рассылку снова."
        )
        return
    text = f"✅ Отправлено {result['sent']} из {result['total']}"
    if result["failed"]:
        text += f", не доставлено {result['failed']}"
    text += "."
    if result["quiet"]:
        text += f"\n🌙 {result['quiet']} делегатов сейчас в тихих часах — им не отправлено, повторите позже."
    if result["muted"]:
        text += f"\n🔕 {result['muted']} отключили рассылки сегодня — им не отправлено."
    await callback.message.answer(text)
