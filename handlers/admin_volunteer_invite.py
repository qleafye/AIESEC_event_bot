"""Идея №5 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): приглашение
волонтёров ссылкой вместо ручного сбора юзернеймов. Держатель `moderate_reg` создаёт ссылку
`https://t.me/<bot>?start=vol_<код>` с сроком жизни ССЫЛКИ, сроком ПРАВ волонтёра и лимитом
переходов; перешедший по рабочей ссылке получает роль «volunteer» (ровно `checkin`,
`handlers/admin_caps.py::ROLES`) на срок прав, без ручного набора @username. Приём переходов —
`handlers/registration.py::cmd_start` (`vol_`-ветка deep-link, СРАЗУ, до анкеты).

Тумблер `volunteer_invite_enabled` — per_city, дефолт OFF (D: «риск утечки ссылки принят», но
не включён нигде без явного решения менеджера города). Своего `Router()` нет — декорирует
`handlers.admin.router`, тот же приём, что `handlers/admin_forum_functions.py`; импортирован в
ХВОСТЕ `handlers/admin.py`, ПОСЛЕДНИМ (золотой снапшот — чистый аппенд).

Гонки закрыты на уровне БД (`database/db.py::claim_volunteer_invite`, см. его докстринг) — этот
модуль только строит экраны и мастер создания ссылки, бизнес-инвариант живёт в одном месте."""
import html
import secrets

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
from database.db import (
    create_volunteer_invite,
    get_user,
    get_volunteer_invite,
    list_volunteer_invite_uses,
    list_volunteer_invites,
    remove_staff,
    revoke_volunteer_invite,
)
from handlers.admin import router
from handlers.admin_checkin import _CITY_FORBIDDEN_ALERT, _admin_city_scope, _city_allowed, _decode_city, _encode_city
from handlers.states import VolunteerInviteWizard
from keyboards.builders import get_cancel_kb
from services.staff_expiry import format_ddmmyyyy, forum_end_date_iso, is_expiry_active, parse_ddmmyyyy, relative_days_iso
from services.settings.audit import set_setting_by_admin

VOLUNTEER_ROLE = "volunteer"  # handlers.admin_caps.ROLES — держит ровно "checkin"
# Ревью 28.09 (D-41): ссылка «с одобрением на месте» выдаёт роль волонтёра регистрации —
# отметка входа И одобрение человека у стойки (checkin + checkin_approve).
REG_VOLUNTEER_ROLE = "reg_volunteer"
_APPROVE_FLAG = "appr"


def invite_role(inv: dict | None) -> str:
    """Роль, которую выдаёт ссылка: у старых ссылок колонка пустая — обычный волонтёр."""
    return (inv or {}).get("role") or VOLUNTEER_ROLE


def _limit_text(max_uses: int | None) -> str:
    return "без лимита" if max_uses is None else str(max_uses)


def _invite_status_text(inv: dict) -> str:
    if inv["revoked"]:
        return "⛔ отозвана"
    if inv["link_expires_at"] and not is_expiry_active(inv["link_expires_at"]):
        return "⌛ истекла"
    if inv["max_uses"] is not None and inv["used"] >= inv["max_uses"]:
        return "🚫 исчерпана"
    return "✅ активна"


def _expiry_suffix(exp_iso: str | None) -> str:
    """«до 04.10.2026» при наличии срока, иначе «бессрочно» — без этого выходит «до
    бессрочно» (бесконечность не наступает «до» точки во времени)."""
    formatted = format_ddmmyyyy(exp_iso)
    return f"до {formatted}" if formatted else "бессрочно"


def _invite_link_html(code_token: str, bot_username: str | None) -> str:
    """Сама ссылка-приглашение, а не код — код человеку ничего не говорит и его никуда не
    вставить; <code> даёт тап-копирование в Telegram."""
    raw = (
        f"https://t.me/{bot_username}?start=vol_{code_token}" if bot_username
        else f"?start=vol_{code_token}"
    )
    return f"<code>{html.escape(raw)}</code>"


