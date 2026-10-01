"""Настройка подключённой формы: «📋 Вкладка таблицы» и «🔗 Адрес и инструкция».

Шов на общий `handlers.admin.router` (своего Router нет, декораторы в одну строку). Права —
в handlers/admin_caps.py: все `extf_*` — `settings`. Экраны зовёт и карточка формы, и мастер
подключения (show_tab_picker / show_hook_info).

Вкладку бот сам не заводит: новая создаётся только нажатием «➕ Новая вкладка» (D-13), иначе
выбирается существующая из реального списка листа (по индексу, список лежит в FSM)."""
import secrets

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import ext_forms_db as xdb
from handlers.admin import router
from handlers.admin_ext_forms import _e, _show, render_form_card
from services import sheets
from services.ext_forms_google import list_tabs as google_list_tabs
from services.ext_forms_mirror import create_mirror_tab

_TAB_BTN_LIMIT = 40
_STALE = "Список вкладок устарел — откройте выбор ещё раз"
_NO_SHEET = "Таблица события не подключена — ответы сохранятся только в боте."


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _back_row(form_id: int) -> list[InlineKeyboardButton]:
    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    return [back_button(f"extf_card:{form_id}")]


def _tail(data: str, parts: int = 1) -> list[int] | None:
    try:
        return [int(x) for x in str(data).split(":")[1:1 + parts]] if parts else None
    except ValueError:
        return None


def _cut(title: str) -> str:
    return title if len(title) <= _TAB_BTN_LIMIT else title[:_TAB_BTN_LIMIT - 1] + "…"


# ---------- выбор вкладки ----------

async def protected_tab_titles(form: dict) -> set[str]:
    """Вкладки, в которые зеркало писать нельзя: основная и служебные вкладки бота, вкладки
    других форм и таблица ответов самой Google-формы (иначе зеркало читалось бы как ответы)."""
    from services.sheet_reconcile import _known_non_delegate_tab_titles
    from settings_ops import current_tab_titles

    hidden: set[str] = {t.title for t in await current_tab_titles()}
    try:
        hidden |= await _known_non_delegate_tab_titles()
    except Exception:  # noqa: BLE001 — список вкладок вторичен, выбор не должен падать
        pass
    for other in await xdb.list_forms():
        if other["id"] != form["id"] and other.get("mirror_tab"):
            hidden.add(other["mirror_tab"])
    if form.get("platform") == "google" and form.get("external_id") == config.GOOGLE_SHEET_ID:
        try:
            _, tabs = await google_list_tabs(form["external_id"])
            gid = form.get("gsheet_gid")
            src = next((t for g, t in tabs if g == gid), None) if gid is not None else (
                tabs[0][1] if tabs else None)
            if src:
                hidden.add(src)
        except Exception:  # noqa: BLE001
            pass
    return hidden

async def show_tab_picker(message, form_id: int, state: FSMContext, *, edit: bool = True) -> None:
    form = await xdb.get_form(form_id)
    if form is None:
        await _show(message, "Форма не найдена — вернитесь к списку форм.",
                    InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ К списку", "admin_ext_forms")]]),
                    edit=edit)
        return
    titles = await sheets.list_worksheet_titles()
    rows: list[list[InlineKeyboardButton]] = []
    if titles is None:
        await state.update_data(extf_tabs=[])
        text = (f"📋 <b>Вкладка таблицы для «{_e(form['title'])}»</b>\n\n{_NO_SHEET}")
    else:
        hidden = await protected_tab_titles(form)
        titles = [t for t in titles if t not in hidden]
        await state.update_data(extf_tabs=list(titles))
        text = (f"📋 <b>Вкладка таблицы для «{_e(form['title'])}»</b>\n\n"
                "Куда складывать копию ответов? Лучше завести новую вкладку. Из существующих "
                "подойдёт только пустая — в чужую вкладку с данными бот писать не будет. "
                "Служебные вкладки бота и таблицы ответов в списке не показываются.")
        rows.append([_btn(f"➕ Новая вкладка «{_cut(form['title'])}»", f"extf_tabnew:{form_id}")])
        for idx, title in enumerate(titles):
            rows.append([_btn(_cut(title), f"extf_tabpick:{form_id}:{idx}")])
    rows.append([_btn("Без копии в таблицу", f"extf_tabnone:{form_id}")])
    rows.append(_back_row(form_id))
    await _show(message, text, InlineKeyboardMarkup(inline_keyboard=rows), edit=edit)


