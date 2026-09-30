"""Валидация значения настройки по типу ключа из SETTINGS_SCHEMA — ДО записи в bot_settings.

Чистая функция, без БД и без aiogram: вызывается из
`handlers.admin_settings.settings_edit_value` (вынесена отдельным модулем, т.к.
admin_settings.py упирается в потолок test_module_size_convention_260816) и — с Phase 22
(план 22-04, D-06) — из веб-слоя Mini App через `settings_ops.validate_batch_item`. Модуль
живёт в КОРНЕ (не в `handlers/`): пакет `handlers` при импорте тянет aiogram, а веб-процессу
и `settings_ops.py` это запрещено; `handlers/settings_validation.py` — шов-реэкспорт.

Правила (согласованы с тем, как значения потом ЧИТАЮТСЯ — `settings_schema._parse_setting`
и `services.scheduler._int_or_default`):

- `int` — целое число >= 0 (пробелы по краям допустимы, нормализуется к `str(int)`).
  Схема не задаёт min/max, поэтому верхней границы нет; отрицательные отклоняются —
  читатель всё равно вернул бы дефолт, а менеджер не понял бы, почему «не применилось».
  Ноль пропускаем: для части ключей «0 = без лимита / без ограничения» прямо описано в
  prompt (game_resubmit_limit, proxy_connect_timeout).
- `enum` — одно из `options`; сравнение без учёта регистра, сохраняется каноническое
  написание из схемы.
- `date_only` — маска `ДД.ММ.ГГГГ`, проверяется реальным `strptime` (Phase 31, 31-03,
  D-30) — не regex, `31.02.2026` отбрасывается. Нормализуется к ведущим нулям.
- `format: "number"` (квик 260927) — число >= 0, дробь через запятую, нормализуется `:g`.
- Остальные типы (text/list/date/toggle/photo/file) и незнакомые ключи — без проверки.

Сброс («-») и пустое значение валидатор не видит — их обрабатывает сам хендлер раньше.
"""
import math
import re
from datetime import datetime

from cities import PER_CITY_SEP
from settings_schema import SETTINGS_SCHEMA, multi_codes

# Quick 260820-rms: одиночная команда — `/slovo` или `/slovo@YouLead_bot`, без пробелов и без
# продолжения. Ровно то, что телеграм отправляет по тапу на подсказку команды; ровно то, чем
# 20.08 затёрли source_options и approve_text (в обоих оказалось «/start»). Текст, который
# просто НАЧИНАЕТСЯ со слэша, но содержит пробел или ещё что-то («/start — так мы называем…»),
# остаётся нормальным значением: отбиваем узкий случай, а не всё подряд.
_COMMAND_RE = re.compile(r"/[A-Za-z0-9_]{1,32}(@[A-Za-z0-9_]{1,32})?\Z")

# Quick 260904-dq1: время «ЧЧ:ММ» для `format: "time"` (тихие часы). Допускает «9:00» —
# менеджер не обязан помнить ведущий ноль; диапазон часов/минут проверяется отдельно, чтобы
# отбить «22:60» с понятным текстом, а не молчаливым regex-провалом.
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def is_command_like(value: str | None) -> bool:
    """Похоже ли присланное на команду боту, а не на значение настройки."""
    return bool(value) and bool(_COMMAND_RE.fullmatch(value.strip()))