def _invite_line_text(inv: dict, bot_username: str | None) -> str:
    rights = "отметка + одобрение на месте" if invite_role(inv) == REG_VOLUNTEER_ROLE else "отметка входа"
    return (
        f"• {_invite_link_html(inv['code'], bot_username)} — {_invite_status_text(inv)} · "
        f"может: {rights} · "
        f"использовано {inv['used']} из {_limit_text(inv['max_uses'])} · "
        f"ссылка: {_expiry_suffix(inv['link_expires_at'])} · "
        f"права волонтёра: {_expiry_suffix(inv['rights_expires_at'])}"
    )


def _invite_is_live(inv: dict) -> bool:
    if inv["revoked"]:
        return False
    if inv["link_expires_at"] and not is_expiry_active(inv["link_expires_at"]):
        return False
    if inv["max_uses"] is not None and inv["used"] >= inv["max_uses"]:
        return False
    return True


async def _bot_username(bot) -> str | None:
    """Тот же fail-soft приём, что `handlers/reg_ambassador.py::_bot_username` — мёртвой
    ссылки быть не должно, `get_me()` не смог -> экран честно говорит об этом."""
    try:
        me = await bot.get_me()
        return me.username
    except Exception:
        return None


async def _render_cfg(admin_id: int, code: str | None, bot: Bot) -> tuple[str, InlineKeyboardMarkup]:
    label = await city_label(code) if code and await cities_module_on() else None
    enabled = await get_setting_typed_for_city("volunteer_invite_enabled", code) == "on"
    lines = [
        "🔗 <b>Приглашение волонтёров ссылкой</b>" + (f" — {html.escape(label)}" if label else ""),
        "",
        f"Приглашения: {'✅ Вкл' if enabled else '❌ Выкл'}",
    ]
    buttons: list[list[InlineKeyboardButton]] = [[InlineKeyboardButton(
        text=f"Приглашения: {'✅ Вкл' if enabled else '❌ Выкл'}",
        callback_data=f"volinvite_toggle:{_encode_city(code)}",
    )]]

    if not enabled:
        lines.append(
            "\nВключите, чтобы создавать ссылки-приглашения — по ним человек получает право "
            "«✅ Чек-ин» этого города без ручного набора @username."
        )
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

    buttons.append([InlineKeyboardButton(
        text="🔗 Создать ссылку", callback_data=f"volinvite_new:{_encode_city(code)}",
    )])
    # Одобрять у стойки может не каждый волонтёр (у дверей залов хватает отметки) — отдельная
    # кнопка, по умолчанию ссылка даёт только отметку входа.
    buttons.append([InlineKeyboardButton(
        text="🔗 Ссылка + одобрение на месте",
        callback_data=f"volinvite_new:{_encode_city(code)}:{_APPROVE_FLAG}",
    )])

    invites = await list_volunteer_invites(city=code)
    if not invites:
        lines.append("\nСсылок пока нет.")
    else:
        bot_username = await _bot_username(bot)
        lines.append("")
        for inv in invites:
            lines.append(_invite_line_text(inv, bot_username))
            row = []
            if _invite_is_live(inv):
                row.append(InlineKeyboardButton(
                    text="⛔ Отозвать", callback_data=f"volinv_revoke:{inv['code']}",
                ))
            if inv["used"] > 0:
                row.append(InlineKeyboardButton(
                    text=f"👥 {inv['used']} вошли", callback_data=f"volinv_users:{inv['code']}",
                ))
            if row:
                buttons.append(row)

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_city_picker() -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"volinvite_city_pick:{c['code']}")]
        for c in await enabled_cities()
    ]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_roles")])
    return "🔗 <b>Приглашение волонтёров</b>\n\nВыберите город.", InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "volinvite_entry")
async def volinvite_entry(callback: types.CallbackQuery, bot: Bot):
    """Точка входа с экрана «👥 Роли и доступы» (там нет своей шапки-города) — тот же
    трёхветочный резолвер, что `handlers.admin_forum_functions._resolve_screen_city`."""
    own_scope = await _admin_city_scope(callback.from_user.id)
    code = own_scope[0] if own_scope is not None else (
        default_city_code() if not await cities_module_on() else None
    )
    if code is None:
        text, kb = await _render_city_picker()
    else:
        text, kb = await _render_cfg(callback.from_user.id, code, bot)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("volinvite_city_pick:"))
