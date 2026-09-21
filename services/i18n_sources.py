"""Phase 27 (27-01, LANG-10/LANG-08) — единственное перечисление того, что в проекте считается
делегатским текстом анкеты. Нужен и оффлайн-замеру `tools/i18n_probe.py` (этот план), и
`bulk_seed`/gate-у очереди перевода (план 27-03) — второго списка источников в проекте быть не
должно (см. `tools/i18n_probe.py`, которое зовёт `corpus()` отсюда, а не строит свой).

Модуль aiogram-free: из `handlers` не импортирует НИЧЕГО (любой `import handlers.x` тянет
`handlers/__init__.py`, а тот — `registration, user_actions, admin, payment`, то есть весь бот
на бот-фреймворке; докстринг `reg_engine.py:11-19` держит тот же инвариант, и этот модуль ему
следует). Разрешённые импорты — `settings_schema`, `reg_engine`, `reg_labels`, `reg_options`,
`cities`, `config`, `database.db`, `payment_options` (Квик 260917-en — тот же aiogram-free
корневой модуль, что использует `handlers/payment.py`, нужен для разбора `payment_options`
на отдельные подписи тарифов, см. `payment_option_texts()` ниже).

## Граница «делегатское / админское» (LANG-08, расширено Квик 260917-en: полный чат бота)

`DELEGATE_GROUPS` — группы реестра настроек (`settings_schema.SETTINGS_SCHEMA`), чьи `text`/
`list`-ключи считаются текстом чата делегата и подлежат переводу:

- `reg_prompts` — тексты вопросов анкеты (`reg_prompt_{step}`, генерируются циклом в конце
  `settings_schema.py`);
- `reg` — остальной текст регистрации: подписи кнопок мастера, тексты после одобрения/отказа,
  списки вариантов с реестровым override (`source_options`, `city_options`, ...);
- `party` — тексты party-трека (форк «Полная регистрация / Гости»);
- `event` — приветствие `/start`, информация о мероприятии, контакты, empty-state'ы
  (программа/спикеры/FAQ), «Задать вопрос» (Квик 260917-en: владелец снял ограничение
  27-CONTEXT.md «делегат может увидеть это, не начав анкету — v2» — приёмка 17.09 показала,
  что именно эти тексты (приветствие) видны раньше всего и остались русскими);
- `game` — геймификация: список заданий, карточка задания, сдача, баланс/история монет,
  рейтинг, рефералка (минус визард СОЗДАНИЯ задания — экран МЕНЕДЖЕРА, см.
  `_ADMIN_ONLY_GAME_KEYS` ниже);
- `pay` — экраны оплаты: выбор варианта, реквизиты, «оплачу позже», подтверждение чека, текст
  напоминания (Квик 260917-en: снят статус follow-up из Q7/LANG-07 27-CONTEXT.md — владелец
  явно включил оплату в объём этой задачи), минус чистые идентификаторы/данные, не язык (см.
  `_NON_LANGUAGE_PAY_KEYS` ниже).

Группы НЕ в списке — и почему:

- `menu` — это НЕ подписи кнопок меню (те — литералы `keyboards/builders.py::MENU_BUTTONS` +
  `i18n_ui_en.MENU_EN`, переводятся отдельным точечным словарём при рендере клавиатуры).
  Ключи группы `menu` — тумблеры «показывать ли пункт» на АДМИНСКОМ экране (`type: "enum"`,
  их `label` менеджеру, не делегату) — `delegate_registry_keys()` их и так не берёт (фильтр
  по `type in ("text", "list")`), группа явно не добавлена, чтобы это было видно без чтения кода.
- `miniapp` — тексты входа в Mini App (`miniapp_open_text`/`miniapp_open_button`/
  `miniapp_disabled_text`) переводятся точечно в `handlers/user_actions.py::open_miniapp_button`
  через ярус A (`i18n_ui_en.UI_EN`) — то же решение, что уже было принято Квик 260915-skg, не
  меняем: это три строки, рукописный перевод для них надёжнее машинного (короткие, участвуют в
  визуальном UAT постоянно).
- `consent` — LANG-09/D-04: согласия НЕ переводятся машинно. Английская редакция (если вообще
  нужна — вопрос юридический, не технический) вводится менеджером руками в плане 27-06.
- `sheets`, `dashboard`, `apps`, `system`, `roles`, `toggles` — чисто административные
  поверхности, делегат их не видит никогда.

`ADMIN_KEYS_IN_DELEGATE_GROUPS` — явное исключение внутри `DELEGATE_GROUPS`: ключи, которые
физически лежат в группе `reg` (потому что технически это текст, связанный с регистрацией), но
по смыслу — метка для МЕНЕДЖЕРА в карточке заявки, не текст ДЛЯ ДЕЛЕГАТА. Правило: «ключ группы
`reg` с подстрокой `admin` в имени → админский» — вычислено автоматически, а не выписано руками,
чтобы новый ключ с тем же паттерном не пролез в корпус молча.

`_ADMIN_ONLY_GAME_KEYS` (Квик 260917-en) — та же граница внутри группы `game`, но БЕЗ общего
паттерна имени (в отличие от `reg`, здесь нет подстроки вроде «admin» — визард создания задания
называется `game_task_title_prompt`/`game_wizard_publish_btn`, не «admin_...»), поэтому список
явный, а не вычисленный: `game_task_title_prompt`/`game_task_photo_prompt` — шаги визарда
СОЗДАНИЯ задания (видит менеджер, не делегат), `game_task_preview_intro`/
`game_wizard_preview_title`/`game_wizard_publish_btn` — заголовки менеджерского экрана «👁 Как
видит делегат» и кнопка публикации, `coins_manual_amount_presets` — CSV чисел для
кнопок-пресетов менеджерского визарда начисления монет (не язык вообще).

`_NON_LANGUAGE_PAY_KEYS` (Квик 260917-en) — в группе `pay` исключены `payment_requisites_by_lc`
(формат `ЛК | реквизиты` построчно — при машинном переводе ВСЕЙ строки как одного текста хеш
переведённой строки никогда не совпадёт с хешем ПОДСТРОКИ реквизитов, которую реально показывает
`handlers/payment.py::_resolve_requisites` делегату — перевод осел бы в `translations` мёртвым
грузом, ни разу не найденным `tr()`; отдельный корпус под подстроки — за рамками этой правки,
задокументировано как известное ограничение) и `penalty_schedule` (формат `дата|сумма` — числа
и даты, machine-перевод не изменил бы ни одного символа, лишняя строка в очереди). Сама
`payment_requisites` (одна строка на всё событие, БЕЗ построчного формата) переводится как есть.

## Два яруса (A-02 в CONTEXT.md)

Ярус A (служебные слова — «Готово», «Отмена», «Пропустить», «Другое», «Да»/«Нет», тексты ошибок
валидации `_SKIP_TEXT_ERRORS`/`_BESPOKE_CHOICE`/`_MEMBERSHIP_STEPS`) в этот корпус НЕ входит:
эти литералы участвуют в жёстких сравнениях (`F.text == "Отмена"`) и в фильтрах aiogram — их
перевод обязан быть детерминированным и рукописным (`i18n_ui_en.py`, план 27-02), а не машинным
(машинный перевод недетерминирован между версиями модели — фильтр мог бы «расклеиться» после
апдейта движка). `code_literals()` ниже берёт только ярус B (контент менеджера: тексты
вопросов, списки вариантов).
"""
from __future__ import annotations

