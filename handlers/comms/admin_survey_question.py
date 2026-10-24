"""Конструктор анкеты: мастер добавления вопроса всех пяти типов (callback psv_qadd:<pid>).

Шаги: тип → текст → параметры типа (варианты и лимит «до N», подписи краёв шкалы, строки таблицы
и подпись «не могу оценить») → «Спросить подробнее» (для вариантов) → обязательность → сохранить.
Собираемый вопрос живёт в FSM data под ключом "q" (в БД он попадает только по psv_qsave);
текущий экран — q["step"], по нему отсекаются «чужие» нажатия старых кнопок.

Право — `broadcast` ("psv_*", "state:SurveyBuild:*" в handlers/access/admin_caps.py).

INVARIANT (cap-test): каждый декоратор `@router.*` — в ОДНУ строку.
"""
import re

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove

from database.survey_db import add_survey_question, list_survey_questions
from services.comms.surveys import (
    QTYPE_LABELS,
    SURVEY_MAX_QUESTIONS,
    QUESTION_MAX,
    OPTION_MAX,
    OPTIONS_MIN,
    OPTIONS_MAX,
    ROWS_MAX,
    LABEL_MAX,
    DEFAULT_NA_LABEL,
    validate_question_text,
    validate_option,
    validate_options,
    validate_rows,
    validate_label,
)
from keyboards.builders import get_cancel_kb
from handlers.states import SurveyBuild
from handlers.comms.admin_polls import _parse_id, _short
from handlers.comms.admin_poll_wizard import split_options
from handlers.comms.admin_survey_builder import (
    _btn,
    _edit,
    _esc,
    _guard_cb,
    _guard_msg,
    _show_draft_cb,
)
from handlers.admin import router

_EXPIRED = "Сессия истекла — откройте черновик в «📊 Опросы» и добавьте вопрос заново."
_QTYPE_HINTS = {
    "single": "Делегат выберет один вариант из списка.",
    "multi": "Делегат отметит несколько вариантов и нажмёт «Готово».",
    "scale": "Делегат поставит оценку от 1 до 5 кнопками.",
    "text": "Делегат ответит своими словами, одним сообщением.",
    "table": "Список строк (например, сессий), и каждую делегат оценивает от 1 до 5.",
}
_TEXT_EXAMPLES = {
    "single": "Как тебе программа первого дня?",
    "multi": "Что тебе понравилось больше всего?",
    "scale": "Насколько тебе было полезно на Юлиде?",
    "text": "Что бы ты улучшил в организации?",
    "table": "Оцени сессии первого дня",
}
_MORE_EXAMPLE = "Какая сессия или спикер? Что понравилось?"


def _cancel_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ Отменить вопрос", f"psv_draft:{pid}")]])


async def _send(target, text: str, kb=None) -> None:
    if hasattr(target, "data"):  # CallbackQuery (у Message такого поля нет)
        await _edit(target, text, kb)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


async def _load(callback: types.CallbackQuery, state: FSMContext, *steps: str):
    """q из FSM или None (с алертом). Проверяет, что анкета — черновик, и что экран актуален."""
    q = (await state.get_data()).get("q")
    if not q or "pid" not in q:
        await callback.answer(_EXPIRED, show_alert=True)
        return None
    if not await _guard_cb(callback, q["pid"]):
        return None
    if steps and q.get("step") not in steps:
        await callback.answer("Этот экран уже устарел — продолжайте с последнего сообщения.", show_alert=True)
        return None
    return q


async def _load_msg(message: types.Message, state: FSMContext):
    q = (await state.get_data()).get("q")
    if not q or "pid" not in q:
        await state.clear()
        await message.answer(_EXPIRED, reply_markup=ReplyKeyboardRemove())
        return None
    if not await _guard_msg(message, state, q["pid"]):
        return None
    return q


async def _put(state: FSMContext, q: dict, new_state=None) -> None:
    """Сохранить q (pid дублируем на верхний уровень — его читает общая «Отмена»)."""
    await state.update_data(q=q, pid=q["pid"])
    await state.set_state(new_state)


