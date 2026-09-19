"""Квик 260919-mlu, закрытие дыры: имена вкладок Google Sheets правятся ТОЛЬКО из бота.

Развилка «переименовать существующую / писать в имеющуюся / завести новую пустую»
(`handlers/admin_sheet_tabs.py`) стоит на пути правки настройки в боте. Веб-поверхность
(Mini App `PUT /app/api/admin/settings`) ходит мимо неё и раньше просто перезаписывала ключ:
бот заводил пустую вкладку, строки оставались в брошенной. Живое доказательство — прод 19.09:
«Незавершённые» 956 строк, «NOT FILLED REG» 20, «NOT FILLED REGS» 432; «Гейма» 158 и
«Гейма бот» 64.

Сторожа ниже фиксируют ровно две вещи: ключи-имена вкладок не редактируются из веба, а
безопасные соседи по группе `sheets` — редактируются (исключать группу целиком было бы
перебором).
"""
import pytest

import settings_ops
from settings_schema import SETTINGS_SCHEMA


def test_tab_name_keys_are_not_web_editable():
    editable = set(settings_ops.editable_keys())
    leaked = sorted(k for k in settings_ops.SHEET_TAB_NAME_KEYS if k in editable)
    assert leaked == [], (
        f"ключи-имена вкладок доступны для правки из веба мимо развилки: {leaked}"
    )


@pytest.mark.parametrize("key", ["sheet_logs_autosync", "sheet_tab_bot_prefix"])
def test_safe_sheets_group_keys_stay_web_editable(key):
    """Группа `sheets` целиком НЕ исключается: тумблер журнала и сам префикс менять из веба
    безопасно — префикс ничего не переименовывает, переименование делает отдельная кнопка
    с подтверждением."""
    assert SETTINGS_SCHEMA[key]["group"] == "sheets"
    assert key in settings_ops.editable_keys()


def test_excluded_keys_are_real_registry_keys():
    """Опечатка в EXCLUDED_KEYS не должна тихо ничего не исключать."""
    unknown = sorted(k for k in settings_ops.EXCLUDED_KEYS if k not in SETTINGS_SCHEMA)
    assert unknown == []


def test_group_exclusion_also_applies_inside_screen_order():
    """Регрессия на ловушку, которая стояла заряженной: проверка исключений была только во
    втором цикле `editable_keys`, поэтому исключить группу из `_GROUP_SCREEN_ORDER` было
    нельзя — отфильтровалась бы лишь та её часть, что в хвосте реестра. Группа `sheets`
    в порядке экранов есть, так что предикат обязан работать в ОБОИХ циклах."""
    assert "sheets" in settings_ops._GROUP_SCREEN_ORDER
    assert not settings_ops._key_editable_in_web("main_sheet_tab", "sheets")
    assert settings_ops._key_editable_in_web("sheet_logs_autosync", "sheets")
    assert not settings_ops._key_editable_in_web("role_caps_reg_manager", "roles")


def test_city_tab_suffix_keys_are_documented_as_still_open():
    """ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ, зафиксированное намеренно: суффиксы городских вкладок не
    заперты, потому что развилки под них нет и в самом боте. Тест падает, когда кто-то
    добавит им гейт (или запрёт их в вебе) — и тогда надо обновить и комментарий, и этот
    сторож, а не просто перекрасить его в зелёный."""
    editable = set(settings_ops.editable_keys())
    suffix_keys = [k for k in SETTINGS_SCHEMA if k.startswith("city_tab_suffix__")]
    assert suffix_keys, "ключи суффиксов пропали из реестра — тест устарел"
    assert all(k in editable for k in suffix_keys)
    assert all(k not in settings_ops.SHEET_TAB_WRITE_MODE for k in suffix_keys)
