import html
import logging
from aiogram import Router, F, types, Bot
from aiogram.types import FSInputFile
from aiogram.fsm.context import FSMContext
from database.db import get_user, get_referrals, get_setting
from keyboards.builders import (
    get_cancel_kb, 
    get_main_menu_kb,
    get_info_submenu_kb,
    get_socials_kb
)
from handlers.states import Question
from config import config

router = Router()
logger = logging.getLogger(__name__)

async def ensure_registered(message: types.Message) -> bool:
    user = await get_user(message.from_user.id)
    if user:
        return True

    await message.answer(
        "Чтобы пользоваться ботом, сначала нужно зарегистрироваться. Отправь команду /start.",
    )
    return False


#ℹ️ Информация о форуме
@router.message(F.text == "ℹ️ Информация о форуме")
async def show_info_menu(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Info menu")

    event_date = await get_setting("event_date")
    event_time = await get_setting("event_time")
    place_name = await get_setting("event_place_name")

    if event_date and place_name:
        text = "<b>Информация о мероприятии</b>\n\n"
        text += f"🗓 <b>Дата:</b> {event_date}\n"
        if event_time:
            text += f"⌚ <b>Время:</b> {event_time}\n"
        text += f"📍 <b>Место:</b> {place_name}"
    else:
        text = (
            "Информация о мероприятии пока заполняется.\n\n"
            "Выбери, что тебя интересует:"
        )
    await message.answer(text, reply_markup=get_info_submenu_kb(), parse_mode="HTML")

@router.callback_query(F.data == "info_date")
async def info_date(callback: types.CallbackQuery):
    event_date = await get_setting("event_date")
    event_time = await get_setting("event_time")
    if event_date:
        text = f"🗓 Форум пройдет <b>{event_date}</b>!"
        if event_time:
            text += f"\n⌚ Время: {event_time}"
    else:
        text = "🗓 Дата пока уточняется. Скоро сообщим! 🙂"
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "info_place")
async def info_place(callback: types.CallbackQuery):
    place_name = await get_setting("event_place_name")
    place_address = await get_setting("event_place_address")
    if place_name:
        text = f"<b>Наша площадка — {place_name}!</b> 🚀"
        if place_address:
            text += f"\n\n📍 <b>Адрес:</b> {place_address}"

        venue_photo = await get_setting("venue_photo_file_id")
        if venue_photo:
            try:
                await callback.message.answer_photo(venue_photo, caption=text, parse_mode="HTML")
                await callback.answer()
                return
            except Exception:
                pass

        try:
            photo = FSInputFile("resources/venue.jpg")
            await callback.message.answer_photo(photo, caption=text, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, parse_mode="HTML")
    else:
        await callback.message.answer(
            "📍 Место проведения в процессе подтверждения. Как только всё будет готово, мы напишем!"
        )

    await callback.answer()