import logging

from cities import CITIES, split_per_city_key
import cities
from config import config
from settings_schema import SETTINGS_SCHEMA
import reg_engine
import reg_labels
import reg_options
import payment_options

logger = logging.getLogger(__name__)

DELEGATE_GROUPS = ("reg_prompts", "reg", "party", "event", "game", "pay")

# Правило «`admin` в имени ключа группы `reg`» найдено вычислением, не выписано руками. На
# 06.09.2026 это ровно три ключа-метки для менеджера в карточке заявки:
#   reg_edited_admin_label       — «Изменена {date}»
#   reg_resubmit_admin_label     — «Повторная подача»
#   reg_prev_reject_admin_label  — «Ранее отклонена: {reason}»
# Ни один из них делегат не видит — это заметки в карточке модерации.
ADMIN_KEYS_IN_DELEGATE_GROUPS: frozenset[str] = frozenset(
    key for key, spec in SETTINGS_SCHEMA.items()
    if spec.get("group") in DELEGATE_GROUPS and "admin" in key
)

# Квик 260917-en: у группы `game` нет общего паттерна имени для «это менеджерский экран»
# (в отличие от `reg`, где подстрока «admin» вычислима) — визард СОЗДАНИЯ задания называется
# `game_task_title_prompt`/`game_wizard_publish_btn`, делегат эти экраны не видит никогда
# (см. докстринг модуля выше). Список явный и должен обновляться руками при добавлении новых
# менеджерских экранов в группу `game`.
#
# Phase 32 (32-02, D-04): `wave_end_manager_text` — сообщение МЕНЕДЖЕРУ о конце волны (топ по
# уже проверенным сдачам + счётчик непроверенных), делегат его никогда не видит — та же логика
# исключения, что у визарда создания задания выше.
_ADMIN_ONLY_GAME_KEYS: frozenset[str] = frozenset({
    "game_task_title_prompt", "game_task_photo_prompt", "game_task_preview_intro",
    "game_wizard_preview_title", "game_wizard_publish_btn", "coins_manual_amount_presets",
    "wave_end_manager_text",
})

