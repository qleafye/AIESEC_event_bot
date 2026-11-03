"""CSV для партнёра: одобренные текущего сезона и фиксированный набор колонок.

Служебные поля в SELECT не входят. Соединение и фильтры сезона и города — те же,
что у списка участников. Aiogram этот модуль не импортирует.
"""
from __future__ import annotations

import database.db as db
from domain.regform.engine import parse_repeatable, repeatable_display

PARTNER_SQL_COLUMNS = (
    "full_name", "email", "phone", "city", "education_status", "course", "university",
    "study_field", "stack", "experience", "readiness", "formats", "resume_type",
    "resume_link", "resume_url", "mini_projects", "mini_portfolio", "mini_direction",
)

HEADERS = [
    "ФИО", "Email", "Телефон", "Город", "Образование", "Курс", "ВУЗ", "Направление",
    "Стек", "Опыт", "Готовность", "Форматы", "Тип резюме", "Ссылка на резюме",
    "Мини-профиль: проекты", "Мини-профиль: портфолио", "Мини-профиль: направление",
]

# Копия подписей листа. Сверка с reg_schema — в тесте.
RESUME_TYPE_LABELS = {"file": "Файл", "link": "Ссылка", "text": "Текстом", "mini": "Мини-профиль"}


def partner_select_prefix() -> str:
    return "SELECT " + ", ".join(PARTNER_SQL_COLUMNS)


def _text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _resume_href(link, url) -> str:
    text = _text(link).strip()
    if text:
        return text
    return _text(url).strip()


def _resume_type_label(raw) -> str:
    code = _text(raw).strip()
    if not code:
        return ""
    return RESUME_TYPE_LABELS.get(code, code)


def _portfolio(raw) -> str:
    return repeatable_display(parse_repeatable(raw))


def _row(record: tuple) -> tuple:
    (
        full_name, email, phone, city, education_status, course, university,
        study_field, stack, experience, readiness, formats, resume_type,
        resume_link, resume_url, mini_projects, mini_portfolio, mini_direction,
    ) = record
    cells = [
        _text(full_name), _text(email), _text(phone), _text(city),
        _text(education_status), _text(course), _text(university), _text(study_field),
        _text(stack), _text(experience), _text(readiness), _text(formats),
        _resume_type_label(resume_type), _resume_href(resume_link, resume_url),
        _text(mini_projects), _portfolio(mini_portfolio), _text(mini_direction),
    ]
    return tuple(db._csv_safe(cell) for cell in cells)


async def export_partner_csv(*, city_scope=None) -> tuple[list[str], list[tuple]]:
    """Заголовки и строки. Пустой набор — заголовки и пустой список."""
    season = (await db.get_setting("event_season") or "").strip() or None
    frag, params = db._approved_current_season_frag(season)
    city_frag, city_params = db._city_clause(city_scope)
    where = frag + (f" AND {city_frag}" if city_frag else "")
    sql = (
        partner_select_prefix()
        + " FROM users WHERE " + where
        + " ORDER BY full_name COLLATE NOCASE"
    )
    async with db._connect() as conn:
        async with conn.execute(sql, tuple(params + city_params)) as cursor:
            raw = await cursor.fetchall()
    return list(HEADERS), [_row(record) for record in raw]
