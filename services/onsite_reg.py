"""Регистрация на месте (FORUM-CHECKIN.md D-41): человек, которого нет в базе или который не
одобрен, проходит на форум через стойку проблемных случаев. Решение принимает волонтёр/DXP
одной кнопкой, и оно записано на него.

Два входа:
- walk-in — человека нет в базе: волонтёр показывает QR ссылки `walkin_<город>`, человек
  отвечает в боте на 3 вопроса (`database.db.create_onsite_user`, строка pending без QR), потом
  волонтёр находит его в списке «Ждут на стойке» и одобряет;
- door — заявка есть, но не одобрена (или прошлого сезона): волонтёр одобряет её у стойки.

Оба сходятся в `approve_at_door`: одобрение ОДНОГО человека (`approve_onsite`), запись в журнал
решений (на волонтёра) и в журнал площадки, отметка входа. Массового варианта нет (урок
инцидента 06.09 — тихие массовые одобрения). QR человеку уходит только ПОСЛЕ одобрения (D-02):
`after_onsite_approved` в процессе бота — напрямую из чата или через outbox из Mini App.

Модуль импортирует и процесс Mini App: Google-листы и aiogram — только ленивыми импортами
внутри `after_onsite_approved` (сторож tests/test_sheet_arrival_queue_260925.py)."""
from __future__ import annotations

import io
import logging

import segno

from domain.cities import cities_module_on, city_label, normalize_city, per_city_key
from database import db as _db
from database.db import approve_onsite, get_user
from domain.regform.engine import is_past_season_row
from services.checkin import DENIAL_REASON_TEXT, ENTRY_POINT, checkin_denial, record_arrival
from services.infra.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

# Статус строки в Google-листе после одобрения у стойки — отдельный от «Одобрен», чтобы
# организатор видел в таблице, кого пустили на месте.
ONSITE_SHEET_LABEL = "На месте"
WALKIN_PREFIX = "walkin"
_DECISION_REASON = "Одобрен(а) на месте"
# Волонтёр пропустил вопреки отказу менеджера — отдельная пометка в журнале решений, чтобы
# менеджер видел, чей отказ отменён у стойки и кем.
_OVERRIDE_REASON = "Одобрен(а) на месте вопреки отказу"


async def onsite_enabled(city: str | None) -> bool:
    """D-36: тумблер `onsite_reg_enabled` по городу стойки, дефолт выключен. При включённом
    модуле городов — СТРОГО значение города, без отката на общий ключ (тот же приём, что
    `services.chat_rating_post.enabled_for`): одно общее «вкл» не должно открыть регистрацию на
    месте во всех городах сразу. Без города при включённом модуле — выключено."""
    if await cities_module_on():
        key = per_city_key("onsite_reg_enabled", city) if city else None
        if key is None:
            return False
        return ((await _db.get_setting(key)) or "").strip() == "on"
    return await get_setting_typed("onsite_reg_enabled") == "on"


async def walkin_link(bot_username: str | None, city: str | None) -> str | None:
    """Ссылка короткой анкеты для QR у стойки. Модуль городов выключен — без города. Нет
    юзернейма бота — `None` (вызывающий объясняет волонтёру, что делать)."""
    name = (bot_username or "").strip().lstrip("@")
    if not name:
        return None
    if not await cities_module_on():
        return f"https://t.me/{name}?start={WALKIN_PREFIX}"
    if not city:
        # Ссылка без города при включённом модуле увела бы человека в город по умолчанию.
        return None
    return f"https://t.me/{name}?start={WALKIN_PREFIX}_{normalize_city(city)}"


def walkin_qr_png(link: str) -> bytes:
    """PNG QR-кода ссылки walk-in — тот же масштаб/поля, что у QR делегата (`build_checkin_qr`)."""
    buf = io.BytesIO()
    segno.make(link).save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


def parse_walkin_arg(args: str | None) -> tuple[bool, str | None]:
    """Аргумент /start → (это ссылка walk-in?, код города). «walkin» → (True, None);
    «walkin_<код>» → (True, код); прочее → (False, None). Код не проверяется здесь — вызывающий
    сверяет его со списком включённых городов."""
    if not args:
        return False, None
    if args == WALKIN_PREFIX:
        return True, None
    prefix = f"{WALKIN_PREFIX}_"
    if args.startswith(prefix) and len(args) > len(prefix):
        return True, args[len(prefix):]
    return False, None


