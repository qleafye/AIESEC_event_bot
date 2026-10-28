"""Сертификаты после форума: кому какой положен, сборка PDF, рассылка «сертификат готов».

Кому: участник — тот, кого отметили на входе (отметка = был на форуме; одобренный, но не
пришедший сертификат не получает); волонтёр и амбассадор — по спискам @ников, которые менеджер
ведёт в админке. Человек может быть сразу в нескольких ролях — получит все свои сертификаты. Сертификат
компетенций — отмеченному на входе, если он закончил тест компетенций: шкалы — проценты по
последней попытке (`competency_percents`).

Даты: у шаблона есть вариант «оба дня» (`all`) и, если дизайнер нарисовал, варианты на один день
(`YYYY-MM-DD`). Участнику, отмеченному ровно в один день, уходит вариант этого дня, если он
загружен, иначе — «оба дня». Волонтёрам и амбассадорам — всегда «оба дня»: дней их работы бот
не знает.

Когда: менеджер ставит дату и время открытия. До него кнопка отвечает «будут доступны …». С
этого момента джоба `announce_job` раз в 5 минут пишет каждому, кому положен сертификат,
сообщение с кнопкой «🎓 Получить сертификат» — одному человеку один раз (`claim_announce`).
Сам PDF собирается по нажатию, после подтверждения имени: в анкете имена бывают «саша» и
«Иванова Анна Сергеевна», а сертификат печатают."""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import certificates_db as cdb
from database.certificates_db import KIND_AMBASSADOR, KIND_COMPETENCY, KIND_PARTICIPANT, KIND_VOLUNTEER, VARIANT_ALL
from services.forum.checkin import ENTRY_POINT
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

KIND_TITLES = {
    KIND_PARTICIPANT: "Сертификат участника",
    KIND_COMPETENCY: "Сертификат компетенций",
    KIND_VOLUNTEER: "Сертификат волонтёра",
    KIND_AMBASSADOR: "Сертификат амбассадора",
}
KIND_SHORT = {KIND_PARTICIPANT: "Участник", KIND_COMPETENCY: "Компетенции", KIND_VOLUNTEER: "Волонтёр",
              KIND_AMBASSADOR: "Амбассадор"}

# Шкалы шаблона компетенций сверху вниз — по началу названия компетенции из теста (в тесте
# «Критическое и аналитическое мышление», на макете «Критическое мышление»). Нет такой
# компетенции в тесте — шкала остаётся «0%».
COMPETENCY_ROWS = ("критич", "адаптив", "коммуникац", "команд", "эмоцион")
# Пример для «👁 Проверить на себе» и загрузки шаблона, если у админа нет своего результата.
SAMPLE_PERCENTS = [80, 50, 100, 60, 90]

OPEN_AT_FORMAT = "%Y-%m-%d %H:%M"
GET_CALLBACK = "cert_get"
GET_BUTTON = "🎓 Получить сертификат"
NAME_MAX = 60

_template_cache: dict[str, bytes] = {}


def get_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=GET_BUTTON, callback_data=GET_CALLBACK)]])


# ── когда открыта выдача ───────────────────────────────────────────────────────────────

def parse_open_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, OPEN_AT_FORMAT)
    except ValueError:
        return None


def human_open_at(raw: str | None) -> str | None:
    at = parse_open_at(raw)
    return at.strftime("%d.%m.%Y в %H:%M") if at else None


async def is_open(now: datetime | None = None) -> bool:
    at = parse_open_at(await cdb.get_open_at())
    return bool(at and (now or msk_now().replace(tzinfo=None)) >= at)


# ── дни форума для вариантов шаблона ───────────────────────────────────────────────────

async def forum_days() -> list[str]:
    """Дни форума основного города (`YYYY-MM-DD`) — под них менеджер может загрузить версии
    шаблона «на один день». Нет даты форума — пусто, остаётся только «оба дня»."""
    from datetime import timedelta

    from domain.cities import default_city_code
    from services.forum.forum_days import forum_window

    try:
        window = await forum_window(default_city_code())
    except Exception as e:
        logger.warning(f"certificates.forum_days: {e}")
        return []
    if not window:
        return []
    start, end = window
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def kind_variants(kind: str, variants: list[str]) -> list[str]:
    """Сертификат компетенций — один на человека, версий на день у него нет."""
    return [VARIANT_ALL] if kind == KIND_COMPETENCY else variants


