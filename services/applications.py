"""Ядро отбора заявок — общее для бота и веб-слоя Mini App, БЕЗ aiogram.

Перенесено из `handlers/admin_moderation.py` byte-for-byte (Phase 23, план 23-02, D-06/D-08):
`_EDITED_SOURCE_LABELS` -> `EDITED_SOURCE_LABELS`, `_format_edited_date` -> `format_edited_date`,
`_edit_badges_for` -> `edit_badges_for`, словарь подписей треков из `_render_application_card`
-> модульная константа `TRACK_LABELS`. `admin_moderation.py` импортирует отсюда и держит
модульные алиасы под старыми приватными именами (приём 22-01/settings_ops.py) — тела хендлеров
и их порядок не тронуты (golden-снапшот `tests/test_refac_snapshot_260816.py`).

Причина выноса — `miniapp/` (FastAPI-процесс) не имеет права импортировать aiogram-модуль
(`miniapp/deps.py`: «Модуль aiogram-free»), а `admin_moderation.py` стоит у потолка размера
(`tests/test_module_size_convention_260816.py`). Без этого модуля веб-слой фазы 23 либо тянет
бота целиком, либо заводит вторую копию правил очереди/карточки/решения — ИМЕННО тот «второй
движок», который фаза 22 закрыла для настроек выносом `settings_ops.py`.

Плюс — новая, но на переносимой механике, механика отбора: `manager_scope`/`out_of_scope`
(форма `miniapp/routers/review.py::manager_scope`/`submission_out_of_scope`, но от привязки
менеджера — города, переданного значением, — а не от `Principal` целиком, чтобы не тянуть
`miniapp.deps`), `queue_page`/`card_payload` (очередь и карточка для веба, тот же реестр
`modcard_fields`, что у карточки бота — `moderation_card`, не второй набор вопросов),
`claim_approve`/`claim_reject`/`claim_approve_all` (тонкие обёртки атомарных решений — оба
поверхности зовут одно имя), журнал отмены `record_decision`/`undo_decision`/
`flush_due_decisions` (D-06: решение пишется в `users.status` сразу, эффекты — по истечении
окна `UNDO_WINDOW_SECONDS`), `reject_message_text`/`reject_reason_templates` (D-05, единственная
точка правды по тексту отказа делегату).

Зависимости — ТОЛЬКО `database.db`, `cities`, `moderation_card`, `settings_schema`,
`services.consent` (плюс стандартная библиотека). Ни `aiogram`, ни `handlers.*`, ни
`miniapp.*` на уровне модуля не импортируются (сторож
`tests/test_applications_parity.py::test_applications_module_does_not_load_aiogram`,
приём `tests/test_miniapp_labels_drift.py::_loaded_aiogram`).
"""
from __future__ import annotations

import html as html_module
from datetime import datetime, timedelta

import moderation_card
from cities import cities_module_on, city_scope, normalize_city
from database.db import (
    approve_all_pending,
    approve_user_atomic,
    claim_application_undo,
    claim_due_application_decisions,
    get_answer_history,
    get_application_decision,
    get_last_application_decision,
    get_pending_count,
    get_pending_users,
    get_setting,
    get_user,
    record_application_decision,
    reject_user,
    revert_user_to_pending,
)
from reg_engine import STEP_TO_COLUMN, label_for
from services.consent import consent_card_line
from services.timeutil import utc_naive_to_msk
from settings_schema import get_setting_typed

# Phase 21 (21-07, D-14): edited_source — служебный литерал ('bot'|'miniapp', см.
# database.db.mark_user_edited) — CLAUDE.md запрещает показывать код менеджеру, это ЕДИНСТВЕННОЕ
# место, которое превращает его в слова для карточки заявки / экрана истории.
EDITED_SOURCE_LABELS = {"miniapp": "в приложении", "bot": "в чате"}

# Plan 23-06 (Known Stub #1 из 23-05): column (users row) -> человеческая подпись, дословно
# перенесено из `handlers/admin_moderation.py::_COLUMN_TO_LABEL` (та же формула через
# `reg_engine.label_for` в обратную сторону от `STEP_TO_COLUMN`) — единственная точка правды,
# бот теперь импортирует её отсюда, а не строит вторую копию (T-23-28: второй источник правды
# рано или поздно разъедется). Карточка веба использует её же для `history[].changes[].label`.
COLUMN_TO_LABEL: dict[str, str] = {col: label_for(step) for step, col in STEP_TO_COLUMN.items()}