async def wrong_city_text(user: dict, lang: str = "ru", tr_map: dict | None = None) -> str:
    """D-26 для стойки — текст из реестра (D-34), `{city}` — город делегата."""
    from services.i18n import tr

    delegate_city = normalize_city(user.get("event_city"))
    label = await city_label(delegate_city) if delegate_city else "—"
    template = tr(await get_setting_typed("onsite_wrong_city_text"), lang, tr_map or {})
    return template.replace("{city}", label)


def is_pending_walkin(user: dict | None) -> bool:
    """Короткая анкета у стойки без решения. Её город — лишь город ссылки, которую человек
    отсканировал (или город по умолчанию у ссылки без кода): стойка другого города может
    одобрить его, переведя в свой город (ревью 28.09)."""
    return bool(user) and user.get("onsite_kind") == "walkin" and user.get("status") == "pending"


def refine_denial(code: str | None, user: dict | None) -> str | None:
    """Код отказа для сканера: `checkin_denial` отдаёт `not_approved` и на «на рассмотрении»,
    и на отказ менеджера. Сканер различает их — отклонённую заявку волонтёр не должен принять за
    ждущую решения (обычной кнопки одобрения у неё нет, только «вопреки отказу»)."""
    if code == "not_approved" and (user or {}).get("status") == "rejected":
        return "rejected"
    return code


async def _stored_reject_reason(user: dict) -> str | None:
    """Причина отказа, если она записана: последний ручной отказ в журнале решений, иначе
    пометка автоотказа. Fail-soft — без причины текст всё равно честный."""
    try:
        from services.applications import last_rejection_reason
        reason = await last_rejection_reason(user["telegram_id"])
    except Exception:
        logger.exception("onsite_reg: причина отказа не прочитана (tid=%s)", user.get("telegram_id"))
        reason = None
    if not reason and (user.get("auto_reject_rule_ids") or "").strip() not in ("", "[]"):
        reason = (user.get("auto_rule_note") or "").strip() or None
    return reason


async def rejected_reason_text(user: dict, lang: str = "ru", tr_map: dict | None = None) -> str:
    """«Заявка отклонена менеджером» (+ причина, если записана) — тексты из реестра (D-34)."""
    from services.i18n import tr

    reason = await _stored_reject_reason(user)
    if reason:
        template = tr(await get_setting_typed("onsite_rejected_reason_text"), lang, tr_map or {})
        return template.replace("{reason}", reason)
    return tr(await get_setting_typed("onsite_rejected_text"), lang, tr_map or {})


def _door_approved(user: dict | None) -> bool:
    """Текущее одобрение — решение стойки: `approve_onsite` ставит `approved_at` и `onsite_at`
    одним значением; обычное одобрение позже переписало бы `approved_at`."""
    return bool(
        user and user.get("status") == "approved" and user.get("onsite_at")
        and user.get("onsite_at") == user.get("approved_at")
    )