def validate_setting_value(key: str, value: str) -> tuple[str | None, str | None]:
    """Вернуть `(нормализованное_значение, None)` при успехе или `(None, текст_ошибки)`
    при отказе. Текст ошибки — готовое HTML-сообщение менеджеру: что не так и пример
    правильного формата (CLAUDE.md: ошибка объясняет, что сделать).

    `key` может быть per-city composite (`{base}__city__{code}`) — тип берётся у базового
    ключа.
    """
    base = key.split(PER_CITY_SEP)[0]
    entry = SETTINGS_SCHEMA.get(base)
    if entry is None:
        return value, None

    entry_type = entry.get("type")

    if entry_type == "int":
        stripped = value.strip()
        try:
            number = int(stripped)
        except ValueError:
            number = None
        if number is None:
            example = _int_example(entry)
            return None, (
                f"Нужно целое число, например <code>{example}</code>.\n\n"
                "Пришлите ещё раз или «-», чтобы сбросить к значению по умолчанию."
            )
        if number < 0:
            return None, (
                "Число не может быть отрицательным — пришлите 0 или больше "
                f"(например <code>{_int_example(entry)}</code>).\n\n"
                "Пришлите ещё раз или «-», чтобы сбросить к значению по умолчанию."
            )
        minimum = int(entry.get("min") or 0)
        if number < minimum:
            return None, (
                f"Нужно число {minimum} или больше (например <code>{_int_example(entry)}</code>)."
                "\n\nПришлите ещё раз или «-», чтобы сбросить к значению по умолчанию."
            )
        maximum = entry.get("max")
        if maximum is not None and number > int(maximum):
            return None, (
                f"Нужно число не больше {maximum} (например <code>{_int_example(entry)}</code>)."
                "\n\nПришлите ещё раз или «-», чтобы сбросить к значению по умолчанию."
            )
        return str(number), None

    if entry_type == "enum":
        options = list(entry.get("options") or [])
        if not options:
            return value, None
        wanted = value.strip().lower()
        for option in options:
            if str(option).lower() == wanted:
                return str(option), None
        listed = ", ".join(f"<code>{o}</code>" for o in options)
        return None, (
            f"Такого варианта нет. Допустимые значения: {listed}.\n\n"
            f"Пришлите одно из них (например <code>{options[0]}</code>) "
            "или «-», чтобы сбросить к значению по умолчанию."
        )

    # Phase 28 (28-08, SU-08, A5): `list` + `options_from_step` (скоринговые чекбокс-пикеры,
    # form.js рисует их тем же `multiControl`, что закрытый `multi`) — та же поблажка на
    # пустой набор, что у `multi` ниже, но БЕЗ перевода подпись->код: набор открытый
    # (варианты — текущий список ответов вопроса анкеты, reg_engine.options(step_key)),
    # закрытой карты для проверки здесь нет и не будет — эта функция aiogram/reg_engine-free.
    if entry_type == "list" and entry.get("options_from_step"):
        segments = [
            segment.strip()
            for line in value.splitlines()
            for segment in line.split(";")
            if segment.strip()
        ]
        if not segments:
            empty_value = entry.get("empty_value", "—")
            return empty_value, None
        return "\n".join(segments), None

    if entry_type == "multi":
        # Quick 260906-6xe: закрытый набор, отмеченный галочками в вебе — вход приходит
        # ПОДПИСЯМИ (JSON человеку показывает подписи, коды не уезжают в веб вовсе), теми же
        # разделителями, что `settings_schema._parse_setting` для `list` (перевод строки
        # и «;»). Пустой набор — законный ввод (снятие всех галочек), а не «не понял
        # значение»: превращается в сентинел из меты `empty_value`, если её нет — значение
        # проходит как есть (реестр не выдумывает сентинел за модуль-владелец).
        segments = [
            segment.strip()
            for line in value.splitlines()
            for segment in line.split(";")
            if segment.strip()
        ]
        if not segments:
            empty_value = entry.get("empty_value")
            return (empty_value, None) if empty_value is not None else (value, None)
        codes, _bad_label = multi_codes(base, segments)
        if codes is None:
            # Текст НЕ содержит кода шага и НЕ повторяет присланное — оно может быть кодом,
            # а код человеку не показываем (CLAUDE.md), даже в тексте отказа.
            return None, (
                "Такого варианта нет — отметьте варианты галочками. Обновите экран и "
                "попробуйте ещё раз."
            )
        return "\n".join(codes), None

    if entry_type == "date_only":
        # Phase 31 (31-03, D-30): «дата без времени» — реальная проверка через strptime
        # (не regex-маска), 31.02 отбрасывается так же честно, как «abc». Нормализуем к
        # ведущим нулям (%d.%m.%Y), чтобы в базе не копились варианты вида «1.9.2026» — тот
        # же приём, что у int-ветки выше (str(number)).
        stripped = value.strip()
        try:
            parsed = datetime.strptime(stripped, "%d.%m.%Y")
        except ValueError:
            return None, (
                "Нужна дата в формате <code>ДД.ММ.ГГГГ</code>, например "
                "<code>15.10.2026</code> — без времени.\n\n"
                "Пришлите ещё раз или «-», чтобы сбросить значение."
            )
        return parsed.strftime("%d.%m.%Y"), None

    if entry.get("format") == "time":
        match = _TIME_RE.fullmatch(value.strip())
        if not match:
            return None, (
                "Нужно время в формате <code>ЧЧ:ММ</code>, например <code>22:00</code>.\n\n"
                "Пришлите ещё раз или «-», чтобы сбросить к значению по умолчанию."
            )
        hours, minutes = int(match.group(1)), int(match.group(2))
        if hours > 23 or minutes > 59:
            return None, (
                "Часы должны быть от 0 до 23, минуты — от 0 до 59, например "
                "<code>22:00</code>.\n\n"
                "Пришлите ещё раз или «-», чтобы сбросить к значению по умолчанию."
            )
        return f"{hours:02d}:{minutes:02d}", None

    if entry.get("format") == "msk_datetime":
        # Дедлайн ступеней амбассадоров: «ГГГГ-ММ-ДД ЧЧ:ММ» по Москве. «нет» — без дедлайна
        # (хранится пустой строкой); «-» сюда не доходит — это сброс к значению по умолчанию.
        stripped = value.strip()
        if stripped.lower() in ("нет", "без дедлайна"):
            return "", None
        try:
            parsed = datetime.strptime(stripped, "%Y-%m-%d %H:%M")
        except ValueError:
            return None, (
                "Не понял дату. Нужен формат <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> по Москве, "
                "например <code>2026-11-14 23:59</code>.\n\n"
                "Пришлите ещё раз, «нет», чтобы убрать дедлайн, или «-», чтобы вернуть "
                "значение по умолчанию."
            )
        return parsed.strftime("%Y-%m-%d %H:%M"), None

    if entry.get("format") == "number":
        # Квик 260927 (рейтинг чата): число 0 или больше, дробь через запятую («0,5» — так его
        # набирает менеджер). Хранится нормализованным (`:g`), читает chat_score — тот же
        # разбор, поэтому «что приняли здесь» == «что посчитает дашборд».
        number = None
        try:
            number = float(value.strip().replace(",", "."))
        except ValueError:
            pass
        minimum_exclusive = entry.get("number_min_exclusive")
        bad = (
            number is None or not math.isfinite(number) or number < 0
            or (minimum_exclusive is not None and number <= minimum_exclusive)
        )
        if bad:
            example = str(entry.get("default") or "1").replace(".", ",")
            floor = (
                "больше нуля" if minimum_exclusive is not None else "0 или больше"
            )
            return None, (
                f"Нужно число {floor}, например <code>{example}</code> "
                "(дробное — через запятую: <code>0,5</code>).\n\n"
                "Пришлите ещё раз или «-», чтобы сбросить к значению по умолчанию."
            )
        return f"{number:g}", None

    return value, None


