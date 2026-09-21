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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: CRUD, права по городу, валидация условий, очередь перевода
# ══════════════════════════════════════════════════════════════════════════════════════════

async def can_edit_city(admin_id: int, city: str | None) -> bool:
    """ПРАВО менеджера редактировать правило данного города (D-16) — не фильтр отображения.
    Вызывается в КАЖДОМ мутирующем вызове сервиса (`save_rule`/`delete_rule`), а не только при
    отрисовке списка — инлайн-клавиатуры не истекают, тот же довод, что у CITY-05/09.3.

    Правило конкретного города (`city` не `None`) доступно, если код города входит в
    `settings_ops.per_city_visible_codes(admin_id)` — суперадмину и менеджеру без привязки
    видны все города, привязанному менеджеру — только его. Правило «все города» (`city is
    None`) доступно ТОЛЬКО тому, у кого нет привязки к городу (суперадмин или менеджер без
    города, `database.db.get_staff_city` вернул `None`) — привязанный менеджер не имеет права
    завести или включить общее правило."""
    if city is None:
        if admin_id in config.ADMIN_IDS:
            return True
        bound = await get_staff_city(admin_id)
        return bound is None
    visible = await per_city_visible_codes(admin_id)
    return normalize_city(city) in visible


async def rules_for_admin(admin_id: int) -> list[dict]:
    """Список для экрана менеджера — `list_reject_rules` со скоупом по видимым городам плюс
    разбор JSON-колонок `tracks`/`conditions`. Суперадмин и менеджер без привязки видят все
    правила (тот же скоуп, что `can_edit_city` разрешает редактировать)."""
    bound_city = None if admin_id in config.ADMIN_IDS else await get_staff_city(admin_id)
    rows = await list_reject_rules(city_scope=city_scope(bound_city))
    out = []
    for row in rows:
        rule = dict(row)
        try:
            rule["tracks"] = json.loads(row.get("tracks") or "[]") or []
        except (TypeError, ValueError):
            rule["tracks"] = []
        try:
            rule["conditions"] = json.loads(row.get("conditions") or "[]") or []
        except (TypeError, ValueError):
            rule["conditions"] = []
        out.append(rule)
    return out


async def validate_condition(
    step: str, op: str, values, *, event_city: str | None = None,
) -> tuple[dict | None, str | None]:
    """Нормализует одно условие или отдаёт человеческую причину отказа (без кодов вопросов/
    вариантов в тексте — CLAUDE.md «бот для людей»). `event_city` принят по контракту
    `<interfaces>` плана для единообразия с остальными функциями модуля; `reg_engine.options`
    сегодня не резолвит варианты по городу (список вариантов вопроса в проекте общий на все
    города), поэтому параметр здесь не влияет на результат — честно, не притворяется, что
    город учтён там, где его не читает ни один источник данных.

    - `step` обязан быть ключом `REG_FLOW`;
    - `op` обязан входить в `reg_engine.condition_operators(step)`;
    - для категорий select/multi каждое значение обязано присутствовать в ЖИВОМ
      `reg_engine.options(step)` — иначе отказ в тоне `settings_validation` («такого варианта
      нет»), без показа кода;
    - int-операторы приводят значения к `int` (`between` — ровно два, первое меньше второго);
    - `before`/`after` разбирают значение как `%d.%m.%Y`; `age_on_forum_lt` — положительное
      целое; `filled`/`empty`/`has_file`/`no_file` принудительно обнуляют `values`."""
    if step not in _REG_FLOW_STEPS:
        return None, "Такого вопроса анкеты не существует — обновите экран и попробуйте снова."

    allowed_ops = condition_operators(step)
    if op not in allowed_ops:
        return None, "Такое условие недоступно для этого вопроса — обновите экран и попробуйте снова."

    category = reject_condition_category(step)
    values = list(values or [])

    if category in ("select", "multi"):
        if not values:
            return None, "Отметьте хотя бы один вариант."
        live_options = await options(step)
        if any(value not in live_options for value in values):
            return None, (
                "Такого варианта больше нет — отметьте варианты галочками, обновите экран и "
                "попробуйте ещё раз."
            )
        return {"step": step, "op": op, "values": values}, None

    if category == "int":
        try:
            numbers = [int(v) for v in values]
        except (TypeError, ValueError):
            return None, "Нужно число."
        if op == "between":
            if len(numbers) != 2 or numbers[0] >= numbers[1]:
                return None, "Нужны два числа, первое меньше второго."
        elif len(numbers) != 1:
            return None, "Нужно одно число."
        return {"step": step, "op": op, "values": numbers}, None

    if category in ("date", "birth_date") and op in ("before", "after"):
        if len(values) != 1:
            return None, "Нужна одна дата в формате ДД.ММ.ГГГГ."
        raw_date = str(values[0]).strip()
        try:
            datetime.strptime(raw_date, "%d.%m.%Y")
        except (TypeError, ValueError):
            return None, "Не понял дату — пришлите в формате ДД.ММ.ГГГГ, например 15.10.2026."
        return {"step": step, "op": op, "values": [raw_date]}, None

    if category == "birth_date" and op == "age_on_forum_lt":
        try:
            years = int(values[0])
        except (TypeError, ValueError, IndexError):
            return None, "Нужно положительное число лет."
        if years <= 0:
            return None, "Нужно положительное число лет."
        return {"step": step, "op": op, "values": [years]}, None

    if category == "text" and op in ("filled", "empty"):
        return {"step": step, "op": op, "values": []}, None

    if category == "file" and op in ("has_file", "no_file"):
        return {"step": step, "op": op, "values": []}, None

    return None, "Неизвестное условие — обновите экран и попробуйте снова."


