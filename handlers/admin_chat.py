"""Квик 260914-rgr (RGR-01..07), задача 2: экран «💬 Чат» — сводка по каждому привязанному
чату, ручная сверка, тумблер учёта и отвязка с честным предупреждением.

Форма шва — `handlers/admin_quiet_hours.py`: своего `Router()` нет, декоратор на общий
`router` из `handlers.admin`, каждый декоратор В ОДНУ СТРОКУ (инвариант cap-теста
`tests/test_roles_phase8.py`), импорты `admin_sections`/`admin_settings` — ленивые, внутри
функций (иначе цикл: `admin_sections` импортирует этот модуль хвостом).

`city_or_global` в callback_data — строковый токен: код города ИЛИ литерал `"global"` для
привязки без города (модуль городов выключен) — тот же класс кода, что `chatbind:{code}` в
`handlers/group_chat.py`, менеджеру не показывается нигде, кроме как частью тапнутой кнопки.
"""
import html as html_module
import logging

from aiogram import F, types, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import city_label, city_scope
from database import db
from handlers.admin import router
from handlers.admin_caps import resolve_capabilities
from services import background, chat_tracking
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_GLOBAL_TOKEN = "global"


def _encode_city(city: str | None) -> str:
    return city if city is not None else _GLOBAL_TOKEN


def _decode_city(token: str) -> str | None:
    return None if token == _GLOBAL_TOKEN else token


async def _chat_label(entry: dict) -> str:
    if entry["city"]:
        return await city_label(entry["city"])
    return "Общий чат"


async def render_chat_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from handlers.admin_sections import back_button  # ленивый шов (D-03)

    enabled = await chat_tracking.tracking_on()
    refresh_minutes = await get_setting_typed("chat_refresh_minutes")

    lines = ["💬 <b>Чат делегатов</b>"]
    if enabled:
        lines.append(f"✅ Учёт включён, сверка раз в {refresh_minutes} мин.")
    else:
        lines.append("🔇 Учёт выключен — цифры ниже не обновляются.")

    chats = await chat_tracking.bound_chats()
    if not chats:
        lines.append("")
        lines.append(
            "Чат ещё не подключён. Добавьте бота в группу делегатов и дайте ему права "
            "администратора — бот сам спросит в группе, к какому городу её отнести."
        )
    else:
        for entry in chats:
            label = await _chat_label(entry)
            title = html_module.escape(entry["title"] or label)
            scope = city_scope(entry["city"]) if entry["city"] else None
            counts = await db.chat_counts(entry["chat_id"], scope)
            last_sync = await db.chat_last_sync_at(entry["chat_id"])
            lines.append("")
            lines.append(f"<b>{title}</b> ({html_module.escape(label)})")
            lines.append(
                f"одобрено {counts['approved']} · в чате {counts['in_chat']} · "
                f"не в чате {counts['not_in_chat']} · в чате, но не зарегистрированы "
                f"{counts['unknown_members']}"
            )
            lines.append(f"Последняя сверка: {last_sync or 'ещё не было'}")

    text = "\n".join(lines)

    toggle_label = "💬 Учёт чата: ✅ Вкл → ❌ Выкл" if enabled else "💬 Учёт чата: ❌ Выкл → ✅ Вкл"
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=toggle_label, callback_data="chat_chat_tracking_toggle")],
    ]
    if chats:
        buttons.append([InlineKeyboardButton(text="🔄 Сверить сейчас", callback_data="chat_refresh_now")])
        caps = await resolve_capabilities(admin_id)
        can_broadcast = "broadcast" in caps
        for entry in chats:
            token = _encode_city(entry["city"])
            label = await _chat_label(entry)
            row = [InlineKeyboardButton(text=f"🔓 Отвязать «{label}»", callback_data=f"chat_unbind:{token}")]
            buttons.append(row)
            if can_broadcast:
                buttons.append([InlineKeyboardButton(
                    text=f"📣 Рассылка не вступившим «{label}»",
                    callback_data=f"chat_broadcast_out:{token}",
                )])
    buttons.append([back_button("admin_chat")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_chat")
async def admin_chat(callback: types.CallbackQuery):
    text, kb = await render_chat_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "chat_chat_tracking_toggle")
async def chat_chat_tracking_toggle(callback: types.CallbackQuery):
    """ПЕРЕРИСОВКА ЭТОГО ЖЕ экрана — не `_toggle_module_setting` (тот вернул бы менеджера в
    раздел-владелец и выбросил бы его с экрана чата)."""
    current = await chat_tracking.tracking_on()
    await set_setting_by_admin(callback.from_user.id, "chat_tracking_enabled", "off" if current else "on")
    text, kb = await render_chat_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "chat_refresh_now")
async def chat_refresh_now(callback: types.CallbackQuery, bot: Bot):
    await callback.answer("Сверяю состав…")

    async def _run():
        try:
            reports = await chat_tracking.refresh_all_chats(bot)
            if reports:
                total_checked = sum(r["checked"] for r in reports)
                total_present = sum(r["present"] for r in reports)
                total_absent = sum(r["absent"] for r in reports)
                total_errors = sum(r["errors"] for r in reports)
                await bot.send_message(
                    callback.from_user.id,
                    f"✅ Сверка чата завершена: проверено {total_checked}, в чате "
                    f"{total_present}, не в чате {total_absent}, ошибок {total_errors}.",
                )
            else:
                await bot.send_message(callback.from_user.id, "Сверять нечего — учёт выключен или чат не привязан.")
        except Exception as e:
            logger.error("chat_refresh_now: фоновая сверка упала: %s", e)
            try:
                await bot.send_message(callback.from_user.id, "⚠️ Сверка не завершилась — подробности в логе.")
            except Exception:
                pass

    background.spawn(_run())


@router.callback_query(F.data.startswith("chat_unbind:"))
async def chat_unbind(callback: types.CallbackQuery):
    from handlers.admin_sections import back_button  # ленивый шов (D-03)

    token = callback.data.split(":", 1)[1]
    city = _decode_city(token)
    entry = await chat_tracking.chat_for_city(city)
    label = await _chat_label(entry) if entry else token
    text = (
        f"Отвязать чат «{html_module.escape(label)}»?\n\n"
        "Бот перестанет считать состав и активность этого чата; накопленные строки о "
        "участниках, вступлениях и активности будут удалены; сам чат и люди в нём не тронуты."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, отвязать", callback_data=f"chat_unbind_go:{token}")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin_chat")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("chat_unbind_go:"))
async def chat_unbind_go(callback: types.CallbackQuery):
    token = callback.data.split(":", 1)[1]
    city = _decode_city(token)
    entry = await chat_tracking.chat_for_city(city)
    await chat_tracking.unbind_chat(callback.from_user.id, city)
    if entry is not None:
        await db.purge_chat_data(entry["chat_id"])
    text, kb = await render_chat_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Отвязано")
