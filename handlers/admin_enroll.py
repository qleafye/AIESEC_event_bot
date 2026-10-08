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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Экран записи у сессии: трек, компетенции, закрытие, лимит мест
# ══════════════════════════════════════════════════════════════════════════════════════════

_STATE_WORDS = {"open": "открыта", "closed": "закрыта", "full": "мест нет"}


async def enroll_card_lines(session: dict) -> list[str]:
    """Строки карточки сессии: трек, компетенции, состояние записи."""
    track = await edb.get_track(session["track_id"]) if session.get("track_id") else None
    names = (await edb.competency_names_for_sessions([session["id"]]))[session["id"]]
    count = await edb.count_enrollments(session["id"])
    state = _STATE_WORDS[await session_open_state(session, count)]
    limit = session.get("enroll_limit")
    limit_text = f"лимит {limit}" if limit is not None else "без лимита"
    return [
        f"🧭 Трек: {html_module.escape(track['name']) if track else 'общая сессия'}",
        f"🎯 Компетенции: {html_module.escape(', '.join(names)) if names else '—'}",
        f"📅 Запись: {state} · {limit_text} · записано {count}",
    ]


async def render_enroll_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    sid, code = session["id"], session["city"]
    lines = [f"🧭 <b>Запись и треки</b> — {html_module.escape(session['title'])}", ""]
    lines += await enroll_card_lines(session)
    lines.append("")
    buttons: list[list[InlineKeyboardButton]] = []

    tracks = await edb.list_tracks(code)
    if tracks:
        lines.append("Трек определяет, на какие сессии можно записаться. Без трека сессия общая "
                     "(пленарка) — записи на неё нет.")
        for t in tracks:
            mark = "✅ " if t["id"] == session.get("track_id") else ""
            buttons.append([InlineKeyboardButton(
                text=f"{mark}{_short(t['name'], 40)}", callback_data=f"prog_enrtrk:{sid}:{t['id']}",
            )])
        mark = "✅ " if not session.get("track_id") else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}Без трека — общая сессия", callback_data=f"prog_enrtrk:{sid}:0",
        )])
    else:
        lines.append("Треков в этом городе пока нет — заведите их в «🧭 Треки», например «Карьера».")
        buttons.append([InlineKeyboardButton(text="🧭 Завести треки", callback_data=f"prog_trkl:{code}")])

    comps = await edb.list_competencies(code)
    if comps:
        chosen = set(await edb.get_session_competency_ids(sid))
        lines.append("Компетенции сессии — отметьте галочками:")
        for c in comps:
            mark = "☑" if c["id"] in chosen else "☐"
            buttons.append([InlineKeyboardButton(
                text=f"{mark} {_short(c['name'], 40)}", callback_data=f"prog_enrcmp:{sid}:{c['id']}",
            )])
    else:
        lines.append("Компетенций пока нет — заведите их в «🎯 Компетенции».")
        buttons.append([InlineKeyboardButton(text="🎯 Завести компетенции", callback_data=f"prog_cmpl:{code}")])

    closed = bool(session.get("enroll_closed"))
    buttons.append([InlineKeyboardButton(
        text=f"🔒 Запись закрыта: {'да' if closed else 'нет'}", callback_data=f"prog_enrcl:{sid}",
    )])
    limit = session.get("enroll_limit")
    buttons.append([InlineKeyboardButton(
        text=f"👥 Лимит мест: {limit if limit is not None else 'без лимита'}",
        callback_data=f"prog_enrlim:{sid}",
    )])
    if limit is not None:
        buttons.append([InlineKeyboardButton(text="♾ Без лимита", callback_data=f"prog_enrlim0:{sid}")])
    buttons.append([InlineKeyboardButton(text="← К сессии", callback_data=f"prog_v:{sid}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _load_session(callback: types.CallbackQuery, session_id: int) -> dict | None:
    session = await get_program_session(session_id)
    if session is None:
        await callback.answer("Сессия больше недоступна.", show_alert=True)
        return None
    if await _deny_city(callback, session["city"]):
        return None
    return session


async def _show_card(callback: types.CallbackQuery, session_id: int, note: str | None = None) -> None:
    session = await get_program_session(session_id)
    if session is None:
        await callback.message.edit_text("Сессия больше недоступна.")
        await callback.answer()
        return
    text, kb = await render_enroll_card(session)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(note)


@router.callback_query(F.data.startswith("prog_enrcard:"))
async def prog_enrcard(callback: types.CallbackQuery):
    session = await _load_session(callback, int(callback.data.split(":", 1)[1]))
    if session is not None:
        await _show_card(callback, session["id"])


async def _drop_session_enrollments(session_id: int) -> int:
    users = await edb.list_enrolled_users(session_id)
    for u in users:
        await edb.unenroll(u["telegram_id"], session_id)
    return len(users)


@router.callback_query(F.data.startswith("prog_enrtrk:"))
async def prog_enrtrk(callback: types.CallbackQuery):
    _, sid_s, tid_s = callback.data.split(":", 2)
    session = await _load_session(callback, int(sid_s))
    if session is None:
        return
    tid = int(tid_s)
    if tid:
        track = await edb.get_track(tid)
        if track is None or track["city"] != session["city"]:
            await callback.answer("Этого трека нет в городе сессии — обновите экран.", show_alert=True)
            return
        await update_program_session(session["id"], track_id=tid)
        await _show_card(callback, session["id"])
        return
    count = await edb.count_enrollments(session["id"]) if session.get("track_id") else 0
    if count:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, сделать общей", callback_data=f"prog_enrtrkgo:{session['id']}:0")],
            [InlineKeyboardButton(text="← Отмена", callback_data=f"prog_enrcard:{session['id']}")],
        ])
        await callback.message.edit_text(
            f"Сессия «{html_module.escape(session['title'])}» станет общей (без записи), "
            f"записи на эту сессию удалятся: {count}.\n\nСделать общей?",
            parse_mode="HTML", reply_markup=kb,
        )
        await callback.answer()
        return
    await update_program_session(session["id"], track_id=None)
    await _show_card(callback, session["id"])


@router.callback_query(F.data.startswith("prog_enrtrkgo:"))
async def prog_enrtrkgo(callback: types.CallbackQuery):
    _, sid_s, _tid = callback.data.split(":", 2)
    session = await _load_session(callback, int(sid_s))
    if session is None:
        return
    await _drop_session_enrollments(session["id"])
    await update_program_session(session["id"], track_id=None)
    await _show_card(callback, session["id"], "Сессия стала общей.")


@router.callback_query(F.data.startswith("prog_enrcmp:"))
async def prog_enrcmp(callback: types.CallbackQuery):
    _, sid_s, cid_s = callback.data.split(":", 2)
    session = await _load_session(callback, int(sid_s))
    if session is None:
        return
    comp = await edb.get_competency(int(cid_s))
    if comp is None or comp["city"] != session["city"]:
        await callback.answer("Этой компетенции нет в городе сессии — обновите экран.", show_alert=True)
        return
    await edb.toggle_session_competency(session["id"], comp["id"])
    await _show_card(callback, session["id"])


@router.callback_query(F.data.startswith("prog_enrcl:"))
async def prog_enrcl(callback: types.CallbackQuery):
    session = await _load_session(callback, int(callback.data.split(":", 1)[1]))
    if session is None:
        return
    await update_program_session(session["id"], enroll_closed=0 if session.get("enroll_closed") else 1)
    await _show_card(callback, session["id"])


@router.callback_query(F.data.startswith("prog_enrlim0:"))
async def prog_enrlim0(callback: types.CallbackQuery):
    session = await _load_session(callback, int(callback.data.split(":", 1)[1]))
    if session is None:
        return
    await update_program_session(session["id"], enroll_limit=None)
    await _show_card(callback, session["id"], "Лимит снят.")


@router.callback_query(F.data.startswith("prog_enrlim:"))
async def prog_enrlim(callback: types.CallbackQuery, state: FSMContext):
    session = await _load_session(callback, int(callback.data.split(":", 1)[1]))
    if session is None:
        return
    await state.set_data({"enr_sid": session["id"]})
    await state.set_state(ProgramEnrollLimit.value)
    await callback.message.answer(
        "Сколько мест на этой сессии? Пришлите число.\nНапример: 30", reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(ProgramEnrollLimit.value)
async def prog_enrlim_step(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    data = await state.get_data()
    session = await get_program_session(data.get("enr_sid", 0))
    if session is None or not await _city_allowed(message.from_user.id, session["city"]):
        await state.clear()
        await message.answer("Сессия больше недоступна.")
        return
    if raw == "-":
        limit = None
    elif raw.isdigit() and int(raw) >= 1:
        limit = int(raw)
    else:
        await message.answer("Нужно целое число мест, например 30", reply_markup=_cancel_kb())
        return
    await state.clear()
    await update_program_session(session["id"], enroll_limit=limit)
    fresh = await get_program_session(session["id"])
    text, kb = await render_enroll_card(fresh)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
