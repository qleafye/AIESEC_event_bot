import asyncio
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
    add_submission_part,
    parse_proof_types,
    count_rejected_submissions,
    task_title,
)
from handlers.admin_caps import notify_by_capability  # D-13: fan out by capability, not bare ADMIN_IDS
from cities import (
    cities_module_on, normalize_city, city_scope,  # Phase 09.1 (B): show_game_tasks city filter
    get_setting_for_city,  # Phase 09.2 (B): contacts/info screens resolve by delegate city
)
from keyboards.builders import (
    get_cancel_kb,
    get_main_menu_kb,
    get_info_submenu_kb,
    get_socials_kb
)
from handlers.states import Question, GameSubmit
from settings_schema import get_setting_typed  # Phase 09.1 (A): flow texts live in the registry
from services.background import spawn as _spawn
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

def _game_task_deadline_short(task: dict) -> tuple[str, bool]:
    """(dd.mm display, is_overdue) — shared by the list line, the button label truncation
    logic below (via task_title), and the card. Quick 260819-gtl: the list line now shows a
    short dd.mm date (CONTEXT.md decision 3), not the full dd.mm.yyyy hh:mm the pre-existing
    card/prompt strings still use elsewhere in this file."""
    try:
        dt = datetime.strptime(task["deadline_at"], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m"), dt <= datetime.now()
    except (TypeError, ValueError):
        return str(task["deadline_at"] or "—"), False


async def _render_game_task_line(index: int, task: dict, active: dict | None) -> tuple[str, bool]:
    """Renders one task's status line for the delegate list. Returns (line, needs_submit_button)
    -- needs_submit_button is True only when `active is None` (task not yet claimed by this
    delegate), matching D-08's «одна сдача на пару» invariant surfaced to the delegate.

    Quick 260819-gtl (CONTEXT.md decision 3): title, not a raw description preview -- the
    description is shown only on the task CARD (`_render_task_card_text` below), never in the
    list. Format: "N. <статус-эмодзи> <title>[ ⏰] · <coins>🪙 · до <дд.мм>" (or the pending/
    approved variant of the tail). `index` is the delegate-facing 1-based row number."""
    title = html.escape(task_title(task))
    deadline, overdue = _game_task_deadline_short(task)
    overdue_mark = " ⏰" if overdue else ""

    if active is None:
        return f"{index}. 📤 {title}{overdue_mark} · {task['coins']}🪙 · до {deadline}", True
    if active["status"] == "pending":
        submitted = active.get("submitted_at") or "—"
        return f"{index}. ⏳ {title} · на проверке, сдано {submitted}", False
    if active["status"] == "approved":
        coins_awarded = active.get("coins_awarded")
        return f"{index}. ✅ {title} · одобрено, +{coins_awarded}🪙", False
    # 'rejected' submissions never come back from get_active_submission (D-05) -- unreachable
    # in practice, kept as a fail-soft fallback rather than a silent KeyError.
    return f"{index}. 📤 {title}{overdue_mark} · {task['coins']}🪙 · до {deadline}", True


async def _game_task_list_screen(user_id: int) -> tuple[str | None, InlineKeyboardMarkup | None]:
    """Shared by `show_game_tasks` (new message) and `mytask_back` (edit_text back from a
    photo-less card) -- (None, None) means "no active tasks", same "no tasks" gate both call
    sites already needed. Quick 260819-gtl extraction, byte-identical resolve logic to the
    pre-existing inline body of show_game_tasks (Phase 09.1 B)."""
    if await cities_module_on():
        user = await get_user(user_id)
        code = normalize_city(user.get("event_city") if user else None)
        tasks = await list_active_tasks(city_scope=city_scope(code))
    else:
        tasks = await list_active_tasks()
    if not tasks:
        return None, None

    lines = []
    buttons = []
    for i, task in enumerate(tasks, start=1):
        active = await get_active_submission(task["id"], user_id)
        line, needs_button = await _render_game_task_line(i, task, active)
        lines.append(line)
        if needs_button:
            buttons.append([InlineKeyboardButton(
                text=_submit_button_label(task), callback_data=f"mytask:{task['id']}",
            )])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n\n".join(lines), kb


@router.message(F.text == "🎯 Задания")
async def show_game_tasks(message: types.Message):
    if not await ensure_registered(message):
        return
    text, kb = await _game_task_list_screen(message.from_user.id)
    if text is None:
        await message.answer("Активных заданий сейчас нет. Загляни попозже!")
        return
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# Phase 14 (GAME-08): delegate-facing submit-button label — several tasks used to share the
# identical literal "Сдать", making the keyboard ambiguous when more than one was open at
# once. Pure/sync (no I/O) -- text-only, never HTML-escaped because this is a button label,
# not parse_mode="HTML" body text.
# Quick 260819-gtl (CONTEXT.md decision 3): "📤 <title>" (обрезка 30), title not raw text.
def _submit_button_label(task: dict) -> str:
    name = task_title(task)
    if len(name) > 30:
        name = name[:30] + "…"
    return f"📤 {name}"


def _render_task_card_text(task: dict) -> str:
    """Quick 260819-gtl (CONTEXT.md decision 5): the task CARD shown when a delegate taps a
    task's button on the list -- title + full description (unlike the list line, which never
    shows the description). Caller truncates the RESULT for a photo caption's 1024-char limit
    (this function itself targets the no-photo/plain-message path, no ceiling of its own)."""
    title = html.escape(task_title(task))
    category = html.escape(str(task["category"]))
    deadline, overdue = _game_task_deadline_short(task)
    lines = [f"<b>{title}</b>", f"{category} · {task['coins']}🪙 · до {deadline}"]
    if overdue:
        # A-05 (созвон 13.08): дословно как в текущем mytask_submit_start -- дедлайн мягкий,
        # сдача разрешена, решение по коинам остаётся за менеджером.
        lines.append("⏰ Срок вышел — отправить можно, монеты решит менеджер")
    lines.append("")
    lines.append(html.escape(str(task["text"])))
    return "\n".join(lines)


def _game_task_card_kb(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Сдать", callback_data=f"mytask_submit:{task_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="mytask_back")],
    ])


