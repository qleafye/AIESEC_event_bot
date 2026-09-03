"""Phase 19 (D-08)/21 (21-11, D-24)/23.1-05 (UI-REDESIGN-05, D-10): профиль делегата — только
просмотр + вход в правку. `GET /app/api/profile`.

Поля анкеты — по `reg_labels.REG_LABELS` (тот же объект, что у бота и админки), для тех
колонок `users`, где значение непустое, в порядке `REG_LABELS`. Служебные колонки
(`telegram_id`, `resume_file_id`, `receipt_file_id`, `referrer_id`, статусы, даты системы)
наружу не отдаются — они не вопросы анкеты. Ответ с ПД не логируется (T-19-14).

Колонки вопроса анкеты читаются из `reg_engine.STEP_TO_COLUMN` (план 21-11) — второй копии
схемы (литеральный словарь ключ-анкеты-к-колонке, живший раньше прямо в этом файле)
в проекте больше нет. Ключ подписи вопроса — `reg_engine.label_key_for(step_key)` (setting_key
из `REG_FLOW`, тот же ключ, по которому бот и админка берут подпись из `REG_LABELS`); своей
таблицы алиасов у профиля нет — источник один и тот же для бота, мастера и профиля.

Точка входа в правку (D-24): кнопка «✏️ Изменить анкету» ведёт `navigate("#/form")` внутри
приложения (`screens/profile.js`), а не по deep-link в бота — прежний нерабочий deep-link
(старый параметр запуска, который `cmd_start` никогда не разбирал) в профиле больше не
публикуется (`edit_deeplink`/`edit_hint` убраны); признак `can_edit` + подпись кнопки
`edit_cta_text` из реестра `reg_form_profile_edit_cta_text`.

Плита профиля (план 23.1-05, макет `mockups/04-profile.png`): монограмма (`initials`, до двух
заглавных букв из `full_name` — считается ЗДЕСЬ, а не строковой логикой на клиенте, тот же
принцип, что и у `miniapp.avatars.initials`, но с другим пустым исходом — «» вместо «?», у
профиля своя же карточка, заглушка-плейсхолдер плите не нужна) и `city_label` (тот же приём
резолва, что `applications.py::applications_next` для карточки заявки — `cities_module_on()` +
`city_label(normalize_city(event_city))`, а не второй копией правила).

Контакты (`contacts`) выделены из общей анкеты фиксированным кортежем `_CONTACT_LABEL_KEYS`
(email/телефон/работа) — те же ключи, что и `fields`, поэтому исключены ИЗ `fields`, чтобы
строка не показывалась дважды (одна и та же `_profile_columns()`, не вторая схема).

Прогресс анкеты (`form_filled`/`form_total`/`form_percent`/`form_progress_text`) и сводная
строка дат (`form_meta_text` — «Отправлена {date}» + опционально «изменена {date}» +
опционально «одобрена {date}», D-10) — обе собраны на сервере из реестра, клиент только
рисует готовую строку (D-06). Дата форматируется ЕДИНСТВЕННЫМ существующим в проекте
человеческим форматом даты Mini App — `services.applications.format_edited_date` (тот же
помощник, что карточка заявки менеджера использует для «✏️ Изменена»; второго формата дат
этот план не заводит).

D-10 (владелец, `23.1-CONTEXT.md` O-2): `users.approved_at` проставляется ОДНИМ и тем же
атомарным `UPDATE` в `database.db.approve_user_atomic`/`approve_all_pending` — единственная
точка правды для всех трёх путей одобрения (бот: карточка «✅ Одобрить» и «Принять всех»,
веб: `miniapp/routers/applications.py`), т.к. бот кое-где зовёт `database.db.approve_all_pending`
НАПРЯМУЮ, минуя `services.applications.claim_approve_all` (`handlers/admin_moderation.py`,
`appr_all_yes`) — стамповать `approved_at` только в обёртках сервиса означало бы пропустить
чатовое «Принять всех». Профиль здесь просто читает уже проставленную колонку.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

import reg_engine
from cities import cities_module_on, city_label as resolve_city_label, normalize_city
from database.db import get_user
from reg_labels import PAYMENT_STATUS_LABELS, REG_LABELS, STATUS_LABELS
from services.applications import format_edited_date
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

# Phase 23.1-05 (UI-REDESIGN-05): вопросы, вынесенные из общей анкеты в раздел «Контакты» на
# плите профиля — фиксированный кортеж ключей REG_LABELS (не строковых угадываний), в порядке
# показа. Есть в _profile_columns() этого события -> раздел короче, а не ошибка.
_CONTACT_LABEL_KEYS = ("reg_q_email", "reg_q_phone", "reg_q_work")


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
    """`[{key, label, value}]` в порядке REG_LABELS, только непустые значения, БЕЗ вопросов из
    `_CONTACT_LABEL_KEYS` (они — отдельный раздел «Контакты», `contact_fields()` ниже; один и
    тот же вопрос не показывается дважды)."""
    out = []
    columns_by_key = _profile_columns()
    for key, label in REG_LABELS.items():
        if key in _CONTACT_LABEL_KEYS:
            continue
        columns = columns_by_key.get(key)
        if not columns:
            continue
        values = [v for v in (_value(user, c) for c in columns) if v]
        if values:
            out.append({"key": key, "label": label, "value": " / ".join(values)})
    return out


def contact_fields(user: dict) -> list[dict]:
    """`[{key, label, value}]` для `_CONTACT_LABEL_KEYS`, в фиксированном порядке кортежа —
    та же `_profile_columns()`, что и `profile_fields`, второй схемы нет. Вопроса нет в анкете
    этого события (не в `_profile_columns()`) или значение пусто -> элемента нет, список короче,
    не ошибка (CLAUDE.md: ничего не падает на отсутствии необязательного поля)."""
    out = []
    columns_by_key = _profile_columns()
    for key in _CONTACT_LABEL_KEYS:
        columns = columns_by_key.get(key)
        if not columns:
            continue
        values = [v for v in (_value(user, c) for c in columns) if v]
        if values:
            out.append({"key": key, "label": REG_LABELS[key], "value": " / ".join(values)})
    return out


async def _profile_city_label(user: dict) -> str | None:
    """Подпись города делегата — ТОТ ЖЕ вызов, что `applications.py::applications_next` уже
    делает для карточки заявки менеджера (не вторая копия правила D-08/CITY-08): модуль городов
    включён и есть `event_city` -> `city_label(normalize_city(event_city))`; иначе — свободный
    текст `users.city` как есть; ничего не задано -> `None`."""
    if await cities_module_on():
        event_city = user.get("event_city")
        if event_city:
            return await resolve_city_label(normalize_city(event_city))
    raw = user.get("city")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _initials(full_name: str | None) -> str:
    """До двух заглавных букв из `full_name`: «Иван Петров» -> «ИП», «Иван» -> «И», пусто ->
    «» (узла монограммы на плите тогда просто нет — не «?»-заглушка, как у аватара очереди
    заявок `miniapp.avatars.initials`; у профиля своя карточка, второй пустой смысл не нужен)."""
    parts = (full_name or "").split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _form_meta_text(user: dict, submitted_tpl: str | None, edited_tpl: str | None,
                     approved_tpl: str | None) -> str | None:
    """Одна строка из дат анкеты — «Отправлена {date}» (`registration_date`) + опционально
    «изменена {date}» (`edited_at`) + опционально «одобрена {date}» (`approved_at`, D-10),
    склеенных ` · `. Дата отсутствует -> её фрагмента нет вовсе; ни одной даты -> `None`
    (клиент вообще не рисует метастроку)."""
    parts: list[str] = []
    registration_date = user.get("registration_date")
    if registration_date and submitted_tpl:
        parts.append(submitted_tpl.format(date=format_edited_date(registration_date)))
    edited_at = user.get("edited_at")
    if edited_at and edited_tpl:
        parts.append(edited_tpl.format(date=format_edited_date(edited_at)))
    approved_at = user.get("approved_at")
    if approved_at and approved_tpl:
        parts.append(approved_tpl.format(date=format_edited_date(approved_at)))
    return " · ".join(parts) if parts else None


@router.get("/app/api/profile")
async def profile(request: Request, p: Principal = Depends(delegate_gate),
                  _: Principal = Depends(require_section("profile"))) -> dict:
    user = await get_user(p.telegram_id) or {}
    status = user.get("status") or "approved"
    payment_status = user.get("payment_status") or "not_paid"
    # Тумблер «💳 Модуль оплаты» выключен -> статус оплаты не существует как понятие:
    # «Не оплатил» на профиле пугал бы делегата счётом, которого нет (вопрос владельца 02.09).
    payment_on = await get_setting_typed("payment_enabled") == "on"

    fields = profile_fields(user)
    contacts = contact_fields(user)
    form_total = len(_profile_columns())
    form_filled = len(fields) + len(contacts)
    form_percent = round(100 * form_filled / form_total) if form_total else None

    progress_tpl = await get_setting_typed("miniapp_profile_form_progress_text")
    form_progress_text = (
        progress_tpl.format(filled=form_filled, total=form_total) if progress_tpl else None
    )

    submitted_tpl = await get_setting_typed("miniapp_profile_submitted_text")
    edited_tpl = await get_setting_typed("miniapp_profile_edited_text")
    approved_tpl = await get_setting_typed("miniapp_profile_approved_text")  # D-10
    form_meta_text = _form_meta_text(user, submitted_tpl, edited_tpl, approved_tpl)

    return {
        "full_name": user.get("full_name"),
        "username": user.get("username"),
        "initials": _initials(user.get("full_name")),
        "city_label": await _profile_city_label(user),
        "fields": fields,
        "contacts": contacts,
        "contacts_eyebrow": await get_setting_typed("miniapp_profile_contacts_eyebrow"),
        "form_eyebrow": await get_setting_typed("miniapp_profile_form_eyebrow"),
        "form_filled": form_filled,
        "form_total": form_total,
        "form_percent": form_percent,
        "form_progress_text": form_progress_text,
        "form_meta_text": form_meta_text,
        "privacy_note": await get_setting_typed("miniapp_profile_privacy_note"),
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
