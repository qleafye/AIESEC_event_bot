"""Анкета из нескольких вопросов, сторона делегата: кнопки sv:* и ответ свободным текстом.

INVARIANT: каждый @router.* — декоратор в ОДНУ строку (golden-снапшот роутеров и сторож порядка
читают их построчно).

Прогресс делегата лежит в БД (`survey_progress`), а не в FSM: FSM бота — MemoryStorage и сбрасывается
при рестарте, а анкета на 10+ вопросов не должна теряться. Поэтому «ждём текст» — тоже флаг в БД
(`state='awaiting_text'`), а текстовый хендлер ловит сообщение только по нему и только без активного
FSM-состояния: команды, кнопки меню и регистрация не перехватываются.

Модуль импортируется ХВОСТОМ `handlers/user_actions.py` (после `menu_tap_escape`, до фолбэка
`reg_handoff_idle_fallback`): aiogram останавливается на первом совпавшем хендлере, и фолбэк
в самом конце иначе проглотил бы текст ответа. Импортировать его до `handlers.user_actions` нельзя
по той же причине — порядок хендлеров определяется порядком регистрации.

Вход по ссылке `?start=survey_<id>` — `open_from_deeplink` (зовёт handlers/forum/forum_deeplinks.py).
Черновик анкеты и незакрытый шаг диалога ссылке не мешают: человек сразу видит вопрос, не меню.

Callback'и делегата (CapabilityMiddleware их не режет — префикса `sv:` нет в ADMIN_CAPS):
sv:s:<poll> старт, sv:c:<poll> продолжить, sv:o:<q>:<i> вариант, sv:d:<q> «Готово»,
sv:k:<q> пропуск, sv:n:<q>:<1-5> шкала, sv:t:<q>:<0-5> строка таблицы.
"""
from __future__ import annotations

import html as html_module
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, StateFilter

from database.db import get_poll
from database.survey_db import (
    advance_survey,
    finish_survey,
    get_survey_progress,
    get_survey_question,
    get_survey_wait,
    list_survey_questions,
    set_survey_await,
    set_survey_draft,
    start_survey_progress,
    survey_recipient,
    upsert_survey_answer,
)
from handlers.user_actions import router
from services.comms import surveys

logger = logging.getLogger(__name__)

# Черновик и запланированная анкета тоже открываются по ссылке — рассылка для этого не нужна.
# Закрытая анкета ответы не принимает.
_ANSWERABLE_STATUSES = ("draft", "scheduled", "open", "sending")


# ── вывод: правка текущего сообщения или новое ──────────────────────────────────────────────

def _edit_put(callback: types.CallbackQuery):
    async def put(text, kb):
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    return put


def _new_put(message):
    async def put(text, kb):
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    return put


# ── показ вопроса ────────────────────────────────────────────────────────────────────────────

async def _render(poll: dict, progress: dict, q: dict):
    qs = await list_survey_questions(poll["id"])
    wait = progress.get("await") or {}
    sub = wait.get("sub") or ""
    if sub.startswith("m") and sub[1:].isdigit() and int(sub[1:]) < len(q.get("options") or []):
        return surveys.render_more_prompt(q, int(sub[1:]))
    return surveys.render_question(
        q, number=surveys.question_number(qs, q["id"]), total=len(qs), draft=progress.get("draft"),
    )


async def _present(put, poll: dict, uid: int, q: dict) -> None:
    """Показать вопрос q (он уже текущий в прогрессе). Для свободного ответа ставим флаг ожидания."""
    if q["qtype"] == "text":
        await set_survey_await(poll["id"], uid, q["id"], {"qid": q["id"], "sub": "", "queue": []})
    progress = await get_survey_progress(poll["id"], uid)
    if progress is None:
        return
    text, kb = await _render(poll, progress, q)
    await put(text, kb)


async def open_from_deeplink(message, state, poll_id: int) -> bool:
    """`/start survey_<id>`: сразу этот опрос. True даже при ошибке — не главное меню.

    Сбрасывает шаг диалога (черновик регистрации, мастер админки): иначе ответ текстом
    съедает активное FSM-состояние. Статус черновика ссылке не мешает и рассылку не включает.
    """
    try:
        await state.clear()
    except Exception:
        logger.exception("survey deeplink: не удалось сбросить шаг диалога")
    try:
        await _enter_from_link(message, int(poll_id))
    except Exception:
        logger.exception("survey deeplink %s", poll_id)
        await message.answer(
            "Не получилось открыть опрос. Нажмите ссылку ещё раз. "
            "Если снова не выйдет — напишите менеджеру."
        )
    return True