@router.callback_query(F.data.startswith("mytask:"))
async def mytask_open(callback: types.CallbackQuery):
    """Quick 260819-gtl (CONTEXT.md decision 5): opens the task card. Photo present ->
    send_photo as a SEPARATE message (edit_text can never turn a text message into a photo
    message) -- the list message keeps its own keyboard untouched. No photo -> edit_text turns
    THIS message (the list) into the card in place, zero new messages in the chat."""
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    task = await get_task(task_id)
    if task is None or task.get("archived_at"):
        # Same wording as mytask_submit_start's own archived-task alert (T-14-01 precedent) --
        # a stale list message can still carry a button for a task removed since it was sent.
        await callback.answer(
            "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Задания», "
            "там актуальный список.",
            show_alert=True,
        )
        return

    card_text = _render_task_card_text(task)
    card_kb = _game_task_card_kb(task_id)
    photo_id = task.get("photo_file_id")
    if photo_id:
        caption = card_text if len(card_text) <= 1024 else card_text[:1021] + "…"
        await callback.message.answer_photo(
            photo_id, caption=caption, parse_mode="HTML", reply_markup=card_kb,
        )
    else:
        await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=card_kb)
    await callback.answer()


@router.callback_query(F.data == "mytask_back")
async def mytask_back(callback: types.CallbackQuery):
    """Quick 260819-gtl (CONTEXT.md decision 5): a photo card is its own message -- "back" just
    removes it (the list message underneath, untouched, still carries its own keyboard, "это
    ок" per CONTEXT.md). A no-photo card IS the (edited) list message -- "back" re-renders the
    list into the SAME message via edit_text."""
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer()
        return
    text, kb = await _game_task_list_screen(callback.from_user.id)
    if text is None:
        await callback.message.edit_text("Активных заданий сейчас нет. Загляни попозже!")
        await callback.answer()
        return
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# Phase 09.1 (A): the flow's texts live in settings_schema (group "game"), not literals here
# -- the old per-type prompt/mismatch literal dicts are gone. proof_type is no longer a
# validator, only a hint baked into the prompt (_build_proof_prompt below).

_LEGACY_CONTENT_TYPE = {"photo": "photo", "document": "pdf", "text": "text", "link": "link"}

# module dict, same "first message wins, spawn one debounced ack" shape as
# handlers/admin_broadcasts.py::pending_albums / _wait_and_send_album (Phase 13, 13-05: moved
# from handlers/admin.py) -- keyed by media_group_id only
# (Telegram media_group_id is unique enough in practice, same assumption the broadcast idiom
# already makes). ONLY collects an ack here -- never finalizes the submission (no timeout).
_gs_pending_albums: dict[str, bool] = {}

