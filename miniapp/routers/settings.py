"""Phase 19 (19-07 Task 2, D-19): настройки-лайт из Mini App — закрытый белый список
тумблеров `on`/`off`. Экран — облегчённая версия боевого экрана настроек бота
(`handlers/admin_dashboard.py`): подпись из реестра, «✅/☐», тап переключает.

`EDITABLE_KEYS` вычисляется из `SETTINGS_SCHEMA` (не переписывается руками, иначе список
разъедется с реестром при добавлении новых ключей): все ключи `miniapp_section_*`,
`miniapp_staff_only`, `miniapp_enabled`, и ключи группы `game` с `type == "enum"` и
`options == ["on", "off"]` (сейчас в группе `game` таких нет — задел на будущее, список
просто пуст в этой части и подрастёт сам, когда такой ключ появится в реестре). Все
текстовые, числовые, ролевые ключи (`role_caps_*`), ключи оплаты (`payment_*`) и Sheets —
вне списка: их поверхность правки остаётся ботом («настройки только внутри бота» не
нарушается — Mini App тот же бот, T-19-43).

Обе мутации — `require_cap("settings")` + `require_section("settings")`; CSRF по
cookie-ветке уже закрыт в `miniapp.deps.principal` (T-19-04), здесь дублировать не нужно.

260824-8qw (MD-03): опасное направление тумблера («Mini App включён» -> off, «Только
менеджерам» -> on) требует второго тапа на фронте — текст последствий кладётся в поле
`confirm` элемента списка, читается из реестра. Обратное (безопасное) направление —
`confirm` = None, фронт переключает с одного тапа, как раньше. POST не трогается:
подтверждение — шаг интерфейса, серверный контракт (белый список + capability) не слабеет.

Phase 22 (22-04, WEB-SET-01/03/04): ниже старых ручек — полный реестр (`settings/all`),
шапка города (`settings/city`), атомарный пакет правок (`settings/batch`) и превью
(`settings/preview`). Старые `GET`/`POST /app/api/admin/settings` и `EDITABLE_KEYS` живут
как есть — открытые вебвью держат их в руках (22-CONTEXT § Reusable Assets). Всё
бот-знание — из aiogram-free `settings_ops`/`settings_validation`/`settings_schema`/`cities`;
`handlers.*` этот модуль не импортирует.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import reg_engine
import settings_ops
import web_theme
from cities import (
    ALL_CITIES,
    ALL_CITIES_LABEL,
    admin_selected_city,
    cities_module_on,
    city_codes,
    city_label,
    city_override_codes,
    get_setting_typed_for_city,
    per_city_key,
    set_admin_city,
)
from database.db import get_setting, set_setting
from services.sheets import tab_row_count
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from settings_synonyms import SETTINGS_SYNONYMS

from miniapp.deps import Principal, require_cap, require_section

logger = logging.getLogger(__name__)

router = APIRouter()

# Человеческая подпись группы реестра — только для тех групп, что реально бывают в
# EDITABLE_KEYS (T-19-45: код ключа человеку не показывается, группа — да).
GROUP_LABELS = {"miniapp": "Mini App", "game": "Геймификация"}

BAD_VALUE_TEXT = "Значение переключателя — «включено» или «выключено»."


def _is_editable(key: str, meta: dict) -> bool:
    if key == "miniapp_enabled" or key == "miniapp_staff_only" or key.startswith("miniapp_section_"):
        return True
    return (
        meta.get("group") == "game"
        and meta.get("type") == "enum"
        and meta.get("options") == ["on", "off"]
    )


# Вычислено один раз при импорте из текущего SETTINGS_SCHEMA — реестр не меняется в рантайме.
EDITABLE_KEYS = tuple(key for key, meta in SETTINGS_SCHEMA.items() if _is_editable(key, meta))

# 260824-8qw (MD-03): ключ тумблера + значение, В КОТОРОЕ он переключится -> ключ реестра с
# текстом подтверждения. Оба направления отнимают доступ у всего мероприятия разом:
# miniapp_enabled -> off прячет приложение у всех, включая нажавшего (обратно — только из
# бота, T-19-01: _enabled_gate отдаёт 503 на весь /app/api/*); miniapp_staff_only -> on
# отбирает доступ у делегатов (1000+ человек). Обратные направления (вернуть доступ) —
# безопасные, без подтверждения, ровно как «Вернуть из архива» у заданий.
DANGER_CONFIRM = {
    ("miniapp_enabled", "off"): "miniapp_confirm_disable_text",
    ("miniapp_staff_only", "on"): "miniapp_confirm_staff_only_text",
}


async def _items() -> list[dict]:
    items = []
    for key in EDITABLE_KEYS:
        meta = SETTINGS_SCHEMA[key]
        group = meta.get("group")
        value = await get_setting_typed(key)
        next_value = "off" if value == "on" else "on"
        confirm_key = DANGER_CONFIRM.get((key, next_value))
        items.append({
            "key": key,
            "label": meta["label"],
            "value": value,
            "group_label": GROUP_LABELS.get(group, str(group or "")),
            "confirm": await get_setting_typed(confirm_key) if confirm_key else None,
        })
    return items


@router.get("/app/api/admin/settings")
async def settings_list(
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> list[dict]:
    return await _items()


class SettingIn(BaseModel):
    key: str
    value: str


@router.post("/app/api/admin/settings")
async def settings_set(
    body: SettingIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> list[dict]:
    if body.key not in EDITABLE_KEYS:
        raise HTTPException(403, {"reason": "not_editable"})
    if body.value not in ("on", "off"):
        raise HTTPException(400, {"reason": "bad_value", "text": BAD_VALUE_TEXT})
    await set_setting(body.key, body.value)
    return await _items()


# ══ Quick 260903 (BACKLOG-0309-COUNTDOWN): подсказка менеджеру про незаданную дату ═══════
#
# Сегодня пустой `miniapp_hub_countdown_date` = молча отсутствующая строка отсчёта у делегата
# — менеджер об этом не узнаёт никак. Текст собирается ЗДЕСЬ (D-06: hub.js человеко-видимых
# литералов не заводит), ключ настройки менеджеру не показываем — только подпись из реестра.

COUNTDOWN_KEY = "miniapp_hub_countdown_date"


def _countdown_missing_text(cities: list[str] | None) -> str:
    label = SETTINGS_SCHEMA[COUNTDOWN_KEY]["label"]
    text = (
        f"Не задана «{label}» — делегаты не видят, сколько дней осталось. "
        "Укажите дату в формате ДД.ММ.ГГГГ."
    )
    if cities:
        text += f" Города без даты: {', '.join(cities)}."
    return text


async def _countdown_hint(telegram_id: int) -> dict | None:
    """`None` — дата задана (везде, где это сейчас проверяется); иначе — текст с переходом в
    настройки. Три ветки повторяют лестницу `_city_ctx`/`get_setting_typed_for_city` (D-04):
    модуль городов выключен -> одно глобальное значение; шапка на конкретном городе ->
    проверяется только он; шапка «все города» -> обходим все города, называем те, где нет
    даже эффективного (per-city ИЛИ общего) значения."""
    if not await cities_module_on():
        value = await get_setting_typed_for_city(COUNTDOWN_KEY, None)
        return None if value else {"text": _countdown_missing_text(None), "hash": "#/settings"}

    selected = await admin_selected_city(telegram_id)
    if selected not in (None, ALL_CITIES):
        value = await get_setting_typed_for_city(COUNTDOWN_KEY, selected)
        return None if value else {"text": _countdown_missing_text(None), "hash": "#/settings"}

    missing = []
    for code in city_codes():
        value = await get_setting_typed_for_city(COUNTDOWN_KEY, code)
        if not value:
            missing.append(await city_label(code))
    if not missing:
        return None
    return {"text": _countdown_missing_text(missing), "hash": "#/settings"}


@router.get("/app/api/admin/settings/hints")
async def settings_hints(
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    return {"countdown": await _countdown_hint(p.telegram_id)}


# ══ Phase 22 (22-04): весь правимый реестр + шапка города ═══════════════════════════════
#
# D-01: не белый список выше, а весь SETTINGS_SCHEMA минус группа roles — формула
# `settings_ops.editable_keys()`; подписи/подсказки/дефолты/опасность/синонимы — из реестра и
# карт settings_ops/settings_synonyms: роутер ничего не сочиняет и ничего не считает сам.


def _display(value) -> str:
    """Строка значения для экрана (поиск по значению, маркер, diff) — `value` при этом едет
    типизированным, как читает его бот."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