# Quick 260902-tzh / Phase 05 (D-14): подписи треков — та же общая карточка заявки (бот и веб).
# D-11 (23-CONTEXT): подписи остаются литералами здесь (паритет с ботом); перенос в реестр —
# follow-up после фазы. `full` подписи не имеет (карточка вообще не печатает строку трека).
TRACK_LABELS: dict[str, str] = {
    "party_overnight": "🎉 Трек: вечеринка с ночёвкой",
    "party_noovernight": "🎉 Трек: вечеринка без ночёвки",
    "short": "⚡ Трек: краткая анкета (акция)",
}

# Phase 23 (D-08): чип фильтра очереди -> коды `participant_type`, которые он матчит — то же
# деление, что `database.db._track_clause` делает в SQL (T-23-04/T-23-05: один источник правды
# по кодам трека на стороне сервиса, `full` дополнительно матчит NULL — это делает уже сам SQL,
# здесь достаточно "full"). Сторож дрейфа против `reg_engine.PARTY_TRACK_CODES`/`SHORT_TRACK`
# лежит в `tests/test_applications_parity.py` (T-23-05, приём, оставленный планом 23-01).
TRACK_FILTERS: dict[str, tuple[str, ...]] = {
    "full": ("full",),
    "party": ("party_overnight", "party_noovernight"),
    "short": ("short",),
}

# D-12 (23-CONTEXT): окно отмены — 5 секунд, UX-константа, а не настройка менеджера. В реестр
# сознательно не выносим — «Отменить» тонет, если менеджер будет искать это в настройках.
UNDO_WINDOW_SECONDS = 5


def format_edited_date(raw: str | None, *, stored_utc: bool = False) -> str:
    """'2026-09-03 14:12:00' -> '03.09 14:12' (формат подстановки {date} в reg_edited_admin_label).
    Нераспознанный формат — печатаем как есть, а не роняем карточку заявки.

    Quick 260906-52m: `stored_utc` — какое время лежит в поле, которое сюда передали.

        Поле                    | Пишется          | stored_utc
        -------------------------|------------------|------------
        edited_at                | datetime.now()   | False (по умолчанию)
        registration_date        | datetime.now()   | False (по умолчанию)
        approved_at              | datetime.now()   | False (по умолчанию)
        reg_answer_history.changed_at | datetime.utcnow() | True

    По умолчанию `False` — три из четырёх вызывающих (`edit_badges_for` для `edited_at`,
    `miniapp/routers/profile.py` для `registration_date` и `approved_at`) обязаны остаться
    байт-в-байт прежними: эти поля пишутся локальным временем контейнера, сдвигать их в МСК
    было бы новым багом, а не фиксом. `stored_utc=True` используют только вызывающие
    `changed_at` (`_history_entry` здесь и `appr_history` в `handlers/admin_moderation.py`) —
    им поле приходит в UTC. Ветки «пусто» и «не разобралось» флаг не трогает — fail-soft
    остаётся как был."""
    if not raw:
        return ""
    try:
        stamp = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(raw)
    if stored_utc:
        stamp = utc_naive_to_msk(stamp)
    return stamp.strftime("%d.%m %H:%M")


