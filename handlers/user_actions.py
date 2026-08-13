import html
import logging
from datetime import datetime
from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from database.db import (
    get_user,
    get_referrals,
    get_setting,
    get_balance,
    get_leaderboard,
    get_user_rank,
    create_question,
    list_active_tasks,
    get_task,
    get_active_submission,
    create_submission,
)
from handlers.admin_caps import notify_by_capability  # D-13: fan out by capability, not bare ADMIN_IDS
from keyboards.builders import (
    get_cancel_kb,
    get_main_menu_kb,
    get_info_submenu_kb,
    get_socials_kb
)
from handlers.states import Question, GameSubmit
from config import config

router = Router()
logger = logging.getLogger(__name__)

def _gate_decision(status) -> tuple[bool, str | None]:
    """Map a user's status to (allowed, denial_kind). Legacy/missing/unknown -> allowed
    (the ~590 live users have status='approved' via the migration default)."""
    status = status or "approved"
    if status == "pending":
        return False, "pending"
    if status == "rejected":
        return False, "rejected"
    return True, None  # approved + any unknown legacy value


async def ensure_registered(message: types.Message) -> bool:
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Чтобы пользоваться ботом, сначала нужно зарегистрироваться. Отправь команду /start.",
        )
        return False

    allowed, kind = _gate_decision(user.get("status"))
    if allowed:
        return True
    if kind == "pending":
        await message.answer("⏳ Твоя заявка на рассмотрении. Доступ откроется после одобрения.")
    else:  # rejected
        await message.answer(
            await get_setting("reject_text") or "К сожалению, твоя заявка отклонена.",
        )
    return False


# --- Coins (COIN-03) ---

def render_leaderboard(rows: list, requester_id: int, requester_rank, requester_balance: int) -> str:
    lines = ["🏆 <b>Рейтинг по монетам</b>", ""]
    if not rows:
        lines.append("Пока ни у кого нет монет.")
    else:
        for i, row in enumerate(rows, start=1):
            name = row.get("full_name") or row.get("username") or str(row.get("user_id"))
            lines.append(f"{i}. {html.escape(str(name))} — {row.get('balance', 0)}")
    lines.append("")
    rank_text = requester_rank if requester_rank is not None else "—"
    lines.append(f"Твоё место: <b>{rank_text}</b> · баланс: <b>{requester_balance}</b>")
    return "\n".join(lines)


@router.message(F.text == "🪙 Мои монеты")
async def show_my_coins(message: types.Message):
    if not await ensure_registered(message):
        return
    balance = await get_balance(message.from_user.id)
    await message.answer(f"🪙 Твой баланс: <b>{balance}</b> монет(ы)", parse_mode="HTML")


@router.message(Command("рейтинг", "rating", "leaderboard"))
async def show_leaderboard(message: types.Message):
    if not await ensure_registered(message):
        return
    rows = await get_leaderboard(10)
    rank = await get_user_rank(message.from_user.id)
    balance = await get_balance(message.from_user.id)
    await message.answer(
        render_leaderboard(rows, message.from_user.id, rank, balance),
        parse_mode="HTML",
    )


# --- Gamification: task list + submission (GAME-01/02, wave 3, 09-03) ---