# CR-01 (09.1-REVIEW.md): 20 parts x 500 chars in the card is well under Telegram's 4096
# sendMessage limit, and caps MemoryStorage growth -- a delegate (malicious or not) can no
# longer jam the moderation queue by attaching unlimited parts of unlimited length.
MAX_PARTS = 20
MAX_TEXT_PART = 1000


async def _game_done_kb() -> InlineKeyboardMarkup:
    button_text = await get_setting_typed("game_proof_done_button")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text, callback_data="gs_done")]
    ])


async def _build_proof_prompt(task: dict) -> str:
    codes = parse_proof_types(task.get("proof_type"))
    if len(codes) == 1:
        body = await get_setting_typed(f"game_proof_prompt_{codes[0]}")
    else:
        body = await get_setting_typed("game_proof_prompt_any")
        if len(codes) > 1:
            for code in codes:
                body += f"\n• {await get_setting_typed(f'game_proof_prompt_{code}')}"
    hint = await get_setting_typed("game_proof_done_hint")
    return body + "\n\n" + hint


def _classify_part(message: types.Message) -> tuple[str | None, str | None, str | None]:
    """(kind, content, caption) for one incoming message, or (None, None, None) for anything
    the free-form flow doesn't recognize (voice/video/sticker/etc -- a soft refusal, state
    stays put)."""
    if message.photo:
        return "photo", message.photo[-1].file_id, getattr(message, "caption", None)
    if message.document:
        return "document", message.document.file_id, getattr(message, "caption", None)
    if message.text:
        text = message.text
        if text.strip().lower().startswith(("http://", "https://")):
            return "link", text, None
        return "text", text, None
    return None, None, None


async def _ack_album(media_group_id: str, bot: Bot, chat_id: int, state: FSMContext):
    await asyncio.sleep(0.8)  # collection window ONLY -- the finalize gate is still «✅ Готово»
    _gs_pending_albums.pop(media_group_id, None)
    data = await state.get_data()
    parts = data.get("gs_parts", [])
    try:
        await bot.send_message(
            chat_id, f"Принял, частей: {len(parts)}", reply_markup=await _game_done_kb(),
        )
    except Exception:
        pass


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

    # WR-06 (09.1-REVIEW.md): show_game_tasks filters the LIST by city, but this handler used
    # to resolve the task purely from callback_data with no city check -- same class as CR-03,
    # one layer down. cities_module_on() first so an off module stays byte-identical to
    # pre-09.1; task.get("event_city") second so an "all cities" task never triggers a check
    # at all. normalize_city on BOTH sides -- a delegate without a city reads as the default
    # city, same rule show_game_tasks already uses.
    if await cities_module_on() and task.get("event_city"):
        user = await get_user(callback.from_user.id)
        if normalize_city(user.get("event_city") if user else None) != normalize_city(task["event_city"]):
            await callback.answer("Это задание для другого города", show_alert=True)
            return

    # A-05 (созвон 13.08): дедлайн мягкий -- НЕ блокирует сдачу. Единственный оставшийся
    # серверный гвард на этом пути -- дубль-сдача (T-09-09), проверяется ниже.
    active = await get_active_submission(task_id, callback.from_user.id)
    if active is not None:
        await callback.answer("Уже отправлено, ожидай проверки", show_alert=True)
        return

    # T-14-01 (GAME-08, Pitfall 3): a delegate may already have the OLD task-list message
    # open with the old "Сдать" button when the manager archives the task -- Telegram never
    # retroactively disables an already-sent inline keyboard. The gate belongs HERE, not only
    # in the list-render filter (list_active_tasks already excludes archived tasks from the
    # CURRENT render, but that does nothing for a stale message already on the delegate's
    # screen).
    if task.get("archived_at"):
        await callback.answer(
            "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Мои "
            "задания», там актуальный список.",
            show_alert=True,
        )
        return

    # T-14-03 (GAME-10): resubmit limit, counted server-side on every entry into this
    # handler (not cached in FSM/keyboard state) -- a limit=0/None both mean "no limit",
    # byte-identical to pre-phase behavior (regression guard).
    limit = await get_setting_typed("game_resubmit_limit")
    if limit:
        rejected_count = await count_rejected_submissions(task_id, callback.from_user.id)
        if rejected_count >= limit:
            await callback.answer(
                f"Лимит попыток по этому заданию исчерпан ({limit}). Если считаешь, что "
                "это ошибка — напиши менеджеру через «❓ Задать вопрос».",
                show_alert=True,
            )
            return

    await state.update_data(gs_task_id=task_id, gs_parts=[])

    prompt = await _build_proof_prompt(task)
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
    # T-091 (CONTEXT.md A): «✅ Готово» must be available from the very first message, or the
    # empty-submission hint can never be reached before anything is sent.
    await callback.message.answer(
        await get_setting_typed("game_proof_done_hint"), reply_markup=await _game_done_kb(),
    )
    await state.set_state(GameSubmit.proof)
    await callback.answer()


