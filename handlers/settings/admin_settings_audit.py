"""Квик 260913-16o (задача 2): подтверждение выключения модерации + алерт держателям
`moderate_reg`.

Инцидент прода 06.09: в 05:06 UTC кто-то переключил `full_approval` в «авто», 38 заявок
одобрились молча, и никто из держателей `moderate_reg` не узнал об этом до утра. Этот шов
закрывает обе половины: переход в «авто» требует явного подтверждения (с человеческим
объяснением последствий, CLAUDE.md «бот для людей») и, после подтверждения, шлёт алерт всем
держателям `moderate_reg` с именем и id того, кто нажал (тот же `admin=<id>`, что теперь есть
в логе через `settings_audit.set_setting_by_admin`, но здесь — в чат живым людям, не только
в лог). Обратный переход в ручную модерацию остаётся мгновенным — там нечего терять.

Форма шва — `handlers/regform/admin_reg_form.py`: свой `Router()` здесь НЕ заводится, декоратор
навешивается на общий `router` из `handlers.admin` (тот же инвариант cap-теста
`tests/test_roles_phase8.py`). Импортируется последней строкой `handlers/settings/admin_sections.py`
(тот же хвостовой приём, что и соседи).
"""
import logging

from aiogram import Bot, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.settings.ops import APPROVAL_MODE_KEYS, base_setting_key
from handlers.admin import router
from handlers.access.admin_caps import notify_by_capability
from services.settings.audit import set_setting_by_admin

logger = logging.getLogger(__name__)

# Одна человеческая формулировка на три места (экран подтверждения, тост, алерт держателям
# moderate_reg) — второй копии текста нет. (родительный падеж, предложный падеж).
_FORMS: dict[str, tuple[str, str]] = {
    "full_approval": ("полной формы", "по полной форме"),
    "short_approval": ("краткой формы", "по краткой форме"),
    "party_approval": ("вечеринки", "на вечеринку"),
}

_STALE_BUTTON_TEXT = "Кнопка устарела, откройте раздел заново"


def approval_auto_alert_text(
    key: str,
    *,
    user_id: int | None,
    full_name: str | None,
    username: str | None,
    city_name: str | None = None,
) -> str | None:
    """Текст держателям `moderate_reg`. Его зовут и кнопка бота, и очередь приложения.

    Без `parse_mode`: имя и @username пришли из Telegram. Город — только человеческое имя;
    нет имени — город в текст не добавляется, код не подставляется. Чужой ключ → `None`."""
    form = _FORMS.get(base_setting_key(key))
    if form is None:
        return None
    genitive, prepositional = form
    if username and user_id is not None:
        id_part = f"(@{username}, id {user_id})"
    elif user_id is not None:
        id_part = f"(id {user_id})"
    elif username:
        id_part = f"(@{username})"
    else:
        id_part = ""
    actor = f"{full_name} {id_part}".strip() if full_name else id_part
    city = (city_name or "").strip()
    city_bit = f" Город: {city}." if city else ""
    who_bit = f" Выключил: {actor}." if actor else ""
    return (
        f"⚠️ Модерация {genitive} выключена.{city_bit}{who_bit}\n\n"
        f"Новые заявки {prepositional} теперь одобряются автоматически — без очереди и без "
        "письма «прошёл отбор»."
    )


async def _actor_from_chat(bot, by) -> tuple[int | None, str | None, str | None]:
    """(id, имя, username). `get_chat` упал — id остаётся, имени нет, алерт всё равно уйдёт."""
    try:
        user_id = int(by)
    except (TypeError, ValueError):
        return None, None, None
    try:
        chat = await bot.get_chat(user_id)
        full_name = getattr(chat, "full_name", None)
        if not isinstance(full_name, str) or not full_name.strip():
            first = getattr(chat, "first_name", None) or ""
            last = getattr(chat, "last_name", None) or ""
            full_name = f"{first} {last}".strip()
        else:
            full_name = full_name.strip()
        username = getattr(chat, "username", None)
        if isinstance(username, str):
            username = username.strip().lstrip("@") or None
        else:
            username = None
        return user_id, full_name or None, username
    except Exception:
        logger.info("approval auto alert: get_chat(%s) не ответил, в тексте только id", user_id)
        return user_id, None, None


async def _city_name_for_alert(key: str) -> str | None:
    """Имя города для менеджера (`city_label`). Код и пустое имя в текст не попадают."""
    from domain.cities import city_label, split_per_city_key

    parsed = split_per_city_key(key)
    if parsed is None:
        return None
    _base, code = parsed
    try:
        label = await city_label(code)
    except Exception:
        logger.warning("approval auto alert: имя города не прочиталось")
        return None
    label = (label or "").strip()
    if not label or label == code:
        return None
    return label


async def notify_approval_turned_auto(bot, keys, by) -> None:
    """Алерт по списку из очереди `settings_changed`. Чужой ключ пропускается и не роняет разбор."""
    if not isinstance(keys, (list, tuple)):
        return
    pending = [str(key) for key in keys if base_setting_key(str(key)) in APPROVAL_MODE_KEYS]
    if not pending:
        return
    user_id, full_name, username = await _actor_from_chat(bot, by)
    for key in pending:
        text = approval_auto_alert_text(
            key,
            user_id=user_id,
            full_name=full_name,
            username=username,
            city_name=await _city_name_for_alert(key),
        )
        if text is None:
            continue
        await notify_by_capability(bot, "moderate_reg", text)


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
    genitive, _prepositional = form

    await set_setting_by_admin(callback.from_user.id, key, "auto")

    who = callback.from_user
    # Тот же текст, что у очереди приложения. БЕЗ parse_mode: имя и username — от человека.
    alert_text = approval_auto_alert_text(
        key, user_id=who.id, full_name=who.full_name, username=who.username,
    )
    await notify_by_capability(bot, "moderate_reg", alert_text)

    await callback.answer(f"Модерация {genitive}: ⚡ Авто", show_alert=True)

    from handlers.settings.admin_sections import settings_return_screen  # ленивый шов (см. admin_settings.py)
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=f"settings_toggle_{key}")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("approval_auto_no:"))
async def approval_auto_no(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    if key not in _FORMS:
        await callback.answer(_STALE_BUTTON_TEXT, show_alert=True)
        return

    await callback.answer("Ничего не изменилось: заявки по-прежнему проходят модерацию", show_alert=True)

    from handlers.settings.admin_sections import settings_return_screen  # ленивый шов (см. admin_settings.py)
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=f"settings_toggle_{key}")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
