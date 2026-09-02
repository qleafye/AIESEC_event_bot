"""Phase 19 (D-08)/21 (21-11, D-24): профиль делегата — только просмотр + вход в правку.
`GET /app/api/profile`.

Поля анкеты — по `reg_labels.REG_LABELS` (тот же объект, что у бота и админки), для тех
колонок `users`, где значение непустое, в порядке `REG_LABELS`. Служебные колонки
(`telegram_id`, `resume_file_id`, `receipt_file_id`, `referrer_id`, статусы, даты системы)
наружу не отдаются — они не вопросы анкеты. Ответ с ПД не логируется (T-19-14).

Колонки вопроса анкеты читаются из `reg_engine.STEP_TO_COLUMN` (план 21-11) — второй копии
схемы анкеты (литеральный словарь ключ-анкеты-к-колонке, живший раньше прямо в этом файле)
в проекте больше нет. Ключ подписи вопроса — `reg_engine.label_key_for(step_key)` (setting_key
из `REG_FLOW`, тот же ключ, по которому бот и админка берут подпись из `REG_LABELS`); своей
таблицы алиасов у профиля нет — источник один и тот же для бота, мастера и профиля.

Точка входа в правку (D-24): кнопка «✏️ Изменить анкету» ведёт `navigate("#/form")` внутри
приложения (`screens/profile.js`), а не по deep-link в бота — прежний нерабочий deep-link
(старый параметр запуска, который `cmd_start` никогда не разбирал) в профиле больше не
публикуется (`edit_deeplink`/`edit_hint` убраны); признак `can_edit` + подпись кнопки
`edit_cta_text` из реестра `reg_form_profile_edit_cta_text`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

import reg_engine
from database.db import get_user
from reg_labels import PAYMENT_STATUS_LABELS, REG_LABELS, STATUS_LABELS
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, require_section

router = APIRouter()

# Вопросы с составным представлением (две колонки users вместо одной) — резюме
# текстом/ссылкой, ожидания на русском/арабском. Единственное явное исключение поверх
# reg_engine.STEP_TO_COLUMN, не вторая копия схемы (значения перенесены дословно из старого
# литерального словаря этого файла).
_EXTRA_ANSWER_COLUMNS_BY_STEP: dict[str, tuple[str, ...]] = {
    "expectations": ("expectations", "expectations_ar"),
    "resume": ("resume_text", "resume_url"),
}


def _profile_columns() -> dict[str, tuple[str, ...]]:
    """Вопрос анкеты (ключ REG_LABELS) -> колонка(и) `users`, где лежит ответ — выведено из
    `reg_engine.STEP_TO_COLUMN`, единственного источника схемы анкеты (план 21-11). Вопросы
    без подписи в REG_LABELS (например `full_name` — он не «вопрос», а отдельное поле профиля)
    в вывод не попадают."""
    out: dict[str, tuple[str, ...]] = {}
    for step_key, column in reg_engine.STEP_TO_COLUMN.items():
        label_key = reg_engine.label_key_for(step_key)
        if label_key not in REG_LABELS:
            continue
        out[label_key] = _EXTRA_ANSWER_COLUMNS_BY_STEP.get(step_key, (column,))
    return out

# Булевы колонки показываем словом, а не 0/1 — как в таблице (`SHEET_COLUMNS`).
_BOOL_COLUMNS = {"work_status": ("Да", "Нет"), "is_ambassador_candidate": ("Да", None)}


def _value(user: dict, column: str) -> str | None:
    raw = user.get(column)
    if column in _BOOL_COLUMNS:
        yes, no = _BOOL_COLUMNS[column]
        return yes if raw else no
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def profile_fields(user: dict) -> list[dict]:
    """`[{key, label, value}]` в порядке REG_LABELS, только непустые значения."""
    out = []
    columns_by_key = _profile_columns()
    for key, label in REG_LABELS.items():
        columns = columns_by_key.get(key)
        if not columns:
            continue
        values = [v for v in (_value(user, c) for c in columns) if v]
        if values:
            out.append({"key": key, "label": label, "value": " / ".join(values)})
    return out


@router.get("/app/api/profile")
async def profile(request: Request, p: Principal = Depends(delegate_gate),
                  _: Principal = Depends(require_section("profile"))) -> dict:
    user = await get_user(p.telegram_id) or {}
    status = user.get("status") or "approved"
    payment_status = user.get("payment_status") or "not_paid"
    # Тумблер «💳 Модуль оплаты» выключен -> статус оплаты не существует как понятие:
    # «Не оплатил» на профиле пугал бы делегата счётом, которого нет (вопрос владельца 02.09).
    payment_on = await get_setting_typed("payment_enabled") == "on"
    return {
        "full_name": user.get("full_name"),
        "username": user.get("username"),
        "fields": profile_fields(user),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "payment_status": payment_status if payment_on else "",
        "payment_status_label": (
            PAYMENT_STATUS_LABELS.get(payment_status, payment_status) if payment_on else ""
        ),
        # D-24: правка анкеты — экран #/form внутри приложения, не deep-link в бота (прежний
        # параметр запуска нигде не обрабатывался — кнопка «Изменить» вела в никуда, RESEARCH
        # Pitfall 1; `?start=edit` остаётся fallback-путём ИЗ БОТА, план 21-09, но профиль его
        # больше не публикует — навигация внутри приложения, `screens/profile.js`).
        "can_edit": True,
        "edit_cta_text": await get_setting_typed("reg_form_profile_edit_cta_text"),
    }
