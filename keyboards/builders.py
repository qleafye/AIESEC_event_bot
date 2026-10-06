import logging
from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from config import config
from database.db import get_user, has_faq_for_city, has_important_today
from services.timeutil import msk_now
from settings_schema import get_setting_typed
from cities import default_city_code, get_setting_typed_for_city, cities_module_on, normalize_city
# Квик 260912 (W5, Задача 2/3): i18n_ui_en — литеральный модуль-словарь, ни одного импорта
# проекта (инвариант), цикла тут нет. services.i18n — aiogram-free/handlers-free (см. его
# докстринг), тоже без цикла.
from i18n_ui_en import MENU_EN
from services.i18n import resolve_lang
# Phase 21 (21-01, FORM-SYNC-01): литеральные списки вариантов ответа живут в корневом
# aiogram-free reg_options.py — общая точка правды для бота (эти клавиатуры) и будущего
# Mini App (reg_engine.step_spec()). Сами клавиатуры (ReplyKeyboardBuilder, add_other/
# add_skip, порядок kb.adjust(...)) остаются здесь без изменений.
from reg_options import (
    DEFAULT_SOURCE_OPTIONS,
    EDUCATION_STATUS_OPTIONS,
    COURSE_OPTIONS,
    DEPARTMENT_OPTIONS,
    AIESEC_ROLE_OPTIONS,
    ENGLISH_LEVEL_OPTIONS,
    ARRIVAL_OPTIONS,
    HOUSING_OPTIONS,
    POSITION_OPTIONS,
    ATTENDANCE_FORMAT_OPTIONS,
    INFORMAL_DAY_OPTIONS,
    LOCAL_COMMITTEE_OPTIONS,
    YES_NO_OPTIONS,
)

logger = logging.getLogger(__name__)

# --- Main Menu ---

MENU_BUTTONS = [
    ("menu_referral", "🔗 Моя реферальная ссылка"),
    ("menu_invites", "👥 Мои приглашённые"),
    ("menu_info", "ℹ️ Информация о форуме"),
    # D-29 (владелец 24.09): одна кнопка вместо двух — было menu_program (статичное фото) +
    # menu_schedule (интерактивная программа сессий, handlers/program.py). Ключ/подпись
    # оставлены старые (уже настроены у менеджеров), интерактивная программа стала запасным
    # видом ВНУТРИ handlers/user_actions.py::show_program, когда фото не загружено. Гейт ниже
    # (program_on = фото ЕСТЬ или у города есть хоть одна сессия) — тот же приём, что раньше
    # был только у menu_schedule (`has_program_sessions_for_city`).
    ("menu_program", "📅 Программа форума"),
    ("menu_speakers", "🗣 Спикеры"),
    ("menu_contacts", "📞 Контакты"),
    ("menu_question", "❓ Задать вопрос"),
    # Quick 260906-8uq (FAQ-01..06): рядом с «Задать вопрос» — экран готовых ответов.
    # Дополнительный гейт ниже (`has_faq_for_city`) прячет кнопку, пока в FAQ нет ни одного
    # включённого пункта, — тот же приём, что у menu_miniapp (T-19-54).
    ("menu_faq", "❓ Частые вопросы"),
    ("menu_coins", "🪙 Мои монеты"),
    ("menu_game_tasks", "🎯 Задания"),
    # Phase 19 (D-10): текстовая reply-кнопка «📱 Приложение» — НЕ web_app-кнопка (Pitfall 1:
    # KeyboardButton(web_app=...) даёт simple web view без initData, делегат не
    # аутентифицируется). Хендлер `handlers/user_actions.py::open_miniapp_button` отвечает
    # сообщением с inline web_app-кнопкой; там initData полный.
    ("menu_miniapp", "📱 Приложение"),
    # Phase 27 (27-04, LANG-01): переключатель языка анкеты в главном меню. Default этого
    # ключа — "off" (единственное исключение из конвенции menu_* default "on", см.
    # settings_schema.py) И доп. гейт ниже (тот же приём, что у menu_miniapp/menu_faq):
    # кнопка не появится ни от одного включения по отдельности — нужны оба, менеджер
    # включает модуль («📝 Анкета» → «Английский язык анкеты») и саму кнопку («🔘 Кнопки
    # меню»), иначе тап по показанной, но мёртвой кнопке (модуль ещё выключен) был бы
    # нарушением «бот для людей».
    ("menu_lang", "🌐 Язык / Language"),
    # Квик 260923 (форум-чекин, D-03): личный QR одобренного делегата — та же форма записи,
    # что и у menu_miniapp выше (двойной гейт: своя видимость menu_checkin_qr + модуль
    # checkin_qr_enabled, см. get_main_menu_kb ниже).
    ("menu_checkin_qr", "🎟 Мой QR"),
    # Форум-ночь п.7 (D-XX, «❗ Важное»): список важных рассылок делегату за сегодня. Гейт
    # ниже (has_important_today) прячет кнопку, пока сегодня для этого делегата не было ни
    # одной важной рассылки — тот же приём, что у menu_faq/menu_checkin_qr.
    ("menu_important", "❗ Важное"),
    # Форум-ночь п.8 (идея №19, SOS): кнопка видна только в дни форума города — гейт ниже
    # (services.sos.is_sos_active_for_city), тот же приём, что у menu_schedule/menu_important.
    ("menu_sos", "🆘 SOS"),
]

