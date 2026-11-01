"""Карточка внешней формы: «🎯 Назначение» и «📝 Настройки регистрации».

Шов на общий `handlers.admin.router` (своего Router нет, декораторы в одну строку). Права — все
`extf_*` под «⚙️ Настройки» (handlers/access/admin_caps.py), новых записей нет. Всё кнопками,
FSM не нужен.

Смена назначения у формы с ответами или у формы делегаций идёт через экран «что изменится»:
ничего не удаляется, новое назначение действует для ответов, пришедших после смены
(`purpose_since`). Названия форм и подписи вопросов — чужой текст, только через `html.escape`.
"""
from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import ext_forms_db as xdb
from domain.cities import city_label, cities_module_on, default_city_code, enabled_cities
from handlers.admin import router
from handlers.ext_forms.admin_ext_forms import _btn, _e, _show, _tail_id, render_form_card
from services.ext_forms import ext_forms_purpose as purp
from services.infra.timeutil import msk_now
from services.settings.audit import set_setting_by_admin

_NOT_FOUND = "Форма не найдена — обновите список."
_STALE = "Список вопросов устарел — откройте выбор ещё раз"
_NOT_REG = "Сначала выберите назначение «📝 Регистрация» в карточке формы."
_NO_QUESTIONS = ("Сначала нужен первый ответ или загрузка файла — тогда я узнаю вопросы формы.")
_LABEL_LIMIT = 40
_APPROVE_LABEL = {"queue": "в очередь заявок", "auto": "сразу одобрять"}
_REG_HELP = (
    "Каждый новый ответ становится заявкой участника — анкету в боте заполнять не нужно. "
    "Человека бот узнаёт по нику в Telegram из формы: сразу, если он уже писал боту, или когда "
    "нажмёт /start."
)
# Какой вопрос выбирается: f — ФИО, p — телефон, u — ник.
_WHAT = {"f": "ФИО", "p": "телефон", "u": "ник в Telegram"}


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cut(text: str) -> str:
    return text if len(text) <= _LABEL_LIMIT else text[:_LABEL_LIMIT - 1] + "…"


def _parts(data: str, size: int) -> tuple[int, list[str]] | None:
    parts = str(data).split(":")
    if len(parts) != size:
        return None
    try:
        return int(parts[1]), parts[2:]
    except ValueError:
        return None


async def _form_of(data: str, size: int) -> tuple[dict | None, list[str]]:
    parsed = _parts(data, size)
    if parsed is None:
        return None, []
    return await xdb.get_form(parsed[0]), parsed[1]


def _to_card(fid: int) -> list[InlineKeyboardButton]:
    return [_btn("← К форме", f"extf_card:{fid}")]


# ── назначение ────────────────────────────────────────────────────────────────────────────

async def _purpose_screen(callback, form: dict) -> None:
    current = await purp.purpose_of(form)
    rows = [[_btn(("✅ " if value == current else "") + purp.PURPOSE_LABELS[value],
                  f"extf_purpose_set:{form['id']}:{code}")]
            for code, value in purp.CODES.items()]
    rows.append(_to_card(form["id"]))
    text = (f"🎯 <b>Назначение формы «{_e(form['title'])}»</b>\n\n{purp.PURPOSE_HELP}\n\n"
            "Новое назначение действует для ответов, которые придут после смены.")
    await _show(callback, text, _kb(rows))


@router.callback_query(F.data.startswith("extf_purpose:"))
async def extf_purpose(callback: types.CallbackQuery):
    fid = _tail_id(callback.data)
    form = await xdb.get_form(fid) if fid is not None else None
    if form is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    await _purpose_screen(callback, form)
    await callback.answer()


async def _apply_purpose(admin_id, form: dict, purpose: str, since: str | None) -> None:
    """Сменить назначение. Ничего не удаляется: ответы, оценки делегаций и заявки остаются."""
    fid = int(form["id"])
    await xdb.set_form_purpose(fid, purpose, since)
    if purpose != "delegations" and await purp.raw_delegation_form_id() == fid:
        await set_setting_by_admin(admin_id, "delegation_form_id", "")
        await set_setting_by_admin(admin_id, "delegation_armed_form_id", "")
    if form.get("mirror_mode") == "yandex_export":
        await xdb.set_form_mirror(fid, None, None)
        await xdb.set_form_mirror_mode(fid, "bot")
    if purpose == "registration" and not form.get("reg_q_fullname"):
        from services.delegations.delegations import guess_delegation_questions
        cols = await xdb.list_columns(fid)
        guess = guess_delegation_questions([(c["qkey"], c.get("label") or "") for c in cols])
        if guess.get("fullname"):
            await xdb.set_form_reg_fullname_q(fid, guess["fullname"])


