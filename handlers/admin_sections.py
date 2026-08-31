"""Phase 20 (20-01, ADMIN-IA-01): разделы админки по пути делегата.

До фазы 20 раскладка админки была размазана по двум местам: 22 плоские строки
`_ADMIN_MENU_ROWS` (в порядке появления фаз, а не смысла) и тело `build_settings_keyboard`
(18 тумблеров + 7 входов + 8 групп на одном экране). Разделов было не «слишком много» —
их не было вовсе, и менеджер искал «После одобрения» в настройках анкеты.

Здесь лежит ОДНА декларация `SECTIONS`, из которой выводятся: экран раздела, обратный
индекс «группа настроек -> её раздел» (нужен «Назад» из экрана группы) и структурный тест
покрытия `tests/test_admin_sections_ia20.py`. Ни один callback_data, хендлер, capability или
ключ `SETTINGS_SCHEMA` этим модулем не создаётся и не меняется — переезжают только кнопки.

Форма модуля — та же, что у соседних швов (`admin_dashboard.py`, `admin_miniapp.py`):
своего `Router()` нет, хендлеры декорируют ОБЩИЙ `admin.router` (техника 13-02), а сам
модуль импортируется ХВОСТОМ `handlers/admin_settings.py`. Импорты `admin_core`/
`admin_settings` — ленивые, внутри функций: на уровне модуля они дают цикл
(admin_core -> admin_sections -> admin_settings -> admin_core). Прецедент ленивого шва —
`handlers/admin_gamification.py`.
"""
from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from cities import admin_selected_city, city_label, ALL_CITIES, ALL_CITIES_LABEL
from handlers.admin_caps import required_capability, resolve_capabilities, _holds
from handlers.admin import router

# INVARIANT (13-01 cap-test): каждый `@router.*` декоратор ниже — в ОДНУ строку.

# Типы строк раздела (первый элемент кортежа):
#   ("op", callback_data)                  -- операция; подпись берётся из _ADMIN_MENU_ROWS
#   ("screen", callback_data, label)       -- вход в существующий под-экран
#   ("toggle", callback_data)              -- строка(и) тумблера из settings_toggle_rows
#   ("group", group_token)                 -- кнопка settings_group:{token}
#   ("screen_admin", callback_data, label) -- то же, что screen, но рисуется только суперадмину
#
# Порядок внутри раздела: операции сверху, настройки ниже. Порядок разделов — путь делегата:
# узнал о событии -> заполнил анкету -> ждёт решения -> платит -> получает рассылки -> играет;
# «Данные» и «Управление» — хвост для менеджера, а не для делегата.
SECTIONS: list[tuple[str, str, list[tuple]]] = [
    ("event", "🎪 Событие", [
        ("group", "event"),
        ("screen", "admin_menu_buttons", "🔘 Кнопки меню"),
        ("toggle", "settings_toggle_bonus"),
    ]),
    ("form", "📝 Анкета", [
        ("screen", "admin_event_preset", "🎛 Тип события (пресет)"),
        ("screen", "admin_reg_questions", "📋 Вопросы регистрации"),
        ("screen", "admin_reg_prompts", "✏️ Тексты вопросов"),
        ("toggle", "settings_toggle_reg"),
        ("toggle", "toggle_uni_mode"),
        ("toggle", "toggle_edu_conditional"),
        ("toggle", "toggle_show_progress"),
        ("toggle", "toggle_party_enabled"),
        ("toggle", "toggle_party_fork_question"),
        ("toggle", "toggle_consent_enabled"),
        ("screen", "admin_consent_pdfs", "🧾 PDF согласий"),
        ("group", "reg"),
        ("group", "party"),
        ("group", "consent"),
    ]),
    ("apps", "📋 Заявки", [
        ("op", "admin_applications"),
        ("op", "admin_stuck_questions"),
        ("toggle", "settings_toggle_full_approval"),
        ("toggle", "settings_toggle_short_approval"),
        ("toggle", "settings_toggle_party_approval"),
        ("toggle", "settings_toggle_notify"),
        ("toggle", "toggle_preselect_enabled"),
        ("toggle", "toggle_pending_reminder"),
        ("toggle", "toggle_nudge_enabled"),
        ("group", "apps"),
    ]),
    ("pay", "💳 Оплата", [
        ("op", "admin_receipts"),
        ("toggle", "toggle_payment_enabled"),
        ("toggle", "toggle_payment_reminders"),
        ("group", "pay"),
    ]),
    ("comms", "📢 Общение", [
        ("op", "admin_broadcast"),
        ("op", "admin_polls"),
    ]),
    ("game", "🎮 Геймификация", [
        ("op", "admin_game_tasks"),
        ("op", "admin_game_review"),
        ("op", "admin_coins_manual"),
        ("op", "admin_coins_journal"),
        ("op", "admin_game_sync_sheet"),
        ("op", "admin_game_stats"),
        ("group", "game"),
    ]),
    ("data", "📊 Данные", [
        ("op", "admin_stats"),
        ("op", "admin_monthly_stats"),
        ("op", "admin_source_stats"),
        ("op", "admin_export_csv"),
        ("op", "admin_export_incomplete"),
        ("op", "admin_sync_sheet"),
        ("op", "admin_rebuild_sheet"),
        ("op", "admin_dedupe_sheet"),
        ("group", "sheets"),
        ("screen", "admin_dashboard_settings", "📊 Дашборд"),
    ]),
    ("manage", "🔧 Управление", [
        ("op", "admin_cities"),
        ("op", "admin_settings_guide"),
        ("screen", "admin_roles", "👥 Роли и доступы"),
        ("screen", "admin_miniapp_settings", "🎨 Оформление"),
        # Phase 07.3 (02, RET-01): «🔄 Новый сезон» строже, чем весь экран, — только
        # суперадмин из config.ADMIN_IDS. Скрытие кнопки — это UX «бот для людей», НЕ
        # настоящий гейт: настоящий — перепроверка ADMIN_IDS внутри самих хендлеров
        # визарда, потому что стейл-клавиатура в чате живёт вечно.
        ("screen_admin", "admin_season_reset", "🔄 Новый сезон"),
        ("screen", "admin_season_import", "📥 Импорт прошлого события"),
        ("group", "system"),
    ]),
]

