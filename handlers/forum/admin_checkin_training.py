"""«🧪 Учебные QR» (бэклог чек-ина №7): лист A4 с пятью учебными QR для тренировки сканера.

Кнопка — на экране «✅ Отметки на форуме» (`handlers/forum/admin_checkin.py`), сам лист и учебные
токены — `services/checkin_training.py`. Волонтёр сканирует лист в точке «🧪 Тренировка»
Mini App (или в любой точке — учебный QR всё равно ничего не отметит) либо своим
приложением-сканером и присылает выгрузку в обычную загрузку CSV: учебные коды там
считаются отдельной строкой и не пишутся.

Право: `checkin` (волонтёр берёт лист сам) ИЛИ `moderate_reg` (менеджер на инструктаже). Карта
прав знает одно право на кнопку, поэтому ключ `checkin_training_sheet` в `ADMIN_CAPS` — «любое
право панели», а пара проверяется здесь (`_ALLOWED_CAPS`). Лист не даёт никаких прав и ничего не
пишет — широкий вход ничем не рискует.

Форма шва — как у соседей: своего `Router()` нет, `from handlers.admin import router`, импорт
из хвоста `handlers/forum/admin_checkin.py`."""
import asyncio
import logging

from aiogram import F, types
from aiogram.types import BufferedInputFile, InputMediaPhoto

from handlers.admin import router
from handlers.access.admin_caps import resolve_capabilities
from services import checkin_training
from services.i18n import i18n

logger = logging.getLogger(__name__)

_ALLOWED_CAPS = ("checkin", "moderate_reg")


def sheet_allowed(caps: set) -> bool:
    """Право на лист: `checkin` ИЛИ `moderate_reg`. Этим же фильтром хаб «🎪 Форум: функции»
    решает, рисовать ли кнопку (карта прав видит здесь «любое право панели»)."""
    return any(cap in caps for cap in _ALLOWED_CAPS)
_NO_RIGHTS = "Лист учебных QR доступен волонтёрам чек-ина и менеджерам заявок."


@router.callback_query(F.data == "checkin_training_sheet")
async def checkin_training_sheet(callback: types.CallbackQuery):
    caps = await resolve_capabilities(callback.from_user.id)
    if not sheet_allowed(caps):
        await callback.answer(_NO_RIGHTS, show_alert=True)
        return
    await callback.answer()
    lang, tr_map = await i18n.context(callback.from_user.id)
    caption = await i18n.tr_setting("checkin_training_sheet_caption_text", lang, tr_map) or ""
    try:
        # Pillow (шрифты, рендер) блокирует — в потоке, чтобы бот не вставал на секунду.
        inputs = await checkin_training.training_sheet_inputs(lang, tr_map)
        png = await asyncio.to_thread(checkin_training.render_training_sheet, inputs)
    except Exception:  # noqa: BLE001 — нет Pillow/шрифта: пять QR отдельными картинками
        logger.warning("checkin_training_sheet: лист A4 не собрался, шлю QR по одному", exc_info=True)
        pngs = await checkin_training.training_qr_pngs(lang, tr_map)
        await callback.bot.send_media_group(callback.from_user.id, [
            InputMediaPhoto(media=BufferedInputFile(img, filename=f"training_{i}.png"), caption=text)
            for i, (img, text) in enumerate(pngs, start=1)
        ])
        await callback.message.answer(caption)
        return
    fits = len(caption) <= 1024  # предел подписи Telegram; длинный текст — отдельным сообщением
    await callback.bot.send_document(
        callback.from_user.id,
        BufferedInputFile(png, filename="training_qr_a4.png"),
        caption=caption if fits else None,
    )
    if not fits:
        await callback.message.answer(caption)
