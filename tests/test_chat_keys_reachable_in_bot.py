"""Сторож: ключ реестра, который читает код чата, можно поправить из админки бота.

Правило проекта — менеджер настраивает всё сам, в том числе на событии без приложения. Ключ,
текст которого бот показывает в чате, но который правится только в приложении, — дыра: на таком
событии его не поменять никак. Так жили оффер реф-ссылки, кнопки догонялки, «Не присылать
сегодня», тексты анкеты в чате и четыре текста SOS.

«Читает код чата» определяется статически, по AST: вызов чтения настройки, у которого ключ —
строковый литерал (`READERS`: сами `get_setting*` и обёртки над ними с тем же смыслом), в
`handlers/`, `services/`, `keyboards/` (не `miniapp/` — это код приложения). Чтение через
переменную или собранный f-строкой ключ сюда не попадает: сторож ловит то, что видно глазами в
коде, без догадок, поэтому и ложных тревог не даёт.

«Достижим из экрана бота» — одно из:
  1. ключ в группе настроек бота (`admin_settings.SETTINGS_GROUPS` + «📦 Прочие») или фото/файл
     события (`PHOTO_FIELDS`/`FILE_FIELDS`);
  2. в коде есть кнопка `settings_edit:<ключ>` / `settings_edit_city:<ключ>`;
  3. ключ кнопки главного меню (`keyboards.builders.MENU_BUTTONS`, экран «🔘 Кнопки меню»);
  4. ключ из таблицы своего экрана (`SCREEN_TABLES` ниже — экран строит кнопки циклом по ней);
  5. ключ-литерал в аргументах записи настройки в `handlers/` (`WRITERS`: тумблеры и т.п.);
  6. `OWN_SCREENS` — свой экран пишет ключ через переменную; для каждого назван модуль, и
     сторож проверяет, что ключ в нём действительно есть.
Остальное — `EXCEPTIONS`, с причиной для каждого ключа.
"""
from __future__ import annotations

import ast
import functools
import re
import warnings
from collections import defaultdict
from pathlib import Path

from settings_schema import SETTINGS_SCHEMA

ROOT = Path(__file__).resolve().parent.parent
CHAT_DIRS = ("handlers", "services", "keyboards")
# Корневые модули, где чтение настройки — не показ в чате: сам реестр (типизированное чтение
# внутри get_setting_typed).
ROOT_NOT_CHAT = {"settings_schema.py"}
# Функции общего с приложением кода, которые собирают экран ТОЛЬКО для приложения: их тексты
# в чат не уходят (чат-анкета строит вопросы сама, handlers/registration.py).
APP_ONLY_FUNCTIONS = {
    "reg_engine.py": {"step_spec", "_v2_texts_for"},  # спека шага формы приложения
}

# (имя функции, позиция ключа). Обёртки — те же чтения настройки: `ctx.t` (тест и запись на
# сессии), `_say` (регистрация на месте), `tr_setting`/`_tr_key`/`tr_key` (чтение + перевод),
# `_setting_or` (отзыв о сессии), `with_deadline` (запись на сессии).
READERS = {
    ("get_setting", 0), ("get_setting_typed", 0),
    ("get_setting_for_city", 0), ("get_setting_typed_for_city", 0),
    ("t", 0), ("_say", 1), ("tr_setting", 0), ("_tr_key", 0), ("tr_key", 0),
    ("_setting_or", 0), ("with_deadline", 0),
    ("ui_text", 0), ("ui_tr", 0),  # settings_ui_text_fields: подпись + дефолт при пустом
}

WRITERS = {
    "set_setting_by_admin", "delete_setting_by_admin", "_toggle_module_setting",
    "_cycle_enum_setting", "_toggle_approval_setting", "_toggle_value_setting",
    "_feedback_settings_key", "_write_key",
}