def _is_default(raw: str | None, meta: dict) -> bool:
    """«по умолчанию» = сырого значения нет либо оно равно дефолту реестра (D-03)."""
    if raw is None:
        return True
    default = meta.get("default")
    if default is None:
        return False
    if isinstance(default, list):
        items = [s.strip() for line in raw.splitlines() for s in line.split(";") if s.strip()]
        return items == list(default)
    return raw == str(default)


class _CityCtx:
    """Контекст города на один запрос: модуль, шапка (`admin_selected_city` — та же функция,
    что у бота, D-04) и право на правку (`per_city_visible_codes`, D-11)."""

    def __init__(self, on: bool, selected: str | None, visible: list[str]):
        self.on = on
        self.selected = selected
        self.visible = visible

    @property
    def sees_all(self) -> bool:
        return self.visible == city_codes()


async def _city_ctx(telegram_id: int) -> _CityCtx:
    if not await cities_module_on():
        return _CityCtx(False, None, city_codes())
    return _CityCtx(
        True,
        await admin_selected_city(telegram_id),
        await settings_ops.per_city_visible_codes(telegram_id),
    )


async def _city_header(ctx: _CityCtx) -> dict | None:
    if not ctx.on:
        return None
    selected = ctx.selected
    return {
        "selected": selected,
        "selected_label": ALL_CITIES_LABEL if selected == ALL_CITIES else await city_label(selected),
        "cities": [{"code": code, "label": await city_label(code)} for code in ctx.visible],
        "all_cities": ALL_CITIES,
        "all_cities_label": ALL_CITIES_LABEL,
        "can_select_all": ctx.sees_all,
    }