async def edit_badges_for(user: dict) -> tuple[str | None, str | None, bool]:
    """(edited_line, resubmit_line, has_history) для карточки заявки (D-14/D-15/D-10).
    Пустой `edited_at` -> ничего нет (пометка ставится только при непустом diff — уже гарантия
    reg_engine.diff/mark_user_edited, здесь просто читаем факт). `has_history` — по ОДНОЙ и той
    же выборке get_answer_history(limit=1), которую переиспользует и признак повторной подачи,
    так что карточка не делает лишний запрос ради одного только флага кнопки."""
    edited_at = user.get("edited_at")
    if not edited_at:
        return None, None, False
    tid = user.get("telegram_id")
    history = await get_answer_history(tid, limit=1) if tid is not None else []
    has_history = bool(history)
    tmpl = await get_setting("reg_edited_admin_label") or "✏️ Изменена {date}"
    edited_line = tmpl.replace("{date}", format_edited_date(edited_at))
    src_label = EDITED_SOURCE_LABELS.get(user.get("edited_source"))
    if src_label:
        edited_line = f"{edited_line} ({src_label})"
    resubmit_line = None
    if history:
        changes = history[0].get("changes") or []
        # план 21-08 отмечает повторную подачу отклонённой анкеты отдельной записью
        # {"column": "status", "old": "rejected", "new": "pending"} в ТОЙ ЖЕ выборке.
        was_resubmit = any(c.get("column") == "status" and c.get("old") == "rejected" for c in changes)
        if was_resubmit:
            resubmit_line = await get_setting("reg_resubmit_admin_label") or "🔁 Повторная подача"
    return edited_line, resubmit_line, has_history


async def prev_reject_line(user: dict, *, escape_reason: bool = False) -> str | None:
    """Quick 260904-liz: «🚫 Ранее отклонена: <причина>» для карточки повторно поданной заявки
    — читает `last_rejection_reason` по `user["telegram_id"]` (единый аксессор с экраном
    делегата, `miniapp/routers/hub.py::hub_status`). НЕ расширяет кортеж `edit_badges_for` —
    у той функции два вызывающих и сторожа паритета (T-23-28), отдельная функция дешевле, чем
    менять форму существующего контракта.

    `escape_reason=True` (карточка бота) — причина проходит `html_module.escape` ПЕРЕД
    подстановкой в шаблон: шаблон `reg_prev_reject_admin_label` — текст реестра менеджера, его
    экранировать нельзя (карточка бота печатает готовые строки как есть, тот же контракт, что
    у `edited_line`/`resubmit_line`). Карточка веба (`card_payload`, `escape_reason=False` по
    умолчанию) несёт ПЛОСКИЙ текст — экранирует только бот.

    Нет `telegram_id` или нет причины (её и не было, отказ отменён, или последнее решение —
    одобрение) -> None (бейджа/строки нет вовсе); пустой шаблон реестра — тот же дефолт-фолбэк
    в коде, что у `edit_badges_for` выше."""
    tid = user.get("telegram_id")
    if tid is None:
        return None
    reason = await last_rejection_reason(tid)
    if not reason:
        return None
    tmpl = await get_setting("reg_prev_reject_admin_label") or "🚫 Ранее отклонена: {reason}"
    shown_reason = html_module.escape(reason) if escape_reason else reason
    return tmpl.replace("{reason}", shown_reason)


def _history_changes(raw_changes: list[dict] | None) -> list[dict]:
    """Одна запись `reg_answer_history.changes` -> `[{label, old, new}]` для «было → стало»
    (D-03, Known Stub #1 из 23-05). Дословно правило `handlers/admin_moderation.py::appr_history`:
    служебный маркер повторной подачи (`column == "status"`) не поле анкеты — пропускается (D-10,
    уже показан отдельным бейджем `resubmit`); колонка без подписи в `COLUMN_TO_LABEL` тоже
    пропускается — показать код менеджеру вместо слова запрещает CLAUDE.md, а не показать одну
    строку истории безопаснее второй копии словаря переводов в JS."""
    out: list[dict] = []
    for ch in raw_changes or []:
        column = ch.get("column")
        if column == "status":
            continue
        label = COLUMN_TO_LABEL.get(column)
        if not label:
            continue
        old = ch.get("old")
        new = ch.get("new")
        out.append({
            "label": label,
            "old": old if old not in (None, "") else None,
            "new": new if new not in (None, "") else None,
        })
    return out


def _history_entry(row: dict) -> dict | None:
    """Одна строка `get_answer_history` -> `{when, source_label, changes}` для карточки веба.
    `None`, если после фильтра `_history_changes` не осталось ни одной строки (та же строка
    бота в этом случае тоже не печатает ничего — запись с одним только маркером `status`)."""
    changes = _history_changes(row.get("changes"))
    if not changes:
        return None
    return {
        "when": format_edited_date(row.get("changed_at"), stored_utc=True),
        "source_label": EDITED_SOURCE_LABELS.get(row.get("source"), row.get("source") or ""),
        "changes": changes,
    }


