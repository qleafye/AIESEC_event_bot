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
- Остальные типы (text/list/date/toggle/photo/file) и незнакомые ключи — без проверки.

Сброс («-») и пустое значение валидатор не видит — их обрабатывает сам хендлер раньше.
"""
import re

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

    return value, None


def _int_example(entry: dict) -> str:
    """Пример для подсказки: дефолт из схемы, если он положительный, иначе 120."""
    default = entry.get("default")
    if isinstance(default, int) and default > 0:
        return str(default)
    return "120"