async def _confirm_text(base: str, value) -> str | None:
    """Текст подтверждения для направления «из текущего значения» — источник один
    (`settings_ops.dangerous_confirm_key`), plain-текст (D-06). Ключи вкладок Sheets — None:
    их подтверждение считается при записи по числу строк вкладки."""
    return await settings_ops.dangerous_confirm_text(base, settings_ops.next_value_from(base, value))


async def _item_for(base: str, ctx: _CityCtx) -> dict:
    """Один элемент ответа для базового ключа реестра с учётом шапки города: при выбранном
    городе per-city ключ едет композитным (`{base}__city__{code}`), при «все города» или
    выключенном модуле — глобальным со счётчиком переопределений (D-03/D-04)."""
    meta = SETTINGS_SCHEMA[base]
    per_city = bool(meta.get("per_city"))
    key = base
    is_city_override = False
    override_labels: list[str] = []
    editable = True

    composite = None
    if per_city and ctx.on and ctx.selected not in (None, ALL_CITIES):
        composite = per_city_key(base, ctx.selected)
    if composite:
        key = composite
        raw_override = await get_setting(composite)
        is_city_override = bool(raw_override)
        raw = raw_override if raw_override else await get_setting(base)
        value = await get_setting_typed_for_city(base, ctx.selected)
        editable = ctx.selected in ctx.visible
    else:
        raw = await get_setting(base)
        value = await get_setting_typed(base)
        if per_city and ctx.on:
            override_labels = [await city_label(code) for code in await city_override_codes(base)]
            editable = ctx.sees_all

    spec = settings_ops.item_spec(key, raw=raw, value=value, is_default=_is_default(raw, meta))
    spec.setdefault("max_len", None)
    spec.update({
        "display": _display(value),
        "is_city_override": is_city_override,
        "city_override_count": len(override_labels),
        "city_override_labels": override_labels,
        "confirm_text": await _confirm_text(base, value),
        "search_terms": list(SETTINGS_SYNONYMS.get(base, [])),
        "editable": editable,
        # Quick 260904-8o3 Task 3 (E5/E6): группа "miniapp" несёт ключи оформления ВПЕРЕМЕШКУ
        # с обычными текстами Mini App (row["theme_preview"] в _sections отмечает всю группу
        # целиком) — фронт различает, какие из group.items реально можно слать в
        # POST /admin/theme/preview, без второго хардкод-списка ключей в JS (T-8o3-02: чужой
        # ключ там 403, случайно попавшая в pending НЕ-тема правка не должна ронять весь запрос
        # превью и уводить экран в «нет доступа», см. api.js::authErrorHandler на любой 403).
        "theme_key": base in web_theme.THEME_KEYS.values(),
    })
    return spec


