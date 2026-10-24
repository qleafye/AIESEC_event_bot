"""Конструктор анкеты из нескольких вопросов (раздел «📊 Опросы» → «➕ Новый опрос» → «📋 Анкета»).

Черновик живёт в БД (polls.status='draft', kind='survey'), FSM хранит только poll_id и текущий шаг
ввода: после рестарта бота черновик не пропадает, «Отмена» его не удаляет. Здесь: создание, экран
черновика, правка текстов и вопросов, перестановка, удаление, превью, аудитория, отправка и
планирование. Добавление нового вопроса — в admin_survey_question.py (callback psv_qadd:<pid>).

Право — `broadcast` ("psv_*", "state:SurveyBuild:*" в handlers/access/admin_caps.py).

INVARIANT (cap-test): каждый декоратор `@router.*` — в ОДНУ строку.
"""
import html as html_module
import logging

from aiogram import F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.db import get_poll, count_and_list_filtered
from database.survey_db import (
    create_survey_draft,
    update_survey_texts,
    list_survey_questions,
    get_survey_question,
    update_survey_question_text,
    delete_survey_question,
    move_survey_question,
    publish_survey_draft,
)
from services.comms.polls import audience_label
from services.comms.surveys import (
    QTYPE_LABELS,
    SURVEY_MAX_QUESTIONS,
    DEFAULT_OUTRO,
    QUESTION_MAX,
    validate_question_text,
    render_survey_preview,
)
from services.scheduler import (
    _parse_schedule_dt,
    _fmt_dt,
    _now_moscow_naive,
    schedule_poll_job,
)
from services.infra.background import spawn as _spawn
from keyboards.builders import get_cancel_kb
from handlers.states import SurveyBuild
from handlers.settings.admin_core import _admin_city_view
from handlers.comms.admin_polls import _poll_out_of_scope, _parse_id, _short
from handlers.comms.admin_poll_wizard import (
    _audience_kb,
    build_audience_spec,
    _deliver_and_report,
    _quiet_hours_warning,
)
from handlers.admin import router

logger = logging.getLogger(__name__)

TITLE_MAX = 300
TEXT_MAX = 1000
_DRAFT_KEPT = "Черновик сохранён — найдёте его в «📊 Опросы»."
_ALREADY_SENT = "Анкета уже отправлена — правка недоступна."


def _esc(value) -> str:
    return html_module.escape(str(value or ""))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _back_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ К черновику", f"psv_draft:{pid}")]])


# ── экран черновика ─────────────────────────────────────────────────────────────────────────