# Квик 260912 (W5, Задача 2) — множества «русская подпись + английская подпись» для входного
# матчинга фильтров aiogram (`F.text.in_(MENU_TEXTS[key])` вместо `F.text == "..."`). Собрано
# ВЫЧИСЛЕНИЕМ из `MENU_BUTTONS` + `i18n_ui_en.MENU_EN`, а не выписано руками — расширение
# набора подписей идёт по построению, без риска забыть одну из точек входа. Ключи — все ключи
# `MENU_BUTTONS` (12) плюс синтетический `"menu_payment"` для литерала «💳 Оплата» ниже (этот
# литерал НЕ входит в `MENU_BUTTONS` намеренно — экран тумблеров админки и реестр перебирают
# именно `MENU_BUTTONS`, несуществующий ключ `menu_payment` там сломал бы сверку со
# `SETTINGS_SCHEMA`). `MENU_EN.get(text, text)` — русская подпись без записи в `MENU_EN`
# (сейчас такой нет, кроме `menu_lang`, которая и так двуязычна) даёт множество из одного
# элемента, а не падает.
MENU_TEXTS: dict[str, frozenset[str]] = {
    key: frozenset({text, MENU_EN.get(text, text)}) for key, text in MENU_BUTTONS
}

# Национальная конференция (съезд АЙСЕК) — не форум: при event_type == "conference" две
# подписи меню меняются. Входной матчинг принимает обе формы — клавиатура, выданная делегату
# до смены типа события, продолжает работать.
CONFERENCE_MENU_LABELS: dict[str, str] = {
    "menu_info": "ℹ️ О конференции",
    "menu_program": "📅 Программа конференции",
}
for _key, _text in CONFERENCE_MENU_LABELS.items():
    MENU_TEXTS[_key] = MENU_TEXTS[_key] | {_text, MENU_EN.get(_text, _text)}
MENU_TEXTS["menu_payment"] = frozenset({"💳 Оплата", MENU_EN.get("💳 Оплата", "💳 Оплата")})
# D-29 объединил «🗓 Программа» (menu_schedule) с «📅 Программа форума». У делегатов с
# закэшированной старой клавиатурой кнопка осталась и молчала — её подписи (RU+EN, как были
# до объединения) ведут в тот же show_program. В меню кнопку не возвращаем.
LEGACY_MENU_TEXTS: dict[str, frozenset[str]] = {
    "menu_program": frozenset({"🗓 Программа", "🗓 Schedule"}),
}
for _key, _texts in LEGACY_MENU_TEXTS.items():
    MENU_TEXTS[_key] = MENU_TEXTS[_key] | _texts

# Идея №1 бэклога чек-ина (режим «день форума», services/forum_day_menu.py): пока для города
# делегата идёт форум, эти четыре кнопки (если каждая и так прошла СВОЙ обычный гейт — сама
# константа НИЧЕГО не включает и не выключает) поднимаются наверх меню в этом порядке, всё
# остальное сдвигается вниз БЕЗ сокрытия (см. get_main_menu_kb).
FORUM_DAY_MENU_PRIORITY_KEYS: tuple[str, ...] = (
    "menu_checkin_qr", "menu_program", "menu_important", "menu_sos",
)

