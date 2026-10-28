"""«🛍 Магазин баллов»: фото товара — загрузить, заменить, убрать (с подтверждением).

INVARIANT: каждый @router.* — декоратор в ОДНУ строку (golden-снапшот роутеров читает их построчно).

Менеджер жмёт в карточке товара «🖼 Загрузить фото» / «🖼 Заменить фото» и присылает картинку
обычным фото или файлом (JPG, PNG, WEBP, до 10 МБ). Файл бот скачивает и присылает обратно как
фото: в товар пишется file_id ЭТОГО фото, а не документа — документ Телеграм не покажет ни в
альбоме витрины (`InputMediaPhoto`), ни как картинку в Mini App. Заодно менеджер сразу видит превью.

Шов `handlers/game/admin_shop.py`: хендлеры на общем `admin.router`, импорт хвостом admin_shop.
Колбэки — под префиксом `shp_*`, состояние — `ShopAdmin.photo`: право `moderate_game` уже
выдаётся записями ADMIN_CAPS магазина, новых записей не нужно."""
from __future__ import annotations

import html
import io
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile

from database import shop_db
from handlers.game.admin_shop import _edit_or_answer, _kb, item_text_kb, router
from handlers.states import ShopAdmin

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 10 * 1024 * 1024
_IMAGE_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_GONE = "Товар уже удалён."
_PROMPT = (
    "Пришлите фото товара — так его увидят делегаты в витрине.\n\n"
    "Можно обычным фото или файлом-картинкой (JPG, PNG, WEBP, до 10 МБ)."
)
_NOT_IMAGE = (
    "Это не картинка. Пришлите фото товара или файл JPG, PNG или WEBP. "
    "Передумали — нажмите «Отмена»."
)
_TOO_BIG = "Файл больше 10 МБ — Телеграм не покажет его как фото. Сожмите картинку или пришлите обычным фото."
_BROKEN = (
    "Телеграм не смог открыть этот файл как фото. Пришлите картинку обычным фото "
    "или другим файлом JPG, PNG или WEBP."
)


def _cancel_kb(iid: int):
    return _kb([[("Отмена", f"shp_it:{iid}")]])


def _saved(title: str) -> str:
    return f"✅ Фото товара «{title}» сохранено — так его увидят делегаты."


@router.callback_query(F.data.startswith("shp_ph:"))
async def shop_photo_start(callback: types.CallbackQuery, state: FSMContext):
    item = await shop_db.get_item(int(callback.data.split(":", 1)[1]))
    if item is None:
        await callback.answer(_GONE, show_alert=True)
        return
    await state.set_state(ShopAdmin.photo)
    await state.update_data(shp_photo_item=item["id"])
    if item.get("photo_file_id"):
        try:
            await callback.message.answer_photo(item["photo_file_id"], caption="Сейчас у товара это фото.")
        except TelegramAPIError as e:  # старое фото недоступно — просто попросим новое
            logger.info(f"admin_shop_photo: текущее фото товара {item['id']} не показалось: {type(e).__name__}")
    await callback.message.answer(_PROMPT, reply_markup=_cancel_kb(item["id"]))
    await callback.answer()


async def _item_in_state(message: types.Message, state: FSMContext) -> dict | None:
    iid = (await state.get_data()).get("shp_photo_item")
    item = await shop_db.get_item(int(iid)) if iid else None
    if item is None:
        await state.set_state(None)
        await message.answer(_GONE)
    return item


async def _finish(message: types.Message, state: FSMContext, item: dict, file_id: str) -> None:
    await shop_db.set_item_photo(item["id"], file_id)
    await state.set_state(None)
    text, kb = await item_text_kb(await shop_db.get_item(item["id"]) or item)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(ShopAdmin.photo, F.photo)
async def shop_photo_got_photo(message: types.Message, state: FSMContext):
    item = await _item_in_state(message, state)
    if item is None:
        return
    file_id = message.photo[-1].file_id
    await message.answer_photo(file_id, caption=_saved(item["title"]))
    await _finish(message, state, item, file_id)


@router.message(ShopAdmin.photo, F.document)
async def shop_photo_got_document(message: types.Message, state: FSMContext):
    item = await _item_in_state(message, state)
    if item is None:
        return
    doc = message.document
    ext = _IMAGE_MIME.get((doc.mime_type or "").lower())
    if ext is None:
        await message.answer(_NOT_IMAGE, reply_markup=_cancel_kb(item["id"]))
        return
    if doc.file_size is not None and doc.file_size > MAX_PHOTO_BYTES:
        await message.answer(_TOO_BIG, reply_markup=_cancel_kb(item["id"]))
        return
    try:
        buf = io.BytesIO()
        await message.bot.download(doc, destination=buf)
        sent = await message.answer_photo(
            BufferedInputFile(buf.getvalue(), filename=f"item{item['id']}.{ext}"), caption=_saved(item["title"]),
        )
        file_id = sent.photo[-1].file_id
    except (TelegramAPIError, AttributeError, IndexError, TypeError) as e:
        logger.info(f"admin_shop_photo: файл-картинка товара {item['id']} не превратилась в фото: {type(e).__name__}")
        await message.answer(_BROKEN, reply_markup=_cancel_kb(item["id"]))
        return
    await _finish(message, state, item, file_id)


@router.message(ShopAdmin.photo)
async def shop_photo_other(message: types.Message, state: FSMContext):
    iid = (await state.get_data()).get("shp_photo_item")
    await message.answer(_NOT_IMAGE, reply_markup=_cancel_kb(iid) if iid else None)


@router.callback_query(F.data.startswith("shp_phdel:"))
async def shop_photo_delete(callback: types.CallbackQuery):
    item = await shop_db.get_item(int(callback.data.split(":", 1)[1]))
    if item is None:
        await callback.answer(_GONE, show_alert=True)
        return
    text = (
        f"Убрать фото у «{html.escape(item['title'])}»?\n\n"
        "Делегаты перестанут видеть его в витрине в чате и в приложении. "
        "Сам товар, цена и остаток не меняются."
    )
    kb = _kb([[("🗑 Да, убрать фото", f"shp_phdelgo:{item['id']}"), ("Не убирать", f"shp_it:{item['id']}")]])
    await _edit_or_answer(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("shp_phdelgo:"))
async def shop_photo_delete_go(callback: types.CallbackQuery):
    item = await shop_db.get_item(int(callback.data.split(":", 1)[1]))
    if item is None:
        await callback.answer(_GONE, show_alert=True)
        return
    await shop_db.set_item_photo(item["id"], None)
    text, kb = await item_text_kb(await shop_db.get_item(item["id"]))
    await _edit_or_answer(callback, text, kb)
    await callback.answer("Фото убрано")
