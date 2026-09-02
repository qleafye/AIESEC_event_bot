"""Шов-реэкспорт: тело модуля переехало в корневой `settings_validation.py` (Phase 22, план
22-04, D-06/D-12) — тот же приём, что `handlers/reg_schema.py` -> `reg_labels.py`.

Причина: `handlers/__init__.py` при импорте ЛЮБОГО `handlers.x` загружает владельцев роутеров
(aiogram), а валидатор нужен aiogram-free потребителям — `settings_ops.py` (сторож
`tests/test_settings_ops.py::test_settings_ops_module_does_not_load_aiogram`) и веб-слою
`miniapp/routers/settings.py`. Один валидатор на две поверхности (D-06) возможен только из
корня. Бот и старые тесты продолжают импортировать отсюда — объекты те же (`is`), не копия.
"""
from settings_validation import (  # noqa: F401
    _COMMAND_RE,
    _int_example,
    is_command_like,
    validate_setting_value,
)

__all__ = ["is_command_like", "validate_setting_value"]