async def ensure_onsite_outbox(user: dict | None) -> None:
    """Событие `onsite_approved` для бота (лист, сообщение, QR) — ровно одно на решение стойки.
    Ставится сразу после выигранного флипа и записи журналов (fail-soft), до отметки входа:
    сбой дальше не теряет хвост. Повторное нажатие волонтёра ставит его снова, если первое не записалось; дубль
    отсекает `enqueue_miniapp_outbox_once` (payload несёт `onsite_at` решения — новое решение в
    следующем сезоне даст новое событие). Сбой — в лог ошибкой: следующее нажатие повторит."""
    if not _door_approved(user):
        return
    tid = user["telegram_id"]
    try:
        await _db.enqueue_miniapp_outbox_once(
            "onsite_approved", {"telegram_id": tid, "onsite_at": user["onsite_at"]},
            msk_now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception:
        logger.exception("onsite_reg: событие onsite_approved не поставлено (tid=%s)", tid)


async def approve_at_door(user: dict | None, *, city: str | None, staff_id: int,
                          staff_name: str | None, bound: str | None,
                          override_reject: bool = False) -> dict:
    """Одобрение ОДНОГО человека у стойки + отметка входа.

    `city` — город стойки, выбранный в сканере; `bound` — город, к которому привязан волонтёр
    (D-26, важнее выбранного). Возвращает результат `record_arrival` ("new"/"duplicate"/...) и,
    если одобрение выиграно этим вызовом, `onsite_approved: True`. Событие outbox
    `onsite_approved` (лист, сообщение и QR человеку шлёт бот, `after_onsite_approved`) ставит
    сам сервис сразу после флипа — `ensure_onsite_outbox`. `first_entry` остаётся в результате — Mini App
    переносит его в outbox сам. Отказы: `onsite_off` (тумблер города выключен), `wrong_city`
    (D-26), `not_found`, `rejected` (заявку отклонил менеджер — без `override_reject` ничего не
    меняется). У одобрения нет `log_id`: кнопка «↩️ Отменить» сняла бы отметку, но не решение."""
    if not user:
        return {"status": "not_found", "reason_text": DENIAL_REASON_TEXT["no_user"]}
    tid = user["telegram_id"]
    resolved = bound or city or normalize_city(user.get("event_city"))
    if not await onsite_enabled(resolved):
        return {"status": "onsite_off", "reason_text": await get_setting_typed("onsite_off_text")}

    event_season = await get_setting_typed("event_season") or None
    past = is_past_season_row(user, event_season)
    # D-26: привязанный к городу волонтёр работает только со своим городом — и для уже
    # одобренного делегата (иначе стойка поставила бы вход в чужом городе). Делегата прошлого
    # сезона и walk-in без решения город не держит: их пускают на форум города стойки, и
    # одобрение переводит их туда.
    walkin = is_pending_walkin(user)
    if bound and not past and not walkin and normalize_city(user.get("event_city")) != bound:
        return {"status": "wrong_city", "reason_text": await wrong_city_text(user)}
    move_city = resolved if (
        past or (walkin and resolved and normalize_city(user.get("event_city")) != resolved)
    ) else None

    denial = await checkin_denial(user)
    if denial is None:
        # Повторное нажатие после сбоя: одобрение уже записано — восстановить хвост, если его
        # событие не встало в очередь (дубль отсекается).
        await ensure_onsite_outbox(user)
        return await record_arrival(
            user, ENTRY_POINT, source="manual", by_staff_id=staff_id, staff_name=staff_name,
        )

    overriding = user.get("status") == "rejected"
    if overriding and not override_reject:
        return {"status": "rejected", "reason_text": await rejected_reason_text(user)}

    flipped = await approve_onsite(
        tid, by_staff_id=staff_id, season=event_season,
        event_city=move_city, override_reject=overriding,
    )
    if flipped:
        from services import venue_log
        from services.applications import record_decision

        reason = _OVERRIDE_REASON if overriding else _DECISION_REASON
        details: dict = {"override_reject": True} if overriding else {}
        try:
            await record_decision(
                tid, "approved", reason, staff_id, msk_now(), effects_already_sent=True,
            )
        except Exception:
            # Не молча: ошибкой в лог, пометка в журнале площадки (кто одобрил, остаётся там и
            # в users.onsite_by), а бот, разбирая событие onsite_approved, пишет админам
            # (`_alert_if_journal_missing`) — процесс Mini App сам в Telegram не пишет.
            logger.error(
                "onsite_reg: журнал решений не записан (tid=%s, staff=%s)", tid, staff_id,
                exc_info=True,
            )
            details["decision_journal"] = "failed"
        await venue_log.log_action(
            venue_log.ACTION_ONSITE_APPROVE, staff_id=staff_id, staff_name=staff_name,
            telegram_id=tid, city=resolved, point=ENTRY_POINT, source="manual",
            details=details or None,
        )

    # Событие для бота — сразу после флипа и двух fail-soft записей журналов (они не бросают),
    # ДО отметки входа: сбой дальше не теряет хвост. Журнал решений к этому моменту уже
    # записан — бот, разбирая событие, не примет «ещё не записан» за сбой.
    fresh = await get_user(tid) or user
    await ensure_onsite_outbox(fresh)
    # Флип проигран (параллельно одобрил кто-то другой) — пускаем, только если человек теперь
    # действительно допущен; иначе отказ словами, отметку не ставим.
    fresh_denial = await checkin_denial(fresh)
    if fresh_denial is not None:
        return {"status": "denied", "reason_text": DENIAL_REASON_TEXT.get(fresh_denial, fresh_denial)}

    result = await record_arrival(
        fresh, ENTRY_POINT, source="manual", by_staff_id=staff_id, staff_name=staff_name,
    )
    result.pop("log_id", None)
    if flipped:
        result["onsite_approved"] = True
    return result


async def _alert_if_journal_missing(full: dict) -> None:
    """Процесс бота: одобрение у стойки есть, а строки «одобрено» в журнале решений нет (сбой
    записи в `approve_at_door`) — письмо админам тем же помощником, что у сбоев листа. Кто
    одобрил, видно в `users.onsite_by` и журнале площадки."""
    tid = full.get("telegram_id")
    try:
        last = await _db.get_last_application_decision(tid)
        if last and last.get("decision") == "approved" and (last.get("decided_at") or "") >= (
            full.get("onsite_at") or ""
        ):
            return
        from services.sheets import _send_admin_alert
        await _send_admin_alert(
            f"⚠️ Одобрение на месте не попало в журнал решений: делегат {tid}, одобрил(а) "
            f"{full.get('onsite_by')} в {full.get('onsite_at')}. Одобрение действует, но в "
            "истории решений и статистике менеджеров его нет — проверьте журнал площадки."
        )
    except Exception:
        logger.exception("onsite_reg: проверка журнала решений не прошла (tid=%s)", tid)


async def after_onsite_approved(bot, telegram_id: int) -> None:
    """Хвост одобрения у стойки в процессе бота: строка в Google-листе (тем же маршрутом, что
    анкета: обновить, если есть, иначе дописать), статус «На месте», сообщение человеку и QR
    (D-02: QR только после одобрения; если выпуск QR включён). Служебное сообщение — тихие часы
    не действуют (D-35). Каждый шаг fail-soft."""
    full = await get_user(telegram_id)
    if not full:
        logger.warning("onsite_reg: after_onsite_approved — нет пользователя %s", telegram_id)
        return
    if _door_approved(full):
        await _alert_if_journal_missing(full)

    try:
        from services.reg_finalize import write_sheet_row
        await write_sheet_row(telegram_id, full, "new")
    except Exception:
        logger.exception("onsite_reg: строка листа не записана (tid=%s)", telegram_id)
    try:
        from services.sheets import update_status_in_sheet
        await update_status_in_sheet(telegram_id, ONSITE_SHEET_LABEL)
    except Exception:
        logger.exception("onsite_reg: статус в листе не обновлён (tid=%s)", telegram_id)

    lang, tr_map = "ru", {}
    try:
        from services.i18n import context as i18n_context
        lang, tr_map = await i18n_context(telegram_id)
    except Exception:
        logger.exception("onsite_reg: язык человека не определён (tid=%s)", telegram_id)

    from services.i18n import tr
    from services.infra.telegram_send import send_with_retry

    try:
        text = tr(await get_setting_typed("onsite_reg_approved_text"), lang, tr_map)
        err = await send_with_retry(lambda: bot.send_message(telegram_id, text))
        if err is not None:
            logger.error("onsite_reg: сообщение об одобрении не доставлено %s: %s", telegram_id, err)
    except Exception:
        logger.exception("onsite_reg: сообщение об одобрении не отправлено (tid=%s)", telegram_id)

    try:
        if await get_setting_typed("checkin_qr_enabled") != "on":
            return
        if await checkin_denial(full) is not None:
            return
        from aiogram.types import BufferedInputFile
        from services.checkin import build_checkin_qr

        png, caption = await build_checkin_qr(full)
        caption = tr(caption, lang, tr_map)
        photo = BufferedInputFile(png, filename="qr.png")
        err = await send_with_retry(lambda: bot.send_photo(telegram_id, photo, caption=caption))
        if err is not None:
            logger.error("onsite_reg: QR не доставлен %s: %s", telegram_id, err)
    except Exception:
        logger.exception("onsite_reg: QR не отправлен (tid=%s)", telegram_id)
