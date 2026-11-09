"""Сторож карты «фича → где она настраивается».

Markdown из `.planning/` не читается: в CI его нет. Каждая пара ниже найдена
в исходниках: тумблер или ключ живёт в одном разделе или группе, форма или
текст — в другом. Если пару сведут в одно место, тест краснеет фразой и
просит обновить «Полувключено», а не молчит.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from domain.settings.schema import SETTINGS_SCHEMA
from handlers.settings.admin_sections import SECTIONS
from handlers.settings.admin_settings import SETTINGS_GROUPS

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_JS = (ROOT / "miniapp" / "static" / "js" / "screens" / "settings.js").read_text(encoding="utf-8")
ADMIN_SETTINGS_PY = (ROOT / "handlers" / "settings" / "admin_settings.py").read_text(encoding="utf-8")


def _fail(pair: str, why: str) -> None:
    pytest.fail(f"Пара «{pair}»: {why} Обновите раздел «Полувключено» в карте фич.")


def _extract_object(source: str, marker: str) -> str:
    start = source.find(marker)
    if start < 0:
        return ""
    brace = source.find("{", start)
    if brace < 0:
        return ""
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace:index + 1]
    return ""


def _set_items(block: str, field: str) -> set[str]:
    match = re.search(rf"{field}: new Set\(\[(.*?)\]\)", block, re.S)
    if not match:
        return set()
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _row_filter(code: str) -> dict:
    filters = _extract_object(SETTINGS_JS, "const ROW_FILTERS")
    block = _extract_object(filters, f'"{code}"')
    return {
        "block": block,
        "groups": _set_items(block, "groups"),
        "keys": _set_items(block, "keys"),
        "skip": _set_items(block, "skip"),
        "reg_text": "regText: true" in block,
    }


def _group_of(key: str) -> str | None:
    for _label, token, keys in SETTINGS_GROUPS:
        if key in keys:
            return token
    return None


def _section_of_callback(callback: str) -> str | None:
    for token, _label, rows in SECTIONS:
        for row in rows:
            if row[0] == "group":
                continue
            if len(row) > 1 and row[1] == callback:
                return token
    return None


def _section_of_group(group_token: str) -> str | None:
    for token, _label, rows in SECTIONS:
        for row in rows:
            if row[0] == "group" and row[1] == group_token:
                return token
    return None


def _section_has_group(section: str, group_token: str) -> bool:
    return _section_of_group(group_token) == section


def _token_block(token: str) -> str:
    """Тело `if token == "...":` в клавиатуре группы настроек бота."""
    lines = ADMIN_SETTINGS_PY.splitlines()
    start = None
    needle = f'if token == "{token}":'
    for index, line in enumerate(lines):
        if line.startswith(f"    {needle}"):
            start = index
            break
    if start is None:
        return ""
    body = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("    if ") or (line and not line.startswith(" ")):
            break
        if (
            line.startswith("    ")
            and not line.startswith("        ")
            and line.strip()
            and not line.strip().startswith("#")
        ):
            break
        body.append(line)
    return "\n".join(body)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_payment_reminder_toggle_is_not_with_its_texts():
    """Тумблер напоминаний — «Запись и напоминания», тексты — «Оплата и согласия»."""
    pair = "Напоминания об оплате"
    reminders = _row_filter("row-reminders")
    pay = _row_filter("row-pay")
    toggle = "payment_reminders_enabled"
    texts = ("payment_reminder_text", "payment_overdue_text")
    if not reminders["block"] or not pay["block"]:
        _fail(pair, "не прочитались строки приложения.")
    together = (
        toggle in pay["keys"] and toggle not in pay["skip"]
        or any(key in reminders["keys"] for key in texts)
        or any(_group_of(key) in reminders["groups"] for key in texts)
    )
    if together:
        _fail(pair, "тумблер и тексты напоминания оказались в одной строке приложения.")
    if toggle not in reminders["keys"] or toggle not in pay["skip"] or "pay" not in pay["groups"]:
        _fail(pair, "тумблер больше не в «Записи и напоминаниях», отдельно от «Оплаты и согласий».")
    if any(_group_of(key) != "pay" for key in texts):
        _fail(pair, "тексты напоминания ушли из группы «Оплата».")
    if _section_of_callback("toggle_payment_reminders") is not None or _section_of_group("pay") is not None:
        _fail(pair, "в боте тумблер напоминаний или группа «Оплата» снова в разделе, карта про приложение устарела.")


def test_reject_return_text_is_not_on_the_rules_screen():
    """Правила и рубильник — «Заявки», текст возврата — группа «Регистрация» в «Анкете»."""
    pair = "Возврат из автоотказа"
    key = "reject_rules_return_text"
    rules = _read("handlers/applications/admin_reject_rules.py")
    text_group = _group_of(key)
    text_section = _section_of_group(text_group) if text_group else None
    rules_section = _section_of_callback("admin_reject_rules")
    together = (
        text_section == rules_section
        or SETTINGS_SCHEMA[key]["group"] == "apps"
        or f"settings_edit:{key}" in rules
    )
    if together:
        _fail(pair, "текст возврата оказался рядом с «Правилами автоотказа».")
    if text_group != "reg" or text_section != "form" or rules_section != "apps":
        _fail(pair, "текст возврата больше не в «Анкете», а правила — не в «Заявках».")
    if SETTINGS_SCHEMA[key]["group"] != "reg":
        _fail(pair, "группа реестра текста возврата больше не reg, приложение перестанет уносить его в «Тексты делегату».")


def test_ambassador_selection_toggle_is_not_with_ambassador_texts():
    """Кнопка отбора — на экране группы «Геймификация», тексты — группа amb, в боте её нет."""
    pair = "Отбор амбассадоров"
    game_block = _token_block("game")
    amb_block = _token_block("amb")
    text_key = "amb_candidate_ack_text"
    text_group = _group_of(text_key)
    together = (
        "selection_toggle_button" in amb_block
        or text_group == "game"
        or _section_of_group("amb") == "game"
    )
    if together:
        _fail(pair, "тумблер отбора и тексты амбассадоров оказались в одном разделе.")
    if "selection_toggle_button" not in game_block or text_group != "amb":
        _fail(pair, "тумблер больше не на группе «Геймификация» или тексты ушли из группы amb.")
    if _section_of_group("game") != "game" or _section_of_group("amb") is not None:
        _fail(pair, "группа текстов amb снова объявлена в разделе бота рядом с тумблером.")
    if SETTINGS_SCHEMA["amb_team_selection_enabled"]["group"] != "amb":
        _fail(pair, "ключ тумблера отбора ушёл из группы реестра amb, приложение покажет его не с текстами амбассадоров.")


def test_city_chat_link_is_not_on_the_city_chat_screen():
    """Ссылка для {chat} — группа амбассадоров, экран чата и учёт — «Чат города»."""
    pair = "Ссылка на чат города"
    key = "delegate_chat_link"
    chat_row = _row_filter("row-chat")
    amb_row = _row_filter("row-amb")
    screen = _read("handlers/chat/admin_city_chat.py")
    together = (
        _group_of(key) == "chat"
        or key in chat_row["keys"]
        or key in screen
    )
    if together:
        _fail(pair, "ссылка на чат города оказалась на экране «Чат города» или в его группе.")
    if _group_of(key) != "amb" or "amb" not in amb_row["groups"]:
        _fail(pair, "ссылка больше не в группе амбассадоров, которую забирает строка «Амбассадоры».")
    if _section_of_callback("admin_city_chat") != "manage" or "chat_tracking_enabled" not in chat_row["keys"]:
        _fail(pair, "экран чата или тумблер учёта уехали, пара больше не про разные места.")
    if "chat" not in chat_row["groups"] or "amb" in chat_row["groups"]:
        _fail(pair, "строка «Чат города» стала забирать группу амбассадоров.")


def test_consent_toggle_is_not_with_consent_texts():
    """Тумблер согласий — бот «Анкета», список и тексты — приложение «Оплата и согласия»."""
    pair = "Модуль согласий"
    pay = _row_filter("row-pay")
    texts = ("consent_button_text", "consent_list")
    together = (
        _section_has_group("form", "consent")
        or "consent_enabled" in pay["keys"]
        or any(_group_of(key) != "consent" for key in texts)
    )
    if together:
        _fail(pair, "тумблер согласий и тексты согласий оказались в одном разделе.")
    if _section_of_callback("toggle_consent_enabled") != "form":
        _fail(pair, "тумблер согласий больше не в разделе «Анкета».")
    if "consent" not in pay["groups"] or _section_of_group("consent") is not None:
        _fail(pair, "тексты согласий больше не только в строке «Оплата и согласия».")


def test_delegation_game_toggle_is_not_with_game_texts():
    """Рубильник игры для делегатов — «Делегации» в «Заявках», тексты игры — «Геймификация»."""
    pair = "Геймификация для делегатов"
    screen = _read("handlers/delegations/admin_delegations.py")
    game_block = _token_block("game")
    text_key = "game_task_list_empty"
    together = (
        "delegation_game_enabled" in game_block
        or "dlg_game" in ADMIN_SETTINGS_PY
        or _group_of("delegation_game_enabled") == "game"
    )
    if together:
        _fail(pair, "рубильник игры для делегатов оказался на экране «Геймификация».")
    if 'F.data == "dlg_game"' not in screen or "delegation_game_enabled" not in screen:
        _fail(pair, "рубильник больше не на экране «Делегации».")
    if _section_of_callback("admin_delegations") != "apps":
        _fail(pair, "экран «Делегации» уехал из «Заявок».")
    if _group_of(text_key) != "game" or _section_of_group("game") != "game":
        _fail(pair, "тексты геймификации больше не в разделе «Геймификация».")
    if SETTINGS_SCHEMA["delegation_game_enabled"]["group"] != "apps":
        _fail(pair, "группа реестра рубильника игры для делегатов изменилась.")


def test_delegation_welcome_is_not_on_the_arm_screen():
    """Включение делегаций — «Таблица и формы», приветствие — экран «Делегации» в боте."""
    pair = "Приветствие делегации"
    key = "delegation_welcome_text"
    screen = _read("handlers/delegations/admin_delegations.py")
    forms_js = _read("miniapp/static/js/screens/sheets_forms.js")
    forms_py = _read("miniapp/routers/sheets_forms.py")
    together = key in forms_js or key in forms_py or (_group_of(key) is not None)
    if together:
        _fail(pair, "приветствие делегации оказалось на «Таблице и формах» или в общей группе настроек.")
    if key not in screen or '"welcome"' not in screen:
        _fail(pair, "приветствие больше не правится с экрана «Делегации».")
    if "sheets-forms/delegations" not in forms_js or _section_of_callback("admin_delegations") != "apps":
        _fail(pair, "включение делегаций и экран «Делегации» больше не в разных местах.")
    if SETTINGS_SCHEMA[key]["group"] != "reg":
        _fail(pair, "группа реестра приветствия изменилась.")


def test_external_registration_text_is_not_only_on_the_forms_screen():
    """Текст «заявка получена» — группа «Регистрация» в «Анкете», форма — «Данные»."""
    pair = "Заявка из внешней формы"
    key = "ext_reg_pending_text"
    purpose = _read("handlers/ext_forms/admin_ext_forms_purpose.py")
    text_group = _group_of(key)
    text_section = _section_of_group(text_group) if text_group else None
    forms_section = _section_of_callback("admin_ext_forms")
    together = text_section is None or text_section == forms_section
    if together:
        _fail(pair, "текст «заявка получена» больше не в другом разделе, чем «Внешние формы».")
    if text_group != "reg" or text_section != "form" or forms_section != "data":
        _fail(pair, "текст уехал из «Анкеты» или экран форм уехал из «Данных».")
    if f"settings_edit:{key}" not in purpose:
        _fail(pair, "на карточке формы пропала кнопка этого текста, карта про два входа устарела.")
    if SETTINGS_SCHEMA[key]["group"] != "reg":
        _fail(pair, "группа реестра текста внешней регистрации изменилась.")


def test_miniapp_shop_texts_are_not_with_the_shop_screen():
    """Витрина включается в «Геймификации», тексты экрана магазина в приложении — в «Редком»."""
    pair = "Тексты магазина в приложении"
    key = "miniapp_shop_closed_text"
    rare = _row_filter("row-rare")
    shop = _read("handlers/game/admin_shop.py")
    together = (
        SETTINGS_SCHEMA[key]["group"] == "game"
        or _group_of(key) == "game"
        or key in shop
    )
    if together:
        _fail(pair, "тексты магазина в приложении оказались рядом с витриной в «Геймификации».")
    if SETTINGS_SCHEMA[key]["group"] != "miniapp" or "miniapp" not in rare["groups"]:
        _fail(pair, "тексты магазина больше не в группе miniapp строки «Редкое».")
    if _section_of_callback("admin_shop") != "game":
        _fail(pair, "«Магазин баллов» уехал из «Геймификации».")
    if _group_of("shop_take_button_text") != "game":
        _fail(pair, "тексты витрины в чате ушли из группы геймификации — это другая пара, карту надо сверить.")


def test_quiet_hours_fields_stay_in_apps_without_the_toggle():
    """Часы «с» и «до» всё ещё в группе «Заявки», тумблера в этом разделе бота нет."""
    pair = "Тихие часы"
    reminders = _row_filter("row-reminders")
    fields = ("quiet_hours_start", "quiet_hours_end", "quiet_hours_manager_notice_text")
    toggle_in_apps = _section_of_callback("toggle_quiet_hours") == "apps"
    fields_in_apps = all(_group_of(key) == "apps" and _section_of_group("apps") == "apps" for key in fields)
    app_has_both = (
        "quiet_hours_enabled" in reminders["keys"]
        and all(key in reminders["keys"] for key in fields)
    )
    together = toggle_in_apps or not fields_in_apps
    if together:
        _fail(pair, "часы и тумблер тишины снова в одном разделе бота либо часы ушли из «Заявок».")
    if not app_has_both:
        _fail(pair, "в «Записи и напоминаниях» тумблер и часы больше не вместе — карта про бот отдельно от приложения устарела.")
    if _section_of_callback("admin_quiet_hours") is not None:
        _fail(pair, "экран «Тихие часы» снова объявлен в разделе бота.")


def test_checkin_caption_registry_group_is_not_the_toggle_section():
    """В боте подпись QR лежит в группе «Заявки» рядом с тумблером.

    Группа реестра при этом reg, и приложение уносит подпись в «Тексты делегату».
    """
    pair = "Подпись QR чек-ина"
    keys = ("checkin_qr_caption_text", "checkin_qr_disabled_text")
    texts = _row_filter("row-texts")
    together = any(SETTINGS_SCHEMA[key]["group"] != "reg" for key in keys) or not texts["reg_text"]
    if _section_of_callback("toggle_checkin_qr_enabled") != "apps":
        _fail(pair, "тумблер QR больше не в «Заявках», пару не с чем сравнивать.")
    if any(_group_of(key) != "apps" for key in keys):
        _fail(pair, "в боте подпись ушла из группы «Заявки», где лежит тумблер. Карта про два входа устарела.")
    if together:
        _fail(pair, "подпись QR в реестре перестала быть группой reg, приложение больше не уносит её в «Тексты делегату».")
    if any(key in texts["keys"] for key in keys):
        _fail(pair, "подпись QR перечислена в строке «Тексты делегату» явным ключом, а не из-за группы reg.")