async def volinvite_city_pick(callback: types.CallbackQuery, bot: Bot):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _render_cfg(callback.from_user.id, code, bot)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("volinvite_cfg:"))
async def volinvite_cfg_screen(callback: types.CallbackQuery, bot: Bot):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _render_cfg(callback.from_user.id, code, bot)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("volinvite_toggle:"))
async def volinvite_toggle_go(callback: types.CallbackQuery, bot: Bot):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "volunteer_invite_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current == "on" else "on"
    if code and await cities_module_on():
        await set_setting_by_admin(callback.from_user.id, per_city_key(key, code), new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    text, kb = await _render_cfg(callback.from_user.id, code, bot)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


# ── Мастер создания ссылки (три кнопочных шага + опциональный ввод даты) ────────────────────

_LINK_PRESETS = {"3": 3, "7": 7}


def _link_preset_kb(city: str | None) -> InlineKeyboardMarkup:
    enc = _encode_city(city)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 3 дня", callback_data="volinv_le:3")],
        [InlineKeyboardButton(text="📅 7 дней", callback_data="volinv_le:7")],
        [InlineKeyboardButton(text="🏁 До конца форума города", callback_data="volinv_le:forum")],
        [InlineKeyboardButton(text="✏️ Ввести дату", callback_data="volinv_le:custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"volinvite_cfg:{enc}")],
    ])


@router.callback_query(F.data.startswith("volinvite_new:"))
async def volinvite_new_start(callback: types.CallbackQuery, state: FSMContext):
    enc, _, flag = callback.data.split(":", 1)[1].partition(":")
    code = _decode_city(enc)
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    with_approve = flag == _APPROVE_FLAG
    await state.update_data(
        volinv_city=code, volinv_role=REG_VOLUNTEER_ROLE if with_approve else VOLUNTEER_ROLE,
    )
    rights = (
        "Вошедший сможет отмечать вход и одобрять людей у стойки (решение запишется на него)."
        if with_approve else "Вошедший сможет отмечать вход, но не одобрять людей у стойки."
    )
    await callback.message.answer(
        f"🔗 <b>Новая ссылка-приглашение</b>\n\n{rights}\n\nНа сколько ССЫЛКА остаётся рабочей?",
        parse_mode="HTML",
        reply_markup=_link_preset_kb(code),
    )
    await callback.answer()


def _rights_preset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♾ Бессрочно", callback_data="volinv_re:none")],
        [InlineKeyboardButton(text="🏁 До конца форума города", callback_data="volinv_re:forum")],
        [InlineKeyboardButton(text="✏️ Ввести дату", callback_data="volinv_re:custom")],
    ])


