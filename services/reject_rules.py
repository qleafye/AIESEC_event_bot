"""Служебный слой правил автоотказа — единственная дверь между базой и всем остальным (Phase 31,
план 31-04, D-08). БЕЗ aiogram (сторож `tests/test_reject_rules_i18n.py`/
`tests/test_reject_rules_service.py::test_reject_rules_module_does_not_load_aiogram`, тот же
приём, что `services/applications.py`).

Разрез — ровно тот же, что у `services/applications.py` против `handlers/admin_moderation.py`,
и по той же причине: чистый оценщик (`reg_engine.evaluate_reject_rules`, план 31-01) ничего не
знает про базу и реестр; экраны редактора (планы 31-08/31-10 — чат, будущий Mini App —
`.planning/backlog.md`) — это кнопки и текст, им нельзя нести бизнес-правила. Между ними обязан
стоять aiogram-free сервис: веб-процесс Mini App (редактор правил появится следующей фазой, D-08)
не имеет права импортировать aiogram, а второго формата правил/второй копии логики заводить
нельзя (та же формула, что закрыла `settings_ops.py`/`services/applications.py` для своих
экранов).

Отвечает за:
- `active_rules`/`forum_date_for` — готовый к передаче в `reg_engine.evaluate_reject_rules`
  набор правил для финала анкеты (план 31-06), с пересчётом паузы (D-14) на каждой загрузке.
- CRUD (`save_rule`/`delete_rule`/`rules_for_admin`) с правом по городу (`can_edit_city`, D-16)
  и валидацией условий против живых вариантов вопроса (`validate_condition`).
- Автоописание (`rule_summary`, D-10), заготовки (`RULE_PRESETS`, D-11), счётчик dry-run
  (`dry_run_count`, D-13) и постановка текста отказа в очередь машинного перевода (D-25).

Зависимости — ТОЛЬКО `config`, `cities`, `database.db`, `reg_engine`, `services.i18n`,
`settings_ops`, `settings_schema` (плюс стандартная библиотека). Ни `aiogram`, ни `handlers.*`
на уровне модуля не импортируются.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from config import config
from cities import city_label, city_scope, normalize_city, get_setting_typed_for_city
from database.db import (
    create_reject_rule,
    delete_reject_rule,
    enqueue_translation,
    get_all_users_dicts,
    get_reject_rule,
    get_staff_city,
    list_reject_rules,
    update_reject_rule,
)
from reg_engine import (
    REG_FLOW,
    condition_operators,
    evaluate_reject_rules,
    is_step_enabled_for_track,
    label_for,
    options,
    reject_condition_category,
    rule_pause_reason,
)
from services.i18n import src_hash
from settings_ops import per_city_visible_codes
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

# Ключ шага REG_FLOW обязан быть в этом множестве — единственный источник правды «такой вопрос
# анкеты существует» для `validate_condition` (T-31-04-02).
_REG_FLOW_STEPS = {step_key for step_key, _setting_key, _t in REG_FLOW}


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: загрузчик активных правил и дата форума
# ══════════════════════════════════════════════════════════════════════════════════════════

async def forum_date_for(event_city: str | None) -> str | None:
    """Дата начала форума этого города строкой `%d.%m.%Y` — ровно тот формат, который ждёт
    `reg_engine.evaluate_reject_rules(forum_date=...)`. Пустое значение (настройка не задана,
    город без переопределения и без общего значения) -> `None` — тогда возрастные условия
    (`age_on_forum_lt`) просто не сработают (D-31: «нет данных — условие не выполнено»), а не
    уронят финал анкеты. Собственный try/except с логом: сбой чтения даты форума не имеет
    права уронить финал анкеты делегата."""
    try:
        value = await get_setting_typed_for_city("forum_date", event_city)
    except Exception as exc:  # noqa: BLE001 — намеренно широкий fail-soft (D-31)
        logger.error(
            "services.reject_rules.forum_date_for: сбой чтения даты форума города %r (%s)",
            event_city, exc,
        )
        return None
    if not value:
        return None
    return value.strftime("%d.%m.%Y")


async def active_rules(*, event_city: str | None = None, participant_type: str | None = None) -> list[dict]:
    """Готовый к передаче в `reg_engine.evaluate_reject_rules` список — единственная точка
    правды для финала анкеты (план 31-06). Пустой список, если общий рубильник
    `reject_rules_enabled` выключен (D-15) — гейт ПЕРВЫМ действием, без единого похода в базу
    (тот же приём, что `_score_patch` применяет к `reg_scoring_enabled`).

    Фильтрует по городу (правило с `city IS NULL` подходит всем) и по треку (пустой список
    `tracks` трактуется как `["full"]` — дефолт D-05), затем пересчитывает паузу правила
    (D-14) НА КАЖДОЙ ЗАГРУЗКЕ — тихо неработающего правила не бывает (Security Domain). Пауза
    считается по `reg_engine.rule_pause_reason` над множеством реально включённых для этого
    трека/города шагов (`reg_engine.is_step_enabled_for_track` по каждому `REG_FLOW`) и живыми
    вариантами ТОЛЬКО тех шагов, что реально встречаются в условиях оставшихся правил (не всей
    анкеты — лишних походов в реестр на кандидата быть не должно). Если пауза изменилась,
    строка правила в базе обновляется (`update_reject_rule`), а возвращаемая запись несёт
    `paused_changed` (`"on"`/`"off"`/`None`) — вызывающий решает, нужно ли уведомить держателей
    права «Настройки» о свежей паузе.

    Весь пересчёт паузы обёрнут в собственный try/except с логом: сбой пересчёта не должен
    отменять оценку правил — заявка важнее диагностики (T-31-04-04)."""
    # T-31-04-06 (найдено при тестировании): "reject_rules_enabled" — тип "toggle" (не "enum"
    # с текстовыми "on"/"off", как delegate_lang_enabled ниже) — `get_setting_typed` для этого
    # типа отдаёт готовый bool (`settings_schema._parse_setting`, ветка "toggle"), сравнение со
    # строкой "on" всегда ложно и держало бы рубильник фиктивно выключенным даже при "on" в
    # базе. Проверяем истинность напрямую, той же идиомой, что REG_DEFAULTS/reg_q_* тумблеры.
    if not await get_setting_typed("reject_rules_enabled"):
        return []

    raw_rows = await list_reject_rules(enabled_only=True)
    rules: list[dict] = []
    for row in raw_rows:
        try:
            tracks = json.loads(row.get("tracks") or "[]") or []
        except (TypeError, ValueError):
            tracks = []
        try:
            conditions = json.loads(row.get("conditions") or "[]") or []
        except (TypeError, ValueError) as exc:
            logger.error(
                "services.reject_rules.active_rules: правило id=%s пропущено — битые условия (%s)",
                row.get("id"), exc,
            )
            continue

        rule_city = row.get("city")
        if rule_city is not None and normalize_city(rule_city) != normalize_city(event_city):
            continue
        track_list = tracks or ["full"]
        if (participant_type or "full") not in track_list:
            continue

        rules.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "city": rule_city,
            "tracks": track_list,
            "conditions": conditions,
            "action": row.get("action"),
            "reject_text": row.get("reject_text"),
            "enabled": row.get("enabled"),
            "paused_reason": row.get("paused_reason"),
            "paused_changed": None,
        })

    try:
        enabled_step_set: set[str] = set()
        for step_key, setting_key, *_rest in REG_FLOW:
            if await is_step_enabled_for_track(setting_key, participant_type, event_city):
                enabled_step_set.add(step_key)

        steps_in_use: set[str] = set()
        for rule in rules:
            for group in rule["conditions"] or []:
                for cond in group or []:
                    step = (cond or {}).get("step")
                    if step:
                        steps_in_use.add(step)
        options_by_step = {step: await options(step) for step in steps_in_use}

        for rule in rules:
            old_reason = rule["paused_reason"]
            new_reason = rule_pause_reason(rule, enabled_step_set, options_by_step)
            if new_reason != old_reason:
                try:
                    await update_reject_rule(rule["id"], paused_reason=new_reason)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "services.reject_rules.active_rules: не удалось записать паузу правила id=%s (%s)",
                        rule["id"], exc,
                    )
                if new_reason and not old_reason:
                    rule["paused_changed"] = "on"
                elif old_reason and not new_reason:
                    rule["paused_changed"] = "off"
            rule["paused_reason"] = new_reason
    except Exception as exc:  # noqa: BLE001 — пересчёт паузы не имеет права уронить оценку
        logger.error(
            "services.reject_rules.active_rules: пересчёт паузы сорвался, правила отданы как есть (%s)",
            exc,
        )

    return rules
