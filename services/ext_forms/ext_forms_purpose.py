"""Назначение внешней формы: «🏫 Делегации вузов», «📝 Регистрация», «📄 Только ответы».

Зачем: раньше любая подключённая форма с вопросом про вуз считалась формой делегаций, а
зарегистрироваться через стороннюю форму было нельзя. Теперь менеджер выбирает назначение
кнопками — при подключении и в карточке формы. Логика делегаций работает только у форм
«Делегации» (гейт — `services.delegations.delegations.delegation_form_id`), ответ формы
«Регистрация» становится заявкой участника (`services.ext_forms.ext_forms_registration`),
у «Только ответы» ответы просто сохраняются и копируются в таблицу.

Формы, подключённые до назначений (`purpose IS NULL`), получают его по прежнему правилу
«форма относится к делегациям» (`legacy_purpose`): выбрана в настройке `delegation_form_id`
или среди вопросов есть вопрос про вуз — «Делегации», иначе «Только ответы». При старте бот
один раз записывает это значение (`backfill_purposes`), так что поведение прода не меняется.

Кодовые значения назначений человеку не показываются — только подписи `PURPOSE_LABELS`.
"""
from __future__ import annotations

import html

from database import ext_forms_db as ef
from database.ext_forms_db import PURPOSES
from domain.settings.schema import get_setting_typed

PURPOSE_LABELS = {
    "delegations": "🏫 Делегации вузов",
    "registration": "📝 Регистрация",
    "answers": "📄 Только ответы",
}
# Пояснение трёх вариантов — шаг мастера подключения и экран «🎯 Назначение» карточки.
PURPOSE_HELP = (
    "🏫 <b>Делегации вузов</b> — бот оценивает ответы как делегации и одобряет делегатов "
    "(настраивается в «📋 Заявки → 🏫 Делегации»).\n"
    "📝 <b>Регистрация</b> — каждый ответ становится заявкой участника, анкету в боте "
    "заполнять не нужно.\n"
    "📄 <b>Только ответы</b> — ответы сохраняются и копируются в таблицу, бот с ними больше "
    "ничего не делает."
)
# Короткие коды в callback_data кнопок.
CODES = {"d": "delegations", "r": "registration", "a": "answers"}
CODE_OF = {v: k for k, v in CODES.items()}
# Метка воронки (reg_events.source_tag) у заявок из формы «Регистрация».
SOURCE_TAG = "ext_form"
_SOURCE_MAX = 60


async def raw_delegation_form_id() -> int | None:
    """Сырое значение настройки без гейта назначения — иначе legacy_purpose зациклился бы
    через delegations.delegation_form_id()."""
    try:
        raw = await get_setting_typed("delegation_form_id")
        if raw is None or str(raw).strip() == "":
            return None
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def legacy_purpose(form: dict) -> str:
    """Назначение формы, подключённой до назначений: правило фазы делегаций «одно место»."""
    from services.delegations.delegations import guess_delegation_questions  # лениво: цикл

    if await raw_delegation_form_id() == int(form["id"]):
        return "delegations"
    cols = await ef.list_columns(int(form["id"]))
    guess = guess_delegation_questions([(c["qkey"], c.get("label") or "") for c in cols])
    return "delegations" if guess.get("university") is not None else "answers"


async def purpose_of(form: dict) -> str:
    """Действующее назначение формы. Без записанного значения — legacy_purpose, в БД не пишется."""
    value = form.get("purpose")
    if value in PURPOSES:
        return value
    return await legacy_purpose(form)


async def backfill_purposes() -> int:
    """Один раз записать назначение старым формам. Повторный вызов ничего не меняет (0).
    purpose_since остаётся NULL — все ответы таких форм считаются «новыми»."""
    n = 0
    for form in await ef.list_forms_without_purpose():
        await ef.set_form_purpose(int(form["id"]), await legacy_purpose(form), None)
        n += 1
    return n


async def delegation_forms() -> list[dict]:
    """Подключённые формы с назначением «Делегации» (для выбора формы на экране делегаций)."""
    return [f for f in await ef.list_forms() if await purpose_of(f) == "delegations"]


def before_purpose(form: dict, answer: dict) -> bool:
    """Ответ пришёл до смены назначения — по новой логике его не трогаем."""
    since = form.get("purpose_since")
    if not since:
        return False
    return str(answer.get("received_at") or "") < str(since)


def source_label(form: dict) -> str:
    """Источник заявки из формы для «📈 Источники» и фильтра рассылки."""
    title = str(form.get("title") or "").strip() or "без названия"
    text = f"Внешняя форма «{title}»"
    if len(text) > _SOURCE_MAX:
        text = text[:_SOURCE_MAX - 2] + "…»"
    return text


async def change_effects(form: dict, new: str) -> list[str]:
    """Строки экрана «что изменится» при смене назначения формы на `new`."""
    old = await purpose_of(form)
    n = await ef.count_answers(int(form["id"]))
    lines = [f"Уже полученные ответы ({n}) не пересчитываются — новое назначение действует для "
             "ответов, которые придут после смены."]
    if old == "delegations" and new != "delegations":
        lines.append("🏫 Бот перестанет оценивать новые ответы как делегации; уже одобренные "
                     "делегаты остаются в боте.")
    if new != "delegations" and await raw_delegation_form_id() == int(form["id"]):
        lines.append("Делегации выключатся: форма перестанет быть формой делегаций в "
                     "«📋 Заявки → 🏫 Делегации».")
    if form.get("mirror_mode") == "yandex_export":
        tab = html.escape(str(form.get("mirror_tab") or ""))
        lines.append(f"Копия ответов во вкладку «{tab}» остановится — выберите новую вкладку "
                     "в карточке формы.")
    if new == "registration" and old != "registration":
        lines.append("📝 Каждый новый ответ станет заявкой участника — проверьте вопросы ФИО, "
                     "телефона и ника в «📝 Настройки регистрации».")
    if old == "registration" and new != "registration":
        lines.append("📝 Новые ответы перестанут становиться заявками; уже созданные заявки "
                     "остаются.")
    if new == "delegations" and old != "delegations":
        lines.append("🏫 Новые ответы бот сможет учитывать как делегации после настройки в "
                     "«📋 Заявки → 🏫 Делегации».")
    if new == "answers":
        lines.append("📄 Ответы будут только сохраняться и копироваться в таблицу.")
    return lines
