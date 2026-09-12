"""Phase 30 (30-01, A2-08): шов «📝 Анкета» — девять callback-хендлеров тумблеров «Анкета 2.0»
(`reg_form_v2_enabled` + восемь элементов). Отдельный файл, а не ещё девять функций внутри
`handlers/admin_settings.py`: тот модуль уже стоит на документированном потолке
(`tests/test_module_size_convention_260816.py::KNOWN_OVERAGES["admin_settings.py"]`), и девять
новых хендлеров подняли бы его дальше без всякой пользы — сама механика переключения (общий
`_toggle_module_setting`) уже живёт там и переиспользуется отсюда, второй копии нет.

Форма шва — `handlers/admin_quiet_hours.py`: свой роутер здесь НЕ заводится, декоратор
навешивается на общий `router` из `handlers.admin` (инвариант cap-теста
`tests/test_roles_phase8.py` — новый роутер уместен только для команд вне
`CapabilityMiddleware`, регистрация не из их числа)."""
from aiogram import F, types

from handlers.admin import router
from handlers.admin_settings import _toggle_module_setting
from settings_schema import SETTINGS_SCHEMA


@router.callback_query(F.data == "toggle_reg_form_v2")
async def toggle_reg_form_v2(callback: types.CallbackQuery):
    # Мастер-тумблер всей «Анкеты 2.0» — дефолт off (D-06: делегат не видит ничего нового, пока
    # менеджер явно не включит; см. reg_engine.degrade_kind — "legacy" при выключенном флаге).
    await _toggle_module_setting(
        callback, "reg_form_v2_enabled", SETTINGS_SCHEMA["reg_form_v2_enabled"]["label"],
    )


@router.callback_query(F.data == "toggle_reg_form_chips")
async def toggle_reg_form_chips(callback: types.CallbackQuery):
    await _toggle_module_setting(
        callback, "reg_form_chips", SETTINGS_SCHEMA["reg_form_chips"]["label"],
    )


@router.callback_query(F.data == "toggle_reg_form_lookup_search")
async def toggle_reg_form_lookup_search(callback: types.CallbackQuery):
    await _toggle_module_setting(
        callback, "reg_form_lookup_search", SETTINGS_SCHEMA["reg_form_lookup_search"]["label"],
    )


@router.callback_query(F.data == "toggle_reg_form_edu_card")
async def toggle_reg_form_edu_card(callback: types.CallbackQuery):
    await _toggle_module_setting(
        callback, "reg_form_edu_card", SETTINGS_SCHEMA["reg_form_edu_card"]["label"],
    )


@router.callback_query(F.data == "toggle_reg_form_repeatable")
async def toggle_reg_form_repeatable(callback: types.CallbackQuery):
    await _toggle_module_setting(
        callback, "reg_form_repeatable", SETTINGS_SCHEMA["reg_form_repeatable"]["label"],
    )


@router.callback_query(F.data == "toggle_reg_form_limit_counter")
async def toggle_reg_form_limit_counter(callback: types.CallbackQuery):
    await _toggle_module_setting(
        callback, "reg_form_limit_counter", SETTINGS_SCHEMA["reg_form_limit_counter"]["label"],
    )


@router.callback_query(F.data == "toggle_reg_form_status_screen")
async def toggle_reg_form_status_screen(callback: types.CallbackQuery):
    await _toggle_module_setting(
        callback, "reg_form_status_screen", SETTINGS_SCHEMA["reg_form_status_screen"]["label"],
    )


@router.callback_query(F.data == "toggle_reg_form_header_settings")
async def toggle_reg_form_header_settings(callback: types.CallbackQuery):
    await _toggle_module_setting(
        callback, "reg_form_header_settings", SETTINGS_SCHEMA["reg_form_header_settings"]["label"],
    )


@router.callback_query(F.data == "toggle_reg_form_haptics")
async def toggle_reg_form_haptics(callback: types.CallbackQuery):
    await _toggle_module_setting(
        callback, "reg_form_haptics", SETTINGS_SCHEMA["reg_form_haptics"]["label"],
    )