def variant_label(variant: str) -> str:
    if variant == VARIANT_ALL:
        return "все дни"
    try:
        return datetime.strptime(variant, "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        return variant


# ── кому что положено ──────────────────────────────────────────────────────────────────

async def entitlements(telegram_id: int, username: str | None) -> list[tuple[str, str]]:
    """`[(вид, вариант дат)]` в порядке выдачи: участник, волонтёр, амбассадор."""
    out: list[tuple[str, str]] = []
    days = await cdb.entry_days(telegram_id, ENTRY_POINT)
    if days:
        out.append((KIND_PARTICIPANT, days[0] if len(days) == 1 else VARIANT_ALL))
        if await competency_percents(telegram_id) is not None:
            out.append((KIND_COMPETENCY, VARIANT_ALL))
    listed = await cdb.kinds_for_username(username, telegram_id)
    for kind in (KIND_VOLUNTEER, KIND_AMBASSADOR):
        if kind in listed:
            out.append((kind, VARIANT_ALL))
    return out


async def competency_percents(telegram_id: int) -> list[int] | None:
    """Проценты по шкалам шаблона (`COMPETENCY_ROWS`) из последней законченной попытки теста
    основного города. `None` — тест не пройден (или теста нет)."""
    from database import quiz_db
    from domain.cities import default_city_code
    from services.forum.quiz import result_lines

    try:
        quiz = await quiz_db.get_quiz_for_city(default_city_code())
        lines = await result_lines(telegram_id, quiz) if quiz else None
    except Exception as e:
        logger.warning(f"certificates.competency_percents {telegram_id}: {e}")
        return None
    if not lines:
        return None
    out = []
    for stem in COMPETENCY_ROWS:
        line = next((ln for ln in lines if (ln["competency"] or "").lower().startswith(stem)), None)
        out.append(int(line["percent"] or 0) if line else 0)
    return out


def pick_template(templates: dict[tuple[str, str], str], kind: str, variant: str) -> str | None:
    return templates.get((kind, variant)) or templates.get((kind, VARIANT_ALL))


async def default_name(telegram_id: int, fallback: str | None) -> str:
    saved = await cdb.get_name(telegram_id)
    if saved:
        return saved
    from database.db import get_user

    user = await get_user(telegram_id)
    name = ((user or {}).get("full_name") or "").strip()
    return name or (fallback or "").strip()


def clean_name(raw: str) -> str | None:
    """Имя для печати: одна строка, 2–60 символов, без лишних пробелов. `None` — не подходит."""
    name = " ".join((raw or "").split())
    if len(name) < 2 or len(name) > NAME_MAX or any(ch in name for ch in "<>@/\\"):
        return None
    return name


# ── сборка PDF ─────────────────────────────────────────────────────────────────────────

async def _template_bytes(bot, file_id: str) -> bytes:
    cached = _template_cache.get(file_id)
    if cached is None:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        cached = buf.getvalue()
        _template_cache[file_id] = cached
    return cached


async def build_pdf(bot, file_id: str, name: str, kind: str = KIND_PARTICIPANT,
                    percents: list[int] | None = None) -> bytes:
    """`percents` — шкалы сертификата компетенций; без них у этого вида — `SAMPLE_PERCENTS`."""
    from services.forum.certificate_render import COMPETENCY_NAME_BOX, NAME_BOX, render_certificate

    template = await _template_bytes(bot, file_id)
    if kind == KIND_COMPETENCY:
        return await asyncio.to_thread(render_certificate, template, name, COMPETENCY_NAME_BOX,
                                       percents or SAMPLE_PERCENTS)
    return await asyncio.to_thread(render_certificate, template, name, NAME_BOX)


def file_name(kind: str, name: str) -> str:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in " -").strip() or "участник"
    return f"{KIND_TITLES[kind]} — {safe}.pdf"


# ── рассылка «сертификат готов» ───────────────────────────────────────────────────────

async def recipients() -> list[int]:
    ids = set(await cdb.checked_in_ids(ENTRY_POINT))
    ids |= set((await cdb.ids_for_people()).values())
    return sorted(ids)


async def announce_text() -> str:
    from domain.settings.schema import get_setting_typed

    event = (await get_setting_typed("event_name") or "").strip()
    where = f" за {event}" if event else ""
    return (
        f"🎓 Твой сертификат{where} готов!\n\n"
        "Нажми кнопку ниже — бот покажет, как будет написано имя, и пришлёт PDF."
    )


async def announce_job() -> int:
    """Интервальная джоба: выдача открыта — написать «готов» всем, кому ещё не писали.
    Возвращает, скольким отправлено за этот проход."""
    if not await is_open():
        return 0
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

    from services import scheduler as _sched

    bot = _sched.get_bot()
    if bot is None:
        return 0
    text, kb, sent = await announce_text(), get_kb(), 0
    for telegram_id in await recipients():
        if not await cdb.claim_announce(telegram_id):
            continue
        try:
            try:
                await bot.send_message(telegram_id, text, reply_markup=kb)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await bot.send_message(telegram_id, text, reply_markup=kb)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            # заблокировал бота / чата нет — повтор не поможет; кнопка доступна командой /certificate
            logger.info(f"certificates.announce {telegram_id}: {type(e).__name__}")
        except asyncio.CancelledError:  # остановка бота посреди отправки
            await cdb.release_announce(telegram_id)
            raise
        except Exception as e:
            # сеть, 5xx, второй flood-limit: место освобождаем — следующий проход напишет снова
            logger.warning(f"certificates.announce {telegram_id}: {e}")
            await cdb.release_announce(telegram_id)
        await asyncio.sleep(0.05)
    if sent:
        logger.info(f"certificates.announce: отправлено {sent}")
    return sent
