"""Форум-ночь п.8 (идея №19 бэклога чек-ина, `.planning/IDEAS-CHECKIN-BACKLOG-260924.md`):
«🆘 SOS» — делегат в беде жмёт кнопку, карточка уходит в чат оргов, орг «берёт» (атомарный
захват — та же идиома, что `claim_question`/T-08-33), отвечает реплаем, при молчании
эскалирует.

Домен вынесен из `handlers/sos.py` (делегатская сторона) и `handlers/admin_sos.py`
(менеджерская сторона) в этот модуль по правилу проекта «своего Router() нет — домен в
services/, хендлеры — тонкий шов» (та же форма, что `services/questions.py` для «❓ Задать
вопрос», `services/chat_tracking.py` для привязки чата делегатов).

Метки времени — московские (`services.timeutil.msk_now()`, конвенция квика 260912-mcj для
НОВОГО кода — SOS заведён 24.09, после этой конвенции, поэтому в
`database.db._MSK_MIGRATION_COLUMNS`-исключение из UTC-семьи `delegate_questions` не
наследует).

Привязка чата SOS — байт-в-байт форма `services/chat_tracking.py` (`bound_chats`/`bind_chat`):
composite-ключ `per_city_key(SOS_CHAT_ID_KEY, code)`, читается напрямую через
`get_setting_typed` (НЕ `get_setting_typed_for_city` — та же причина D-7 у chat_tracking:
чат одного города не должен протечь другому)."""
from __future__ import annotations

import html as html_module
import logging
from datetime import datetime, timedelta

from cities import cities_module_on, get_setting_typed_for_city, per_city_key
from database.db import get_sos_report, set_sos_escalated
from services.questions import format_stamp
from services.timeutil import msk_now
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

# ── Категории (кнопки делегата, пункт 1 плана) — фиксированный набор, не реестр: владелец
# согласовал именно эти четыре (идея №19), «Кодовые значения... человеку не показываем»
# (CLAUDE.md) — код категории («bad»/«lost»/...) никогда не виден делегату/менеджеру, только
# подпись из этого словаря.
CATEGORY_BAD = "bad"
CATEGORY_LOST = "lost"
CATEGORY_ITEM = "lost_item"
CATEGORY_OTHER = "other"
CATEGORY_ORDER = (CATEGORY_BAD, CATEGORY_LOST, CATEGORY_ITEM, CATEGORY_OTHER)
CATEGORY_LABELS = {
    CATEGORY_BAD: "🤒 Плохо себя чувствую",
    CATEGORY_LOST: "🧭 Потерялся",
    CATEGORY_ITEM: "🔑 Потерял вещь",
    CATEGORY_OTHER: "⚠️ Другое",
}

# ── Статус строки (зеркало database.db._SOS_STATUS_SQL — то же правило "чистой" функцией,
# тот же приём, что services/questions.py::question_status рядом со своим SQL-зеркалом).
STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_RESOLVED = "resolved"
STATUS_LABELS = {
    STATUS_OPEN: "🆕 открыт",
    STATUS_CLAIMED: "✍️ в работе",
    STATUS_RESOLVED: "✅ решён",
}


def report_status(row: dict) -> str:
    if row.get("resolved_at"):
        return STATUS_RESOLVED
    if row.get("claimed_by") is not None:
        return STATUS_CLAIMED
    return STATUS_OPEN


# ── Пункт 1: гейт дня — кнопка «🆘 SOS» видна только в дни форума города ──────────────────

DEFAULT_ACTIVE_DAYS = 2

# Пункт 4 плана: дефолт эскалации «без «Беру»» — 5 минут, настройка per_city
# `sos_escalation_minutes`.
DEFAULT_ESCALATION_MINUTES = 5


async def is_sos_active_for_city(city: str | None) -> bool:
    """`forum_date` (пункт settings_schema.py, «Дата начала форума») + `sos_active_days`
    (per_city, дефолт 2 — большинство форумов идут 1-2 дня, см. память «Forum plan deck»:
    «Москва 30-31.10») дают окно `[forum_date, forum_date + days - 1]`. Форум-дата не задана
    ИЛИ не парсится -> False (fail-soft = кнопки нет, тот же баланс, что у menu_schedule/
    menu_important: лучше спрятать кнопку, чем показать нерабочую)."""
    from services.reject_rules import forum_date_for  # ленивый импорт — тот же цикл-разрыв,
    # что уже документирован в services/checkin_broadcast.py, reject_rules.py тяжелее этого
    # модуля не нужно тянуть на уровне импорта ради одной функции.

    raw = await forum_date_for(city)
    if not raw:
        return False
    try:
        start = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return False
    raw_days = await get_setting_typed_for_city("sos_active_days", city)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = DEFAULT_ACTIVE_DAYS
    if days < 1:
        days = 1
    today = msk_now().date()
    return start <= today <= start + timedelta(days=days - 1)