# Квик 260917-en: `payment_requisites_by_lc`/`penalty_schedule` — построчные данные (ЛК+реквизиты
# / дата+сумма), не естественный язык, см. докстринг модуля выше про хеш-адресацию подстрок.
# `payment_options` — тот же класс проблемы: строка «Название | Цена[ | треки]» целиком НЕ
# совпадает по хешу с ПОДСТРОКОЙ «Название», которую реально показывает делегату
# `handlers/payment.py` (кнопка выбора тарифа, {option} в шаблоне экрана оплаты) — обычный
# построчный сбор дал бы мёртвый перевод, который tr() никогда не найдёт. Здесь исключаем из
# общего построчного сбора, но не теряем: `payment_option_texts()` ниже — отдельный извлекатель
# ИМЕННО подстроки-названия, как `city_texts()` для городов.
_NON_LANGUAGE_PAY_KEYS: frozenset[str] = frozenset({
    "payment_requisites_by_lc", "penalty_schedule", "payment_options",
})

# Квик 260917-en: `contact_person`/`contact_vk`/`contact_tg` — юзернейм/URL, не текст на языке
# (машинный перевод URL/@username в лучшем случае no-op, в худшем — риск порчи ссылки).
_NON_LANGUAGE_EVENT_KEYS: frozenset[str] = frozenset({"contact_person", "contact_vk", "contact_tg"})

_NON_DELEGATE_TEXT_KEYS: frozenset[str] = (
    ADMIN_KEYS_IN_DELEGATE_GROUPS | _ADMIN_ONLY_GAME_KEYS | _NON_LANGUAGE_PAY_KEYS
    | _NON_LANGUAGE_EVENT_KEYS
)

# Динамические ключи вне SETTINGS_SCHEMA — только эти два префикса (help_text/prompt
# докстринги reg_engine.py явно говорят «в SETTINGS_SCHEMA НЕ заводится»).
_DYNAMIC_PREFIXES = ("reg_prompt_", "reg_help_")
_TRACK_SUFFIXES = ("__party", "__short")


def delegate_registry_keys() -> frozenset[str]:
    """Ключи `SETTINGS_SCHEMA` из `DELEGATE_GROUPS` типа text/list, минус админские метки и
    нелингвистические поля (см. `_NON_DELEGATE_TEXT_KEYS`, докстринг модуля)."""
    return frozenset(
        key for key, spec in SETTINGS_SCHEMA.items()
        if spec.get("group") in DELEGATE_GROUPS
        and spec.get("type") in ("text", "list")
        and key not in _NON_DELEGATE_TEXT_KEYS
    )


