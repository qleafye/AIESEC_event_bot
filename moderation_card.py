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

Приёмка 19.09 (review-260919, раздел «Модерация», находки №1/№2/№3): два фикса поверх схемы
выше, оба — единая точка правды для бота (`handlers/admin_moderation.py`) и веба
(`services/applications.py::card_payload`), второй копии условий не заводим ни там, ни там.
1. `_column_value` перестаёт печатать сентинел `"-"` (`reg_engine`'а «вопрос пропущен/выключен
   на анкете», см. `reg_engine.py`, предикат `value not in (None, "", "-")`, использован
   ~десяток раз) как настоящий ответ — карточка печатала «Поле: -» для КАЖДОГО выключенного
   вопроса, чей столбец получил сентинел при финализации анкеты (34/34 заявок в очереди).
2. `age`/`birth_date` — тот же вопрос, переключённый 16-17.09 (`age` off, `birth_date` on):
   `_age_birth_date_line`/дедуп в `card_answers` показывают то значение, которое реально есть
   у ЭТОГО делегата, независимо от того, какой из двух шагов менеджер выбрал в «Полях карточки
   заявки» — без этого 1330 заявок до переключения показывали пустоту вместо возраста.
3. `resume_summary`/`mini_resume_fields` — развилка резюме (`reg_resume_mode=fork`), ветка
   «мини-профиль»: карточка резюме раньше умела показать файл/ссылку/текст, но не мини-профиль
   (32 делегата читались как «резюме не приложено», 8 уже отклонены). `warning=True` — сигнал
   потери данных (`resume_type` задан, но ни одно из его полей не заполнено), а не «нет
   резюме» — так вызывающий отличает пустую анкету от бага.