# ── Привязка чата SOS — форма services/chat_tracking.py (bound_chats/bind_chat) ──────────

SOS_CHAT_ID_KEY = "sos_chat_id"
SOS_CHAT_TITLE_KEY = "sos_chat_title"


def _parse_chat_id(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def sos_chat_for_city(city: str | None) -> dict | None:
    """`{"chat_id": int, "title": str}` или `None` — чат не привязан. Модуль городов
    выключен ИЛИ `city is None` -> глобальные ключи; иначе — ТОЛЬКО per-city ключ, без
    фолбэка на глобальный (D-7 chat_tracking: чат одного города не должен протечь другому)."""
    if not await cities_module_on() or city is None:
        chat_id = _parse_chat_id(await get_setting_typed(SOS_CHAT_ID_KEY))
        if chat_id is None:
            return None
        title = await get_setting_typed(SOS_CHAT_TITLE_KEY) or ""
        return {"chat_id": chat_id, "title": title}
    id_key = per_city_key(SOS_CHAT_ID_KEY, city)
    if id_key is None:
        return None
    chat_id = _parse_chat_id(await get_setting_typed(id_key))
    if chat_id is None:
        return None
    title_key = per_city_key(SOS_CHAT_TITLE_KEY, city)
    title = (await get_setting_typed(title_key)) if title_key else ""
    return {"chat_id": chat_id, "title": title or ""}


async def bind_sos_chat(admin_id: int | None, chat_id: int, title: str, city: str | None) -> None:
    if not await cities_module_on() or city is None:
        await set_setting_by_admin(admin_id, SOS_CHAT_ID_KEY, str(chat_id))
        await set_setting_by_admin(admin_id, SOS_CHAT_TITLE_KEY, title or "")
        return
    id_key = per_city_key(SOS_CHAT_ID_KEY, city)
    title_key = per_city_key(SOS_CHAT_TITLE_KEY, city)
    if id_key is None or title_key is None:
        logger.warning("sos.bind_sos_chat: неизвестный код города %r — привязка отклонена", city)
        return
    await set_setting_by_admin(admin_id, id_key, str(chat_id))
    await set_setting_by_admin(admin_id, title_key, title or "")


# ── Заявка «Привязать чат SOS» (пункт 2 плана): бот просит добавить его в группу и прислать
# оттуда ПОДТВЕРЖДЕНИЕ — пересылкой сообщения ИЗ группы (личка, admin.router) ИЛИ командой
# `/sos_id`, набранной прямо в группе (узкое, явное исключение из D-9 `handlers/group_chat.py`
# — команда не «читает контент», это адресное действие того же класса, что my_chat_member).
# Хранится в `sos_chat_bind_pending`, не в FSM: вторая ветка (команда в группе) физически не
# имеет доступа к приватному FSM-состоянию менеджера (другой chat_id), а второй сторонний
# стор ради одного чтения через StorageKey — больше кода, чем простая таблица с TTL.
PENDING_BIND_TTL_MINUTES = 15


async def set_pending_bind(admin_id: int, city: str | None) -> None:
    from database.db import set_sos_bind_pending

    await set_sos_bind_pending(admin_id, city)


async def get_pending_bind(admin_id: int) -> dict | None:
    """Просроченная заявка (`requested_at` старше `PENDING_BIND_TTL_MINUTES`) -> `None` и
    тихая уборка строки — менеджер, вернувшийся через час и написавший что-то не по адресу,
    не должен неожиданно привязать чат к устаревшему запросу."""
    from database.db import clear_sos_bind_pending, get_sos_bind_pending

    row = await get_sos_bind_pending(admin_id)
    if row is None:
        return None
    stamp = _parse_stamp(row.get("requested_at"))
    if stamp is None or (msk_now() - stamp).total_seconds() > PENDING_BIND_TTL_MINUTES * 60:
        await clear_sos_bind_pending(admin_id)
        return None
    return row


async def clear_pending_bind(admin_id: int) -> None:
    from database.db import clear_sos_bind_pending

    await clear_sos_bind_pending(admin_id)


def _parse_stamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


async def complete_chat_bind(bot, admin_id: int, chat_id: int, title: str) -> bool:
    """Общий хвост обеих веток подтверждения (пересылка в личке / команда `/sos_id` в
    группе) — резолвит и потребляет заявку, привязывает чат к ГОРОДУ ИЗ ЗАЯВКИ (не аргумент —
    единственный источник правды, что просили привязать, это сама заявка, поставленная
    `asos_bind_start` ДО отправки бота в группу), шлёт подтверждение личным сообщением
    (никогда не пишет в саму группу, тот же приём, что `chat_tracking`/D-1). Не находит
    подходящей заявки (просрочена/не было) -> False, вызывающий решает, что сказать (личка
    получает явный ответ; команда в группе — молчит, D-9)."""
    pending = await get_pending_bind(admin_id)
    if pending is None:
        return False
    await clear_pending_bind(admin_id)
    await bind_sos_chat(admin_id, chat_id, title, pending.get("city"))
    try:
        await bot.send_message(
            admin_id,
            f"✅ Чат SOS «{html_module.escape(title or str(chat_id))}» привязан.",
        )
    except Exception as e:
        logger.info("sos.complete_chat_bind: не удалось подтвердить admin_id=%s: %s", admin_id, e)
    return True


# ── Карточка SOS (пункт 3 плана) — маркеры "🆔"+"🆘" для реплай-детекции, та же идиома, что
# "🆔"+"❓" у карточки вопроса (handlers/admin.py::is_question_reply).

def render_card_text(report: dict, user: dict | None) -> str:
    user = user or {}
    full_name = html_module.escape(str(user.get("full_name") or "—"))
    username = user.get("username")
    username_line = html_module.escape(str(username)) if username and username != "-" else "—"
    university = html_module.escape(str(user.get("university") or "—"))
    phone = html_module.escape(str(user.get("phone") or "—"))
    city = report.get("city")
    category_label = CATEGORY_LABELS.get(report.get("category"), str(report.get("category")))

    lines = [
        f"🆘 <b>SOS #{report['id']}</b> · {category_label}",
        f"🆔 <code>{report['telegram_id']}</code> {full_name}",
        f"👤 {username_line}",
        f"🏙 {html_module.escape(str(city)) if city else '—'}",
        f"🎓 {university}",
        f"📞 {phone}",
    ]
    details = report.get("details_text")
    if details:
        lines.append(f"«{html_module.escape(str(details))}»")
    if report.get("details_photo_file_id") and not details:
        lines.append("📷 фото приложено")
    lat, lon = report.get("latitude"), report.get("longitude")
    if lat is not None and lon is not None:
        lines.append(f"📍 https://maps.google.com/?q={lat},{lon}")
    lines.append(f"🕓 {format_stamp(report.get('created_at'), stored_utc=False)}")
    return "\n".join(lines)


def build_card_kb(report_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🙋 Беру", callback_data=f"sos_claim:{report_id}"),
        InlineKeyboardButton(text="✅ Решено", callback_data=f"sos_resolve:{report_id}"),
    ]])