@router.callback_query(F.data.startswith("extf_purpose_set:"))
async def extf_purpose_set(callback: types.CallbackQuery):
    form, rest = await _form_of(callback.data, 3)
    purpose = purp.CODES.get(rest[0]) if rest else None
    if form is None or purpose is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    if await purp.purpose_of(form) == purpose:
        await callback.answer("Это назначение уже выбрано")
        return
    fid = int(form["id"])
    quiet = (await xdb.count_answers(fid) == 0
             and await purp.raw_delegation_form_id() != fid
             and form.get("mirror_mode") != "yandex_export")
    if quiet:
        await _apply_purpose(callback.from_user.id, form, purpose, None)
        await render_form_card(callback, fid)
        await callback.answer(f"Назначение: {purp.PURPOSE_LABELS[purpose]}")
        return
    lines = await purp.change_effects(form, purpose)
    text = (f"🎯 <b>Сменить назначение формы «{_e(form['title'])}» на "
            f"«{purp.PURPOSE_LABELS[purpose]}»?</b>\n\nЧто изменится:\n"
            + "\n".join(f"• {line}" for line in lines))
    kb = _kb([[_btn("✅ Да, сменить назначение", f"extf_purpose_ok:{fid}:{rest[0]}")],
              [_btn("Отмена", f"extf_card:{fid}")]])
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_purpose_ok:"))
async def extf_purpose_ok(callback: types.CallbackQuery):
    form, rest = await _form_of(callback.data, 3)
    purpose = purp.CODES.get(rest[0]) if rest else None
    if form is None or purpose is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    since = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    await _apply_purpose(callback.from_user.id, form, purpose, since)
    await render_form_card(callback, int(form["id"]))
    await callback.answer(f"Назначение: {purp.PURPOSE_LABELS[purpose]}")


# ── настройки регистрации ────────────────────────────────────────────────────────────────

async def _reg_form(callback, form: dict | None) -> dict | None:
    if form is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return None
    if await purp.purpose_of(form) != "registration":
        await callback.answer(_NOT_REG, show_alert=True)
        return None
    return form


def _q_line(title: str, labels: dict, qkey) -> str:
    label = labels.get(qkey) if qkey else None
    return f"{title} — " + (f"вопрос «{_e(label)}»" if label else "не выбран")


async def _reg_screen(callback, form: dict) -> None:
    fid = int(form["id"])
    labels = {c["qkey"]: c.get("label") or c["qkey"] for c in await xdb.list_columns(fid)}
    approve = form.get("reg_approve") or "queue"
    lines = [f"📝 <b>Регистрация из формы «{_e(form['title'])}»</b>", "", _REG_HELP, "",
             _q_line("ФИО", labels, form.get("reg_q_fullname")),
             _q_line("Телефон", labels, form.get("key_phone_q")),
             _q_line("Ник в Telegram", labels, form.get("key_username_q"))]
    cities_on = await cities_module_on()
    city_text = None
    if cities_on:
        city_text = await city_label(form.get("reg_city") or default_city_code())
        lines.append(f"Город: {_e(city_text)}")
    lines.append(f"Одобрение: {_APPROVE_LABEL.get(approve, _APPROVE_LABEL['queue'])}")
    if not form.get("key_username_q"):
        lines.append("\n⚠️ Без вопроса про ник бот не узнает человека — выберите его.")
    rows = [[_btn("✏️ Вопрос для ФИО", f"extf_regq:{fid}:f")],
            [_btn("✏️ Вопрос для телефона", f"extf_regq:{fid}:p")],
            [_btn("✏️ Вопрос для ника", f"extf_regq:{fid}:u")]]
    if cities_on and len(await enabled_cities()) > 1:
        rows.append([_btn(_cut(f"🏙 Город: {city_text}"), f"extf_regcity:{fid}")])
    rows.append([_btn("⚡ Одобрение: сразу одобрять" if approve == "auto"
                      else "✅ Одобрение: в очередь заявок", f"extf_regappr:{fid}")])
    rows.append([_btn("✏️ Текст человеку: заявка получена", "settings_edit:ext_reg_pending_text")])
    rows.append(_to_card(fid))
    await _show(callback, "\n".join(lines), _kb(rows))