# 📅 Программа форума
@router.message(F.text == "📅 Программа форума")
async def show_program(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Program")

    program_file_id = await get_setting("program_photo_file_id")
    program_caption = await get_setting("program_caption")

    if program_file_id:
        try:
            await message.answer_photo(program_file_id, caption=program_caption, parse_mode="HTML")
            return
        except Exception:
            pass

    try:
        photo = FSInputFile("resources/program.jpg")
        await message.answer_photo(photo, caption=program_caption, parse_mode="HTML")
    except Exception:
        await message.answer("Программа форума ещё не загружена.")

# 🗣 Спикеры
@router.message(F.text == "🗣 Спикеры")
async def show_speakers(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Speakers")

    speakers_file_id = await get_setting("speakers_photo_file_id")
    speakers_caption = await get_setting("speakers_caption")

    if speakers_file_id:
        try:
            await message.answer_photo(speakers_file_id, caption=speakers_caption, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer("Список спикеров формируется и скоро появится здесь.")

# 📞 Контакты
@router.message(F.text == "📞 Контакты")
async def show_contacts(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Contacts")

    contact_person = await get_setting("contact_person")
    contact_vk = await get_setting("contact_vk")
    contact_tg = await get_setting("contact_tg")

    if not contact_person and not contact_vk and not contact_tg:
        await message.answer("Контакты пока не указаны. Обратитесь к организаторам.")
        return

    parts = []
    if contact_person:
        parts.append(f"По всем вопросам пиши сюда: {contact_person}")
    links = []
    if contact_vk:
        links.append(f"VK: {contact_vk}")
    if contact_tg:
        links.append(f"TG: {contact_tg}")
    if links:
        parts.append("Наши группы:\n" + "\n".join(links))

    text = "\n\n".join(parts)
    await message.answer(text, reply_markup=get_socials_kb(contact_tg, contact_vk))

@router.message(F.text == "🔗 Моя реферальная ссылка")
async def my_referral_link(message: types.Message, bot: Bot):
    if not await ensure_registered(message):
        return

    bot_user = await bot.get_me()
    referral_link = f"https://t.me/{bot_user.username}?start={message.from_user.id}"
    await message.answer(
        "Отправь эту ссылку друзьям, чтобы пригласить их на форум!\n\n"
        f"{referral_link}"
    )


@router.message(F.text == "👥 Мои приглашённые")
async def my_referrals(message: types.Message, bot: Bot):
    if not await ensure_registered(message):
        return

    referrals = await get_referrals(message.from_user.id)

    if not referrals:
        bot_user = await bot.get_me()
        referral_link = f"https://t.me/{bot_user.username}?start={message.from_user.id}"
        await message.answer(
            "Пока никто не зарегистрировался по твоей ссылке.\n\n"
            f"Поделись ей с друзьями:\n{referral_link}"
        )
        return

    names = "\n".join(f"• {name}" for name in referrals)
    await message.answer(
        f"👥 <b>Твои приглашённые ({len(referrals)}):</b>\n\n{names}",
        parse_mode="HTML",
    )


# ❓ Задать вопрос
@router.message(F.text == "❓ Задать вопрос")
async def ask_organizer_start(message: types.Message, state: FSMContext):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} wants to ask a question")
    await message.answer(
        "Напиши свой вопрос, и мы передадим его организаторам.",
        reply_markup=get_cancel_kb()
    )
    await state.set_state(Question.waiting_for_question)

@router.message(Question.waiting_for_question, F.text.in_({"Отмена", "/cancel"}))
async def cancel_question(message: types.Message, state: FSMContext):
    logger.info(f"User {message.from_user.id} canceled question")
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=await get_main_menu_kb())


@router.message(Question.waiting_for_question)
async def process_question(message: types.Message, state: FSMContext, bot: Bot):
    if not message.text:
        await message.answer("Пожалуйста, отправь вопрос текстом.")
        return
    question_text = message.text
    logger.info(f"User {message.from_user.id} sent question: {question_text}")
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.from_user.id}"

    admin_text = (
        f"❓ <b>Новый вопрос от {user_info}:</b>\n"
        f"🆔 <code>{message.from_user.id}</code>\n\n"
        f"{html.escape(question_text)}\n\n"
        f"<i>↩️ Ответьте reply'ем на это сообщение, чтобы отправить ответ.</i>"
    )
    
    # Send to all admins
    sent_count = 0
    if config.ADMIN_IDS:
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text, parse_mode="HTML")
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send question to admin {admin_id}: {e}")
                pass
        
        if sent_count > 0:
            await message.answer("Твой вопрос отправлен!", reply_markup=await get_main_menu_kb())
        else:
            logger.error(f"Failed to send question from {message.from_user.id} to any admin")
            await message.answer("Не удалось отправить вопрос, попробуйте позже.", reply_markup=await get_main_menu_kb())
    else:
        logger.warning("No admins configured to receive questions")
        await message.answer("Администраторы не настроены.", reply_markup=await get_main_menu_kb())
    
    await state.clear()
