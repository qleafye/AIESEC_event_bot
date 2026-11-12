"""Клавиатура делегата во ВК. Пункт без ответа бота не показывается."""
from __future__ import annotations

import json

from domain.cities import get_setting_typed_for_city
from keyboards.builders import MENU_BUTTONS
from keyboards.menu_dynamic import caption_for

# Ключ попадает сюда, когда слушатель реально отвечает на этот пункт.
# Пока список пуст, клавиатура пустая. Mini App не добавлять:
# приложения во ВК нет, отдельная отписка «только в Telegram» не рисуется.
VK_HANDLED_MENU_KEYS: frozenset[str] = frozenset()


async def vk_main_menu_keyboard(vk_id: int, event_city: str | None = None) -> str:
    """JSON для messages.send: живые ответы, в порядке меню Telegram, по две в ряд.

    vk_id оставлен в подписи: город кнопки пока приходит аргументом, карточка ВК
    здесь не читается. Выключенная кнопка и кнопка без ответа скрыты.
    """
    del vk_id
    labels: list[str] = []
    for key, _default in MENU_BUTTONS:
        if key == "menu_miniapp" or key not in VK_HANDLED_MENU_KEYS:
            continue
        if await get_setting_typed_for_city(key, event_city) != "on":
            continue
        labels.append(await caption_for(key, event_city, "ru"))
    rows = [
        [{"action": {"type": "text", "label": label}} for label in labels[i:i + 2]]
        for i in range(0, len(labels), 2)
    ]
    return json.dumps({"one_time": False, "buttons": rows}, ensure_ascii=False)
