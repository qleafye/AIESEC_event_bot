"""Phase 27 (27-06, LANG-04/LANG-05/LANG-09) — экран «🌐 Английские тексты».

Даёт менеджеру руки над корпусом английских строк анкеты (~265 штук, `services/i18n_sources.py
::corpus()`): пагинированный список с фильтрами по состоянию, карточка строки (русский рядом с
английским), ручная правка (`manual=1` — машина строку больше не трогает, LANG-05), «перевести
заново» с подтверждением (осознанный возврат машине) и метка «русский изменился» (менеджер
позже поправил русский исходник, старая ручная правка осиротела). Отдельный фильтр «Согласия»
даёт ручной ввод английского текста согласия (LANG-09) — эти строки НИКОГДА не попадают в
очередь машинного перевода (граница `services/i18n_sources.py`), но обязаны быть доступны для
правки здесь.

Форма — Phase 13 (REFAC-01), тот же приём, что у `handlers/admin_faq.py`: своего `Router()`
нет, хендлеры декорируют ОБЩИЙ `handlers.admin.router`; `handlers.admin_sections` (`back_button`)
импортируется ЛЕНИВО внутри функций — цикл на уровне модуля (admin_sections тянет
admin_settings, тот — обратно к admin_core).

Callback-намespace: `admin_i18n` (вход), `admin_i18n:{state}:{page}` (список), `admin_i18n_row:
{state}:{page}:{idx}` (карточка — ИНДЕКС на странице, не `src_hash`: хеш в `callback_data` съел
бы 32 символа и ничего не дал бы менеджеру, T-27-06-05), `admin_i18n_edit:.../admin_i18n_edit_
new:...` (начать правку — основной строки / новой редакции при «русский изменился»),
`admin_i18n_retr:.../admin_i18n_retr_go:...` (подтверждение «перевести заново» и его
исполнение). `state` — один из пяти фильтров: `all`/`pending`/`manual`/`failed`/`consent`.
"""
import html as html_module

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.db import (
    clear_translation_manual,
    enqueue_translation,
    fetch_translations,
    get_translation,
    list_translations,
    reset_translation_attempts,
    upsert_translation,
)
from handlers.admin import router
from handlers.states import AdminI18nEdit
from keyboards.builders import get_cancel_kb
from services.i18n import src_hash as compute_src_hash
from services.i18n_sources import corpus
from services.i18n_worker import bulk_seed, progress
from settings_schema import SETTINGS_SCHEMA, get_setting_typed

LANG = "en"
PAGE_SIZE = 10
CONSENT_ORIGIN_PREFIX = "consent:"

# Токен фильтра (в callback_data) -> состояние `database.db.list_translations`. `"consent"` —
# синтетический пятый фильтр, не state этой функции (см. `_consent_rows` ниже).
_TOKEN_STATES = {"all": None, "pending": "pending", "manual": "manual", "failed": "failed"}
_FILTER_LABELS = [
    ("all", "🌐 Все"),
    ("pending", "🕓 Без перевода"),
    ("manual", "✏️ Правлено вручную"),
    ("failed", "⚠️ Не удалось"),
    ("consent", "📄 Согласия"),
]


