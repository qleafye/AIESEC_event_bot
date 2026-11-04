"""Редактор правил автоотказа — одна логика для чата и Mini App.

Без aiogram: веб-процесс импортирует этот модуль, а `services.applications.reject_rules_notify`
тащит планировщик. Запись правил — только `save_rule`/`delete_rule`. Рубильник этот модуль
не переключает: он лишь считает, сколько правил выключится, и решает, кому можно.

Окно между проверкой слепка и записью — миллисекунды. Два сохранения в один миг могут оба
пройти (принятый остаток гонки). Слепок не включает `updated_at` и `paused_reason`: пересчёт
паузы не делает чужое сохранение «устаревшим».

Зависимости: `database.db`, `domain.cities`, `domain.regform.engine`, `domain.settings`,
`services.applications.reject_rules`, стандартная библиотека.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from database.db import count_auto_reject_log_for_rule, count_reject_rules, get_reject_rule
from domain.cities import city_label
from domain.regform.engine import (
    condition_operators,
    label_for,
    options,
    reject_condition_category,
    rule_pause_reason,
)
from domain.settings.ops import per_city_visible_codes
from domain.settings.schema import get_setting_typed
from services.applications.reject_rules import (
    OPERATOR_LABELS,
    RULE_PRESETS,
    can_edit_city,
    delete_rule,
    dry_run_count,
    enabled_steps,
    forum_date_for,
    rule_summary,
    save_rule,
)

logger = logging.getLogger(__name__)

TRACKS = (("full", "Полная"), ("short", "Краткая"), ("party", "Вечеринка"))
_TRACK_CODES = {
    "full": ("full",),
    "short": ("short",),
    "party": ("party_overnight", "party_noovernight"),
}
_ACTION_LABELS = {"reject": "Отклонять", "flag": "Только пометить"}
_STEP_GROUPS = {
    "age": "Про делегата", "birth_date": "Про делегата", "phone": "Про делегата",
    "alumni_status": "Про делегата", "vk": "Про делегата", "city": "Про делегата",
    "email": "Про делегата",
    "education_status": "Образование", "course": "Образование", "university": "Образование",
    "study_field": "Образование", "specialty": "Образование",
    "stack": "Опыт", "experience": "Опыт", "readiness": "Опыт", "goal": "Опыт",
    "work_status": "Опыт", "work_sphere": "Опыт", "missing_skills": "Опыт",
    "resume": "Опыт", "resume_link": "Опыт", "mini_projects": "Опыт",
    "mini_portfolio": "Опыт", "mini_direction": "Опыт", "case_optin": "Опыт",
}
_OTHER_CITY = (
    "Это правило относится к другому городу. Править его может руководитель без привязки к городу."
)
_EMPTY_PREVIEW = "Добавьте условие — тогда посчитаем."


@dataclass
class EditorResult:
    view: dict | None = None
    error: str | None = None
    stale: bool = False
    fresh: dict | None = None
    needs_confirm: dict | None = None
    not_found: bool = False
    forbidden: bool = False


def conditions_fp(conditions) -> str:
    """8 hex — тот же отпечаток, что у кнопок «удалить условие» в чате."""
    raw = json.dumps(conditions or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def rule_rev(rule: dict) -> str:
    """12 hex по содержанию правила. Пауза и время правки в слепок не входят."""
    payload = [
        rule.get("name"),
        rule.get("city"),
        list(rule.get("tracks") or ["full"]),
        conditions_fp(rule.get("conditions")),
        rule.get("action"),
        rule.get("reject_text"),
        int(bool(rule.get("enabled"))),
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _parse_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _as_track_list(tracks) -> list[str]:
    if tracks is None or tracks == "":
        return ["full"]
    if isinstance(tracks, str):
        parts = [part.strip() for part in tracks.split(",") if part.strip()]
        return parts or ["full"]
    return list(tracks) or ["full"]


def _expand_tracks(tracks) -> list[str]:
    """«Вечеринка» в каталоге — один чип, в базе — оба внутренних кода, как в чате."""
    out: list[str] = []
    for code in _as_track_list(tracks):
        for internal in _TRACK_CODES.get(code, (code,)):
            if internal not in out:
                out.append(internal)
    return out or ["full"]


def _ui_tracks(stored) -> list[str]:
    stored = list(stored or ["full"])
    out = []
    for ui_code, _label in TRACKS:
        internals = _TRACK_CODES[ui_code]
        if ui_code in stored or any(code in stored for code in internals):
            out.append(ui_code)
    return out or ["full"]


def _preset_steps(preset: dict) -> list[str]:
    steps = []
    for group in preset.get("conditions") or []:
        for cond in group or []:
            step = (cond or {}).get("step")
            if step and step not in steps:
                steps.append(step)
    return steps


def _has_age_condition(rule: dict) -> bool:
    for group in rule.get("conditions") or []:
        for cond in group or []:
            if (cond or {}).get("op") == "age_on_forum_lt":
                return True
    return False


def _unit_for(step: str, category: str) -> str | None:
    if step in ("age", "birth_date") or category == "birth_date":
        return "лет"
    return None


def _hint_for(category: str) -> str | None:
    if category == "text":
        return "Текст ответа не сравнивается — проверяем только, заполнено ли поле"
    if category == "int":
        return "Целое число, например 18"
    if category == "date":
        return "Например: 31.12.2026"
    return None


async def _city_phrase(city: str | None) -> str:
    if not city:
        return "всех городов"
    return f"города «{await city_label(city)}»"


async def load_rule(rule_id: int) -> dict | None:
    row = await get_reject_rule(rule_id)
    if row is None:
        return None
    rule = dict(row)
    rule["tracks"] = _parse_list(row.get("tracks")) or ["full"]
    rule["conditions"] = _parse_list(row.get("conditions"))
    return rule


async def _paused_text(rule: dict) -> str | None:
    reason = rule.get("paused_reason")
    if not reason and rule.get("enabled"):
        live = await enabled_steps(rule)
        used = {
            (cond or {}).get("step")
            for group in rule.get("conditions") or []
            for cond in group or []
            if (cond or {}).get("step")
        }
        options_by_step = {step: await options(step) for step in used}
        reason = rule_pause_reason(rule, live, options_by_step)
    if not reason:
        return None
    return (
        f"Опиралось на вопрос «{reason}», а он выключен или его вариант удалён из анкеты. "
        "Поправьте условие или уберите его."
    )


async def rule_view(admin_id: int, rule: dict) -> dict:
    reason_text = await _paused_text(rule)
    enabled = int(bool(rule.get("enabled")))
    if not enabled:
        status = "off"
    elif reason_text:
        status = "paused"
    else:
        status = "on"
    city = rule.get("city")
    rule_id = rule.get("id")
    rejected = 0
    if rule_id is not None:
        rejected = await count_auto_reject_log_for_rule(
            int(rule_id), city_scope=None, include_returned=False,
        )
    return {
        "id": rule_id,
        "name": rule.get("name"),
        "city": city,
        "city_label": "Все города" if not city else await city_label(city),
        "tracks": _ui_tracks(rule.get("tracks")),
        "conditions": rule.get("conditions") or [],
        "action": rule.get("action"),
        "action_label": _ACTION_LABELS.get(rule.get("action"), rule.get("action") or ""),
        "reject_text": rule.get("reject_text"),
        "enabled": enabled,
        "status": status,
        "paused_text": reason_text,
        "summary": await rule_summary(rule),
        "rejected_count": rejected,
        "can_edit": await can_edit_city(admin_id, city),
        "rev": rule_rev(rule),
    }


async def catalog(admin_id: int, city, tracks) -> dict:
    internal = _expand_tracks(tracks)
    live = await enabled_steps({"city": city, "tracks": internal})
    steps = []
    for step in live:
        category = reject_condition_category(step)
        ops = []
        for op in condition_operators(step):
            ops.append({"op": op, "label": OPERATOR_LABELS.get(op, op)})
        step_options = await options(step) if category in ("select", "multi") else []
        steps.append({
            "step": step,
            "label": label_for(step),
            "category": category,
            "operators": ops,
            "options": step_options,
            "unit": _unit_for(step, category),
            "hint": _hint_for(category),
            "group_label": _STEP_GROUPS.get(step, "Прочее"),
        })

    live_set = set(live)
    presets = []
    for index, preset in enumerate(await RULE_PRESETS()):
        missing = [step for step in _preset_steps(preset) if step not in live_set]
        available = not missing
        unavailable = None
        if missing:
            unavailable = f"Нужен вопрос «{label_for(missing[0])}» — он выключен"
        summary = await rule_summary({
            "city": city,
            "tracks": internal,
            "conditions": preset.get("conditions") or [],
            "action": preset.get("action"),
        })
        presets.append({
            "index": index,
            "name": preset.get("name"),
            "summary": summary,
            "available": available,
            "unavailable_text": unavailable,
        })

    cities = []
    for code in await per_city_visible_codes(admin_id):
        cities.append({"code": code, "label": await city_label(code)})
    if await can_edit_city(admin_id, None):
        cities.insert(0, {"code": None, "label": "Все города"})

    return {
        "steps": steps,
        "presets": presets,
        "cities": cities,
        "tracks": [{"code": code, "label": label} for code, label in TRACKS],
        "forum_date": {"date": await forum_date_for(city, inherit_common=True)},
    }


async def _check_then_write(admin_id: int, rule_id: int, base_rev: str | None):
    rule = await load_rule(rule_id)
    if rule is None:
        return None, EditorResult(
            not_found=True, error="Правило не найдено — возможно, его уже удалили.",
        )
    if not await can_edit_city(admin_id, rule.get("city")):
        return None, EditorResult(forbidden=True, error=_OTHER_CITY)
    if base_rev is not None and base_rev != rule_rev(rule):
        return None, EditorResult(stale=True, fresh=await rule_view(admin_id, rule))
    return rule, None


def _fields(rule_or_draft: dict, **overrides) -> dict:
    fields = {
        "name": rule_or_draft.get("name"),
        "city": rule_or_draft.get("city"),
        "tracks": _expand_tracks(rule_or_draft.get("tracks")),
        "conditions": rule_or_draft.get("conditions") or [],
        "action": rule_or_draft.get("action") or "reject",
        "reject_text": rule_or_draft.get("reject_text"),
        "enabled": rule_or_draft.get("enabled"),
    }
    fields.update(overrides)
    if "tracks" in overrides:
        fields["tracks"] = _expand_tracks(overrides["tracks"])
    return fields


async def save_patch(admin_id: int, rule: dict, **overrides):
    return await save_rule(admin_id, rule["id"], **_fields(rule, **overrides))


def _from_save(error: str | None, *, not_found: bool = False) -> EditorResult | None:
    if not error:
        return None
    missing = "не найдено" in error.lower()
    forbidden = error.startswith("Нет прав")
    return EditorResult(error=error, not_found=not_found or missing, forbidden=forbidden)


async def _view_of(admin_id: int, rule_id: int) -> EditorResult:
    rule = await load_rule(rule_id)
    if rule is None:
        return EditorResult(not_found=True, error="Правило не найдено — возможно, его уже удалили.")
    return EditorResult(view=await rule_view(admin_id, rule))


async def create_rule(admin_id: int, draft: dict) -> EditorResult:
    if not _as_track_list(draft.get("tracks")) or draft.get("tracks") == []:
        return EditorResult(error="Выберите хотя бы один тип заявки")
    if draft.get("tracks") is not None and not list(draft.get("tracks") or []):
        return EditorResult(error="Выберите хотя бы один тип заявки")
    new_id, error = await save_rule(admin_id, None, **_fields(draft, enabled=0))
    blocked = _from_save(error)
    if blocked:
        return blocked
    return await _view_of(admin_id, new_id)


async def save_draft(admin_id: int, rule_id: int | None, draft: dict, base_rev: str | None) -> EditorResult:
    if not draft.get("tracks"):
        return EditorResult(error="Выберите хотя бы один тип заявки")
    if rule_id is None:
        return await create_rule(admin_id, draft)
    rule, blocked = await _check_then_write(admin_id, rule_id, base_rev)
    if blocked:
        return blocked
    new_id, error = await save_rule(
        admin_id, rule_id, **_fields(draft, enabled=rule.get("enabled")),
    )
    blocked = _from_save(error)
    if blocked:
        return blocked
    return await _view_of(admin_id, new_id)


async def create_from_preset(admin_id: int, index: int, city) -> EditorResult:
    presets = await RULE_PRESETS()
    if not (0 <= index < len(presets)):
        return EditorResult(error="Такой заготовки нет — обновите экран.")
    info = await catalog(admin_id, city, ["full"])
    chosen = info["presets"][index]
    if not chosen["available"]:
        return EditorResult(error=chosen["unavailable_text"])
    preset = presets[index]
    if not await can_edit_city(admin_id, city):
        return EditorResult(forbidden=True, error="Нет прав создать правило в этом городе.")
    new_id, error = await save_rule(
        admin_id, None,
        name=preset.get("name"), city=city, tracks=["full"],
        conditions=preset.get("conditions") or [],
        action=preset.get("action") or "reject",
        reject_text=preset.get("reject_text"), enabled=0,
    )
    blocked = _from_save(error)
    if blocked:
        return blocked
    return await _view_of(admin_id, new_id)


async def _forum_missing(rule: dict) -> bool:
    if not _has_age_condition(rule):
        return False
    return not await forum_date_for(rule.get("city"), inherit_common=True)


async def _enable_confirm(rule: dict) -> dict:
    phrase = await _city_phrase(rule.get("city"))
    if await _forum_missing(rule):
        return {
            "text": (
                f"Посчитать не получилось: у {phrase} не указана дата форума, "
                "а в правиле есть условие по возрасту. Пока даты нет, это условие не сработает ни на ком."
            ),
            "matched": None,
            "total": None,
            "forum_date_missing": True,
            "too_wide": False,
        }
    matched, total = await dry_run_count(rule)
    too_wide = bool(total) and matched * 2 > total
    if matched == 0:
        text = "Сейчас под правило не попадает ни одна заявка. Правило будет ждать новых."
    else:
        text = (
            f"Под правило попали бы {matched} из {total} заявок. "
            "Уже поданные заявки не изменятся — правило сработает только на новые."
        )
        if rule.get("action") == "reject":
            text += " Подходящие новые заявки будут отклонены сразу, делегат получит ваш текст отказа."
    if too_wide:
        text += " Похоже, правило слишком широкое — проверьте условия."
    return {
        "text": text,
        "matched": matched,
        "total": total,
        "forum_date_missing": False,
        "too_wide": too_wide,
    }


async def set_enabled(
    admin_id: int, rule_id: int, enabled: bool, *, confirm: bool, base_rev: str | None,
) -> EditorResult:
    rule, blocked = await _check_then_write(admin_id, rule_id, base_rev)
    if blocked:
        return blocked
    if not enabled:
        _new_id, error = await save_patch(admin_id, rule, enabled=0)
        blocked = _from_save(error)
        if blocked:
            return blocked
        return await _view_of(admin_id, rule_id)
    if rule.get("enabled") and not confirm:
        return EditorResult(view=await rule_view(admin_id, rule))
    if not confirm:
        return EditorResult(needs_confirm=await _enable_confirm(rule))
    _new_id, error = await save_patch(admin_id, rule, enabled=1)
    blocked = _from_save(error)
    if blocked:
        return blocked
    return await _view_of(admin_id, rule_id)


async def preview(admin_id: int, draft: dict) -> dict:
    """Только числа и текст. Ни строк людей, ни их ответов."""
    if not await can_edit_city(admin_id, draft.get("city")):
        return {
            "matched": None, "total": None, "too_wide": False, "forum_date_missing": False,
            "text": _OTHER_CITY,
        }
    conditions = draft.get("conditions") or []
    if not conditions or all(not group for group in conditions):
        return {
            "matched": None, "total": None, "too_wide": False, "forum_date_missing": False,
            "text": _EMPTY_PREVIEW,
        }
    rule = _fields(draft, enabled=1)
    phrase = await _city_phrase(rule.get("city"))
    if await _forum_missing(rule):
        return {
            "matched": None,
            "total": None,
            "too_wide": False,
            "forum_date_missing": True,
            "text": (
                f"Не посчитать: у {phrase} не указана дата форума, "
                "а в правиле есть условие по возрасту."
            ),
        }
    matched, total = await dry_run_count(rule)
    too_wide = bool(total) and matched * 2 > total
    text = f"Попали бы {matched} из {total} заявок."
    if too_wide:
        text += " Похоже, правило слишком широкое — проверьте условия."
    return {
        "matched": matched,
        "total": total,
        "too_wide": too_wide,
        "forum_date_missing": False,
        "text": text,
    }


async def copy_to_city(admin_id: int, rule_id: int, target_city, base_rev: str | None = None) -> EditorResult:
    rule, blocked = await _check_then_write(admin_id, rule_id, base_rev)
    if blocked:
        return blocked
    if target_city == rule.get("city") or (
        target_city and rule.get("city") and str(target_city) == str(rule.get("city"))
    ):
        return EditorResult(error="Этот город уже у самого правила — выберите другой.")
    if not await can_edit_city(admin_id, target_city):
        return EditorResult(forbidden=True, error="Нет прав скопировать правило в этот город.")
    name = rule.get("name") or "Правило"
    new_id, error = await save_rule(
        admin_id, None,
        name=f"{name} (копия)", city=target_city,
        tracks=rule.get("tracks") or ["full"],
        conditions=rule.get("conditions") or [],
        action=rule.get("action"), reject_text=rule.get("reject_text"), enabled=0,
    )
    blocked = _from_save(error)
    if blocked:
        return blocked
    return await _view_of(admin_id, new_id)


async def delete(admin_id: int, rule_id: int, *, confirm: bool, base_rev: str | None = None) -> EditorResult:
    rule, blocked = await _check_then_write(admin_id, rule_id, base_rev)
    if blocked:
        return blocked
    if not confirm:
        rejected = await count_auto_reject_log_for_rule(
            rule_id, city_scope=None, include_returned=False,
        )
        name = rule.get("name") or await rule_summary(rule)
        parts = [
            f"Удалить правило «{name}»?",
            "Правило пропадёт совсем, вернуть его будет нельзя. "
            "Условия, текст отказа и настройки правила будут потеряны.",
        ]
        if rejected:
            parts.append(
                f"Оно уже отклонило {rejected} заявок — эти заявки останутся отклонёнными, "
                "но запись о том, каким правилом они были отклонены, потеряет название."
            )
        enabled = bool(rule.get("enabled"))
        if enabled:
            parts.append("Сейчас правило включено — после удаления оно сразу перестанет работать.")
        return EditorResult(needs_confirm={
            "text": " ".join(parts),
            "rejected_so_far": rejected,
            "enabled": enabled,
            "can_disable_instead": enabled,
        })
    removed, error = await delete_rule(admin_id, rule_id)
    if not removed:
        return _from_save(error or "Не удалось удалить правило.")
    return EditorResult()


async def master_state(admin_id: int) -> dict:
    can_toggle = await can_edit_city(admin_id, None)
    enabled_count = await count_reject_rules(enabled_only=True)
    master_on = bool(await get_setting_typed("reject_rules_enabled"))
    if enabled_count == 0:
        confirm = "Сейчас нет включённых правил, поэтому ничего не изменится."
    else:
        confirm = (
            f"Перестанут работать {enabled_count} включённых правил во всех городах. "
            "Новые заявки будут ждать модерации. Правила сохранятся."
        )
    if can_toggle:
        status = "Сейчас правила работают." if master_on else "Сейчас правила выключены."
    else:
        status = "Автоотказ включает и выключает руководитель без привязки к городу."
    return {
        "enabled": master_on,
        "can_toggle": can_toggle,
        "enabled_count": enabled_count,
        "confirm_text": confirm,
        "status_text": status,
    }
