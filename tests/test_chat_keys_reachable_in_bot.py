"""Сторож: ключ реестра, который читает код чата, можно поправить из админки бота.

Правило проекта — менеджер настраивает всё сам, в том числе на событии без приложения. Ключ,
текст которого бот показывает в чате, но который правится только в приложении, — дыра: на таком
событии его не поменять никак. Так жили оффер реф-ссылки, кнопки догонялки, «Не присылать
сегодня», тексты анкеты в чате и четыре текста SOS.

«Читает код чата» определяется статически, по AST: вызов чтения настройки, у которого ключ —
строковый литерал (`READERS`: сами `get_setting*` и обёртки над ними с тем же смыслом), в
`handlers/`, `services/`, `keyboards/` (не `miniapp/` — это код приложения). Ключ может прийти и
через имя, если оно связано литералом ключа реестра (`_resolver`): присваивание в том же модуле
(`KEY = "…"`, `KEY: str = "…"`, `key, default = "…", "…"`), `from <модуль чата> import KEY [as X]`
и `mod.KEY`, где `mod` — импортированный модуль чата. Имя, не связанное литералом, пропускается;
ключ, собранный f-строкой, — через `FSTRING_FAMILIES`. Сторож ловит то, что видно глазами в коде,
без догадок, поэтому и ложных тревог не даёт.

Тексты, которые в чат шлёт сам код приложения (`miniapp/`: копии файлов, итог анкеты), объявлены
в `APP_CHAT_SENDERS`: функция-отправитель -> ключи её текстов. Новый отправитель без записи,
устаревшая запись и ключ записи без экрана бота роняют свои тесты.

«Достижим из экрана бота» — одно из:
  1. ключ в группе настроек бота (`admin_settings.SETTINGS_GROUPS` + «📦 Прочие») или фото/файл
     события (`PHOTO_FIELDS`/`FILE_FIELDS`);
  2. в коде есть кнопка `settings_edit:<ключ>` / `settings_edit_city:<ключ>`;
  3. ключ кнопки главного меню (`keyboards.builders.MENU_BUTTONS`, экран «🔘 Кнопки меню»);
  4. ключ из таблицы своего экрана (`SCREEN_TABLES` ниже — экран строит кнопки циклом по ней);
  5. ключ в аргументах записи настройки в `handlers/` (`WRITERS`: тумблеры и т.п.) — литералом
     или через имя по тем же правилам, что у чтения;
  6. `OWN_SCREENS` — свой экран пишет ключ через переменную; для каждого назван модуль, и
     сторож проверяет, что ключ в нём действительно есть.
Остальное — `EXCEPTIONS`, с причиной для каждого ключа.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import ast
import functools
import re
import warnings
from collections import defaultdict
from pathlib import Path

from domain.settings.schema import SETTINGS_SCHEMA

ROOT = REPO_ROOT
CHAT_DIRS = ("handlers", "services", "keyboards", "domain", "shared")
# Модули, где чтение настройки — не показ в чате: сам реестр (типизированное чтение
# внутри get_setting_typed).
NOT_CHAT = {"domain/settings/schema.py"}
# Функции общего с приложением кода, которые собирают экран ТОЛЬКО для приложения: их тексты
# в чат не уходят (чат-анкета строит вопросы сама, handlers/registration.py).
APP_ONLY_FUNCTIONS = {
    "domain/regform/engine.py": {"step_spec", "_v2_texts_for"},  # спека шага формы приложения
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
    "session_remind_text": "handlers/forum/admin_session_remind.py",
    "session_morning_head": "handlers/forum/admin_session_remind.py",
    "checkin_qr_broadcast_time": "handlers/forum/admin_checkin.py",
    "checkin_qr_morning_repeat_time": "handlers/forum/admin_checkin.py",
    "checkin_qr_morning_catchup_until": "handlers/forum/admin_checkin.py",
    "miniapp_theme_pattern": "handlers/settings/admin_miniapp_theme.py",
}

# Читается в чате, но в экран бота не выводится — и почему.
EXCEPTIONS = {
    # Служебные ключи движка перевода: какой переводчик и его адрес выбирает владелец бота при
    # установке, менеджеру тут выбирать нечего.
    "delegate_lang_driver": "служебное: движок машинного перевода",
    "delegate_lang_http_url": "служебное: адрес движка машинного перевода",
    # Читаются в services/forum/onsite_reg.py, но показываются только на плашке сканера приложения
    # (approve_at_door / wrong_city_text / rejected_reason_text зовёт miniapp/routers/checkin.py):
    # без приложения нет и сканера, править такой текст в боте незачем.
    "onsite_off_text": "плашка сканера приложения",
    "onsite_rejected_text": "плашка сканера приложения",
    "onsite_rejected_reason_text": "плашка сканера приложения",
    "onsite_wrong_city_text": "плашка сканера приложения",
    # Учебные плашки сканера (services/forum/checkin_training.py: _demo_undo, training_point_entry) —
    # их видит только волонтёр в сканере приложения. Тексты листа учебных QR, который бот
    # присылает в чат, — в группе «🎪 Форум: тексты в чате».
    "checkin_training_note_text": "учебная плашка сканера приложения",
    "checkin_training_undo_demo_text": "учебная плашка сканера приложения",
    "checkin_undo_button_text": "кнопка отмены отметки в сканере приложения",
    # Описание того, что включает пресет «🎓 Форум СкиллАп»: меняется вместе с составом
    # пресета в коде (domain/regform/presets.py), правка менеджером сделала бы описание неправдой.
    "skillup_preset_confirm_text": "описание пресета, привязано к коду пресета",
    # Начало имён вкладок бота в Google-таблице (settings_ops.bot_tab_prefix): в чате не
    # показывается, а смена переименовывает все вкладки бота разом — операция владельца бота.
    "sheet_tab_bot_prefix": "имя вкладок таблицы, смена переименовывает все вкладки бота",
    # id/название привязанного чата делегатов и чата SOS (services/chat_tracking.py,
    # services/sos.py) пишет сама привязка чата — вводить их руками незачем (как на экране «💬 Чат»).
    "delegate_chat_id": "id/название привязанного чата пишет сама привязка чата, руками не вводится",
    "delegate_chat_title": "id/название привязанного чата пишет сама привязка чата, руками не вводится",
    "sos_chat_id": "id/название привязанного чата пишет сама привязка чата, руками не вводится",
    "sos_chat_title": "id/название привязанного чата пишет сама привязка чата, руками не вводится",
}

# Тексты, которые в чат шлёт код приложения: «miniapp/<путь>.py::<функция>» -> ключи реестра,
# текст которых функция отправляет. Функция-отправитель — любая функция в miniapp/, где есть
# вызов из SEND_PRIMITIVES; новая такая функция без записи здесь роняет
# test_every_app_chat_sender_is_declared — решение принимает человек.
APP_CHAT_SENDERS = {
    # Итог подачи анкеты в чат: отказ по правилу автоотказа, «Заявка принята», правка/повторная
    # подача (resolve_delegate_text из services/registration/reg_finalize.py — её ключи видит
    # сторож чата).
    "miniapp/routers/form.py::draft_submit": (
        "reject_text", "reg_form_complete_heading_text", "reg_form_complete_body_text",
    ),
    "miniapp/routers/submissions.py::_upload_resume": ("miniapp_upload_caption_resume",),
    "miniapp/routers/submissions.py::_upload_part": (
        "miniapp_upload_caption_delegate", "miniapp_upload_caption_staff", "miniapp_upload_caption_settings",
    ),
    # Карточку товара собирает код бота (services/game/shop_card.py) — её ключи видит сторож чата.
    "miniapp/routers/checkin_shop.py::scan": (),
    # Ответ организатора: текст пишет человек, заголовок — org_reply_title из кода бота.
    "miniapp/routers/questions.py::questions_answer": (),
    # Текст уведомления приходит аргументом от вызывающего кода.
    "miniapp/routers/review.py::_notify_delegate": (),
    "miniapp/routers/review.py::_sender": (),
}
SEND_PRIMITIVES = {
    "send_message", "send_document", "send_photo", "send_media_group",
    "send_or_queue_text", "send_or_queue_text_due",
}

def _form_v2_toggle_keys():
    from domain.regform.engine import FORM_V2_TOGGLE_KEYS
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
    """Код бота: каталоги слоёв и корневые модули (`main.py`…), кроме самого реестра — там
    чтения служебные."""
    files = [p for d in CHAT_DIRS for p in sorted((ROOT / d).rglob("*.py"))] + sorted(ROOT.glob("*.py"))
    return [p for p in files if _rel(p) not in NOT_CHAT]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _calls_outside_app_only(path: Path):
    """Вызовы модуля, кроме тел функций из `APP_ONLY_FUNCTIONS`."""
    skip = APP_ONLY_FUNCTIONS.get(_rel(path), set())
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


def _schema_const(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in SETTINGS_SCHEMA:
        return node.value
    return None


@functools.lru_cache(maxsize=None)
def _bindings(path: Path) -> dict[str, frozenset[str]]:
    """Имя -> ключи реестра, которыми оно связано литералом в модуле (на любом уровне
    вложенности): `NAME = "key"`, `NAME: str = "key"`, `a, b = "key", "default"`."""
    found: dict[str, set[str]] = defaultdict(set)

    def bind(target, value):
        if isinstance(target, ast.Name):
            key = _schema_const(value)
            if key:
                found[target.id].add(key)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            if len(target.elts) == len(value.elts):
                for t, v in zip(target.elts, value.elts):
                    bind(t, v)

    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            bind(node.target, node.value)
    return {name: frozenset(keys) for name, keys in found.items()}


@functools.lru_cache(maxsize=None)
def _chat_file_set() -> frozenset[Path]:
    return frozenset(_chat_files())


def _module_file(dotted: str) -> Path | None:
    """Файл модуля чата по точечному имени (`pkg/mod.py` или `pkg/mod/__init__.py`)."""
    base = ROOT.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate in _chat_file_set():
            return candidate
    return None


def _absolute(path: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = path.relative_to(ROOT).parent.parts
    package = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*package, *([node.module] if node.module else [])])


@functools.lru_cache(maxsize=None)
def _resolver(path: Path):
    """Функция node -> ключи реестра: литерал — сам ключ; имя — связывание в модуле или
    `from <модуль чата> import NAME [as X]`; `mod.NAME` — связывание NAME в импортированном
    модуле чата. Остальное — пусто (без догадок)."""
    names: dict[str, set[str]] = defaultdict(set)
    for name, keys in _bindings(path).items():
        names[name] |= keys
    modules: dict[str, Path] = {}
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    target = _module_file(alias.name)
                    if target is not None:
                        modules[alias.asname] = target
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(path, node)
            for alias in node.names:
                local = alias.asname or alias.name
                submodule = _module_file(f"{base}.{alias.name}" if base else alias.name)
                if submodule is not None:
                    modules[local] = submodule
                    continue
                source = _module_file(base) if base else None
                if source is not None:
                    names[local] |= _bindings(source).get(alias.name, frozenset())

    def resolve(node) -> set[str]:
        key = _schema_const(node)
        if key:
            return {key}
        if isinstance(node, ast.Name):
            return set(names.get(node.id, ()))
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in modules:
            return set(_bindings(modules[node.value.id]).get(node.attr, ()))
        return set()

    return resolve


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
        res = _resolver(path)
        for node in _calls_outside_app_only(path):
            name = _fname(node.func)
            for pos, arg in enumerate(node.args):
                if (name, pos) not in READERS:
                    continue
                via = "" if isinstance(arg, ast.Constant) else f" (через {ast.unparse(arg)})"
                for key in sorted(res(arg)):
                    reads[key].append(f"{rel}:{node.lineno}{via}")
                prefix = _fstring_prefix(arg)
                if prefix is not None:
                    fstring_prefixes.add(prefix)
                    for key in _family_keys(prefix):
                        reads[key].append(f"{rel}:{node.lineno} (f-строка)")
            if name in WRITERS and rel.startswith("handlers/"):
                for arg in node.args:
                    for sub in ast.walk(arg):
                        writes |= res(sub)
    return reads, writes, edit_buttons, frozenset(fstring_prefixes)


def _screen_tables() -> set[str]:
    from handlers.amb import admin_amb_points, admin_amb_section, admin_amb_tiers
    from handlers.delegations import admin_delegations
    from handlers.settings import admin_miniapp
    from handlers.forum import admin_quiz_levels, admin_sos, session_feedback
    from services.forum import session_enroll

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
    from handlers.regform.admin_reg_percity import _prompt_steps
    for step, _label in _prompt_steps():
        keys |= {f"reg_prompt_{step}", f"reg_prompt_{step}__party"}
    return keys


def _computed_reachable(writes: set[str], edit_buttons: set[str]) -> set[str]:
    from handlers.settings import admin_settings as st
    from keyboards.builders import MENU_BUTTONS

    keys: set[str] = set()
    for _label, token in st._settings_nav_groups():
        keys |= set(st._settings_group_keys(token))
    for prefix, _label, _prompt in st.PHOTO_FIELDS + st.FILE_FIELDS:
        keys |= {f"{prefix}_photo_file_id", f"{prefix}_doc_file_id", f"{prefix}_caption"}
    keys |= {key for key, _label in MENU_BUTTONS}
    return keys | edit_buttons | writes | _screen_tables()


def _module_mentions(module: str) -> set[str]:
    tree = _tree(ROOT / module)
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value in SETTINGS_SCHEMA}


_FIX_HINT = (
    "выведите ключ в группу domain/settings/chat_fields.py (строка (\"group\", токен) в "
    "handlers/settings/admin_sections.SECTIONS) или запишите в EXCEPTIONS с причиной"
)


def test_every_chat_read_key_is_reachable_from_a_bot_screen():
    reads, writes, edit_buttons, _prefixes = _scan()
    reachable = _computed_reachable(writes, edit_buttons) | set(OWN_SCREENS) | set(EXCEPTIONS)
    missing = sorted(k for k in reads if k not in reachable)
    assert not missing, (
        "ключ читает код чата, а в боте его не поправить — " + _FIX_HINT + ":\n"
        + "\n".join(f"  {k}  ({', '.join(reads[k][:2])})" for k in missing)
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
    app_keys = {k for keys in APP_CHAT_SENDERS.values() for k in keys}
    for name, listed in (("OWN_SCREENS", OWN_SCREENS), ("EXCEPTIONS", EXCEPTIONS)):
        not_read = sorted(k for k in listed if k not in reads and k not in app_keys)
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
    """Группы domain/settings/chat_fields.py — настоящие экраны: строка раздела ведёт в группу, у
    каждого ключа есть подпись и пояснение для менеджера, ключ не задвоен с другой группой."""
    from handlers.settings import admin_sections as sec
    from handlers.settings import admin_settings as st
    from domain.settings.chat_fields import CHAT_TEXT_GROUPS

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


def _app_function_calls(func):
    """Вызовы тела функции без вложенных функций (у тех своя запись; лямбды — часть тела)."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


