"""Форум-ночь п.8 (идея №19 бэклога чек-ина, `.planning/IDEAS-CHECKIN-BACKLOG-260924.md`):
«🆘 SOS» — делегат в беде жмёт кнопку, карточка уходит в чат оргов, орг «берёт» (атомарный
захват — та же идиома, что `claim_question`/T-08-33), отвечает реплаем, при молчании
эскалирует.

Домен вынесен из `handlers/sos.py` (делегатская сторона) и `handlers/admin_sos.py`
(менеджерская сторона) в этот модуль по правилу проекта «своего Router() нет — домен в
services/, хендлеры — тонкий шов» (та же форма, что `services/questions.py` для «❓ Задать
вопрос», `services/chat_tracking.py` для привязки чата делегатов).

Решение владельца D-31 (24.09, `.planning/FORUM-CHECKIN.md`): **SOS без категорий.** Кнопки
категорий без пояснительного текста бесполезны — в экстренной ситуации важна скорость, не
классификация. Кнопка «🆘 SOS» СРАЗУ создаёт заявку и публикует карточку («подробности ещё не
прислали»); делегат попадает в режим «дописываю SOS» (`SosReport.collecting`,
`handlers/sos.py`) — всё, что он пишет/присылает (текст, фото, геопозиция), уходит В ТРЕД
карточки И дописывает саму карточку (первый текст/фото снимает пометку «подробности ещё не
прислали»). Режим живёт до «Готово», решения заявки оргом, таймаута
(`sos_collecting_timeout_minutes`) или следующего `/start`. Старые категорийные кнопки/тексты
(`CATEGORY_*`, `sos_category_prompt_text`/`sos_details_prompt_text`/`sos_location_prompt_text`)
удалены из потока целиком; колонка `sos_reports.category` осталась в БД NULL-able ради
обратной совместимости (старые строки), но новый код её никогда не пишет и не читает.

Метки времени — московские (`services.timeutil.msk_now()`, конвенция квика 260912-mcj для
НОВОГО кода — SOS заведён 24.09, после этой конвенции, поэтому в
`database.db._MSK_MIGRATION_COLUMNS`-исключение из UTC-семьи `delegate_questions` не
наследует).

Привязка чата SOS — байт-в-байт форма `services/chat_tracking.py` (`bound_chats`/`bind_chat`):
composite-ключ `per_city_key(SOS_CHAT_ID_KEY, code)`, читается напрямую через
`get_setting_typed` (НЕ `get_setting_typed_for_city` — та же причина D-7 у chat_tracking:
чат одного города не должен протечь другому)."""
from __future__ import annotations

import asyncio
import html as html_module
import logging
import re
from datetime import date, datetime, timedelta

from cities import cities_module_on, get_setting_typed_for_city, per_city_key
from database.db import advance_sos_claimed_remind, get_sos_report, set_sos_escalated
from services.questions import format_stamp
from services.timeutil import msk_now
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

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

# D-31: дефолт «сколько ждать дозапись» (режим «дописываю SOS» после мгновенной карточки) —
# 30 минут, настройка per_city `sos_collecting_timeout_minutes` (тот же довод, что у
# sos_reopen_window_minutes/sos_claimed_remind_minutes — форумы городов идут в разные дни).
DEFAULT_COLLECTING_TIMEOUT_MINUTES = 30


async def is_sos_active_for_city(city: str | None) -> bool:
    """`forum_date` (пункт settings_schema.py, «Дата начала форума») + `sos_active_days`
    (per_city, дефолт 2 — большинство форумов идут 1-2 дня, см. память «Forum plan deck»:
    «Москва 30-31.10») дают окно `[forum_date, forum_date + days - 1]`. Форум-дата не задана
    ИЛИ не парсится -> False (fail-soft = кнопки нет, тот же баланс, что у menu_program/
    menu_important: лучше спрятать кнопку, чем показать нерабочую)."""
    window = await sos_active_window(city)
    if window is None:
        return False
    start, end = window
    return start <= msk_now().date() <= end


async def sos_active_window(city: str | None) -> tuple[date, date] | None:
    """Окно дней форума города `[первый, последний]` (включительно) для
    `is_sos_active_for_city` и напоминаний взявшему SOS; `None` — дата форума не задана или
    не парсится."""
    from services.reject_rules import forum_date_for  # ленивый импорт — тот же цикл-разрыв,
    # что уже документирован в services/checkin_broadcast.py, reject_rules.py тяжелее этого
    # модуля не нужно тянуть на уровне импорта ради одной функции.

    raw = await forum_date_for(city)
    if not raw:
        return None
    try:
        start = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    raw_days = await get_setting_typed_for_city("sos_active_days", city)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = DEFAULT_ACTIVE_DAYS
    if days < 1:
        days = 1
    return start, start + timedelta(days=days - 1)


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


# Ревью 24.09 (находка 2): три исхода вместо голого bool — «личка устарела»/«личка молчит,
# D-9» команды `/sos_id` в группе должны различаться от «бота ещё нет в чате», иначе
# `handlers/admin_sos.py::asos_bind_step` не может объяснить менеджеру, что сделать дальше
# (CLAUDE.md: «ошибка объясняет, что делать»).
BIND_OK = "ok"
BIND_EXPIRED = "expired"
BIND_NOT_MEMBER = "not_member"