@router.callback_query(F.data.startswith("extf_tab:"))
async def extf_tab(callback: types.CallbackQuery, state: FSMContext):
    ids = _tail(callback.data)
    if not ids:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    await show_tab_picker(callback, ids[0], state, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_tabnew:"))
async def extf_tabnew(callback: types.CallbackQuery, state: FSMContext):
    ids = _tail(callback.data)
    form = await xdb.get_form(ids[0]) if ids else None
    if form is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    result = await create_mirror_tab(form["id"], form["title"])
    if result == "exists":
        await callback.answer("Вкладка с таким названием уже есть — выберите её в списке",
                              show_alert=True)
        await show_tab_picker(callback, form["id"], state, edit=True)
        return
    if result != "ok":
        await callback.answer("Не получилось создать вкладку — таблица сейчас недоступна. "
                              "Попробуйте позже или выберите «Без копии в таблицу».",
                              show_alert=True)
        return
    await render_form_card(callback, form["id"], edit=True)
    await callback.answer("Вкладка создана")


@router.callback_query(F.data.startswith("extf_tabpick:"))
async def extf_tabpick(callback: types.CallbackQuery, state: FSMContext):
    ids = _tail(callback.data, 2)
    if not ids or len(ids) != 2:
        await callback.answer(_STALE, show_alert=True)
        return
    form_id, idx = ids
    tabs = (await state.get_data()).get("extf_tabs") or []
    if await xdb.get_form(form_id) is None or not 0 <= idx < len(tabs):
        await callback.answer(_STALE, show_alert=True)
        return
    form = await xdb.get_form(form_id)
    if tabs[idx] in await protected_tab_titles(form):
        await callback.answer("Эта вкладка служебная — выберите другую или создайте новую",
                              show_alert=True)
        return
    await xdb.set_form_mirror(form_id, tabs[idx], None)
    await render_form_card(callback, form_id, edit=True)
    await callback.answer("Вкладка выбрана")


@router.callback_query(F.data.startswith("extf_tabnone:"))
async def extf_tabnone(callback: types.CallbackQuery):
    ids = _tail(callback.data)
    if not ids or await xdb.get_form(ids[0]) is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    await xdb.set_form_mirror(ids[0], None, None)
    await render_form_card(callback, ids[0], edit=True)
    await callback.answer("Копия в таблицу отключена")


# ---------- адрес приёмника ----------

def _hook_url(secret: str) -> str | None:
    base = (config.DASHBOARD_PUBLIC_URL or "").rstrip("/")
    return f"{base}/app/hooks/yform/{secret}" if base else None


def _hook_text(form: dict) -> str:
    head = f"🔗 <b>Адрес и инструкция — «{_e(form['title'])}»</b>\n\n"
    if form["platform"] != "yandex":
        return head + ("Для Google Формы адрес не нужен — бот сам читает таблицу ответов "
                       "раз в 5 минут.")
    url = _hook_url(form.get("secret") or "")
    if not url or not form.get("secret"):
        return head + ("Адрес сайта события не настроен — без него Яндекс не сможет присылать "
                       "ответы. Ответы всё равно подтянутся сверкой раз в 10 минут.")
    return head + (
        "Чтобы ответы приходили сразу, один раз подключите адрес в Яндекс Формах:\n\n"
        f"<code>{_e(url)}</code>\n\n"
        "1. Откройте форму → «Интеграции».\n"
        "2. Выберите «API» → «Запрос JSON-RPC POST» и вставьте адрес выше.\n"
        "3. Нажмите «Сохранить».\n\n"
        "Поля «Запрос заданным методом» и «Параметры» не заполняйте.\n"
        "Если ответы не приходят — загляните в «Выполненные интеграции» в той же вкладке.\n"
        "Ответы, пришедшие раньше, бот подтянет сам.\n\n"
        "Адрес секретный: не пересылайте его посторонним."
    )


async def show_hook_info(message, form_id: int, *, edit: bool = True) -> None:
    form = await xdb.get_form(form_id)
    if form is None:
        await _show(message, "Форма не найдена — вернитесь к списку форм.",
                    InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ К списку", "admin_ext_forms")]]),
                    edit=edit)
        return
    rows = []
    if form["platform"] == "yandex" and form.get("secret"):
        rows.append([_btn("🔁 Новый адрес", f"extf_rehook:{form_id}")])
    rows.append(_back_row(form_id))
    await _show(message, _hook_text(form), InlineKeyboardMarkup(inline_keyboard=rows), edit=edit)


@router.callback_query(F.data.startswith("extf_hook:"))
async def extf_hook(callback: types.CallbackQuery):
    ids = _tail(callback.data)
    if not ids or await xdb.get_form(ids[0]) is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    await show_hook_info(callback, ids[0], edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_rehook:"))
async def extf_rehook(callback: types.CallbackQuery):
    ids = _tail(callback.data)
    if not ids or await xdb.get_form(ids[0]) is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    fid = ids[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔁 Да, сменить адрес", f"extf_rehook_ok:{fid}")],
        [_btn("⬅️ Не менять", f"extf_hook:{fid}")],
    ])
    await _show(callback, "Старый адрес перестанет работать — замените его в Интеграциях Яндекса. "
                          "Пока не замените, ответы будут приходить только сверкой раз в 10 минут.\n\n"
                          "Сменить адрес?", kb, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("extf_rehook_ok:"))
async def extf_rehook_ok(callback: types.CallbackQuery):
    ids = _tail(callback.data)
    if not ids or await xdb.get_form(ids[0]) is None:
        await callback.answer("Форма не найдена — обновите список.", show_alert=True)
        return
    await xdb.set_form_secret(ids[0], secrets.token_urlsafe(24))
    await show_hook_info(callback, ids[0], edit=True)
    await callback.answer("Адрес заменён")
