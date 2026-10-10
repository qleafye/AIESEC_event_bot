"""Приёмка 09.10: `/admin` от человека без прав — молчание.

Deny-by-default `CapabilityMiddleware` (`handlers/access/admin_caps.py::_deny`) намеренно не отвечает
человеку без единого права и пропускает апдейт дальше (UNHANDLED) — поверхность админки не
раскрывается. Но сам `/admin` дальше не ловил никто, и человек решал, что бот завис. Этот
роутер подключён в main.py СРАЗУ после `admin.router` и отвечает только на `/admin` в личке:
остальные админ-команды по-прежнему молчат.
"""
import logging

from aiogram import F, Router, types
from aiogram.filters import Command

from handlers.i18n import reg_i18n
from handlers.reg_silence_fallback import _is_staff_or_admin

logger = logging.getLogger(__name__)

router = Router(name="admin_no_access")

ADMIN_NO_ACCESS_TEXT = (
    "Это команда для организаторов. Если вы организатор — попросите доступ у руководителя."
)


@router.message(Command("admin"), F.chat.type == "private")
async def admin_no_access(message: types.Message) -> None:
    # Человек с правами сюда не доходит (его /admin забирает admin.router). Если всё же
    # дошёл — молчим, а не говорим организатору «у вас нет доступа».
    if await _is_staff_or_admin(message.from_user.id):
        return
    logger.info("admin_no_access: /admin без прав uid=%s", message.from_user.id)
    await reg_i18n.say(message, ADMIN_NO_ACCESS_TEXT)