@functools.lru_cache(maxsize=None)
def _app_functions() -> dict[str, tuple[Path, ast.AST]]:
    """«miniapp/<путь>.py::<функция>» -> (файл, узел) для всех функций приложения."""
    found = {}
    for path in sorted((ROOT / "miniapp").rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[f"{_rel(path)}::{node.name}"] = (path, node)
    return found


def _app_senders() -> set[str]:
    return {
        ref for ref, (_path, func) in _app_functions().items()
        if any(_fname(call.func) in SEND_PRIMITIVES for call in _app_function_calls(func))
    }


def test_every_app_chat_sender_is_declared():
    """Функция приложения, которая шлёт в чат, объявлена в APP_CHAT_SENDERS, и наоборот."""
    senders = _app_senders()
    undeclared = sorted(senders - set(APP_CHAT_SENDERS))
    stale = sorted(set(APP_CHAT_SENDERS) - senders)
    assert not undeclared, (
        "функция приложения шлёт в чат, а в APP_CHAT_SENDERS её нет — допишите запись "
        f"«путь::функция» -> ключи реестра её текстов (пустой кортеж — с причиной): {undeclared}"
    )
    assert not stale, f"APP_CHAT_SENDERS: функции нет или она больше не шлёт в чат — уберите: {stale}"


def test_app_chat_sender_keys_are_mentioned():
    """Запись не устаревает молча: каждый объявленный ключ есть в теле своей функции —
    литералом или через имя, связанное литералом."""
    functions = _app_functions()
    stale = []
    for ref, keys in APP_CHAT_SENDERS.items():
        if ref not in functions:
            continue  # отсутствующую функцию называет test_every_app_chat_sender_is_declared
        path, func = functions[ref]
        res = _resolver(path)
        mentioned = set()
        for node in ast.walk(func):
            mentioned |= res(node)
        stale += [f"{ref}: {key}" for key in keys if key not in mentioned]
    assert not stale, f"ключа нет в теле функции — поправьте APP_CHAT_SENDERS: {stale}"


def test_app_chat_sender_keys_are_reachable_from_a_bot_screen():
    """Текст, который в чат шлёт приложение, правится и из бота (событие без приложения)."""
    _reads, writes, edit_buttons, _prefixes = _scan()
    reachable = _computed_reachable(writes, edit_buttons) | set(OWN_SCREENS) | set(EXCEPTIONS)
    missing = sorted(
        f"{key}  ({ref})" for ref, keys in APP_CHAT_SENDERS.items() for key in keys if key not in reachable
    )
    assert not missing, (
        "приложение шлёт этот текст в чат, а в боте его не поправить — " + _FIX_HINT + ":\n  "
        + "\n  ".join(missing)
    )