def _strip_dynamic_suffixes(key: str) -> str:
    """Снимает городской, потом трековый хвост — реальные ключи composитны:
    `reg_prompt_goal__party__city__spb`. Городской хвост снимается через `cities.
    split_per_city_key` (та же функция, что использует остальной проект для этой операции —
    T-092-01/V5: код города только из закрытого множества `city_codes()`, а не с любым
    `[a-z0-9_]+`), трековый — сравнением с известными суффиксами."""
    split = split_per_city_key(key)
    base = split[0] if split else key
    for suffix in _TRACK_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def is_delegate_dynamic_key(key: str) -> bool:
    """Истинно для ключей `bot_settings`, которых нет в `SETTINGS_SCHEMA` НАПРЯМУЮ, но которые
    всё равно являются делегатским текстом анкеты: `reg_prompt_*`/`reg_help_*` (динамические
    оверрайды текстов вопросов/подсказок) и любые их трек-/городские варианты. Ключи, которые
    УЖЕ есть в реестре под своим полным именем (например `approve_text__party` — это отдельная
    явная запись `SETTINGS_SCHEMA`, не производная), тоже проходят — через
    `delegate_registry_keys()`, ЛИБО после снятия городского хвоста."""
    base = _strip_dynamic_suffixes(key)
    if base in delegate_registry_keys():
        return True
    return base.startswith(_DYNAMIC_PREFIXES)