async def _bot_is_chat_member(bot, chat_id: int) -> bool:
    """`getChatMember(chat_id, bot.id)` (CLAUDE.md tech stack: getChatMember, no new library) —
    пересланное сообщение доказывает только, что бот КОГДА-ТО состоял в чате (пересылка того
    старого сообщения не тратит статус членства), а не что он там СЕЙЧАС — бота могли выгнать
    между добавлением и пересылкой подтверждения."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        return getattr(member, "status", None) in ("member", "administrator", "creator")
    except Exception as e:
        logger.info(
            "sos._bot_is_chat_member: get_chat_member(%s) failed: %s: %s",
            chat_id, type(e).__name__, e,
        )
        return False


async def complete_chat_bind(bot, admin_id: int, chat_id: int, title: str) -> str:
    """Общий хвост обеих веток подтверждения (пересылка в личке / команда `/sos_id` в
    группе) — резолвит и потребляет заявку, привязывает чат к ГОРОДУ ИЗ ЗАЯВКИ (не аргумент —
    единственный источник правды, что просили привязать, это сама заявка, поставленная
    `asos_bind_start` ДО отправки бота в группу), шлёт подтверждение личным сообщением
    (никогда не пишет в саму группу, тот же приём, что `chat_tracking`/D-1).

    Возвращает `BIND_OK` / `BIND_EXPIRED` (заявка просрочена/не было — вызывающий решает, что
    сказать: личка получает явный ответ, команда в группе молчит, D-9) / `BIND_NOT_MEMBER`
    (ревью 24.09, находка 2: бота сейчас нет в целевом чате — заявка НЕ потребляется, менеджер
    может добавить бота и переслать сообщение ещё раз без похода в «🔗 Привязать чат SOS»
    заново)."""
    pending = await get_pending_bind(admin_id)
    if pending is None:
        return BIND_EXPIRED
    if not await _bot_is_chat_member(bot, chat_id):
        return BIND_NOT_MEMBER
    await clear_pending_bind(admin_id)
    await bind_sos_chat(admin_id, chat_id, title, pending.get("city"))
    try:
        await bot.send_message(
            admin_id,
            f"✅ Чат SOS «{html_module.escape(title or str(chat_id))}» привязан.",
        )
    except Exception as e:
        logger.info("sos.complete_chat_bind: не удалось подтвердить admin_id=%s: %s", admin_id, e)
    return BIND_OK


# ── Карточка SOS (пункт 3 плана) — маркеры "🆔"+"🆘" для реплай-детекции, та же идиома, что
# "🆔"+"❓" у карточки вопроса (handlers/admin.py::is_question_reply).

async def resolve_city_label(city_code: str | None) -> str | None:
    """`cities.city_label(code)` -> человеческая подпись («Москва»), не код — CLAUDE.md:
    «Кодовые значения ... человеку не показываем». `None`/неизвестный код -> `None` (карточка
    покажет «—», см. `render_card_text`), fail-soft — сбой резолва не должен ронять карточку."""
    if not city_code:
        return None
    try:
        from cities import city_label

        return await city_label(city_code)
    except Exception as e:
        logger.warning("sos.resolve_city_label(%r) failed: %s", city_code, e)
        return None


def render_card_text(report: dict, user: dict | None, *, city_label: str | None = None) -> str:
    """`city_label` — уже РЕЗОЛВЕННАЯ человеческая подпись города (CLAUDE.md: «Кодовые значения
    ... человеку не показываем»), не код. Функция остаётся синхронной/чистой (`cities.city_label`
    — async, резолвится ОДИН раз в вызывающем коде — `post_card`/`refresh_card`, оба уже в
    async-контексте) — сырой код `report["city"]` сюда не подставляется никогда.

    D-31: карточка публикуется МГНОВЕННО, до того как делегат прислал хоть слово — пока
    `details_text`/`details_photo_file_id` оба пусты, строка «🆘 СРОЧНО — подробности ещё не
    прислали» держит место вместо категории (которую убрали целиком); как только делегат в
    режиме «дописываю SOS» (`SosReport.collecting`) присылает первый текст/фото
    (`database.db.add_sos_details`), пометка сменяется на сами подробности."""
    user = user or {}
    full_name = html_module.escape(str(user.get("full_name") or "—"))
    username = user.get("username")
    username_line = html_module.escape(str(username)) if username and username != "-" else "—"
    university = html_module.escape(str(user.get("university") or "—"))
    phone = html_module.escape(str(user.get("phone") or "—"))
    city_text = html_module.escape(str(city_label)) if city_label else "—"

    lines = [
        f"🆘 <b>SOS #{report['id']}</b>",
        f"🆔 <code>{report['telegram_id']}</code> {full_name}",
        f"👤 {username_line}",
        f"🏙 {city_text}",
        f"🎓 {university}",
        f"📞 {phone}",
    ]
    details = report.get("details_text")
    photo = report.get("details_photo_file_id")
    if details:
        lines.append(f"«{html_module.escape(str(details))}»")
        if photo:
            lines.append("📷 фото приложено")
    elif photo:
        lines.append("📷 фото приложено")
    else:
        lines.append("🆘 СРОЧНО — подробности ещё не прислали")
    lat, lon = report.get("latitude"), report.get("longitude")
    if lat is not None and lon is not None:
        lines.append(f"📍 https://maps.google.com/?q={lat},{lon}")
    lines.append(f"🕓 {format_stamp(report.get('created_at'), stored_utc=False)}")

    # Ревью 24.09 (находка 3): «старое» открытое SOS того же делегата разрешено (окно
    # `sos_reopen_window_minutes` истекло) — карточка НОВОГО SOS честно ссылается на прежний,
    # не привязанный (тот остаётся открытым и не подхватывается автоматически).
    prior_id = report.get("prior_open_report_id")
    if prior_id:
        lines.append(f"⚠️ У делегата есть открытый SOS #{prior_id}")

    # Пункт 3 плана: карточка обновляется «Взял: … в HH:MM» после «🙋 Беру», «✅ Решено …» после
    # решения — тот же приём, что `handlers/admin_sos.py::_row_text` (журнал экрана менеджера),
    # здесь для карточки, которую видит весь чат оргов.
    status = report_status(report)
    if status == STATUS_CLAIMED:
        who = html_module.escape(str(report.get("claimed_by_name") or "—"))
        when = format_stamp(report.get("claimed_at"), stored_utc=False)
        lines.append(f"✍️ Взял(а): {who} в {when[-5:] if when else '—'}")
    elif status == STATUS_RESOLVED:
        who = html_module.escape(str(report.get("resolved_by_name") or "—"))
        when = format_stamp(report.get("resolved_at"), stored_utc=False)
        lines.append(f"✅ Решено: {who} в {when[-5:] if when else '—'}")
    return "\n".join(lines)


def build_card_kb(report_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🙋 Беру", callback_data=f"sos_claim:{report_id}"),
        InlineKeyboardButton(text="✅ Решено", callback_data=f"sos_resolve:{report_id}"),
    ]])


# ── Ревью 24.09 (находка 1): `post_card` раньше возвращал голый `bool` («ушло в чат?»), который
# `handlers/sos.py::_finalize_sos` даже не читал — делегат слышал «Оргкомитет получил» и тогда,
# когда карточка не дошла НИКУДА (чат упал, фоллбэк-веер разошёлся нулю получателей, например
# все держатели `moderate_reg` заблокировали бота). `PostCardResult` несёт РЕАЛЬНОЕ число
# доставок по каждому каналу — вызывающий код сам решает, что сказать делегату
# (`delivered_total == 0` -> честный текст + контакт + повторная попытка).
class PostCardResult:
    __slots__ = ("chat_delivered", "dm_delivered")

    def __init__(self, chat_delivered: bool = False, dm_delivered: int = 0):
        self.chat_delivered = chat_delivered
        self.dm_delivered = dm_delivered

    @property
    def delivered_total(self) -> int:
        return (1 if self.chat_delivered else 0) + self.dm_delivered

    def __repr__(self) -> str:  # отладка/тесты
        return f"PostCardResult(chat_delivered={self.chat_delivered}, dm_delivered={self.dm_delivered})"


async def _send_to_bound_chat(bot, city: str | None, chat: dict, text: str, **kwargs):
    """Общая отправка в привязанный чат SOS — ОБЩИЙ контур «здоровье чата» для `post_card`
    (карточка) И `_deliver_escalation` (текст эскалации, ревью 24.09 находка 2 просила ровно
    это: те же обработки на эскалации, что уже были только у карточки). Чат мигрировал в
    супергруппу (`TelegramMigrateToChat`) — перепривязывает `sos_chat_id` города на новый
    `migrate_to_chat_id` и повторяет ОДНУ попытку в новый chat_id. Любая другая ошибка (в т.ч.
    повтор после миграции) — чат помечается нездоровым (`_mark_chat_unhealthy`, троттлинг
    алерта менеджерам раз в час).

    Возвращает `(msg, chat_id_доставки)` при успехе (исходный ИЛИ новый после миграции chat_id
    — вызывающему нужен фактический id, не только объект сообщения, см. `post_card::
    set_sos_card`) либо `(None, None)`, если доставка не удалась НИ В ОДИН chat_id — вызывающий
    сам решает, что делать дальше (фоллбэк-веер личкой у `post_card`, просто лог у эскалации)."""
    from aiogram.exceptions import TelegramMigrateToChat

    chat_id = chat["chat_id"]
    try:
        msg = await bot.send_message(chat_id, text, **kwargs)
        _mark_chat_healthy(chat_id)
        return msg, chat_id
    except TelegramMigrateToChat as e:
        new_chat_id = e.migrate_to_chat_id
        logger.warning(
            "sos._send_to_bound_chat: чат id=%s мигрировал в супергруппу id=%s, перепривязываю",
            chat_id, new_chat_id,
        )
        await bind_sos_chat(None, new_chat_id, chat.get("title") or "", city)
        try:
            msg = await bot.send_message(new_chat_id, text, **kwargs)
            _mark_chat_healthy(new_chat_id)
            return msg, new_chat_id
        except Exception as e2:
            logger.error(
                "sos._send_to_bound_chat: повтор в новый чат id=%s (после миграции) тоже упал: %s",
                new_chat_id, e2,
            )
            await _mark_chat_unhealthy(new_chat_id, city)
            return None, None
    except Exception as e:
        logger.error(
            "sos._send_to_bound_chat: не удалось отправить в чат id=%s: %s",
            chat_id, e,
        )
        await _mark_chat_unhealthy(chat_id, city)
        return None, None


async def post_card(bot, report_id: int) -> PostCardResult:
    """Публикует карточку (пункт 3 плана): в привязанный чат SOS города, либо (чат не привязан
    ИЛИ отправка упала) — фоллбэк-веером в личку держателям `moderate_reg` города (та же капа,
    что экран менеджера), с ТЕМИ ЖЕ кнопками «Беру»/«Решено» — атомарный захват
    (`database.db.claim_sos_report`) работает одинаково для обоих путей, первый клик выигрывает
    независимо от числа разошедшихся копий. `card_message_id` сохраняется ТОЛЬКО для чата
    (тред «ответ реплаем» — пункт 3 плана); у веера личных копий общего треда физически нет —
    известное ограничение, задокументировано в SUMMARY. Возвращает `PostCardResult` — реальное
    число доставок, не голый факт «дошло ли в чат» (ревью 24.09, находка 1).

    Здоровье чата (миграция в супергруппу/провал отправки) — общий хелпер `_send_to_bound_chat`
    (тот же контур, что и у `_deliver_escalation`)."""
    from database.db import get_user, set_sos_card

    report = await get_sos_report(report_id)
    if report is None:
        return PostCardResult()
    user = await get_user(report["telegram_id"])
    text = render_card_text(report, user, city_label=await resolve_city_label(report.get("city")))
    kb = build_card_kb(report_id)
    chat = await sos_chat_for_city(report.get("city"))
    if chat is not None:
        msg, chat_id_used = await _send_to_bound_chat(
            bot, report.get("city"), chat, text, parse_mode="HTML", reply_markup=kb,
        )
        if msg is not None:
            await set_sos_card(report_id, chat_id_used, msg.message_id)
            return PostCardResult(chat_delivered=True)
    dm_count = await _fallback_fanout(bot, report, text, kb)
    return PostCardResult(dm_delivered=dm_count)


async def record_delivery_outcome(report_id: int, result: PostCardResult) -> None:
    """Хвост `post_card` (изначальная попытка ИЛИ `delivery_retry_job`) — штампует/снимает
    `sos_reports.delivery_failed_at` по факту, доставлено ли хоть кому-то (находка 1)."""
    from database.db import set_sos_delivery_failed

    await set_sos_delivery_failed(report_id, result.delivered_total == 0)


# ── D-31: перерисовка карточки в чате после «дописывания» (текст/фото/гео режима collecting) ─
#
# Тот же рендер, что раньше жил ТОЛЬКО в `handlers/admin_sos.py::_refresh_card` (после захвата/
# решения заявки) — перенесён сюда, потому что теперь его зовёт ещё и делегатская сторона
# (`handlers/sos.py`, режим «дописываю SOS»), а домен карточки целиком живёт в этом модуле
# (докстринг файла). `admin_sos._refresh_card` остаётся тонкой обёрткой ради обратной
# совместимости места вызова.

async def refresh_card(bot, report_id: int) -> None:
    """Перерисовывает карточку в чате (если она там есть) — fail-soft: карточка могла быть
    удалена/устареть, это не должно ронять сам вызов (дозапись делегата/захват/решение)."""
    from database.db import get_user

    from database.db import list_sos_card_copies

    report = await get_sos_report(report_id)
    if report is None:
        return
    targets = await list_sos_card_copies(report_id)  # копии фоллбэка в личке админов
    if report.get("chat_id") and report.get("card_message_id"):
        targets.append((report["chat_id"], report["card_message_id"]))
    if not targets:
        return
    user = await get_user(report["telegram_id"])
    text = render_card_text(report, user, city_label=await resolve_city_label(report.get("city")))
    kb = (
        None if report_status(report) == STATUS_RESOLVED
        else build_card_kb(report_id)
    )
    from aiogram.exceptions import TelegramRetryAfter

    for chat_id, message_id in targets:
        # Флуд-лимит на веере копий — пауза и один повтор, иначе копия так и останется
        # «открытой» у этого админа. Прочие ошибки (копию удалили, бота заблокировали) —
        # fail-soft, остальным перерисуем.
        for attempt in range(2):
            try:
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id,
                    parse_mode="HTML", reply_markup=kb,
                )
            except TelegramRetryAfter as e:
                if attempt == 0:
                    await asyncio.sleep(e.retry_after)
                    continue
            except Exception:
                pass
            break


# ── Ответ орга РЕПЛАЕМ на карточку — общий путь для чата SOS (`handlers/group_chat.py`) и
# личной копии карточки (`handlers/admin_sos.py::admin_reply_to_sos`). Кто вправе ответить,
# решают вызывающие: в чате — сам факт, что карточка из привязанного чата SOS этой заявки, в
# личке — капа `moderate_reg` + город заявки. Здесь только захват, доставка и отчёт.

_CARD_NUMBER_RE = re.compile(r"SOS #([0-9]+)")


def card_report_id(replied) -> int | None:
    """Номер заявки из карточки SOS, на которую ответили; `None` — это не карточка (нет
    маркеров 🆔+🆘 или номера). Текст реплая у Telegram плоский — HTML-разметка карточки
    (`<b>`/`<code>`) в нём не участвует."""
    text = getattr(replied, "text", None) or ""
    if "🆔" not in text or "🆘" not in text:
        return None
    match = _CARD_NUMBER_RE.search(text)
    return int(match.group(1)) if match else None


async def deliver_org_reply(bot, message, report: dict) -> bool:
    """Захватывает заявку за ответившим (если её ещё никто не взял) и доставляет ответ
    делегату немедленно — тихие часы к SOS не применяются (`services/quiet_hours.py` здесь не
    зовётся, в отличие от «❓ Задать вопрос»). Получатель — `report["telegram_id"]`, не 🆔 из
    текста карточки: номер заявки — единственное, что читается из сообщения. True — ответ
    дошёл до делегата."""
    from database.db import claim_sos_report
    from secret_redact import redact_secrets

    report_id = report["id"]
    admin_name = message.from_user.full_name or message.from_user.username or "Орг"
    claimed = await claim_sos_report(report_id, message.from_user.id, admin_name)
    if not claimed:
        row = await get_sos_report(report_id)
        same_claimant = (
            row and row.get("claimed_by") == message.from_user.id and not row.get("resolved_at")
        )
        if row and row.get("resolved_at"):
            await message.reply(
                f"⚠️ SOS #{report_id} уже отмечен решённым — ответ не отправлен. Если нужно "
                f"что-то добавить, свяжитесь с делегатом по телефону из карточки."
            )
            return False
        if not same_claimant:
            winner = (row or {}).get("claimed_by_name") or "коллега"
            await message.reply(
                f"⚠️ SOS #{report_id} уже взял(а) {winner} — напишите ему(ей) или "
                f"дождитесь «✅ Решено»."
            )
            return False

    user_id = report["telegram_id"]
    header = f"🆘 <b>Ответ по SOS #{report_id}:</b>"
    try:
        if message.text:
            await bot.send_message(user_id, f"{header}\n\n{message.html_text}", parse_mode="HTML")
        else:
            await bot.send_message(user_id, header, parse_mode="HTML")
            await message.copy_to(user_id)
    except Exception as e:
        await message.reply(
            "❌ Не удалось отправить ответ делегату: "
            f"{html_module.escape(redact_secrets(e))}. Свяжитесь по телефону из карточки."
        )
        return False

    cancel_escalation(report_id)
    await message.reply("✅ Ответ отправлен делегату.")
    await refresh_card(bot, report_id)
    return True


# ── D-31: режим «дописываю SOS» — всё, что делегат шлёт после мгновенной карточки, уходит В
# ТРЕД (реплаем на карточку в чате оргов ЛИБО, без привязанного чата/при её провале, личным
# веером держателям `moderate_reg` — известное ограничение фоллбэка без треда, то же, что у
# `post_card`/`_fallback_fanout`).

async def relay_delegate_message(message, report_id: int) -> None:
    """`message` — оригинал делегата (текст/фото/геопозиция), копируется КАК ЕСТЬ
    (`message.copy_to`) — та же форма, что была у `handlers/sos.py::_relay_report_followup`
    до переноса сюда (пункт 3 плана: реплай уводит ответ орга мимо тихих часов, здесь —
    обратное направление, делегат дописывает свою же заявку)."""
    report = await get_sos_report(report_id)
    if report is None:
        return
    if report.get("chat_id") and report.get("card_message_id"):
        try:
            await message.copy_to(
                report["chat_id"], reply_to_message_id=report["card_message_id"],
            )
            return
        except Exception as e:
            logger.warning(
                f"sos.relay_delegate_message: не удалось отправить в чат id={report['chat_id']}: {e}",
            )
    from config import config
    from handlers.admin_caps import capability_holders

    recipients = await capability_holders("moderate_reg", city=report.get("city"))
    if not recipients:
        recipients = list(config.ADMIN_IDS)
    prefix = f"💬 Делегат дополнил SOS #{report_id}:"
    for uid in recipients:
        try:
            await message.bot.send_message(uid, prefix)
            await message.copy_to(uid)
        except Exception as e:
            logger.info(f"sos.relay_delegate_message: не удалось написать id={uid}: {e}")


# ── Повторная попытка доставки (пункт 1 плана, находка 1) — ОДНА попытка через минуту, джоба
# перечитывает статус ПЕРЕД повтором (та же идиома, что escalation_job ниже): решённая/уже
# доставленная заявка не получает лишний повтор.

def _delivery_retry_job_id(report_id: int) -> str:
    return f"sos_delivery_retry_{report_id}"


def schedule_delivery_retry(report_id: int, delay_minutes: int = 1) -> None:
    try:
        from services.scheduler import _now_moscow_naive, get_scheduler

        run_at = _now_moscow_naive() + timedelta(minutes=max(1, delay_minutes))
        get_scheduler().add_job(
            delivery_retry_job, "date", run_date=run_at, args=[report_id],
            id=_delivery_retry_job_id(report_id), replace_existing=True,
        )
    except Exception as e:
        logger.warning("sos.schedule_delivery_retry(%s) failed: %s: %s", report_id, type(e).__name__, e)


async def delivery_retry_job(report_id: int) -> None:
    try:
        report = await get_sos_report(report_id)
        if report is None or report.get("resolved_at") or not report.get("delivery_failed_at"):
            return  # решён, не найден или уже доставлен другим путём — повтор не нужен
        import services.scheduler as scheduler_module

        bot = scheduler_module.get_bot()
        result = await post_card(bot, report_id)
        await record_delivery_outcome(report_id, result)
        if result.delivered_total == 0:
            logger.error(
                "sos.delivery_retry_job(%s): повторная попытка тоже не доставлена никому",
                report_id,
            )
    except Exception as e:
        logger.error("sos.delivery_retry_job(%s) failed: %s: %s", report_id, type(e).__name__, e)


# ── Здоровье привязанного чата (пункт 2 плана, находка 2) — процессный (не в БД) словарь,
# та же форма, что `handlers/admin_caps.py::_blocked_notified_at` (троттлинг «раз в час», после
# рестарта алерт может повториться — это приемлемо, не критичный журнал). Красная строка на
# экране менеджера (`handlers/admin_sos.py::render_sos_screen`) читает `chat_is_unhealthy`
# напрямую — тот же процесс шлёт алерт и рендерит экран.
_unhealthy_chats: set[int] = set()
_chat_alert_sent_at: dict[int, float] = {}
_CHAT_ALERT_COOLDOWN_SECONDS = 60 * 60


def chat_is_unhealthy(chat_id: int) -> bool:
    return chat_id in _unhealthy_chats


def _mark_chat_healthy(chat_id: int) -> None:
    _unhealthy_chats.discard(chat_id)


async def _mark_chat_unhealthy(chat_id: int, city: str | None) -> None:
    import time

    _unhealthy_chats.add(chat_id)
    now = time.monotonic()
    # None, а не 0.0: monotonic считает от загрузки машины — на свежем сервере (аптайм < 1 ч)
    # «0.0» глушил САМЫЙ ПЕРВЫЙ алерт как «уже был в пределах часа».
    last = _chat_alert_sent_at.get(chat_id)
    if last is not None and now - last < _CHAT_ALERT_COOLDOWN_SECONDS:
        return  # алерт этого чата уже уходил в пределах часа — тихо, без повтора
    _chat_alert_sent_at[chat_id] = now
    try:
        import services.scheduler as scheduler_module
        from handlers.admin_caps import notify_by_capability

        bot = scheduler_module.get_bot()
        await notify_by_capability(
            bot, "settings",
            "⚠️ Чат SOS недоступен — SOS идут в личку. Перепривяжите чат.",
            city=city,
        )
    except Exception as e:
        logger.error("sos._mark_chat_unhealthy: алерт менеджерам не отправлен: %s: %s", type(e).__name__, e)


async def _fallback_fanout(bot, report: dict, text: str, kb) -> int:
    from config import config
    from database.db import add_sos_card_copy
    from handlers.admin_caps import capability_holders

    recipients = await capability_holders("moderate_reg", city=report.get("city"))
    if not recipients:
        recipients = list(config.ADMIN_IDS)
    sent = 0
    for uid in recipients:
        try:
            msg = await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
            sent += 1
        except Exception as e:
            logger.info("sos._fallback_fanout: не удалось написать id=%s: %s", uid, e)
            continue
        # Запоминаем копию, чтобы «Беру»/«Решено» перерисовали её у всех (refresh_card).
        try:
            await add_sos_card_copy(report["id"], uid, msg.message_id)
        except Exception as e:
            logger.error("sos._fallback_fanout: копия карточки id=%s не сохранена: %s", uid, e)
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
    """Эскалация невзятого SOS — тексты «никто не взял за N мин»."""
    minutes = await get_setting_typed_for_city("sos_escalation_minutes", report.get("city")) \
        or DEFAULT_ESCALATION_MINUTES
    await _send_escalation(
        bot, report,
        f"⏰ Никто не взял SOS #{report['id']} за {minutes} мин.",
        alert_head=f"⏰ <b>SOS #{report['id']} без ответа {minutes} мин.</b>",
    )


async def _send_escalation(bot, report: dict, group_text: str, *, alert_head: str) -> None:
    """Общий путь эскалации: `group_text` (простой текст) — в привязанный чат SOS реплаем на
    карточку; `alert_head` (HTML) + карточка заявки — лично менеджерам с капой `moderate_reg`
    города.

    Ревью 24.09 (находка 2, добавка): текст в привязанный чат идёт через тот же
    `_send_to_bound_chat`, что и карточка (`post_card`) — миграция чата в супергруппу больше не
    роняет эскалацию молча (перепривязка + один повтор в новый chat_id), а провал отправки
    метит чат нездоровым (алерт держателям settings, троттлинг раз в час)."""
    from database.db import get_user
    from handlers.admin_caps import notify_by_capability

    chat = await sos_chat_for_city(report.get("city"))
    if chat is not None:
        await _send_to_bound_chat(
            bot, report.get("city"), chat, group_text,
            reply_to_message_id=report.get("card_message_id") or None,
        )
    user = await get_user(report["telegram_id"])
    alert_text = f"{alert_head}\n\n" + render_card_text(report, user)
    await notify_by_capability(bot, "moderate_reg", alert_text, parse_mode="HTML", city=report.get("city"))


# ── Напоминание взявшему «🙋 Беру», кто не отметил «✅ Решено» (ревью 24.09, находка 3,
# часть А; лесенка — стенд 25.09). Раньше джоба перепланировала себя на тот же интервал
# бесконечно — на стенде за ночь 16 напоминаний по одной заявке. Теперь:
# - три напоминания с растущими паузами (`CLAIMED_REMIND_DELAYS_MINUTES`; первую паузу менеджер
#   может сменить настройкой `sos_claimed_remind_minutes`), после третьего — одно сообщение
#   менеджерам тем же путём, что эскалация невзятого SOS (`_send_escalation`), и больше ничего;
# - счётчик ушедших напоминаний — в БД (`sos_reports.claimed_remind_count`, compare-and-set
#   `advance_sos_claimed_remind`), id джобы фиксированный: рестарт лесенку не сбрасывает и
#   ступень дважды не шлёт;
# - ступень, попавшая в тихие часы города, переносится на конец окна (не теряется);
# - после последнего дня форума города (`sos_active_window`) — ни отправки, ни новой джобы;
# - джоба перечитывает заявку: решена или уже у другого взявшего — молча гаснет.
DEFAULT_CLAIMED_REMIND_MINUTES = 20
CLAIMED_REMIND_DELAYS_MINUTES = (DEFAULT_CLAIMED_REMIND_MINUTES, 60, 180)
CLAIMED_REMIND_MAX = len(CLAIMED_REMIND_DELAYS_MINUTES)


def _claimed_remind_job_id(report_id: int) -> str:
    return f"sos_claimed_remind_{report_id}"


def _schedule_claimed_reminder_at(report_id: int, claimant_id: int | None, run_at: datetime) -> None:
    try:
        from services.scheduler import get_scheduler

        get_scheduler().add_job(
            claimed_reminder_job, "date", run_date=run_at, args=[report_id],
            kwargs={"claimant_id": claimant_id},
            id=_claimed_remind_job_id(report_id), replace_existing=True,
        )
    except Exception as e:
        logger.warning("sos._schedule_claimed_reminder_at(%s) failed: %s: %s", report_id, type(e).__name__, e)


def schedule_claimed_reminder(report_id: int, minutes: int, claimant_id: int | None = None) -> None:
    """Первая ступень — через `minutes` (настройка `sos_claimed_remind_minutes`) после «Беру»."""
    try:
        from services.scheduler import _now_moscow_naive

        run_at = _now_moscow_naive() + timedelta(minutes=max(1, minutes))
    except Exception as e:
        logger.warning("sos.schedule_claimed_reminder(%s) failed: %s: %s", report_id, type(e).__name__, e)
        return
    _schedule_claimed_reminder_at(report_id, claimant_id, run_at)


def cancel_claimed_reminder(report_id: int) -> None:
    try:
        from services.scheduler import get_scheduler

        get_scheduler().remove_job(_claimed_remind_job_id(report_id))
    except Exception:
        pass  # не стояла или уже сработала — оба случая ОК (форма cancel_escalation)


def _minutes_in_work(report: dict, now: datetime) -> int:
    claimed_at = _parse_stamp(report.get("claimed_at"))
    if claimed_at is None:
        return 0
    return max(0, int((now - claimed_at).total_seconds() // 60))


def _fill(template: str, **subs) -> str:
    text = template
    for key, value in subs.items():
        text = text.replace("{" + key + "}", str(value))
    return text


async def _translated_for(telegram_id: int, template: str, **subs) -> str:
    """Шаблон реестра -> язык получателя (шаблон переводится ДО подстановки, как везде в чате —
    `handlers.reg_i18n.tr_fmt`). Сбой перевода -> русский шаблон с подстановкой."""
    try:
        from handlers import reg_i18n
        from services import i18n as i18n_service

        lang, tr_map = await i18n_service.context(telegram_id)
        return reg_i18n.tr_fmt(template, lang, tr_map, **subs)
    except Exception as e:
        logger.info("sos._translated_for(%s): перевод не удался: %s", telegram_id, e)
        return _fill(template, **subs)


async def claimed_reminder_job(report_id: int, _legacy_minutes: int | None = None, *,
                               claimant_id: int | None = None) -> None:
    """Одна ступень лесенки напоминаний. `_legacy_minutes` — второй позиционный аргумент
    джоб, поставленных до лесенки (они лежат в персистентном хранилище APScheduler) — не
    используется: пауза следующей ступени берётся из `CLAIMED_REMIND_DELAYS_MINUTES`."""
    try:
        from services import quiet_hours
        from services.scheduler import _now_moscow_naive

        report = await get_sos_report(report_id)
        if report is None or report_status(report) != STATUS_CLAIMED:
            return
        holder = report.get("claimed_by")
        if claimant_id is not None and holder != claimant_id:
            return  # заявка уже у другого — лесенка прежнего взявшего гаснет
        count = int(report.get("claimed_remind_count") or 0)
        if count >= CLAIMED_REMIND_MAX:
            return
        city = report.get("city")
        now = _now_moscow_naive()
        window = await sos_active_window(city)
        if window is None or now.date() > window[1]:
            return  # форум города закончился (или дата не задана) — не шлём и не ставим

        quiet = await quiet_hours.window_for_city(city)
        if quiet is not None and quiet_hours.is_quiet(now, *quiet):
            wake = quiet_hours.next_window_end(now, *quiet)
            if wake.date() <= window[1]:
                _schedule_claimed_reminder_at(report_id, holder, wake)
            return

        if not await advance_sos_claimed_remind(report_id, holder, count):
            return
        count += 1
        minutes = _minutes_in_work(report, now)

        import services.scheduler as scheduler_module

        bot = scheduler_module.get_bot()
        template = await get_setting_typed("sos_claimed_remind_text")
        text = await _translated_for(holder, template, id=report_id, minutes=minutes)
        try:
            await bot.send_message(holder, text)
        except Exception as e:
            logger.info("sos.claimed_reminder_job: не удалось написать claimant id=%s: %s", holder, e)

        if count < CLAIMED_REMIND_MAX:
            run_at = now + timedelta(minutes=CLAIMED_REMIND_DELAYS_MINUTES[count])
            if run_at.date() <= window[1]:
                _schedule_claimed_reminder_at(report_id, holder, run_at)
            return

        who = report.get("claimed_by_name") or "коллега"
        esc_template = await get_setting_typed("sos_claimed_escalation_text")
        esc_text = _fill(esc_template, id=report_id, who=who, minutes=minutes)
        await _send_escalation(bot, report, esc_text, alert_head=html_module.escape(esc_text))
    except Exception as e:
        logger.error("sos.claimed_reminder_job(%s) failed: %s: %s", report_id, type(e).__name__, e)


# ── Ревью 24.09 (находка 3, часть Б): «свежий» открытый SOS того же делегата — вместо
# повторной блокировки (старое поведение `sos_already_open_text` на КАЖДОМ повторном тапе)
# предлагает дополнить существующую заявку; «старый» открытый — новый SOS разрешён, с
# явной ссылкой в карточке на прежний (`prior_open_report_id`, см. `database.db.
# create_sos_report`/`render_card_text`).
DEFAULT_REOPEN_WINDOW_MINUTES = 10


def report_age_minutes(report: dict) -> float | None:
    stamp = _parse_stamp(report.get("created_at"))
    if stamp is None:
        return None
    return (msk_now() - stamp).total_seconds() / 60


def claim_status_parts(report: dict) -> tuple[str, str | None]:
    """`(шаблон, имя)` для перевода делегату (Часть А ревью 24.09: `sos_recent_followup_text`
    уходил сырой русской строкой, потому что `{claim_status}` собирался ЗДЕСЬ, ДО перевода
    шаблона) — шаблон переводится словарём (`services/i18n_form_manual.py::FORM_DEFAULT_EN`,
    ярус B), имя — собственное, НЕ участвует в переводе шаблона, подставляется ПОСЛЕ
    (`handlers/reg_i18n.py::tr_fmt`, тот же порядок «шаблон сначала», что везде в чате).
    `who is None` -> шаблон без плейсхолдера («ещё не взяли»); иначе — имя держателя ИЛИ
    переводимый фолбэк «коллега» (нет отображаемого имени у самого держателя в БД) — фолбэк
    тоже переводится вызывающим (`reg_i18n.tr_text` на `who`, дословное имя просто не найдётся
    в словаре и уйдёт как есть)."""
    if report.get("claimed_by") is not None:
        who = report.get("claimed_by_name") or "коллега"
        return "взял(а) {who}", who
    return "ещё не взяли", None


def claim_status_label(report: dict) -> str:
    """RU-версия (менеджерские экраны, где перевод не нужен) — тонкая обёртка над
    `claim_status_parts`, чтобы формула статуса считалась в одном месте."""
    template, who = claim_status_parts(report)
    return template.format(who=who) if who else template


# ── «✅ Решено» у орга закрывает режим «дописываю SOS» у делегата ────────────────────────────

async def close_delegate_collecting(bot, storage, report: dict) -> None:
    """Орг отметил заявку решённой: если делегат всё ещё в режиме «дописываю SOS» ПО ЭТОЙ
    заявке — режим закрываем (иначе его следующее сообщение ушло бы в тред решённой заявки), и
    пишем ему `sos_resolved_notify_text`. `storage` — FSM-хранилище диспетчера (aiogram кладёт
    его в данные хендлера как `fsm_storage`); ключ делегата — личка, chat_id == user_id.
    Режим по ДРУГОЙ заявке (делегат уже открыл новый SOS) не трогаем. Fail-soft: сбой не
    мешает самой отметке «Решено»."""
    tid = report.get("telegram_id")
    if not tid:
        return
    cleared = False
    try:
        if storage is not None:
            from aiogram.fsm.context import FSMContext
            from aiogram.fsm.storage.base import StorageKey
            from handlers.states import SosReport

            ctx = FSMContext(storage=storage, key=StorageKey(bot_id=bot.id, chat_id=tid, user_id=tid))
            if (await ctx.get_state() == SosReport.collecting.state
                    and (await ctx.get_data()).get("sos_collecting_report_id") == report["id"]):
                await ctx.clear()
                cleared = True
    except Exception as e:
        logger.error("sos.close_delegate_collecting(%s): FSM делегата не сброшен: %s", report.get("id"), e)
    try:
        from handlers import reg_i18n
        from services import i18n as i18n_service

        lang, tr_map = await i18n_service.context(tid)
        text = reg_i18n.tr_text(await get_setting_typed("sos_resolved_notify_text"), lang, tr_map)
        kb = None
        if cleared:  # вернуть главное меню вместо клавиатуры «Готово»
            from keyboards.builders import get_main_menu_kb
            kb = reg_i18n.tr_kb(await get_main_menu_kb(tid), lang, tr_map)
        await bot.send_message(tid, text, reply_markup=kb)
    except Exception as e:
        logger.info("sos.close_delegate_collecting: делегату %s не написать: %s", tid, e)