# ── Скоуп менеджера (D-08, форма miniapp/routers/review.py::manager_scope) ──────────────────

async def manager_scope(city: str | None):
    """`city_scope` для SQL-очереди заявок: модуль городов выключен или привязки нет -> None
    (без фильтра). `city` — уже резолвленная привязка вызывающей стороны (`Principal.city` у
    веба, выбор админа у бота), сервис намеренно не знает про `Principal`."""
    if not await cities_module_on() or city is None:
        return None
    return city_scope(normalize_city(city))


async def out_of_scope(city: str | None, telegram_id: int) -> bool:
    """Дословное правило `handlers/admin_core.py::_card_out_of_scope`, но от переданной
    привязки менеджера, а не от `admin_id`: модуль городов выключен или привязки нет -> False."""
    if not await cities_module_on() or city is None:
        return False
    user = await get_user(telegram_id)
    return normalize_city((user or {}).get("event_city")) != normalize_city(city)


# ── Очередь (D-08) ────────────────────────────────────────────────────────────────────────

async def queue_page(*, scope, offset: int = 0, track: str | None = None,
                      changed_only: bool = False) -> tuple[dict | None, int]:
    """Одна карточка очереди + общий счётчик — СЧЁТЧИК И ВЫБОРКА идут по ОДНОМУ набору
    фильтров (T-23-04), иначе «Осталось: N» врёт. `None` в первом элементе — очередь пуста
    на этом offset (конец очереди или пустой скоуп)."""
    total = await get_pending_count(city_scope=scope, track=track, changed_only=changed_only)
    if total == 0 or offset >= total:
        return None, total
    rows = await get_pending_users(
        limit=1, offset=offset, city_scope=scope, track=track, changed_only=changed_only,
    )
    return (rows[0] if rows else None), total


# ── Карточка (D-01/D-02/D-03) ────────────────────────────────────────────────────────────

async def card_payload(user: dict) -> dict:
    """Карточка заявки для веба. Значения — ЧИСТЫЙ текст (HTML-экранирование делает только
    карточка бота, JSON его не несёт — тот же принцип, что `settings_ops.plain_text`).

    `main_fields` — ответы по `moderation_card.enabled_steps(modcard_fields)` (минус шаг
    `resume` — у него свой блок), обрезка по `modcard_answer_limit`.
    `extra_fields` — ВСЕ остальные непустые ответы `moderation_card.CARD_STEPS` (минус то же
    `resume`) в порядке анкеты, БЕЗ обрезки — «Показать всё» (D-01); не пересекается с
    `main_fields`.
    `badges` — `[{kind, text}]`: трек (`TRACK_LABELS`), «🔁 Повторный …» (`prev_season`, тот же
    разбор служебного литерала `legacy`, что и у карточки бота), `edited`/`resubmit`
    (`edit_badges_for`), `prev_reject` — «🚫 Ранее отклонена: <причина>» (`prev_reject_line`,
    quick 260904-liz), ПОСЛЕ `resubmit` и ПЕРЕД `consent` — строка согласия ВСЕГДА последней.
    `resume` — `{kind: "file"|"text"|"none", file_id, text}`.
    `history` — до 5 записей `get_answer_history` (D-03), КАЖДАЯ уже переведена в
    `{when, source_label, changes:[{label, old, new}]}` — `_history_entry` (найдено планом
    23-05 как Known Stub: до 23-06 фронт получал сырые `column`/`source` литералы).
    `show_resume` — включён ли шаг `resume` в наборе полей карточки."""
    steps = moderation_card.enabled_steps(await get_setting_typed("modcard_fields"))
    answer_limit = await get_setting_typed("modcard_answer_limit")
    enabled_set = set(steps)
    main_steps = [s for s in steps if s != "resume"]
    main_fields = moderation_card.card_answers(user, main_steps, answer_limit)
    extra_steps = [s for s in moderation_card.CARD_STEPS if s not in enabled_set and s != "resume"]
    extra_fields = moderation_card.card_answers(user, extra_steps, None)

    badges: list[dict] = []
    track = user.get("participant_type") or "full"
    if track != "full":
        track_text = TRACK_LABELS.get(track, f"🎉 Трек: {track}")
        badges.append({"kind": "track", "text": track_text})
    prev_season_raw = (user.get("prev_season") or "").strip()
    if prev_season_raw:
        if prev_season_raw == "legacy":
            badges.append({"kind": "prev_season", "text": "🔁 Повторный: был(а) на прошлом событии"})
        else:
            badges.append({"kind": "prev_season", "text": f"🔁 Повторный: был(а) в {prev_season_raw}"})
    edited_line, resubmit_line, _has_history = await edit_badges_for(user)
    if edited_line:
        badges.append({"kind": "edited", "text": edited_line})
    if resubmit_line:
        badges.append({"kind": "resubmit", "text": resubmit_line})
    prev = await prev_reject_line(user)
    if prev:
        badges.append({"kind": "prev_reject", "text": prev})
    consent_line = await consent_card_line(user.get("telegram_id"))
    if consent_line:
        badges.append({"kind": "consent", "text": consent_line})

    if user.get("resume_file_id"):
        resume = {"kind": "file", "file_id": user["resume_file_id"], "text": None}
    elif user.get("resume_text"):
        resume = {"kind": "text", "file_id": None, "text": user["resume_text"]}
    else:
        resume = {"kind": "none", "file_id": None, "text": None}

    tid = user.get("telegram_id")
    history_raw = await get_answer_history(tid) if tid is not None else []
    history = [entry for entry in (_history_entry(row) for row in history_raw) if entry is not None]

    return {
        "main_fields": main_fields,
        "extra_fields": extra_fields,
        "badges": badges,
        "resume": resume,
        "history": history,
        "show_resume": "resume" in enabled_set,
    }