async def render_survey_draft(poll_id: int):
    """(текст, клавиатура) экрана черновика или None, если анкеты нет."""
    poll = await get_poll(poll_id)
    if poll is None:
        return None
    questions = await list_survey_questions(poll_id)
    n = len(questions)
    intro = "задано" if (poll.get("intro_text") or "").strip() else "не задано"
    outro = "задан" if (poll.get("outro_text") or "").strip() else f"по умолчанию («{_esc(DEFAULT_OUTRO)}»)"
    text = (
        f"📋 <b>{_esc(poll['question'])}</b> — черновик\n\n"
        f"Вопросов: {n} из {SURVEY_MAX_QUESTIONS}\n"
        f"Приглашение: {intro}\n"
        f"Финальный текст: {outro}\n\n"
        "Нажмите на вопрос, чтобы изменить его, удалить или переставить. "
        "Когда всё готово — «➡️ Аудитория и отправка»."
    )
    rows = []
    for k, q in enumerate(questions, start=1):
        label = f"{k}. [{QTYPE_LABELS.get(q['qtype'], q['qtype'])}] {_short(q['text'], 30)}"
        rows.append([_btn(label, f"psv_q:{q['id']}")])
    if n < SURVEY_MAX_QUESTIONS:
        rows.append([_btn("➕ Добавить вопрос", f"psv_qadd:{poll_id}")])
    rows.append([_btn("📝 Приглашение", f"psv_intro:{poll_id}"),
                 _btn("🏁 Финальный текст", f"psv_outro:{poll_id}")])
    rows.append([_btn("✏️ Название", f"psv_title:{poll_id}"), _btn("👁 Превью", f"psv_prev:{poll_id}")])
    rows.append([_btn("➡️ Аудитория и отправка", f"psv_go:{poll_id}")])
    rows.append([_btn("⬅️ К списку опросов", "admin_polls")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(callback: types.CallbackQuery, text: str, kb=None) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _show_draft_cb(callback: types.CallbackQuery, pid: int) -> None:
    screen = await render_survey_draft(pid)
    if screen is None:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return
    await _edit(callback, *screen)


async def _show_draft_msg(message: types.Message, pid: int) -> None:
    screen = await render_survey_draft(pid)
    if screen is not None:
        await message.answer(screen[0], parse_mode="HTML", reply_markup=screen[1])


async def _load_draft(admin_id: int, pid: int | None):
    """(poll, ошибка). Анкета должна быть черновиком и не чужого города."""
    poll = await get_poll(pid) if pid is not None else None
    if poll is None or poll.get("kind") != "survey":
        return None, "Анкета не найдена — откройте «📊 Опросы»."
    if await _poll_out_of_scope(admin_id, poll):
        return None, "Эта анкета адресована другому городу — переключите город в шапке."
    if poll.get("status") != "draft":
        return None, _ALREADY_SENT
    return poll, None


async def _guard_cb(callback: types.CallbackQuery, pid: int | None):
    poll, err = await _load_draft(callback.from_user.id, pid)
    if err:
        await callback.answer(err, show_alert=True)
    return poll


async def _guard_msg(message: types.Message, state: FSMContext, pid: int | None):
    poll, err = await _load_draft(message.from_user.id, pid)
    if err:
        await state.clear()
        await message.answer(err, reply_markup=ReplyKeyboardRemove())
    return poll


async def _load_question(callback: types.CallbackQuery):
    """(вопрос, анкета) или (None, None) с алертом."""
    q = await get_survey_question(_parse_id(callback.data) or 0)
    if q is None:
        await callback.answer("Вопрос не найден — он уже удалён.", show_alert=True)
        return None, None
    poll = await _guard_cb(callback, q["poll_id"])
    return (q, poll) if poll else (None, None)


# ── отмена (до текстовых шагов) ─────────────────────────────────────────────────────────────

@router.message(StateFilter(SurveyBuild), Command("cancel"))
@router.message(StateFilter(SurveyBuild), F.text == "Отмена")
async def survey_cancel(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    text = _DRAFT_KEPT if data.get("pid") else "Создание анкеты отменено."
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


# ── создание ────────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "psv_new")
async def survey_new(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SurveyBuild.title)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"📋 <b>Новая анкета</b>\n\nНапишите название (до {TITLE_MAX} символов) — его видите только вы.\n"
        "Например: <i>Итоги первого дня Юлида</i>",
        parse_mode="HTML", reply_markup=get_cancel_kb(),
    )


@router.message(SurveyBuild.title)
async def survey_title_step(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text or len(text) > TITLE_MAX:
        await message.answer(
            f"Нужно название до {TITLE_MAX} символов (сейчас {len(text)}). "
            "Например: Итоги первого дня Юлида."
        )
        return
    scope, _ = await _admin_city_view(message.from_user.id)
    city = scope[0] if scope else None
    pid = await create_survey_draft(text, message.from_user.id, city)
    await state.clear()
    await state.update_data(pid=pid)
    await message.answer("Черновик создан.", reply_markup=ReplyKeyboardRemove())
    await _show_draft_msg(message, pid)


@router.callback_query(F.data.startswith("psv_draft:"))
async def survey_draft(callback: types.CallbackQuery, state: FSMContext):
    pid = _parse_id(callback.data)
    if not await _guard_cb(callback, pid):
        return
    await state.clear()
    await state.update_data(pid=pid)
    await callback.answer()
    await _show_draft_cb(callback, pid)


# ── тексты анкеты ───────────────────────────────────────────────────────────────────────────

_TEXT_STEPS = {
    "title": (
        SurveyBuild.edit_title, "✏️ Новое название",
        f"Пришлите название до {TITLE_MAX} символов.\nНапример: <i>Итоги первого дня Юлида</i>", TITLE_MAX),
    "intro": (
        SurveyBuild.intro, "📝 Приглашение",
        "Пришлите текст, с которым делегат получит анкету.\n"
        "Например: <i>Привет! Расскажи, как прошёл первый день Юлида — 3 минуты</i>", TEXT_MAX),
    "outro": (
        SurveyBuild.outro, "🏁 Финальный текст",
        "Пришлите текст, который делегат увидит после последнего вопроса.\n"
        "Например: <i>Спасибо, что поделился мнением! 🧡</i>", TEXT_MAX),
}


async def _ask_text(callback: types.CallbackQuery, state: FSMContext, kind: str):
    pid = _parse_id(callback.data)
    if not await _guard_cb(callback, pid):
        return
    st, head, hint, limit = _TEXT_STEPS[kind]
    await state.clear()
    await state.set_state(st)
    await state.update_data(pid=pid)
    await callback.answer()
    await callback.message.answer(
        f"{head}\n\n{hint}\n(до {limit} символов)", parse_mode="HTML", reply_markup=get_cancel_kb(),
    )


@router.callback_query(F.data.startswith("psv_title:"))
async def survey_title_ask(callback: types.CallbackQuery, state: FSMContext):
    await _ask_text(callback, state, "title")


@router.callback_query(F.data.startswith("psv_intro:"))
async def survey_intro_ask(callback: types.CallbackQuery, state: FSMContext):
    await _ask_text(callback, state, "intro")


@router.callback_query(F.data.startswith("psv_outro:"))
async def survey_outro_ask(callback: types.CallbackQuery, state: FSMContext):
    await _ask_text(callback, state, "outro")


async def _save_text(message: types.Message, state: FSMContext, kind: str):
    limit = _TEXT_STEPS[kind][3]
    text = (message.text or "").strip()
    if not text or len(text) > limit:
        await message.answer(
            f"Нужен текст до {limit} символов (сейчас {len(text)}). Пришлите его сообщением ещё раз."
        )
        return
    data = await state.get_data()
    pid = data.get("pid")
    if not await _guard_msg(message, state, pid):
        return
    await update_survey_texts(pid, **{kind: text})
    await state.clear()
    await state.update_data(pid=pid)
    await message.answer("Сохранено ✅", reply_markup=ReplyKeyboardRemove())
    await _show_draft_msg(message, pid)


@router.message(SurveyBuild.edit_title)
async def survey_title_save(message: types.Message, state: FSMContext):
    await _save_text(message, state, "title")


@router.message(SurveyBuild.intro)
async def survey_intro_save(message: types.Message, state: FSMContext):
    await _save_text(message, state, "intro")


@router.message(SurveyBuild.outro)
async def survey_outro_save(message: types.Message, state: FSMContext):
    await _save_text(message, state, "outro")


# ── вопросы: карточка, текст, порядок, удаление ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("psv_q:"))
async def survey_question_card(callback: types.CallbackQuery):
    q, poll = await _load_question(callback)
    if q is None:
        return
    lines = [f"<b>[{QTYPE_LABELS.get(q['qtype'], q['qtype'])}]</b> {_esc(q['text'])}"]
    for opt in q.get("options") or []:
        lines.append(f"• {_esc(opt.get('t'))}")
    for row in q.get("rows") or []:
        lines.append(f"• {_esc(row)}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✏️ Текст", f"psv_qtxt:{q['id']}"), _btn("🗑 Удалить", f"psv_qdel:{q['id']}")],
        [_btn("⬆️ Выше", f"psv_qup:{q['id']}"), _btn("⬇️ Ниже", f"psv_qdn:{q['id']}")],
        [_btn("⬅️ К черновику", f"psv_draft:{poll['id']}")],
    ])
    await callback.answer()
    await _edit(callback, "\n".join(lines), kb)


