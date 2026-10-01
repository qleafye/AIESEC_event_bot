"""Раздел админки «📝 Внешние формы»: список форм (Яндекс/Google), карточка формы, пауза,
отключение, удаление собранного с подтверждением, тумблер уведомлений и просмотр ответов
делегата с карточки /find.

Шов на общий `handlers.admin.router`: своего Router нет, каждый декоратор — в одну строку
(инвариант cap-теста), `admin_sections` импортируется лениво (цикл на уровне модуля).
Права — в handlers/admin_caps.py: раздел и все `extf_*` — `settings`, просмотр ответов
делегата `extf_view:*` — `moderate_reg` (как у /find). Мастер подключения (ссылка, вход через
Яндекс, ключи приложения, вкладка таблицы, адрес для Яндекса) живёт в соседних швах: здесь
для них только кнопки-входы.

Все значения из чужих форм (названия, вопросы, ответы) идут в HTML-сообщения только через
`html.escape`. ID формы, секрет приёмника и ключи вопросов менеджеру не показываются."""
import html as html_module

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import ext_forms_db as xdb
from handlers.admin import router
from services.timeutil import msk_now

FORMS_PAGE = 8
ANSWER_TEXT_MAX = 3500

_PLATFORM_LABEL = {"yandex": "Яндекс Форма", "google": "Google Форма"}
_STATUS_WORDS = {
    "active": "принимает ответы",
    "paused": "на паузе",
    "disabled": "отключена",
}


def _e(value) -> str:
    return html_module.escape(str(value if value is not None else ""))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _back(callback_data: str = "admin_ext_forms") -> InlineKeyboardButton:
    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    return back_button(callback_data)