# ── тип ─────────────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("psv_qadd:"))
async def survey_qadd(callback: types.CallbackQuery, state: FSMContext):
    pid = _parse_id(callback.data)
    if not await _guard_cb(callback, pid):
        return
    if len(await list_survey_questions(pid)) >= SURVEY_MAX_QUESTIONS:
        await callback.answer(
            f"В анкете уже {SURVEY_MAX_QUESTIONS} вопросов — удалите лишний, чтобы добавить новый",
            show_alert=True,
        )
        return
    q = {"pid": pid, "step": "type", "qtype": None, "text": "", "options": [], "max_choices": None,
         "rows": [], "scale_lo": None, "scale_hi": None, "na_label": None, "required": True,
         "more_idx": None}
    await state.clear()
    await _put(state, q)
    rows = [[_btn(label, f"psv_qt:{code}")] for code, label in QTYPE_LABELS.items()]
    rows.append([_btn("❌ Отменить вопрос", f"psv_draft:{pid}")])
    hints = "\n".join(f"• <b>{label}</b> — {_QTYPE_HINTS[code]}" for code, label in QTYPE_LABELS.items())
    await callback.answer()
    await _edit(callback, f"➕ <b>Новый вопрос</b>\n\nКакого типа вопрос?\n\n{hints}",
                InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("psv_qt:"))
async def survey_qtype(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "type")
    if q is None:
        return
    qtype = callback.data[len("psv_qt:"):]
    if qtype not in QTYPE_LABELS:
        await callback.answer("Выберите тип одной из кнопок.", show_alert=True)
        return
    q.update(qtype=qtype, step="text")
    await _put(state, q, SurveyBuild.q_text)
    await callback.answer()
    await callback.message.answer(
        f"Тип: <b>{QTYPE_LABELS[qtype]}</b>. Пришлите текст вопроса (до {QUESTION_MAX} символов).\n"
        f"Например: <i>{_TEXT_EXAMPLES[qtype]}</i>",
        parse_mode="HTML", reply_markup=get_cancel_kb(),
    )


