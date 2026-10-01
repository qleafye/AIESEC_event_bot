"""Мастер подключения формы: «➕ Подключить Яндекс Форму» / «➕ Подключить Google Форму».

Шов на общий `handlers.admin.router` (своего Router нет, декораторы в одну строку). Права —
в handlers/admin_caps.py: все `extf_*` и состояние ExtFormConnect — `settings`.

Шаги: ссылка -> проверка доступа -> ключевые вопросы (по ним ответ привязывается к делегату) ->
создание формы -> вкладка и адрес приёмника (экраны handlers/admin_ext_forms_setup.py) ->
старые ответы. Из ссылки берётся только id формы / таблицы (+ gid), хосты запросов фиксированы.
Ход мастера лежит в FSM; после рестарта бота нажатие «✅ Верно» честно просит начать заново."""
import logging
import secrets

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database import ext_forms_db as xdb
from handlers.admin import router
from handlers.admin_ext_forms import _e, _show, render_form_card
from handlers.states import ExtFormConnect
from keyboards.builders import get_cancel_kb
from services import ext_forms_google as gg
from services import ext_forms_yandex as yx
from services.background import spawn
from services.ext_forms_match import guess_key_questions
from services.ext_forms_parse import parse_yandex_questions
from services.ext_forms_yandex_sync import backfill_form, ensure_fresh_token

logger = logging.getLogger(__name__)

_LABEL_LIMIT = 40
_RESTART = "Мастер прервался — начните подключение заново"
_YANDEX_EXAMPLE = "https://forms.yandex.ru/cloud/6a94ad5c1f1eb5c9649c12bd/"
_LOGIN_BTN = "🔑 Войти через Яндекс"

_YANDEX_REASONS = {
    "unauthorized": "У аккаунта, под которым вы вошли в Яндекс, нет доступа к этой форме — "
                    "войдите под тем, кто её создал",
    "forbidden": "У аккаунта, под которым вы вошли в Яндекс, нет доступа к этой форме — "
                 "войдите под тем, кто её создал",
    "not_found": "Форма не найдена — проверьте ссылку",
    "upstream_unavailable": "Яндекс сейчас не отвечает — попробуйте через пару минут",
}


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _to_list() -> list[InlineKeyboardButton]:
    return [_btn("📝 Внешние формы", "admin_ext_forms")]


def _cut(text: str) -> str:
    return text if len(text) <= _LABEL_LIMIT else text[:_LABEL_LIMIT - 1] + "…"


