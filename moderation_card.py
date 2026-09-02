"""Quick 260902-tzh: сервис карточки заявки — какие ответы анкеты показывать модератору,
как их читать из `users`, как обрезать длинные значения и не влезающую в лимит Telegram
карточку.

Корневой модуль (сосед `reg_engine.py`/`reg_labels.py`/`settings_schema.py`) — БЕЗ импорта
aiogram, БЕЗ импорта `miniapp.*`/`handlers.*`. Единственный источник схемы анкеты —
`reg_engine.STEP_TO_COLUMN`/`reg_engine.label_for`; второй карты «шаг → подпись» здесь нет и
не будет (план 21-13 закрыл алиасы, заводить их заново запрещено).

Логика составных/булевых колонок (`_EXTRA_ANSWER_COLUMNS_BY_STEP`/`_BOOL_COLUMNS`/значение)
повторяет `miniapp/routers/profile.py::_profile_columns/_value` — тот же приём, другой набор
вопросов (карточка модератора печатает не все вопросы анкеты, а выбранные тумблерами; резюме
печатает свой блок в самом рендере карточки, здесь не дублируется).
"""
from __future__ import annotations

import reg_engine
from reg_labels import REG_LABELS

# Вопрос анкеты (step_key) -> человеческая подпись, ТОЛЬКО через reg_engine.label_for —
# движок сам знает про девять шагов, где setting_key расходится с step_key (план 21-13).
# Шаги без подписи в REG_LABELS (например full_name — не «вопрос анкеты», а поле профиля,
# спрашивается вне REG_FLOW) в набор не попадают.
CARD_STEPS: dict[str, str] = {
    step_key: reg_engine.label_for(step_key)
    for step_key in reg_engine.STEP_TO_COLUMN
    if reg_engine.label_key_for(step_key) in REG_LABELS
}

# Единственное составное поле карточки — ожидания на русском/арабском через « / ». Резюме
# печатает свой блок в _render_application_card (файлом/текстом/нет), сюда не дублируется.
_EXTRA_ANSWER_COLUMNS_BY_STEP: dict[str, tuple[str, ...]] = {
    "expectations": ("expectations", "expectations_ar"),
}

# Булевы колонки показываем словом, а не 0/1 — как в профиле Mini App/таблице.
_BOOL_COLUMNS: dict[str, tuple[str, str | None]] = {
    "work_status": ("Да", "Нет"),
    "is_ambassador_candidate": ("Да", None),
}

# 20 вопросов, разумных для первого экрана отбора (Таня, план 260902-tzh) — стартовый набор
# реестра modcard_fields; менеджер меняет тумблерами на экране «🧾 Поля карточки заявки».
DEFAULT_CARD_STEPS: tuple[str, ...] = (
    "age", "city", "education_status", "university", "course", "local_committee",
    "position", "alumni_status", "aiesec_role", "source", "work_sphere",
    "english_level", "attendance_format", "goal", "expectations", "exp_organizers",
    "exp_content", "missing_skills", "volunteer", "resume",
)

# Реестр type:"list" отдаёт `default` на falsy raw (settings_schema._parse_setting) — пустая
# строка молча вернула бы дефолтные 20 вопросов, противоположность тому, что нажал менеджер.
# Пустой набор пишется этим сентинелом (тот же приём, что role_caps_* — handlers/admin_roles.py).
EMPTY_SENTINEL = "—"

ANSWER_LIMIT_DEFAULT = 300
CARD_TEXT_LIMIT = 3900
TELEGRAM_LIMIT = 4096
OVERFLOW_HINT = "…\n📄 Ответы целиком — кнопка ниже"


def enabled_steps(raw: list[str] | None) -> list[str]:
    """Список включённых вопросов из значения реестра `modcard_fields`, отфильтрованный по
    известным шагам и приведённый к порядку `CARD_STEPS` (не порядку хранения в БД)."""
    if not raw:
        return list(DEFAULT_CARD_STEPS)
    if list(raw) == [EMPTY_SENTINEL]:
        return []
    chosen = set(raw) & set(CARD_STEPS)
    return [step for step in CARD_STEPS if step in chosen]


def _column_value(user: dict, column: str) -> str | None:
    """Дословное поведение `miniapp/routers/profile.py::_value` — единственный источник
    приёма «булева колонка → слово, пустое/None → нет строки»."""
    raw = user.get(column)
    if column in _BOOL_COLUMNS:
        yes, no = _BOOL_COLUMNS[column]
        return yes if raw else no
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def answer_value(user: dict, step_key: str) -> str | None:
    """Ответ на один вопрос анкеты для карточки, или `None` — пусто/нет значения."""
    columns = _EXTRA_ANSWER_COLUMNS_BY_STEP.get(step_key, (reg_engine.STEP_TO_COLUMN[step_key],))
    parts = [v for v in (_column_value(user, c) for c in columns) if v]
    return " / ".join(parts) if parts else None


def card_answers(user: dict, steps: list[str], limit: int | None) -> list[tuple[str, str]]:
    """`[(label, value)]` для непустых ответов на выбранные вопросы, в порядке `steps`;
    значение длиннее `limit` обрезается до `limit` символов + «…». `limit=None` — без
    обрезки (экран «📄 Полная анкета», appr_full: ответ печатается целиком)."""
    out: list[tuple[str, str]] = []
    for step_key in steps:
        value = answer_value(user, step_key)
        if not value:
            continue
        if limit is not None and len(value) > limit:
            value = value[:limit] + "…"
        out.append((CARD_STEPS.get(step_key, step_key), value))
    return out


def fit_card(text: str, limit: int = CARD_TEXT_LIMIT) -> tuple[str, bool]:
    """Обрезать текст карточки до `limit` по границе строки, если он длиннее. Возвращает
    `(text, overflowed)`; при переполнении хвост режется по последней помещающейся строке и
    заканчивается `OVERFLOW_HINT`."""
    if len(text) <= limit:
        return text, False
    budget = limit - len(OVERFLOW_HINT)
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        addition = len(line) + (1 if kept else 0)
        if used + addition > budget:
            break
        kept.append(line)
        used += addition
    cut = "\n".join(kept)
    return f"{cut}\n{OVERFLOW_HINT}" if cut else OVERFLOW_HINT, True


def split_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Разбить длинный текст на куски ≤ `limit` по границам строк — склейка через «\\n»
    равна исходнику, ни одна строка не режется посередине."""
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for line in lines:
        addition = len(line) + (1 if current else 0)
        if current and used + addition > limit:
            chunks.append("\n".join(current))
            current = []
            used = 0
            addition = len(line)
        current.append(line)
        used += addition
    if current:
        chunks.append("\n".join(current))
    return chunks
