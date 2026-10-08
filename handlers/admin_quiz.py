"""«🧭 Тест компетенций» города в админке: настройки, вопросы, варианты, баллы по компетенциям.

Контент заводит менеджер кнопками (или файлом — `admin_quiz_import.py`); в коде нет ни одного
вопроса. Форма шва — как у `admin_enroll_list.py`: общий `admin.router`, импорт хвостом
`handlers/admin.py`, право `settings` по префиксу `prog_*`, ввод текста — `QuizEdit.value`.
Уровни/статистика/ссылка/тексты и импорт — соседние швы, подключаются хвостовыми импортами ниже."""
import html as html_module

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import city_label, get_setting_typed_for_city
from database import quiz_db as qdb
from database import session_enroll_db as edb
from handlers.admin import router
from handlers.admin_program import _CITY_FORBIDDEN_ALERT, _city_allowed, _short
from handlers.states import QuizEdit

_PAGE = 8
_CANCEL_WORDS = {"Отмена", "/cancel"}
_TITLE_MAX = 100
_INTRO_MAX = 1000
_QUESTION_MAX = 500
_OPTION_MAX = 200
_STALE = "Этого больше нет — обновите экран."


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="prog_qzcancel")]])


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def deny(callback: types.CallbackQuery, code: str | None) -> bool:
    if await _city_allowed(callback.from_user.id, code):
        return False
    await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
    return True


async def points_max(code: str) -> int:
    try:
        return max(1, int(await get_setting_typed_for_city("quiz_points_max", code)))
    except (TypeError, ValueError):
        return 5


async def quiz_by_code(code: str) -> dict:
    return await qdb.get_or_create_quiz(code)


async def _question_ctx(callback: types.CallbackQuery, qid_s: str):
    """(вопрос, тест) или None — с ответом об устаревшей кнопке/чужом городе."""
    q = await qdb.get_question(int(qid_s))
    quiz = await qdb.get_quiz(q["quiz_id"]) if q else None
    if quiz is None:
        await callback.answer(_STALE, show_alert=True)
        return None
    if await deny(callback, quiz["city"]):
        return None
    return q, quiz


async def _option_ctx(callback: types.CallbackQuery, oid_s: str):
    o = await qdb.get_option(int(oid_s))
    q = await qdb.get_question(o["question_id"]) if o else None
    quiz = await qdb.get_quiz(q["quiz_id"]) if q else None
    if quiz is None:
        await callback.answer(_STALE, show_alert=True)
        return None
    if await deny(callback, quiz["city"]):
        return None
    return o, q, quiz


# ── Экран теста ──────────────────────────────────────────────────────────────────────────────

async def render_quiz(code: str) -> tuple[str, InlineKeyboardMarkup]:
    quiz = await quiz_by_code(code)
    n_q = await qdb.count_questions(quiz["id"])
    n_lv = len(await qdb.list_levels(quiz["id"]))
    on = bool(quiz["enabled"])
    pct = quiz["score_mode"] == "percent"
    lines = [
        f"🧭 <b>Тест компетенций</b> — {html_module.escape(await city_label(code))}", "",
        f"Название: {html_module.escape(quiz['title']) if quiz['title'] else 'не задано'}",
        f"Вопросов: {n_q} · Уровней: {n_lv}",
        f"Тест {'включён' if on else 'выключен'} · пересдача {'разрешена' if quiz['allow_retake'] else 'запрещена'}",
        f"Считать: {'в %' if pct else 'в баллах'}",
    ]
    if not n_q:
        lines += ["", "Добавьте вопросы или загрузите их из таблицы."]
    elif not on:
        lines += ["", "Делегаты тест не видят, пока он выключен."]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Тест включён → выключить" if on else "❌ Тест выключен → включить", f"prog_qzsw:{code}")],
        [_btn("🔁 Пересдача: можно" if quiz["allow_retake"] else "🔁 Пересдача: нельзя", f"prog_qzrt:{code}")],
        [_btn("Считать: в %" if pct else "Считать: в баллах", f"prog_qzmode:{code}")],
        [_btn("✏️ Название", f"prog_qzf:{code}:title"), _btn("✏️ Вступление", f"prog_qzf:{code}:intro")],
        [_btn(f"❓ Вопросы ({n_q})", f"prog_qzql:{code}:0"), _btn(f"📊 Уровни ({n_lv})", f"prog_qzl:{code}")],
        [_btn("📥 Загрузить из таблицы", f"prog_qzimp:{code}")],
        [_btn("📈 Статистика", f"prog_qzst:{code}"), _btn("🔗 Ссылка на тест", f"prog_qzlink:{code}")],
        [_btn("✏️ Тексты", f"prog_qztx:{code}:0")],
        [_btn("← Настройки записи", f"prog_enrset:{code}")],
    ])
    return "\n".join(lines), kb