@router.message(GameSubmit.proof, F.text.in_({"Отмена"}))
async def cancel_game_submit(message: types.Message, state: FSMContext):
    await state.update_data(gs_parts=[])
    await state.set_state(None)
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(GameSubmit.proof)
async def receive_proof(message: types.Message, bot: Bot, state: FSMContext):
    kind, content, caption = _classify_part(message)
    if kind is None:
        await message.answer("Не понял, пришли фото, документ, текст или ссылку.")
        return  # остаёмся в GameSubmit.proof, часть НЕ добавлена

    data = await state.get_data()
    parts = list(data.get("gs_parts", []))

    if len(parts) >= MAX_PARTS:
        # CR-01: part NOT added, state NOT reset -- delegate stays in GameSubmit.proof and can
        # still press «✅ Готово». Dedup within one album: an album can carry up to 10 parts
        # that would each hit this branch and produce 10 identical hints.
        mgid = message.media_group_id
        if mgid and mgid == data.get("gs_overflow_mgid"):
            return
        await message.answer(
            f"Больше {MAX_PARTS} частей в одну сдачу не влезет — нажми «✅ Готово», "
            "менеджер уже увидит присланное.",
            reply_markup=await _game_done_kb(),
        )
        if mgid:
            await state.update_data(gs_overflow_mgid=mgid)
        return

    if kind in ("text", "link") and content and len(content) > MAX_TEXT_PART:
        content = content[:MAX_TEXT_PART]

    parts.append({"kind": kind, "content": content, "caption": caption})
    await state.update_data(gs_parts=parts)

    mgid = message.media_group_id
    if mgid:
        if mgid not in _gs_pending_albums:
            _gs_pending_albums[mgid] = True
            _spawn(_ack_album(mgid, bot, message.from_user.id, state))
        return  # ack приходит одним сообщением после сборки альбома, не на каждое фото

    await message.answer(f"Принял, частей: {len(parts)}", reply_markup=await _game_done_kb())