# Свой экран пишет ключ через переменную (`key = ...; set_setting_by_admin(..., key, ...)`).
OWN_SCREENS = {
    "checkin_qr_broadcast_enabled": "admin_checkin.py",
    "checkin_qr_broadcast_time": "admin_checkin.py",
    "checkin_qr_morning_repeat_time": "admin_checkin.py",
    "checkin_qr_morning_catchup_until": "admin_checkin.py",
    "checkin_volunteer_guide_broadcast_enabled": "admin_forum_functions.py",
    "checkin_volunteer_guide_broadcast_time": "admin_forum_functions.py",
    "forum_day_menu_enabled": "admin_forum_functions.py",
    "forum_day_menu_start_time": "admin_forum_functions.py",
    "forum_day_report_enabled": "admin_forum_functions.py",
    "forum_day_report_time": "admin_forum_functions.py",
    "forum_noshow_poll_enabled": "admin_forum_functions.py",
    "forum_noshow_poll_time": "admin_forum_functions.py",
    "forum_welcome_enabled": "admin_forum_functions.py",
    "regional_noshow_offer_enabled": "admin_forum_functions.py",
    "regional_noshow_offer_time": "admin_forum_functions.py",
    "regional_noshow_target_city": "admin_forum_functions.py",
    "regional_noshow_move_status": "admin_forum_functions.py",
    "forum_stats_card_enabled": "admin_forum_stats_card.py",
    "lost_found_enabled": "admin_lost_found.py",
    "city_tz_offset": "admin_forum_tz.py",
    "miniapp_theme_pattern": "admin_miniapp_theme.py",
    "onsite_reg_enabled": "admin_onsite_reg.py",
    "volunteer_invite_enabled": "admin_volunteer_invite.py",
}

# Читается в чате, но в экран бота не выводится — и почему.
EXCEPTIONS = {
    # Служебные ключи движка перевода: какой переводчик и его адрес выбирает владелец бота при
    # установке, менеджеру тут выбирать нечего.
    "delegate_lang_driver": "служебное: движок машинного перевода",
    "delegate_lang_http_url": "служебное: адрес движка машинного перевода",
    # Читаются в services/onsite_reg.py, но показываются только на плашке сканера приложения
    # (approve_at_door / wrong_city_text / rejected_reason_text зовёт miniapp/routers/checkin.py):
    # без приложения нет и сканера, править такой текст в боте незачем.
    "onsite_off_text": "плашка сканера приложения",
    "onsite_rejected_text": "плашка сканера приложения",
    "onsite_rejected_reason_text": "плашка сканера приложения",
    "onsite_wrong_city_text": "плашка сканера приложения",
    # Учебные плашки сканера (services/checkin_training.py: _demo_undo, training_point_entry) —
    # их видит только волонтёр в сканере приложения. Тексты листа учебных QR, который бот
    # присылает в чат, — в группе «🎪 Форум: тексты в чате».
    "checkin_training_note_text": "учебная плашка сканера приложения",
    "checkin_training_undo_demo_text": "учебная плашка сканера приложения",
    "checkin_undo_button_text": "кнопка отмены отметки в сканере приложения",
    # Описание того, что включает пресет «🎓 Форум СкиллАп»: меняется вместе с составом
    # пресета в коде (reg_presets.py), правка менеджером сделала бы описание неправдой.
    "skillup_preset_confirm_text": "описание пресета, привязано к коду пресета",
    # Начало имён вкладок бота в Google-таблице (settings_ops.bot_tab_prefix): в чате не
    # показывается, а смена переименовывает все вкладки бота разом — операция владельца бота.
    "sheet_tab_bot_prefix": "имя вкладок таблицы, смена переименовывает все вкладки бота",
}

def _form_v2_toggle_keys():
    from reg_engine import FORM_V2_TOGGLE_KEYS
    return [f"reg_form_{name}" for name in FORM_V2_TOGGLE_KEYS]


def _by_prefix(prefix):
    return lambda: [k for k in SETTINGS_SCHEMA if k.startswith(prefix)]


# Ключ, собранный f-строкой: начало -> какие ключи реестра так читаются. Новое начало без
# записи здесь роняет test_every_fstring_read_is_declared — решение принимает человек.
FSTRING_FAMILIES = {
    "reg_form_": _form_v2_toggle_keys,  # f"reg_form_{name}" — тумблеры «Анкета 2.0»
    "reg_prompt_": _by_prefix("reg_prompt_"),  # тексты вопросов анкеты
    "reg_multi_max_": _by_prefix("reg_multi_max_"),  # лимиты мультивыбора
    "reg_repeatable_max_": _by_prefix("reg_repeatable_max_"),  # лимит блоков повторяемого вопроса
    "game_proof_prompt_": _by_prefix("game_proof_prompt_"),  # подсказка к сдаче задания по типу
    "city_tab_suffix__": _by_prefix("city_tab_suffix__"),  # окончание вкладки листа города
}

_EDIT_CB = re.compile(r"settings_edit(?:_city)?:([a-z0-9_]+)")