async def _reg_questions_matrix() -> dict:
    """Матрица «трек × вопрос» (D-17 Task 3, владелец 03.09): одна строка на вопрос анкеты
    (`REG_FLOW`, тот же порядок, что мастер и бот), три ячейки — полная форма/party/short.

    party — трёхзначный гейт бота (`reg_engine.is_step_enabled_for_track`): отсутствие
    `{key}__party` значит «наследует полную форму», явное значение — переопределение;
    `is_inherited` здесь = «ключ не задан явно» (муть на экране, реестр читает то же самое).
    short — двузначный (07-01/SHORT-03): отсутствие значит «не задаётся», НЕ наследование —
    `is_inherited` тоже True на отсутствии (тот же визуальный приём «не переопределено
    явно»), но эффективное значение — жёсткий "off", а не значение полной формы.

    Тумблер матрицы всегда шлёт явный "on"/"off" (T-22-… D-17): у веба нет кнопки «вернуть
    наследование», как у бота (`reg_q_ptoggle` цикл inherit->on->off->inherit) — наименьшее
    из решений, отдельный «сбросить трек» откладывается до появления запроса менеджера."""
    rows = []
    for step_key, setting_key, *_rest in reg_engine.REG_FLOW:
        full_value = await get_setting_typed(setting_key)
        party_raw = await get_setting(f"{setting_key}__party")
        short_raw = await get_setting(f"{setting_key}__short")
        rows.append({
            "step_key": step_key,
            "label": reg_engine.label_for(step_key),
            "full": {"key": setting_key, "value": full_value},
            "party": {
                "key": f"{setting_key}__party",
                "value": party_raw if party_raw is not None else full_value,
                "is_inherited": party_raw is None,
            },
            "short": {
                "key": f"{setting_key}__short",
                "value": "on" if short_raw == "on" else "off",
                "is_inherited": short_raw is None,
            },
        })
    return {"rows": rows}