# quick-260916: inline (not reply-keyboard) caption sent alongside the delegate's welcome-back
# message to an admin — single source shared with handlers/registration.py so the literal is
# never typed twice; see ADMIN_MISC_BUTTON_TEXTS below for why it matters outside registration.
ADMIN_REREG_BUTTON_TEXT = "\U0001f504 Пройти регистрацию заново"

# quick-260916: captions that LOOK like a button tap but have no downstream `F.text` handler
# (e.g. the admin-rereg button above is inline-only — a callback_query, never a text message).
# Kept separate from `all_menu_button_texts()`: an admin settings guard that sees one of THESE
# strings can only refuse to save it and explain, never "let the real handler run".
ADMIN_MISC_BUTTON_TEXTS: frozenset[str] = frozenset({ADMIN_REREG_BUTTON_TEXT})


def all_menu_button_texts() -> frozenset[str]:
    """Every caption the persistent main-menu reply keyboard can show right now, RU+EN,
    flattened into one set. Single source of truth used both to BUILD the keyboard (via
    MENU_TEXTS above) and to GUARD against silently saving a stray button tap as free text
    elsewhere (handlers/admin_settings.py::settings_edit_value) — the admin's reply keyboard
    stays the main menu while they type a setting value (settings edit never sends its own
    reply keyboard), so a habitual tap on e.g. "🪙 Мои монеты" sends its caption here as a
    normal text message."""
    texts: set[str] = set()
    for values in MENU_TEXTS.values():
        texts |= set(values)
    return frozenset(texts)


