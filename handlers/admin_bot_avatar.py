"""«🖼 Аватар бота» (раздел «🎪 Событие»): менеджер присылает фото — бот ставит его аватаром
в Telegram сам (`setMyProfilePhoto`), без BotFather. Там же «🗑 Убрать аватар» с
подтверждением (`removeMyProfilePhoto`).

Аватар в базе не хранится: Telegram держит его сам, а «фото профиля нельзя переиспользовать
по file_id» (Bot API) — бот скачивает присланное фото и загружает его заново. Поэтому
настройки реестра под аватар нет, и в приложении его не поменять — только здесь.

Форма шва — как у соседей (`handlers/forum/admin_forum_tz.py`): своего `Router()` нет,
`from handlers.admin import router`, импорт из хвоста `handlers/admin.py`. Право — `settings`,
как у остальных строк раздела «🎪 Событие».
"""
import io
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from handlers.states import BotAvatar

logger = logging.getLogger(__name__)

INTRO_TEXT = (
    "🖼 <b>Аватар бота</b>\n\n"
    "Пришлите фото — бот спросит подтверждение и только после него поставит фото аватаром "
    "в Telegram. Лучше квадратное, главное — по центру: Telegram обрежет картинку до круга.\n\n"
    "Отправляйте именно как фото, не файлом."
)
NOT_PHOTO_TEXT = "Не понял — пришлите картинку как фото (не файлом) или нажмите «✖️ Отмена»."
DONE_TEXT = "✅ Аватар бота обновлён. У людей в Telegram он сменится в течение пары минут."
CONFIRM_TEXT = "Поставить это фото аватаром бота?"
EXPIRED_TEXT = "Фото для аватара не нашлось — откройте «🖼 Аватар бота» и пришлите его заново."
REMOVE_CONFIRM_TEXT = (
    "Убрать аватар бота?\n\nВместо картинки у бота в Telegram останется цветной кружок с первой "
    "буквой имени. Поставить новый аватар можно в любой момент."
)
REMOVED_TEXT = "✅ Аватар бота убран."
CANCELLED_TEXT = "Аватар не менялся."
UNSUPPORTED_TEXT = (
    "Эта версия бота не умеет менять аватар. Поставьте его в @BotFather: /mybots → ваш бот → "
    "Edit Bot → Edit Botpic."
)

_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✖️ Отмена", callback_data="botava_cancel")],
])


def _intro_kb() -> InlineKeyboardMarkup:
    # «Назад» здесь — та же «Отмена»: она снимает ожидание фото. Обычная кнопка раздела
    # оставила бы его висеть, и случайное фото позже молча сменило бы аватар.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Убрать аватар", callback_data="botava_rm")],
        [InlineKeyboardButton(text="← Назад", callback_data="botava_cancel")],
    ])