async def _sections(ctx: _CityCtx) -> tuple[list[dict], int]:
    """Разделы в порядке SECTION_GROUPS; внутри — тумблеры раздела (TOGGLE_SECTION, строки
    уровня раздела, как в боте) и группы в порядке карты; пустое не рисуется. Ключ, не
    попавший ни в одну карту (сторож test_settings_ops держит это множество пустым), уезжает
    в хвостовой раздел, а не теряется молча."""
    by_group: dict[str, list[str]] = {}
    toggles_by_section: dict[str, list[str]] = {}
    leftovers: list[str] = []
    known_groups = {g for _s, _l, groups in settings_ops.SECTION_GROUPS for g in groups}
    for key in settings_ops.editable_keys():
        section = settings_ops.TOGGLE_SECTION.get(key)
        group = SETTINGS_SCHEMA[key].get("group")
        if section:
            toggles_by_section.setdefault(section, []).append(key)
        elif group in known_groups:
            by_group.setdefault(group, []).append(key)
        else:
            leftovers.append(key)

    sections: list[dict] = []
    total = 0
    for token, label, groups in settings_ops.SECTION_GROUPS:
        toggles = [await _item_for(k, ctx) for k in toggles_by_section.get(token, [])]
        group_rows = []
        for group in groups:
            keys = by_group.get(group, [])
            if not keys:
                continue
            row = {
                "token": group,
                "label": settings_ops.GROUP_LABELS.get(group, group),
                "items": [await _item_for(k, ctx) for k in keys],
            }
            if group == "reg_questions":
                # D-17 Task 3: тот же список ключей, что и `items` выше (флат-список остаётся
                # для поиска, T-19-45), плюс матричный вид трек × вопрос для экрана — тумблеры
                # матрицы шлют трек-композиты (settings_ops.reg_question_track_base), не сами
                # эти ключи.
                row["matrix"] = await _reg_questions_matrix()
            # Quick 260904-8o3 Task 3 (E5/E6): решение «где рисовать живое превью оформления»
            # принимает СЕРВЕР, не фронт (тот же приём, что row["matrix"] выше) — признак:
            # пересечение ключей группы с web_theme.THEME_KEYS.
            if set(keys) & set(web_theme.THEME_KEYS.values()):
                row["theme_preview"] = True
            group_rows.append(row)
        if not toggles and not group_rows:
            continue
        total += len(toggles) + sum(len(g["items"]) for g in group_rows)
        tier = "main" if token in settings_ops.SETTINGS_MAIN_SECTIONS else "rare"
        sections.append({"token": token, "label": label, "toggles": toggles, "groups": group_rows, "tier": tier})
    if leftovers:
        misc_label = settings_ops.GROUP_LABELS.get("misc", "📦 Прочие")
        items = [await _item_for(k, ctx) for k in leftovers]
        total += len(items)
        sections.append({
            "token": "misc",
            "label": misc_label,
            "toggles": [],
            "groups": [{"token": "misc", "label": misc_label, "items": items}],
            "tier": "rare",
        })
    return sections, total


async def _texts() -> dict[str, str]:
    """Все надписи экрана — ключи `miniapp_settings_*` реестра (план 22-02): экран не хранит
    ни одной строки (Copywriting Contract 22-UI-SPEC)."""
    return {
        key: await get_setting_typed(key)
        for key in settings_ops.editable_keys()
        if key.startswith("miniapp_settings_")
    }