@router.message(SurveyBuild.q_text)
async def survey_qtext(message: types.Message, state: FSMContext):
    err = validate_question_text(message.text)
    if err:
        await message.answer(err)
        return
    q = await _load_msg(message, state)
    if q is None:
        return
    q["text"] = message.text.strip()
    qtype = q["qtype"]
    if qtype in ("single", "multi"):
        q["step"] = "options"
        await _put(state, q, SurveyBuild.q_options)
        await message.answer(
            f"Пришлите варианты ответа — по одному сообщением или сразу несколько через «;» "
            f"(например: <i>Да; Нет; Не знаю</i>).\nОт {OPTIONS_MIN} до {OPTIONS_MAX} вариантов, "
            f"каждый до {OPTION_MAX} символов. Когда закончите — «✅ Варианты готовы».",
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(_options_status(q), parse_mode="HTML", reply_markup=_options_kb(q))
    elif qtype == "scale":
        q["step"] = "scale_lo"
        await _put(state, q, SurveyBuild.q_scale_lo)
        await message.answer(
            "Как подписать оценку <b>1</b>? Пришлите короткую подпись (до "
            f"{LABEL_MAX} символов).\nНапример: <i>1 — не моё</i>",
            parse_mode="HTML",
        )
    elif qtype == "table":
        q["step"] = "rows"
        await _put(state, q, SurveyBuild.q_rows)
        await message.answer(
            "Пришлите строки таблицы — по одной сообщением или сразу несколько через «;» "
            f"(например: <i>Открытие; Сессия Карьера; Ужин</i>).\nДо {ROWS_MAX} строк, каждая до "
            f"{OPTION_MAX} символов. Когда закончите — «✅ Строки готовы».",
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(_rows_status(q), parse_mode="HTML", reply_markup=_rows_kb(q))
    else:
        await _ask_required(message, state, q)


# ── варианты (один / несколько) ─────────────────────────────────────────────────────────────

def _options_status(q: dict) -> str:
    opts = q["options"]
    lines = "\n".join(f"{i}. {_esc(o['t'])}" for i, o in enumerate(opts, start=1))
    head = f"Варианты: {len(opts)} (нужно {OPTIONS_MIN}–{OPTIONS_MAX})"
    return f"{head}\n{lines}" if lines else head


def _options_kb(q: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Варианты готовы", "psv_qopts")],
        [_btn("❌ Отменить вопрос", f"psv_draft:{q['pid']}")],
    ])


@router.message(SurveyBuild.q_options)
async def survey_qoptions_step(message: types.Message, state: FSMContext):
    q = await _load_msg(message, state)
    if q is None:
        return
    new = split_options(message.text or "")
    if not new:
        await message.answer("Нужен текст варианта — пришлите его сообщением, например: Да.")
        return
    for piece in new:
        err = validate_option(piece)
        if err:
            await message.answer(f"«{_short(piece, 40)}»: {err}", reply_markup=_options_kb(q))
            return
    if len(q["options"]) + len(new) > OPTIONS_MAX:
        await message.answer(
            f"Получится {len(q['options']) + len(new)} вариантов, а можно не больше {OPTIONS_MAX}. "
            f"Уже есть {len(q['options'])} — добавьте не больше {OPTIONS_MAX - len(q['options'])} "
            "или нажмите «✅ Варианты готовы».",
            reply_markup=_options_kb(q),
        )
        return
    q["options"] = q["options"] + [{"t": t, "more": None} for t in new]
    await _put(state, q, SurveyBuild.q_options)
    await message.answer(_options_status(q), parse_mode="HTML", reply_markup=_options_kb(q))


@router.callback_query(F.data == "psv_qopts", SurveyBuild.q_options)
async def survey_qoptions_done(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "options")
    if q is None:
        return
    err = validate_options([o["t"] for o in q["options"]])
    if err:
        await callback.answer(
            f"Нужно хотя бы {OPTIONS_MIN} варианта — пришлите ещё." if len(q["options"]) < OPTIONS_MIN else err,
            show_alert=True,
        )
        return
    await callback.answer()
    if q["qtype"] == "multi":
        q["step"] = "max"
        await _put(state, q)
        n = len(q["options"])
        buttons = [_btn(str(k), f"psv_qmax:{k}") for k in range(1, n)]
        rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        rows.append([_btn("Без лимита", "psv_qmax:0")])
        await _edit(callback, "Сколько вариантов можно выбрать самое большее?\n"
                              "Нажмите число или «Без лимита».", InlineKeyboardMarkup(inline_keyboard=rows))
        return
    await _ask_more(callback, state, q)


@router.callback_query(F.data.startswith("psv_qmax:"))
async def survey_qmax(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "max")
    if q is None:
        return
    n = _parse_id(callback.data)
    if n is None or not 0 <= n < len(q["options"]):
        await callback.answer("Выберите число одной из кнопок.", show_alert=True)
        return
    q["max_choices"] = n or None
    await callback.answer()
    await _ask_more(callback, state, q)


# ── «Спросить подробнее» ────────────────────────────────────────────────────────────────────

def _more_screen(q: dict):
    rows = []
    for i, opt in enumerate(q["options"]):
        mark = "💬 " if (opt.get("more") or "").strip() else ""
        rows.append([_btn(f"{mark}{_short(opt['t'], 40)}", f"psv_qmore:{i}")])
    rows.append([_btn("➡️ Дальше", "psv_qmore_done")])
    rows.append([_btn("❌ Отменить вопрос", f"psv_draft:{q['pid']}")])
    text = (
        "💬 <b>Спросить подробнее</b>\n\nЕсли делегат выберет вариант с пометкой 💬, бот попросит "
        "его дописать пояснение. Нажмите на вариант, чтобы добавить уточнение, и ещё раз — чтобы "
        "убрать. Если не нужно — «➡️ Дальше»."
    )
    for i, opt in enumerate(q["options"], start=1):
        if (opt.get("more") or "").strip():
            text += f"\n{i}. {_esc(opt['t'])} — «{_esc(opt['more'])}»"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_more(target, state: FSMContext, q: dict) -> None:
    q.update(step="more", more_idx=None)
    await _put(state, q)
    await _send(target, *_more_screen(q))


@router.callback_query(F.data.startswith("psv_qmore:"))
async def survey_qmore(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "more")
    if q is None:
        return
    i = _parse_id(callback.data)
    if i is None or not 0 <= i < len(q["options"]):
        await callback.answer("Выберите вариант из списка кнопок.", show_alert=True)
        return
    await callback.answer()
    if (q["options"][i].get("more") or "").strip():
        q["options"][i]["more"] = None
        await _ask_more(callback, state, q)
        return
    q.update(step="more_text", more_idx=i)
    await _put(state, q, SurveyBuild.q_more_text)
    await callback.message.answer(
        f"Что спросить у тех, кто выберет «{_esc(_short(q['options'][i]['t'], 40))}»? "
        f"Пришлите вопрос-уточнение (до {QUESTION_MAX} символов).\nНапример: <i>{_MORE_EXAMPLE}</i>",
        parse_mode="HTML", reply_markup=get_cancel_kb(),
    )


@router.message(SurveyBuild.q_more_text)
async def survey_qmore_text(message: types.Message, state: FSMContext):
    err = validate_question_text(message.text)
    if err:
        await message.answer(err)
        return
    q = await _load_msg(message, state)
    if q is None:
        return
    i = q.get("more_idx")
    if i is None or not 0 <= i < len(q["options"]):
        await state.clear()
        await message.answer(_EXPIRED, reply_markup=ReplyKeyboardRemove())
        return
    q["options"][i]["more"] = message.text.strip()
    q.update(step="more", more_idx=None)
    await _put(state, q)
    await message.answer("Уточнение добавлено ✅", reply_markup=ReplyKeyboardRemove())
    await _send(message, *_more_screen(q))


@router.callback_query(F.data == "psv_qmore_done")
async def survey_qmore_done(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "more")
    if q is None:
        return
    await callback.answer()
    await _ask_required(callback, state, q)


# ── шкала ───────────────────────────────────────────────────────────────────────────────────

def _clean_scale_label(text: str, digit: str) -> str:
    """«1 — не моё» → «не моё»: цифру и тире бот дописывает сам."""
    return re.sub(rf"^\s*{digit}\s*[—–\-:.)]*\s*", "", text.strip()).strip()


async def _scale_label(message: types.Message, state: FSMContext, digit: str):
    raw = message.text or ""
    label = _clean_scale_label(raw, digit)
    err = validate_label(label) if label else (
        f"Подпись пустая. Пришлите короткую подпись, например: {digit} — "
        f"{'не моё' if digit == '1' else 'классно'}."
    )
    if err:
        await message.answer(err)
        return None
    return label


@router.message(SurveyBuild.q_scale_lo)
async def survey_scale_lo(message: types.Message, state: FSMContext):
    label = await _scale_label(message, state, "1")
    if label is None:
        return
    q = await _load_msg(message, state)
    if q is None:
        return
    q.update(scale_lo=label, step="scale_hi")
    await _put(state, q, SurveyBuild.q_scale_hi)
    await message.answer(
        f"Как подписать оценку <b>5</b>? Пришлите короткую подпись (до {LABEL_MAX} символов).\n"
        "Например: <i>5 — классно</i>", parse_mode="HTML",
    )


@router.message(SurveyBuild.q_scale_hi)
async def survey_scale_hi(message: types.Message, state: FSMContext):
    label = await _scale_label(message, state, "5")
    if label is None:
        return
    q = await _load_msg(message, state)
    if q is None:
        return
    q["scale_hi"] = label
    await _ask_required(message, state, q)


# ── таблица ─────────────────────────────────────────────────────────────────────────────────

def _rows_status(q: dict) -> str:
    rows = q["rows"]
    lines = "\n".join(f"{i}. {_esc(r)}" for i, r in enumerate(rows, start=1))
    head = f"Строк: {len(rows)} (до {ROWS_MAX})"
    return f"{head}\n{lines}" if lines else head


def _rows_kb(q: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Строки готовы", "psv_qrows")],
        [_btn("❌ Отменить вопрос", f"psv_draft:{q['pid']}")],
    ])


