"""Quick 260906 (UAT-фикс 27-05) — сторож регрессии «делегатский текст отправлен мимо шва
перевода». Стендовый UAT нашёл ровно этот класс бага: `handlers/registration.py` посылал
вопросы/баннеры анкеты через `message.answer(...)`/`callback.message.answer(...)` напрямую,
минуя `_safe_answer`/`reg_i18n.say`/`reg_i18n.tr_text`/`tr_kb` — делегат с `lang="en"` видел
русский текст, даже когда сам корпус уже был переведён (`translations` заполнена).

Метод — статический AST-разбор (не построчный grep, чтобы не спотыкаться о перенос строк в
многострочных вызовах): для каждого файла из `SCANNED_FILES` находим вызовы
`<получатель>.answer(...)`/`.answer_document(...)`/`.answer_photo(...)`/`.edit_text(...)`, где
получатель похож на объект чата делегата (`message`, `callback.message`, `tap_message`, ...,
НЕ `callback` сам по себе — алерты `callback.answer(...)` идут через `reg_i18n.tr_for`
отдельно, не через этот шов). Для каждого такого вызова первый позиционный аргумент обязан
либо визуально проходить через `reg_i18n.tr_text/tr_kb/say/tr_for` (инлайн или через
переприсвоенную переменную в той же функции), либо явно фигурировать в `ALLOWED_RAW_SENDS`
ниже — с обоснованием, почему машинный перевод здесь не нужен (LANG-09 согласие,
админ-адресованный текст).

Файлы сторожа — только те, что реально правились в этом квике (`handlers/registration.py`,
`handlers/reg_flow.py`, `handlers/reg_consent.py`); `reg_steps.py`/`reg_flow.py`'s остальные
хендлеры/`reg_resume.py`/`reg_handoff.py` уже покрыты собственными сторожами плана 27-05
(`tests/test_i18n_bot_render_27.py`, `tests/test_i18n_service_words_27.py`) — не
переизобретаем."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCANNED_FILES = [
    ROOT / "handlers" / "registration.py",
    ROOT / "handlers" / "reg_flow.py",
    ROOT / "handlers" / "reg_consent.py",
]

# Функции, которые ЯВЛЯЮТСЯ швом (реализуют перевод изнутри и потом сами шлют raw .answer/
# .answer_photo) — не потребители шва, поэтому не сканируются как «подозрительные».
SEAM_DEFINING_FUNCS = {"_safe_answer", "_send_welcome"}

# Методы отправки делегату текста, которые обязаны идти через перевод.
_SEND_ATTRS = {"answer", "answer_document", "answer_photo", "edit_text"}

# Маркеры «этот аргумент точно прошёл перевод инлайн» — подстрокой, не через AST-типы: и
# f-строки, и .format(), и html.escape(...) вокруг — обёртки должны просто где-то содержать
# вызов шва.
_WIRED_MARKERS = ("reg_i18n.tr_text(", "reg_i18n.tr_kb(", "reg_i18n.say(", "reg_i18n.tr_for(")
# Функции переприсвоения переменной через шов ("var = reg_i18n.tr_text(var, ...)" и т.п.) —
# та же идиома, которой написаны все правки этого квика.
_REASSIGN_FUNCS = ("tr_text", "tr_kb", "tr_for", "say")

# ── Явные, задокументированные исключения (НЕ регрессия) ───────────────────────────────────
# Ключ — (относительный путь файла, имя функции, исходный текст первого аргумента ast.unparse).
ALLOWED_RAW_SENDS = {
    (
        "handlers/registration.py", "_ask_step", "caption",
    ): (
        "LANG-09: caption — САМ текст вопроса согласия (та же карточка и тот же вызов "
        "_prompt(f'consent_{consent_key}', label), что и handlers/reg_consent.py::"
        "_send_renew_card, где это уже документировано как исключение) — машинный перевод "
        "согласий запрещён."
    ),
    (
        "handlers/registration.py", "cmd_start",
        "'Вы админ — можете пройти регистрацию заново для теста.'",
    ): (
        "Адресовано менеджеру-админу (user_id in config.ADMIN_IDS), не делегату — админка "
        "бота остаётся русской, языковой модуль анкеты сюда не относится."
    ),
    (
        "handlers/reg_consent.py", "_send_renew_card", "caption",
    ): (
        "LANG-09 (документировано в докстринге _send_renew_card): caption — САМ текст "
        "согласия при пересогласии, PDF/подпись кнопки тоже остаются русскими."
    ),
}


def _looks_like_message_receiver(receiver_src: str) -> bool:
    """True для message/callback.message/tap_message/... — НЕ для голого `callback`
    (`callback.answer(...)` — всплывающий алерт, переводится через reg_i18n.tr_for на месте
    вызова, отдельный контракт, не этот шов)."""
    tail = receiver_src.rsplit(".", 1)[-1]
    return tail in {"message", "msg", "tap_message"}


def _base_identifier(expr_src: str) -> str | None:
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\b", expr_src)
    return m.group(1) if m else None


def _is_wired(func_source: str, arg_src: str) -> bool:
    if any(marker in arg_src for marker in _WIRED_MARKERS):
        return True
    base = _base_identifier(arg_src)
    if base:
        # Переприсвоение через шов где-то раньше в тексте функции (тот же паттерн, что везде
        # в этом квике: "var = reg_i18n.tr_text(var, ...)", "var = html.escape(reg_i18n.tr_text
        # (...))", "var = await reg_i18n.tr_for(...)" — reg_i18n.* может быть не сразу после
        # "=", поэтому `.*?` между ними, а не якорь сразу за знаком равенства). Матчим по
        # базовому идентификатору, даже если сам аргумент вызова — выражение над ним
        # (`template.format(...)`, `html.escape(text)`), а не голое имя.
        pattern = (
            rf"\b{re.escape(base)}\s*=\s*.*?reg_i18n\."
            rf"(?:{'|'.join(_REASSIGN_FUNCS)})\("
        )
        if re.search(pattern, func_source):
            return True
    return False


def _text_bearing_arg(call: ast.Call, attr: str) -> ast.AST | None:
    """Аргумент, который реально несёт видимый делегату текст: первый позиционный для
    answer/edit_text, `caption=` для answer_document/answer_photo (эти отправки шлют
    подпись, а не .args[0], который — file_id/InputFile, не текст)."""
    if attr in {"answer_document", "answer_photo"}:
        for kw in call.keywords:
            if kw.arg == "caption":
                return kw.value
        return None  # без caption — нет делегатского текста, ничего проверять
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "text":
            return kw.value
    return None


def _find_unwired_sends(path: Path):
    """Возвращает список (func_name, lineno, arg_src) для «подозрительных» отправок в файле —
    вызовов .answer/.answer_document/.answer_photo/.edit_text на message-подобном получателе,
    чей текстовый аргумент визуально не проходит перевод."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_name = node.name
        if func_name in SEAM_DEFINING_FUNCS:
            continue
        func_source = "\n".join(lines[node.lineno - 1: node.end_lineno])

        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in _SEND_ATTRS:
                continue
            receiver_src = ast.unparse(call.func.value)
            if not _looks_like_message_receiver(receiver_src):
                continue
            text_arg = _text_bearing_arg(call, call.func.attr)
            if text_arg is None:
                continue
            arg_src = ast.unparse(text_arg)
            if _is_wired(func_source, arg_src):
                continue
            findings.append((func_name, call.lineno, arg_src))

    return findings