def _tail_id(data: str) -> int | None:
    try:
        return int(str(data).split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _message_of(target):
    return getattr(target, "message", None) or target


async def _show(target, text: str, kb: InlineKeyboardMarkup, *, edit: bool = True) -> None:
    msg = _message_of(target)
    if edit and hasattr(msg, "edit_text"):
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── список ────────────────────────────────────────────────────────────────────────────────

async def _access_line() -> str:
    conn = await xdb.get_yandex_connection()
    if conn is None:
        return "Доступ к Яндекс Формам не подключён"
    if conn.get("status") == "ok":
        return "✅ Доступ к Яндекс Формам подключён"
    return "⚠️ Доступ к Яндекс Формам потерян — войдите заново"


def _form_button_text(f: dict) -> str:
    text = f"{f['title']} · {f['total']} ответов"
    if f.get("unmatched"):
        text += f" · ⚠️ {f['unmatched']} без делегата"
    if f.get("status") == "paused":
        text += " · ⏸"
    elif f.get("status") == "disabled":
        text += " · ⛔"
    return text[:60]


async def render_forms_screen(offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    forms = await xdb.list_forms()
    lines = ["📝 <b>Внешние формы</b>", await _access_line()]
    rows: list[list[InlineKeyboardButton]] = []
    if not forms:
        lines.append("\nПока ни одной формы. Подключите форму — ответы будут сами попадать "
                     "в карточки делегатов.")
    else:
        lines.append(f"\nВсего форм: {len(forms)}. «Без делегата» — ответы, которых не удалось "
                     "сопоставить с анкетой в боте.")
        offset = max(0, min(offset, max(0, len(forms) - 1)))
        for f in forms[offset:offset + FORMS_PAGE]:
            rows.append([_btn(_form_button_text(f), f"extf_card:{f['id']}")])
        nav: list[InlineKeyboardButton] = []
        if offset > 0:
            nav.append(_btn("⬅️", f"extf_p:{max(0, offset - FORMS_PAGE)}"))
        if offset + FORMS_PAGE < len(forms):
            nav.append(_btn("➡️", f"extf_p:{offset + FORMS_PAGE}"))
        if nav:
            rows.append(nav)
    rows.append([_btn("➕ Подключить Яндекс Форму", "extf_add:yandex")])
    rows.append([_btn("➕ Подключить Google Форму", "extf_add:google")])
    rows.append([_btn("🔑 Войти через Яндекс", "extf_oauth")])
    rows.append([_btn("🔑 Ключи приложения Яндекса", "extf_appkeys")])
    rows.append([_back("admin_ext_forms")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_ext_forms")
async def admin_ext_forms(callback: types.CallbackQuery):
    text, kb = await render_forms_screen(0)
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_p:"))
async def extf_page(callback: types.CallbackQuery):
    offset = _tail_id(callback.data) or 0
    text, kb = await render_forms_screen(offset)
    await _show(callback, text, kb)
    await callback.answer()


# ── карточка ──────────────────────────────────────────────────────────────────────────────

async def _form_with_stats(form_id: int) -> dict | None:
    for f in await xdb.list_forms():
        if f["id"] == form_id:
            return f
    return None


def _card_text(f: dict) -> str:
    status = _STATUS_WORDS.get(f.get("status"), "—")
    lines = [
        f"📝 <b>{_e(f['title'])}</b>",
        _PLATFORM_LABEL.get(f["platform"], "Форма"),
        f"Состояние: {status}",
        f"Ответов: {f['total']}",
        f"Не сопоставлено с делегатом: {f['unmatched']}",
        f"Последний ответ: {_e(f.get('last_answer_at') or 'ещё не было')}",
    ]
    if f.get("last_sync_at"):
        lines.append(f"Последняя сверка: {_e(f['last_sync_at'])}")
    if f.get("sync_error"):
        lines.append(f"⚠️ Ошибка сверки: {_e(f['sync_error'])}")
    lines.append(f"Вкладка таблицы: {_e(f['mirror_tab']) if f.get('mirror_tab') else 'не выбрана'}")
    if f.get("mirror_error"):
        lines.append(f"⚠️ Вкладка: {_e(f['mirror_error'])}")
    lines.append("Уведомления о новых ответах: " + ("включены" if f.get("notify") else "выключены"))
    return "\n".join(lines)


def _card_kb(f: dict) -> InlineKeyboardMarkup:
    fid = f["id"]
    rows: list[list[InlineKeyboardButton]] = []
    if f["status"] == "active":
        rows.append([_btn("⏸ Поставить на паузу", f"extf_pause:{fid}")])
    elif f["status"] == "paused":
        rows.append([_btn("▶️ Возобновить", f"extf_resume:{fid}")])
    notify_text = "🔔 Уведомления о новых ответах: " + ("вкл" if f.get("notify") else "выкл")
    rows.append([_btn(notify_text, f"extf_notify:{fid}")])
    rows.append([_btn("📋 Вкладка таблицы", f"extf_tab:{fid}")])
    if f["platform"] == "yandex":
        rows.append([_btn("🔗 Адрес и инструкция", f"extf_hook:{fid}")])
    if f["status"] != "disabled":
        rows.append([_btn("⛔ Отключить", f"extf_disable:{fid}")])
    if f["total"] > 0:
        rows.append([_btn("🗑 Удалить собранное", f"extf_purge:{fid}")])
    rows.append([_back("admin_ext_forms")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_form_card(message_or_callback, form_id: int, *, edit: bool = True) -> bool:
    """Рисует карточку формы; False — формы больше нет. Экспорт для мастера подключения."""
    f = await _form_with_stats(form_id)
    if f is None:
        return False
    await _show(message_or_callback, _card_text(f), _card_kb(f), edit=edit)
    return True


async def _open_card(callback: types.CallbackQuery, answer_text: str | None = None) -> int | None:
    form_id = _tail_id(callback.data)
    if form_id is None or not await render_form_card(callback, form_id):
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return None
    await callback.answer(answer_text)
    return form_id


@router.callback_query(F.data.startswith("extf_card:"))
async def extf_card(callback: types.CallbackQuery):
    await _open_card(callback)


@router.callback_query(F.data.startswith("extf_pause:"))
async def extf_pause(callback: types.CallbackQuery):
    form_id = _tail_id(callback.data)
    f = await xdb.get_form(form_id) if form_id is not None else None
    if f is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    if f["status"] == "active":
        await xdb.set_form_status(form_id, "paused")
    await _open_card(callback, "Приём ответов на паузе.")


@router.callback_query(F.data.startswith("extf_resume:"))
async def extf_resume(callback: types.CallbackQuery):
    form_id = _tail_id(callback.data)
    f = await xdb.get_form(form_id) if form_id is not None else None
    if f is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    # Отключённую форму возобновлять нельзя: интеграцию в самой форме менеджер уже убрал.
    if f["status"] == "paused":
        await xdb.set_form_status(form_id, "active")
    await _open_card(callback, "Приём ответов возобновлён.")


@router.callback_query(F.data.startswith("extf_notify:"))
async def extf_notify(callback: types.CallbackQuery):
    form_id = _tail_id(callback.data)
    f = await xdb.get_form(form_id) if form_id is not None else None
    if f is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    if f.get("notify"):
        await xdb.set_form_notify(form_id, False)
        note = "Уведомления выключены."
    else:
        # notified_at = «сейчас»: иначе первая же рассылка принесла бы пачку старых ответов.
        await xdb.set_form_notified(form_id, msk_now().strftime("%Y-%m-%d %H:%M:%S"))
        await xdb.set_form_notify(form_id, True)
        note = "Уведомления включены: сообщу только о новых ответах."
    await _open_card(callback, note)


# ── отключение ────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("extf_disable:"))
async def extf_disable(callback: types.CallbackQuery):
    form_id = _tail_id(callback.data)
    f = await xdb.get_form(form_id) if form_id is not None else None
    if f is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    n = await xdb.count_answers(form_id)
    text = (
        f"⛔ <b>Отключить форму «{_e(f['title'])}»?</b>\n\n"
        f"Новые ответы перестанут приниматься. Собранные {n} анкет останутся в боте и в таблице."
    )
    if f["platform"] == "yandex":
        text += ("\n\nУдалите интеграцию и в самой форме: откройте форму в Яндекс Формах → "
                 "«Интеграции» → уберите адрес бота. Иначе Яндекс продолжит присылать ответы.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("⛔ Да, отключить", f"extf_disable_ok:{form_id}")],
        [_btn("Отмена", f"extf_card:{form_id}")],
    ])
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_disable_ok:"))
async def extf_disable_ok(callback: types.CallbackQuery):
    form_id = _tail_id(callback.data)
    if form_id is None or await xdb.get_form(form_id) is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    await xdb.set_form_status(form_id, "disabled")
    await _open_card(callback, "Форма отключена. Собранное осталось.")


# ── удаление собранного ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("extf_purge:"))
async def extf_purge(callback: types.CallbackQuery):
    form_id = _tail_id(callback.data)
    f = await xdb.get_form(form_id) if form_id is not None else None
    if f is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    n = await xdb.count_answers(form_id)
    text = (
        f"🗑 <b>Удалить собранное по форме «{_e(f['title'])}»?</b>\n\n"
        f"Пропадут {n} анкет из бота. Строки во вкладке таблицы останутся. "
        "Отменить нельзя."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🗑 Да, удалить", f"extf_purge_ok:{form_id}")],
        [_btn("Отмена", f"extf_card:{form_id}")],
    ])
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_purge_ok:"))
async def extf_purge_ok(callback: types.CallbackQuery):
    form_id = _tail_id(callback.data)
    if form_id is None or await xdb.get_form(form_id) is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    deleted = await xdb.delete_form_answers(form_id)
    await _open_card(callback, f"Удалено {deleted} анкет")


# ── ответы делегата ───────────────────────────────────────────────────────────────────────

def _answer_text(item: dict, number: int, total: int) -> str:
    when = item.get("answered_at") or item.get("received_at") or ""
    head = f"📝 <b>{_e(item.get('title'))}</b> · {_e(when)}"
    if total > 1:
        head += f"\nАнкета {number} из {total}"
    body_lines = []
    for q in item.get("payload") or []:
        if not isinstance(q, dict):
            continue
        body_lines.append(f"{_e(q.get('label'))}: {_e(q.get('value'))}")
    body = "\n".join(body_lines)
    room = ANSWER_TEXT_MAX - len(head) - 2
    if len(body) > room:
        body = body[:max(0, room - 1)] + "…"
    return head + "\n\n" + body


@router.callback_query(F.data.startswith("extf_view:"))
async def extf_view(callback: types.CallbackQuery):
    parts = str(callback.data).split(":")
    try:
        tid = int(parts[1])
        offset = int(parts[2]) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        await callback.answer("Не понял, чьи ответы показать.", show_alert=True)
        return
    items = await xdb.answers_for_user(tid)
    if not items:
        await callback.answer("У делегата нет ответов во внешних формах", show_alert=True)
        return
    offset = max(0, min(offset, len(items) - 1))
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(_btn("⬅️", f"extf_view:{tid}:{offset - 1}"))
    if offset + 1 < len(items):
        nav.append(_btn("➡️", f"extf_view:{tid}:{offset + 1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])
    text = _answer_text(items[offset], offset + 1, len(items))
    msg = callback.message
    # С карточки /find ответы приходят отдельным сообщением, чтобы не затирать карточку;
    # листание правит это же сообщение (offset в callback_data).
    if len(parts) > 2:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

# Вход через Яндекс и ключи приложения (шов).
from handlers import admin_ext_forms_oauth  # noqa: E402,F401
from handlers import admin_ext_forms_setup  # noqa: E402,F401