@router.message(SurveyBuild.q_rows)
async def survey_qrows_step(message: types.Message, state: FSMContext):
    q = await _load_msg(message, state)
    if q is None:
        return
    new = split_options(message.text or "")
    if not new:
        await message.answer("Нужен текст строки — пришлите его сообщением, например: Ужин.")
        return
    total = q["rows"] + new
    err = validate_rows(total)
    if err:
        await message.answer(
            f"{err}\nУже принято строк: {len(q['rows'])} — они сохранены.", reply_markup=_rows_kb(q),
        )
        return
    q["rows"] = total
    await _put(state, q, SurveyBuild.q_rows)
    await message.answer(_rows_status(q), parse_mode="HTML", reply_markup=_rows_kb(q))


@router.callback_query(F.data == "psv_qrows", SurveyBuild.q_rows)
async def survey_qrows_done(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "rows")
    if q is None:
        return
    if not q["rows"]:
        await callback.answer("Нужна хотя бы одна строка — пришлите её сообщением.", show_alert=True)
        return
    q["step"] = "na"
    await _put(state, q)
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"Оставить «{DEFAULT_NA_LABEL}»", "psv_qna:keep")],
        [_btn("✏️ Своя подпись", "psv_qna:edit")],
        [_btn("❌ Отменить вопрос", f"psv_draft:{q['pid']}")],
    ])
    await _edit(
        callback,
        "К каждой строке есть кнопка на случай, если делегат не был на этой сессии. "
        f"Сейчас она подписана «{DEFAULT_NA_LABEL}». Оставить или написать свою (например «Не был(а)»)?",
        kb,
    )