async def _render_game_task_line(task: dict, active: dict | None) -> tuple[str, bool]:
    """Renders one task's status line for the delegate list. Returns (line, needs_submit_button)
    -- needs_submit_button is True only when `active is None` (task not yet claimed by this
    delegate), matching D-08's «одна сдача на пару» invariant surfaced to the delegate."""
    category = html.escape(str(task["category"]))
    text_preview = html.escape(str(task["text"])[:80])
    try:
        deadline = datetime.strptime(task["deadline_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        deadline = str(task["deadline_at"] or "—")
    base = f"{category} · {text_preview} · {task['coins']}🪙 · до {deadline}"

    if active is None:
        return f"📤 {base}", True
    if active["status"] == "pending":
        submitted = active.get("submitted_at") or "—"
        return f"📤 {base}\n⏳ на проверке, сдано {submitted}", False
    if active["status"] == "approved":
        coins_awarded = active.get("coins_awarded")
        return f"✅ {category} · {text_preview} · одобрено, +{coins_awarded}🪙", False
    # 'rejected' submissions never come back from get_active_submission (D-05) -- unreachable
    # in practice, kept as a fail-soft fallback rather than a silent KeyError.
    return f"📤 {base}", True


@router.message(F.text == "🎯 Задания")
async def show_game_tasks(message: types.Message):
    if not await ensure_registered(message):
        return

    tasks = await list_active_tasks()
    if not tasks:
        await message.answer("Активных заданий сейчас нет. Загляни попозже!")
        return

    lines = []
    buttons = []
    for task in tasks:
        active = await get_active_submission(task["id"], message.from_user.id)
        line, needs_button = await _render_game_task_line(task, active)
        lines.append(line)
        if needs_button:
            buttons.append([InlineKeyboardButton(
                text="Сдать", callback_data=f"mytask_submit:{task['id']}",
            )])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer("\n\n".join(lines), parse_mode="HTML", reply_markup=kb)


_GS_PROOF_PROMPTS = {
    "photo": "Пришли скриншот/фото:",
    "pdf": "Пришли файл (PDF):",
    "text": "Напиши текстом:",
    "link": "Пришли ссылку:",
}

_GS_MISMATCH_PROMPTS = {
    "photo": "Пришли, пожалуйста, скриншот/фото.",
    "pdf": "Пришли, пожалуйста, файл (PDF).",
    "text": "Пришли, пожалуйста, текст.",
    "link": "Пришли, пожалуйста, ссылку.",
}


@router.callback_query(F.data.startswith("mytask_submit:"))
async def mytask_submit_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return

    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return

    # A-05 (созвон 13.08): дедлайн мягкий -- НЕ блокирует сдачу. Единственный оставшийся
    # серверный гвард на этом пути -- дубль-сдача (T-09-09), проверяется ниже.
    active = await get_active_submission(task_id, callback.from_user.id)
    if active is not None:
        await callback.answer("Уже отправлено, ожидай проверки", show_alert=True)
        return

    await state.update_data(gs_task_id=task_id)

    prompt = _GS_PROOF_PROMPTS.get(task["proof_type"], "Пришли подтверждение:")
    try:
        deadline_passed = (
            datetime.strptime(task["deadline_at"], "%Y-%m-%d %H:%M:%S") <= datetime.now()
        )
    except (TypeError, ValueError):
        deadline_passed = False
    if deadline_passed:
        # Делегат не должен узнавать об этом только из отсутствия коинов -- предупреждаем
        # прямо в промпте, отправка при этом РАЗРЕШЕНА (A-05, созвон 13.08).
        prompt = (
            "⏰ Срок сдачи вышел. Отправить можно, но начислять коины будет решать менеджер.\n\n"
            + prompt
        )

    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await state.set_state(GameSubmit.proof)
    await callback.answer()


@router.message(GameSubmit.proof, F.text.in_({"Отмена"}))
async def cancel_game_submit(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(GameSubmit.proof)
async def receive_proof(message: types.Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("gs_task_id")
    task = await get_task(task_id)
    if task is None:
        # Задание исчезло, пока делегат печатал -- выходим из состояния, не молчим.
        await state.set_state(None)
        await message.answer("Это задание больше не доступно.", reply_markup=ReplyKeyboardRemove())
        return

    proof_type = task["proof_type"]
    content = None
    if proof_type == "photo" and message.photo:
        content = message.photo[-1].file_id
    elif proof_type == "pdf" and message.document:
        content = message.document.file_id
    elif proof_type in ("text", "link") and message.text:
        content = message.text

    if content is None:
        await message.answer(_GS_MISMATCH_PROMPTS.get(proof_type, "Пришли, пожалуйста, подтверждение."))
        return  # остаёмся в GameSubmit.proof, create_submission НЕ вызывается

    submission_id = await create_submission(
        task_id, message.from_user.id, content_type=proof_type, content=content,
        submitted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if submission_id is None:
        # T-09-01/D-05: гонка -- параллельная сдача той же пары успела раньше. Партиционный
        # индекс отклонил вставку. Без уведомления менеджеров, без технической ошибки делегату.
        await state.set_state(None)
        await message.answer(
            "Уже отправлено — кто-то опередил на долю секунды. Обнови список заданий.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await state.set_state(None)
    await message.answer("Принято! Менеджер проверит и начислит монеты.", reply_markup=ReplyKeyboardRemove())

    submitter_name = message.from_user.full_name or str(message.from_user.id)
    # D-13: fan out to every current moderate_game holder, not a bare loop over ADMIN_IDS.
    await notify_by_capability(
        bot, "moderate_game",
        f"🎮 Новая сдача по заданию «{html.escape(str(task['text'])[:60])}» от "
        f"{html.escape(str(submitter_name))}",
        parse_mode="HTML",
    )


@router.message(F.text == "💳 Оплата")
async def upload_receipt_entry(message: types.Message, bot: Bot):
    """Re-entry into the payment step for a user who deferred (or lost FSM state on a
    bot restart). The button only appears while a receipt is owed, but re-check here in
    case status changed since the keyboard was rendered."""
    if not await ensure_registered(message):
        return
    from handlers.payment import should_offer_receipt_upload, start_payment_step
    if not await should_offer_receipt_upload(message.from_user.id):
        await message.answer("Оплатили или оплата не требуется.")
        return
    try:
        user_row = await get_user(message.from_user.id)
        participant_type = (user_row or {}).get("participant_type") or "full"
    except Exception as e:
        logger.error(f"Failed to resolve participant_type for {message.from_user.id}, defaulting to 'full': {e}")
        participant_type = "full"
    await start_payment_step(bot, message.from_user.id, participant_type)


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
        text += f"🗓 <b>Дата:</b> {html.escape(event_date)}\n"
        if event_time:
            text += f"⌚ <b>Время:</b> {html.escape(event_time)}\n"
        text += f"📍 <b>Место:</b> {html.escape(place_name)}"
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
        text = f"🗓 Форум пройдет <b>{html.escape(event_date)}</b>!"
        if event_time:
            text += f"\n⌚ Время: {html.escape(event_time)}"
    else:
        text = "🗓 Дата пока уточняется. Скоро сообщим! 🙂"
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "info_place")
async def info_place(callback: types.CallbackQuery):
    place_name = await get_setting("event_place_name")
    place_address = await get_setting("event_place_address")
    if place_name:
        text = f"<b>Наша площадка — {html.escape(place_name)}!</b> 🚀"
        if place_address:
            text += f"\n\n📍 <b>Адрес:</b> {html.escape(place_address)}"

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
    program_caption = html.escape(program_caption) if program_caption else program_caption

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
    speakers_caption = html.escape(speakers_caption) if speakers_caption else speakers_caption

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
    # WR-04: an invalid admin URL (BUTTON_URL_INVALID) or stray &/< in a contact field under
    # the bot's default HTML parse mode would otherwise fail this send with no fallback.
    try:
        await message.answer(text, reply_markup=get_socials_kb(contact_tg, contact_vk))
    except Exception as e:
        logger.error(f"show_contacts send failed for {message.from_user.id}: {e}")
        await message.answer(text, parse_mode=None)

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

    names = "\n".join(f"• {html.escape(str(name))}" for name in referrals)
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
    await message.answer("Действие отменено.", reply_markup=await get_main_menu_kb(message.from_user.id))


@router.message(Question.waiting_for_question)
async def process_question(message: types.Message, state: FSMContext, bot: Bot):
    if not message.text:
        await message.answer("Пожалуйста, отправь вопрос текстом.")
        return
    question_text = message.text
    logger.info(f"User {message.from_user.id} sent question: {question_text}")
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.from_user.id}"

    # D-14: the row is created ONCE, before the D-13 fan-out below -- every recipient's copy
    # of admin_text embeds the SAME question_id, so a reply from any one of them resolves to
    # the same claim target (08-RESEARCH Pitfall 6). Do not move this call after the fan-out.
    question_id = await create_question(message.from_user.id, question_text)

    admin_text = (
        f"❓ <b>Новый вопрос от {user_info}:</b>\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"🧾 Вопрос #<code>{question_id}</code>\n\n"
        f"{html.escape(question_text)}\n\n"
        f"<i>↩️ Ответьте reply'ем на это сообщение, чтобы отправить ответ.</i>"
    )

    # D-13: fan out to every current moderate_reg holder (falls back to config.ADMIN_IDS if
    # nobody holds it -- T-08-31, never silently dropped).
    sent_count = await notify_by_capability(bot, "moderate_reg", admin_text, parse_mode="HTML")

    if sent_count > 0:
        await message.answer("Твой вопрос отправлен!", reply_markup=await get_main_menu_kb(message.from_user.id))
    elif config.ADMIN_IDS:
        logger.error(f"Failed to send question from {message.from_user.id} to any admin")
        await message.answer("Не удалось отправить вопрос, попробуйте позже.", reply_markup=await get_main_menu_kb(message.from_user.id))
    else:
        logger.warning("No admins configured to receive questions")
        await message.answer("Администраторы не настроены.", reply_markup=await get_main_menu_kb(message.from_user.id))


    await state.clear()
