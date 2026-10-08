"""Запись на сессии: справочники «🧭 Треки» и «🎯 Компетенции» города и экран записи у сессии
(трек, компетенции, закрытие, лимит мест) — всё кнопками.

Форма шва та же, что у `admin_program_halls.py`: своего `Router()` нет, `from handlers.admin
import router`, каждый декоратор в одну строку; импортирован ХВОСТОМ `handlers/admin.py`. Право —
`settings` по префиксу `prog_*` (handlers/admin_caps.py), состояния — `state:Program*:*`.
Город экрана по id всегда берётся из строки БД (трека/компетенции/сессии), а не из callback'а,
и сверяется с `_city_allowed`: подменить id в кнопке и править чужой город нельзя."""
import html as html_module

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import city_label
from database import session_enroll_db as edb
from database.db import get_program_session, update_program_session
from handlers.admin import router
from handlers.admin_program import _CITY_FORBIDDEN_ALERT, _city_allowed, _short
from handlers.states import ProgramCompetencyEdit, ProgramEnrollLimit, ProgramTrackEdit
from services.session_enroll import session_open_state

_NAME_MAX = 60
_CANCEL_WORDS = {"Отмена", "/cancel"}

# Всё, чем отличаются треки от компетенций: подписи и функции слоя данных.
_KINDS = {
    "trk": {
        "title": "🧭 Треки", "one": "трек", "example": "Карьера", "state": ProgramTrackEdit.name,
        "list": edb.list_tracks, "get": edb.get_track, "create": edb.create_track,
        "rename": edb.rename_track, "move": edb.move_track,
    },
    "cmp": {
        "title": "🎯 Компетенции", "one": "компетенцию", "example": "Лидерство",
        "state": ProgramCompetencyEdit.name,
        "list": edb.list_competencies, "get": edb.get_competency, "create": edb.create_competency,
        "rename": edb.rename_competency, "move": edb.move_competency,
    },
}


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="prog_enrcancel")],
    ])


async def _deny_city(callback: types.CallbackQuery, code: str | None) -> bool:
    """True — доступа нет, отказ уже показан."""
    if await _city_allowed(callback.from_user.id, code):
        return False
    await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
    return True


async def _load_item(callback: types.CallbackQuery, kind: str, item_id: int) -> dict | None:
    """Элемент справочника по id с проверкой права на его город; None — ответ уже дан."""
    row = await _KINDS[kind]["get"](item_id)
    if row is None:
        await callback.answer("Этого пункта уже нет — обновите список.", show_alert=True)
        return None
    if await _deny_city(callback, row["city"]):
        return None
    return row


# ── Экраны справочника ────────────────────────────────────────────────────────────────────────

async def render_list_screen(kind: str, code: str) -> tuple[str, InlineKeyboardMarkup]:
    cfg = _KINDS[kind]
    items = await cfg["list"](code)
    lines = [f"{cfg['title']} — {html_module.escape(await city_label(code))}", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if items:
        for item in items:
            buttons.append([InlineKeyboardButton(
                text=_short(item["name"], 40), callback_data=f"prog_{kind}o:{item['id']}",
            )])
        if kind == "trk":
            lines.append("Нажмите на трек, чтобы переименовать, сдвинуть или удалить его.")
        else:
            lines.append("Нажмите на компетенцию, чтобы переименовать, сдвинуть или удалить её.")
    elif kind == "trk":
        lines.append("Треков пока нет. Добавьте первый, например «Карьера» — тогда сессиям можно "
                     "будет назначать трек, а делегатам — записываться.")
    else:
        lines.append("Компетенций пока нет. Добавьте первую, например «Лидерство» — её можно будет "
                     "отметить у сессий и в тесте.")
    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"prog_{kind}n:{code}")])
    buttons.append([InlineKeyboardButton(text="← Запись на сессии", callback_data=f"prog_enrset:{code}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _item_screen(kind: str, row: dict) -> tuple[str, InlineKeyboardMarkup]:
    cfg = _KINDS[kind]
    if kind == "trk":
        used = await edb.count_sessions_for_track(row["id"])
    else:
        used = await edb.count_sessions_for_competency(row["id"])
    head = f"{cfg['title']}: <b>{html_module.escape(row['name'])}</b>\n\n"
    text = head + (f"Сессий в треке: {used}" if kind == "trk" else f"Отмечена у сессий: {used}")
    rid = row["id"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"prog_{kind}r:{rid}")],
        [InlineKeyboardButton(text="⬆️ Выше", callback_data=f"prog_{kind}m:{rid}:-1"),
         InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"prog_{kind}m:{rid}:1")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"prog_{kind}x:{rid}")],
        [InlineKeyboardButton(text="← К списку", callback_data=f"prog_{kind}l:{row['city']}")],
    ])
    return text, kb


