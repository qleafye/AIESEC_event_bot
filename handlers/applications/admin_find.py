"""Карточка /find: @username, числовой id или пересланное сообщение.

Команда остаётся в handlers/admin.py (снимок хендлеров). Этот модуль собирает текст
и кнопки и ждёт следующее сообщение, если аргумента не было. Поиск по куску имени
не делается. Чужой город менеджеру с привязкой не показывается.
"""
import html as html_module

from aiogram import F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_reg_started_by_id, get_reg_started_by_username, get_user
from domain.cities import cities_module_on, enabled_cities, normalize_city
from handlers.access.admin_caps import has_capability
from handlers.admin import router
from handlers.forum.admin_checkin import _city_allowed
from handlers.states import FindPerson
from services.access.person_search import parse_query
from services.applications import decision_delivery

_ASK = "Пришлите @username, числовой id или пересланное сообщение."
_NAME = "Не понял. Пришлите @username, числовой id или пересланное сообщение — не имя."
_NOT_PERSON = (
    "Это не человек. Пришлите @username, числовой id или пересланное сообщение от человека."
)
_HIDDEN_FORWARD = (
    "В этой пересылке нет аккаунта человека — либо у него включена приватность "
    "пересылок, либо сообщение переслано из чата/канала. Попросите его @username "
    "или числовой id."
)
_LEGACY_HIDDEN = (
    "У этого человека скрыт аккаунт при пересылке — попросите его @username или "
    "числовой id."
)
_OTHER_CITY = (
    "Этот человек из другого города — карточку откроет руководитель без привязки к городу."
)


def _find_email_line(user) -> str:
    """Строка «Email» карточки /find — только когда он есть: прочерк «-» (заглушка финала анкеты
    на событиях без вопроса про почту) и пустое значение не показываем."""
    email = str(user["email"] or "").strip()
    if email in ("", "-"):
        return ""
    return f"Email: {html_module.escape(email)}\n"


async def _user_by_username(username: str):
    """Имя в handlers.admin: старые тесты карточки подменяют именно его."""
    import handlers.admin as admin_mod

    return await admin_mod.get_user_by_username(username)


def _viewer_id(message) -> int | None:
    viewer = getattr(message, "from_user", None)
    return None if viewer is None else viewer.id


async def _may_show(message, event_city) -> bool:
    """Нет отправителя — прямого вызова из теста, город не режем. Живое сообщение проверяем."""
    viewer_id = _viewer_id(message)
    if viewer_id is None:
        return True
    return await _city_allowed(viewer_id, normalize_city(event_city))


async def _show_city_move(message, event_city) -> bool:
    """Модуль городов выключен — кнопку оставляем, перевод сам скажет, что некуда.
    Включён — прячем, если в зоне менеджера нет другого включённого города."""
    if not await cities_module_on():
        return True
    viewer_id = _viewer_id(message)
    if viewer_id is None:
        return True
    from domain.settings.ops import per_city_visible_codes

    delegate_city = normalize_city(event_city)
    enabled = {c["code"] for c in await enabled_cities()}
    visible = await per_city_visible_codes(viewer_id)
    return any(code != delegate_city and code in enabled for code in visible)


async def _load_by_id(telegram_id: int):
    user = await get_user(telegram_id)
    if user:
        return "users", user
    started = await get_reg_started_by_id(telegram_id)
    if started:
        return "started", started
    return None