@router.callback_query(F.data.startswith("extf_reg:"))
async def extf_reg(callback: types.CallbackQuery):
    fid = _tail_id(callback.data)
    form = await _reg_form(callback, await xdb.get_form(fid) if fid is not None else None)
    if form is None:
        return
    await _reg_screen(callback, form)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_regq:"))
async def extf_regq(callback: types.CallbackQuery):
    raw, rest = await _form_of(callback.data, 3)
    form = await _reg_form(callback, raw if rest and rest[0] in _WHAT else None)
    if form is None:
        return
    columns = await xdb.list_columns(int(form["id"]))
    if not columns:
        await callback.answer(_NO_QUESTIONS, show_alert=True)
        return
    which = rest[0]
    rows = [[_btn(_cut(c.get("label") or c["qkey"]), f"extf_regqset:{form['id']}:{which}:{i}")]
            for i, c in enumerate(columns)]
    rows.append([_btn("Такого вопроса нет", f"extf_regqset:{form['id']}:{which}:none")])
    rows.append([_btn("← Назад", f"extf_reg:{form['id']}")])
    await _show(callback, f"Какой вопрос формы — {_WHAT[which]}?", _kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("extf_regqset:"))
async def extf_regqset(callback: types.CallbackQuery):
    raw, rest = await _form_of(callback.data, 4)
    form = await _reg_form(callback, raw if rest and rest[0] in _WHAT else None)
    if form is None:
        return
    fid, which = int(form["id"]), rest[0]
    if rest[1] == "none":
        qkey = None
    else:
        try:
            qkey = (await xdb.list_columns(fid))[int(rest[1])]["qkey"]
        except (ValueError, IndexError):
            await callback.answer(_STALE, show_alert=True)
            return
    if which == "f":
        await xdb.set_form_reg_fullname_q(fid, qkey)
    else:
        uq, pq = form.get("key_username_q"), form.get("key_phone_q")
        await xdb.set_form_keys(fid, qkey if which == "u" else uq, qkey if which == "p" else pq)
    await _reg_screen(callback, await xdb.get_form(fid))
    await callback.answer("Сохранено")


@router.callback_query(F.data.startswith("extf_regcity:"))
async def extf_regcity(callback: types.CallbackQuery):
    fid = _tail_id(callback.data)
    form = await _reg_form(callback, await xdb.get_form(fid) if fid is not None else None)
    if form is None:
        return
    cities = await enabled_cities()
    rows = [[_btn(_cut(await city_label(c["code"])), f"extf_regcityset:{fid}:{i}")]
            for i, c in enumerate(cities)]
    rows.append([_btn("← Назад", f"extf_reg:{fid}")])
    await _show(callback, "🏙 В какой город записывать заявки из этой формы?", _kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("extf_regcityset:"))
async def extf_regcityset(callback: types.CallbackQuery):
    raw, rest = await _form_of(callback.data, 3)
    form = await _reg_form(callback, raw)
    if form is None:
        return
    cities = await enabled_cities()
    try:
        code = cities[int(rest[0])]["code"]
    except (ValueError, IndexError):
        await callback.answer("Список городов устарел — откройте выбор ещё раз", show_alert=True)
        return
    await xdb.set_form_reg_city(int(form["id"]), code)
    await _reg_screen(callback, await xdb.get_form(int(form["id"])))
    await callback.answer(f"Город: {await city_label(code)}")


@router.callback_query(F.data.startswith("extf_regappr:"))
async def extf_regappr(callback: types.CallbackQuery):
    fid = _tail_id(callback.data)
    form = await _reg_form(callback, await xdb.get_form(fid) if fid is not None else None)
    if form is None:
        return
    new = "queue" if (form.get("reg_approve") or "queue") == "auto" else "auto"
    await xdb.set_form_reg_approve(fid, new)
    await _reg_screen(callback, await xdb.get_form(fid))
    await callback.answer(f"Одобрение: {_APPROVE_LABEL[new]}")
