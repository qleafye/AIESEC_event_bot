"""Квик 260927: экран «🧹 Служебные сообщения в чате» (раздел «🔧 Управление»).

Менеджер отмечает галочками, какие служебные уведомления Telegram бот удаляет в чатах
делегатов («вступил(а) в группу», «покинул(а) группу», «закрепил(а) сообщение»…), и задаёт
задержку удаления. Под списком — состояние бота в каждом привязанном чате: удаляет /
нет права «Удаление сообщений» / права ещё не проверялись. Коды типов на экране не
показываются — только подписи (бот для людей).

Логика удаления — `services/chat_cleanup.py`; здесь только настройка. Регистрируется на общий
`admin.router` хвостовым импортом `handlers/admin.py`.
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_chat_bot_state, get_setting
from handlers.admin import router
from domain.settings.validation import validate_setting_value
from handlers.states import ChatCleanupEdit
from services import chat_cleanup, chat_tracking
from services.background import spawn
from services.settings.audit import delete_setting_by_admin, set_setting_by_admin

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test): every `@router.*` decorator below MUST fit on ONE line.

_EMPTY = "—"  # сентинел пустого набора — тот же, что empty_value ключа в реестре


async def _delay_text() -> str:
    raw = await get_setting(chat_cleanup.DELAY_KEY)
    try:
        seconds = int(raw) if raw is not None else 0
    except ValueError:
        seconds = 0
    return "сразу" if seconds <= 0 else f"{seconds} сек"


async def _status_lines() -> list[str]:
    bound = await chat_tracking.bound_chats()
    if not bound:
        return ["Чат делегатов ещё не подключён — добавьте бота в группу администратором."]
    lines = []
    for entry in bound:
        title = html_module.escape(entry["title"] or "чат")
        state = await get_chat_bot_state(entry["chat_id"])
        flag = state.get("can_delete") if state else None
        if flag == 1:
            lines.append(f"✅ «{title}»: бот удаляет")
        elif flag == 0:
            lines.append(
                f"⚠️ «{title}»: сделайте бота администратором с правом «Удаление сообщений»"
            )
        else:
            lines.append(f"❔ «{title}»: права ещё не проверялись")
    return lines


async def render_chat_cleanup_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    ticked = set(await chat_cleanup.ticked_codes())
    lines = [
        "🧹 <b>Служебные сообщения в чате</b>",
        "",
        "Отмеченные уведомления Telegram бот удаляет в чатах делегатов городов. Чаты SOS и "
        "команды не трогаются. Пока ничего не отмечено — ничего не удаляется.",
        "",
        "Учёт вступлений и выходов ведётся как раньше — уведомление удаляется уже после него.",
        "",
        *await _status_lines(),
    ]
    rows = []
    for code, label in chat_cleanup.CLEANUP_TYPES.items():
        mark = "✅" if code in ticked else "☑️"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"chclean:t:{code}")])
    rows.append([InlineKeyboardButton(
        text=f"⏱ Удалять через: {await _delay_text()}", callback_data="chclean:delay",
    )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_sec:manage")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_chat_cleanup")
async def chat_cleanup_open(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_chat_cleanup_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("chclean:t:"))
async def chclean_toggle(callback: types.CallbackQuery):
    code = callback.data.split(":", 2)[2]
    if code not in chat_cleanup.CLEANUP_TYPES:
        await callback.answer("Кнопка устарела — откройте экран заново.", show_alert=True)
        return
    ticked = await chat_cleanup.ticked_codes()
    if code in ticked:
        ticked.remove(code)
        note = "Больше не удаляю"
    else:
        ticked.append(code)
        note = "Буду удалять"
    ordered = [c for c in chat_cleanup.CLEANUP_TYPES if c in ticked]
    await set_setting_by_admin(
        callback.from_user.id, chat_cleanup.TYPES_KEY, "\n".join(ordered) if ordered else _EMPTY,
    )
    text, kb = await render_chat_cleanup_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(note)


@router.callback_query(F.data == "chclean:delay")
async def chclean_delay(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "⏱ <b>Через сколько секунд удалять уведомление</b>\n\n"
        f"Сейчас: <b>{await _delay_text()}</b>\n\n"
        "Пришлите число секунд. <code>0</code> — сразу. Например <code>60</code> — админы "
        "успеют увидеть уведомление."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_chat_cleanup")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(ChatCleanupEdit.waiting_for_value)
    await callback.answer()


@router.message(StateFilter(ChatCleanupEdit.waiting_for_value))
async def chclean_delay_value(message: types.Message, state: FSMContext):
    value = (message.text or "").strip()
    if value == "-":
        await delete_setting_by_admin(message.from_user.id, chat_cleanup.DELAY_KEY)
    else:
        normalized, error = validate_setting_value(chat_cleanup.DELAY_KEY, value) if value else (None, None)
        if normalized is None:
            await message.answer(
                error or "Не понял — пришлите число секунд, например <code>60</code>, или "
                "<code>0</code>, чтобы удалять сразу.",
                parse_mode="HTML",
            )
            return
        await set_setting_by_admin(message.from_user.id, chat_cleanup.DELAY_KEY, normalized)
    await state.clear()
    text, kb = await render_chat_cleanup_screen(message.from_user.id)
    await message.answer("✅ Сохранено\n\n" + text, parse_mode="HTML", reply_markup=kb)


# ── 29.09: «🔄 Сверить состав чата» (раздел «🔧 Управление») ────────────────────────────────
# Сверка идёт фоном (до пары минут на 500 делегатов), итог — личным сообщением менеджеру.
# Колбэк отвечает сразу, чтобы у менеджера не висел спиннер.

async def _reconcile_and_report(bot, admin_id: int) -> None:
    """Фоновая часть: флаг сверки уже занят хендлером, `reconcile_all_now` его снимет."""
    try:
        reports = await chat_tracking.reconcile_all_now(bot, claimed=True)
        await bot.send_message(admin_id, chat_tracking.reconcile_report_text(reports or []),
                               parse_mode="HTML")
    except Exception:
        chat_tracking.release_reconcile()
        logger.exception("chat_reconcile_now: сверка состава чата упала")
        try:
            await bot.send_message(admin_id, "Сверка состава чата не удалась — попробуйте ещё раз "
                                             "через пару минут.")
        except Exception:
            pass


@router.callback_query(F.data == "admin_chat_reconcile")
async def chat_reconcile_now(callback: types.CallbackQuery, bot):
    if not await chat_tracking.bound_chats():
        await callback.answer(
            "Чат делегатов не подключён — добавьте бота в группу делегатов администратором.",
            show_alert=True,
        )
        return
    if not chat_tracking.claim_reconcile():
        await callback.answer("Сверка уже идёт — итог придёт сюда, в личку.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        "🔄 Сверяю состав чата, это займёт до пары минут — пришлю итог сюда."
    )
    spawn(_reconcile_and_report(bot, callback.from_user.id))