async def _enter_from_link(message, poll_id: int) -> None:
    uid = message.from_user.id
    poll = await get_poll(poll_id)
    if poll is None or poll.get("kind") != "survey":
        await message.answer(
            "Эта ссылка больше не ведёт на опрос. Попросите менеджера прислать новую."
        )
        return
    if poll.get("status") not in _ANSWERABLE_STATUSES:
        await message.answer(
            "Этот опрос уже закрыт — ответить по ссылке нельзя. "
            "Напишите менеджеру, если его нужно открыть снова."
        )
        return
    questions = await list_survey_questions(poll["id"])
    if not questions:
        await message.answer(
            "В этом опросе пока нет вопросов. Напишите менеджеру — "
            "ответить можно будет, когда вопросы добавят."
        )
        return
    progress = await get_survey_progress(poll["id"], uid)
    if progress is not None and progress.get("state") == "done":
        outro = (poll.get("outro_text") or "").strip() or surveys.DEFAULT_OUTRO
        await message.answer(html_module.escape(outro), parse_mode="HTML")
        return
    if progress is None:
        progress = await start_survey_progress(poll["id"], uid, questions[0]["id"])
    question = await get_survey_question(progress["current_qid"]) if progress.get("current_qid") else None
    if question is None:
        await message.answer(
            "Не получилось открыть вопрос. Нажмите ссылку ещё раз или напишите менеджеру."
        )
        return
    await _present(_new_put(message), poll, uid, question)


async def _ask_more(put, poll: dict, uid: int, q: dict, queue: list[int]) -> None:
    """Уточнение по варианту queue[0]; остальные ждут своей очереди."""
    first, rest = queue[0], queue[1:]
    await set_survey_await(poll["id"], uid, q["id"], {"qid": q["id"], "sub": f"m{first}", "queue": rest})
    text, kb = surveys.render_more_prompt(q, first)
    await put(text, kb)


async def _advance(put, poll: dict, uid: int, q: dict) -> None:
    """Вопрос закончен: следующий по порядку либо финальный текст."""
    qs = await list_survey_questions(poll["id"])
    nid = surveys.next_question_id(qs, q["id"])
    if nid is None:
        if await finish_survey(poll["id"], uid, q["id"]):
            outro = (poll.get("outro_text") or "").strip() or surveys.DEFAULT_OUTRO
            await put(html_module.escape(outro), None)
        return
    if not await advance_survey(poll["id"], uid, q["id"], nid):
        return  # двойной тап: переход уже сделан другим нажатием
    nxt = await get_survey_question(nid)
    if nxt is not None:
        await _present(put, poll, uid, nxt)


async def _after_answer(put, poll: dict, uid: int, q: dict, queue: list[int]) -> None:
    if queue:
        await _ask_more(put, poll, uid, q, queue)
    else:
        await _advance(put, poll, uid, q)


# ── разбор callback и общие проверки ─────────────────────────────────────────────────────────

def _ints(data: str, n: int) -> list[int] | None:
    parts = (data or "").split(":")[2:]
    if len(parts) != n:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        return None


async def _bad(callback: types.CallbackQuery, text: str) -> None:
    await callback.answer(text, show_alert=True)


async def _load(callback: types.CallbackQuery, *, poll_id: int | None = None, qid: int | None = None,
                allow_missing: bool = False, allow_more: bool = False):
    """(poll, progress, question, uid) либо None, когда делегату уже ответили алертом/перерисовкой.

    Порядок проверок: анкета существует -> закрыта -> уже пройдена -> вопрос из этой анкеты ->
    вопрос текущий. user_id берётся только из callback.from_user (T-39-07)."""
    uid = callback.from_user.id
    q = None
    if qid is not None:
        q = await get_survey_question(qid)
        if q is None:
            await _bad(callback, "Этого вопроса уже нет.")
            return None
        poll_id = q["poll_id"]
    poll = await get_poll(poll_id) if poll_id is not None else None
    if poll is None or poll.get("kind") != "survey":
        await _bad(callback, "Этот опрос не найден.")
        return None
    if poll.get("status") not in _ANSWERABLE_STATUSES:
        await _bad(callback, "Опрос закрыт")
        return None
    progress = await get_survey_progress(poll["id"], uid)
    if progress is None:
        if allow_missing:
            return poll, None, None, uid
        await _bad(callback, "Начни опрос кнопкой «Пройти опрос» в приглашении.")
        return None
    if progress.get("state") == "done":
        await _bad(callback, "Ты уже ответил(а), спасибо")
        return None
    current = await get_survey_question(progress["current_qid"]) if progress.get("current_qid") else None
    if current is None:
        await _bad(callback, "Не получилось найти текущий вопрос. Напиши менеджеру.")
        return None
    if q is not None and q["id"] != progress["current_qid"]:
        await callback.answer()
        text, kb = await _render(poll, progress, current)
        await _edit_put(callback)(text, kb)
        return None
    sub = (progress.get("await") or {}).get("sub") or ""
    if not allow_more and sub.startswith("m"):
        await callback.answer()
        text, kb = await _render(poll, progress, current)
        await _edit_put(callback)(text, kb)
        return None
    return poll, progress, current, uid