# ── Решения (D-06/D-07): тонкие обёртки над атомарными UPDATE ───────────────────────────────
#
# D-10 (23.1-CONTEXT.md O-2): `users.approved_at` стамповать здесь, вторым запросом, было бы
# неверно — `database.db.approve_user_atomic`/`approve_all_pending` уже пишут его в ТОЙ ЖЕ
# атомарной `UPDATE`, что и `status` (одна запись, не гонка approve/read). Ниже — тонкие
# обёртки, approved_at уже приехал вместе со status.

async def claim_approve(telegram_id: int) -> bool:
    """Правило «выигрывает ровно один» — одно имя для бота и веба. approved_at ставит
    `approve_user_atomic` в той же атомарной записи, что и status (D-10)."""
    return await approve_user_atomic(telegram_id)


async def claim_reject(telegram_id: int) -> bool:
    return await reject_user(telegram_id)


async def claim_approve_all(scope) -> list[int]:
    """«Принять всех» веб-слоя. approved_at ставит `approve_all_pending` в той же атомарной
    записи, что и status (D-10). Бот зовёт `approve_all_pending` НАПРЯМУЮ для своего «Принять
    всех» (`handlers/admin_moderation.py::appr_all_yes`), минуя эту обёртку — approved_at всё
    равно проставляется, т.к. живёт в database.db, а не здесь."""
    return await approve_all_pending(city_scope=scope)


# ── Журнал отмены (D-06) ─────────────────────────────────────────────────────────────────

def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def record_decision(telegram_id: int, decision: str, reason: str | None, by: int,
                           now: datetime, *, effects_already_sent: bool = False) -> int:
    """Пишет решение в журнал; `effects_due_at = now + UNDO_WINDOW_SECONDS` — эффекты
    (приветствие/отказ/лист) остаются отложенными до истечения окна отмены.

    `effects_already_sent` (quick 260904-liz, keyword-only) — для бот-пути
    (`handlers/admin_moderation.py::appr_reject_reason`/`appr_approve`): бот применяет эффекты
    СИНХРОННО и САМ (`_spawn(apply_decision_effects(...))`), ДО вызова этой функции, поэтому
    строка журнала обязана родиться УЖЕ помеченной отправленной (`effects_sent_at = _stamp(now)`)
    — живая строка (`effects_sent_at IS NULL`) означала бы, что `claim_due_application_decisions`
    рано или поздно заберёт её и один из двух сметателей (`miniapp/outbox.py::
    flush_application_decisions` в веб-процессе или `services/scheduler.py::
    _flush_due_application_decisions` в бот-процессе — сметателей ДВА, оба слушают одну и ту же
    таблицу) отправит делегату сообщение об отказе/приветствие ВТОРОЙ раз. `effects_due_at`
    в этом случае тоже ставится в `_stamp(now)` — значение уже не имеет смысла (эффекты не ждут
    его), но колонка NOT NULL. Журнал бот-пути пишется РАДИ ИСТОРИИ (чтобы
    `last_rejection_reason` видел причину независимо от того, кто принял решение), не ради
    доставки — доставка уже случилась синхронно, до этого вызова."""
    decided_at = _stamp(now)
    if effects_already_sent:
        sent_at = _stamp(now)
        return await record_application_decision(
            telegram_id, decision, reason, by, decided_at, sent_at, effects_sent_at=sent_at,
        )
    effects_due_at = _stamp(now + timedelta(seconds=UNDO_WINDOW_SECONDS))
    return await record_application_decision(telegram_id, decision, reason, by, decided_at, effects_due_at)


