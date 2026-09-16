"""Phase 23.1 (UI-REDESIGN-02): хаб делегата — тексты и факты первого экрана. Только чтение.
С плана 23.1-06 — официально ручка делегатской обвязки: подписи плиты зовут отсюда не только
хаб, но и три списочных экрана (задания/монеты/рейтинг), четвёртый независимый источник
подписей заводить нельзя.

Клиенту достаточно ОДНОГО запроса, чтобы получить все надписи и посчитанные факты плиты —
подстановка `{done}`/`{total}`/`{days}` делается ЗДЕСЬ, на сервере: `hub.js` ничего не
форматирует (D-06 — тексты хаба это реестр, не литералы JS). `done`/`total` — тот же
источник правды, что список заданий (`miniapp.routers.tasks.tasks_progress`, тот же
`list_active_tasks(city_scope=…)`), `event_dates`/`event_place` — те же ключи
`event_date`/`event_place_name`, что видит бот, с городским скоупом делегата.
`rank_unit` — подпись места в рейтинге с уже подставленным `{total}` (число участников,
`miniapp.routers.coins.count_participants` — тот же источник, что у самого рейтинга и
у баланса монет).

Гейт — `delegate_gate` (тот же приём, что у `miniapp/routers/coins.py`): у хаба нет своего
раздела-чекбокса в `SECTIONS` (он и есть дом приложения), поэтому `require_section` здесь
не нужен — только принадлежность к одобренным делегатам.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request

from cities import get_setting_typed_for_city
from database.db import get_referrals, get_setting, get_user
from payment_options import parse_options
from services import applications
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, form_gate
from miniapp.routers.coins import count_participants
from miniapp.routers.tasks import delegate_city_scope, tasks_progress
from miniapp.timeutil import today_msk

router = APIRouter()


# ── Phase 30 (30-05, задача 3, A2-07): экран статуса заявки + плита-ссылка на хабе ──────────
#
# Расширяет `/app/api/hub/status`, новую ручку не заводит (`<interfaces>` 30-05-PLAN.md).
# Поля до этой фазы (`status/heading/body/days/cta_text/event_dates/event_place/reason_line`)
# НЕ трогаются — старый клиент (тумблер `reg_form_status_screen` выключен, дефолт) получает
# байт-в-байт прежний ответ; новые поля ниже читаются ТОЛЬКО когда `status_screen_enabled`
# истинен — `screens/status.js`/расширённая плита `hub.js` проверяют именно этот флаг, не
# наличие отдельных полей поодиночке.


_STATUS_SCREEN_EXTRAS_OFF: dict = {
    "badge": None, "title": None, "screen_body": None,
    "next_steps_eyebrow": None, "next_steps": [],
    "edit_button_text": None, "payment": None, "pay_button_text": None,
    "reason_eyebrow": None, "reason_text": None, "reason_date": None,
    "fix_eyebrow": None, "fix_fields": None, "saved_answers_label": None,
    "resubmit_button_text": None, "tile_text": None,
}


async def _next_steps(prefix: str) -> list[dict]:
    """Три шага «Что дальше» одного состояния — `reg_status_{prefix}_step{1..3}_title_text`/
    `_body_text`, тот же реестровый шаблон для review/approved (30-UI-SPEC.md таблицы)."""
    steps = []
    for i in (1, 2, 3):
        title = await get_setting_typed(f"reg_status_{prefix}_step{i}_title_text")
        body = await get_setting_typed(f"reg_status_{prefix}_step{i}_body_text")
        if title or body:
            steps.append({"title": title, "body": body})
    return steps


async def _payment_card(user: dict) -> dict | None:
    """Карточка оплаты экрана «Одобрена» — сумма и срок ТОЛЬКО когда модуль оплаты включён и у
    делегата есть тариф (30-CONTEXT.md решение оркестратора: «следует за payment_enabled и
    наличием тарифа, отдельного ключа нет»). Переиспользует существующий парсер
    `payment_options` (корневой `payment_options.parse_options`, без aiogram-зависимости —
    `handlers/payment.py` держит `_parse_options` как реэкспорт оттуда же) — новой логики
    оплаты не пишем."""
    if await get_setting_typed("payment_enabled") != "on":
        return None
    option_label = user.get("payment_option")
    payment_due = user.get("payment_due")
    if not option_label or not payment_due:
        return None

    amount = None
    for label, price, _tracks in parse_options(await get_setting("payment_options") or ""):
        if label == option_label:
            amount = price
            break
    if amount is None:
        return None
    due_date = str(payment_due).split()[0]
    due_label_tpl = await get_setting_typed("reg_status_payment_due_label_text")
    return {
        "amount": amount,
        "due_date": due_date,
        "due_label": due_label_tpl.replace("{дата}", due_date) if due_label_tpl else None,
        "reminder_note": await get_setting_typed("reg_status_payment_reminder_note_text"),
    }


def _days_until(raw: str | None) -> int | None:
    """Целое число полных дней от московского «сегодня» (`miniapp.timeutil.today_msk` —
    план 23.1-05 вынес общий помощник отсюда, чтобы `tasks.py` не заводил второй литерал
    часового пояса) до даты `raw` (строго ДД.ММ.ГГГГ). Дата не задана, не разбирается или уже
    прошла (строго меньше сегодняшней) -> `None`."""
    if not raw:
        return None
    try:
        target = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    delta = (target - today_msk()).days
    return delta if delta >= 0 else None


# ── Приёмка 17.09 (п.1): постоянное место реф-ссылки в хабе ─────────────────────────────────
#
# Правило видимости — ТО ЖЕ, что у кнопки чата «🔗 Моя реферальная ссылка»
# (`keyboards/builders.py::MENU_BUTTONS`, тумблер `menu_referral`): маршрут уже требует
# `delegate_gate` (аналог `ensure_registered` — одобренная заявка, любой legacy-статус без
# записи трактуется как approved), здесь дополнительно тот же тумблер `menu_referral`
# (per_city) — выключен для города делегата -> `referral: None`, блока в хабе нет вовсе
# (то же самое «кнопки нет» на клавиатуре бота). Число приглашённых — второй, независимый
# тумблер `menu_invites` (кнопка «👥 Мои приглашённые»): выключен -> ссылка остаётся,
# `invites_text` пуст. Ссылка строится в ТОМ ЖЕ формате, что и ботовская кнопка меню
# (`https://t.me/<bot>?start=<telegram_id>`, БЕЗ префикса `amb_` — это формат другого,
# отдельного потока «Хочу свою ссылку» на финальном экране анкеты, `handlers/reg_ambassador.py`,
# сюда не переносится: делаем ровно то же самое, что видит делегат по кнопке меню).
async def _referral_block(telegram_id: int, event_city: str | None, bot_username: str | None) -> dict | None:
    if await get_setting_typed_for_city("menu_referral", event_city) != "on":
        return None
    if not bot_username:
        return None
    invites_text = None
    if await get_setting_typed_for_city("menu_invites", event_city) == "on":
        count = len(await get_referrals(telegram_id))
        invites_tpl = await get_setting_typed("miniapp_hub_referral_invites_text")
        invites_text = invites_tpl.format(count=count) if invites_tpl else None
    return {
        "label": await get_setting_typed("miniapp_hub_referral_label_text"),
        "link": f"https://t.me/{bot_username}?start={telegram_id}",
        "copy_button": await get_setting_typed("miniapp_form_ambassador_copy_button_text"),
        "copied_toast": await get_setting_typed("miniapp_form_ambassador_copied_toast_text"),
        "invites_text": invites_text,
    }


@router.get("/app/api/hub")
async def hub(request: Request, p: Principal = Depends(delegate_gate)) -> dict:
    user = await get_user(p.telegram_id)
    event_city = user.get("event_city") if user else None

    done, total = await tasks_progress(p.telegram_id, await delegate_city_scope(p.telegram_id))
    tasks_fact_text = await get_setting_typed("miniapp_hub_tasks_fact_text")
    tasks_fact = tasks_fact_text.format(done=done, total=total) if tasks_fact_text else None

    countdown_date = await get_setting_typed_for_city("miniapp_hub_countdown_date", event_city)
    days = _days_until(countdown_date)
    days_fact_text = await get_setting_typed("miniapp_hub_days_fact_text")
    days_fact = days_fact_text.format(days=days) if (days is not None and days_fact_text) else None

    event_dates = await get_setting_typed_for_city("event_date", event_city) or None
    event_place = await get_setting_typed_for_city("event_place_name", event_city) or None

    # Плита списочного экрана «Рейтинг» (план 23.1-06): «из {total}» подставляется здесь —
    # число участников известно ручке (тот же count_participants, что у /coins/balance и
    # /leaderboard), отдавать шаблон с недоставленной подстановкой нельзя.
    rank_unit_text = await get_setting_typed("miniapp_leaderboard_plate_unit")
    total_participants = await count_participants()
    rank_unit = rank_unit_text.format(total=total_participants) if rank_unit_text else None

    referral = await _referral_block(p.telegram_id, event_city, request.app.state.cfg.bot_username)

    return {
        "balance_eyebrow": await get_setting_typed("miniapp_hub_balance_eyebrow"),
        "balance_unit": await get_setting_typed("miniapp_hub_balance_unit"),
        "next_eyebrow": await get_setting_typed("miniapp_hub_next_eyebrow"),
        "sections_eyebrow": await get_setting_typed("miniapp_hub_sections_eyebrow"),
        "tasks_fact": tasks_fact,
        "days_fact": days_fact,
        "event_dates": event_dates,
        "event_place": event_place,
        "tasks_eyebrow": await get_setting_typed("miniapp_tasks_plate_eyebrow"),
        "rank_eyebrow": await get_setting_typed("miniapp_leaderboard_plate_eyebrow"),
        "rank_unit": rank_unit,
        "referral": referral,
    }


@router.get("/app/api/hub/status")
async def hub_status(p: Principal = Depends(form_gate)) -> dict:
    """Плита состояния анкеты над плитками хаба (UAT D3, quick 260904-aup): pending/rejected
    делегату сегодня нечего показать (`delegate_gate` отдал бы ему 403), а `form_gate`
    пропускает ровно этих двоих плюс незарегистрированного/черновик — им ручка отвечает
    `heading: None`, клиент просто не рисует плиту. `require_section` не нужен — у хаба нет
    своего раздела-чекбокса (см. докстринг модуля выше)."""
    user = await get_user(p.telegram_id) or {}
    status = user.get("status") or "approved"
    event_city = user.get("event_city")

    event_dates = await get_setting_typed_for_city("event_date", event_city) or None
    event_place = await get_setting_typed_for_city("event_place_name", event_city) or None
    # Phase 30 (30-05, задача 3): выключенный тумблер -> ниже опубликованные поля пустые
    # (`status_screen_enabled: False`), клиент рисует ТОЛЬКО старые поля — сегодняшнее
    # поведение обеих поверхностей (плита хаба, экрана #/status ещё нет) без изменений.
    status_screen_on = await get_setting_typed_for_city("reg_form_status_screen", event_city) == "on"
    # Тумблер вибрации анкеты (30-03) — независим от status_screen: тап по плите хаба обязан
    # молчать при выключенной вибрации, тем же способом, что form_types.js::haptic (свой
    # локальный хелпер на файл, второй общий мотор не заводим).
    haptics_on = await get_setting_typed("reg_form_haptics") == "on"

    if status == "pending":
        heading = await get_setting_typed("miniapp_hub_pending_heading_text")
        body_tpl = await get_setting_typed("miniapp_hub_pending_body_text")
        days = await get_setting_typed_for_city("miniapp_hub_pending_days", event_city)
        # Та же подстановка, что applications.py::applications_next делает для «{count}» —
        # `.format()` уронил бы ручку, если менеджер случайно сотрёт фигурные скобки в тексте.
        body = body_tpl.replace("{days}", str(days)) if body_tpl else None
        extra = {
            "badge": await get_setting_typed("reg_status_review_badge_text"),
            "title": await get_setting_typed("reg_status_review_title_text"),
            "screen_body": await get_setting_typed("reg_status_review_body_text"),
            "next_steps_eyebrow": await get_setting_typed("reg_status_next_eyebrow_text"),
            "next_steps": await _next_steps("review"),
            "edit_button_text": await get_setting_typed("reg_status_edit_button_text"),
            "payment": None, "pay_button_text": None,
            "reason_eyebrow": None, "reason_text": None, "reason_date": None,
            "fix_eyebrow": None, "fix_fields": None, "saved_answers_label": None,
            "resubmit_button_text": None,
            "tile_text": await get_setting_typed("reg_status_tile_review_text"),
        } if status_screen_on else _STATUS_SCREEN_EXTRAS_OFF
        return {
            "status": status, "heading": heading, "body": body, "days": days,
            "cta_text": None, "event_dates": event_dates, "event_place": event_place,
            "reason_line": None, "status_screen_enabled": status_screen_on,
            "haptics_enabled": haptics_on, **extra,
        }
    if status == "rejected":
        heading = await get_setting_typed("miniapp_hub_rejected_heading_text")
        body = await get_setting_typed("miniapp_hub_rejected_body_text")
        cta_text = await get_setting_typed("miniapp_hub_rejected_cta_text")
        # Quick 260904-liz: причина последнего НЕ отменённого отказа — `last_rejection_reason`
        # читает СТРОГО по `p.telegram_id` из `form_gate` (T-liz-02: чужую причину узнать
        # нельзя, параметра для этого в ручке нет). `.replace`, не `.format` — та же защита от
        # стёртых менеджером фигурных скобок, что и у `body` выше; строки нет вовсе, если нет
        # либо причины (старый отказ/отказ без причины), либо самого шаблона.
        reason = await applications.last_rejection_reason(p.telegram_id)
        reason_tpl = await get_setting_typed("miniapp_hub_rejected_reason_text")
        reason_line = reason_tpl.replace("{reason}", reason) if (reason and reason_tpl) else None
        extra = {
            "badge": await get_setting_typed("reg_status_rejected_badge_text"),
            "title": await get_setting_typed("reg_status_rejected_title_text"),
            "screen_body": await get_setting_typed("reg_status_rejected_body_text"),
            "next_steps_eyebrow": None, "next_steps": [],
            "edit_button_text": None, "payment": None, "pay_button_text": None,
            "reason_eyebrow": await get_setting_typed("reg_status_reason_eyebrow_text"),
            # T-30-11/T-30-12 (threat register): причина строго СВОЯ (та же `last_rejection_
            # reason(p.telegram_id)`, что и `reason_line` выше — второй запрос не заводим), без
            # имени менеджера (30-CONTEXT.md решение владельца №5) — под текстом ТОЛЬКО дата
            # решения (`rejected_at`, план 30-05 задача 3).
            "reason_text": reason,
            "reason_date": applications.format_decision_date(user.get("rejected_at")),
            "fix_eyebrow": await get_setting_typed("reg_status_fix_eyebrow_text"),
            # Список проблемных полей — нет источника данных (менеджер пишет причину свободным
            # текстом, структурной разметки «какое поле не так» проект не ведёт): пусто, пока
            # такая функциональность не появится отдельным планом (см. SUMMARY, Known Stubs).
            "fix_fields": None,
            "saved_answers_label": await get_setting_typed("reg_status_saved_answers_label_text"),
            "resubmit_button_text": await get_setting_typed("reg_status_resubmit_button_text"),
            "tile_text": await get_setting_typed("reg_status_tile_rejected_text"),
        } if status_screen_on else _STATUS_SCREEN_EXTRAS_OFF
        return {
            "status": status, "heading": heading, "body": body, "days": None,
            "cta_text": cta_text, "event_dates": event_dates, "event_place": event_place,
            "reason_line": reason_line, "status_screen_enabled": status_screen_on,
            "haptics_enabled": haptics_on, **extra,
        }
    if status == "approved" and status_screen_on:
        payment = await _payment_card(user)
        extra = {
            "badge": await get_setting_typed("reg_status_approved_badge_text"),
            "title": (await get_setting_typed("reg_status_approved_title_text") or "").replace(
                "{имя}", user.get("full_name") or "",
            ),
            "screen_body": (await get_setting_typed("reg_status_approved_body_text") or "")
                .replace("{дата}", event_dates or "").replace("{город}", event_place or ""),
            "next_steps_eyebrow": await get_setting_typed("reg_status_next_eyebrow_text"),
            "next_steps": await _next_steps("approved"),
            "edit_button_text": None,
            "payment": payment,
            "pay_button_text": await get_setting_typed("reg_status_pay_button_text") if payment else None,
            "reason_eyebrow": None, "reason_text": None, "reason_date": None,
            "fix_eyebrow": None, "fix_fields": None, "saved_answers_label": None,
            "resubmit_button_text": None,
            "tile_text": (await get_setting_typed("reg_status_tile_approved_text") or "").replace(
                "{дата}", payment["due_date"] if payment else "",
            ),
        }
        return {
            "status": status, "heading": None, "body": None, "days": None,
            "cta_text": None, "event_dates": event_dates, "event_place": event_place,
            "reason_line": None, "status_screen_enabled": True,
            "haptics_enabled": haptics_on, **extra,
        }
    return {
        "status": status, "heading": None, "body": None, "days": None,
        "cta_text": None, "event_dates": event_dates, "event_place": event_place,
        "reason_line": None, "status_screen_enabled": status_screen_on,
        "haptics_enabled": haptics_on, **_STATUS_SCREEN_EXTRAS_OFF,
    }
