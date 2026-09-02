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
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from cities import admin_selected_city, city_label, ALL_CITIES, ALL_CITIES_LABEL
from handlers.admin_caps import required_capability, resolve_capabilities, _holds
from handlers.admin import router

logger = logging.getLogger(__name__)

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
        ("screen", "modcard_open", "🧾 Поля карточки заявки"),
        ("toggle", "settings_toggle_full_approval"),
        ("toggle", "settings_toggle_short_approval"),
        ("toggle", "settings_toggle_party_approval"),
        ("toggle", "settings_toggle_notify"),
        ("toggle", "toggle_preselect_enabled"),
        ("toggle", "toggle_pending_reminder"),
        ("toggle", "toggle_nudge_enabled"),
        ("toggle", "toggle_reg_edit_remoderation"),
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
        ("screen", "sheet_logs_open", "🕓 Журналы в таблицу"),
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
       Решает тот же `section_screen`, что и открытие раздела по кнопке, — один предикат
       «раздел показуем» на весь проект. Если права на раздел отозвали между нажатием и
       перерисовкой, шаг проваливается в шаг 3 и менеджер приземляется в корне: титульный
       экран раздела с единственной кнопкой «← Назад» был бы хуже.
    3. Иначе -> КОРЕНЬ разделов с объяснением переезда. Тупика не бывает ни при какой
       раскладке — тот же принцип запасного выхода, что у `back_button` (T-20-11).

    Шага «экран» здесь НЕТ и не будет: перерисовать под-экран (например «🧾 PDF согласий»)
    резолвер не может — под-экран строит свой хендлер, а реестра «callback_data -> экран» в
    проекте нет и заводить его значило бы завести вторую карту (D-01/D-15). Поэтому
    `callback_data="admin_consent_pdfs"` даёт шаг 2 и возвращает в раздел-владелец экрана,
    «📝 Анкета». Менеджер, загружающий пять PDF подряд, каждый раз открывает список заново —
    известное ограничение, а не промах перерисовки.

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
        screen = await section_screen(admin_id, section_token)
        if screen is not None:
            return screen

    # T-20-18: корень рисуется через `admin_keyboard_for` -> `build_admin_keyboard`, то есть
    # через свежий `resolve_capabilities` (D-05) — человек без прав получит пустую клавиатуру,
    # а не чужой список разделов.
    return SETTINGS_MOVED_TEXT, await admin_keyboard_for(admin_id)


async def op_return_keyboard(admin_id: int, callback_data: str) -> InlineKeyboardMarkup:
    """Клавиатура экрана возврата для ОПЕРАЦИИ (статистика, синхронизация, выгрузки, гейма).

    У операции свой текст — отчёт о проделанной работе, — а вот клавиатура должна быть той же,
    что и у экрана, с которого её запустили. Раньше все они дорисовывали КОРЕНЬ: до фазы 20 это
    и был предыдущий экран, после — нет, и менеджер, запустивший «🔄 Синхронизация» из раздела
    «📊 Данные», вылетал в корень и заходил в раздел заново ради соседней кнопки.

    Второй карты «операция -> экран» не заводим: раздел выводится из `callback_data` тем же
    `settings_return_screen`, что и у тумблеров. Операции за гейтом подтверждения
    (`admin_rebuild_sheet_go`, `admin_dedupe_sheet_go`) называют callback_data СВОЕЙ кнопки в
    разделе — строки `*_go` в `SECTIONS` не объявлены и без этого уводили бы в корень."""
    _text, kb = await settings_return_screen(admin_id, callback_data=callback_data)
    return kb


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


GROUP_IN_SECTION_LABEL = "⚙️ Тексты и настройки"


def section_group_label(section_token: str, group_token: str) -> str:
    """Подпись кнопки группы настроек ВНУТРИ раздела. Три группы («📋 Заявки», «💳 Оплата»,
    «🎮 Геймификация») называются так же, как их раздел, и кнопка с той же подписью прямо
    под заголовком раздела читалась как дубль — менеджеру неясно, что она ведёт в тексты и
    настройки (UAT фазы 20, п.3). Совпала с подписью раздела — подписываем по назначению;
    остальные («🎪 Событие/Медиа», «📝 Регистрация», «🎉 Party»…) остаются своим именем.
    Заголовок экрана самой группы («⚙️ Настройки → 📋 Заявки») не трогаем — там это имя группы."""
    from handlers.admin_settings import _settings_group_label  # ленивый шов (см. docstring модуля)

    label = _settings_group_label(group_token)
    return GROUP_IN_SECTION_LABEL if label == _SECTION_LABELS.get(section_token) else label