async def get_main_menu_kb(telegram_id: int | None = None) -> ReplyKeyboardMarkup:
    # Квик 260912 (W5, Задача 3): lang_module_on резолвится ПЕРВЫМ (раньше жил ниже, рядом с
    # miniapp_on/faq_on) — он нужен, чтобы решить, требуется ли единый `get_user` ниже, наравне
    # с cities_module_on(). Каждый резолв — в своём try/except, тот же fail-soft идиом, что и
    # остальные в этой функции: сбой чтения значит «нет перевода/города», меню цело.
    lang_module_on = False
    try:
        lang_module_on = await get_setting_typed("delegate_lang_enabled") == "on"
    except Exception as e:
        logger.error(f"get_main_menu_kb: delegate_lang_enabled resolve failed: {e}")
        lang_module_on = False

    cities_on = False
    try:
        cities_on = await cities_module_on()
    except Exception as e:
        logger.error(f"get_main_menu_kb: cities_module_on resolve failed: {e}")
        cities_on = False

    # Один `get_user` на ВСЕ резолвы ниже (город + язык + делегация вуза) — не три отдельных
    # чтения. Нет telegram_id (legacy-тесты, зовущие get_main_menu_kb() голым) — `get_user`
    # не зовётся вовсе. Делегации вузов (D-08): раньше чтение шло только при включённых
    # городах/языке; теперь всегда при telegram_id — иначе гейт игры ниже не узнал бы делегата.
    user = None
    if telegram_id is not None:
        try:
            user = await get_user(telegram_id)
        except Exception as e:
            logger.error(f"get_main_menu_kb: get_user failed for {telegram_id}: {e}")
            user = None

    # Делегации вузов (D-08): по умолчанию без игры — у делегата вуза кнопки монет/заданий
    # прячутся, пока менеджер не включил «🎮 Геймификация для делегатов». Рейтинг чата не
    # трогаем. Fail-soft: сбой чтения = кнопки на месте, меню важнее гейта.
    game_hidden = False
    try:
        if user and user.get("delegation"):
            game_hidden = (await get_setting_typed("delegation_game_enabled")) != "on"
    except Exception as e:
        logger.error(f"get_main_menu_kb: delegation game gate resolve failed: {e}")
        game_hidden = False

    # Phase 09.2 (B): city resolve failure must not break the menu -- buttons matter more than
    # the city, so it fails soft to code=None (global values), same idiom as
    # handlers/user_actions.py::show_game_tasks.
    code = None
    try:
        if cities_on:
            code = normalize_city(user.get("event_city") if user else None)
    except Exception as e:
        logger.error(f"get_main_menu_kb: city resolve failed for {telegram_id}: {e}")
        code = None

    # Квик 260912 (W5, Задача 3): язык делегата резолвится ровно как везде в проекте --
    # `services.i18n.resolve_lang`, `language_code` клиента здесь намеренно не передаётся
    # (D-06 уже отработал на /start, здесь ничего не угадываем повторно). Исход "ask"
    # (модуль включён, выбор ещё не сохранён) трактуется как "ru" -- то же самое, что и любая
    # ошибка резолюции: меню важнее языка.
    lang = "ru"
    try:
        if lang_module_on:
            stored_lang = user.get("lang") if user else None
            if resolve_lang(True, stored_lang, None) == "en":
                lang = "en"
    except Exception as e:
        logger.error(f"get_main_menu_kb: lang resolve failed for {telegram_id}: {e}")
        lang = "ru"

    # Phase 19 (D-10, WR-05): miniapp_enabled resolved ONCE before the loop (same idiom as the
    # city code above) -- a single extra DB read per menu render, not one per button. Fail-soft:
    # a read failure means "no button", never a broken menu.
    miniapp_on = False
    try:
        miniapp_on = await get_setting_typed("miniapp_enabled") == "on"
    except Exception as e:
        logger.error(f"get_main_menu_kb: miniapp_enabled resolve failed: {e}")
        miniapp_on = False

    # Quick 260906-8uq (FAQ-01..06, T-19-54 idiom): one extra read before the loop -- fail-soft
    # (a read error means "no button", never a broken menu), same shape as miniapp_on above.
    faq_on = False
    try:
        faq_on = await has_faq_for_city(code)
    except Exception as e:
        logger.error(f"get_main_menu_kb: has_faq_for_city resolve failed for {telegram_id}: {e}")
        faq_on = False

    # Квик 260923 (форум-чекин, D-03): checkin_qr_enabled — тот же приём, что и miniapp_on
    # выше (одно чтение до цикла, fail-soft к False). Проверка «одобрена ли заявка» здесь
    # НЕ делается — это гейт клика (handlers/user_actions.py::ensure_registered), не гейт
    # видимости кнопки, тот же баланс "лишнее чтение user" vs "менеджер выключил", что у
    # прочих module-флагов этой функции.
    checkin_qr_on = False
    try:
        checkin_qr_on = await get_setting_typed("checkin_qr_enabled") == "on"
    except Exception as e:
        logger.error(f"get_main_menu_kb: checkin_qr_enabled resolve failed: {e}")
        checkin_qr_on = False

    # Тип события — одно чтение до цикла, fail-soft к форумным подписям.
    conference = False
    try:
        conference = await get_setting_typed("event_type") == "conference"
    except Exception as e:
        logger.error(f"get_main_menu_kb: event_type resolve failed: {e}")
        conference = False

    # D-29 (объединённая кнопка «📅 Программа форума»): кнопка видна, пока есть ЧТО показать —
    # фото (СВОЙ per_city файл ИЛИ общий, «если загружено») ИЛИ у города делегата есть хотя бы
    # одна сессия программы (запасной текстовый вид). `code` — `None`, когда модуль городов
    # выключен (см. выше), но у сессий/фото «нет города» не бывает — `has_program_content`
    # сама резолвит `default_city_code()` (тот же однocity-фоллбэк, что использует админский
    # экран `handlers/admin_program.py._resolve_city_for_screen`). Одна проверка вместо двух
    # независимых — см. `services.program.has_program_content` docstring.
    program_photo_on = False
    try:
        from services.program import has_program_content
        program_photo_on = await has_program_content(code)
    except Exception as e:
        logger.error(f"get_main_menu_kb: has_program_content resolve failed: {e}")
        program_photo_on = False

    schedule_on = program_photo_on

    # Форум-ночь п.7 («❗ Важное»): кнопка только пока сегодня БЫЛА хоть одна важная рассылка
    # этому делегату (database.db.has_important_today) — тот же приём, что у schedule_on выше.
    # Нет telegram_id (легаси-вызов без аргумента) -> нечего проверять, кнопки не будет.
    important_on = False
    if telegram_id is not None:
        try:
            important_on = await has_important_today(telegram_id, msk_now().strftime("%Y-%m-%d"))
        except Exception as e:
            logger.error(f"get_main_menu_kb: has_important_today resolve failed for {telegram_id}: {e}")
            important_on = False

    # Форум-ночь п.8 (идея №19, SOS): кнопка только в дни форума города — тот же приём, что
    # schedule_on выше (единственное чтение до цикла, fail-soft к False, город без фолбэка на
    # default_city_code() не бывает — SOS привязывается к конкретному чату конкретного города).
    sos_on = False
    try:
        sos_city = code if code is not None else default_city_code()
        from services.sos import is_sos_active_for_city
        sos_on = await is_sos_active_for_city(sos_city)
    except Exception as e:
        logger.error(f"get_main_menu_kb: is_sos_active_for_city resolve failed for {telegram_id}: {e}")
        sos_on = False

    # Идея №1 бэклога чек-ина (режим «день форума»): тот же приём, что sos_on выше — одно
    # чтение до цикла, fail-soft к False (сбой резолва = обычный порядок меню, кнопки важнее
    # раскладки). Ничего не прячет и не показывает сама по себе — только переставляет ниже
    # уже собранный список кнопок, прошедших свои обычные гейты.
    forum_day_on = False
    try:
        forum_day_city = code if code is not None else default_city_code()
        from services.forum_day_menu import is_forum_day_menu_active_for_city
        forum_day_on = await is_forum_day_menu_active_for_city(forum_day_city)
    except Exception as e:
        logger.error(f"get_main_menu_kb: is_forum_day_menu_active_for_city resolve failed for {telegram_id}: {e}")
        forum_day_on = False

    collected: list[tuple[str, str]] = []
    for key, text in MENU_BUTTONS:
        if conference:
            text = CONFERENCE_MENU_LABELS.get(key, text)
        # menu_* is a registry `enum` key (options ["on","off"], default "on") -- the enum
        # branch of `_parse_setting` is `raw if raw else default`, so an unset/empty stored
        # value resolves to "on" exactly like the old `val is None or val == "on"` idiom;
        # any other stored value (including junk) resolves to itself and fails the `== "on"`
        # check, hiding the button exactly as before.
        val = await get_setting_typed_for_city(key, code)
        if val == "on":
            # Phase 19 (D-10, T-19-54): menu_miniapp needs TWO extra gates on top of the
            # ordinary menu toggle — the app itself must be enabled, and a public URL must be
            # configured (empty URL means no entry points exist at all, Pitfall 10). Every
            # other button is untouched by this branch.
            if key == "menu_miniapp" and not (miniapp_on and config.DASHBOARD_PUBLIC_URL):
                continue
            # Quick 260906-8uq: пустой FAQ (ни одного включённого пункта, ни общего, ни
            # своего города) — кнопки нет; появляется сама, как только менеджер завёл первый
            # пункт (has_faq_for_city).
            if key == "menu_faq" and not faq_on:
                continue
            # Делегации вузов (D-08): по умолчанию без игры — делегат вуза не видит монеты и
            # задания, пока тумблер выключен; рейтинг чата не трогаем (см. game_hidden выше).
            if key in ("menu_coins", "menu_game_tasks") and game_hidden:
                continue
            # D-29: вторая половина гейта — сама кнопка value=="on" (уже проверено выше)
            # недостаточна, пока нет ни фото программы, ни хотя бы одной сессии в ней.
            if key == "menu_program" and not (program_photo_on or schedule_on):
                continue
            # Phase 27 (27-04): вторая половина гейта — сама кнопка value=="on" (проверено
            # выше общей веткой `if val == "on"`) недостаточна, пока не включён модуль.
            if key == "menu_lang" and not lang_module_on:
                continue
            # Квик 260923 (форум-чекин, D-03): вторая половина гейта — сама кнопка value=="on"
            # недостаточна, пока менеджер не включил модуль checkin_qr_enabled.
            if key == "menu_checkin_qr" and not checkin_qr_on:
                continue
            # Форум-ночь п.7: вторая половина гейта — сама кнопка value=="on" недостаточна,
            # пока сегодня не было ни одной важной рассылки этому делегату.
            if key == "menu_important" and not important_on:
                continue
            # Форум-ночь п.8 (SOS): вторая половина гейта — сама кнопка value=="on"
            # недостаточна вне дней форума города.
            if key == "menu_sos" and not sos_on:
                continue
            # Квик 260912 (W5, Задача 3): перевод подписи в ОДНОМ месте, прямо перед
            # добавлением кнопки -- не через services.i18n.tr() (та лезла бы в UI_EN/tr_map,
            # подписей меню там нет и быть не должно, см. i18n_ui_en.py::MENU_EN).
            collected.append((key, MENU_EN.get(text, text) if lang == "en" else text))

    # Идея №1 бэклога чек-ина: в режиме «день форума» четыре приоритетные кнопки (та из них,
    # что вообще прошла свой гейт выше) поднимаются наверх в фиксированном порядке
    # FORUM_DAY_MENU_PRIORITY_KEYS, остальные остаются в прежнем взаимном порядке следом —
    # сдвиг вниз, не скрытие. Вне режима (форум не сегодня, тумблер выключен, сбой резолва)
    # `collected` не трогается — порядок меню байт-в-байт прежний.
    if forum_day_on:
        priority_rank = {k: i for i, k in enumerate(FORUM_DAY_MENU_PRIORITY_KEYS)}
        priority = sorted(
            (item for item in collected if item[0] in priority_rank),
            key=lambda item: priority_rank[item[0]],
        )
        rest = [item for item in collected if item[0] not in priority_rank]
        collected = priority + rest

    kb = ReplyKeyboardBuilder()
    for _key, resolved_text in collected:
        kb.button(text=resolved_text)
    # Persistent "upload receipt" entry — only while the user still owes one.
    # Lazy import avoids a circular import (payment imports get_main_menu_kb); fail-soft.
    if telegram_id is not None:
        try:
            from handlers.payment import should_offer_receipt_upload
            if await should_offer_receipt_upload(telegram_id):
                payment_text = "💳 Оплата"
                kb.button(text=MENU_EN.get(payment_text, payment_text) if lang == "en" else payment_text)
        except Exception:
            pass
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