@router.callback_query(F.data.startswith("volinv_le:"))
async def volinv_link_expiry_pick(callback: types.CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    data = await state.get_data()
    code = data.get("volinv_city")
    if choice == "custom":
        await state.set_state(VolunteerInviteWizard.waiting_link_date)
        await callback.message.answer(
            "До какого дня действует САМА ССЫЛКА? Формат <code>ДД.ММ.ГГГГ</code>, например "
            "<code>04.10.2026</code>.",
            parse_mode="HTML",
            reply_markup=get_cancel_kb(),
        )
        await callback.answer()
        return
    if choice in _LINK_PRESETS:
        link_exp = relative_days_iso(_LINK_PRESETS[choice])
    elif choice == "forum":
        link_exp = await forum_end_date_iso(code)
        if link_exp is None:
            await callback.answer("Дата форума этого города не задана", show_alert=True)
            return
    else:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    await state.update_data(volinv_link_exp=link_exp)
    await callback.message.answer(
        "На какой срок волонтёр получает ПРАВА (можно продлить/снять позже в «👥 Роли и доступы»)?",
        reply_markup=_rights_preset_kb(),
    )
    await callback.answer()


@router.message(StateFilter(VolunteerInviteWizard), Command("cancel"))
@router.message(StateFilter(VolunteerInviteWizard), F.text == "Отмена")
async def volunteer_invite_wizard_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(VolunteerInviteWizard.waiting_link_date)
async def volinv_link_date_step(message: types.Message, state: FSMContext):
    link_exp = parse_ddmmyyyy((message.text or "").strip())
    if link_exp is None:
        await message.answer(
            "Не понял дату. Нужен формат <code>ДД.ММ.ГГГГ</code>, например <code>04.10.2026</code> "
            "— пришлите ещё раз.",
            parse_mode="HTML",
            reply_markup=get_cancel_kb(),
        )
        return
    await state.update_data(volinv_link_exp=link_exp)
    await state.set_state(None)
    await message.answer(
        "На какой срок волонтёр получает ПРАВА (можно продлить/снять позже в «👥 Роли и доступы»)?",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer("Выберите:", reply_markup=_rights_preset_kb())


@router.callback_query(F.data.startswith("volinv_re:"))
async def volinv_rights_expiry_pick(callback: types.CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    data = await state.get_data()
    code = data.get("volinv_city")
    if choice == "custom":
        await state.set_state(VolunteerInviteWizard.waiting_rights_date)
        await callback.message.answer(
            "До какого дня действуют ПРАВА волонтёра? Формат <code>ДД.ММ.ГГГГ</code>, например "
            "<code>04.10.2026</code>.",
            parse_mode="HTML",
            reply_markup=get_cancel_kb(),
        )
        await callback.answer()
        return
    if choice == "none":
        rights_exp = None
    elif choice == "forum":
        rights_exp = await forum_end_date_iso(code)
        if rights_exp is None:
            await callback.answer("Дата форума этого города не задана", show_alert=True)
            return
    else:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    await state.update_data(volinv_rights_exp=rights_exp)
    await callback.message.answer("Сколько человек могут перейти по ссылке?", reply_markup=_limit_preset_kb())
    await callback.answer()


@router.message(VolunteerInviteWizard.waiting_rights_date)
async def volinv_rights_date_step(message: types.Message, state: FSMContext):
    rights_exp = parse_ddmmyyyy((message.text or "").strip())
    if rights_exp is None:
        await message.answer(
            "Не понял дату. Нужен формат <code>ДД.ММ.ГГГГ</code>, например <code>04.10.2026</code> "
            "— пришлите ещё раз.",
            parse_mode="HTML",
            reply_markup=get_cancel_kb(),
        )
        return
    await state.update_data(volinv_rights_exp=rights_exp)
    await state.set_state(None)
    await message.answer("Сколько человек могут перейти по ссылке?", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите:", reply_markup=_limit_preset_kb())


_LIMIT_PRESETS = {"10": 10, "30": 30, "50": 50, "0": None}


def _limit_preset_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10", callback_data="volinv_lim:10")],
        [InlineKeyboardButton(text="30", callback_data="volinv_lim:30")],
        [InlineKeyboardButton(text="50", callback_data="volinv_lim:50")],
        [InlineKeyboardButton(text="♾ Без лимита", callback_data="volinv_lim:0")],
    ])


@router.callback_query(F.data.startswith("volinv_lim:"))
async def volinv_limit_pick_and_create(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    choice = callback.data.split(":", 1)[1]
    if choice not in _LIMIT_PRESETS:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    data = await state.get_data()
    code = data.get("volinv_city")
    link_exp = data.get("volinv_link_exp")
    rights_exp = data.get("volinv_rights_exp")
    role = data.get("volinv_role") or VOLUNTEER_ROLE
    await state.set_state(None)

    code_token = secrets.token_urlsafe(9)
    await create_volunteer_invite(
        code_token, code, callback.from_user.id, link_exp, rights_exp, _LIMIT_PRESETS[choice],
        role=None if role == VOLUNTEER_ROLE else role,
    )

    bot_username = await _bot_username(bot)
    link_line = f"https://t.me/{bot_username}?start=vol_{code_token}" if bot_username else (
        "Не удалось получить имя бота — ссылку соберите вручную: "
        f"<code>?start=vol_{html.escape(code_token)}</code>"
    )
    text = (
        "✅ Ссылка создана.\n\n"
        f"{link_line}\n\n"
        f"Ссылка действует: {_expiry_suffix(link_exp)}\n"
        f"Права волонтёра: {_expiry_suffix(rights_exp)}\n"
        f"Лимит переходов: {_limit_text(_LIMIT_PRESETS[choice])}"
    )
    kb_text, kb = await _render_cfg(callback.from_user.id, code, bot)
    await callback.message.answer(text)
    await callback.message.answer(kb_text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Отзыв ссылки + список вошедших ───────────────────────────────────────────────────────

async def _invite_in_scope(callback: types.CallbackQuery, code_token: str) -> dict | None:
    """Ссылка по коду из callback_data + проверка, что её город в зоне нажавшего. Код в кнопке
    подделывается так же легко, как код города, — менеджер города A не должен отзывать ссылки
    города B и снимать их волонтёров."""
    inv = await get_volunteer_invite(code_token)
    if inv is None:
        await callback.answer("Ссылка уже не существует", show_alert=True)
        return None
    if not await _city_allowed(callback.from_user.id, inv["city"]):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return None
    return inv

@router.callback_query(F.data.startswith("volinv_revoke:"))
async def volinv_revoke_confirm(callback: types.CallbackQuery):
    code_token = callback.data.split(":", 1)[1]
    if await _invite_in_scope(callback, code_token) is None:
        return
    text = (
        f"⛔ Отозвать ссылку <code>{html.escape(code_token)}</code>?\n\n"
        "Ссылка перестанет работать, выданные права останутся — снять их можно в списке ниже."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, отозвать", callback_data=f"volinv_revoke_go:{code_token}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"volinv_revoke_no:{code_token}"),
    ]])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("volinv_revoke_go:"))
