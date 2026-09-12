"""Квик 260913-16o (задача 2): подтверждение выключения модерации + алерт держателям
`moderate_reg`.

Инцидент прода 06.09: в 05:06 UTC кто-то переключил `full_approval` в «авто», 38 заявок
одобрились молча, и никто из держателей `moderate_reg` не узнал об этом до утра. Этот шов
закрывает обе половины: переход в «авто» требует явного подтверждения (с человеческим
объяснением последствий, CLAUDE.md «бот для людей») и, после подтверждения, шлёт алерт всем
держателям `moderate_reg` с именем и id того, кто нажал (тот же `admin=<id>`, что теперь есть
в логе через `settings_audit.set_setting_by_admin`, но здесь — в чат живым людям, не только
в лог). Обратный переход в ручную модерацию остаётся мгновенным — там нечего терять.

Форма шва — `handlers/admin_reg_form.py`: свой `Router()` здесь НЕ заводится, декоратор
навешивается на общий `router` из `handlers.admin` (тот же инвариант cap-теста
`tests/test_roles_phase8.py`). Импортируется последней строкой `handlers/admin_sections.py`
(тот же хвостовой приём, что и соседи).
"""
from aiogram import Bot, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from handlers.admin_caps import notify_by_capability
from settings_audit import set_setting_by_admin

# Одна человеческая формулировка на три места (экран подтверждения, тост, алерт держателям
# moderate_reg) — второй копии текста нет. (родительный падеж, предложный падеж).
_FORMS: dict[str, tuple[str, str]] = {
    "full_approval": ("полной формы", "по полной форме"),
    "short_approval": ("краткой формы", "по краткой форме"),
    "party_approval": ("вечеринки", "на вечеринку"),
}

_STALE_BUTTON_TEXT = "Кнопка устарела, откройте раздел заново"


async def ask_auto_confirm(callback: types.CallbackQuery, key: str, title: str) -> None:
    """Экран подтверждения — вызывается ИЗ `admin_settings._toggle_approval_setting` вместо
    немедленной записи, когда новое значение — «авто». Ничего не пишет в bot_settings."""
    form = _FORMS.get(key)
    if form is None:
        await callback.answer(_STALE_BUTTON_TEXT, show_alert=True)
        return
    genitive, prepositional = form
    text = (
        f"⚠️ Выключить модерацию {genitive}?\n\n"
        f"Все новые заявки {prepositional} будут одобряться сами: без очереди модерации и "
        "без письма «прошёл отбор»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, выключить модерацию", callback_data=f"approval_auto_go:{key}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"approval_auto_no:{key}")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("approval_auto_go:"))
async def approval_auto_go(callback: types.CallbackQuery, bot: Bot):
    key = callback.data.split(":", 1)[1]
    form = _FORMS.get(key)
    if form is None:
        await callback.answer(_STALE_BUTTON_TEXT, show_alert=True)
        return
    genitive, prepositional = form

    await set_setting_by_admin(callback.from_user.id, key, "auto")

    who = callback.from_user
    id_part = f"(@{who.username}, id {who.id})" if who.username else f"(id {who.id})"
    alert_text = (
        f"⚠️ Модерация {genitive} выключена. Выключил: {who.full_name} {id_part}.\n\n"
        f"Новые заявки {prepositional} теперь одобряются автоматически — без очереди и без "
        "письма «прошёл отбор»."
    )
    # T-16o-03: БЕЗ parse_mode — full_name/username пришли от пользователя Telegram и могут
    # содержать «<»/«>»; без parse_mode это просто текст, а не сломанная HTML-разметка.
    await notify_by_capability(bot, "moderate_reg", alert_text)

    await callback.answer(f"Модерация {genitive}: ⚡ Авто", show_alert=True)

    from handlers.admin_sections import settings_return_screen  # ленивый шов (см. admin_settings.py)
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=f"settings_toggle_{key}")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("approval_auto_no:"))
async def approval_auto_no(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    if key not in _FORMS:
        await callback.answer(_STALE_BUTTON_TEXT, show_alert=True)
        return

    await callback.answer("Ничего не изменилось: заявки по-прежнему проходят модерацию", show_alert=True)

    from handlers.admin_sections import settings_return_screen  # ленивый шов (см. admin_settings.py)
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=f"settings_toggle_{key}")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