async def get_decision(decision_id: int) -> dict | None:
    """Read-only lookup — плана 23-04, ownership/state проверка ДО `undo_decision` (T-23-17):
    веб-роутер обязан отказать чужому/уже разрешённому `decision_id`, не потратив claim."""
    return await get_application_decision(decision_id)


async def last_rejection_reason(telegram_id: int) -> str | None:
    """Quick 260904-liz: причина ПОСЛЕДНЕГО НЕ отменённого отказа делегата — обёртка над
    `get_last_application_decision`. `None`, если: отказов не было, последнее решение —
    одобрение, у отказа пустая причина (`"-"` менеджер вводит явно за «без причины», но здесь
    достаточно общей проверки `.strip()`), или последний отказ отменён (уже отфильтровано SQL
    в `get_last_application_decision`). Единая точка правды и для экрана делегата
    (`miniapp/routers/hub.py::hub_status`), и для карточки менеджера
    (`prev_reject_line` ниже) — обе поверхности не должны разъезжаться в трактовке «была ли
    причина»."""
    row = await get_last_application_decision(telegram_id)
    if row is None or row.get("decision") != "rejected":
        return None
    reason = (row.get("reason") or "").strip()
    return reason or None


async def undo_decision(decision_id: int) -> dict:
    """Заявляет отмену (`claim_application_undo`), затем откатывает `users.status`
    (`revert_user_to_pending`). Откат не сработал (или отмену уже заявили/эффекты уже ушли) —
    `{"ok": False, "reason": "already"}`: решение считается состоявшимся, второй попытки нет."""
    claimed = await claim_application_undo(decision_id)
    if claimed is None:
        return {"ok": False, "reason": "already"}
    telegram_id = claimed["telegram_id"]
    from_status = claimed["decision"]
    reverted = await revert_user_to_pending(telegram_id, from_status)
    if not reverted:
        return {"ok": False, "reason": "already"}
    return {"ok": True, "telegram_id": telegram_id}


async def flush_due_decisions(now: datetime, enqueue) -> int:
    """Забирает все просроченные живые решения (`claim_due_application_decisions`) и на каждую
    выигранную строку зовёт переданный `enqueue(kind, payload)`. Колбэк ВНЕДРЯЕТСЯ параметром —
    модуль остаётся свободен и от `miniapp.outbox`, и от `services.*`-цикла очереди."""
    due = await claim_due_application_decisions(_stamp(now))
    for row in due:
        enqueue(row["decision"], row)
    return len(due)


# ── Текст отказа (D-05) ──────────────────────────────────────────────────────────────────

async def reject_message_text(reason: str | None) -> str:
    """Единственная точка правды по тексту отказа делегату: `reject_text`-префикс (или
    дефолт) + `\\n\\n` + причина, экранирование — как в боте. Пустая/отсутствующая причина —
    только префикс, без хвоста."""
    prefix = await get_setting("reject_text") or "К сожалению, твоя заявка отклонена."
    text = html_module.escape(prefix)
    if reason:
        text = f"{text}\n\n{html_module.escape(reason)}"
    return text


async def reject_reason_templates() -> list[str]:
    """Шаблоны причин отказа для шторки Mini App (D-05) — ключ реестра `reject_reason_templates`."""
    return await get_setting_typed("reject_reason_templates") or []