async def build_section_keyboard(token: str, admin_id: int, *, caps: set | None = None) -> InlineKeyboardMarkup:
    """Клавиатура раздела: шапка города (если модуль городов включён) -> строки раздела в
    объявленном порядке -> «← Назад» в корень.

    Шапка рисуется ТЕМ ЖЕ способом, что в `admin_keyboard_for` (09.3): модуль выключен
    (`code is None`) — строки шапки нет вовсе, паритет с до-фазовым поведением.

    D-05 (свежее чтение прав, без кеша) остаётся значением по умолчанию: не передали `caps` —
    читаем их здесь. Вызывающий, который права УЖЕ прочитал на ЭТОТ ЖЕ рендер (`section_screen`
    решает ими, показывать раздел вообще), передаёт их готовыми: тогда гейт и рендер физически
    не могут разъехаться, а вместо двух чтений подряд остаётся одно. Параметр keyword-only —
    случайно передать третьим позиционным аргументом чужой набор прав нельзя.

    WR-05: шапка города читается ЗДЕСЬ ровно один раз на рендер и передаётся в
    `settings_toggle_rows` готовой. Прежде каждая из двух читала её сама — два независимых
    await по одному ключу внутри сборки ОДНОЙ клавиатуры, и переключение города между ними
    давало экран с шапкой одного города и тумблером регистрации другого."""
    from handlers.admin_core import _ADMIN_MENU_ROWS  # ленивый шов (см. docstring модуля)
    from handlers.admin_settings import settings_toggle_rows

    if caps is None:
        caps = await resolve_capabilities(admin_id)
    rows = visible_rows(token, caps, admin_id in config.ADMIN_IDS)
    op_labels = {callback_data: text for text, callback_data in _ADMIN_MENU_ROWS}
    code = await admin_selected_city(admin_id)  # ЕДИНСТВЕННОЕ чтение шапки на рендер
    toggles = await settings_toggle_rows(admin_id, header_code=code) if any(r[0] == "toggle" for r in rows) else {}

    buttons: list[list[InlineKeyboardButton]] = []
    if code is not None:
        label_text = ALL_CITIES_LABEL if code == ALL_CITIES else f"🏙 Город: {await city_label(code)}"
        # Раздел-источник едет в callback_data: смена города — это контекст ВСЕГО раздела, и
        # после неё менеджер должен остаться в нём, а не оказаться в корне. Голая форма
        # «admin_city_switch» (корень, стейл-клавиатуры) продолжает работать.
        buttons.append([InlineKeyboardButton(text=label_text, callback_data=f"admin_city_switch:{token}")])

    # Строка-сирота (опечатка в реестре, снесённый тумблер) должна быть НЕВИДИМОЙ, а не ронять
    # `show_admin_section` до `callback.answer()` — иначе менеджер получает вечный спиннер.
    for row in rows:
        kind = row[0]
        if kind == "op":
            label = op_labels.get(row[1])
            if label is None:
                logger.warning("Раздел %s: строка без подписи в _ADMIN_MENU_ROWS (%s) — пропущена", token, row[1])
                continue
            buttons.append([InlineKeyboardButton(text=label, callback_data=row[1])])
        elif kind in ("screen", "screen_admin"):
            buttons.append([InlineKeyboardButton(text=row[2], callback_data=row[1])])
        elif kind == "toggle":
            # Проверяем НАЛИЧИЕ ключа, а не истинность: пустой список строк у существующего
            # тумблера легален (тумблер скрыт настройками) и сиротой не является.
            if row[1] not in toggles:
                logger.warning("Раздел %s: тумблер %s не объявлен в settings_toggle_rows — пропущен", token, row[1])
                continue
            buttons.extend(toggles[row[1]])
        elif kind == "group":
            buttons.append([InlineKeyboardButton(text=section_group_label(token, row[1]), callback_data=f"settings_group:{row[1]}")])

    # «← Назад» ведёт в существующий корень admin_menu — новых callback'ов не заводим.
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def is_section(token: str | None) -> bool:
    """Есть ли такой раздел. Нужен там, где токен приходит ИЗ callback_data (переключатель
    города несёт в себе раздел-источник): у стейл-кнопки в чате раздел мог быть переименован
    или убран, и такой токен должен тихо уводить в корень, а не в пустой экран."""
    return bool(token) and token in _SECTION_LABELS


async def section_screen(admin_id: int, token: str | None) -> tuple[str, InlineKeyboardMarkup] | None:
    """Пара (текст, клавиатура) экрана раздела — или `None`, если раздела нет либо в нём не
    осталось ни одной доступной строки (права отозвали между нажатием и перерисовкой).

    ОДИН предикат «раздел показуем» на весь проект: и экран раздела, и возврат из
    переключателя города спрашивают здесь, а не каждый по-своему."""
    if not is_section(token):
        return None
    caps = await resolve_capabilities(admin_id)
    if not visible_rows(token, caps, admin_id in config.ADMIN_IDS):
        return None
    return render_section_text(token), await build_section_keyboard(token, admin_id, caps=caps)


@router.callback_query(F.data.startswith("admin_sec:"))
async def show_admin_section(callback: types.CallbackQuery):
    # T-20-03: токен ищется в SECTIONS точным сравнением; неизвестный — алерт и выход, в
    # сообщение пользовательская строка не форматируется никогда.
    screen = await section_screen(callback.from_user.id, callback.data.split(":", 1)[1])
    if screen is None:
        # T-20-04: текст не перечисляет существующие разделы — чужая раскладка не утекает.
        await callback.answer("Раздел недоступен.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# Quick 260902-vth: шов «🕓 Журналы в таблицу» — регистрируется ПОСЛЕДНИМ (после всех хендлеров
# этого модуля), чтобы его строка в GOLDEN_SNAPSHOT (tests/test_refac_snapshot_260816.py)
# встала строго в хвосте, а не разъехалась порядком с уже существующими хендлерами раздела.
from handlers import admin_sheet_logs  # noqa: E402,F401