def _fname(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _chat_files() -> list[Path]:
    """Код бота: три каталога и корневые модули (`reg_engine.py`, `game_labels.py`, `main.py`…),
    кроме самого реестра — там чтения служебные."""
    root = [p for p in sorted(ROOT.glob("*.py")) if p.name not in ROOT_NOT_CHAT]
    return [p for d in CHAT_DIRS for p in sorted((ROOT / d).rglob("*.py"))] + root


def _calls_outside_app_only(path: Path):
    """Вызовы модуля, кроме тел функций из `APP_ONLY_FUNCTIONS`."""
    skip = APP_ONLY_FUNCTIONS.get(path.name, set())
    stack = [_tree(path)]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in skip:
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _fstring_prefix(arg) -> str | None:
    """Начало ключа, собранного f-строкой (f"game_proof_prompt_{code}" -> "game_proof_prompt_").
    Пустое начало (f"{key}__city__{code}" — городское значение уже названного ключа) и начало,
    под которое не подходит ни один ключ реестра (`consent_pdf_`, `reg_help_`), — не чтение
    ключа реестра."""
    if not (isinstance(arg, ast.JoinedStr) and arg.values and isinstance(arg.values[0], ast.Constant)):
        return None
    prefix = arg.values[0].value
    if not prefix or not any(k.startswith(prefix) for k in SETTINGS_SCHEMA):
        return None
    return prefix


def _family_keys(prefix: str) -> list[str]:
    source = FSTRING_FAMILIES.get(prefix)
    return list(source()) if source else []


@functools.lru_cache(maxsize=None)
def _tree(path: Path) -> ast.AST:
    with warnings.catch_warnings():  # «\|» в чужих строках — не наше дело
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        return ast.parse(path.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def _scan():
    """(читатели ключа, ключи из аргументов записи, ключи кнопок settings_edit)."""
    reads: dict[str, list[str]] = defaultdict(list)
    writes: set[str] = set()
    edit_buttons: set[str] = set()
    fstring_prefixes: set[str] = set()
    for path in _chat_files():
        src = path.read_text(encoding="utf-8")
        edit_buttons |= set(_EDIT_CB.findall(src))
        rel = path.relative_to(ROOT).as_posix()
        for node in _calls_outside_app_only(path):
            name = _fname(node.func)
            for pos, arg in enumerate(node.args):
                if (name, pos) not in READERS:
                    continue
                if isinstance(arg, ast.Constant) and arg.value in SETTINGS_SCHEMA:
                    reads[arg.value].append(f"{rel}:{node.lineno}")
                prefix = _fstring_prefix(arg)
                if prefix is not None:
                    fstring_prefixes.add(prefix)
                    for key in _family_keys(prefix):
                        reads[key].append(f"{rel}:{node.lineno} (f-строка)")
            if name in WRITERS and rel.startswith("handlers/"):
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Constant) and sub.value in SETTINGS_SCHEMA:
                            writes.add(sub.value)
    return reads, writes, edit_buttons, frozenset(fstring_prefixes)


def _screen_tables() -> set[str]:
    from handlers import (admin_amb_points, admin_amb_section, admin_amb_tiers, admin_delegations,
                          admin_miniapp, admin_quiz_levels, admin_sos, session_feedback)
    from services import session_enroll

    keys = set(admin_sos._SOS_TEXT_FIELDS[f][0] for f in admin_sos._SOS_TEXT_FIELDS)
    keys |= set(admin_sos._SOS_DELAY_FIELDS.values())
    keys |= {k for k, _label in session_feedback._FEEDBACK_TEXT_FIELDS.values()}
    keys |= {k for k, _label in admin_delegations._TEXT_KEYS.values()}
    keys |= set(admin_amb_section.TEXT_KEYS)
    keys |= {entry[0] for entry in admin_amb_points._TOGGLES.values()}
    keys |= {entry[0] for entry in admin_amb_tiers._TOGGLES.values()}
    keys |= set(admin_quiz_levels._EDIT_KEYS) | set(session_enroll.ENROLL_TEXT_KEYS)
    keys |= set(admin_miniapp.SECTION_KEYS)
    # «✏️ Тексты вопросов»: кнопка на каждый шаг анкеты, общий трек и трек 🎉 Party.
    from handlers.admin_reg_percity import _prompt_steps
    for step, _label in _prompt_steps():
        keys |= {f"reg_prompt_{step}", f"reg_prompt_{step}__party"}
    return keys


def _computed_reachable(writes: set[str], edit_buttons: set[str]) -> set[str]:
    from handlers import admin_settings as st
    from keyboards.builders import MENU_BUTTONS

    keys: set[str] = set()
    for _label, token in st._settings_nav_groups():
        keys |= set(st._settings_group_keys(token))
    for prefix, _label, _prompt in st.PHOTO_FIELDS + st.FILE_FIELDS:
        keys |= {f"{prefix}_photo_file_id", f"{prefix}_doc_file_id", f"{prefix}_caption"}
    keys |= {key for key, _label in MENU_BUTTONS}
    return keys | edit_buttons | writes | _screen_tables()


def _module_mentions(module: str) -> set[str]:
    tree = _tree(ROOT / "handlers" / module)
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value in SETTINGS_SCHEMA}