@router.callback_query(F.data == "gs_done", GameSubmit.proof)
async def finalize_game_submission(callback: types.CallbackQuery, bot: Bot, state: FSMContext):
    """T-091-01: task_id comes ONLY from state (gs_task_id), never from callback_data --
    can't be tampered with to finalize under a different task."""
    data = await state.get_data()
    task_id = data.get("gs_task_id")
    parts = data.get("gs_parts", [])

    if not parts:
        # CONTEXT.md A: the ONE server-side validation -- state is NOT reset, delegate can
        # keep sending parts.
        await callback.answer(await get_setting_typed("game_proof_empty_hint"), show_alert=True)
        return

    task = await get_task(task_id)
    if task is None:
        # Задание исчезло, пока делегат собирал сдачу -- выходим из состояния, не молчим.
        await state.set_state(None)
        await callback.answer()
        await callback.message.answer("Это задание больше не доступно.", reply_markup=ReplyKeyboardRemove())
        return

    first = parts[0]
    submission_id = await create_submission(
        task_id, callback.from_user.id,
        content_type=_LEGACY_CONTENT_TYPE.get(first["kind"], "text"),
        content=first.get("content") or "",
        submitted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if submission_id is None:
        # T-09-01/D-05: гонка -- параллельная сдача той же пары успела раньше. Партиционный
        # индекс отклонил вставку. Без уведомления менеджеров, без технической ошибки делегату.
        await state.set_state(None)
        await callback.answer()
        await callback.message.answer(
            "Уже отправлено — кто-то опередил на долю секунды. Обнови список заданий.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    for i, part in enumerate(parts):
        await add_submission_part(submission_id, i, part["kind"], part.get("content"), part.get("caption"))

    await state.set_state(None)
    await callback.answer()
    await callback.message.answer(
        await get_setting_typed("game_submit_accepted_text"), reply_markup=ReplyKeyboardRemove(),
    )

    submitter_name = callback.from_user.full_name or str(callback.from_user.id)
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


# Phase 09.2 (B): shared делегат-city resolve for the four info/contacts screens below --
# same idiom as show_game_tasks (09.1 B), pulled into one helper because now FOUR screens
# read it instead of one. Module off, or any resolve failure (bad row, exception), fails
# soft to None -- a screen must never break because a city couldn't be resolved.
async def _delegate_city(telegram_id: int) -> str | None:
    try:
        if not await cities_module_on():
            return None
        user = await get_user(telegram_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception as e:
        logger.error(f"_delegate_city failed for {telegram_id}: {e}")
        return None


#ℹ️ Информация о форуме
@router.message(F.text == "ℹ️ Информация о форуме")
async def show_info_menu(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Info menu")

    code = await _delegate_city(message.from_user.id)
    event_date = await get_setting_for_city("event_date", code)
    event_time = await get_setting_for_city("event_time", code)
    place_name = await get_setting_for_city("event_place_name", code)

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
    code = await _delegate_city(callback.from_user.id)
    event_date = await get_setting_for_city("event_date", code)
    event_time = await get_setting_for_city("event_time", code)
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
    code = await _delegate_city(callback.from_user.id)
    place_name = await get_setting_for_city("event_place_name", code)
    place_address = await get_setting_for_city("event_place_address", code)
    if place_name:
        text = f"<b>Наша площадка — {html.escape(place_name)}!</b> 🚀"
        if place_address:
            text += f"\n\n📍 <b>Адрес:</b> {html.escape(place_address)}"

        # Phase 09.2: venue_photo_file_id is out of the per-city mechanism (RESEARCH
        # Pitfall 1 — photo/file fields are never independently editable registry text,
        # same reasoning that keeps program_photo_file_id/speakers_photo_file_id global).
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


# Phase 09.2: подписи program_caption/speakers_caption не ключи SETTINGS_SCHEMA (пишутся
# как побочный эффект загрузки фото) — их пер-городной вариант отложен, см.
# 09.2-RESEARCH Pitfall 1.
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

    code = await _delegate_city(message.from_user.id)
    contact_person = await get_setting_for_city("contact_person", code)
    contact_vk = await get_setting_for_city("contact_vk", code)
    contact_tg = await get_setting_for_city("contact_tg", code)

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

    # Phase 09.2 (D, CITY-06): resolve the delegate's city ONCE, same idiom as
    # show_game_tasks (cities_module_on -> get_user -> normalize_city). Fail-soft: a resolve
    # error must never eat the question, so any exception here falls back to city=None
    # (today's global fan-out), same shape as the fail-soft gates in cmd_start.
    city = None
    if await cities_module_on():
        try:
            _q_user = await get_user(message.from_user.id)
            city = normalize_city(_q_user.get("event_city") if _q_user else None)
        except Exception as e:
            logger.error(f"Failed to resolve city for question from {message.from_user.id}: {e}")
            city = None

    admin_text = (
        f"❓ <b>Новый вопрос от {user_info}:</b>\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"🧾 Вопрос #<code>{question_id}</code>\n\n"
        f"{html.escape(question_text)}\n\n"
        f"<i>↩️ Ответьте reply'ем на это сообщение, чтобы отправить ответ.</i>"
    )

    # D-13: fan out to every current moderate_reg holder (falls back to config.ADMIN_IDS if
    # nobody holds it -- T-08-31, never silently dropped). Phase 09.2 (D): city narrows the
    # fan-out to the delegate's city (None = today's global fan-out); the "filter emptied the
    # list" fallback lives inside notify_by_capability/capability_holders, not here.
    sent_count = await notify_by_capability(bot, "moderate_reg", admin_text, parse_mode="HTML", city=city)

    if sent_count > 0:
        await message.answer("Твой вопрос отправлен!", reply_markup=await get_main_menu_kb(message.from_user.id))
    elif config.ADMIN_IDS:
        logger.error(f"Failed to send question from {message.from_user.id} to any admin")
        await message.answer("Не удалось отправить вопрос, попробуйте позже.", reply_markup=await get_main_menu_kb(message.from_user.id))
    else:
        logger.warning("No admins configured to receive questions")
        await message.answer("Администраторы не настроены.", reply_markup=await get_main_menu_kb(message.from_user.id))


    await state.clear()