@router.callback_query(F.data.startswith("psv_qtxt:"))
async def survey_question_text_ask(callback: types.CallbackQuery, state: FSMContext):
    q, poll = await _load_question(callback)
    if q is None:
        return
    await state.clear()
    await state.set_state(SurveyBuild.q_edit_text)
    await state.update_data(pid=poll["id"], qid=q["id"])
    await callback.answer()
    await callback.message.answer(
        f"✏️ Пришлите новый текст вопроса (до {QUESTION_MAX} символов).\n"
        "Например: Как тебе программа первого дня?",
        reply_markup=get_cancel_kb(),
    )


@router.message(SurveyBuild.q_edit_text)
async def survey_question_text_save(message: types.Message, state: FSMContext):
    err = validate_question_text(message.text)
    if err:
        await message.answer(err)
        return
    data = await state.get_data()
    pid = data.get("pid")
    if not await _guard_msg(message, state, pid):
        return
    q = await get_survey_question(data.get("qid") or 0)
    if q is None or q["poll_id"] != pid:
        await state.clear()
        await message.answer("Вопрос не найден — он уже удалён.", reply_markup=ReplyKeyboardRemove())
        return
    await update_survey_question_text(q["id"], message.text.strip())
    await state.clear()
    await state.update_data(pid=pid)
    await message.answer("Текст вопроса обновлён ✅", reply_markup=ReplyKeyboardRemove())
    await _show_draft_msg(message, pid)