async def handle_find_command(message: types.Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(_ASK)
        if state is not None:
            await state.set_state(FindPerson.waiting)
        return
    await _open_from_text(message, parts[1])


async def _open_from_text(message: types.Message, raw: str) -> str:
    kind, value = parse_query(raw)
    if kind == "id":
        if isinstance(value, int) and value < 0:
            await message.answer(_NOT_PERSON)
            return "retry"
        loaded = await _load_by_id(int(value))
        return await _open_loaded(message, loaded, str(value))
    if kind == "username":
        user = await _user_by_username(str(value))
        if user:
            return await _open_loaded(message, ("users", user), str(value))
        started = await get_reg_started_by_username(str(value))
        if started:
            return await _open_loaded(message, ("started", started), str(value))
        await message.answer(f"❌ Пользователь {value} не найден в базе данных.")
        return "retry"
    await message.answer(_NAME)
    return "retry"


async def _open_from_message(message: types.Message) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        if sender is None:
            await message.answer(_HIDDEN_FORWARD)
            return "retry"
        loaded = await _load_by_id(sender.id)
        return await _open_loaded(message, loaded, str(sender.id))
    forwarded = getattr(message, "forward_from", None)
    if forwarded is not None:
        loaded = await _load_by_id(forwarded.id)
        return await _open_loaded(message, loaded, str(forwarded.id))
    if getattr(message, "forward_sender_name", None) or getattr(message, "forward_date", None):
        await message.answer(_LEGACY_HIDDEN)
        return "retry"
    return await _open_from_text(message, message.text or "")


async def _open_loaded(message: types.Message, loaded, label: str) -> str:
    if loaded is None:
        await message.answer(f"❌ Пользователь {label} не найден в базе данных.")
        return "retry"
    kind, row = loaded
    if not await _may_show(message, row.get("event_city")):
        await message.answer(_OTHER_CITY)
        return "denied"
    if kind == "users":
        await _answer_users_card(message, row)
    else:
        await _answer_started_card(message, row)
    return "shown"


async def _answer_users_card(message: types.Message, user: dict) -> None:
    text = (
        f"👤 <b>Пользователь найден:</b>\n"
        f"ID: <code>{user['telegram_id']}</code>\n"
        f"Имя: {html_module.escape(str(user['full_name'] or ''))}\n"
        f"Username: {html_module.escape(str(user['username'] or ''))}\n"
        f"{_find_email_line(user)}"
        f"Регистрация: {user['registration_date']}"
    )
    from services.applications.delegate_card import ext_forms_card_lines, status_city_season_lines

    text += await status_city_season_lines(user)
    forms_line, has_forms = await ext_forms_card_lines(user["telegram_id"])
    text += forms_line
    rows = [
        [InlineKeyboardButton(
            text="🔄 Перевыпустить QR", callback_data=f"checkin_reissue:{user['telegram_id']}",
        )],
    ]
    if await _show_city_move(message, user.get("event_city")):
        rows.append([InlineKeyboardButton(
            text="🏙 Перевести в город", callback_data=f"citymv_start:{user['telegram_id']}",
        )])
    if has_forms:
        rows.append([InlineKeyboardButton(
            text="📝 Ответы форм", callback_data=f"extf_view:{user['telegram_id']}",
        )])
    if user.get("status") in ("approved", "rejected"):
        rows.append([InlineKeyboardButton(
            text="↩️ Вернуть в ожидание", callback_data=f"revertp_start:{user['telegram_id']}",
        )])
    if user.get("status") in ("approved", "rejected"):
        text += decision_delivery.failure_line(user)
        rows.append([InlineKeyboardButton(
            text="📨 Отправить решение заново",
            callback_data=f"decresend_start:{user['telegram_id']}",
        )])
    from services.applications import delegate_overrides

    if user.get("status") == "rejected":
        resubmit_override = await delegate_overrides.active_override(
            user["telegram_id"], delegate_overrides.KIND_RESUBMIT,
        )
        if resubmit_override:
            granted_by_user = await get_user(resubmit_override["granted_by"])
            granted_by_label = html_module.escape(str(
                (granted_by_user or {}).get("full_name") or resubmit_override["granted_by"]
            ))
            text += (
                f"\n\n🔁 Разрешена повторная подача — выдал {granted_by_label}, "
                f"{resubmit_override['granted_at']}"
            )
            rows.append([InlineKeyboardButton(
                text="🔁 Отозвать разрешение", callback_data=f"resubg_revoke:{user['telegram_id']}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                text="🔁 Разрешить повторную подачу",
                callback_data=f"resubg_start:{user['telegram_id']}",
            )])
    if user.get("status") == "approved":
        edit_override = await delegate_overrides.active_override(
            user["telegram_id"], delegate_overrides.KIND_EDIT,
        )
        if edit_override:
            granted_by_user = await get_user(edit_override["granted_by"])
            granted_by_label = html_module.escape(str(
                (granted_by_user or {}).get("full_name") or edit_override["granted_by"]
            ))
            text += (
                f"\n\n✏️ Открыта правка — выдал {granted_by_label}, "
                f"{edit_override['granted_at']}"
            )
            rows.append([InlineKeyboardButton(
                text="✏️ Отозвать разрешение", callback_data=f"editg_revoke:{user['telegram_id']}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                text="✏️ Открыть правку после решения",
                callback_data=f"editg_start:{user['telegram_id']}",
            )])
    from services.registration.reg_stuck_reset import preview_stuck_reset

    if await preview_stuck_reset(user["telegram_id"]) is not None:
        rows.append([InlineKeyboardButton(
            text="🧹 Сбросить зависшую анкету", callback_data=f"regreset_start:{user['telegram_id']}",
        )])
    rows.append([InlineKeyboardButton(
        text="📎 Заменить резюме", callback_data=f"resumerep_start:{user['telegram_id']}",
    )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _answer_started_card(message: types.Message, started: dict) -> None:
    from domain.cities import city_label

    city_code = started.get("event_city")
    city_text = await city_label(city_code) if city_code else "-"
    text = (
        f"👤 <b>Пользователь найден (анкету не подавал(а)):</b>\n"
        f"ID: <code>{started['telegram_id']}</code>\n"
        f"Username: {html_module.escape(str(started.get('username') or ''))}\n"
        f"Начал(а) регистрацию: {started.get('started_at') or '-'}\n"
        f"Город (по анкете): {html_module.escape(str(city_text))}\n\n"
        "<i>Нажимал(а) /start, анкету пока не подавал(а) — записи делегата в базе нет.</i>"
    )
    rows = []
    viewer_id = _viewer_id(message)
    if viewer_id is not None and await has_capability(viewer_id, "settings"):
        rows.append([InlineKeyboardButton(
            text="👥 Выдать роль", callback_data=f"roles_addfor:{started['telegram_id']}",
        )])
    from services.registration.reg_stuck_reset import preview_stuck_reset

    if await preview_stuck_reset(started["telegram_id"]) is not None:
        rows.append([InlineKeyboardButton(
            text="🧹 Сбросить зависшую анкету", callback_data=f"regreset_start:{started['telegram_id']}",
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(StateFilter(FindPerson), Command("cancel"))
@router.message(StateFilter(FindPerson), F.text == "Отмена")
async def find_person_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Поиск отменён.")


@router.message(StateFilter(FindPerson.waiting))
async def find_person_message(message: types.Message, state: FSMContext):
    outcome = await _open_from_message(message)
    if outcome != "retry":
        await state.clear()