_SECTION_LABELS = {token: label for token, label, _ in SECTIONS}

# Одна человеческая строка под заголовком: зачем сюда заходят. Без кодов, токенов и
# английских терминов (CLAUDE.md: «мы делаем бота для людей, не для прогеров»).
_SECTION_HINTS = {
    "event": "Название, даты, место, контакты, приветствие и фото — то, что делегат видит до регистрации.",
    "form": "Какие вопросы задаём при регистрации и как они выглядят.",
    "apps": "Очередь заявок и всё, что делегат видит после подачи.",
    "pay": "Чеки делегатов, реквизиты, сроки и напоминания об оплате.",
    "comms": "Рассылки и опросы — всё, что уходит делегатам разом.",
    "game": "Задания, проверка сдач и монеты.",
    "data": "Статистика, выгрузки и Google-таблица.",
    "manage": "Города, роли, оформление и запуск нового сезона.",
}


def row_callback(row: tuple) -> str:
    """callback_data строки раздела — и для фильтра прав, и для теста покрытия."""
    if row[0] == "group":
        return f"settings_group:{row[1]}"
    return row[1]


def _declared_rows(token: str) -> list[tuple]:
    for tok, _label, rows in SECTIONS:
        if tok == token:
            return list(rows)
    return []


def section_rows(token: str) -> list[tuple]:
    """Объявленные строки раздела + динамическая группа «📦 Прочие» в «🔧 Управление».

    Leftover-safety: `_settings_group_keys("misc")` собирает ключи, не попавшие ни в одну
    объявленную группу. Без этой строки перекладка ключей между группами могла бы тихо
    спрятать настройку от менеджера — она осталась бы в реестре, но исчезла бы с экранов."""
    rows = _declared_rows(token)
    if token == "manage":
        from handlers.admin_settings import _settings_group_keys  # ленивый шов (см. docstring)
        if _settings_group_keys("misc"):
            rows.append(("group", "misc"))
    return rows


def section_of(callback_data: str) -> str | None:
    """Токен раздела, в котором объявлена строка с таким `callback_data`, или `None`.

    Это ЕДИНСТВЕННЫЙ ответ на вопрос «в какой раздел ведёт „← Назад“ с этого экрана», и он
    выведен ИЗ `SECTIONS`. Второй карты «кнопка -> раздел» в проекте нет и не будет — тот же
    инвариант, что и «нет второй карты кнопка -> право» (D-01/D-15): литеральный словарь
    разъехался бы с реестром при первом же переезде кнопки, и менеджер уехал бы «назад» в
    чужой раздел.

    Строка-группа матчится двумя способами сразу: и по полному `settings_group:{g}` (так
    выглядит кнопка), и по голому `g` (так группа зовётся внутри экрана группы)."""
    # «Прочие» рождаются в `section_rows("manage")` условно и в самом `SECTIONS` не объявлены —
    # обходом реестра их не найти, поэтому единственное исключение живёт здесь.
    if callback_data in ("misc", "settings_group:misc"):
        return "manage"
    for token, _label, rows in SECTIONS:
        for row in rows:
            if row_callback(row) == callback_data:
                return token
            if row[0] == "group" and row[1] == callback_data:
                return token
    return None


