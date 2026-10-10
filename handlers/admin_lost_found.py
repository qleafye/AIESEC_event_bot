"""Идея №20 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): бюро находок.

Волонтёр (право `checkin`) фотографирует находку, описывает, где нашли/куда подойти
забрать, и публикует пост в чат делегатов (per-city `services.chat_tracking.chat_for_city`)
с кнопкой «✅ Нашёлся хозяин» — тот, кто её нажмёт (держатель `checkin` ИЛИ `moderate_reg`,
обе аудитории по плану), закрывает находку идемпотентно (`database.db.lost_found`).

Мастер: фото → «где нашли/куда подойти» → предпросмотр («📢 Опубликовать»/«✖️ Отмена»);
город резолвится тем же трёхветочным приёмом, что `handlers.admin_forum_functions.
_resolve_screen_city`/`handlers.admin_volunteer_invite.volinvite_entry` (своя копия —
приватные хелперы не экспортируются между модулями). Тумблер per_city
`lost_found_enabled` (дефолт OFF) — свой экран здесь (`lostfound_cfg:*`), статусная СТРОКА —
хаб «🎪 Форум: функции» (`handlers/admin_forum_functions.py`, правка аддитивная — см. его
докстринг). Текст поста — общий текстовый редактор («📋 Заявки»,
`handlers/admin_settings.py::_APPS_FIELD_ORDER`), group "apps" (НЕ переводится, см.
докстринг `settings_schema.SETTINGS_SCHEMA["lost_found_post_text"]`).

Своего `Router()` нет — декорирует `handlers.admin.router`, тот же приём, что
`handlers/admin_volunteer_invite.py`; импортирован в ХВОСТЕ `handlers/admin.py`,
ПОСЛЕДНИМ (золотой снапшот — чистый аппенд).

Право доступа — АСИММЕТРИЧНО, сознательно:
- Создание находки (весь мастер + тумблер) — ОДНА капа «checkin» (создание) либо
  «moderate_reg» (тумблер), тот же приём, что у остального хаба «🎪 Форум: функции»/
  «✅ Отметки на форуме»: ADMIN_CAPS несёт ровно одно значение на ключ (D-01/D-15).
- Кнопка «✅ Нашёлся хозяин» живёт ПОД постом в ГРУППОВОМ чате делегатов — сматчится
  `admin.router`'ом независимо от чата (`CapabilityMiddleware` не различает тип чата, тот
  же прецедент, что `sos_claim:*`/`sos_resolve:*` в `handlers/admin_sos.py`), и её видит
  ЛЮБОЙ участник чата, не только штат. План явно называет ДВЕ аудитории («checkin» И
  «moderate_reg»), а один ключ карты — одно значение; здесь запись ANY_CAPABILITY
  (навигационная, тот же приём, что «admin_city_pick:*» в `handlers/admin_caps.py`), а
  настоящая проверка (OR двух прав) — вручную, внутри `lostfound_return` ниже.
"""
import html
import logging

from aiogram import Bot, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import (
    cities_module_on,
    city_label,
    default_city_code,
    enabled_cities,
    get_setting_typed_for_city,
    per_city_key,
)
from database.db import create_lost_found_item, get_lost_found_item, mark_lost_found_returned
from handlers.admin import router
from handlers.admin_caps import DENIAL_TEXT, resolve_capabilities
from handlers.admin_checkin import (
    _CITY_FORBIDDEN_ALERT,
    _admin_city_scope,
    _city_allowed,
    _decode_city,
    _encode_city,
)
from handlers.states import LostFoundNew
from keyboards.builders import get_cancel_kb
from services import chat_tracking
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

# Обе аудитории плана — см. докстринг модуля выше про `lostfound_return:*`.
_ALLOWED_RETURN_CAPS = {"checkin", "moderate_reg"}

_NO_CHAT_TEXT = (
    "⚠️ Чат делегатов не привязан — скажи менеджеру, пусть добавит бота администратором в "
    "группу делегатов этого города (бот сам предложит привязку после этого)."
)

_DISABLED_TEXT = "Бюро находок выключено."

_WHERE_HINT = "Например: «Нашли у зала А, забрать на стойке 1»."


async def _resolve_own_city(admin_id: int) -> str | None:
    """Тот же трёхветочный резолвер «город из шапки», что `handlers.admin_forum_functions.
    _resolve_screen_city`/`handlers.admin_volunteer_invite.volinvite_entry` (см. докстринг
    модуля — приватная копия, не общий импорт)."""
    own_scope = await _admin_city_scope(admin_id)
    if own_scope is not None:
        return own_scope[0]
    if not await cities_module_on():
        return default_city_code()
    return None


async def _render_city_picker() -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"lostfound_city_pick:{c['code']}")]
        for c in await enabled_cities()
    ]
    return "🧳 <b>Бюро находок</b>\n\nВыберите город.", InlineKeyboardMarkup(inline_keyboard=buttons)


