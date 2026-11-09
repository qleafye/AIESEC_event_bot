"""Подтверждение экрана «как прочитает делегат» для тестов, которые проверяют саму запись.

Экран стоит до set_setting. Тест, которому нужна уже записанная настройка, зовёт это
после settings_edit_value. Если экрана не было (число, вкладка, отказ), вызов ничего не делает.
"""
from __future__ import annotations


async def confirm_delegate_preview(message, state) -> bool:
    data = await state.get_data()
    pending = data.get("text_preview_value")
    if not isinstance(pending, str):
        return False
    from handlers.settings.admin_settings import settings_edit_value
    from handlers.states import EditSetting

    await state.update_data(text_preview_ack=pending, ph_ack=pending)
    await state.set_state(EditSetting.waiting_for_value)
    await settings_edit_value(message, state)
    return True