def _section_of_group(group_token: str) -> str | None:
    """Обратный индекс «группа настроек -> раздел» — частный случай `section_of`, а не второй
    обход реестра. Оставлен именем: на него ссылается «Назад» с экрана группы."""
    return section_of(group_token)


def back_button(callback_data: str, text: str = "← Назад") -> InlineKeyboardButton:
    """Кнопка возврата в РАЗДЕЛ-владелец экрана `callback_data`.

    Подпись по умолчанию — просто «← Назад»: старая подпись про возврат в настройки больше
    не описывает результат нажатия, кнопка ведёт в раздел (CLAUDE.md: подпись говорит, что
    произойдёт).

    T-20-11: неизвестный `callback_data` даёт запасной `admin_menu` (существующий корень) —
    тупика и необработанного callback'а не возникает ни при какой раскладке.

    T-20-10: это только навигация. При открытии раздела `show_admin_section` заново резолвит
    права и фильтрует строки, а каждый реальный callback проверяет `CapabilityMiddleware`."""
    token = section_of(callback_data)
    return InlineKeyboardButton(text=text, callback_data=f"admin_sec:{token}" if token else "admin_menu")


# Экран, на который приземляется старая кнопка «⚙️ Настройки форума» (callback `admin_settings`)
# из клавиатур, отрисованных ДО фазы 20 и живущих в чатах вечно (D-03), — и он же экран
# возврата, когда подсказки «откуда пришли» нет вовсе. Объявлен ОДИН раз: и `show_admin_settings`,
# и ветка КОРЕНЬ резолвера ниже берут текст отсюда, иначе две формулировки разъедутся.
SETTINGS_MOVED_TEXT = (
    "⚙️ <b>Настройки форума</b>\n\n"
    "Настройки переехали внутрь разделов: открой раздел — операции сверху, настройки ниже."
)


