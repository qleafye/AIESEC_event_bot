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
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database.db import set_setting
from settings_schema import SETTINGS_SCHEMA, get_setting_typed

from miniapp.deps import Principal, require_cap, require_section

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


__all__ = ["router", "EDITABLE_KEYS"]