_VALID_ACTIONS = ("reject", "flag")  # D-03: закрытое множество, автоодобрения нет


async def save_rule(admin_id: int, rule_id: int | None, **fields) -> tuple[int | None, str | None]:
    """Единственная дверь записи правила. `(id, None)` при успехе, `(None, причина)` при
    нарушении прав или валидации — причина человеческая, без кодов.

    1. `can_edit_city` для НОВОГО города правила И (при правке) для СТАРОГО города строки из
       базы — менеджер не может «увести» чужое правило в свой город (T-31-04-01).
    2. Все условия прогоняются через `validate_condition`.
    3. `action` обязан быть `"reject"` или `"flag"` (D-03).
    4. При `action == "reject"` и включении правила обязателен непустой `reject_text` (D-21).
    5. Сериализация `tracks`/`conditions` в JSON, вызов `create_reject_rule`/`update_reject_rule`.
    6. Текст отказа ставится в очередь машинного перевода (D-25), сбой очереди не теряет
       правило."""
    new_city = fields.get("city")

    if rule_id is not None:
        existing = await get_reject_rule(rule_id)
        if existing is None:
            return None, "Правило не найдено — возможно, его уже удалили."
        if not await can_edit_city(admin_id, existing.get("city")):
            return None, "Нет прав редактировать правило этого города."

    if not await can_edit_city(admin_id, new_city):
        return None, "Нет прав сохранить правило в этот город."

    action = fields.get("action")
    if action not in _VALID_ACTIONS:
        return None, "Неизвестное действие правила."

    raw_groups = fields.get("conditions") or []
    normalized_groups: list[list[dict]] = []
    for group in raw_groups:
        normalized_group: list[dict] = []
        for cond in group or []:
            normalized, error = await validate_condition(
                (cond or {}).get("step"), (cond or {}).get("op"), (cond or {}).get("values"),
                event_city=new_city,
            )
            if error:
                return None, error
            normalized_group.append(normalized)
        normalized_groups.append(normalized_group)

    enabled = 1 if fields.get("enabled") else 0
    reject_text = str(fields.get("reject_text") or "").strip() or None
    if action == "reject" and enabled and not reject_text:
        return None, "Без текста отказа делегат не поймёт причину — впишите текст."

    tracks = fields.get("tracks") or ["full"]
    tracks_json = json.dumps(list(tracks), ensure_ascii=False)
    conditions_json = json.dumps(normalized_groups, ensure_ascii=False)
    name = fields.get("name")

    if rule_id is None:
        new_id = await create_reject_rule(
            name=name, city=new_city, tracks=tracks_json, conditions=conditions_json,
            action=action, reject_text=reject_text, enabled=enabled, created_by=admin_id,
        )
        await _maybe_enqueue_rule_text_translation(reject_text, new_id)
        return new_id, None

    await update_reject_rule(
        rule_id,
        name=name, city=new_city, tracks=tracks_json, conditions=conditions_json,
        action=action, reject_text=reject_text, enabled=enabled,
    )
    await _maybe_enqueue_rule_text_translation(reject_text, rule_id)
    return rule_id, None