# ── хендлеры sv:* ────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sv:s:"))
async def sv_start(callback: types.CallbackQuery):
    nums = _ints(callback.data, 1)
    if nums is None:
        await callback.answer()
        return
    ctx = await _load(callback, poll_id=nums[0], allow_missing=True)
    if ctx is None:
        return
    poll, progress, _, uid = ctx
    if not await survey_recipient(poll["id"], uid):
        await _bad(callback, "Этот опрос отправлен не тебе")
        return
    qs = await list_survey_questions(poll["id"])
    if not qs:
        await _bad(callback, "В этом опросе пока нет вопросов.")
        return
    if progress is None:
        progress = await start_survey_progress(poll["id"], uid, qs[0]["id"])
    q = await get_survey_question(progress["current_qid"])
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=surveys.invite_kb(poll["id"], started=True))
    except Exception as e:
        logger.info("sv_start: invite button edit failed for %s: %s", uid, e)
    if q is not None:
        await _present(_new_put(callback.message), poll, uid, q)


@router.callback_query(F.data.startswith("sv:c:"))
async def sv_continue(callback: types.CallbackQuery):
    nums = _ints(callback.data, 1)
    if nums is None:
        await callback.answer()
        return
    ctx = await _load(callback, poll_id=nums[0], allow_more=True)
    if ctx is None:
        return
    poll, progress, q, uid = ctx
    await callback.answer()
    text, kb = await _render(poll, progress, q)
    await _new_put(callback.message)(text, kb)


@router.callback_query(F.data.startswith("sv:o:"))
async def sv_option(callback: types.CallbackQuery):
    nums = _ints(callback.data, 2)
    if nums is None:
        await callback.answer()
        return
    qid, idx = nums
    ctx = await _load(callback, qid=qid)
    if ctx is None:
        return
    poll, progress, q, uid = ctx
    options = q.get("options") or []
    if q["qtype"] not in ("single", "multi") or not 0 <= idx < len(options):
        await _bad(callback, "Такого варианта нет.")
        return
    put = _edit_put(callback)
    if q["qtype"] == "single":
        await callback.answer()
        await upsert_survey_answer(poll["id"], uid, q["id"], "", idx)
        await _after_answer(put, poll, uid, q, surveys.more_queue(q, [idx]))
        return
    sel = list((progress.get("draft") or {}).get("sel") or [])
    sel, err = surveys.toggle_multi(sel, idx, q.get("max_choices"))
    if err:
        await _bad(callback, err)
        return
    draft = {"sel": sel}
    await set_survey_draft(poll["id"], uid, q["id"], draft)
    await callback.answer()
    qs = await list_survey_questions(poll["id"])
    text, kb = surveys.render_question(
        q, number=surveys.question_number(qs, q["id"]), total=len(qs), draft=draft,
    )
    await put(text, kb)


@router.callback_query(F.data.startswith("sv:d:"))
async def sv_done(callback: types.CallbackQuery):
    nums = _ints(callback.data, 1)
    if nums is None:
        await callback.answer()
        return
    ctx = await _load(callback, qid=nums[0])
    if ctx is None:
        return
    poll, progress, q, uid = ctx
    if q["qtype"] != "multi":
        await _bad(callback, "Для этого вопроса кнопка «Готово» не нужна.")
        return
    options = q.get("options") or []
    sel = sorted(i for i in (progress.get("draft") or {}).get("sel") or [] if 0 <= i < len(options))
    if not sel and q.get("required"):
        await _bad(callback, "Выбери хотя бы один вариант")
        return
    await callback.answer()
    put = _edit_put(callback)
    if not sel:
        await _advance(put, poll, uid, q)
        return
    await upsert_survey_answer(poll["id"], uid, q["id"], "", sel)
    await _after_answer(put, poll, uid, q, surveys.more_queue(q, sel))


