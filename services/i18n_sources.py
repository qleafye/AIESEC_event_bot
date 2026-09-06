"""Phase 27 (27-01, LANG-10/LANG-08) — единственное перечисление того, что в проекте считается
делегатским текстом анкеты. Нужен и оффлайн-замеру `tools/i18n_probe.py` (этот план), и
`bulk_seed`/gate-у очереди перевода (план 27-03) — второго списка источников в проекте быть не
должно (см. `tools/i18n_probe.py`, которое зовёт `corpus()` отсюда, а не строит свой).

Модуль aiogram-free: из `handlers` не импортирует НИЧЕГО (любой `import handlers.x` тянет
`handlers/__init__.py`, а тот — `registration, user_actions, admin, payment`, то есть весь бот
на бот-фреймворке; докстринг `reg_engine.py:11-19` держит тот же инвариант, и этот модуль ему
следует). Разрешённые импорты — `settings_schema`, `reg_engine`, `reg_labels`, `reg_options`,
`cities`, `config`, `database.db`.

## Граница «делегатское / админское» (LANG-08)

`DELEGATE_GROUPS` — группы реестра настроек (`settings_schema.SETTINGS_SCHEMA`), чьи `text`/
`list`-ключи считаются текстом анкеты и подлежат переводу в v1:

- `reg_prompts` — тексты вопросов анкеты (`reg_prompt_{step}`, генерируются циклом в конце
  `settings_schema.py`);
- `reg` — остальной текст регистрации: подписи кнопок мастера, тексты после одобрения/отказа,
  списки вариантов с реестровым override (`source_options`, `city_options`, ...);
- `party` — тексты party-трека (форк «Полная регистрация / Гости»).

Группы НЕ в списке — и почему:

- `event`, `menu`, `miniapp`, `game` — делегат может увидеть эти тексты, НЕ начав анкету
  (главное меню, лендинг мероприятия, геймификация). Правило границы фазы (CONTEXT.md, «вне
  объёма»): «если делегат может это увидеть, не начав анкету, — это v2».
- `pay` — оплата размечена как follow-up самой фазой (Q7/LANG-07 в CONTEXT.md), в v1 не входит.
- `consent` — LANG-09/D-04: согласия НЕ переводятся машинно. Английская редакция (если вообще
  нужна — вопрос юридический, не технический) вводится менеджером руками в плане 27-06.
- `sheets`, `dashboard`, `apps`, `system`, `roles`, `toggles` — чисто административные
  поверхности, делегат их не видит никогда.

`ADMIN_KEYS_IN_DELEGATE_GROUPS` — явное исключение внутри `DELEGATE_GROUPS`: ключи, которые
физически лежат в группе `reg` (потому что технически это текст, связанный с регистрацией), но
по смыслу — метка для МЕНЕДЖЕРА в карточке заявки, не текст ДЛЯ ДЕЛЕГАТА. Правило: «ключ группы
`reg` с подстрокой `admin` в имени → админский» — вычислено автоматически, а не выписано руками,
чтобы новый ключ с тем же паттерном не пролез в корпус молча.

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

from cities import split_per_city_key
from config import config
from settings_schema import SETTINGS_SCHEMA
import reg_engine
import reg_labels
import reg_options

logger = logging.getLogger(__name__)

DELEGATE_GROUPS = ("reg_prompts", "reg", "party")

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

# Динамические ключи вне SETTINGS_SCHEMA — только эти два префикса (help_text/prompt
# докстринги reg_engine.py явно говорят «в SETTINGS_SCHEMA НЕ заводится»).
_DYNAMIC_PREFIXES = ("reg_prompt_", "reg_help_")
_TRACK_SUFFIXES = ("__party", "__short")


def delegate_registry_keys() -> frozenset[str]:
    """Ключи `SETTINGS_SCHEMA` из `DELEGATE_GROUPS` типа text/list, минус админские метки."""
    return frozenset(
        key for key, spec in SETTINGS_SCHEMA.items()
        if spec.get("group") in DELEGATE_GROUPS
        and spec.get("type") in ("text", "list")
        and key not in ADMIN_KEYS_IN_DELEGATE_GROUPS
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

    return items


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
    + дефолты `delegate_registry_keys()` из схемы — с дедупликацией по `strip()`-нутому тексту
    (порядок сохранять, отчёт читает человек: первым делом попадаются самые «частые» строки,
    как правило самые важные). Пустые строки и одиночное «-» (значение «оставить дефолт» в
    админке) пропускаются — это не текст для перевода."""
    items: list[tuple[str, str]] = list(code_literals())
    items.extend(await stored_delegate_texts())

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