async def delete_rule(admin_id: int, rule_id: int) -> tuple[bool, str | None]:
    """Та же проверка права (T-31-04-01), затем `delete_reject_rule`."""
    existing = await get_reject_rule(rule_id)
    if existing is None:
        return False, "Правило не найдено — возможно, его уже удалили."
    if not await can_edit_city(admin_id, existing.get("city")):
        return False, "Нет прав удалить правило этого города."
    deleted = await delete_reject_rule(rule_id)
    return deleted, None if deleted else "Не удалось удалить правило."


async def _maybe_enqueue_rule_text_translation(text: str | None, rule_id: int | None) -> None:
    """Постановка текста отказа правила в очередь машинного перевода (D-25) — форма
    СКОПИРОВАНА с `database.db._maybe_enqueue_city_label_translation`, а не вызов существующего
    `database.db._maybe_enqueue_translation`: тот хук висит на `set_setting` и гейтится
    `services.i18n_sources.is_delegate_dynamic_key` (сверяет ключ реестра `bot_settings`), а
    текст правила живёт не в `bot_settings` — в колонке `reject_rules.reject_text`, второго
    ключа реестра под него не заводится. Тот же гейт (`delegate_lang_enabled`), тот же широкий
    fail-soft `except` с логом (T-31-04-05): сбой очереди перевода не должен потерять уже
    сохранённое правило — делегат получит русский текст, пока перевода нет (D-25)."""
    try:
        if not text:
            return
        if await get_setting_typed("delegate_lang_enabled") != "on":
            return
        await enqueue_translation(
            "en", src_hash(text), text, origin_key=f"reject_rule__{rule_id}",
        )
    except Exception as exc:  # noqa: BLE001 — намеренно широкий fail-soft (T-31-04-05)
        logger.error(
            "services.reject_rules._maybe_enqueue_rule_text_translation: очередь перевода не "
            "приняла текст правила id=%s (%s)",
            rule_id, exc,
        )


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: автоописание, заготовки и счётчик «попали бы N из M»
# ══════════════════════════════════════════════════════════════════════════════════════════

# Человеческие подписи операторов (D-10) — ни одного кодового значения в результате
# `rule_summary` (CLAUDE.md).
_OPERATOR_LABELS = {
    "in": "один из", "not_in": "ни один из",
    "lt": "меньше", "gt": "больше", "between": "между",
    "before": "раньше", "after": "позже",
    "age_on_forum_lt": "возраст на дату форума меньше",
    "filled": "заполнено", "empty": "не заполнено",
    "has_file": "есть", "no_file": "нет",
}

_ACTION_LABELS = {"reject": "отказ", "flag": "пометка"}

# Операторы, у которых значения условия не печатаются (сам оператор — уже полная мысль).
_NO_VALUE_OPERATORS = ("filled", "empty", "has_file", "no_file")


async def rule_summary(rule: dict) -> str:
    """D-10: автоописание ЧЕЛОВЕЧЕСКИМИ словами — «Москва · Курс — один из: 1, 2 → отказ».
    Город (`cities.city_label`, «Все города» при `None`) · условия (`reg_engine.label_for(step)`
    + человеческая подпись оператора; группы разделяются «ИЛИ», условия внутри группы — « и »)
    · стрелка и действие. Ни одного кодового значения — своё имя правила (если менеджер его
    задал) печатается вызывающим экраном ОТДЕЛЬНО, в шапке, эта функция отдаёт только
    автоописание."""
    rule_city = rule.get("city")
    city_text = "Все города" if not rule_city else await city_label(rule_city)

    group_texts = []
    for group in rule.get("conditions") or []:
        cond_texts = []
        for cond in group or []:
            cond = cond or {}
            step = cond.get("step")
            op = cond.get("op")
            values = cond.get("values") or []
            step_label = label_for(step) if step else "?"
            op_label = _OPERATOR_LABELS.get(op, op or "?")
            if op in _NO_VALUE_OPERATORS:
                cond_texts.append(f"{step_label} — {op_label}")
            elif op == "between" and len(values) == 2:
                cond_texts.append(f"{step_label} — {op_label} {values[0]} и {values[1]}")
            else:
                values_text = ", ".join(str(v) for v in values)
                cond_texts.append(f"{step_label} — {op_label}: {values_text}")
        if cond_texts:
            group_texts.append(" и ".join(cond_texts))

    conditions_text = " ИЛИ ".join(group_texts)
    action_label = _ACTION_LABELS.get(rule.get("action"), rule.get("action") or "?")

    parts = [city_text]
    if conditions_text:
        parts.append(conditions_text)
    return f"{' · '.join(parts)} → {action_label}"