async def _open_list(callback: types.CallbackQuery, kind: str, code: str) -> None:
    if await _deny_city(callback, code):
        return
    text, kb = await render_list_screen(kind, code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _start_create(callback: types.CallbackQuery, state: FSMContext, kind: str, code: str) -> None:
    if await _deny_city(callback, code):
        return
    cfg = _KINDS[kind]
    await state.set_data({"enr_kind": kind, "enr_mode": "create", "enr_city": code})
    await state.set_state(cfg["state"])
    await callback.message.answer(
        f"Как назвать {cfg['one']}? Пришлите название до {_NAME_MAX} символов.\n"
        f"Например: {cfg['example']}", reply_markup=_cancel_kb(),
    )
    await callback.answer()


async def _open_item(callback: types.CallbackQuery, kind: str) -> None:
    row = await _load_item(callback, kind, int(callback.data.split(":", 1)[1]))
    if row is None:
        return
    text, kb = await _item_screen(kind, row)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _start_rename(callback: types.CallbackQuery, state: FSMContext, kind: str) -> None:
    row = await _load_item(callback, kind, int(callback.data.split(":", 1)[1]))
    if row is None:
        return
    cfg = _KINDS[kind]
    await state.set_data({"enr_kind": kind, "enr_mode": "rename", "enr_id": row["id"]})
    await state.set_state(cfg["state"])
    await callback.message.answer(
        f"Новое название для «{html_module.escape(row['name'])}». Например: {cfg['example']}",
        parse_mode="HTML", reply_markup=_cancel_kb(),
    )
    await callback.answer()


async def _move(callback: types.CallbackQuery, kind: str) -> None:
    _, rest = callback.data.split(":", 1)
    rid_s, delta_s = rest.split(":", 1)
    row = await _load_item(callback, kind, int(rid_s))
    if row is None:
        return
    moved = await _KINDS[kind]["move"](row["id"], int(delta_s))
    text, kb = await render_list_screen(kind, row["city"])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer() if moved else await callback.answer("Дальше двигать некуда.")


async def _delete_confirm(callback: types.CallbackQuery, kind: str) -> None:
    row = await _load_item(callback, kind, int(callback.data.split(":", 1)[1]))
    if row is None:
        return
    name = html_module.escape(row["name"])
    if kind == "trk":
        sessions = await edb.count_sessions_for_track(row["id"])
        enrolls = await edb.count_enrollments_for_track(row["id"])
        lines = [f"🗑 <b>Удалить трек «{name}»?</b>", "",
                 f"Сессий в треке: {sessions}. Сессии останутся в программе, но станут общими "
                 f"(без записи), записи на них удалятся: {enrolls}."]
    else:
        sessions = await edb.count_sessions_for_competency(row["id"])
        lines = [f"🗑 <b>Удалить компетенцию «{name}»?</b>", "",
                 f"Она снимется с сессий: {sessions}. Сами сессии не изменятся."]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"prog_{kind}xgo:{row['id']}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"prog_{kind}o:{row['id']}")],
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _delete_go(callback: types.CallbackQuery, kind: str) -> None:
    row = await _load_item(callback, kind, int(callback.data.split(":", 1)[1]))
    if row is None:
        return
    if kind == "trk":
        await edb.delete_track(row["id"])
    else:
        await edb.delete_competency(row["id"])
    text, kb = await render_list_screen(kind, row["city"])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Удалено.")


# ── Треки: callback'и ─────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_trkl:"))
async def prog_trkl(callback: types.CallbackQuery):
    await _open_list(callback, "trk", callback.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("prog_trkn:"))
async def prog_trkn(callback: types.CallbackQuery, state: FSMContext):
    await _start_create(callback, state, "trk", callback.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("prog_trko:"))
async def prog_trko(callback: types.CallbackQuery):
    await _open_item(callback, "trk")


@router.callback_query(F.data.startswith("prog_trkr:"))
async def prog_trkr(callback: types.CallbackQuery, state: FSMContext):
    await _start_rename(callback, state, "trk")


@router.callback_query(F.data.startswith("prog_trkm:"))
async def prog_trkm(callback: types.CallbackQuery):
    await _move(callback, "trk")


@router.callback_query(F.data.startswith("prog_trkx:"))
async def prog_trkx(callback: types.CallbackQuery):
    await _delete_confirm(callback, "trk")


@router.callback_query(F.data.startswith("prog_trkxgo:"))
async def prog_trkxgo(callback: types.CallbackQuery):
    await _delete_go(callback, "trk")


# ── Компетенции: callback'и ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_cmpl:"))
async def prog_cmpl(callback: types.CallbackQuery):
    await _open_list(callback, "cmp", callback.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("prog_cmpn:"))
async def prog_cmpn(callback: types.CallbackQuery, state: FSMContext):
    await _start_create(callback, state, "cmp", callback.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("prog_cmpo:"))
async def prog_cmpo(callback: types.CallbackQuery):
    await _open_item(callback, "cmp")


@router.callback_query(F.data.startswith("prog_cmpr:"))
async def prog_cmpr(callback: types.CallbackQuery, state: FSMContext):
    await _start_rename(callback, state, "cmp")


@router.callback_query(F.data.startswith("prog_cmpm:"))
async def prog_cmpm(callback: types.CallbackQuery):
    await _move(callback, "cmp")


@router.callback_query(F.data.startswith("prog_cmpx:"))
async def prog_cmpx(callback: types.CallbackQuery):
    await _delete_confirm(callback, "cmp")


@router.callback_query(F.data.startswith("prog_cmpxgo:"))
async def prog_cmpxgo(callback: types.CallbackQuery):
    await _delete_go(callback, "cmp")


# ── Ввод названия ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "prog_enrcancel")
async def prog_enrcancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await callback.answer()


async def _name_step(message: types.Message, state: FSMContext) -> None:
    name = " ".join((message.text or "").split())
    data = await state.get_data()
    kind = data.get("enr_kind")
    if kind not in _KINDS:
        await state.clear()
        await message.answer("Что-то пошло не так — откройте раздел заново.")
        return
    cfg = _KINDS[kind]
    if not name or len(name) > _NAME_MAX:
        await message.answer(
            f"Название — от 1 до {_NAME_MAX} символов. Например: {cfg['example']}",
            reply_markup=_cancel_kb(),
        )
        return
    if data.get("enr_mode") == "rename":
        row = await cfg["get"](data.get("enr_id", 0))
        code = row["city"] if row else None
    else:
        code = data.get("enr_city")
    if not code or not await _city_allowed(message.from_user.id, code):
        await state.clear()
        await message.answer(_CITY_FORBIDDEN_ALERT)
        return
    same = [i for i in await cfg["list"](code)
            if i["name"].lower() == name.lower() and i["id"] != data.get("enr_id")]
    if same:
        await message.answer("Такое название уже есть — придумайте другое.", reply_markup=_cancel_kb())
        return
    if data.get("enr_mode") == "rename":
        await cfg["rename"](data["enr_id"], name)
    else:
        await cfg["create"](code, name)
    await state.clear()
    text, kb = await render_list_screen(kind, code)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _cancel_word(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(StateFilter(ProgramTrackEdit), F.text.in_(_CANCEL_WORDS))
async def prog_trk_cancel_word(message: types.Message, state: FSMContext):
    await _cancel_word(message, state)


@router.message(StateFilter(ProgramCompetencyEdit), F.text.in_(_CANCEL_WORDS))
async def prog_cmp_cancel_word(message: types.Message, state: FSMContext):
    await _cancel_word(message, state)


@router.message(StateFilter(ProgramEnrollLimit), F.text.in_(_CANCEL_WORDS))
async def prog_lim_cancel_word(message: types.Message, state: FSMContext):
    await _cancel_word(message, state)


@router.message(ProgramTrackEdit.name)
async def prog_trk_name_step(message: types.Message, state: FSMContext):
    await _name_step(message, state)


@router.message(ProgramCompetencyEdit.name)
async def prog_cmp_name_step(message: types.Message, state: FSMContext):
    await _name_step(message, state)