# --- Почему включённая кнопка меню сейчас не видна делегату ---
#
# Один реестр на ВСЕ кнопки со вторым гейтом в `get_main_menu_kb` выше (`if key == ... and
# not ...: continue`): экран «🔘 Кнопки меню» (handlers/admin_reg_config.py) пишет рядом с
# включённой кнопкой «сейчас скрыта: <причина>». Функция получает город (None — общие
# значения) и возвращает причину словами или None (кнопка видна). Новый гейт без записи здесь
# роняет tests/test_admin_checkin_labels_260924.py::test_every_menu_gate_has_hidden_reason.

async def _hidden_miniapp(code: str | None) -> str | None:
    if await get_setting_typed("miniapp_enabled") != "on":
        return "выключено приложение (Mini App)"
    if not config.DASHBOARD_PUBLIC_URL:
        return "у приложения не задан адрес — нужен разработчик"
    return None


async def _hidden_faq(code: str | None) -> str | None:
    if await has_faq_for_city(code):
        return None
    return "нет ни одного включённого вопроса в «❓ Частые вопросы»"


async def _hidden_program(code: str | None) -> str | None:
    from services.program import has_program_content  # то же правило, что у делегата
    if await has_program_content(code):
        return None
    return "не загружено фото программы и нет ни одной сессии"