@router.get("/app/api/admin/settings/all")
async def settings_all(
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    ctx = await _city_ctx(p.telegram_id)
    sections, total = await _sections(ctx)
    return {
        "sections": sections,
        "city_header": await _city_header(ctx),
        "texts": await _texts(),
        "total": total,
    }


class CityIn(BaseModel):
    code: str


@router.post("/app/api/admin/settings/city")
async def settings_city(
    body: CityIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    """Переключение шапки города — тот же `cities.set_admin_city`, что и в боте (D-04): один
    замок на город менеджера, одна запись `admin_city__{id}` в bot_settings."""
    if not await cities_module_on():
        raise HTTPException(400, {"reason": "cities_off"})
    if not await set_admin_city(p.telegram_id, body.code):
        raise HTTPException(400, {"reason": "bad_city"})
    header = await _city_header(await _city_ctx(p.telegram_id))
    assert header is not None
    return header


# ══ Phase 22 (22-04): атомарный пакет правок ═════════════════════════════════════════════


class BatchChange(BaseModel):
    key: str
    value: str | None = None  # None = сброс к значению по умолчанию (D-10, «-» бота)


class SettingsBatchIn(BaseModel):
    changes: list[BatchChange]
    base: dict[str, str | None] = {}  # сырое значение, которое видел экран (D-09)
    confirm: list[str] = []  # ключи, подтверждённые в диалоге (опасные, вкладки, stale)


def _editable_target(key: str) -> str | None:
    """База ключа из `changes`, если его можно править из веба: сам ключ реестра, per-city
    композит над per_city-ключом реестра, либо трек-композит вопроса анкеты
    (D-17 Task 3, `settings_ops.reg_question_track_base`). Иначе None -> 403 not_editable.

    Трек-композит возвращается САМ СОБОЙ (не своей базой) — у него нет `item_spec`-обёртки
    (матрица красит ячейку сама, без переотрисовки `item`, см. `settings_batch` ниже), в
    отличие от per-city композита, для которого `_item_for(base, ctx)` с шапкой города сам
    пересчитывает правильный составной ключ ответа."""
    if settings_ops.reg_question_track_base(key) is not None:
        return key
    base = settings_ops.base_setting_key(key)
    if base not in settings_ops.editable_keys():
        return None
    if key != base and not SETTINGS_SCHEMA[base].get("per_city"):
        return None
    return base


@router.post("/app/api/admin/settings/batch")
async def settings_batch(
    body: SettingsBatchIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    """Две фазы, между ними ни одной записи (D-08, T-22-02): (1) по каждому ключу — право на
    правку, `stale`-сверка `base` с текущим сырым значением, проверки бота
    (`settings_ops.validate_batch_item`, вкладка Sheets — `tab_row_count` отсюда); (2) только
    при пустых `errors`/`needs_confirm`/`stale` — `commit_batch_item` по каждому ключу.
    HTTP 200 и при непустых `errors` — это состояние формы, а не отказ запроса."""
    seen: set[str] = set()
    targets: dict[str, str] = {}
    for change in body.changes:
        if change.key in seen:
            raise HTTPException(400, {"reason": "duplicate_key", "key": change.key})
        seen.add(change.key)
        base = _editable_target(change.key)
        if base is None:
            raise HTTPException(403, {"reason": "not_editable", "key": change.key})
        targets[change.key] = base

    ctx = await _city_ctx(p.telegram_id)
    confirmed = set(body.confirm)
    errors: dict[str, str] = {}
    needs_confirm: list[dict] = []
    stale: list[dict] = []
    warnings: dict[str, str] = {}
    checked: dict[str, str | None] = {}

    # Фаза 1 — проверки по всем ключам.
    for change in body.changes:
        key = change.key
        if key in body.base and key not in confirmed:
            current = await get_setting(key)
            if current != body.base[key]:
                stale.append({
                    "key": key,
                    "raw": current,
                    "value": await get_setting_typed(key) if key == targets[key] else current,
                })
        probe = None
        if key in settings_ops.SHEET_TAB_WRITE_MODE and change.value and key not in confirmed:
            probe = await tab_row_count(change.value.strip())
        check = await settings_ops.validate_batch_item(
            key, change.value,
            visible_codes=ctx.visible, selected_city=ctx.selected, cities_on=ctx.on,
            tab_probe=probe, confirmed=key in confirmed,
        )
        if check.error:
            errors[key] = check.error
            continue
        if check.needs_confirm:
            needs_confirm.append({"key": key, "text": check.needs_confirm})
            continue
        if check.warning:
            warnings[key] = check.warning
        checked[key] = check.value

    saved: list[str] = []
    if not errors and not needs_confirm and not stale:
        # Фаза 2 — записи. Аудит «кто правит» — та же строка, что у бота (Quick 260820-rms).
        for change in body.changes:
            key = change.key
            logger.info(f"admin {p.telegram_id} правит настройку {key}")
            warning = await settings_ops.commit_batch_item(key, checked[key])
            if warning:
                warnings[key] = (warnings.get(key, "") + "\n\n" + warning).strip()
            saved.append(key)
        # E5 (quick 260904-de4): смена пресета в вебе обязана дописать ручки пресета — тот же
        # приём, что у кнопки пресета в боте (`miniapp_preset_apply`). Дозапись — ТОЛЬКО после
        # успешной фазы 2 (право "settings" уже проверил `require_cap` выше), только по ключам
        # из `web_theme.THEME_KEYS`, значения — из `PRESETS`, не из тела запроса (T-de4-03).
        preset_key = web_theme.THEME_KEYS["preset"]
        if preset_key in saved:
            preset_name = checked.get(preset_key)
            if isinstance(preset_name, str) and preset_name in web_theme.PRESETS:
                preset_writes = web_theme.preset_handle_writes(preset_name, skip_keys=seen)
                for handle_key, handle_value in preset_writes.items():
                    await set_setting(handle_key, handle_value)
                    targets[handle_key] = handle_key
                    saved.append(handle_key)
                logger.info(f"admin {p.telegram_id} применил пресет {preset_name} в вебе")
    else:
        warnings = {}

    # Свежие элементы затронутых ключей — экран не перезапрашивает весь реестр. Трек-композит
    # (D-17 Task 3) сюда не попадает: у него нет item_spec-обёртки (targets[key] == key, не
    # обычный ключ реестра), матрица красит свою ячейку сама — фронт уже знает точное
    # записанное значение (тумблер матрицы всегда шлёт явное "on"/"off", не сброс).
    ctx = await _city_ctx(p.telegram_id)
    items = [
        await _item_for(targets[key], ctx)
        for key in saved
        if settings_ops.reg_question_track_base(key) is None
    ]
    return {
        "saved": saved,
        "errors": errors,
        "needs_confirm": needs_confirm,
        "stale": stale,
        "warnings": warnings,
        "items": items,
    }


# ══ Phase 22 (22-04, D-07): превью — текст глазами делегата, до сохранения ═══════════════


@router.get("/app/api/admin/settings/preview")
async def settings_preview(
    key: str = Query(...),
    value: str = Query(""),
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    """Значение берётся из запроса (ещё не сохранённое), плейсхолдеры подставляются теми же
    `.replace`-цепочками, что у консьюмеров бота; результат — plain-текст для DOM."""
    if _editable_target(key) is None:
        raise HTTPException(403, {"reason": "not_editable", "key": key})
    samples = await settings_ops.preview_samples()
    return {"text": settings_ops.preview_text(key, value, samples=samples)}


# ══ Quick 260904-8o3 Task 3 (E5/E6, T-8o3-02): живое превью оформления, ДО сохранения ═════


class ThemePreviewIn(BaseModel):
    changes: list[BatchChange]


@router.post("/app/api/admin/theme/preview")
async def theme_preview(
    body: ThemePreviewIn,
    p: Principal = Depends(require_cap("settings")),
    _: Principal = Depends(require_section("settings")),
) -> dict:
    """Мини-плита «🎨 Оформление» на экране настроек должна реагировать на смену пресета/
    паттерна/шрифта ДО того, как менеджер нажмёт «Сохранить» (владелец 04.09, E5/E6: сегодня
    видна только полоска сверху). Ручка НИЧЕГО не пишет в БД — читает текущие значения
    `web_theme.THEME_KEYS`, накладывает сверху черновик `changes` и пересчитывает CSS-
    переменные тем же `resolve_theme`/`theme_css_vars`, что и боевая `/app/theme.css`.

    Ключ вне `THEME_KEYS.values()` — недоверенный вход с клиента (T-8o3-02) — отклоняется
    403 `not_editable`, не тихим игнором: экран мог бы иначе прислать чужой ключ реестра
    незамеченным.

    Quick 260904-de4 (E5, второй корень): чтения — СЫРЫЕ (`get_setting`, `None` = «не
    задано»), а не `get_setting_typed`: незаданная ручка приходила бы дефолтом реестра, и
    `resolve_theme` считал бы любое валидное значение явной ручкой — пресет в превью никогда
    не побеждал бы. Если среди `changes` пришла смена пресета на валидное имя — подмешиваем
    `preset_handle_writes` (те же ручки, что допишет `settings_batch` при сохранении) поверх
    ключей из `changes`: превью обязано показывать РОВНО то состояние, которое даст
    «Сохранить», иначе менеджер увидит одно, а получит другое."""
    theme_keys = set(web_theme.THEME_KEYS.values())
    for change in body.changes:
        if change.key not in theme_keys:
            raise HTTPException(403, {"reason": "not_editable", "key": change.key})

    settings = {key: await get_setting(key) for key in theme_keys}
    changed_keys = {change.key for change in body.changes}
    for change in body.changes:
        settings[change.key] = change.value

    preset_key = web_theme.THEME_KEYS["preset"]
    if preset_key in changed_keys:
        preset_writes = web_theme.preset_handle_writes(settings[preset_key], skip_keys=changed_keys)
        settings.update(preset_writes)

    resolved = web_theme.resolve_theme(settings)
    return {"vars": web_theme.theme_css_vars(resolved)}


__all__ = ["router", "EDITABLE_KEYS"]
