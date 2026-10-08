"""Делегатский поток теста компетенций (в чате).

Своего `Router()` нет: хендлеры садятся на `handlers.user_actions.router`, модуль подключён
хвостовым импортом `user_actions.py` до фолбэка `reg_handoff_idle_fallback`. Входы: кнопка меню
(подпись настраивается — `DynamicMenuText("menu_quiz")`) и deep-link `/start quiz`
(`handlers/forum_deeplinks.py`).

Прогресс живёт ТОЛЬКО в БД (`quiz_attempts`), FSM не используется: после рестарта бота делегат
продолжает с первого неотвеченного вопроса. Допуск — тот же, что у записи на сессии: одобренный
делегат текущего сезона; перепроверяется на КАЖДЫЙ callback. Тексты интерфейса — из реестра
настроек (переводятся); контент теста (вопросы, варианты, компетенции, уровни) — как есть.

Callback'и (числа, <= 64 байт): qz:go (начать/продолжить), qz:a:{attempt}:{question}:{option},
qz:res (мой результат), qz:re (пройти заново)."""
import html
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext

from cities import normalize_city
from database import quiz_db
from database.db import get_user
from handlers import reg_i18n
from handlers.session_enroll import _Ctx, _btn, _kb, _show
from handlers.user_actions import _returning_text_if_past_season, ensure_registered, router
from keyboards.menu_dynamic import DynamicMenuText
from services import quiz as svc
from services import session_enroll as enroll_svc
from services.checkin import checkin_denial

logger = logging.getLogger(__name__)


async def _gate(event, telegram_id: int) -> tuple[_Ctx | None, dict | None, str | None]:
    """(контекст, тест, None) для допущенного делегата или (None, None, текст отказа)."""
    lang, tr_map = await reg_i18n.ctx_for(event)
    user = await get_user(telegram_id)
    city = normalize_city(user.get("event_city")) if user else None
    ctx = _Ctx(user, city, lang, tr_map)
    denial = await checkin_denial(user)
    if denial == "past_season":
        text = await _returning_text_if_past_season(telegram_id, user, lang, tr_map)
        return None, None, text or await ctx.t("quiz_not_approved_text")
    if denial:
        return None, None, await ctx.t("quiz_not_approved_text")
    quiz = await quiz_db.active_quiz_for_city(city) if city else None
    if quiz is None:
        return None, None, await ctx.t("quiz_disabled_text")
    return ctx, quiz, None


async def _question_screen(target: types.Message, ctx: _Ctx, attempt: dict, *,
                           head: str | None = None, edit: bool = True) -> bool:
    """Текущий вопрос попытки. False — отвечены все (показывать нечего)."""
    current = await svc.current_question(attempt)
    if current is None:
        return False
    question, options, n, total = current
    text = await ctx.t("quiz_question_header", n=n, total=total) + "\n\n" + html.escape(question["text"] or "")
    if head:
        text = f"{head}\n\n{text}"
    rows = [[_btn(o["text"], f"qz:a:{attempt['id']}:{question['id']}:{o['id']}")] for o in options]
    await _show(target, text, _kb(rows), edit=edit)
    return True


async def _result_screen(target: types.Message, ctx: _Ctx, quiz: dict, *, edit: bool) -> None:
    lines = await svc.result_lines(ctx.user["telegram_id"], quiz)
    if not lines:  # результата нет или нет оцениваемых компетенций — показываем заглушку
        await _show(target, await ctx.t("quiz_no_result_text"), None, edit=edit)
        return
    no_level = await ctx.t("quiz_no_level_label")
    parts = [await ctx.t("quiz_result_header")]
    for line in lines:
        row = await ctx.t("quiz_result_line", competency=html.escape(line["competency"] or ""),
                          level=html.escape(line["level_name"] or "") or no_level)
        if line["level_description"]:
            row += "\n" + html.escape(line["level_description"])
        parts.append(row)
    rows = []
    if await enroll_svc.module_enabled(ctx.city):
        rows.append([_btn(await ctx.t("quiz_choose_sessions_button"), "se:open")])
    if quiz.get("allow_retake"):
        rows.append([_btn(await ctx.t("quiz_retake_button"), "qz:re")])
    await _show(target, "\n\n".join(parts), _kb(rows) if rows else None, edit=edit)