@router.callback_query(F.data.startswith("sv:k:"))
async def sv_skip(callback: types.CallbackQuery):
    nums = _ints(callback.data, 1)
    if nums is None:
        await callback.answer()
        return
    ctx = await _load(callback, qid=nums[0], allow_more=True)
    if ctx is None:
        return
    poll, progress, q, uid = ctx
    wait = progress.get("await") or {}
    sub = wait.get("sub") or ""
    put = _edit_put(callback)
    if sub.startswith("m"):
        # «Пропустить» на уточнении: сам выбор уже записан, идём к следующему уточнению.
        await callback.answer()
        await _after_answer(put, poll, uid, q, list(wait.get("queue") or []))
        return
    if q.get("required"):
        await _bad(callback, "Этот вопрос обязательный")
        return
    await callback.answer()
    await _advance(put, poll, uid, q)


@router.callback_query(F.data.startswith("sv:n:"))
async def sv_scale(callback: types.CallbackQuery):
    nums = _ints(callback.data, 2)
    if nums is None:
        await callback.answer()
        return
    qid, value = nums
    ctx = await _load(callback, qid=qid)
    if ctx is None:
        return
    poll, _, q, uid = ctx
    if q["qtype"] != "scale" or not 1 <= value <= 5:
        await _bad(callback, "Выбери оценку от 1 до 5.")
        return
    await callback.answer()
    await upsert_survey_answer(poll["id"], uid, q["id"], "", value)
    await _advance(_edit_put(callback), poll, uid, q)


@router.callback_query(F.data.startswith("sv:t:"))
async def sv_table(callback: types.CallbackQuery):
    nums = _ints(callback.data, 2)
    if nums is None:
        await callback.answer()
        return
    qid, value = nums
    ctx = await _load(callback, qid=qid)
    if ctx is None:
        return
    poll, progress, q, uid = ctx
    rows = q.get("rows") or []
    if q["qtype"] != "table" or not 0 <= value <= 5 or not rows:
        await _bad(callback, "Выбери оценку от 1 до 5.")
        return
    row = int((progress.get("draft") or {}).get("row") or 0)
    row = max(0, min(row, len(rows) - 1))
    await callback.answer()
    await upsert_survey_answer(poll["id"], uid, q["id"], f"r{row}", value)
    put = _edit_put(callback)
    if row + 1 < len(rows):
        draft = {"row": row + 1}
        await set_survey_draft(poll["id"], uid, q["id"], draft)
        qs = await list_survey_questions(poll["id"])
        text, kb = surveys.render_question(
            q, number=surveys.question_number(qs, q["id"]), total=len(qs), draft=draft,
        )
        await put(text, kb)
        return
    await _advance(put, poll, uid, q)


# ── свободный текст по флагу в БД ────────────────────────────────────────────────────────────

class _AwaitingSurveyText(BaseFilter):
    """Пропускает сообщение только если делегат сейчас в статусе «жду текст» по живой анкете.
    Быстрый отказ до запроса в БД: не личка, команда (T-39-10)."""

    def __init__(self, text_only: bool = True):
        self.text_only = text_only

    async def __call__(self, message: types.Message):
        chat = getattr(message, "chat", None)
        if chat is not None and getattr(chat, "type", "private") != "private":
            return False
        user = getattr(message, "from_user", None)
        if user is None:
            return False
        text = getattr(message, "text", None)
        if text and text.startswith("/"):
            return False
        if self.text_only and not text:
            return False
        wait = await get_survey_wait(user.id)
        if wait is None:
            return False
        return {"survey_wait": wait}


@router.message(StateFilter(None), F.text, _AwaitingSurveyText(text_only=True))
async def survey_text_step(message: types.Message, survey_wait: dict):
    uid = message.from_user.id
    error = surveys.validate_answer_text(message.text)
    if error:
        await message.answer(error)
        return
    poll = await get_poll(survey_wait["poll_id"])
    q = await get_survey_question(survey_wait["current_qid"]) if survey_wait.get("current_qid") else None
    wait = survey_wait.get("await") or {}
    if poll is None or q is None or wait.get("qid") not in (None, q["id"]):
        return
    sub = wait.get("sub") or ""
    await upsert_survey_answer(poll["id"], uid, q["id"], sub, message.text.strip())
    await message.answer("Записал")
    put = _new_put(message)
    queue = list(wait.get("queue") or []) if sub.startswith("m") else []
    await _after_answer(put, poll, uid, q, queue)


@router.message(StateFilter(None), ~F.text, _AwaitingSurveyText(text_only=False))
async def survey_non_text_step(message: types.Message, survey_wait: dict):
    await message.answer("Пришли ответ текстом одним сообщением")