async def settings_return_screen(
    admin_id: int,
    *,
    callback_data: str | None = None,
    setting_key: str | None = None,
    group_token: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """ЕДИНСТВЕННЫЙ ответ на вопрос «куда вернуть менеджера ПОСЛЕ действия».

    Возвращает ПАРУ (текст, клавиатура): экран возврата меняет и то, и другое, поэтому
    вызывающий делает `text, kb = await settings_return_screen(...)` и один edit_text/answer.

    Порядок разрешения (первое совпадение выигрывает); каждый шаг выведен из уже существующих
    реестров — второй карты «кнопка -> экран» в проекте нет и не будет (D-01/D-15):

    1. Названа существующая ГРУППА настроек (явно через `group_token` или по ключу настройки
       через `_group_of_setting_key`) -> экран группы. Обоснование: кнопки `settings_edit:` /
       `settings_photo:` / `settings_file:` живут ТОЛЬКО на экране группы, значит менеджер
       правил значение именно оттуда, и возврат на уровень раздела отбросил бы его на шаг
       назад посреди работы. Путь наверх остаётся: «← Назад» экрана группы ведёт в раздел.
    2. `callback_data` объявлен строкой раздела (`section_of` вернул токен) -> экран РАЗДЕЛА.
       Это путь всех тумблеров: тумблер — строка раздела, значит менеджер был на разделе.
    3. Иначе -> КОРЕНЬ разделов с объяснением переезда. Тупика не бывает ни при какой
       раскладке — тот же принцип запасного выхода, что у `back_button` (T-20-11).

    WR-05: сам резолвер шапку города НЕ читает — ни одного её чтения здесь нет; каждая из
    трёх пар рендера читает шапку ровно один раз внутри себя, как и до фазы."""
    from handlers.admin_settings import (  # ленивый шов (см. docstring модуля)
        SETTINGS_GROUPS, _group_of_setting_key,
        render_settings_group_text, build_settings_group_keyboard,
    )
    from handlers.admin_core import admin_keyboard_for

    token = group_token or (_group_of_setting_key(setting_key) if setting_key else None)
    if token and token in {tok for _label, tok, _keys in SETTINGS_GROUPS} | {"misc"}:
        return (await render_settings_group_text(token, admin_id),
                await build_settings_group_keyboard(token, admin_id))

    section_token = section_of(callback_data) if callback_data else None
    if section_token:
        return render_section_text(section_token), await build_section_keyboard(section_token, admin_id)

    # T-20-18: корень рисуется через `admin_keyboard_for` -> `build_admin_keyboard`, то есть
    # через свежий `resolve_capabilities` (D-05) — человек без прав получит пустую клавиатуру,
    # а не чужой список разделов.
    return SETTINGS_MOVED_TEXT, await admin_keyboard_for(admin_id)


def visible_rows(token: str, caps: set, is_superadmin: bool) -> list[tuple]:
    """Чистая, синхронная, без I/O (идиома `_private`-хелперов CONVENTIONS.md) — та же
    форма, что у `_visible_menu_rows`. Право строки берётся ЖИВЬЁМ из `ADMIN_CAPS` через
    `required_capability`; второй карты «кнопка -> право» в проекте нет и не будет (D-01/D-15,
    сторож — tests/test_roles_phase8.py::test_menu_has_no_second_map).

    Скрытие строки — удобство, а не защита: `CapabilityMiddleware` проверяет каждый реальный
    callback независимо от того, что вернула эта функция (D-15 требует «И», не «ИЛИ»)."""
    rows = []
    for row in section_rows(token):
        cap = required_capability(callback_data=row_callback(row))
        if cap is None:
            continue  # deny-by-default (D-02): строка без записи в ADMIN_CAPS не рисуется
        if not _holds(caps, cap):
            continue
        if row[0] == "screen_admin" and not is_superadmin:
            continue
        rows.append(row)
    return rows


def visible_sections(caps: set, is_superadmin: bool) -> list[tuple[str, str]]:
    """(token, label) разделов, в которых есть хотя бы одна доступная строка. Пустой раздел
    не показывается вовсе — менеджер не должен упираться в экран «здесь ничего нет»."""
    return [(token, label) for token, label, _ in SECTIONS
            if visible_rows(token, caps, is_superadmin)]


def render_section_text(token: str) -> str:
    label = _SECTION_LABELS.get(token, token)
    hint = _SECTION_HINTS.get(token, "")
    return f"<b>{label}</b>\n\n{hint}" if hint else f"<b>{label}</b>"


async def build_section_keyboard(token: str, admin_id: int) -> InlineKeyboardMarkup:
    """Клавиатура раздела: шапка города (если модуль городов включён) -> строки раздела в
    объявленном порядке -> «← Назад» в корень.

    Шапка рисуется ТЕМ ЖЕ способом, что в `admin_keyboard_for` (09.3): модуль выключен
    (`code is None`) — строки шапки нет вовсе, паритет с до-фазовым поведением.

    Про два чтения шапки: `settings_toggle_rows` читает её сама (WR-05 внутри своего вызова),
    здесь читаем для строки-переключателя города — ровно та же пара независимых чтений, что
    у существующей пары render_settings_text/build_settings_keyboard на одном экране."""
    from handlers.admin_core import _ADMIN_MENU_ROWS  # ленивый шов (см. docstring модуля)
    from handlers.admin_settings import settings_toggle_rows, _settings_group_label

    caps = await resolve_capabilities(admin_id)  # D-05: свежее чтение, без кеша
    rows = visible_rows(token, caps, admin_id in config.ADMIN_IDS)
    op_labels = {callback_data: text for text, callback_data in _ADMIN_MENU_ROWS}
    toggles = await settings_toggle_rows(admin_id) if any(r[0] == "toggle" for r in rows) else {}

    buttons: list[list[InlineKeyboardButton]] = []
    code = await admin_selected_city(admin_id)
    if code is not None:
        label_text = ALL_CITIES_LABEL if code == ALL_CITIES else f"🏙 Город: {await city_label(code)}"
        buttons.append([InlineKeyboardButton(text=label_text, callback_data="admin_city_switch")])

    for row in rows:
        kind = row[0]
        if kind == "op":
            buttons.append([InlineKeyboardButton(text=op_labels[row[1]], callback_data=row[1])])
        elif kind in ("screen", "screen_admin"):
            buttons.append([InlineKeyboardButton(text=row[2], callback_data=row[1])])
        elif kind == "toggle":
            buttons.extend(toggles[row[1]])
        elif kind == "group":
            buttons.append([InlineKeyboardButton(text=_settings_group_label(row[1]), callback_data=f"settings_group:{row[1]}")])

    # «← Назад» ведёт в существующий корень admin_menu — новых callback'ов не заводим.
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("admin_sec:"))
async def show_admin_section(callback: types.CallbackQuery):
    # T-20-03: токен ищется в SECTIONS точным сравнением; неизвестный — алерт и выход, в
    # сообщение пользовательская строка не форматируется никогда.
    token = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    caps = await resolve_capabilities(admin_id)
    if token not in _SECTION_LABELS or not visible_rows(token, caps, admin_id in config.ADMIN_IDS):
        # T-20-04: текст не перечисляет существующие разделы — чужая раскладка не утекает.
        await callback.answer("Раздел недоступен.", show_alert=True)
        return
    await callback.message.edit_text(render_section_text(token), parse_mode="HTML", reply_markup=await build_section_keyboard(token, admin_id))
    await callback.answer()