async def _intro_screen(target: types.Message, ctx: _Ctx, quiz: dict, *, finished: bool,
                        edit: bool = False) -> None:
    text = "\n\n".join(html.escape(p) for p in (quiz.get("title"), quiz.get("intro")) if p) or \
        await ctx.t("quiz_start_button")
    rows = [[_btn(await ctx.t("quiz_start_button"), "qz:go")]]
    if finished:
        rows.append([_btn(await ctx.t("quiz_my_result_button"), "qz:res")])
    await _show(target, text, _kb(rows), edit=edit)


async def open_quiz(target: types.Message, telegram_id: int) -> bool:
    """Общий вход: гейт, затем вступление или (если тест уже пройден) результат."""
    ctx, quiz, refusal = await _gate(target, telegram_id)
    if ctx is None:
        await target.answer(refusal)
        return False
    open_attempt = await quiz_db.get_open_attempt(telegram_id, quiz["id"])
    finished = await quiz_db.get_last_finished_attempt(telegram_id, quiz["id"])
    if finished and not open_attempt:
        await _result_screen(target, ctx, quiz, edit=False)
        return True
    await _intro_screen(target, ctx, quiz, finished=bool(finished))
    return True


async def open_from_deeplink(message: types.Message, state: FSMContext) -> bool:
    """`/start quiz`: незарегистрированному — False (идёт обычный /start)."""
    if not await get_user(message.from_user.id):
        return False
    await state.clear()
    await open_quiz(message, message.from_user.id)
    return True


# ── Хендлеры ────────────────────────────────────────────────────────────────────────────────

@router.message(DynamicMenuText("menu_quiz"))
async def quiz_menu(message: types.Message):
    if not await ensure_registered(message):
        return
    await open_quiz(message, message.from_user.id)


async def _callback_gate(callback: types.CallbackQuery) -> tuple[_Ctx | None, dict | None]:
    ctx, quiz, refusal = await _gate(callback, callback.from_user.id)
    if ctx is None:
        await callback.answer(refusal, show_alert=True)
    return ctx, quiz


async def _show_attempt(callback, ctx, quiz, attempt: dict, status: str = "") -> None:
    """Вопрос попытки; все отвечены — результат."""
    head = None
    if status == "restarted":
        head = await ctx.t("quiz_restarted_text")
    elif status == "resumed":
        current = await svc.current_question(attempt)
        if current:
            head = await ctx.t("quiz_resume_text", n=current[2], total=current[3])
    if not await _question_screen(callback.message, ctx, attempt, head=head):
        await _result_screen(callback.message, ctx, quiz, edit=True)


@router.callback_query(F.data == "qz:go")
async def quiz_go(callback: types.CallbackQuery):
    ctx, quiz = await _callback_gate(callback)
    if ctx is None:
        return
    await callback.answer()
    attempt, status = await svc.start_or_resume(callback.from_user.id, quiz)
    if status == "finished":
        await _result_screen(callback.message, ctx, quiz, edit=True)
        return
    await _show_attempt(callback, ctx, quiz, attempt, status)


@router.callback_query(F.data.startswith("qz:a:"))
async def quiz_answer(callback: types.CallbackQuery):
    ctx, quiz = await _callback_gate(callback)
    if ctx is None:
        return
    parts = callback.data.split(":")
    try:
        if len(parts) != 5:
            raise ValueError
        attempt_id, question_id, option_id = (int(p) for p in parts[2:])
    except ValueError:
        await callback.answer()
        return
    uid = callback.from_user.id
    status = await svc.answer(uid, attempt_id, question_id, option_id)
    await callback.answer()
    if status == "done":
        await _result_screen(callback.message, ctx, quiz, edit=True)
    elif status in ("ok", "stale"):
        attempt = await quiz_db.get_attempt(attempt_id)
        await _show_attempt(callback, ctx, quiz, attempt)
    # not_owner / bad_option / finished: чужой или устаревший callback, экран не трогаем


@router.callback_query(F.data == "qz:res")
async def quiz_result(callback: types.CallbackQuery):
    ctx, quiz = await _callback_gate(callback)
    if ctx is None:
        return
    await callback.answer()
    await _result_screen(callback.message, ctx, quiz, edit=True)


@router.callback_query(F.data == "qz:re")
async def quiz_retake(callback: types.CallbackQuery):
    ctx, quiz = await _callback_gate(callback)
    if ctx is None:
        return
    await callback.answer()
    attempt = await svc.retake(callback.from_user.id, quiz)
    if attempt is None:
        await _result_screen(callback.message, ctx, quiz, edit=True)
        return
    await _show_attempt(callback, ctx, quiz, attempt)