def code_literals() -> list[tuple[str, str]]:
    """Ярус B из кода (не из реестра): дефолтные тексты вопросов, подсказки формата, подписи
    шагов, литеральные списки вариантов, дефолты конфигурируемых списков, ВУЗы. Ярус A
    (`_SKIP_TEXT_ERRORS`, `_BESPOKE_CHOICE`, `_MEMBERSHIP_STEPS`, служебные слова) сюда
    намеренно не включается — см. докстринг модуля."""
    items: list[tuple[str, str]] = []

    for step_key, text in reg_engine.PROMPT_DEFAULTS.items():
        items.append((f"lit:PROMPT_DEFAULTS.{step_key}", text))

    for step_key, text in reg_engine.STEP_HELP.items():
        items.append((f"lit:STEP_HELP.{step_key}", text))

    for step_key, text in reg_engine.STEP_HELP_EXAMPLES.items():
        items.append((f"lit:STEP_HELP_EXAMPLES.{step_key}", text))

    for step_type, text in reg_engine._GENERIC_FALLBACK_LABEL.items():
        items.append((f"lit:_GENERIC_FALLBACK_LABEL.{step_type}", text))

    for setting_key, label in reg_labels.REG_LABELS.items():
        items.append((f"lit:REG_LABELS.{setting_key}", label))

    # UAT-фикс (стенд, lang=en, 27-05): подписи сводки анкеты (_build_summary) — ОТДЕЛЬНЫЙ
    # голый список bare-строк ("Возраст", "Город", ...), НЕ REG_LABELS (те несут эмоджи-префикс,
    # «🎂 Возраст» — другой src_hash). Без этой регистрации handlers/registration.py::_build_summary
    # честно зовёт tr_text(label, ...) на отправке, но карта переводов эту строку никогда не
    # видела — фоновый переводчик её не переводил, tr() fail-soft отдаёт русский (D-04). Тот же
    # класс дыры, что LANG-06 уже закрыла для ВАРИАНТОВ ответа (option_pairs) — здесь для МЕТОК.
    for label, _column in reg_engine._SUMMARY_FIELD_LABELS:
        items.append((f"lit:_SUMMARY_FIELD_LABELS.{_column}", label))
    # "Работа"/"Амбассадор"/"Резюме" — computed-строки summary_fields() поверх списка выше (bool
    # -> "Да"/"Нет", факт вложения файла), та же дыра корпуса.
    items.append(("lit:reg_engine.summary_fields.work_status", "Работа"))
    items.append(("lit:reg_engine.summary_fields.ambassador", "Амбассадор"))
    items.append(("lit:reg_engine.summary_fields.resume", "Резюме"))
    items.append(("lit:reg_engine.summary_fields.resume_attached", "прикреплено файлом"))

    # Все UPPERCASE-списки reg_options — перебор через vars(), а не 18 имён руками: новый
    # список вариантов, добавленный когда-нибудь в reg_options.py, попадёт в корпус сам.
    # Большинство элементов — простые строки; PARTY_TRACK_OPTIONS — список (код, подпись) —
    # берём только строковые «хвостовые» элементы кортежа (подпись), не код.
    for name, value in vars(reg_options).items():
        if not name.isupper() or not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str):
                items.append((f"lit:reg_options.{name}", item))
            elif isinstance(item, (tuple, list)):
                for sub in item[1:]:
                    if isinstance(sub, str):
                        items.append((f"lit:reg_options.{name}", sub))

    for step_key, (opt_key, defaults) in {
        **reg_engine.SELECT_CONFIG, **reg_engine.MULTI_CONFIG,
    }.items():
        for text in defaults:
            items.append((f"lit:{opt_key}", text))

    for uni in config.UNIVERSITIES:
        items.append(("lit:config.UNIVERSITIES", uni))

    # Quick 260906 (UAT-фикс 27-05): /start-литералы handlers/registration.py, найденные
    # стендовым UAT (делегат видел русский, даже когда bulk_seed уже перевёл весь остальной
    # корпус) — этот модуль НЕ импортирует handlers ни при каких условиях (докстринг модуля
    # выше, тот же инвариант, что у reg_engine.py) -> строки продублированы буквально, а не
    # импортированы из DEFAULT_START_REGISTERED_TEXT/DEFAULT_START_RETURNING_TEXT. Правка
    # текста в registration.py без зеркальной правки здесь тихо расходится с корпусом —
    # `tests/test_i18n_sources_27.py` держит обе строки байт-в-байт списком, «сверено на дату
    # плана» (тот же приём, что уже используется в i18n_ui_en.py для validate_date_range).
    # DEFAULT_START_RETURNING_TEXT несёт {season} — плейсхолдер сентинелится автоматически
    # (services/i18n_glossary.py::_TAG_OR_PLACEHOLDER_RE), отдельно защищать не нужно.
    items.append((
        "lit:registration.DEFAULT_START_REGISTERED_TEXT",
        "С возвращением! Ты уже зарегистрирован(а) — всё нужное в меню ниже \U0001f447",
    ))
    items.append((
        "lit:registration.DEFAULT_START_RETURNING_TEXT",
        "С возвращением! Ты уже был(а) с нами на {season}. Давай обновим анкету — "
        "большинство ответов уже заполнено, останется только подтвердить \U0001f447",
    ))
    items.append((
        "lit:registration._start_registration_flow",
        "Отлично, начинаем регистрацию.",
    ))
    items.append((
        "lit:registration._start_registration_flow",
        "Отлично, ты пришёл по приглашению друга. Начинаем регистрацию.",
    ))

    # UAT run 2 (Quick 260906, второй проход после 2773791/44c77df/222340a): те же два aiogram-
    # free модуля (handlers/reg_resume.py, handlers/reg_flow.py, handlers/reg_consent.py,
    # handlers/registration.py) НЕ импортируются отсюда (докстринг модуля выше) — литералы
    # продублированы буквально, `tests/test_i18n_sources_27.py` держит их байт-в-байт списком.
    items.append((
        "lit:reg_resume.offer_resume",
        "У тебя есть незаконченная анкета — что дальше?",
    ))
    items.append((
        "lit:reg_resume.reg_resume_restart_yes",
        "Изменения отменены — анкета осталась прежней.",
    ))
    items.append((
        # Тот же литерал шлёт handlers/reg_handoff.py::reg_handoff_to_bot — один корпусный
        # источник на оба сайта, дедуп по strip()-нутому тексту в corpus() ниже.
        "lit:reg_resume.reg_resume_continue",
        "Черновик не найден — начни заново с /start.",
    ))
    items.append((
        "lit:reg_flow.rereg_start",
        "Ты уже зарегистрирован(а) на этот сезон",
    ))
    items.append((
        "lit:reg_flow.cancel_registration",
        "Точно отменить регистрацию? Все введённые ответы сотрутся.",
    ))
    items.append((
        "lit:reg_flow.cancel_registration_confirm",
        "Регистрация отменена. Чтобы начать заново, отправь /start.",
    ))
    items.append((
        "lit:reg_flow.process_resume",
        "Принимаются только PDF или DOCX. Прикрепи файл ещё раз.",
    ))
    items.append((
        "lit:reg_flow.process_resume",
        "❌ Файл слишком большой (максимум 10 МБ). Прикрепи резюме меньшего размера.",
    ))
    items.append((
        # НЕ совпадает с UI_EN («Напиши резюме текстом...» — текст ошибки validate_answer)
        # дословно: тут «Пришли...» — приглашение на невалидный тип апдейта (не текст ошибки
        # ввода), отдельный литерал, отдельная строка корпуса.
        "lit:reg_flow.process_resume_invalid",
        "Пришли резюме текстом или прикрепи файл (PDF или DOCX).",
    ))
    items.append((
        "lit:reg_flow.process_multi_ignore",
        "Отмечай варианты кнопками выше и нажми «Готово».",
    ))
    items.append((
        "lit:reg_consent.consent_renew_accept",
        "✅ Спасибо! Согласие обновлено.",
    ))
    items.append((
        "lit:registration._build_summary",
        "Проверь свои ответы:",
    ))
    # Задача 4 (известный остаток первого фикса, docstring `_ask_full_name_plain`): дефолт
    # override reg_prompt_full_name ушёл в корпус через `code_literals()` фикса 2773791
    # (override, когда он задан), но CALLER-SUPPLIED default (когда override НЕ задан,
    # `_prompt("full_name", ...)`) — нет, отдельный литерал того же вопроса.
    items.append((
        "lit:registration._ask_full_name_plain",
        "Напиши свои ФИО (Фамилия Имя Отчество):",
    ))

    # Квик 260917-en: полный чат бота на английском — литералы `handlers/user_actions.py`
    # (главное меню: инфо/программа/спикеры/контакты/рефералка/FAQ/вопрос менеджеру/
    # геймификация), `handlers/payment.py` (оплата) и `services/application_effects.py`
    # (отказ) — эти модули aiogram-зависимы, `i18n_sources.py` их не импортирует (докстринг
    # модуля), поэтому строки продублированы буквально, тем же приёмом, что и литералы
    # `registration`/`reg_flow`/`reg_resume`/`reg_consent` выше. `tests/test_i18n_sources_27.py`
    # держит их байт-в-байт списком.
    items.append(("lit:user_actions.info_date", "🗓 Дата пока уточняется. Скоро сообщим! 🙂"))
    items.append((
        "lit:user_actions.info_place",
        "📍 Место проведения в процессе подтверждения. Как только всё будет готово, мы напишем!",
    ))
    items.append((
        "lit:user_actions.show_info_menu_ready",
        "Информация о мероприятии пока заполняется.",
    ))
    items.append(("lit:user_actions.show_info_menu_ready", "Выбери, что тебя интересует:"))
    items.append((
        "lit:user_actions.gtask_open_archived",
        "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Задания», "
        "там актуальный список.",
    ))
    items.append((
        "lit:user_actions.mytask_submit_archived",
        "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Мои "
        "задания», там актуальный список.",
    ))
    # WR-08 (32-REVIEW.md): прямой callback по task_id (карточка/начало сдачи) обходил
    # проверку аудитории/волны, которую уже применяет список — общий алерт обеих точек.
    items.append((
        "lit:user_actions.task_not_visible",
        "Это задание сейчас тебе недоступно — загляни в «🎯 Задания».",
    ))
    items.append(("lit:user_actions.mytask_submit_active", "Уже отправлено, ожидай проверки"))
    items.append((
        "lit:user_actions.mytask_submit_limit",
        "Лимит попыток по этому заданию исчерпан ({limit}). Если считаешь, что "
        "это ошибка — напиши менеджеру через «❓ Задать вопрос».",
    ))
    items.append((
        "lit:user_actions.mytask_submit_deadline_passed",
        "⏰ Срок сдачи вышел. Отправить можно, но начислять коины будет решать менеджер.",
    ))
    items.append(("lit:user_actions.cancel_game_submit", "Действие отменено."))
    items.append((
        "lit:user_actions.receive_proof_unrecognized",
        "Не понял, пришли фото, документ, текст или ссылку.",
    ))
    items.append((
        "lit:user_actions.receive_proof_overflow",
        "Больше {max_parts} частей в одну сдачу не влезет — нажми «✅ Готово», "
        "менеджер уже увидит присланное.",
    ))
    items.append(("lit:user_actions.finalize_task_gone", "Это задание больше не доступно."))
    items.append((
        "lit:user_actions.finalize_race",
        "Уже отправлено — кто-то опередил на долю секунды. Обнови список заданий.",
    ))
    items.append(("lit:user_actions.gs_cancel", "Сдача отменена, части не сохранены."))
    items.append(("lit:user_actions.upload_receipt_not_owed", "Оплатили или оплата не требуется."))
    items.append(("lit:user_actions.process_question_text_only", "Пожалуйста, отправь вопрос текстом."))
    items.append((
        "lit:user_actions.process_question_no_admins",
        "Не удалось отправить вопрос, попробуйте позже.",
    ))
    items.append(("lit:user_actions.process_question_none_configured", "Администраторы не настроены."))
    items.append(("lit:user_actions.cancel_question", "Действие отменено."))
    items.append(("lit:user_actions.not_registered", "Чтобы пользоваться ботом, сначала нужно зарегистрироваться. Отправь команду /start."))

    # Phase 32 (32-06, D-24/D-29/D-32/D-38): кнопка рейтинга волны в списке заданий, отказ по
    # доступу к рейтингу вне волны и подтверждение выхода амбассадора — тот же приём.
    items.append(("lit:user_actions.wave_rating_button", "🏅 Рейтинг волны"))
    items.append((
        "lit:user_actions.wave_rating_not_eligible",
        "Рейтинг волны виден только участникам текущей волны амбассадоров.",
    ))
    items.append(("lit:user_actions.ambassador_leave_confirm_yes", "Да, выйти"))

    items.append(("lit:payment.receipt_bad_mime", "❌ Принимается только PDF-документ. Для скриншота используй функцию отправки фото."))
    items.append(("lit:payment.receipt_too_large_doc", "❌ Файл слишком большой (максимум 10 МБ). Пришли чек меньшего размера."))
    items.append(("lit:payment.receipt_too_large_photo", "❌ Изображение слишком большое (максимум 10 МБ). Пришли чек меньшего размера."))
    items.append(("lit:payment.receipt_rate_limited", "⏳ Слишком часто. Подожди пару секунд и попробуй снова."))
    items.append((
        "lit:payment.receipt_invalid",
        "❌ Отправь чек оплаты (PDF-документ или фото).\nИли /start — вернуться в меню "
        "(загрузить чек можно будет позже).",
    ))
    items.append((
        "lit:payment.tariff_wrong_track",
        "Этот вариант недоступен для твоего трека.",
    ))
    items.append(("lit:payment.tariff_unavailable", "Вариант больше не доступен."))
    items.append(("lit:payment.tariff_bad_value", "Некорректный вариант."))

    items.append(("lit:application_effects.default_reject_text", "К сожалению, твоя заявка отклонена."))
    items.append(("lit:reg_schema.default_approve_text", "Твоя заявка одобрена! Добро пожаловать 🎉"))
    items.append(("lit:reg_schema.default_approve_auto_text", "Заявка принята ✅ Всё получили — ждём тебя!"))
    items.append(("lit:reg_schema.default_bonus_caption", "\U0001f381 Бонус за регистрацию!"))

    return items


