"""Phase 19 (D-08): профиль делегата — только просмотр. `GET /app/api/profile`.

Поля анкеты — по `reg_labels.REG_LABELS` (тот же объект, что у бота и админки), для тех
колонок `users`, где значение непустое, в порядке `REG_LABELS`. Служебные колонки
(`telegram_id`, `resume_file_id`, `receipt_file_id`, `referrer_id`, статусы, даты системы)
наружу не отдаются — они не вопросы анкеты. Ответ с ПД не логируется (T-19-14).

Редактирования нет: кнопка «Изменить — в боте» — deep-link `?start=rereg` на перерегистрацию;
текст подсказки — ключ реестра `miniapp_profile_edit_hint`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from database.db import get_user
from reg_labels import PAYMENT_STATUS_LABELS, REG_LABELS, STATUS_LABELS
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, require_section

router = APIRouter()

# Вопрос анкеты (ключ REG_LABELS) -> колонка(и) `users`, где лежит ответ. Зеркало
# `handlers/reg_schema.py::SHEET_COLUMNS` (оттуда импортировать нельзя — aiogram). Вопросы
# без колонки в users (пока нет такого поля) здесь отсутствуют и в профиль не попадают.
PROFILE_COLUMNS: dict[str, tuple[str, ...]] = {
    "reg_q_age": ("age",),
    "reg_q_vk": ("vk_username",),
    "reg_q_email": ("email",),
    "reg_q_phone": ("phone",),
    "reg_q_city": ("city",),
    "reg_q_source": ("source",),
    "reg_q_lc": ("local_committee",),
    "reg_q_position": ("position",),
    "reg_q_education": ("education_status",),
    "reg_q_university": ("university",),
    "reg_q_course": ("course",),
    "reg_q_specialty": ("specialty",),
    "reg_q_work": ("work_status",),
    "reg_q_work_sphere": ("work_sphere",),
    "reg_q_skills": ("missing_skills",),
    "reg_q_expectations": ("expectations", "expectations_ar"),
    "reg_q_informal_day": ("informal_day",),
    "reg_q_attendance": ("attendance_format",),
    "reg_q_comments": ("comments",),
    "reg_q_department": ("department",),
    "reg_q_aiesec_role": ("aiesec_role",),
    "reg_q_certificate": ("needs_certificate",),
    "reg_q_alumni_status": ("alumni_status",),
    "reg_q_english": ("english_level",),
    "reg_q_allergies": ("allergies",),
    "reg_q_food": ("food_pref",),
    "reg_q_arrival": ("arrival",),
    "reg_q_housing": ("housing",),
    "reg_q_bed_sharing": ("bed_sharing",),
    "reg_q_bed_partner": ("bed_partner",),
    "reg_q_transport": ("transport",),
    "reg_q_payment_date": ("payment_plan_date",),
    "reg_q_cc_shop": ("cc_shop",),
    "reg_q_exp_organizers": ("exp_organizers",),
    "reg_q_exp_content": ("exp_content",),
    "reg_q_volunteer": ("volunteer",),
    "reg_q_arrival_date": ("arrival_date",),
    "reg_q_birth_date": ("birth_date",),
    "reg_q_study_field": ("study_field",),
    "reg_q_goal": ("goal",),
    "reg_q_formats": ("formats",),
    "reg_q_ambassador": ("is_ambassador_candidate",),
    "reg_q_resume": ("resume_text", "resume_url"),
}

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
    for key, label in REG_LABELS.items():
        columns = PROFILE_COLUMNS.get(key)
        if not columns:
            continue
        values = [v for v in (_value(user, c) for c in columns) if v]
        if values:
            out.append({"key": key, "label": label, "value": " / ".join(values)})
    return out


def rereg_deeplink(bot_username: str | None) -> str:
    return f"https://t.me/{bot_username}?start=rereg" if bot_username else ""


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
        "edit_deeplink": rereg_deeplink(request.app.state.cfg.bot_username),
        "edit_hint": await get_setting_typed("miniapp_profile_edit_hint"),
    }
