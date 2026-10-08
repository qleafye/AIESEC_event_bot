"""Тест компетенций в админке: уровни, статистика, ссылка-приглашение, тексты.

Продолжение `admin_quiz.py` (подключается его хвостовым импортом). Ввод текста идёт через то же
состояние `QuizEdit.value`; значения `what`, начинающиеся с `level`, разбирает `level_input`."""
import html as html_module

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import get_setting_typed_for_city
from database import quiz_db as qdb
from handlers.admin import router
from handlers.admin_enroll_list import _write_key
from handlers.admin_program import _CITY_FORBIDDEN_ALERT, _city_allowed, _short
from handlers.admin_quiz import _STALE, _btn, _cancel_kb, ask_value, deny, quiz_by_code
from handlers.states import EditSetting
from services import quiz as quiz_service
from settings_placeholders import hint
from settings_schema import SETTINGS_SCHEMA

_TEXT_PAGE = 8
_NAME_MAX = 60
_DESC_MAX = 500
_CANCEL_ROW = [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]


def _unit(quiz: dict) -> str:
    return "в % от максимума" if quiz["score_mode"] == "percent" else "в баллах"


def _threshold_example(quiz: dict) -> str:
    return "Например: 50 (от 0 до 100)" if quiz["score_mode"] == "percent" else "Например: 12 (целое число баллов)"


def _parse_threshold(raw: str, quiz: dict) -> int | None:
    raw = (raw or "").strip().rstrip("%").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    if quiz["score_mode"] == "percent" and value > 100:
        return None
    return value


# ── Уровни ───────────────────────────────────────────────────────────────────────────────────