async def post_card(bot, report_id: int) -> bool:
    """Публикует карточку (пункт 3 плана): в привязанный чат SOS города, либо (чат не привязан
    ИЛИ отправка упала) — фоллбэк-веером в личку держателям `moderate_reg` города (та же капа,
    что экран менеджера), с ТЕМИ ЖЕ кнопками «Беру»/«Решено» — атомарный захват
    (`database.db.claim_sos_report`) работает одинаково для обоих путей, первый клик выигрывает
    независимо от числа разошедшихся копий. `card_message_id` сохраняется ТОЛЬКО для чата
    (тред «ответ реплаем» — пункт 3 плана); у веера личных копий общего треда физически нет —
    известное ограничение, задокументировано в SUMMARY. Возвращает `True`, если карточка ушла
    в чат (для решения о повторном авто-фоллбэке звонящим кодом не нужно, но полезно логам)."""
    from database.db import get_user, set_sos_card

    report = await get_sos_report(report_id)
    if report is None:
        return False
    user = await get_user(report["telegram_id"])
    text = render_card_text(report, user)
    kb = build_card_kb(report_id)
    chat = await sos_chat_for_city(report.get("city"))
    if chat is not None:
        try:
            msg = await bot.send_message(chat["chat_id"], text, parse_mode="HTML", reply_markup=kb)
            await set_sos_card(report_id, chat["chat_id"], msg.message_id)
            return True
        except Exception as e:
            logger.error(
                "sos.post_card: не удалось отправить в чат id=%s, ухожу в фоллбэк: %s",
                chat["chat_id"], e,
            )
    await _fallback_fanout(bot, report, text, kb)
    return False


