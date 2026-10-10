"""Сборка текста карточки заявки — общая для очереди «📋 Заявки» и экрана кандидатов в
амбассадоры («🧾 Анкета»).

Раньше сборка жила внутри `admin_moderation._show_current_card`: бейджи правки/переподачи,
прежний отказ, пометки правил, «сменил ответ», поля по выбору менеджера (реестр
`modcard_fields`), лимит длины ответа, балл, согласие и подгонка под лимит Telegram. Экрану
кандидатов нужна та же карточка — не второй рендер, поэтому сборка вынесена сюда.

Роутера нет. Помощники берутся из `handlers.applications.admin_moderation` лениво и через атрибут модуля:
тот импортирует этот модуль, а тесты подменяют его атрибуты.
"""
from __future__ import annotations

import html
from typing import NamedTuple


class CardText(NamedTuple):
    """`text` — HTML карточки; `overflow` — не влезла в лимит Telegram (кнопка «📄 Полная
    анкета»); `has_history` — у заявки есть история правок (кнопка «🕓 История»)."""
    text: str
    overflow: bool
    has_history: bool


async def build_card_text(user: dict, *, position: int | None = None, total: int | None = None,
                          city_label_text: str | None = None) -> CardText:
    """Карточка заявки `user`. Без `position`/`total` — шапка без «N/M»."""
    from handlers.applications import admin_moderation as am

    # Одна выборка истории обслуживает и пометку карточки, и видимость кнопки «🕓 История».
    edited_line, resubmit_line, has_history = await am._edit_badges_for(user)
    # «🚫 Ранее отклонена: <причина>» — уже экранирована (escape_reason=True).
    prev_reject = await am._prev_reject_line(user, escape_reason=True)
    # Бейджи правил и «сменил ответ после автоотказа» экранирует вызывающий.
    rule_lines = [html.escape(line) for line in await am._rule_badge_lines(user)]
    cleared_line = await am._auto_reject_cleared_line(user)
    if cleared_line:
        cleared_line = html.escape(cleared_line)
    # Набор вопросов и лимит длины ответа — реестром («🧾 Поля карточки заявки»); «resume» —
    # отдельный блок карточки, из fields исключается и управляет только show_resume.
    steps = am.moderation_card.enabled_steps(await am.get_setting_typed("modcard_fields"))
    answer_limit = await am.get_setting_typed("modcard_answer_limit")
    fields = am.moderation_card.card_answers(user, [s for s in steps if s != "resume"], answer_limit)
    scoring_enabled = bool(await am.get_setting_typed("reg_scoring_enabled"))
    text, overflow = am.moderation_card.fit_card(
        am._render_application_card(
            user, position, total, city_label_text=city_label_text,
            consent_line=await am.consent_card_line(user["telegram_id"]),
            edited_line=edited_line, resubmit_line=resubmit_line,
            prev_reject_line=prev_reject,
            rule_lines=rule_lines, cleared_line=cleared_line,
            fields=fields, show_resume=("resume" in steps),
            scoring_enabled=scoring_enabled,
        )
    )
    return CardText(text, overflow, has_history)