def test_every_chat_read_key_is_reachable_from_a_bot_screen():
    reads, writes, edit_buttons, _prefixes = _scan()
    reachable = _computed_reachable(writes, edit_buttons) | set(OWN_SCREENS) | set(EXCEPTIONS)
    missing = sorted(k for k in reads if k not in reachable)
    assert not missing, (
        "ключ читает код чата, а в боте его не поправить — выведите его в экран бота (группа в "
        "handlers/admin_settings.py или settings_chat_fields.py) или запишите в EXCEPTIONS "
        "с причиной:\n" + "\n".join(f"  {k}  ({', '.join(reads[k][:2])})" for k in missing)
    )


def test_own_screens_really_mention_their_keys():
    stale = [f"{key} -> {module}" for key, module in OWN_SCREENS.items() if key not in _module_mentions(module)]
    assert not stale, f"в названном экране ключа нет — поправьте OWN_SCREENS: {stale}"


def test_lists_hold_only_what_the_computation_cannot_see():
    """Списки не копят мусор: ключ, который больше не читается в чате или уже виден из экрана
    по вычислению, из OWN_SCREENS/EXCEPTIONS убирается; ключ — ровно в одном из списков."""
    reads, writes, edit_buttons, _prefixes = _scan()
    computed = _computed_reachable(writes, edit_buttons)
    assert not set(OWN_SCREENS) & set(EXCEPTIONS)
    for name, listed in (("OWN_SCREENS", OWN_SCREENS), ("EXCEPTIONS", EXCEPTIONS)):
        not_read = sorted(k for k in listed if k not in reads)
        assert not not_read, f"{name}: код чата их больше не читает — уберите: {not_read}"
        already = sorted(k for k in listed if k in computed)
        assert not already, f"{name}: уже достижимы из экрана бота — уберите: {already}"


def test_every_reader_form_is_still_in_use():
    """Обёртка, переименованная в коде, молча выпала бы из READERS — сторож ослеп бы на её
    ключах. Каждая форма из READERS обязана встречаться хотя бы раз."""
    seen: set[tuple[str, int]] = set()
    for path in _chat_files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call):
                for pos, arg in enumerate(node.args):
                    if isinstance(arg, ast.Constant) and arg.value in SETTINGS_SCHEMA:
                        seen.add((_fname(node.func), pos))
    assert not READERS - seen, f"формы чтения больше не встречаются: {sorted(READERS - seen)}"


def test_new_chat_text_groups_are_screens_of_the_bot():
    """Группы settings_chat_fields.py — настоящие экраны: строка раздела ведёт в группу, у
    каждого ключа есть подпись и пояснение для менеджера, ключ не задвоен с другой группой."""
    from handlers import admin_sections as sec
    from handlers import admin_settings as st
    from settings_chat_fields import CHAT_TEXT_GROUPS

    for _label, token, keys in CHAT_TEXT_GROUPS:
        assert sec.section_of(f"settings_group:{token}"), token
        for key in keys:
            assert SETTINGS_SCHEMA[key]["label"] and SETTINGS_SCHEMA[key]["prompt"], key
    grouped = [k for _l, _t, keys in st.SETTINGS_GROUPS for k in keys]
    assert len(grouped) == len(set(grouped)), "ключ в двух группах бота"


def test_every_fstring_read_is_declared():
    """Ключ, собранный f-строкой, сторож иначе не увидел бы: каждое такое начало обязано быть
    в FSTRING_FAMILIES (и наоборот — запись без чтения в коде убирается)."""
    _reads, _writes, _buttons, prefixes = _scan()
    assert set(prefixes) == set(FSTRING_FAMILIES), (
        f"не описаны: {sorted(set(prefixes) - set(FSTRING_FAMILIES))}; "
        f"больше не встречаются: {sorted(set(FSTRING_FAMILIES) - set(prefixes))}"
    )
