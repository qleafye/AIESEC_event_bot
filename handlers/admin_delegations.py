"""Экран «🏫 Делегации» внутри раздела «📋 Заявки»: делегации вузов на Москву.

Менеджер заявок выбирает форму делегаций из подключённых внешних форм, подтверждает ключевые
вопросы (ФИО, вуз, курс, почта) по их подписям, видит счётчики и сводку по вузам и правит все
настройки модуля (тумблер геймификации, дата отсечки ЦА, курсы не ЦА, тексты делегату)
собственными кнопками — без права «⚙️ Настройки» и без общего редактора `settings_edit:*`.

Шов на общий `handlers.admin.router`: своего Router нет, каждый декоратор — в одну строку
(инвариант cap-теста), `admin_sections` импортируется лениво (цикл на уровне модуля).
Права — в handlers/admin_caps.py: экран и все `dlg_*` — `moderate_reg` (то же право, что у
модерации заявок). Своего раздела в корне нет: девять разделов зафиксированы, экран — строка
`screen` в «📋 Заявки», поэтому «← Назад» = `back_button("admin_delegations")` →
`admin_sec:apps` (группа настроек того же раздела гейтится правом `settings` и менеджеру
с одной капой заявок ответила бы «Раздел недоступен» — её не используем).

Все значения из чужой формы (название, подписи вопросов, вузы) идут в HTML только через
`html.escape`. Коды вопросов (qkey) и id формы менеджеру не показываются — только подписи.
Списки «❔ Проверить курс», «⏳ Не зашли» и выбор листа UR REGS живут в соседнем шве
(handlers/admin_delegations_review.py); здесь — только кнопки-входы.
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import delegations_db as ddb
from database import ext_forms_db as ef
from handlers.admin import router
from services import delegations
from services.background import spawn
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

SCREEN = "admin_delegations"
PAGE = 8
_LABEL_LIMIT = 60

# Роль ключевого вопроса -> ключ реестра (qkey вопроса формы) и подписи для человека.
_KEY_SETTINGS = {
    "fullname": "delegation_q_fullname",
    "university": "delegation_q_university",
    "course": "delegation_q_course",
    "email": "delegation_q_email",
}
_KEY_TITLES = {"fullname": "ФИО", "university": "Вуз", "course": "Курс", "email": "Почта"}
_KEY_FOR = {"fullname": "ФИО", "university": "вуза", "course": "курса", "email": "почты"}
_REQUIRED_KEYS = ("fullname", "university", "course")

_NO_FORM_TEXT = (
    "🏫 <b>Делегации вузов</b>\n\n"
    "Форма делегаций не выбрана. Нажмите «📝 Выбрать форму» — список подключённых форм из "
    "«📝 Внешние формы».\n\n"
    "Сначала подключите Яндекс Форму делегаций в «📊 Данные → 📝 Внешние формы»."
)
_FORM_GONE_TEXT = (
    "🏫 <b>Делегации вузов</b>\n\n"
    "Форма отключена или удалена — выберите другую."
)
_NO_FORMS_ALERT = (
    "Ни одна форма ещё не подключена. Сначала подключите Яндекс Форму в "
    "«📊 Данные → 📝 Внешние формы»."
)
_FORM_NOT_FOUND = "Форма не найдена — обновите список."
_STALE_QUESTIONS = "Список вопросов устарел — откройте выбор ещё раз"
_KEYS_MISSING = "Без вопросов ФИО, вуз и курс бот не сможет узнать делегата — выберите их."
_KEYS_SAVED = "Вопросы сохранены, разбираю ответы формы…"


def _e(value) -> str:
    return html_module.escape(str(value if value is not None else ""))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _back() -> InlineKeyboardButton:
    """«← Назад» в раздел-владелец («📋 Заявки»), не в группу настроек."""
    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    return back_button("admin_delegations")


def _to_screen() -> InlineKeyboardButton:
    return _btn("← К делегациям", SCREEN)


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tail(data: str) -> str | None:
    parts = str(data).split(":", 1)
    return parts[1] if len(parts) == 2 else None


def _tail_int(data: str) -> int | None:
    try:
        return int(_tail(data))
    except (TypeError, ValueError):
        return None


def _cut(text: str) -> str:
    return text if len(text) <= _LABEL_LIMIT else text[:_LABEL_LIMIT - 1] + "…"


def _message_of(target):
    return getattr(target, "message", None) or target


async def _show(target, text: str, kb: InlineKeyboardMarkup, *, edit: bool = True) -> None:
    msg = _message_of(target)
    if edit and hasattr(msg, "edit_text"):
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


def _admin_id(target) -> int | None:
    user = getattr(target, "from_user", None)
    return getattr(user, "id", None)


def _form_title(form: dict) -> str:
    return form.get("title") or f"Форма №{form['id']}"


# ── настройки модуля (чтение) ─────────────────────────────────────────────────────────────

async def _game_on() -> bool:
    return await get_setting_typed("delegation_game_enabled") == "on"


async def _cutoff_label() -> str:
    dt = delegations.cutoff_dt(await get_setting_typed("delegation_ta_cutoff"))
    return dt.strftime("%d.%m.%Y") if dt else "не задана"


def _chosen(raw) -> list[str]:
    """Значение list-ключа без сентинела пустого набора (как в handlers/admin_reg_scoring)."""
    from moderation_card import EMPTY_SENTINEL
    if not raw or list(raw) == [EMPTY_SENTINEL]:
        return []
    return [str(v) for v in raw]


async def _not_ta_courses() -> list[str]:
    return _chosen(await get_setting_typed("delegation_not_ta_courses"))


# ── экран ─────────────────────────────────────────────────────────────────────────────────

async def _counters(fid: int) -> dict:
    ok = await ddb.count_by_status(fid, "ok", linked=None)
    no = await ddb.count_by_status(fid, "no", linked=None)
    check = await ddb.count_by_status(fid, "check", linked=None)
    in_bot = await ddb.count_by_status(fid, "ok", linked=True)
    absent = await ddb.count_by_status(fid, "ok", linked=False)
    summary = await ddb.summary_by_university(fid)
    arrived = sum(int(r.get("arrived") or 0) for r in summary)
    return {"total": ok + no + check, "ok": ok, "no": no, "check": check,
            "in_bot": in_bot, "absent": absent, "arrived": arrived}


def _sheet_lines(form: dict) -> list[str]:
    if form.get("mirror_mode") == "yandex_export" and form.get("mirror_tab"):
        lines = [f"📋 Лист: «{_e(form['mirror_tab'])}» · запись включена"]
    else:
        lines = ["📋 Лист: не выбран — нажмите «📋 Лист UR REGS»"]
    if form.get("mirror_error"):
        lines.append(f"⛔ Запись остановлена: {_e(form['mirror_error'])}")
    if form.get("mirror_warning"):
        lines.append(f"⚠️ {_e(form['mirror_warning'])}")
    return lines


def _keys_line(keys: dict) -> str:
    marks = " · ".join(
        f"{title} {'✓' if keys.get(which) else '—'}"
        for which, title in (("fullname", "ФИО"), ("university", "вуз"),
                             ("course", "курс"), ("email", "почта"))
    )
    line = f"Вопросы: {marks}"
    if any(not keys.get(which) for which in _REQUIRED_KEYS):
        line += "\n⚠️ Подтвердите вопросы формы"
    return line


async def _settings_rows() -> list[list[InlineKeyboardButton]]:
    game = "✅" if await _game_on() else "☐"
    courses = ", ".join(await _not_ta_courses()) or "нет"
    return [
        [_btn(f"🎮 Геймификация для делегатов: {game}", "dlg_game")],
        [_btn(f"📅 Отсечка ЦА: {await _cutoff_label()}", "dlg_cutoff")],
        [_btn(f"🎓 Курсы не ЦА: {courses}", "dlg_courses")],
        [_btn("✏️ Тексты делегату", "dlg_text")],
    ]


async def render_screen(target, *, edit: bool = True) -> None:
    fid = await delegations.delegation_form_id()
    form = await ef.get_form(fid) if fid is not None else None
    if form is None:
        text = _NO_FORM_TEXT if fid is None else _FORM_GONE_TEXT
        await _show(target, text, _kb([[_btn("📝 Выбрать форму", "dlg_form_pick")], [_back()]]),
                    edit=edit)
        return
    keys = await delegations.field_keys()
    c = await _counters(fid)
    lines = [f"🏫 <b>Делегации вузов</b>\n📝 Форма: {_e(_form_title(form))}"]
    lines.extend(_sheet_lines(form))
    lines.append(_keys_line(keys))
    lines.append(
        f"\nВ форме: {c['total']} · ЦА: {c['ok']} · не ЦА: {c['no']} · проверить: {c['check']} · "
        f"зашли в бота: {c['in_bot']} · пришли: {c['arrived']}"
    )
    rows = [
        [_btn(f"❔ Проверить курс ({c['check']})", "dlg_review:0")],
        [_btn(f"⏳ Не зашли ({c['absent']})", "dlg_absent:0")],
        [_btn("🏫 По вузам", "dlg_univ:0")],
        [_btn("📋 Лист UR REGS", "dlg_sheet")],
    ]
    rows.extend(await _settings_rows())
    rows.append([_btn("❓ Вопросы формы", "dlg_keys"), _btn("📝 Сменить форму", "dlg_form_pick")])
    rows.append([_back()])
    await _show(target, "\n".join(lines), _kb(rows), edit=edit)


@router.callback_query(F.data == "admin_delegations")
async def admin_delegations(callback: types.CallbackQuery):
    await render_screen(callback)
    await callback.answer()


# ── сводка по вузам ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dlg_univ:"))
async def dlg_univ(callback: types.CallbackQuery):
    fid = await delegations.delegation_form_id()
    rows_db = await ddb.summary_by_university(fid) if fid is not None else []
    offset = max(0, _tail_int(callback.data) or 0)
    lines = ["🏫 <b>По вузам</b>"]
    rows: list[list[InlineKeyboardButton]] = []
    if not rows_db:
        lines.append("\nПока ни одного ответа ЦА в форме.")
    else:
        offset = min(offset, max(0, len(rows_db) - 1))
        for r in rows_db[offset:offset + PAGE]:
            name = r.get("university") or "вуз не указан"
            lines.append(f"{_e(name)} — ЦА {int(r.get('ta') or 0)} · в боте "
                         f"{int(r.get('in_bot') or 0)} · пришли {int(r.get('arrived') or 0)}")
        nav: list[InlineKeyboardButton] = []
        if offset > 0:
            nav.append(_btn("⬅️", f"dlg_univ:{max(0, offset - PAGE)}"))
        if offset + PAGE < len(rows_db):
            nav.append(_btn("➡️", f"dlg_univ:{offset + PAGE}"))
        if nav:
            rows.append(nav)
        lines.append(f"\nВсего вузов: {len(rows_db)}")
    rows.append([_to_screen()])
    await _show(callback, "\n".join(lines), _kb(rows))
    await callback.answer()


# ── выбор формы ───────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "dlg_form_pick")
async def dlg_form_pick(callback: types.CallbackQuery):
    forms = await ef.list_forms()
    if not forms:
        await callback.answer(_NO_FORMS_ALERT, show_alert=True)
        return
    rows = [[_btn(_cut(f"📝 {_form_title(f)}"), f"dlg_form:{f['id']}")] for f in forms]
    rows.append([_to_screen()])
    await _show(callback, "📝 Какая форма — список делегаций вузов?", _kb(rows))
    await callback.answer()


async def _release_previous_export(prev_id: int | None, new_id: int) -> None:
    """Прежняя форма делегаций не продолжает писать в UR REGS: её зеркало «как выгрузка
    Яндекса» снимается, режим возвращается к раскладке бота, предупреждение листа гасится."""
    if prev_id is None or prev_id == new_id:
        return
    prev = await ef.get_form(prev_id)
    if prev is None or prev.get("mirror_mode") != "yandex_export":
        return
    await ef.set_form_mirror(prev_id, None, None)
    await ef.set_form_mirror_mode(prev_id, "bot")
    await ef.set_form_mirror_warning(prev_id, None)


@router.callback_query(F.data.startswith("dlg_form:"))
async def dlg_form(callback: types.CallbackQuery):
    fid = _tail_int(callback.data)
    form = await ef.get_form(fid) if fid is not None else None
    if form is None:
        await callback.answer(_FORM_NOT_FOUND, show_alert=True)
        return
    admin = _admin_id(callback)
    await _release_previous_export(await delegations.delegation_form_id(), fid)
    await set_setting_by_admin(admin, "delegation_form_id", str(fid))
    columns = await ef.list_columns(fid)
    guess = delegations.guess_delegation_questions([(c["qkey"], c["label"]) for c in columns])
    for which, key in _KEY_SETTINGS.items():
        await set_setting_by_admin(admin, key, guess.get(which) or "")
    await _show_keys(callback, form)
    await callback.answer()


# ── ключевые вопросы ──────────────────────────────────────────────────────────────────────

async def _current_form() -> dict | None:
    fid = await delegations.delegation_form_id()
    return await ef.get_form(fid) if fid is not None else None


def _q_line(title: str, labels: dict, qkey) -> str:
    label = labels.get(qkey) if qkey else None
    return f"{title} — " + (f"вопрос «{_e(label)}»" if label else "не выбран")


async def _show_keys(target, form: dict, *, edit: bool = True) -> None:
    labels = {c["qkey"]: c["label"] for c in await ef.list_columns(form["id"])}
    keys = await delegations.field_keys()
    text = (f"📋 <b>{_e(_form_title(form))}</b>\n\n"
            "Проверьте вопросы формы — по ним бот узнаёт делегата:\n"
            + "\n".join(_q_line(_KEY_TITLES[w], labels, keys.get(w)) for w in _KEY_SETTINGS)
            + "\n" + _q_line("Ник в Telegram", labels, form.get("key_username_q"))
            + " (из настроек формы)")
    if any(not keys.get(w) for w in _REQUIRED_KEYS):
        text += "\n\n⚠️ Без вопросов ФИО, вуз и курс бот не узнает делегата."
    rows = [[_btn("✅ Верно", "dlg_keys_ok")]]
    rows.extend([_btn(f"✏️ Другой вопрос для {_KEY_FOR[w]}", f"dlg_key:{w}")] for w in _KEY_SETTINGS)
    rows.append([_to_screen()])
    await _show(target, text, _kb(rows), edit=edit)


@router.callback_query(F.data == "dlg_keys")
async def dlg_keys(callback: types.CallbackQuery):
    form = await _current_form()
    if form is None:
        await render_screen(callback)
    else:
        await _show_keys(callback, form)
    await callback.answer()


@router.callback_query(F.data.startswith("dlg_key:"))
async def dlg_key(callback: types.CallbackQuery):
    which = _tail(callback.data)
    form = await _current_form()
    if form is None or which not in _KEY_SETTINGS:
        await callback.answer(_FORM_NOT_FOUND, show_alert=True)
        return
    columns = await ef.list_columns(form["id"])
    rows = [[_btn(_cut(c["label"] or c["qkey"]), f"dlg_keyset:{which}:{i}")]
            for i, c in enumerate(columns)]
    rows.append([_btn("Такого вопроса нет", f"dlg_keyset:{which}:none")])
    rows.append([_btn("← Назад", "dlg_keys")])
    await _show(callback, f"Какой вопрос формы — {_KEY_FOR[which]}?", _kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("dlg_keyset:"))
async def dlg_keyset(callback: types.CallbackQuery):
    parts = str(callback.data).split(":")
    form = await _current_form()
    if form is None or len(parts) != 3 or parts[1] not in _KEY_SETTINGS:
        await callback.answer(_FORM_NOT_FOUND, show_alert=True)
        return
    if parts[2] == "none":
        qkey = ""
    else:
        columns = await ef.list_columns(form["id"])
        try:
            qkey = columns[int(parts[2])]["qkey"]
        except (ValueError, IndexError):
            await callback.answer(_STALE_QUESTIONS, show_alert=True)
            return
    await set_setting_by_admin(_admin_id(callback), _KEY_SETTINGS[parts[1]], qkey)
    await _show_keys(callback, form)
    await callback.answer()


@router.callback_query(F.data == "dlg_keys_ok")
async def dlg_keys_ok(callback: types.CallbackQuery):
    keys = await delegations.field_keys()
    if any(not keys.get(w) for w in _REQUIRED_KEYS):
        await callback.answer(_KEYS_MISSING, show_alert=True)
        return
    spawn(delegations.sweep_pending(reevaluate=True))
    await callback.answer(_KEYS_SAVED)
    await render_screen(callback)