async def _move(callback: types.CallbackQuery, direction: int):
    q, poll = await _load_question(callback)
    if q is None:
        return
    if not await move_survey_question(q["id"], direction):
        await callback.answer("Вопрос уже первый." if direction < 0 else "Вопрос уже последний.",
                              show_alert=True)
        return
    await callback.answer()
    await _show_draft_cb(callback, poll["id"])


@router.callback_query(F.data.startswith("psv_qup:"))
async def survey_question_up(callback: types.CallbackQuery):
    await _move(callback, -1)


@router.callback_query(F.data.startswith("psv_qdn:"))
async def survey_question_down(callback: types.CallbackQuery):
    await _move(callback, 1)


@router.callback_query(F.data.startswith("psv_qdel:"))
async def survey_question_delete_ask(callback: types.CallbackQuery):
    q, poll = await _load_question(callback)
    if q is None:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🗑 Да, удалить", f"psv_qdelgo:{q['id']}")],
        [_btn("⬅️ Нет, оставить", f"psv_q:{q['id']}")],
    ])
    await callback.answer()
    await _edit(
        callback,
        f"Удалить вопрос «{_esc(_short(q['text'], 60))}»? Пропадёт сам вопрос и его варианты.", kb,
    )


@router.callback_query(F.data.startswith("psv_qdelgo:"))
async def survey_question_delete_go(callback: types.CallbackQuery):
    q, poll = await _load_question(callback)
    if q is None:
        return
    await delete_survey_question(q["id"])
    await callback.answer("Вопрос удалён")
    await _show_draft_cb(callback, poll["id"])


# ── превью ──────────────────────────────────────────────────────────────────────────────────

async def _send_preview(callback: types.CallbackQuery, poll: dict, kb: InlineKeyboardMarkup) -> None:
    questions = await list_survey_questions(poll["id"])
    chunks = render_survey_preview(
        {"title": poll["question"], "intro_text": poll.get("intro_text"),
         "outro_text": poll.get("outro_text")},
        questions,
    )
    for chunk in chunks[:-1]:
        await callback.message.answer(chunk, parse_mode="HTML")
    await callback.message.answer(chunks[-1], parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("psv_prev:"))
async def survey_preview(callback: types.CallbackQuery):
    poll = await _guard_cb(callback, _parse_id(callback.data))
    if poll is None:
        return
    await callback.answer()
    await _send_preview(callback, poll, _back_kb(poll["id"]))


# ── аудитория и отправка ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("psv_go:"))
async def survey_go(callback: types.CallbackQuery, state: FSMContext):
    pid = _parse_id(callback.data)
    poll = await _guard_cb(callback, pid)
    if poll is None:
        return
    if not await list_survey_questions(pid):
        await callback.answer("Сначала добавьте хотя бы один вопрос — кнопка «➕ Добавить вопрос».",
                              show_alert=True)
        return
    if not (poll.get("intro_text") or "").strip():
        await callback.answer("Сначала задайте текст приглашения — кнопка «📝 Приглашение».",
                              show_alert=True)
        return
    await state.clear()
    await state.set_state(SurveyBuild.audience)
    await state.update_data(pid=pid)
    text, kb = await _audience_kb(callback.from_user.id)
    rows = [r for r in kb.inline_keyboard if all(b.callback_data != "poll_cancel" for b in r)]
    rows.append([_btn("⬅️ К черновику", f"psv_draft:{pid}")])
    await callback.answer()
    await _edit(callback, text.replace("опрос", "анкету"), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("poll_aud:"), SurveyBuild.audience)