async def _fallback_fanout(bot, report: dict, text: str, kb) -> int:
    from config import config
    from handlers.admin_caps import capability_holders

    recipients = await capability_holders("moderate_reg", city=report.get("city"))
    if not recipients:
        recipients = list(config.ADMIN_IDS)
    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
            sent += 1
        except Exception as e:
            logger.info("sos._fallback_fanout: не удалось написать id=%s: %s", uid, e)
    return sent


# ── Эскалация (пункт 4 плана) — APScheduler date-джоба, персистентная (SQLAlchemyJobStore,
# см. CLAUDE.md), перечитывает статус ПЕРЕД отправкой (та же идиома, что payment reminders/
# chat_tracking.bind_reconcile_job) — снятая заявкой «Беру»/«Решено» джоба не обязана быть
# отменена день-в-день, повторный тик после claim/resolve — no-op по чтению статуса.

def _escalate_job_id(report_id: int) -> str:
    return f"sos_escalate_{report_id}"


def schedule_escalation(report_id: int, minutes: int) -> None:
    try:
        from services.scheduler import _now_moscow_naive, get_scheduler

        run_at = _now_moscow_naive() + timedelta(minutes=max(1, minutes))
        get_scheduler().add_job(
            escalation_job, "date", run_date=run_at, args=[report_id],
            id=_escalate_job_id(report_id), replace_existing=True,
        )
    except Exception as e:
        logger.warning("sos.schedule_escalation(%s) failed: %s: %s", report_id, type(e).__name__, e)


def cancel_escalation(report_id: int) -> None:
    try:
        from services.scheduler import get_scheduler

        get_scheduler().remove_job(_escalate_job_id(report_id))
    except Exception:
        pass  # не стояла или уже сработала — оба случая ОК (форма cancel_task_deadline_reminder)


async def escalation_job(report_id: int) -> None:
    """Цель джобы: перечитывает `sos_reports` ПЕРЕД отправкой — claim/resolve, случившиеся
    между постановкой и тиком, гасят эскалацию без дополнительной отмены джобы (fail-soft:
    гонка «Беру» и джобы решается в пользу «уже взяли», не двойным сообщением)."""
    try:
        report = await get_sos_report(report_id)
        if report is None or report_status(report) != STATUS_OPEN:
            return
        stamped = await set_sos_escalated(report_id)
        if not stamped:
            return
        import services.scheduler as scheduler_module

        bot = scheduler_module.get_bot()
        await _deliver_escalation(bot, report)
    except Exception as e:
        logger.error("sos.escalation_job(%s) failed: %s: %s", report_id, type(e).__name__, e)


async def _deliver_escalation(bot, report: dict) -> None:
    from database.db import get_user
    from handlers.admin_caps import notify_by_capability

    minutes = await get_setting_typed_for_city("sos_escalation_minutes", report.get("city")) \
        or DEFAULT_ESCALATION_MINUTES
    text_group = f"⏰ Никто не взял SOS #{report['id']} за {minutes} мин."
    chat = await sos_chat_for_city(report.get("city"))
    if chat is not None:
        try:
            await bot.send_message(
                chat["chat_id"], text_group,
                reply_to_message_id=report.get("card_message_id") or None,
            )
        except Exception as e:
            logger.warning(
                "sos._deliver_escalation: не удалось написать в чат id=%s: %s",
                chat["chat_id"], e,
            )
    user = await get_user(report["telegram_id"])
    alert_text = (
        f"⏰ <b>SOS #{report['id']} без ответа {minutes} мин.</b>\n\n"
        + render_card_text(report, user)
    )
    await notify_by_capability(bot, "moderate_reg", alert_text, parse_mode="HTML", city=report.get("city"))