def _wait_text(seconds: int) -> str:
    minutes = max(1, -(-int(seconds) // 60))
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest} мин" if rest else f"{hours} ч"


def telegram_error_text(exc: Exception) -> str:
    """Отказ Telegram — человеческим текстом с подсказкой, что делать."""
    if isinstance(exc, TelegramRetryAfter):
        return (f"Telegram просит подождать со сменой аватара. Попробуйте через "
                f"{_wait_text(exc.retry_after)}.")
    if isinstance(exc, TelegramBadRequest):
        return ("Telegram не принял эту картинку. Пришлите другое фото — обычный снимок или "
                "картинку в JPG, не слишком маленькую.")
    return "Не получилось связаться с Telegram. Попробуйте ещё раз через минуту."


async def set_avatar_from_photo(bot, file_id: str) -> str | None:
    """Скачивает присланное фото и ставит аватаром. `None` — получилось, иначе текст ошибки."""
    if not hasattr(bot, "set_my_profile_photo"):
        return UNSUPPORTED_TEXT
    from aiogram.types import InputProfilePhotoStatic

    try:
        buf = await bot.download(file_id, destination=io.BytesIO())
        data = buf.getvalue()
        await bot.set_my_profile_photo(
            photo=InputProfilePhotoStatic(photo=BufferedInputFile(data, filename="avatar.jpg")),
        )
    except Exception as exc:  # noqa: BLE001 — в лог причину, человеку — что делать
        logger.warning("bot_avatar: аватар не поставлен: %s", exc)
        return telegram_error_text(exc)
    logger.info("bot_avatar: аватар бота обновлён")
    return None


@router.callback_query(F.data == "admin_bot_avatar")
async def bot_avatar_open(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BotAvatar.photo)
    await callback.message.answer(INTRO_TEXT, parse_mode="HTML", reply_markup=_intro_kb())
    await callback.answer()


@router.callback_query(F.data == "botava_cancel")
async def bot_avatar_cancel(callback: types.CallbackQuery, state: FSMContext):
    """Снимает ожидание фото и возвращает в раздел «🎪 Событие»."""
    from handlers.admin_sections import settings_return_screen  # ленивый шов

    await state.clear()
    text, kb = await settings_return_screen(callback.from_user.id, callback_data="admin_bot_avatar")
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(CANCELLED_TEXT)


@router.message(StateFilter(BotAvatar), Command("cancel"))
@router.message(StateFilter(BotAvatar), F.text == "Отмена")
async def bot_avatar_cancel_text(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(CANCELLED_TEXT)


@router.message(StateFilter(BotAvatar), F.photo)
async def bot_avatar_photo(message: types.Message, state: FSMContext):
    """Фото не ставится сразу: ожидание могло пережить уход с экрана, и фото, присланное
    позже для другого дела, молча стало бы аватаром. Сначала — вопрос с кнопками."""
    await state.set_state(BotAvatar.confirm)
    await state.update_data(avatar_file_id=message.photo[-1].file_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, поставить", callback_data="botava_set_yes")],
        [InlineKeyboardButton(text="← Отмена", callback_data="botava_cancel")],
    ])
    await message.reply(CONFIRM_TEXT, reply_markup=kb)


@router.callback_query(F.data == "botava_set_yes")
async def bot_avatar_set_go(callback: types.CallbackQuery, state: FSMContext):
    file_id = (await state.get_data()).get("avatar_file_id")
    if await state.get_state() != BotAvatar.confirm.state or not file_id:
        await callback.answer(EXPIRED_TEXT, show_alert=True)
        return
    await callback.answer()
    error = await set_avatar_from_photo(callback.bot, file_id)
    if error:
        await state.set_state(BotAvatar.photo)  # можно сразу прислать другое фото
        await callback.message.answer(error, reply_markup=_CANCEL_KB)
        return
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(DONE_TEXT)


# Команды («/…») не перехватываем: менеджер ушёл в другую команду — пусть она и сработает.
@router.message(StateFilter(BotAvatar), ~F.text.startswith("/"))
async def bot_avatar_not_photo(message: types.Message):
    await message.answer(NOT_PHOTO_TEXT, reply_markup=_CANCEL_KB)


@router.callback_query(F.data == "botava_rm")
async def bot_avatar_remove_ask(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, убрать", callback_data="botava_rm_yes")],
        [InlineKeyboardButton(text="← Отмена", callback_data="botava_cancel")],
    ])
    await callback.message.answer(REMOVE_CONFIRM_TEXT, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "botava_rm_yes")
async def bot_avatar_remove_go(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    bot = callback.bot
    if not hasattr(bot, "remove_my_profile_photo"):
        await callback.message.answer(UNSUPPORTED_TEXT)
        await callback.answer()
        return
    try:
        await bot.remove_my_profile_photo()
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot_avatar: аватар не убран: %s", exc)
        await callback.message.answer(telegram_error_text(exc))
        await callback.answer()
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(REMOVED_TEXT)
    await callback.answer()