def _short(text: str, limit: int = 40) -> str:
    """Схлопывает пробелы/переносы и обрезает для строки списка — «бот для людей»: менеджер
    узнаёт вопрос по первым словам, не по хешу."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _origin_label(origin_key: str | None) -> str:
    """Человеческая подсказка «откуда строка» (T-27-06-05: сырой `origin_key` менеджеру не
    показываем). Неизвестный/отсутствующий источник — «из анкеты», не код."""
    if not origin_key:
        return "из анкеты"
    if origin_key.startswith(CONSENT_ORIGIN_PREFIX):
        return "Текст согласия"
    if origin_key.startswith("lit:"):
        return "Текст анкеты (значение по умолчанию)"
    if origin_key.startswith("reg_prompt_") or origin_key.startswith("reg_help_"):
        return "Текст вопроса анкеты"
    spec = SETTINGS_SCHEMA.get(origin_key)
    if spec and spec.get("label"):
        return f"Настройка «{spec['label']}»"
    return "из анкеты"


async def _corpus_hash_index() -> dict[str, dict[str, str]]:
    """`origin_key -> {текущий src_hash: текущий русский текст}` — единственный способ узнать,
    устарела ли ручная правка («русский изменился»): контент-адресация не хранит связь
    «старый хеш -> новый хеш» сама по себе, только пересчёт текущего корпуса её восстанавливает.
    Один вызов `corpus()` на рендер — экран административный, не горячий путь делегата."""
    idx: dict[str, dict[str, str]] = {}
    for origin_key, text in await corpus():
        if not origin_key:
            continue
        idx.setdefault(origin_key, {})[compute_src_hash(text)] = text
    return idx


async def _corpus_gap() -> tuple[int, int]:
    """Квик 260912 (W5, Задача 4) — `(всего_в_корпусе, без_перевода)`. `corpus()` перечисляет
    ВЕСЬ делегатский текст анкеты (`services/i18n_sources.py`), включая строки, которые
    никогда не ставились в очередь перевода вовсе — дыру, которую `progress()['total']`
    принципиально не видит: тот счётчик считает строки, УЖЕ лежащие в `translations`, а не
    размер корпуса. `fetch_translations` фильтрует пустые `text` — та же карта, которой
    пользуется `services.i18n.tr()`, то есть ровно то, что делегат ещё увидит по-русски."""
    items = await corpus()
    translated = await fetch_translations(LANG)
    without = sum(1 for _origin_key, text in items if compute_src_hash(text) not in translated)
    return len(items), without


def _is_stale_manual(row: dict, corpus_idx: dict[str, dict[str, str]]) -> bool:
    """LANG-05/UAT 27-06: `row` — ручная правка (`manual=1`), у которой текущий русский текст
    ЭТОГО ЖЕ источника (`origin_key`) больше не даёт тот же `src_hash` — менеджер поправил
    русский ПОСЛЕ того, как ввёл английский вручную. Consent-строки (`origin_key` вне корпуса)
    никогда не помечаются — `corpus_idx` их не содержит по построению."""
    if not row.get("manual") or not row.get("origin_key"):
        return False
    current = corpus_idx.get(row["origin_key"])
    if not current:
        return False
    return row["src_hash"] not in current


async def _consent_rows() -> list[dict]:
    """Строки согласий (LANG-09) — вне очереди/корпуса всегда, но обязаны быть доступны для
    РУЧНОГО ввода здесь. Каждая — либо уже сохранённая запись `translations` (менеджер уже
    вводил английский), либо «виртуальная» строка без сохранённого перевода (`text=None`) —
    правка создаёт настоящую запись через тот же `upsert_translation`, что и обычные строки."""
    texts: list[tuple[str, str]] = []
    button = await get_setting_typed("consent_button_text")
    if button:
        texts.append((f"{CONSENT_ORIGIN_PREFIX}button", str(button).strip()))
    recollect = await get_setting_typed("consent_recollect_text")
    if recollect:
        texts.append((f"{CONSENT_ORIGIN_PREFIX}recollect", str(recollect).strip()))
    raw_list = await get_setting_typed("consent_list")
    if raw_list:
        lines = raw_list.splitlines() if isinstance(raw_list, str) else list(raw_list)
        for line in lines:
            name = str(line).split("|", 1)[0].strip()
            if name:
                texts.append((f"{CONSENT_ORIGIN_PREFIX}item", name))

    rows: list[dict] = []
    seen_hashes: set[str] = set()
    for origin_key, text in texts:
        if not text:
            continue
        text_hash = compute_src_hash(text)
        if text_hash in seen_hashes:
            continue
        seen_hashes.add(text_hash)
        existing = await get_translation(LANG, text_hash)
        if existing:
            rows.append(existing)
        else:
            rows.append({
                "lang": LANG, "src_hash": text_hash, "src_text": text, "text": None,
                "manual": 0, "origin_key": origin_key, "updated_at": None,
            })
    return rows


async def _load_page(state_token: str, page: int) -> tuple[list[dict], int]:
    offset = page * PAGE_SIZE
    if state_token == "consent":
        rows_all = await _consent_rows()
        return rows_all[offset: offset + PAGE_SIZE], len(rows_all)
    state = _TOKEN_STATES.get(state_token)
    return await list_translations(LANG, offset=offset, limit=PAGE_SIZE, state=state)


def _row_badge(row: dict, changed: bool) -> str:
    if changed:
        return "⚠️ русский изменился"
    if row.get("manual"):
        return "✏️ вручную"
    text_value = row.get("text")
    if text_value is None:
        return "🕓 без перевода"
    if text_value == "":
        return "⚠️ не удалось"
    return "✅"


async def render_i18n_list(state_token: str = "all", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    if state_token not in _TOKEN_STATES and state_token != "consent":
        state_token = "all"
    page = max(0, page)

    prog = await progress(LANG)
    module_on = (await get_setting_typed("delegate_lang_enabled")) == "on"
    # Квик 260912 (W5, Задача 4): `prog['total']` считает только строки, УЖЕ лежащие в
    # `translations` — дыру «строка корпуса никогда не ставилась в очередь» этот счётчик
    # принципиально не видит (bulk_seed при включении тумблера — одноразовый фоновый спавн,
    # не переживающий рестарт/сбой посреди посева). corpus_total/corpus_without считают ВЕСЬ
    # корпус — это и есть метрика, которую видит менеджер, и число на кнопке догонялки.
    corpus_total, corpus_without = await _corpus_gap()

    rows, total = await _load_page(state_token, page)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page >= pages:
        page = pages - 1
        rows, total = await _load_page(state_token, page)

    # Метка «русский изменился» имеет смысл только там, где вообще могут быть ручные строки.
    corpus_idx = await _corpus_hash_index() if state_token in ("all", "manual") else {}

    lines = ["🌐 <b>Английские тексты</b>"]
    if not module_on:
        lines.append(
            "Модуль английского языка сейчас выключен — делегаты видят русский. "
            "Включить: раздел «📝 Анкета» → «🌐 Английский язык анкеты»."
        )
    lines.append(
        f"Переведено {prog['done']} из {prog['total']}, вручную {prog['manual']}, "
        f"не удалось {prog['failed']}."
    )
    lines.append(
        f"В корпусе анкеты {corpus_total} строк, делегат всё ещё увидит по-русски "
        f"{corpus_without}."
    )
    lines.append("")
    lines.append(f"Страница {page + 1} из {pages} (всего {total})." if total else "Пусто в этом фильтре.")

    buttons: list[list[InlineKeyboardButton]] = []
    # Задача 4: кнопка ВСЕГДА видна (менеджеру нужна и тогда, когда он не уверен, есть ли
    # дыра) — число в подписи только когда оно больше нуля, само нажатие неразрушительно
    # (ручные правки менеджера защищены upsert_translation/bulk_seed, INSERT OR IGNORE не
    # плодит дублей) — экран подтверждения был бы шумом (CLAUDE.md: только для разрушительных).
    seed_label = "⟳ Догнать перевод" if not corpus_without else f"⟳ Догнать перевод ({corpus_without})"
    buttons.append([InlineKeyboardButton(text=seed_label, callback_data="admin_i18n_seed")])

    filter_buttons = [
        InlineKeyboardButton(
            text=(f"• {label}" if token == state_token else label),
            callback_data=f"admin_i18n:{token}:0",
        )
        for token, label in _FILTER_LABELS
    ]
    buttons.append(filter_buttons[:3])
    buttons.append(filter_buttons[3:])

    for idx, row in enumerate(rows):
        changed = _is_stale_manual(row, corpus_idx)
        badge = _row_badge(row, changed)
        title = html_module.escape(_short(row.get("src_text") or ""))
        buttons.append([InlineKeyboardButton(
            text=f"{badge} {title}", callback_data=f"admin_i18n_row:{state_token}:{page}:{idx}",
        )])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            text="◀ Назад", callback_data=f"admin_i18n:{state_token}:{page - 1}",
        ))
    if total:
        nav_row.append(InlineKeyboardButton(text=f"стр. {page + 1} из {pages}", callback_data="admin_i18n_noop"))
    if page + 1 < pages:
        nav_row.append(InlineKeyboardButton(
            text="Вперёд ▶", callback_data=f"admin_i18n:{state_token}:{page + 1}",
        ))
    if nav_row:
        buttons.append(nav_row)

    from handlers.admin_sections import back_button  # ленивый шов (см. докстринг модуля)
    buttons.append([back_button("admin_i18n")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_i18n_card(state_token: str, page: int, idx: int) -> tuple[str, InlineKeyboardMarkup, dict] | None:
    """`None` — строка исчезла/сдвинулась между рендерами (стейл-клавиатура, тот же приём, что
    у `handlers/admin_faq.py::render_faq_card`); вызывающий отвечает алертом, не правкой."""
    rows, _total = await _load_page(state_token, page)
    if idx < 0 or idx >= len(rows):
        return None
    row = rows[idx]
    corpus_idx = await _corpus_hash_index()
    changed = _is_stale_manual(row, corpus_idx)
    is_consent = str(row.get("origin_key") or "").startswith(CONSENT_ORIGIN_PREFIX)

    src_text = row.get("src_text") or ""
    text_value = row.get("text")

    lines = ["🌐 <b>Строка анкеты</b>", f"Откуда: {_origin_label(row.get('origin_key'))}", ""]
    lines.append(f"<b>Русский:</b>\n{html_module.escape(src_text)}")
    lines.append("")
    if text_value:
        lines.append(f"<b>Английский:</b>\n{html_module.escape(text_value)}")
    else:
        lines.append("<b>Английский:</b> перевода пока нет.")
    if row.get("manual"):
        lines.append("")
        lines.append("✏️ Текст правлен вручную — машина эту строку больше не трогает.")
    if text_value == "":
        lines.append("")
        lines.append("⚠️ Не удалось перевести автоматически (движок перевода недоступен).")
    if is_consent:
        lines.append("")
        lines.append("Машинный перевод согласий запрещён — только ручной ввод (LANG-09).")

    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="✏️ Изменить английский", callback_data=f"admin_i18n_edit:{state_token}:{page}:{idx}",
        )],
    ]
    if not is_consent:
        buttons.append([InlineKeyboardButton(
            text="↻ Перевести заново", callback_data=f"admin_i18n_retr:{state_token}:{page}:{idx}",
        )])

    if changed:
        current = corpus_idx.get(row["origin_key"], {})
        _new_hash, new_text = next(iter(current.items()))
        lines.append("")
        lines.append("⚠️ <b>Русский изменился</b> — ваш английский относится к прежней редакции:")
        lines.append(html_module.escape(_short(new_text, 200)))
        buttons.append([InlineKeyboardButton(
            text="✏️ Изменить английский (новая редакция)",
            callback_data=f"admin_i18n_edit_new:{state_token}:{page}:{idx}",
        )])

    buttons.append([InlineKeyboardButton(text="◀ К списку", callback_data=f"admin_i18n:{state_token}:{page}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons), row


def _parse_triplet(data: str) -> tuple[str, int, int] | None:
    try:
        rest = data.split(":", 1)[1]
        state_token, page_s, idx_s = rest.split(":")
        return state_token, int(page_s), int(idx_s)
    except (IndexError, ValueError):
        return None


async def _edit_prompt_text(header: str, ru_text: str, en_hint: str | None) -> str:
    lines = [header, "", f"<b>Русский:</b> {html_module.escape(ru_text)}"]
    if en_hint is not None:
        lines.append(f"<b>Сейчас по-английски:</b> {html_module.escape(en_hint)}")
    lines.append("")
    lines.append(
        "Если нужно несколько строк, а Enter на телефоне отправляет сообщение — "
        "разделите строки знаком «;»."
    )
    return "\n".join(lines)


# ── Экраны: вход, список, карточка ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_i18n")
async def admin_i18n_entry(callback: types.CallbackQuery):
    text, kb = await render_i18n_list("all", 0)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin_i18n_noop")
async def admin_i18n_noop(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("admin_i18n:"))
async def admin_i18n_list_page(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    state_token = parts[1] if len(parts) > 1 else "all"
    try:
        page = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        page = 0
    text, kb = await render_i18n_list(state_token, page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_i18n_row:"))
async def admin_i18n_row(callback: types.CallbackQuery):
    parsed = _parse_triplet(callback.data)
    if parsed is None:
        await callback.answer("Не найдено.", show_alert=True)
        return
    screen = await render_i18n_card(*parsed)
    if screen is None:
        await callback.answer("Строка недоступна — обновите список.", show_alert=True)
        return
    text, kb, _row = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Ручная правка ────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_i18n_edit_new:"))
async def admin_i18n_edit_new_start(callback: types.CallbackQuery, state: FSMContext):
    parsed = _parse_triplet(callback.data)
    if parsed is None:
        await callback.answer("Не найдено.", show_alert=True)
        return
    state_token, page, idx = parsed
    rows, _total = await _load_page(state_token, page)
    if idx < 0 or idx >= len(rows):
        await callback.answer("Строка недоступна — обновите список.", show_alert=True)
        return
    row = rows[idx]
    corpus_idx = await _corpus_hash_index()
    current = corpus_idx.get(row.get("origin_key") or "")
    if not current or row.get("src_hash") in current:
        await callback.answer("Новой редакции больше нет — обновите список.", show_alert=True)
        return
    new_hash, new_text = next(iter(current.items()))
    await state.update_data(
        i18n_hash=new_hash, i18n_src_text=new_text, i18n_origin_key=row.get("origin_key"),
        i18n_return=f"{state_token}:{page}:{idx}",
    )
    await state.set_state(AdminI18nEdit.text)
    prompt = await _edit_prompt_text("Пришлите английский текст для НОВОЙ редакции русского:", new_text, None)
    await callback.message.answer(prompt, parse_mode="HTML", reply_markup=get_cancel_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("admin_i18n_edit:"))
async def admin_i18n_edit_start(callback: types.CallbackQuery, state: FSMContext):
    parsed = _parse_triplet(callback.data)
    if parsed is None:
        await callback.answer("Не найдено.", show_alert=True)
        return
    state_token, page, idx = parsed
    rows, _total = await _load_page(state_token, page)
    if idx < 0 or idx >= len(rows):
        await callback.answer("Строка недоступна — обновите список.", show_alert=True)
        return
    row = rows[idx]
    await state.update_data(
        i18n_hash=row["src_hash"], i18n_src_text=row["src_text"], i18n_origin_key=row.get("origin_key"),
        i18n_return=f"{state_token}:{page}:{idx}",
    )
    await state.set_state(AdminI18nEdit.text)
    prompt = await _edit_prompt_text(
        "Пришлите новый английский текст.", row.get("src_text") or "",
        row.get("text") or "перевода пока нет",
    )
    await callback.message.answer(prompt, parse_mode="HTML", reply_markup=get_cancel_kb())
    await callback.answer()


async def _return_to_card(message: types.Message, ret: str | None) -> None:
    if not ret:
        return
    try:
        state_token, page_s, idx_s = ret.split(":")
        screen = await render_i18n_card(state_token, int(page_s), int(idx_s))
    except ValueError:
        screen = None
    if screen:
        text, kb, _row = screen
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(AdminI18nEdit.text, F.text.in_({"Отмена", "/cancel"}))
async def admin_i18n_edit_cancel(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    await _return_to_card(message, data.get("i18n_return"))


@router.message(AdminI18nEdit.text)
async def admin_i18n_edit_step(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пришлите английский текст или нажмите «Отмена».")
        return
    raw = message.text.strip()
    # Мобильный Enter отправляет сообщение — «;» как разделитель строк (проектная ловушка).
    text_value = "\n".join(part.strip() for part in raw.split(";")) if ";" in raw else raw

    data = await state.get_data()
    target_hash = data.get("i18n_hash")
    src_text = data.get("i18n_src_text")
    origin_key = data.get("i18n_origin_key")
    ret = data.get("i18n_return")
    await state.clear()

    if not target_hash or src_text is None:
        await message.answer("Строка утеряна — откройте экран заново.", reply_markup=ReplyKeyboardRemove())
        return

    await upsert_translation(LANG, target_hash, src_text, text_value, manual=1, origin_key=origin_key)
    await message.answer("✅ Сохранено — теперь машина эту строку не трогает.", reply_markup=ReplyKeyboardRemove())
    await _return_to_card(message, ret)


# ── «Перевести заново» — разрушительно для ручной правки, только с подтверждением ──────────

@router.callback_query(F.data.startswith("admin_i18n_retr:"))
async def admin_i18n_retranslate_confirm(callback: types.CallbackQuery):
    parsed = _parse_triplet(callback.data)
    if parsed is None:
        await callback.answer("Не найдено.", show_alert=True)
        return
    state_token, page, idx = parsed
    rows, _total = await _load_page(state_token, page)
    if idx < 0 or idx >= len(rows):
        await callback.answer("Строка недоступна — обновите список.", show_alert=True)
        return
    row = rows[idx]
    if str(row.get("origin_key") or "").startswith(CONSENT_ORIGIN_PREFIX):
        await callback.answer("Перевод согласий делается только руками.", show_alert=True)
        return
    text = (
        "↻ <b>Перевести заново?</b>\n\n"
        f"{html_module.escape(_short(row.get('src_text') or '', 200))}\n\n"
    )
    if row.get("manual"):
        text += "Ваш английский текст будет заменён машинным переводом. Отменить будет нельзя."
    else:
        text += "Строка будет поставлена в очередь на машинный перевод заново."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="↻ Да, перевести заново", callback_data=f"admin_i18n_retr_go:{state_token}:{page}:{idx}",
        )],
        [InlineKeyboardButton(text="◀ Отмена", callback_data=f"admin_i18n_row:{state_token}:{page}:{idx}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_i18n_retr_go:"))
async def admin_i18n_retranslate_go(callback: types.CallbackQuery):
    parsed = _parse_triplet(callback.data)
    if parsed is None:
        await callback.answer("Не найдено.", show_alert=True)
        return
    state_token, page, idx = parsed
    rows, _total = await _load_page(state_token, page)
    if idx < 0 or idx >= len(rows):
        await callback.answer("Строка недоступна — обновите список.", show_alert=True)
        return
    row = rows[idx]
    if str(row.get("origin_key") or "").startswith(CONSENT_ORIGIN_PREFIX):
        await callback.answer("Перевод согласий делается только руками.", show_alert=True)
        return
    if row.get("manual"):
        await clear_translation_manual(LANG, row["src_hash"])
    await enqueue_translation(LANG, row["src_hash"], row["src_text"], origin_key=row.get("origin_key"))

    screen = await render_i18n_card(state_token, page, idx)
    if screen is None:
        await callback.answer("Поставлено в очередь.", show_alert=True)
        return
    text, kb, _row = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Поставлено в очередь на перевод.")


# ── Догонялка перевода (Квик 260912, W5, Задача 4) ──────────────────────────────────────────

@router.callback_query(F.data == "admin_i18n_seed")
async def admin_i18n_seed(callback: types.CallbackQuery):
    """Наполняет очередь недостающими строками корпуса — см. докстринг модуля и `_corpus_gap`
    про то, почему счётчика `progress()['total']` менеджеру недостаточно. `reset_translation_
    attempts` ПЕРЕД `bulk_seed` — иначе строки, застрявшие на `MAX_ATTEMPTS`, останутся вне
    очереди (`enqueue_translation` для уже стоящей строки — `INSERT OR IGNORE`, не сброс
    попыток). Разбор — фоновая джоба `translation_drain`, ничего тяжёлого не грузится тут же.

    Две независимые метрики в алерте: `reset_count` (строки, УЖЕ стоявшие в очереди, но
    исчерпавшие попытки — ожили) и `queued` (строки, которых в очереди не было вовсе —
    `bulk_seed` вернёт 0 для тех, что уже там или под ручной правкой). Они не пересекаются по
    построению (`reset_translation_attempts` трогает только уже существующие строки,
    `bulk_seed` считает только новые вставки), поэтому суммарный «нечего делать» — это ровно
    оба нуля, а не эвристика."""
    reset_count = await reset_translation_attempts(LANG)
    queued = await bulk_seed(LANG)

    text, kb = await render_i18n_list("all", 0)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass

    if not queued and not reset_count:
        await callback.answer(
            "Догонять нечего — весь корпус уже переведён, в очереди или под ручной правкой.",
            show_alert=True,
        )
        return

    parts = []
    if queued:
        parts.append(f"новых строк в очереди: {queued}")
    if reset_count:
        parts.append(f"застрявших строк оживлено: {reset_count}")
    await callback.answer(
        "Готово — " + ", ".join(parts) + ". Перевод идёт фоном — обновите экран через "
        "минуту-другую, чтобы увидеть результат.",
        show_alert=True,
    )