"""
from __future__ import annotations

from datetime import datetime

import reg_engine
from reg_labels import REG_LABELS
from services.timeutil import msk_now

# Вопрос анкеты (step_key) -> человеческая подпись, ТОЛЬКО через reg_engine.label_for —
# движок сам знает про девять шагов, где setting_key расходится с step_key (план 21-13).
# Шаги без подписи в REG_LABELS в набор не попадают.
# Приёмка 16.09 (п.1): ФИО (`reg_engine.FULL_NAME_STEP`) исключён ЯВНО, а не по отсутствию
# подписи — с 16.09 подпись `reg_q_full_name` у него есть (нужна мастеру приложения), но
# ФИО не «вопрос анкеты» для карточки заявки, а поле профиля: карточка и так печатает имя
# в заголовке (`_render_application_card`), второй раз строкой ответа его не показываем —
# иначе modcard_fields (тумблеры «Поля карточки заявки») предложил бы менеджеру выключить
# то, что от него не зависит.
CARD_STEPS: dict[str, str] = {
    step_key: reg_engine.label_for(step_key)
    for step_key in reg_engine.STEP_TO_COLUMN
    if step_key != reg_engine.FULL_NAME_STEP
    and reg_engine.label_key_for(step_key) in REG_LABELS
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
    """Почти дословное поведение `miniapp/routers/profile.py::_value` — с ОДНИМ расхождением
    (приёмка 19.09, review-260919 «Модерация» находка №1): `"-"` — сентинел `reg_engine`'а
    «вопрос анкеты пропущен/выключен» (`reg_engine.py`, предикат `value not in (None, "", "-")`,
    используется там же ~десяток раз), а не настоящий ответ делегата. Без фильтра карточка
    печатала «Лок. комитет: -»/«Позиция: -»/… для КАЖДОГО выключенного вопроса, чей столбец
    получил сентинел при финализации анкеты — 5 таких строк на всех 34 заявках в очереди прода.
    `miniapp/routers/profile.py::_value` (собственный профиль делегата) этот фильтр НЕ несёт —
    расхождение осознанное и локальное к этому файлу, не второй случайно разошедшийся источник."""
    raw = user.get(column)
    if column in _BOOL_COLUMNS:
        yes, no = _BOOL_COLUMNS[column]
        return yes if raw else no
    if raw in (None, "", "-"):
        return None
    text = str(raw).strip()
    return text or None


def answer_value(user: dict, step_key: str) -> str | None:
    """Ответ на один вопрос анкеты для карточки, или `None` — пусто/нет значения."""
    columns = _EXTRA_ANSWER_COLUMNS_BY_STEP.get(step_key, (reg_engine.STEP_TO_COLUMN[step_key],))
    parts = [v for v in (_column_value(user, c) for c in columns) if v]
    return " / ".join(parts) if parts else None


# Приёмка 19.09 (review-260919 «Модерация» находка №1): `age`/`birth_date` — один и тот же
# вопрос анкеты, переключённый 16-17.09 (`reg_q_age` off, `reg_q_birth_date` on) — у заявок ДО
# переключения заполнен `age`, у заявок ПОСЛЕ — `birth_date`. Что бы менеджер ни выбрал в
# «Полях карточки заявки», печатаем то значение, которое реально есть у ЭТОГО делегата.
_AGE_BIRTH_STEPS = ("age", "birth_date")


def _msk_age_from_birth_date(raw: str) -> int | None:
    """Возраст на сегодня по МСК (`timeutil.msk_now`) из строки `ДД.ММ.ГГГГ` — формат, в
    котором `reg_engine.validate_answer` хранит `birth_date`. `None` при нераспознанном
    формате или дате в будущем — вызывающий тогда просто не печатает строку, а не падает."""
    try:
        born = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        return None
    today = msk_now()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return years if years >= 0 else None


def _age_birth_date_line(user: dict, step_key: str) -> tuple[str, str] | None:
    """`(label, value)` для ОДНОГО шага пары `age`/`birth_date` — своё сырое значение
    побеждает; если его нет, значение другого шага (для `birth_date` — сырой возраст, как
    делегат его ввёл; для `age` — возраст, вычисленный из даты рождения). `None` — нет данных
    ни там, ни там. Дедупликация при включённых ОБОИХ шагах разом — забота `card_answers`."""
    own = _column_value(user, step_key)
    if own:
        return CARD_STEPS[step_key], own
    if step_key == "age":
        birth_raw = _column_value(user, "birth_date")
        if birth_raw:
            computed = _msk_age_from_birth_date(birth_raw)
            if computed is not None:
                return CARD_STEPS["age"], str(computed)
        return None
    age_raw = _column_value(user, "age")
    return (CARD_STEPS["age"], age_raw) if age_raw else None


def card_answers(user: dict, steps: list[str], limit: int | None) -> list[tuple[str, str]]:
    """`[(label, value)]` для непустых ответов на выбранные вопросы, в порядке `steps`;
    значение длиннее `limit` обрезается до `limit` символов + «…». `limit=None` — без
    обрезки (экран «📄 Полная анкета», appr_full: ответ печатается целиком).

    `age`/`birth_date` — если оба шага включены разом И у обоих есть СВОЁ значение, обе строки
    печатаются (разные данные, обе информативны); если у одного из них значение только
    ВЫЧИСЛЕНО/позаимствовано у другого (`_age_birth_date_line`), вторая строка с ТЕМ ЖЕ
    содержимым не повторяется (приёмка 19.09)."""
    out: list[tuple[str, str]] = []
    age_birth_seen: tuple[str, str] | None = None
    for step_key in steps:
        if step_key in _AGE_BIRTH_STEPS:
            resolved = _age_birth_date_line(user, step_key)
            if resolved is None or resolved == age_birth_seen:
                continue
            age_birth_seen = resolved
            label, value = resolved
        else:
            value = answer_value(user, step_key)
            if not value:
                continue
            label = CARD_STEPS.get(step_key, step_key)
        if limit is not None and len(value) > limit:
            value = value[:limit] + "…"
        out.append((label, value))
    return out


# Приёмка 19.09 (review-260919 «Модерация» находки №2/№3): развилка резюме (`reg_resume_mode
# = fork`), ветка «мини-профиль» — три текстовых подшага, свои колонки `users`. `mini_portfolio`
# хранит JSON-список блоков (или легаси голый текст) — тот же формат, что лист/сводка анкеты,
# читается ТОЙ ЖЕ парой `reg_engine.parse_repeatable`/`repeatable_display`, второй точки
# форматирования не заводим (докстринг `reg_engine.repeatable_display`).
_MINI_RESUME_COLUMNS: tuple[str, ...] = ("mini_projects", "mini_portfolio", "mini_direction")


def mini_resume_fields(user: dict) -> list[tuple[str, str]]:
    """`[(подпись, значение)]` непустых подполей мини-профиля, в порядке анкеты (совпадает с
    `CARD_STEPS`, т.к. `_MINI_RESUME_COLUMNS` — те же три шага REG_FLOW в том же порядке)."""
    out: list[tuple[str, str]] = []
    for column in _MINI_RESUME_COLUMNS:
        if column == "mini_portfolio":
            value = reg_engine.repeatable_display(reg_engine.parse_repeatable(user.get(column)))
        else:
            value = _column_value(user, column)
        if value:
            out.append((CARD_STEPS[column], value))
    return out


def resume_summary(user: dict) -> dict:
    """Единая точка разбора «что делегат реально дал в резюме» — использует и карточка бота
    (`handlers/admin_moderation.py`), и карточка веба (`services/applications.py::card_payload`),
    чтобы поверхности не расходились (приёмка 19.09). Приоритет — файл → ссылка → текст →
    мини-профиль → нет (тот же порядок, что уже был у файла/ссылки/текста до этой приёмки).

    `{"kind": "file"|"link"|"text"|"mini"|"none", "warning": bool, "mini_fields": [...]}`.
    `warning=True` — ТОЛЬКО при `kind == "none"` и непустом `resume_type`: делегат прошёл
    развилку и что-то выбрал, но ни один из четырёх карманов (`resume_file_id`/`resume_link`/
    `resume_text`/`mini_*`) не заполнен — сигнал потери данных (другой баг), а не «резюме не
    приложено» (делегат вообще не дошёл до вопроса/пропустил)."""
    if user.get("resume_file_id"):
        return {"kind": "file", "warning": False, "mini_fields": []}
    if user.get("resume_link"):
        return {"kind": "link", "warning": False, "mini_fields": []}
    if user.get("resume_text"):
        return {"kind": "text", "warning": False, "mini_fields": []}
    mini_fields = mini_resume_fields(user)
    if mini_fields:
        return {"kind": "mini", "warning": False, "mini_fields": mini_fields}
    resume_type = str(user.get("resume_type") or "").strip()
    return {"kind": "none", "warning": bool(resume_type), "mini_fields": []}


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
    равна исходнику, ни одна строка не режется посередине.

    ИСКЛЮЧЕНИЕ: строка длиннее `limit` сама по себе (полный ответ анкеты + подпись + HTML-
    экранирование могут дать одну строку за 4096 символов — `sendMessage` тогда падает) —
    такая строка режется жёстко на куски по `limit` символов, каждый кусок — свой элемент
    списка. Для НЕЁ инвариант «"\\n".join(chunks) == text» не держится (разделитель между
    жёсткими кусками одной строки не нужен — конкатенация БЕЗ разделителя восстанавливает
    исходную строку); для всех строк в пределах `limit` инвариант держится как раньше."""
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    used = 0

    def flush():
        nonlocal current, used
        if current:
            chunks.append("\n".join(current))
            current = []
            used = 0

    for line in lines:
        if len(line) > limit:
            flush()
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        addition = len(line) + (1 if current else 0)
        if current and used + addition > limit:
            flush()
            addition = len(line)
        current.append(line)
        used += addition
    flush()
    return chunks