@router.callback_query(F.data.startswith("psv_qna:"))
async def survey_qna(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "na")
    if q is None:
        return
    choice = callback.data[len("psv_qna:"):]
    if choice == "keep":
        q["na_label"] = None
        await callback.answer()
        await _ask_required(callback, state, q)
    elif choice == "edit":
        q["step"] = "na_text"
        await _put(state, q, SurveyBuild.q_na)
        await callback.answer()
        await callback.message.answer(
            f"Пришлите подпись кнопки (до {LABEL_MAX} символов).\nНапример: <i>Не был(а)</i>",
            parse_mode="HTML", reply_markup=get_cancel_kb(),
        )
    else:
        await callback.answer("Выберите вариант одной из кнопок.", show_alert=True)


@router.message(SurveyBuild.q_na)
async def survey_qna_text(message: types.Message, state: FSMContext):
    label = (message.text or "").strip()
    err = validate_label(label) if label else "Подпись пустая. Пришлите, например: Не был(а)."
    if err:
        await message.answer(err)
        return
    q = await _load_msg(message, state)
    if q is None:
        return
    q["na_label"] = label
    await message.answer("Подпись сохранена ✅", reply_markup=ReplyKeyboardRemove())
    await _ask_required(message, state, q)


# ── обязательность и сохранение ─────────────────────────────────────────────────────────────

def _required_screen(q: dict):
    mark = "✅" if q["required"] else "⬜"
    text = (
        f"<b>[{QTYPE_LABELS[q['qtype']]}]</b> {_esc(q['text'])}\n\n"
        "Обязательный вопрос нельзя пропустить. Если выключить — у делегата появится кнопка «Пропустить»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"Обязательный: {mark}", "psv_qreq")],
        [_btn("💾 Сохранить вопрос", "psv_qsave")],
        [_btn("❌ Отменить вопрос", f"psv_draft:{q['pid']}")],
    ])
    return text, kb


async def _ask_required(target, state: FSMContext, q: dict) -> None:
    q["step"] = "req"
    await _put(state, q)
    await _send(target, *_required_screen(q))


@router.callback_query(F.data == "psv_qreq")
async def survey_qreq(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "req")
    if q is None:
        return
    q["required"] = not q["required"]
    await _put(state, q)
    await callback.answer()
    await _edit(callback, *_required_screen(q))


@router.callback_query(F.data == "psv_qsave")
async def survey_qsave(callback: types.CallbackQuery, state: FSMContext):
    q = await _load(callback, state, "req")
    if q is None:
        return
    if len(await list_survey_questions(q["pid"])) >= SURVEY_MAX_QUESTIONS:
        await callback.answer(
            f"В анкете уже {SURVEY_MAX_QUESTIONS} вопросов — удалите лишний, чтобы добавить новый",
            show_alert=True,
        )
        return
    qtype = q["qtype"]
    options = None
    if qtype in ("single", "multi"):
        options = [{"t": o["t"], "more": (o.get("more") or None)} for o in q["options"]]
    await add_survey_question(
        q["pid"], qtype, q["text"],
        required=q["required"],
        max_choices=q["max_choices"] if qtype == "multi" else None,
        options=options,
        rows=q["rows"] if qtype == "table" else None,
        scale_lo=q["scale_lo"] if qtype == "scale" else None,
        scale_hi=q["scale_hi"] if qtype == "scale" else None,
        na_label=q["na_label"] if qtype == "table" else None,
    )
    pid = q["pid"]
    await state.clear()
    await state.update_data(pid=pid)
    await callback.answer("Вопрос добавлен ✅")
    await _show_draft_cb(callback, pid)