def test_no_unwired_delegate_sends_outside_allowlist():
    violations = []
    for path in SCANNED_FILES:
        rel = path.relative_to(ROOT).as_posix()
        for func_name, lineno, arg_src in _find_unwired_sends(path):
            key = (rel, func_name, arg_src)
            if key in ALLOWED_RAW_SENDS:
                continue
            violations.append(f"{rel}:{lineno} in {func_name}() -> answer({arg_src!r}, ...)")

    assert not violations, (
        "Найдены отправки делегату мимо шва перевода (reg_i18n.tr_text/tr_kb/say/tr_for) — "
        "либо оберните вызов через шов, либо явно добавьте обоснованное исключение в "
        "ALLOWED_RAW_SENDS (tests/test_registration_send_guard_260906.py):\n  "
        + "\n  ".join(violations)
    )


def test_allowlist_entries_still_exist_in_source():
    """Обратная проверка: если код изменился так, что allowlist-запись больше не матчится
    НИ ОДНОМУ реальному вызову — запись протухла (переименование/удаление сайта) и должна
    быть убрана, иначе она молча перестаёт что-либо проверять."""
    all_found = set()
    for path in SCANNED_FILES:
        rel = path.relative_to(ROOT).as_posix()
        for func_name, _lineno, arg_src in _find_unwired_sends(path):
            all_found.add((rel, func_name, arg_src))

    stale = [key for key in ALLOWED_RAW_SENDS if key not in all_found]
    assert not stale, f"Устаревшие записи ALLOWED_RAW_SENDS (сайт не найден): {stale!r}"