async def volinv_revoke_go(callback: types.CallbackQuery, bot: Bot):
    code_token = callback.data.split(":", 1)[1]
    inv = await _invite_in_scope(callback, code_token)
    if inv is None:
        return
    await revoke_volunteer_invite(code_token)
    await callback.answer("Ссылка отозвана", show_alert=True)
    text, kb = await _render_cfg(callback.from_user.id, inv["city"] if inv else None, bot)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("volinv_revoke_no:"))
async def volinv_revoke_no(callback: types.CallbackQuery, bot: Bot):
    code_token = callback.data.split(":", 1)[1]
    inv = await _invite_in_scope(callback, code_token)
    if inv is None:
        return
    text, kb = await _render_cfg(callback.from_user.id, inv["city"], bot)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _users_text_kb(code_token: str) -> tuple[str, InlineKeyboardMarkup]:
    inv = await get_volunteer_invite(code_token)
    uses = await list_volunteer_invite_uses(code_token)
    lines = [f"👥 <b>Вошли по ссылке</b> <code>{html.escape(code_token)}</code>", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if not uses:
        lines.append("Пока никто не переходил.")
    for u in uses:
        tid = u["telegram_id"]
        user = await get_user(tid)
        name = (user.get("full_name") or user.get("username")) if user else None
        name = html.escape(str(name or tid))
        lines.append(f"• {name} — {u['used_at']}")
        buttons.append([InlineKeyboardButton(
            text=f"➖ Снять {name}", callback_data=f"volinv_removeuser:{code_token}:{tid}",
        )])
    buttons.append([InlineKeyboardButton(
        text="◀️ Назад", callback_data=f"volinvite_cfg:{_encode_city(inv['city'] if inv else None)}",
    )])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("volinv_users:"))
async def volinv_users_list(callback: types.CallbackQuery):
    code_token = callback.data.split(":", 1)[1]
    if await _invite_in_scope(callback, code_token) is None:
        return
    text, kb = await _users_text_kb(code_token)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("volinv_removeuser:"))
async def volinv_remove_user(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3 or not (parts[2].isascii() and parts[2].isdigit()):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    code_token, tid = parts[1], int(parts[2])
    if await _invite_in_scope(callback, code_token) is None:
        return
    # Снимаем только того, кто действительно вошёл по ЭТОЙ ссылке: иначе подставленный в кнопку
    # telegram_id снимал бы роль волонтёра с кого угодно.
    if tid not in {u["telegram_id"] for u in await list_volunteer_invite_uses(code_token)}:
        await callback.answer("Этот человек не входил по этой ссылке", show_alert=True)
        return
    await remove_staff(tid, invite_role(await get_volunteer_invite(code_token)))
    await callback.answer("Снят", show_alert=True)
    text, kb = await _users_text_kb(code_token)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