async def _show_quiz(callback: types.CallbackQuery, code: str, note: str | None = None) -> None:
    text, kb = await render_quiz(code)
    from handlers.admin_forum_hub_nav import keep_hub_back  # открыт из хаба форума — «Назад» в хаб
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keep_hub_back(callback.message, kb))
    await callback.answer(note)


@router.callback_query(F.data.startswith("prog_qz:"))
async def prog_qz(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    await state.clear()
    await _show_quiz(callback, code)


@router.callback_query(F.data == "prog_qzcancel")
async def prog_qzcancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzsw:"))
async def prog_qzsw(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    quiz = await quiz_by_code(code)
    if not quiz["enabled"]:
        broken = await qdb.first_question_without_options(quiz["id"])
        if broken:
            await callback.answer(
                f"Нельзя включить: у вопроса {broken[0]} «{broken[1]['text'][:60]}» нет вариантов "
                "ответа. Добавьте хотя бы один вариант или удалите вопрос.",
                show_alert=True,
            )
            return
    await qdb.update_quiz(quiz["id"], enabled=0 if quiz["enabled"] else 1)
    await _show_quiz(callback, code, "Сохранено.")


@router.callback_query(F.data.startswith("prog_qzrt:"))
async def prog_qzrt(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    quiz = await quiz_by_code(code)
    await qdb.update_quiz(quiz["id"], allow_retake=0 if quiz["allow_retake"] else 1)
    await _show_quiz(callback, code, "Сохранено.")


@router.callback_query(F.data.startswith("prog_qzmode:"))
async def prog_qzmode(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    quiz = await quiz_by_code(code)
    await qdb.update_quiz(quiz["id"], score_mode="points" if quiz["score_mode"] == "percent" else "percent")
    note = "Сохранено."
    if await qdb.list_levels(quiz["id"]):
        note = "Сохранено. Пороги уровней теперь в другой единице — проверьте их в «Уровнях»."
    await _show_quiz(callback, code, note)


# ── Ввод текста (название, вступление, вопрос, вариант; уровни — в admin_quiz_levels) ───────

_INPUT_HELP = {
    "title": ("Название теста", "Например: Тест компетенций Юлида", _TITLE_MAX),
    "intro": ("Вступление к тесту — его увидит делегат перед первым вопросом",
              "Например: Ответьте на 10 вопросов, это займёт пять минут.", _INTRO_MAX),
    "q_new": ("Текст нового вопроса", "Например: Как вы ведёте команду к цели?", _QUESTION_MAX),
    "q_edit": ("Новый текст вопроса", "Например: Как вы ведёте команду к цели?", _QUESTION_MAX),
    "o_new": ("Текст нового варианта ответа", "Например: Беру ответственность на себя", _OPTION_MAX),
    "o_edit": ("Новый текст варианта", "Например: Беру ответственность на себя", _OPTION_MAX),
}


async def ask_value(callback: types.CallbackQuery, state: FSMContext, what: str, target: int | str,
                    prompt: str | None = None) -> None:
    """Включает ввод текста: what — что правим, target — id/код объекта."""
    await state.set_state(QuizEdit.value)
    await state.set_data({"what": what, "target": target})
    if prompt is None:
        title, example, limit = _INPUT_HELP[what]
        prompt = f"{title}.\n{example}\n\nДо {limit} символов."
    await callback.message.answer(html_module.escape(prompt), parse_mode="HTML", reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzf:"))
async def prog_qzf(callback: types.CallbackQuery, state: FSMContext):
    _, code, what = callback.data.split(":", 2)
    if await deny(callback, code) or what not in ("title", "intro"):
        return
    quiz = await quiz_by_code(code)
    await ask_value(callback, state, what, quiz["id"])


@router.message(StateFilter(QuizEdit.value), F.text.in_(_CANCEL_WORDS))
async def prog_qz_cancel_word(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(QuizEdit.value)
async def prog_qz_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    what, target = data.get("what"), data.get("target")
    if what and what.startswith("level"):
        from handlers.admin_quiz_levels import level_input
        await level_input(message, state, what, target)
        return
    if what not in _INPUT_HELP:
        await state.clear()
        await message.answer("Что-то пошло не так — откройте раздел заново.")
        return
    value = " ".join((message.text or "").split()) if what != "intro" else (message.text or "").strip()
    title, example, limit = _INPUT_HELP[what]
    if not value or len(value) > limit:
        await message.answer(f"Нужен текст от 1 до {limit} символов. {example}", reply_markup=_cancel_kb())
        return
    # Город — из строки БД, а не из state: тап по кнопке чужого города мог остаться в чате.
    if what in ("title", "intro", "q_new"):
        quiz = await qdb.get_quiz(target)
    elif what in ("q_edit", "o_new"):
        q = await qdb.get_question(target)
        quiz = await qdb.get_quiz(q["quiz_id"]) if q else None
    else:
        o = await qdb.get_option(target)
        q = await qdb.get_question(o["question_id"]) if o else None
        quiz = await qdb.get_quiz(q["quiz_id"]) if q else None
    if quiz is None:
        await state.clear()
        await message.answer(_STALE)
        return
    if not await _city_allowed(message.from_user.id, quiz["city"]):
        await state.clear()
        await message.answer(_CITY_FORBIDDEN_ALERT)
        return
    await state.clear()
    if what in ("title", "intro"):
        await qdb.update_quiz(quiz["id"], **{what: value})
        text, kb = await render_quiz(quiz["city"])
    elif what == "q_new":
        qid = await qdb.create_question(quiz["id"], value)
        text, kb = await render_question(qid)
        text = "Вопрос добавлен. Теперь добавьте варианты ответа.\n\n" + text
    elif what == "q_edit":
        await qdb.update_question_text(target, value)
        text, kb = await render_question(target)
    elif what == "o_new":
        oid = await qdb.create_option(target, value)
        text, kb = await render_option(oid)
    else:
        await qdb.update_option_text(target, value)
        text, kb = await render_option(target)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Вопросы ──────────────────────────────────────────────────────────────────────────────────

async def _render_qlist(code: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    quiz = await quiz_by_code(code)
    questions = await qdb.list_questions(quiz["id"])
    pages = max(1, -(-len(questions) // _PAGE))
    page = min(max(page, 0), pages - 1)
    lines = ["❓ <b>Вопросы теста</b>", ""]
    lines.append("Нажмите на вопрос, чтобы открыть его варианты." if questions else "Вопросов пока нет.")
    buttons = []
    for i in range(page * _PAGE, min(len(questions), (page + 1) * _PAGE)):
        buttons.append([_btn(f"{i + 1}. {_short(questions[i]['text'], 50)}", f"prog_qzq:{questions[i]['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("◀️", f"prog_qzql:{code}:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{pages}", f"prog_qzql:{code}:{page}"))
        if page < pages - 1:
            nav.append(_btn("▶️", f"prog_qzql:{code}:{page + 1}"))
        buttons.append(nav)
    buttons.append([_btn("➕ Вопрос", f"prog_qzqn:{code}")])
    buttons.append([_btn("← К тесту", f"prog_qz:{code}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_qzql:"))
async def prog_qzql(callback: types.CallbackQuery):
    _, code, page_s = callback.data.split(":", 2)
    if await deny(callback, code):
        return
    text, kb = await _render_qlist(code, int(page_s))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzqn:"))
async def prog_qzqn(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    quiz = await quiz_by_code(code)
    await ask_value(callback, state, "q_new", quiz["id"])


def _points_summary(option: dict, names: dict[int, str]) -> str:
    parts = [f"{names[cid]} {pts}" for cid, pts in option["points"].items() if cid in names]
    return ", ".join(parts) if parts else "баллов нет"


async def render_question(qid: int) -> tuple[str, InlineKeyboardMarkup]:
    q = await qdb.get_question(qid)
    quiz = await qdb.get_quiz(q["quiz_id"])
    names = {c["id"]: c["name"] for c in await edb.list_competencies(quiz["city"])}
    options = await qdb.list_options(qid)
    lines = [f"❓ <b>{html_module.escape(q['text'])}</b>", ""]
    buttons = []
    if options:
        for i, o in enumerate(options, start=1):
            lines.append(f"{i}. {html_module.escape(o['text'])} — {html_module.escape(_points_summary(o, names))}")
            buttons.append([_btn(f"{i}. {_short(o['text'], 40)}", f"prog_qzo:{o['id']}")])
    else:
        lines.append("Вариантов пока нет — добавьте первый.")
    buttons.append([_btn("➕ Вариант", f"prog_qzon:{qid}")])
    buttons.append([_btn("✏️ Текст", f"prog_qzqe:{qid}"), _btn("⬆️", f"prog_qzqm:{qid}:-1"),
                    _btn("⬇️", f"prog_qzqm:{qid}:1"), _btn("🗑", f"prog_qzqx:{qid}")])
    buttons.append([_btn("← К вопросам", f"prog_qzql:{quiz['city']}:0")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_qzq:"))
async def prog_qzq(callback: types.CallbackQuery):
    if await _question_ctx(callback, callback.data.split(":", 1)[1]) is None:
        return
    text, kb = await render_question(int(callback.data.split(":", 1)[1]))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzqe:"))
async def prog_qzqe(callback: types.CallbackQuery, state: FSMContext):
    qid = callback.data.split(":", 1)[1]
    if await _question_ctx(callback, qid) is None:
        return
    await ask_value(callback, state, "q_edit", int(qid))


@router.callback_query(F.data.startswith("prog_qzqm:"))
async def prog_qzqm(callback: types.CallbackQuery):
    _, qid, delta = callback.data.split(":", 2)
    if await _question_ctx(callback, qid) is None:
        return
    await qdb.move_question(int(qid), int(delta))
    text, kb = await render_question(int(qid))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzqx:"))
async def prog_qzqx(callback: types.CallbackQuery):
    qid = callback.data.split(":", 1)[1]
    ctx = await _question_ctx(callback, qid)
    if ctx is None:
        return
    q, quiz = ctx
    n_opts = len(await qdb.list_options(q["id"]))
    open_n = await qdb.count_open_attempts(quiz["id"])
    text = (f"🗑 Удалить вопрос «{html_module.escape(_short(q['text'], 80))}»?\n\n"
            f"Удалятся варианты: {n_opts}, незавершённые попытки начнутся заново: {open_n}.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Да, удалить вопрос", f"prog_qzqxgo:{qid}")], [_btn("← Отмена", f"prog_qzq:{qid}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzqxgo:"))
async def prog_qzqxgo(callback: types.CallbackQuery):
    qid = callback.data.split(":", 1)[1]
    ctx = await _question_ctx(callback, qid)
    if ctx is None:
        return
    await qdb.delete_question(int(qid))
    text, kb = await _render_qlist(ctx[1]["city"], 0)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Удалено.")


# ── Варианты и баллы ─────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_qzon:"))
async def prog_qzon(callback: types.CallbackQuery, state: FSMContext):
    qid = callback.data.split(":", 1)[1]
    if await _question_ctx(callback, qid) is None:
        return
    await ask_value(callback, state, "o_new", int(qid))


async def render_option(oid: int) -> tuple[str, InlineKeyboardMarkup]:
    o = await qdb.get_option(oid)
    q = await qdb.get_question(o["question_id"])
    quiz = await qdb.get_quiz(q["quiz_id"])
    comps = await edb.list_competencies(quiz["city"])
    lines = [f"🔘 <b>{html_module.escape(o['text'])}</b>", f"Вопрос: {html_module.escape(_short(q['text'], 80))}", ""]
    buttons = []
    if comps:
        lines.append("Сколько баллов этот ответ даёт по каждой компетенции — нажмите на нужную.")
        for c in comps:
            pts = o["points"].get(c["id"], 0)
            buttons.append([_btn(f"{c['name']}: {pts}", f"prog_qzoc:{oid}:{c['id']}")])
    else:
        lines.append("Сначала заведите компетенции в «🎯 Компетенции» — потом сможете начислять по ним баллы.")
        buttons.append([_btn("🎯 Компетенции", f"prog_cmpl:{quiz['city']}")])
    buttons.append([_btn("✏️ Текст", f"prog_qzoe:{oid}"), _btn("🗑", f"prog_qzox:{oid}")])
    buttons.append([_btn("← К вопросу", f"prog_qzq:{q['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_qzo:"))
async def prog_qzo(callback: types.CallbackQuery):
    oid = callback.data.split(":", 1)[1]
    if await _option_ctx(callback, oid) is None:
        return
    text, kb = await render_option(int(oid))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzoe:"))
async def prog_qzoe(callback: types.CallbackQuery, state: FSMContext):
    oid = callback.data.split(":", 1)[1]
    if await _option_ctx(callback, oid) is None:
        return
    await ask_value(callback, state, "o_edit", int(oid))


@router.callback_query(F.data.startswith("prog_qzoc:"))
async def prog_qzoc(callback: types.CallbackQuery):
    _, oid, cid = callback.data.split(":", 2)
    ctx = await _option_ctx(callback, oid)
    comp = await edb.get_competency(int(cid))
    if ctx is None or comp is None or comp["city"] != ctx[2]["city"]:
        if ctx is not None:
            await callback.answer(_STALE, show_alert=True)
        return
    o, _q, quiz = ctx
    top = await points_max(quiz["city"])
    now = o["points"].get(comp["id"], 0)
    row = [_btn(("• " if i == now else "") + str(i), f"prog_qzop:{oid}:{cid}:{i}") for i in range(top + 1)]
    kb = InlineKeyboardMarkup(inline_keyboard=[row, [_btn("← К варианту", f"prog_qzo:{oid}")]])
    await callback.message.edit_text(
        f"Сколько баллов по компетенции «{html_module.escape(comp['name'])}» даёт этот ответ?\n"
        "0 — не даёт ничего.", parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzop:"))
async def prog_qzop(callback: types.CallbackQuery):
    _, oid, cid, pts = callback.data.split(":", 3)
    ctx = await _option_ctx(callback, oid)
    comp = await edb.get_competency(int(cid))
    if ctx is None:
        return
    if comp is None or comp["city"] != ctx[2]["city"] or not 0 <= int(pts) <= await points_max(ctx[2]["city"]):
        await callback.answer(_STALE, show_alert=True)
        return
    await qdb.set_option_points(int(oid), comp["id"], int(pts))
    text, kb = await render_option(int(oid))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Сохранено.")


@router.callback_query(F.data.startswith("prog_qzox:"))
async def prog_qzox(callback: types.CallbackQuery):
    oid = callback.data.split(":", 1)[1]
    ctx = await _option_ctx(callback, oid)
    if ctx is None:
        return
    o, q, quiz = ctx
    open_n = await qdb.count_open_attempts(quiz["id"])
    text = (f"🗑 Удалить вариант «{html_module.escape(_short(o['text'], 80))}»?\n\n"
            f"Пропадут его баллы по компетенциям, незавершённые попытки начнутся заново: {open_n}.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Да, удалить вариант", f"prog_qzoxgo:{oid}")], [_btn("← Отмена", f"prog_qzo:{oid}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzoxgo:"))
async def prog_qzoxgo(callback: types.CallbackQuery):
    oid = callback.data.split(":", 1)[1]
    ctx = await _option_ctx(callback, oid)
    if ctx is None:
        return
    await qdb.delete_option(int(oid))
    text, kb = await render_question(ctx[1]["id"])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Удалено.")


# Уровни, статистика, ссылка, тексты (handlers/admin_quiz_levels.py) — хвостовой импорт.
from handlers import admin_quiz_levels  # noqa: E402,F401
# Импорт из таблицы (handlers/admin_quiz_import.py) — хвостовой импорт.
from handlers import admin_quiz_import  # noqa: E402,F401