async def RULE_PRESETS() -> list[dict]:  # noqa: N802 — имя дословно из <interfaces> плана 31-04
    """D-11: три заготовки правила в один тап — менеджер открывает, правит значения, включает.
    Оформлена АСИНХРОННОЙ функцией (не модульным литералом, хотя имя в интерфейсе плана
    записано без `async def`): заготовка «Курс из списка» обязана брать первые два варианта
    ЖИВОГО `reg_engine.options("course")`, а не литеральную копию статического списка вариантов
    курса из `reg_options.py` — иначе заготовка разъедется с анкетой, которую менеджер уже
    отредактировал в реестре (прямой анти-паттерн, названный в 31-RESEARCH). Статический
    модульный список не может прочитать «текущий» реестр на КАЖДЫЙ вызов — значит, единственный
    вариант, честно удовлетворяющий и D-11, и собственному acceptance-тесту плана («заготовка
    берёт варианты из отредактированного менеджером списка»), — асинхронная функция, вызывающая
    сторона зовёт её через `await RULE_PRESETS()`."""
    course_options = await options("course")
    course_values = course_options[:2]
    return [
        {
            "name": "Младше 18 на дату форума",
            "action": "reject",
            "conditions": [[{"step": "birth_date", "op": "age_on_forum_lt", "values": [18]}]],
            "reject_text": (
                "На дату форума тебе ещё не исполнится 18 лет — по правилам участия это "
                "обязательное условие."
            ),
        },
        {
            "name": "Курс из списка",
            "action": "reject",
            "conditions": [[{"step": "course", "op": "in", "values": course_values}]],
            "reject_text": "Мест на выбранный курс уже нет — набор на него закрыт.",
        },
        {
            "name": "Нет резюме",
            "action": "reject",
            "conditions": [[{"step": "resume", "op": "no_file", "values": []}]],
            "reject_text": (
                "Без резюме заявку пока не можем рассмотреть — пришли его, пожалуйста, при "
                "повторной подаче."
            ),
        },
    ]


async def dry_run_count(rule: dict) -> tuple[int, int]:
    """D-13: `(подпали_бы, всего)` — заявки ВСЕХ статусов в области правила (город + трек), а
    не только ожидающие: менеджеру нужен честный ответ «сколько таких людей вообще», не
    «сколько сейчас в очереди» (кнопки «применить к очереди» нет и не будет, D-19). Структурно
    read-only (T-31-04-03): единственное обращение к базе — `get_all_users_dicts` (SELECT),
    дальше чистый `reg_engine.evaluate_reject_rules` над КАЖДОЙ строкой с временно снятыми
    `enabled`/`paused_reason` (чтобы посчитать ещё не включённое правило) — ни одного
    `UPDATE`/`INSERT`, ни одной строки журнала."""
    rule_city = rule.get("city")
    tracks = rule.get("tracks") or ["full"]
    probe_rule = {**rule, "enabled": 1, "paused_reason": None}

    all_users = await get_all_users_dicts()
    forum_dates: dict = {}
    matched = 0
    total = 0
    for row in all_users:
        user_city = row.get("event_city")
        if rule_city is not None and normalize_city(rule_city) != normalize_city(user_city):
            continue
        track = row.get("participant_type") or "full"
        if track not in tracks:
            continue
        total += 1

        if user_city not in forum_dates:
            forum_dates[user_city] = await forum_date_for(user_city)

        result = evaluate_reject_rules(
            row, [probe_rule],
            birth_date=row.get("birth_date"), forum_date=forum_dates[user_city],
        )
        if result["reject_rule_ids"] or result["flag_rule_ids"]:
            matched += 1

    return matched, total