async def _reply(target, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    msg = getattr(target, "message", None) or target
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ---------- вход в мастер ----------

@router.callback_query(F.data == "extf_add:yandex")
async def extf_add_yandex(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    conn = await xdb.get_yandex_connection()
    if conn is None or conn.get("status") != "ok":
        head = ("Сначала подключите доступ: «🔑 Войти через Яндекс»." if conn is None
                else "Доступ к Яндекс Формам потерян — войдите через Яндекс заново.")
        await _show(callback, head, _kb([[_btn(_LOGIN_BTN, "extf_oauth")], _to_list()]), edit=True)
        await callback.answer()
        return
    await state.set_state(ExtFormConnect.link)
    await state.update_data(platform="yandex")
    await _show(callback, "Пришлите ссылку на форму, например\n"
                          f"<code>{_YANDEX_EXAMPLE}</code>", _kb([_to_list()]), edit=True)
    await callback.message.answer("Жду ссылку. Передумали — нажмите «Отмена».",
                                  reply_markup=get_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "extf_add:google")
async def extf_add_google(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    email = gg.service_account_email()
    if not email:
        await _show(callback, "Google-таблицы не подключены на сервере бота — подключить "
                              "Google Форму пока нельзя.", _kb([_to_list()]), edit=True)
        await callback.answer()
        return
    await state.set_state(ExtFormConnect.link)
    await state.update_data(platform="google")
    await _show(
        callback,
        "Как подключить Google Форму:\n"
        "1. Откройте таблицу с ответами формы (в форме: «Ответы» → «Посмотреть в Таблицах»).\n"
        "2. «Настройки доступа» → добавьте адрес\n"
        f"<code>{_e(email)}</code>\n"
        "с ролью «Читатель».\n"
        "3. Пришлите сюда ссылку на эту таблицу.", _kb([_to_list()]), edit=True)
    await callback.message.answer("Жду ссылку. Передумали — нажмите «Отмена».",
                                  reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(StateFilter(ExtFormConnect), F.text == "Отмена")
async def extf_connect_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Ничего не подключено.", reply_markup=ReplyKeyboardRemove())


# ---------- приём ссылки ----------

@router.message(ExtFormConnect.link, F.text)
async def extf_connect_link(message: types.Message, state: FSMContext):
    platform = (await state.get_data()).get("platform")
    if platform == "yandex":
        await _link_yandex(message, state)
    elif platform == "google":
        await _link_google(message, state)
    else:
        await state.clear()
        await message.answer(_RESTART, reply_markup=_kb([_to_list()]))


async def _open_existing(target, form: dict, state: FSMContext) -> None:
    await state.clear()
    msg = getattr(target, "message", None) or target
    await msg.answer("Эта форма уже подключена — вот её карточка.",
                     reply_markup=ReplyKeyboardRemove())
    await render_form_card(msg, form["id"], edit=False)


async def _link_yandex(message: types.Message, state: FSMContext) -> None:
    form_id = yx.parse_form_id(message.text)
    if not form_id:
        await message.answer("Не нашёл в ссылке номер формы — скопируйте адрес формы из браузера "
                             f"целиком, например {_YANDEX_EXAMPLE}")
        return
    conn = await xdb.get_yandex_connection()
    conn = await ensure_fresh_token(conn) if conn else None
    if conn is None:
        await message.answer("Доступ к Яндекс Формам потерян — войдите через Яндекс заново.",
                             reply_markup=_kb([[_btn(_LOGIN_BTN, "extf_oauth")]]))
        return
    try:
        survey = await yx.get_survey(conn, form_id)
    except yx.YandexApiError as e:
        await message.answer(_YANDEX_REASONS.get(
            e.reason, "Не получилось проверить форму — попробуйте ещё раз чуть позже."))
        return
    existing = await xdb.get_form_by_external("yandex", form_id)
    if existing:
        await _open_existing(message, existing, state)
        return
    try:
        questions = parse_yandex_questions(await yx.get_questions(conn, form_id))
    except yx.YandexApiError:
        questions = []  # ключи можно будет не задавать
    title = str(survey.get("name") or "").strip() or "Яндекс Форма"
    await _keys_step(message, state, platform="yandex", external_id=form_id, gid=None,
                     title=title, questions=questions)


# ---------- Google ----------

def _google_questions(values: list[list[str]]) -> list[tuple[str, str]]:
    """Заголовки первой строки как в rows_to_answers: повтор получает « (2)», отметки времени нет."""
    if not values:
        return []
    seen: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for raw in values[0]:
        h = str(raw).strip()
        n = seen.get(h, 0) + 1
        seen[h] = n
        if n == 1 and h.lower() in gg._TS_HEADERS:
            continue
        title = h if n == 1 else f"{h} ({n})"
        out.append((title, title))
    return out


async def _link_google(message: types.Message, state: FSMContext) -> None:
    parsed = gg.parse_sheet_url(message.text)
    if not parsed:
        await message.answer("Не понял ссылку — пришлите ссылку на таблицу ответов вида "
                             "https://docs.google.com/spreadsheets/d/…")
        return
    sheet_id, gid = parsed
    try:
        doc_title, tabs = await gg.list_tabs(sheet_id)
        if not tabs:
            await message.answer("В таблице нет ни одной вкладки — проверьте ссылку.")
            return
        if gid is None and len(tabs) > 1:
            await state.update_data(g_sheet=sheet_id, g_doc=doc_title,
                                    g_tabs=[[t[0], t[1]] for t in tabs])
            rows = [[_btn(_cut(t[1]), f"extf_gtab:{i}")] for i, t in enumerate(tabs)]
            await message.answer("В таблице несколько вкладок. На какой лежат ответы формы?",
                                 reply_markup=_kb(rows))
            return
        if gid is None:
            gid = tabs[0][0]
        tab_title = next((t[1] for t in tabs if t[0] == gid), None)
        if tab_title is None:
            await message.answer("Вкладка из ссылки не найдена — откройте нужную вкладку в "
                                 "таблице и скопируйте ссылку заново.")
            return
        await _google_checked(message, state, sheet_id, gid, doc_title, tab_title, len(tabs) > 1)
    except gg.GoogleFormError as e:
        await message.answer(e.human)


async def _google_checked(target, state: FSMContext, sheet_id: str, gid: int, doc_title: str,
                          tab_title: str, multi: bool) -> None:
    existing = await xdb.get_form_by_external("google", sheet_id, gid)
    if existing:
        await _open_existing(target, existing, state)
        return
    values = await gg.read_values(sheet_id, gid)
    _, has_ts = gg.rows_to_answers(values)
    title = f"{doc_title} — {tab_title}" if multi else doc_title
    questions = _google_questions(values)
    if not has_ts:
        await state.update_data(platform="google", external_id=sheet_id, gid=gid, title=title,
                                questions=[list(q) for q in questions])
        await _reply(target, "В первой колонке нет «Отметки времени» — это точно таблица ответов "
                             "формы?", _kb([[_btn("Да, продолжить", "extf_gwarn_ok")],
                                            [_btn("Отмена", "extf_gwarn_no")]]))
        return
    await _keys_step(target, state, platform="google", external_id=sheet_id, gid=gid,
                     title=title, questions=questions)


@router.callback_query(F.data.startswith("extf_gtab:"))
async def extf_gtab(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tabs = data.get("g_tabs") or []
    try:
        idx = int(callback.data.split(":", 1)[1])
    except ValueError:
        idx = -1
    if not data.get("g_sheet") or not 0 <= idx < len(tabs):
        await callback.answer(_RESTART, show_alert=True)
        return
    gid, tab_title = tabs[idx]
    try:
        await _google_checked(callback, state, data["g_sheet"], gid, data.get("g_doc") or "",
                              tab_title, True)
    except gg.GoogleFormError as e:
        await _reply(callback, e.human)
    await callback.answer()


@router.callback_query(F.data == "extf_gwarn_ok")
async def extf_gwarn_ok(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("external_id"):
        await callback.answer(_RESTART, show_alert=True)
        return
    await _keys_step(callback, state, platform="google", external_id=data["external_id"],
                     gid=data.get("gid"), title=data.get("title") or "Google Форма",
                     questions=[tuple(q) for q in data.get("questions") or []])
    await callback.answer()


@router.callback_query(F.data == "extf_gwarn_no")
async def extf_gwarn_no(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _show(callback, "Отменено. Ничего не подключено.", _kb([_to_list()]), edit=True)
    await callback.answer()


# ---------- ключевые вопросы ----------

def _q_label(questions: list, qkey: str | None) -> str | None:
    for k, label in questions:
        if k == qkey:
            return label
    return None


def _key_line(title: str, questions: list, qkey: str | None) -> str:
    label = _q_label(questions, qkey)
    return f"{title} — " + (f"вопрос «{_e(label)}»" if label else "не выбран")


async def _keys_step(target, state: FSMContext, *, platform: str, external_id: str, gid,
                     title: str, questions: list) -> None:
    uq, pq = guess_key_questions(list(questions))
    await state.set_state(None)
    await state.update_data(platform=platform, external_id=external_id, gid=gid, title=title,
                            questions=[list(q) for q in questions], uq=uq, pq=pq)
    await _reply(target, "Ссылка принята.", ReplyKeyboardRemove())
    await _show_keys(target, state, edit=False)


async def _show_keys(target, state: FSMContext, *, edit: bool) -> None:
    d = await state.get_data()
    questions = d.get("questions") or []
    text = (f"📋 <b>{_e(d.get('title'))}</b>\n\nКак найти человека среди делегатов:\n"
            f"{_key_line('Ник в Telegram', questions, d.get('uq'))}\n"
            f"{_key_line('Телефон', questions, d.get('pq'))}\n\n"
            "Остальные вопросы формы никак не размечаются — ответы сохранятся целиком.")
    if not d.get("uq") and not d.get("pq"):
        text += ("\n\n⚠️ Без ключей анкеты не будут привязаны к делегатам. "
                 "Можно продолжить и так, но лучше выбрать вопросы.")
    rows = [[_btn("✅ Верно", "extf_keys_ok")]]
    if questions:
        rows.append([_btn("✏️ Другой вопрос для ника", "extf_key:u")])
        rows.append([_btn("✏️ Другой вопрос для телефона", "extf_key:p")])
    await _show(target, text, _kb(rows), edit=edit)


@router.callback_query(F.data.in_({"extf_key:u", "extf_key:p"}))
async def extf_key(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    questions = d.get("questions") or []
    if not d.get("external_id"):
        await callback.answer(_RESTART, show_alert=True)
        return
    which = callback.data.rsplit(":", 1)[1]
    rows = [[_btn(_cut(label or key), f"extf_keyset:{which}:{i}")]
            for i, (key, label) in enumerate(questions)]
    rows.append([_btn("Такого вопроса нет", f"extf_keyset:{which}:none")])
    what = "ник в Telegram" if which == "u" else "телефон"
    await _show(callback, f"Какой вопрос формы — {what}?", _kb(rows), edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_keyset:"))
async def extf_keyset(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    questions = d.get("questions") or []
    parts = callback.data.split(":")
    if not d.get("external_id") or len(parts) != 3 or parts[1] not in ("u", "p"):
        await callback.answer(_RESTART, show_alert=True)
        return
    if parts[2] == "none":
        qkey = None
    else:
        try:
            qkey = questions[int(parts[2])][0]
        except (ValueError, IndexError):
            await callback.answer("Список вопросов устарел — откройте выбор ещё раз", show_alert=True)
            return
    await state.update_data(**{parts[1] + "q": qkey})
    await _show_keys(callback, state, edit=True)
    await callback.answer()


# ---------- создание формы ----------

def _next_steps_rows(form_id: int, *, yandex: bool) -> list[list[InlineKeyboardButton]]:
    rows = [[_btn("📋 Выбрать вкладку", f"extf_tab:{form_id}")]]
    if yandex:
        rows.append([_btn("🔗 Адрес и инструкция", f"extf_hook:{form_id}")])
    rows.append([_btn("📄 Карточка формы", f"extf_card:{form_id}")])
    return rows


@router.callback_query(F.data == "extf_keys_ok")
async def extf_keys_ok(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    platform = d.get("platform")
    if not d.get("external_id") or platform not in ("yandex", "google"):
        await state.clear()
        await _show(callback, _RESTART, _kb([_to_list()]), edit=True)
        await callback.answer()
        return
    existing = await xdb.get_form_by_external(platform, d["external_id"], d.get("gid"))
    if existing:
        await _open_existing(callback, existing, state)
        await callback.answer()
        return
    await state.clear()
    by = callback.from_user.id
    if platform == "yandex":
        conn = await xdb.get_yandex_connection()
        form_id = await xdb.create_form(
            platform="yandex", connection_id=conn["id"] if conn else None,
            external_id=d["external_id"], title=d["title"], secret=secrets.token_urlsafe(24),
            key_username_q=d.get("uq"), key_phone_q=d.get("pq"), created_by=by)
        await _show(callback,
                    "✅ Форма подключена. Подтягиваю старые ответы — они появятся в течение пары "
                    "минут, я напишу, сколько нашлось.\n\nОсталось два шага:\n"
                    "1) выберите вкладку для копии ответов;\n"
                    "2) вставьте адрес в «Интеграции» Яндекс Форм.",
                    _kb(_next_steps_rows(form_id, yandex=True)), edit=True)
        spawn(_backfill_and_report(callback.message, form_id))
    else:
        form_id = await xdb.create_form(
            platform="google", external_id=d["external_id"], gsheet_gid=d.get("gid"),
            title=d["title"], secret=None, key_username_q=d.get("uq"),
            key_phone_q=d.get("pq"), created_by=by)
        form = await xdb.get_form(form_id)
        try:
            added = await gg.sync_google_form(form)
            tail = f"подтянуто ответов: {added}"
        except Exception as e:  # noqa: BLE001 — форма создана, сверка повторится по расписанию
            logger.warning("ext_forms: первая синхронизация формы %s: %s", form_id, type(e).__name__)
            tail = "старые ответы подтянутся при следующей сверке"
        await _show(callback, f"✅ Форма подключена, {tail}.\nОсталось выбрать вкладку для копии.",
                    _kb(_next_steps_rows(form_id, yandex=False)), edit=True)
    await callback.answer()


async def _backfill_and_report(msg, form_id: int) -> None:
    """Старых ответов могут быть сотни — хендлер не держим, итог приходит отдельным сообщением."""
    kb = None
    try:
        n = await backfill_form(form_id)
        text = (f"📥 Подтягиваю {n} ответов, пришедших раньше, — они появятся в форме в течение "
                "пары минут." if n else "📥 Старых ответов у формы нет — новые будут приходить сами.")
    except yx.YandexApiError as e:
        if e.reason == "unauthorized":
            text = ("Форма подключена, но Яндекс не пустил за старыми ответами — войдите через "
                    "Яндекс заново, после этого они подтянутся сами.")
            kb = _kb([[_btn(_LOGIN_BTN, "extf_oauth")]])
        else:
            text = "Старые ответы подтянутся при следующей сверке."
    except Exception as e:  # noqa: BLE001 — форма уже создана, мастер не падает
        logger.warning("ext_forms: бэкфилл формы %s: %s", form_id, type(e).__name__)
        text = "Старые ответы подтянутся при следующей сверке."
    try:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:  # noqa: BLE001
        logger.warning("ext_forms: итог бэкфилла формы %s не отправлен: %s", form_id, type(e).__name__)