async def _hidden_lang(code: str | None) -> str | None:
    if await get_setting_typed("delegate_lang_enabled") == "on":
        return None
    return "выключен английский язык анкеты — «📝 Анкета» → «🌐 Английский язык анкеты»"


async def _hidden_checkin_qr(code: str | None) -> str | None:
    if await get_setting_typed("checkin_qr_enabled") == "on":
        return None
    return "выключен QR для входа на форум — «🎪 Форум: функции»"


async def _hidden_important(code: str | None) -> str | None:
    from database.db import has_any_important_today
    if await has_any_important_today(msk_now().strftime("%Y-%m-%d")):
        return None
    return "видна делегату только в день, когда ему пришла важная рассылка; сегодня их не было"


async def _hidden_sos(code: str | None) -> str | None:
    from services.sos import is_sos_active_for_city
    from services.reject_rules import forum_date_for
    sos_city = code if code is not None else default_city_code()
    if await is_sos_active_for_city(sos_city):
        return None
    date_str = await forum_date_for(sos_city)
    if date_str is None:
        return "не задана дата форума"
    return f"видна только в дни форума, начало {date_str}"


MENU_HIDDEN_REASONS = {
    "menu_miniapp": _hidden_miniapp,
    "menu_faq": _hidden_faq,
    "menu_program": _hidden_program,
    "menu_lang": _hidden_lang,
    "menu_checkin_qr": _hidden_checkin_qr,
    "menu_important": _hidden_important,
    "menu_sos": _hidden_sos,
}