async def _chat_for_own_city(code: str | None) -> dict | None:
    """`services.chat_tracking.chat_for_city` сравнивает `entry["city"] == city` буквально —
    модуль городов выключен -> привязка всегда лежит под глобальными ключами (`city=None`),
    а `_resolve_own_city` в этом состоянии всё равно возвращает настоящий код
    (`default_city_code()`, тот же приём, что `handlers.admin_forum_functions.
    _resolve_screen_city`). Тот же трёхветочный «module off -> None» гейт, что и в
    остальных местах этого модуля (per_city_key при сохранении тумблера) — без него привязка
    глобального чата не находилась бы вовсе (см. `services.sos.sos_chat_for_city`, тот же
    гейт «not cities_module_on() or city is None» в соседнем форумном модуле)."""
    chat_city = code if await cities_module_on() else None
    return await chat_tracking.chat_for_city(chat_city)


async def _post_caption(code: str | None, where_text: str) -> str:
    template = await get_setting_typed_for_city("lost_found_post_text", code) or ""
    return template.replace("{where}", html.escape(where_text))


def _preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Опубликовать", callback_data="lostfound_publish"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="lostfound_cancel"),
    ]])


async def _start_wizard(answer_to, code: str | None, state: FSMContext) -> None:
    """`answer_to` — объект с `.answer(...)` (`types.Message` ИЛИ `CallbackQuery.message`)."""
    enabled = await get_setting_typed_for_city("lost_found_enabled", code) == "on"
    if not enabled:
        await answer_to.answer(_DISABLED_TEXT)
        return
    await state.update_data(lostfound_city=code)
    await state.set_state(LostFoundNew.waiting_photo)
    await answer_to.answer(
        "🧳 <b>Нашли вещь</b>\n\nПришлите фото находки.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )


# ── Вход ──────────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "lost_found_new")
async def lost_found_new_entry(callback: types.CallbackQuery, state: FSMContext):
    code = await _resolve_own_city(callback.from_user.id)
    if code is None:
        text, kb = await _render_city_picker()
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer()
        return
    await _start_wizard(callback.message, code, state)
    await callback.answer()


@router.callback_query(F.data.startswith("lostfound_city_pick:"))
async def lostfound_city_pick(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await _start_wizard(callback.message, code, state)
    await callback.answer()


@router.message(Command("found"))
async def lost_found_cmd(message: types.Message, state: FSMContext):
    code = await _resolve_own_city(message.from_user.id)
    if code is None:
        text, kb = await _render_city_picker()
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    await _start_wizard(message, code, state)


@router.message(StateFilter(LostFoundNew), Command("cancel"))
@router.message(StateFilter(LostFoundNew), F.text == "Отмена")
async def lost_found_cancel_wizard(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


# ── Шаг 1: фото ───────────────────────────────────────────────────────────────────────────

@router.message(LostFoundNew.waiting_photo, F.photo)
async def lost_found_photo_step(message: types.Message, state: FSMContext):
    await state.update_data(lostfound_photo=message.photo[-1].file_id)
    await state.set_state(LostFoundNew.waiting_where)
    await message.answer(
        f"Где нашли / куда подойти забрать? {_WHERE_HINT}",
        reply_markup=get_cancel_kb(),
    )


@router.message(LostFoundNew.waiting_photo)
async def lost_found_photo_invalid(message: types.Message):
    await message.answer("Пришлите фото находки (как фотографию, не файлом).", reply_markup=get_cancel_kb())


# ── Шаг 2: где нашли ─────────────────────────────────────────────────────────────────────

@router.message(LostFoundNew.waiting_where)
async def lost_found_where_step(message: types.Message, state: FSMContext):
    where_text = (message.text or "").strip()
    if not where_text:
        await message.answer(
            f"Не понял — пришлите текстом, где нашли/куда подойти забрать. {_WHERE_HINT}",
            reply_markup=get_cancel_kb(),
        )
        return
    await state.update_data(lostfound_where=where_text)
    await state.set_state(LostFoundNew.preview)
    data = await state.get_data()
    caption = await _post_caption(data.get("lostfound_city"), where_text)
    await message.answer("Предпросмотр — так увидят делегаты:", reply_markup=ReplyKeyboardRemove())
    await message.answer_photo(
        data["lostfound_photo"], caption=caption, parse_mode="HTML", reply_markup=_preview_kb(),
    )


# ── Предпросмотр: публикация/отмена ──────────────────────────────────────────────────────

@router.callback_query(F.data == "lostfound_cancel")
async def lost_found_cancel_preview(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logger.warning("admin_lost_found: не удалось убрать клавиатуру предпросмотра: %s", e)
    await callback.answer("Отменено")


@router.callback_query(F.data == "lostfound_publish")
async def lost_found_publish(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    code = data.get("lostfound_city")
    photo = data.get("lostfound_photo")
    where_text = data.get("lostfound_where")
    await state.set_state(None)

    if not photo or not where_text:
        await callback.answer("Черновик утерян — начните заново, /found.", show_alert=True)
        return
    # Тумблер могли выключить, пока волонтёр смотрел предпросмотр, — проверяем в момент публикации.
    if await get_setting_typed_for_city("lost_found_enabled", code) != "on":
        await callback.answer("Бюро находок выключено — публикация отменена.", show_alert=True)
        return

    entry = await _chat_for_own_city(code)
    if entry is None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning("admin_lost_found: не удалось убрать клавиатуру предпросмотра: %s", e)
        await callback.message.answer(_NO_CHAT_TEXT)
        await callback.answer()
        return

    caption = await _post_caption(code, where_text)
    try:
        sent = await bot.send_photo(entry["chat_id"], photo, caption=caption, parse_mode="HTML")
    except Exception as e:
        logger.warning(
            "admin_lost_found: не удалось опубликовать находку в чат id=%s: %s", entry["chat_id"], e,
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as e2:
            logger.warning("admin_lost_found: не удалось убрать клавиатуру предпросмотра: %s", e2)
        await callback.message.answer(_NO_CHAT_TEXT)
        await callback.answer()
        return

    item_id = await create_lost_found_item(
        code, photo, where_text, callback.from_user.id, entry["chat_id"], sent.message_id,
    )
    try:
        await bot.edit_message_reply_markup(
            entry["chat_id"], sent.message_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Нашёлся хозяин", callback_data=f"lostfound_return:{item_id}"),
            ]]),
        )
    except Exception as e:
        logger.warning("admin_lost_found: не удалось прикрепить кнопку возврата id=%s: %s", item_id, e)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logger.warning("admin_lost_found: не удалось убрать клавиатуру предпросмотра: %s", e)
    await callback.message.answer("✅ Опубликовано в чат делегатов.")
    await callback.answer()


# ── «✅ Нашёлся хозяин» (кнопка живёт в группе делегатов — см. докстринг модуля) ─────────────

@router.callback_query(F.data.startswith("lostfound_return:"))
async def lostfound_return(callback: types.CallbackQuery):
    caps = await resolve_capabilities(callback.from_user.id)
    if not (caps & _ALLOWED_RETURN_CAPS):
        await callback.answer(DENIAL_TEXT, show_alert=True)
        return

    raw_id = callback.data.split(":", 1)[1]
    if not raw_id.isdigit():
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    item_id = int(raw_id)

    item = await get_lost_found_item(item_id)
    if item is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    # id в кнопке подделывается: отмечаем только находку ЭТОГО поста и только своего города.
    msg = callback.message
    if msg is None or (item["chat_id"], item["message_id"]) != (msg.chat.id, msg.message_id):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, item["city"]):
        await callback.answer(DENIAL_TEXT, show_alert=True)
        return
    if item["returned_at"]:
        await callback.answer("Уже отмечено как возвращено", show_alert=True)
        return

    marked = await mark_lost_found_returned(item_id, callback.from_user.id)
    if not marked:
        # Гонка двух одновременных тапов — тот же смысл, что и ветка выше, второй тап проиграл.
        await callback.answer("Уже отмечено как возвращено", show_alert=True)
        return

    try:
        await callback.message.edit_caption(caption="✅ Вещь вернули владельцу", reply_markup=None)
    except Exception as e:
        logger.warning("admin_lost_found: не удалось отредактировать пост находки id=%s: %s", item_id, e)
    await callback.answer("Отмечено")


# ── Тумблер (хаб «🎪 Форум: функции» ссылается сюда строкой) ────────────────────────────────

async def _lostfound_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_setting_typed_for_city("lost_found_enabled", code) == "on"
    label = await city_label(code) if code else None

    lines = ["🧳 <b>Бюро находок</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Приём находок: {'✅ Вкл' if enabled else '❌ Выкл'}")
    text_set = bool((await get_setting_typed("lost_found_post_text") or "").strip())
    if not text_set:
        lines.append("\n⚠️ Текст поста пуст — публикация не пойдёт, даже если включено здесь.")
    chat_entry = await _chat_for_own_city(code)
    if chat_entry is None:
        lines.append(
            "\n⚠️ Чат делегатов этого города не привязан — добавьте бота администратором в "
            "группу делегатов, бот сам предложит привязку."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Приём находок: {'✅ Вкл' if enabled else '❌ Выкл'}",
            callback_data=f"lostfound_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("lostfound_cfg:"))
async def lostfound_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _lostfound_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("lostfound_toggle:"))
async def lostfound_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "lost_found_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current == "on" else "on"
    if code and await cities_module_on():
        await set_setting_by_admin(callback.from_user.id, per_city_key(key, code), new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    text, kb = await _lostfound_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)