async def city_texts() -> list[tuple[str, str]]:
    """Названия городов мероприятия (Квик 260917-en, item 3 приёмки 17.09) — живут в таблице
    `cities` (`cities.py`), НЕ в `SETTINGS_SCHEMA`/`bot_settings` напрямую, поэтому
    `stored_delegate_texts()` их не видит вообще (тот сканирует только `bot_settings`).
    `cities.city_label(code)` уже резолвит override (`city_label__{code}`) поверх базового
    `cities.label` — берём РОВНО то, что реально покажется делегату на кнопке `_city_fork_kb`
    (`handlers/registration.py`), одной строкой на город. `ensure_cities_fresh` — та же
    осторожность, что у `enabled_cities()`: список городов меняется по ходу сезона, читать
    голый холодный `.env`-кэш здесь было бы неверно, если менеджер только что добавил город."""
    try:
        await cities.ensure_cities_fresh()
    except Exception as exc:  # noqa: BLE001 — намеренно широкий fail-soft (D-04)
        logger.warning("i18n_sources.city_texts: обновление списка городов не удалось (%s)", exc)
    result: list[tuple[str, str]] = []
    for code in cities.city_codes():
        try:
            label = await cities.city_label(code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("i18n_sources.city_texts: city_label(%r) не удалось (%s)", code, exc)
            continue
        if label:
            result.append((f"city_label__{code}", label))
    return result


async def payment_option_texts() -> list[tuple[str, str]]:
    """Названия тарифов оплаты (Квик 260917-en, item 4 приёмки 17.09) — `payment_options`
    исключён из общего построчного сбора (`_NON_LANGUAGE_PAY_KEYS`, см. докстринг там же),
    здесь читаем ту же настройку и парсим ЕЁ ЖЕ парсером (`payment_options.parse_options`,
    тот самый, что использует `handlers/payment.py` для рендера), чтобы источник корпуса не
    разошёлся с источником отображения ни на один символ. Fail-soft (D-04): нет
    таблицы/базы -> пустой список, исключение не летит наружу."""
    from database.db import get_setting

    try:
        raw = await get_setting("payment_options")
    except Exception as exc:  # noqa: BLE001 — намеренно широкий fail-soft (D-04)
        logger.warning("i18n_sources.payment_option_texts: payment_options недоступна (%s)", exc)
        return []
    if not raw:
        return []
    return [
        ("payment_options", label)
        for label, _price, _tracks in payment_options.parse_options(raw)
    ]


async def stored_delegate_texts() -> list[tuple[str, str]]:
    """Реально сохранённые в БД делегатские тексты: `SELECT key, value FROM bot_settings`,
    отфильтрованный `is_delegate_dynamic_key`. `list`-ключи разворачиваются построчно — одна
    строка списка = одна переводимая строка (иначе движок получил бы один многострочный блоб).
    Fail-soft (D-04): нет таблицы/базы → пустой список + `logger.warning`, исключение наружу
    не летит — вызывающий (`corpus()`, `tools/i18n_probe.py`) не обязан знать про устройство
    хранения."""
    from database.db import _connect

    try:
        async with _connect() as db:
            cursor = await db.execute("SELECT key, value FROM bot_settings")
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 — намеренно широкий fail-soft (D-04)
        logger.warning("i18n_sources.stored_delegate_texts: bot_settings недоступна (%s)", exc)
        return []

    result: list[tuple[str, str]] = []
    for key, value in rows:
        if value is None or not is_delegate_dynamic_key(key):
            continue
        base = _strip_dynamic_suffixes(key)
        spec = SETTINGS_SCHEMA.get(base)
        if spec and spec.get("type") == "list":
            for line in value.splitlines():
                line = line.strip()
                if line:
                    result.append((key, line))
        else:
            result.append((key, value))
    return result


async def corpus() -> list[tuple[str, str]]:
    """Полный корпус делегатских текстов анкеты: `code_literals()` + `stored_delegate_texts()`
    + дефолты `delegate_registry_keys()` из схемы + подписи городов мероприятия — с
    дедупликацией по `strip()`-нутому тексту (порядок сохранять, отчёт читает человек: первым
    делом попадаются самые «частые» строки, как правило самые важные). Пустые строки и
    одиночное «-» (значение «оставить дефолт» в админке) пропускаются — это не текст для
    перевода.

    Задача «делегатский интерфейс на английском» (после 27-04): подпись города мероприятия
    (`cities.CITIES[i]["label"]`, например «Москва, 30-31 октября») — делегат видит её на
    вилке города ДО первого вопроса анкеты (`form.py::_pre_items`, `city_fork`), но это не
    ключ `SETTINGS_SCHEMA` и не строка `bot_settings` — своя таблица `cities`, свой источник
    добавлен явно, а не через `stored_delegate_texts()` (та читает только `bot_settings`)."""
    items: list[tuple[str, str]] = list(code_literals())
    items.extend(await stored_delegate_texts())
    items.extend(await city_texts())
    items.extend(await payment_option_texts())

    for key in sorted(delegate_registry_keys()):
        spec = SETTINGS_SCHEMA.get(key, {})
        default = spec.get("default")
        if not default:
            continue
        if spec.get("type") == "list" and isinstance(default, str):
            for line in default.splitlines():
                line = line.strip()
                if line:
                    items.append((key, line))
        elif spec.get("type") == "list" and isinstance(default, (list, tuple)):
            for line in default:
                line = str(line).strip()
                if line:
                    items.append((key, line))
        elif isinstance(default, str):
            items.append((key, default))

    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for origin_key, text in items:
        if not text:
            continue
        stripped = text.strip()
        if not stripped or stripped == "-":
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        deduped.append((origin_key, stripped))
    return deduped