async def menu_hidden_reason(key: str, code: str | None) -> str | None:
    """Причина, по которой включённая кнопка `key` сейчас не видна делегату (None — видна
    или у кнопки нет второго гейта). Fail-soft: сбой чтения — без пометки, экран цел."""
    fn = MENU_HIDDEN_REASONS.get(key)
    if fn is None:
        return None
    try:
        return await fn(code)
    except Exception as e:
        logger.error(f"menu_hidden_reason: {key} resolve failed: {e}")
        return None


# --- Registration Keyboards ---

def get_yes_no_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in YES_NO_OPTIONS:
        kb.button(text=opt)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

async def get_source_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    # REG-02: read through the registry accessor. The registry's list default is None
    # (not DEFAULT_SOURCE_OPTIONS), so the empty->DEFAULT_SOURCE_OPTIONS fallback guard is
    # kept here to preserve exact pre-migration behavior (T-06-11).
    items = await get_setting_typed("source_options")
    if not items:
        items = DEFAULT_SOURCE_OPTIONS
    for item in items:
        kb.button(text=item)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_education_status_kb(options: list[str] | None = None) -> ReplyKeyboardMarkup:
    """Phase 28 (28-01, R-A3 CONTEXT): `options=None` строит прежние три кнопки байт-в-байт
    (D-06) — вызывающий (handlers/registration.py) передаёт `await reg_engine.options(
    "education_status")`, который сам резолвит реестровый `education_status_options` с
    фолбэком на `EDUCATION_STATUS_OPTIONS`."""
    kb = ReplyKeyboardBuilder()
    for opt in (options if options is not None else EDUCATION_STATUS_OPTIONS):
        kb.button(text=opt)
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_universities_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for uni in config.UNIVERSITIES:
        kb.button(text=uni)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_local_committee_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for lc in LOCAL_COMMITTEE_OPTIONS:
        kb.button(text=lc)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


# --- Conference (RusCo) reg-flow keyboards ---

def get_department_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for d in DEPARTMENT_OPTIONS:
        kb.button(text=d)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_aiesec_role_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for r in AIESEC_ROLE_OPTIONS:
        kb.button(text=r)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_english_level_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for lvl in ENGLISH_LEVEL_OPTIONS:
        kb.button(text=lvl)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_arrival_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for a in ARRIVAL_OPTIONS:
        kb.button(text=a)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_housing_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for h in HOUSING_OPTIONS:
        kb.button(text=h)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_position_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for pos in POSITION_OPTIONS:
        kb.button(text=pos)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_attendance_format_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in ATTENDANCE_FORMAT_OPTIONS:
        kb.button(text=opt)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_informal_day_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in INFORMAL_DAY_OPTIONS:
        kb.button(text=opt)
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_course_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in COURSE_OPTIONS:
        kb.button(text=opt)
    kb.adjust(3, 2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

# --- Info & Misc ---

def get_info_submenu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Дата и время", callback_data="info_date")
    builder.button(text="Место проведения", callback_data="info_place")
    builder.adjust(1)
    return builder.as_markup()

def _normalize_url(raw: str) -> str | None:
    """WR-04: admin contact settings are free text — a bare "@channel" or "vk.com/x" (no
    scheme) makes Telegram reject the whole message with BUTTON_URL_INVALID. Normalize to a
    valid URL; return None when there's nothing usable so the button is simply omitted."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if raw.startswith(("http://", "https://", "tg://")):
        return raw
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}"
    return f"https://{raw}"


def get_socials_kb(tg_url: str, vk_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    tg = _normalize_url(tg_url)
    vk = _normalize_url(vk_url)
    if tg:
        builder.button(text="Группа в Telegram", url=tg)
    if vk:
        builder.button(text="Группа во ВКонтакте", url=vk)
    return builder.as_markup()

def get_cancel_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Отмена")
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_confirm_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Всё верно")
    kb.button(text="Изменить")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_phone_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="\U0001f4f1 Поделиться контактом", request_contact=True)
    kb.button(text="Пропустить")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_skip_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Пропустить")
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)