def _int_example(entry: dict) -> str:
    """Пример для подсказки: дефолт из схемы, если он положительный, иначе 120."""
    default = entry.get("default")
    if isinstance(default, int) and default > 0:
        return str(default)
    return "120"


# Пороги ступеней амбассадоров: следующая ступень обязана требовать больше прошедших отбор,
# чем предыдущая, иначе вторая ступень выдаётся раньше первой, а прогресс противоречит
# сообщениям. Проверяются только первые `amb_tiers_count` ступеней.
AMB_THRESHOLD_KEYS: tuple[str, ...] = tuple(f"amb_tier{n}_threshold" for n in range(1, 6))


def amb_threshold_order_error(key: str, value: str, current: dict[str, int],
                              count: int = 3) -> str | None:
    """Порядок порогов «ступень 1 < ступень 2 < …» для нового значения `value` ключа `key`
    против текущих значений остальных (`current`, их передаёт вызывающий — функция остаётся без
    БД). `count` — сколько ступеней включено: пороги за его пределами не проверяются.
    `None` — порядок соблюдён или ключ не порог."""
    if key not in AMB_THRESHOLD_KEYS:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    keys = AMB_THRESHOLD_KEYS[:max(1, min(count, len(AMB_THRESHOLD_KEYS)))]
    if key not in keys:
        return None
    values = {**current, key: number}
    ordered = [int(values[k]) for k in keys]
    for index in range(len(keys) - 1):
        lower, upper = ordered[index], ordered[index + 1]
        if lower >= upper:
            shown = " / ".join(str(current.get(k)) for k in keys)
            return (
                f"Порог ступени {index + 2} должен быть больше, чем у ступени {index + 1} "
                f"(сейчас {lower}), а получилось бы {upper}.\n\n"
                f"Пороги сейчас: {shown}. "
                "Пришлите число, при котором каждая следующая ступень больше предыдущей "
                "(например 1 / 3 / 7), или «-», чтобы сбросить к значению по умолчанию. "
                "Сдвигаете все пороги вверх — начните с последней ступени, вниз — с первой."
            )
    return None