async def survey_audience_pick(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pid = data.get("pid")
    poll = await _guard_cb(callback, pid)
    if poll is None:
        return
    choice = callback.data[len("poll_aud:"):]
    spec, city = await build_audience_spec(choice, callback.from_user.id)
    ids = await count_and_list_filtered(spec) if spec else None
    if ids is None:
        from database.db import get_all_users_ids
        ids = await get_all_users_ids()
    await state.update_data(audience=spec, city=city)
    await state.set_state(SurveyBuild.confirm)
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🚀 Отправить сейчас", f"psv_send:{pid}")],
        [_btn("🕒 Запланировать", f"psv_sched:{pid}")],
        [_btn("⬅️ К черновику", f"psv_draft:{pid}")],
    ])
    questions = await list_survey_questions(pid)
    chunks = render_survey_preview(
        {"title": poll["question"], "intro_text": poll.get("intro_text"),
         "outro_text": poll.get("outro_text")},
        questions,
    )
    for chunk in chunks:
        await callback.message.answer(chunk, parse_mode="HTML")
    await callback.message.answer(
        f"Выше — вся анкета, как её увидит делегат.\n\n"
        f"Кому: <b>{_esc(audience_label(spec))}</b> — {len(ids)} чел.\n"
        f"{await _quiet_hours_warning()}"
        "Отправить сейчас или запланировать?",
        parse_mode="HTML", reply_markup=kb,
    )


async def _publish(pid: int, data: dict, when_str: str) -> int:
    return await publish_survey_draft(
        pid, audience=data.get("audience") or [], city=data.get("city"), scheduled_at=when_str,
    )


@router.callback_query(F.data.startswith("psv_send:"))
async def survey_send_now(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    pid = _parse_id(callback.data)
    poll = await get_poll(pid) if pid is not None else None
    if poll is None or poll.get("kind") != "survey":
        await callback.answer("Анкета не найдена — откройте «📊 Опросы».", show_alert=True)
        return
    if poll.get("status") != "draft":
        await callback.answer("Эта анкета уже отправлена.", show_alert=True)
        return
    if await _poll_out_of_scope(callback.from_user.id, poll):
        await callback.answer("Эта анкета адресована другому городу — переключите город в шапке.",
                              show_alert=True)
        return
    data = await state.get_data()
    if data.get("pid") != pid or "audience" not in data:
        await callback.answer("Сессия истекла — нажмите «➡️ Аудитория и отправка» и выберите аудиторию.",
                              show_alert=True)
        return
    if await _publish(pid, data, _fmt_dt(_now_moscow_naive())) != 1:
        await callback.answer("Эта анкета уже отправлена.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _edit(callback, f"🚀 Отправляю анкету #{pid}… Сообщу, когда закончу.")
    _spawn(_deliver_and_report(bot, pid, callback.from_user.id))


@router.callback_query(F.data.startswith("psv_sched:"))
async def survey_schedule_start(callback: types.CallbackQuery, state: FSMContext):
    pid = _parse_id(callback.data)
    if not await _guard_cb(callback, pid):
        return
    data = await state.get_data()
    if data.get("pid") != pid or "audience" not in data:
        await callback.answer("Сессия истекла — нажмите «➡️ Аудитория и отправка» и выберите аудиторию.",
                              show_alert=True)
        return
    await state.set_state(SurveyBuild.schedule_when)
    await callback.answer()
    await _edit(
        callback,
        "🕒 Когда отправить? Введите дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (по Москве)\n"
        "Например: 01.07.2026 14:30",
    )


@router.message(SurveyBuild.schedule_when)
async def survey_schedule_when(message: types.Message, state: FSMContext):
    when = _parse_schedule_dt(message.text)
    if when is None:
        await message.answer("❌ Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30)")
        return
    if when <= _now_moscow_naive():
        await message.answer("❌ Это время уже прошло. Введите будущую дату, например 01.07.2026 14:30.")
        return
    data = await state.get_data()
    pid = data.get("pid")
    if not await _guard_msg(message, state, pid):
        return
    if await _publish(pid, data, _fmt_dt(when)) != 1:
        await state.clear()
        await message.answer("Эта анкета уже отправлена.", reply_markup=ReplyKeyboardRemove())
        return
    schedule_poll_job(pid, when)
    await state.clear()
    await message.answer(
        f"✅ Анкета #{pid} запланирована на {when.strftime('%d.%m.%Y %H:%M')} "
        f"({audience_label(data.get('audience'))}).\nОтменить можно в карточке опроса: «📊 Опросы».",
        reply_markup=ReplyKeyboardRemove(),
    )


# Мастер добавления вопроса живёт в своём модуле; импорт в конце, чтобы избежать цикла.
from handlers.comms import admin_survey_question  # noqa: E402, F401
