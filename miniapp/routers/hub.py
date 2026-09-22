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
from database.db import get_referrals, get_setting, get_user, settings_snapshot
from payment_options import parse_options
import reg_engine
from services import applications, i18n, reg_edit_policy
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


async def _next_steps(prefix: str, lang: str, tr_map: dict[str, str]) -> list[dict]:
    """Три шага «Что дальше» одного состояния — `reg_status_{prefix}_step{1..3}_title_text`/
    `_body_text`, тот же реестровый шаблон для review/approved (30-UI-SPEC.md таблицы)."""
    steps = []
    for i in (1, 2, 3):
        title = await i18n.tr_setting(f"reg_status_{prefix}_step{i}_title_text", lang, tr_map)
        body = await i18n.tr_setting(f"reg_status_{prefix}_step{i}_body_text", lang, tr_map)
        if title or body:
            steps.append({"title": title, "body": body})
    return steps


async def _payment_card(user: dict, lang: str, tr_map: dict[str, str]) -> dict | None:
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
    due_label_tpl = await i18n.tr_setting("reg_status_payment_due_label_text", lang, tr_map)
    return {
        "amount": amount,
        "due_date": due_date,
        "due_label": due_label_tpl.replace("{дата}", due_date) if due_label_tpl else None,
        "reminder_note": await i18n.tr_setting("reg_status_payment_reminder_note_text", lang, tr_map),
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
async def _referral_block(
    telegram_id: int, event_city: str | None, bot_username: str | None,
    lang: str = "ru", tr_map: dict | None = None,
) -> dict | None:
    if await get_setting_typed_for_city("menu_referral", event_city) != "on":
        return None
    if not bot_username:
        return None
    invites_text = None
    if await get_setting_typed_for_city("menu_invites", event_city) == "on":
        count = len(await get_referrals(telegram_id))
        invites_tpl = await i18n.tr_setting("miniapp_hub_referral_invites_text", lang, tr_map or {})
        invites_text = invites_tpl.format(count=count) if invites_tpl else None
    return {
        "label": await i18n.tr_setting("miniapp_hub_referral_label_text", lang, tr_map or {}),
        "link": reg_engine.build_referral_link(bot_username, telegram_id),
        "copy_button": await i18n.tr_setting("miniapp_form_ambassador_copy_button_text", lang, tr_map or {}),
        "copied_toast": await i18n.tr_setting("miniapp_form_ambassador_copied_toast_text", lang, tr_map or {}),
        "invites_text": invites_text,
    }


@router.get("/app/api/hub")
async def hub(request: Request, p: Principal = Depends(delegate_gate)) -> dict:
    # Perf (замер 260917): ~12 последовательных get_setting_typed на один ответ — снимок
    # bot_settings на весь рендер (тот же приём, что у settings_all/_draft_response); ни
    # `tasks_progress`, ни `count_participants`, ни `get_referrals` не порождают create_task.
    async with settings_snapshot():
        return await _hub_impl(request, p)


async def _hub_impl(request: Request, p: Principal) -> dict:
    user = await get_user(p.telegram_id)
    event_city = user.get("event_city") if user else None
    lang, tr_map = await i18n.context(p.telegram_id)
    lang = lang if lang in ("ru", "en") else "ru"

    done, total = await tasks_progress(p.telegram_id, await delegate_city_scope(p.telegram_id))
    tasks_fact_text = await i18n.tr_setting("miniapp_hub_tasks_fact_text", lang, tr_map)
    tasks_fact = tasks_fact_text.format(done=done, total=total) if tasks_fact_text else None

    countdown_date = await get_setting_typed_for_city("miniapp_hub_countdown_date", event_city)
    days = _days_until(countdown_date)
    days_fact_text = await i18n.tr_setting("miniapp_hub_days_fact_text", lang, tr_map)
    days_fact = days_fact_text.format(days=days) if (days is not None and days_fact_text) else None

    event_dates = await i18n.tr_setting_for_city("event_date", event_city, lang, tr_map) or None
    event_place = await i18n.tr_setting_for_city("event_place_name", event_city, lang, tr_map) or None

    # Плита списочного экрана «Рейтинг» (план 23.1-06): «из {total}» подставляется здесь —
    # число участников известно ручке (тот же count_participants, что у /coins/balance и
    # /leaderboard), отдавать шаблон с недоставленной подстановкой нельзя.
    rank_unit_text = await i18n.tr_setting("miniapp_leaderboard_plate_unit", lang, tr_map)
    total_participants = await count_participants()
    rank_unit = rank_unit_text.format(total=total_participants) if rank_unit_text else None

    referral = await _referral_block(p.telegram_id, event_city, request.app.state.cfg.bot_username, lang, tr_map)

    return {
        "balance_eyebrow": await i18n.tr_setting("miniapp_hub_balance_eyebrow", lang, tr_map),
        "balance_unit": await i18n.tr_setting("miniapp_hub_balance_unit", lang, tr_map),
        "next_eyebrow": await i18n.tr_setting("miniapp_hub_next_eyebrow", lang, tr_map),
        "sections_eyebrow": await i18n.tr_setting("miniapp_hub_sections_eyebrow", lang, tr_map),
        "tasks_fact": tasks_fact,
        "days_fact": days_fact,
        "event_dates": event_dates,
        "event_place": event_place,
        "tasks_eyebrow": await i18n.tr_setting("miniapp_tasks_plate_eyebrow", lang, tr_map),
        "rank_eyebrow": await i18n.tr_setting("miniapp_leaderboard_plate_eyebrow", lang, tr_map),
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
    lang, tr_map = await i18n.context(p.telegram_id)
    lang = lang if lang in ("ru", "en") else "ru"

    event_dates = await i18n.tr_setting_for_city("event_date", event_city, lang, tr_map) or None
    event_place = await i18n.tr_setting_for_city("event_place_name", event_city, lang, tr_map) or None
    # Phase 30 (30-05, задача 3): выключенный тумблер -> ниже опубликованные поля пустые
    # (`status_screen_enabled: False`), клиент рисует ТОЛЬКО старые поля — сегодняшнее
    # поведение обеих поверхностей (плита хаба, экрана #/status ещё нет) без изменений.
    status_screen_on = await get_setting_typed_for_city("reg_form_status_screen", event_city) == "on"
    # Тумблер вибрации анкеты (30-03) — независим от status_screen: тап по плите хаба обязан
    # молчать при выключенной вибрации, тем же способом, что form_types.js::haptic (свой
    # локальный хелпер на файл, второй общий мотор не заводим).
    haptics_on = await get_setting_typed("reg_form_haptics") == "on"

    # Квик 260922-wrg (задача 2, B-2): ДО веток по статусу — делегат прошлого сезона (любой
    # статус) возвращенец, а не «Одобрена»/«Отклонена» — та же граница, что form_status
    # (`miniapp/deps.py`), read_engine.is_past_season_row, не is_returning_row (B-3: отклонённый
    # ТЕКУЩЕГО сезона обязан остаться в обычной ветке rejected ниже, с причиной отказа).
    # status_screen_enabled=False — «расширенного» экрана статуса для возвращенца нет, плита
    # ведёт делегата прямо на #/form (тот же приём, что rejected без status_screen_enabled,
    # см. renderTilesOnlyHub/renderDelegateHub в hub.js).
    event_season = await get_setting_typed("event_season") or None
    if reg_engine.is_past_season_row(user, event_season):
        prev_label = (user.get("season") or "").strip() or "прошлом событии"
        heading_tpl = await i18n.tr_setting("start_text_returning", lang, tr_map)
        heading = heading_tpl.replace("{season}", prev_label) if heading_tpl else None
        cta_text = await i18n.tr_setting("start_returning_cta_text", lang, tr_map)
        return {
            "status": "returning", "heading": heading, "body": None, "days": None,
            "cta_text": cta_text, "event_dates": event_dates, "event_place": event_place,
            "reason_line": None, "status_screen_enabled": False,
            "haptics_enabled": haptics_on, **_STATUS_SCREEN_EXTRAS_OFF,
            "tile_text": heading,
        }

    if status == "pending":
        heading = await i18n.tr_setting("miniapp_hub_pending_heading_text", lang, tr_map)
        body_tpl = await i18n.tr_setting("miniapp_hub_pending_body_text", lang, tr_map)
        days = await get_setting_typed_for_city("miniapp_hub_pending_days", event_city)
        # Та же подстановка, что applications.py::applications_next делает для «{count}» —
        # `.format()` уронил бы ручку, если менеджер случайно сотрёт фигурные скобки в тексте.
        body = body_tpl.replace("{days}", str(days)) if body_tpl else None
        extra = {
            "badge": await i18n.tr_setting("reg_status_review_badge_text", lang, tr_map),
            "title": await i18n.tr_setting("reg_status_review_title_text", lang, tr_map),
            "screen_body": await i18n.tr_setting("reg_status_review_body_text", lang, tr_map),
            "next_steps_eyebrow": await i18n.tr_setting("reg_status_next_eyebrow_text", lang, tr_map),
            "next_steps": await _next_steps("review", lang, tr_map),
            "edit_button_text": await i18n.tr_setting("reg_status_edit_button_text", lang, tr_map),
            "payment": None, "pay_button_text": None,
            "reason_eyebrow": None, "reason_text": None, "reason_date": None,
            "fix_eyebrow": None, "fix_fields": None, "saved_answers_label": None,
            "resubmit_button_text": None,
            "tile_text": await i18n.tr_setting("reg_status_tile_review_text", lang, tr_map),
        } if status_screen_on else _STATUS_SCREEN_EXTRAS_OFF
        return {
            "status": status, "heading": heading, "body": body, "days": days,
            "cta_text": None, "event_dates": event_dates, "event_place": event_place,
            "reason_line": None, "status_screen_enabled": status_screen_on,
            "haptics_enabled": haptics_on, **extra,
        }
    if status == "rejected":
        heading = await i18n.tr_setting("miniapp_hub_rejected_heading_text", lang, tr_map)
        body = await i18n.tr_setting("miniapp_hub_rejected_body_text", lang, tr_map)
        cta_text = await i18n.tr_setting("miniapp_hub_rejected_cta_text", lang, tr_map)
        # Quick 260904-liz: причина последнего НЕ отменённого отказа — `last_rejection_reason`
        # читает СТРОГО по `p.telegram_id` из `form_gate` (T-liz-02: чужую причину узнать
        # нельзя, параметра для этого в ручке нет). `.replace`, не `.format` — та же защита от
        # стёртых менеджером фигурных скобок, что и у `body` выше; строки нет вовсе, если нет
        # либо причины (старый отказ/отказ без причины), либо самого шаблона. Сама причина —
        # свободный текст менеджера (не в реестре) — machine-переводу не подвергается, тот же
        # fail-soft, что у остальных manager-written строк вне корпуса анкеты (D-04).
        reason = await applications.last_rejection_reason(p.telegram_id)
        reason_tpl = await i18n.tr_setting("miniapp_hub_rejected_reason_text", lang, tr_map)
        reason_line = reason_tpl.replace("{reason}", reason) if (reason and reason_tpl) else None
        # Квик 260922-wrg (задача 2, A-5): «нельзя» (reg_resubmit_after_reject=deny) у
        # отклонённого ТЕКУЩЕГО сезона (мы уже здесь — возвращенец прошлого сезона отфильтрован
        # веткой is_past_season_row выше) убирает ОБЕ кнопки повторной подачи — причина отказа и
        # остальные поля остаются как есть, делегат по-прежнему видит, почему его отклонили.
        resubmit_ok, _ = await reg_edit_policy.resubmit_gate(user)
        if not resubmit_ok:
            cta_text = None
        extra = {
            "badge": await i18n.tr_setting("reg_status_rejected_badge_text", lang, tr_map),
            "title": await i18n.tr_setting("reg_status_rejected_title_text", lang, tr_map),
            "screen_body": await i18n.tr_setting("reg_status_rejected_body_text", lang, tr_map),
            "next_steps_eyebrow": None, "next_steps": [],
            "edit_button_text": None, "payment": None, "pay_button_text": None,
            "reason_eyebrow": await i18n.tr_setting("reg_status_reason_eyebrow_text", lang, tr_map),
            # T-30-11/T-30-12 (threat register): причина строго СВОЯ (та же `last_rejection_
            # reason(p.telegram_id)`, что и `reason_line` выше — второй запрос не заводим), без
            # имени менеджера (30-CONTEXT.md решение владельца №5) — под текстом ТОЛЬКО дата
            # решения (`rejected_at`, план 30-05 задача 3).
            "reason_text": reason,
            "reason_date": applications.format_decision_date(user.get("rejected_at")),
            "fix_eyebrow": await i18n.tr_setting("reg_status_fix_eyebrow_text", lang, tr_map),
            # Список проблемных полей — нет источника данных (менеджер пишет причину свободным
            # текстом, структурной разметки «какое поле не так» проект не ведёт): пусто, пока
            # такая функциональность не появится отдельным планом (см. SUMMARY, Known Stubs).
            "fix_fields": None,
            "saved_answers_label": await i18n.tr_setting("reg_status_saved_answers_label_text", lang, tr_map),
            "resubmit_button_text": (
                await i18n.tr_setting("reg_status_resubmit_button_text", lang, tr_map) if resubmit_ok else None
            ),
            "tile_text": await i18n.tr_setting("reg_status_tile_rejected_text", lang, tr_map),
        } if status_screen_on else _STATUS_SCREEN_EXTRAS_OFF
        return {
            "status": status, "heading": heading, "body": body, "days": None,
            "cta_text": cta_text, "event_dates": event_dates, "event_place": event_place,
            "reason_line": reason_line, "status_screen_enabled": status_screen_on,
            "haptics_enabled": haptics_on, **extra,
        }
    if status == "approved" and status_screen_on:
        payment = await _payment_card(user, lang, tr_map)
        extra = {
            "badge": await i18n.tr_setting("reg_status_approved_badge_text", lang, tr_map),
            "title": (await i18n.tr_setting("reg_status_approved_title_text", lang, tr_map) or "").replace(
                "{имя}", user.get("full_name") or "",
            ),
            "screen_body": (
                await i18n.tr_setting("reg_status_approved_body_text", lang, tr_map) or ""
            ).replace("{дата}", event_dates or "").replace("{город}", event_place or ""),
            "next_steps_eyebrow": await i18n.tr_setting("reg_status_next_eyebrow_text", lang, tr_map),
            "next_steps": await _next_steps("approved", lang, tr_map),
            "edit_button_text": None,
            "payment": payment,
            "pay_button_text": (
                await i18n.tr_setting("reg_status_pay_button_text", lang, tr_map) if payment else None
            ),
            "reason_eyebrow": None, "reason_text": None, "reason_date": None,
            "fix_eyebrow": None, "fix_fields": None, "saved_answers_label": None,
            "resubmit_button_text": None,
            "tile_text": (
                (await i18n.tr_setting_for_city("reg_status_tile_approved_text", event_city, lang, tr_map) or "").replace(
                    "{дата}", payment["due_date"]
                ) if payment else "Одобрена"
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