async def render_levels(code: str) -> tuple[str, InlineKeyboardMarkup]:
    quiz = await quiz_by_code(code)
    levels = await qdb.list_levels(quiz["id"])
    suffix = "%" if quiz["score_mode"] == "percent" else " б."
    lines = ["📊 <b>Уровни теста</b>", "",
             f"Порог — {_unit(quiz)}: делегат получает самый высокий уровень, порог которого он набрал."]
    if not levels:
        lines += ["", "Уровней пока нет. Например: «Базовый» от 0, «Продвинутый» от 60."]
    buttons = [[_btn(f"{lv['name']} — от {lv['threshold']}{suffix}", f"prog_qzlo:{lv['id']}")] for lv in levels]
    buttons.append([_btn("➕ Уровень", f"prog_qzln:{code}")])
    buttons.append([_btn("← К тесту", f"prog_qz:{code}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _level_ctx(callback: types.CallbackQuery, lid_s: str):
    level = await qdb.get_level(int(lid_s))
    quiz = await qdb.get_quiz(level["quiz_id"]) if level else None
    if quiz is None:
        await callback.answer(_STALE, show_alert=True)
        return None
    if await deny(callback, quiz["city"]):
        return None
    return level, quiz


@router.callback_query(F.data.startswith("prog_qzl:"))
async def prog_qzl(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    text, kb = await render_levels(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzln:"))
async def prog_qzln(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    quiz = await quiz_by_code(code)
    await ask_value(callback, state, "level_new_name", quiz["id"],
                    f"Название уровня.\nНапример: Базовый\n\nДо {_NAME_MAX} символов.")


async def render_level(lid: int) -> tuple[str, InlineKeyboardMarkup]:
    level = await qdb.get_level(lid)
    quiz = await qdb.get_quiz(level["quiz_id"])
    suffix = "%" if quiz["score_mode"] == "percent" else " б."
    lines = [f"📊 <b>{html_module.escape(level['name'])}</b>", "",
             f"Порог: от {level['threshold']}{suffix} ({_unit(quiz)})",
             f"Описание: {html_module.escape(level['description']) if level['description'] else 'нет'}"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✏️ Название", f"prog_qzle:{lid}:name"), _btn("✏️ Порог", f"prog_qzle:{lid}:threshold")],
        [_btn("✏️ Описание", f"prog_qzle:{lid}:description"), _btn("🗑", f"prog_qzlx:{lid}")],
        [_btn("← К уровням", f"prog_qzl:{quiz['city']}")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("prog_qzlo:"))
async def prog_qzlo(callback: types.CallbackQuery):
    lid = callback.data.split(":", 1)[1]
    if await _level_ctx(callback, lid) is None:
        return
    text, kb = await render_level(int(lid))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzle:"))
async def prog_qzle(callback: types.CallbackQuery, state: FSMContext):
    _, lid, field = callback.data.split(":", 2)
    ctx = await _level_ctx(callback, lid)
    if ctx is None or field not in ("name", "threshold", "description"):
        return
    quiz = ctx[1]
    prompts = {
        "name": f"Новое название уровня.\nНапример: Продвинутый\n\nДо {_NAME_MAX} символов.",
        "threshold": f"Порог уровня — {_unit(quiz)}.\n{_threshold_example(quiz)}",
        "description": f"Описание уровня — его увидит делегат.\nНапример: Вы уверенно ведёте команду.\n\n"
                       f"До {_DESC_MAX} символов. «-» — убрать описание.",
    }
    await ask_value(callback, state, f"level_edit_{field}", int(lid), prompts[field])


@router.callback_query(F.data.startswith("prog_qzlx:"))
async def prog_qzlx(callback: types.CallbackQuery):
    lid = callback.data.split(":", 1)[1]
    ctx = await _level_ctx(callback, lid)
    if ctx is None:
        return
    level = ctx[0]
    text = (f"🗑 Удалить уровень «{html_module.escape(level['name'])}»?\n\n"
            "Делегаты, у которых он уже в результате, увидят следующий подходящий уровень "
            "(или «без уровня»). Сами результаты не пропадут.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Да, удалить уровень", f"prog_qzlxgo:{lid}")], [_btn("← Отмена", f"prog_qzlo:{lid}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzlxgo:"))
async def prog_qzlxgo(callback: types.CallbackQuery):
    lid = callback.data.split(":", 1)[1]
    ctx = await _level_ctx(callback, lid)
    if ctx is None:
        return
    await qdb.delete_level(int(lid))
    text, kb = await render_levels(ctx[1]["city"])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Удалено.")


async def level_input(message: types.Message, state: FSMContext, what: str, target: int) -> None:
    """Шаги ввода уровня: создание — название -> порог -> описание; правка — одно поле."""
    data = await state.get_data()
    if what in ("level_new_name", "level_new_threshold", "level_new_description"):
        quiz = await qdb.get_quiz(target)
    else:
        level = await qdb.get_level(target)
        quiz = await qdb.get_quiz(level["quiz_id"]) if level else None
    if quiz is None:
        await state.clear()
        await message.answer(_STALE)
        return
    if not await _city_allowed(message.from_user.id, quiz["city"]):
        await state.clear()
        await message.answer(_CITY_FORBIDDEN_ALERT)
        return
    raw = " ".join((message.text or "").split())
    kind = what.rsplit("_", 1)[1]
    if kind == "name" and not 1 <= len(raw) <= _NAME_MAX:
        await message.answer(f"Название — от 1 до {_NAME_MAX} символов. Например: Базовый", reply_markup=_cancel_kb())
        return
    if kind == "threshold":
        value = _parse_threshold(raw, quiz)
        if value is None:
            hint_ = ("Нужно число от 0 до 100, например 50" if quiz["score_mode"] == "percent"
                     else "Нужно целое число баллов, например 12")
            await message.answer(hint_, reply_markup=_cancel_kb())
            return
    if kind == "description":
        raw = "" if raw == "-" else (message.text or "").strip()
        if len(raw) > _DESC_MAX:
            await message.answer(f"Описание — до {_DESC_MAX} символов.", reply_markup=_cancel_kb())
            return
    if what == "level_new_name":
        await state.set_data({"what": "level_new_threshold", "target": target, "name": raw})
        await message.answer(f"Порог уровня — {_unit(quiz)}.\n{_threshold_example(quiz)}", reply_markup=_cancel_kb())
        return
    if what == "level_new_threshold":
        await state.set_data({"what": "level_new_description", "target": target,
                              "name": data.get("name", ""), "threshold": value})
        await message.answer(
            f"Описание уровня — его увидит делегат.\nНапример: Вы уверенно ведёте команду.\n"
            f"«-» — без описания.", reply_markup=_cancel_kb(),
        )
        return
    await state.clear()
    if what == "level_new_description":
        lid = await qdb.create_level(quiz["id"], data.get("name", ""), int(data.get("threshold", 0)), raw)
    else:
        lid = target
        patch = {"threshold": value} if kind == "threshold" else {kind: raw}
        await qdb.update_level(lid, **patch)
    text, kb = await render_level(lid)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Статистика ───────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_qzst:"))
async def prog_qzst(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    quiz = await quiz_by_code(code)
    st = await quiz_service.stats(quiz)
    no_level = str(await get_setting_typed_for_city("quiz_no_level_label", code) or "без уровня")
    lines = ["📈 <b>Статистика теста</b>", "", f"Начали: {st['started']}, закончили: {st['finished']}"]
    if st["by_competency"]:
        lines += ["", "Уровни по компетенциям (у последнего пройденного теста каждого):"]
        for comp, buckets in st["by_competency"].items():
            parts = ", ".join(f"{name or no_level} — {n}" for name, n in buckets.items())
            lines.append(f"{html_module.escape(comp)}: {html_module.escape(parts)}")
    elif st["finished"]:
        lines += ["", "Компетенций с баллами пока нет."]
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("← К тесту", f"prog_qz:{code}")]])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Ссылка на тест ───────────────────────────────────────────────────────────────────────────

async def _bot_username(bot) -> str | None:
    try:
        return (await bot.get_me()).username
    except Exception:
        return None


@router.callback_query(F.data.startswith("prog_qzlink:"))
async def prog_qzlink(callback: types.CallbackQuery, bot):
    code = callback.data.split(":", 1)[1]
    if await deny(callback, code):
        return
    username = await _bot_username(bot)
    if not username:
        await callback.answer("Не удалось узнать имя бота — попробуйте через минуту.", show_alert=True)
        return
    await callback.message.answer(
        "🔗 Ссылка на тест — нажмите на неё, чтобы скопировать, и вставьте в текст рассылки:\n\n"
        f"<code>https://t.me/{html_module.escape(username)}?start=quiz</code>",
        parse_mode="HTML",
    )
    await callback.answer()


# ── Тексты теста ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_qztx:"))
async def prog_qztx(callback: types.CallbackQuery):
    _, code, page_s = callback.data.split(":", 2)
    if await deny(callback, code):
        return
    keys = quiz_service.QUIZ_TEXT_KEYS
    pages = -(-len(keys) // _TEXT_PAGE)
    page = min(max(int(page_s), 0), pages - 1)
    buttons = []
    for idx in range(page * _TEXT_PAGE, min(len(keys), (page + 1) * _TEXT_PAGE)):
        label = SETTINGS_SCHEMA[keys[idx]]["label"].replace("🧭 Тест: ", "")
        buttons.append([_btn(_short(label, 50), f"prog_qzte:{code}:{idx}")])
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"prog_qztx:{code}:{page - 1}"))
    nav.append(_btn(f"{page + 1}/{pages}", f"prog_qztx:{code}:{page}"))
    if page < pages - 1:
        nav.append(_btn("▶️", f"prog_qztx:{code}:{page + 1}"))
    buttons.append(nav)
    buttons.append([_btn("← К тесту", f"prog_qz:{code}")])
    await callback.message.edit_text(
        "✏️ <b>Тексты теста</b>\n\nВыберите, что изменить.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_qzte:"))
async def prog_qzte(callback: types.CallbackQuery, state: FSMContext):
    _, code, idx_s = callback.data.split(":", 2)
    if await deny(callback, code):
        return
    keys = quiz_service.QUIZ_TEXT_KEYS
    idx = int(idx_s)
    if not 0 <= idx < len(keys):
        await callback.answer("Этого текста нет — обновите экран.", show_alert=True)
        return
    base = keys[idx]
    entry = SETTINGS_SCHEMA[base]
    current = await get_setting_typed_for_city(base, code)
    extra = hint(base)
    lines = [f"✏️ <b>{html_module.escape(entry['label'])}</b>", "",
             f"Сейчас: <b>{html_module.escape(str(current))}</b>" if current else "Сейчас: стандартный текст", "",
             html_module.escape(entry["prompt"])]
    if extra and "Подстановки" not in entry["prompt"]:
        lines += ["", html_module.escape(extra)]
    lines += ["", "<i>«-» — вернуть стандартный текст.</i>"]
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": await _write_key(base, code)})
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[_CANCEL_ROW]),
    )
    await callback.answer()
