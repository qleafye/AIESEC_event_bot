"""Разовый инструмент по инциденту потери резюме 05.09-10.09.2026 (`ef315f9`): в этом окне
бот молча терял резюме, приложенное ФАЙЛОМ, на последнем шаге анкеты — сама анкета не
виновата. 203 делегата сезона «YL 26/2» из этого окна остались без резюме в базе.

Что делает скрипт: находит делегатов, у которых НЕТ резюме ни в одной из
`database.db.RESUME_COLUMNS`, зарегистрированных в окне инцидента, сезона `event_season`, и
рассылает им одно из двух писем в зависимости от того, как их заявку в итоге решили:

  A) `status='rejected'` — заявку отклонили из-за пустого резюме, письмо просит переподать
     анкету через «Обновить анкету» (предыдущие ответы подставятся сами).
  B) `status='approved'` — заявку одобрили, письмо остаётся в силе, но просит дозагрузить
     резюме через deep-link `?start=edit`.
  `status='pending'` — заявка ещё в очереди модерации, письмо НЕ отправляется (менеджер
  разберёт её штатным путём, слать что-то преждевременно значило бы обогнать решение).

Рассылка идёт через штатный механизм бота (`database.db.create_broadcast` +
`services.broadcast_run.run_broadcast`), поэтому обе рассылки видны в истории рассылок бота
(`/scheduled`, карточка рассылки) как обычные — отличить их можно по `text_preview`
(`PREVIEW_A`/`PREVIEW_B` ниже).

ИДЕМПОТЕНТНОСТЬ: отдельной таблицы для «кому уже написали» не заводим — вместо этого
`already_sent_ids()` читает `broadcast_deliveries`, присоединённые к `broadcasts` с ТОЧНО
таким `text_preview` (`PREVIEW_A`/`PREVIEW_B`), которые создаёт этот же скрипт. Это переживает
рестарт контейнера (обе таблицы на диске) и не требует миграции — простейший вариант,
достаточно надёжный, потому что `text_preview` этого скрипта больше никто не пишет.

Запуск НА ПРОДЕ (внутри контейнера бота):

    docker exec youlead26-bot-1 python /app/tools/send_resume_recovery.py
    docker exec youlead26-bot-1 python /app/tools/send_resume_recovery.py --test-to <telegram_id>
    docker exec youlead26-bot-1 python /app/tools/send_resume_recovery.py --apply

ЧАСОВОЙ ПОЯС окна `--from`/`--to`: дефолты заданы в МОСКОВСКОМ времени — `registration_date`
на проде уже мигрирован квиком 260912-mcj (`PRAGMA user_version=1`).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows-консоль по умолчанию открывает stdout/stderr в cp1251 — кириллица в `--help`
# (argparse печатает докстринг модуля) и в отчёте падает `UnicodeEncodeError`.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import aiosqlite

from config import config
from services.timeutil import msk_now

DEFAULT_FROM = "2026-09-05 14:30:00"
DEFAULT_TO = "2026-09-11 00:30:00"

# Префиксы `text_preview` — единственный маркер «это письмо отправил этот скрипт» (см.
# докстринг модуля, раздел ИДЕМПОТЕНТНОСТЬ). Короче 80 символов (лимит `create_broadcast`,
# см. `tools/requeue_auto_approved.py`/`handlers/admin_broadcasts.py::bc_go`), поэтому не
# усечётся и `LIKE`/`IN` по нему надёжен.
PREVIEW_A = "[resume-recovery A]"
PREVIEW_B = "[resume-recovery B]"

TEXT_A = (
    "<b>Привет! 👋</b>\n\n"
    "Мы отклонили твою заявку на Юлид '26 по ошибке. Резюме не дошло до нас из-за сбоя в "
    "боте, анкета здесь ни при чём. Извини, что так вышло.\n\n"
    "Что сделать:\n"
    "– нажми /start и жми кнопку «Обновить анкету»;\n"
    "– прежние ответы подставятся сами;\n"
    "– на шаге резюме приложи файл или ссылку.\n\n"
    "Заявку рассмотрим заново в ближайшие дни 🧡\n\n"
    "Команда Юлид"
)

# `{username}` — подставляется из `bot.get_me()` (см. `render_text_b`), а не зашит литералом:
# юзернейм бота стенда/прода различается (RealTalk/SkillUp тоже могут переиспользовать этот
# скрипт), а сама разметка письма (кроме адреса) должна остаться байт-в-байт ТЗ.
TEXT_B_TEMPLATE = (
    "<b>Ты уже прошёл(-ла) отбор на Юлид '26, и это в силе 🎉</b>\n\n"
    "Но из-за сбоя в боте твоё резюме не сохранилось. Догрузи его, пожалуйста:\n"
    "– открой https://t.me/{username}?start=edit;\n"
    "– на каждом шаге жми «Оставить»;\n"
    "– на шаге резюме приложи файл или ссылку.\n\n"
    "Резюме мы передаём партнёрам форума, чтобы у тебя были карьерные возможности от них. "
    "Спасибо! 🧡💙\n\n"
    "Команда Юлид"
)


def render_text_b(username: str) -> str:
    return TEXT_B_TEMPLATE.format(username=username)


async def read_event_season() -> str | None:
    from settings_schema import get_setting_typed

    return await get_setting_typed("event_season")


async def already_sent_ids() -> set[int]:
    """`telegram_id`, которым УЖЕ уходило письмо ЭТОГО скрипта в прошлом прогоне (см.
    докстринг модуля, раздел ИДЕМПОТЕНТНОСТЬ) — джойн `broadcast_deliveries`/`broadcasts` по
    точному `text_preview`, без новой таблицы."""
    from database.db import _connect

    async with _connect() as db:
        async with db.execute(
            "SELECT DISTINCT bd.chat_id FROM broadcast_deliveries bd "
            "JOIN broadcasts b ON b.id = bd.broadcast_id "
            "WHERE b.text_preview IN (?, ?)",
            (PREVIEW_A, PREVIEW_B),
        ) as cursor:
            return {row[0] for row in await cursor.fetchall()}


def _resume_missing_sql(resume_columns: tuple[str, ...]) -> str:
    """Та же форма, что `database.db._resume_missing_fragment` (приватная, через границу
    модулей не импортируется) — собрана из ПУБЛИЧНОГО `RESUME_COLUMNS`, так что новая колонка
    резюме подхватится сама, без второй копии списка колонок."""
    return " AND ".join(
        f"COALESCE(TRIM({col}), '') IN ('', '-')" for col in resume_columns
    )


async def select_candidates(season: str, date_from: str, date_to: str) -> list[dict]:
    """Все делегаты сезона `season`, зарегистрированные в окне `[date_from, date_to]`, у
    которых НЕТ резюме ни в одной из `RESUME_COLUMNS`. Включает уже написанных ранее этим же
    скриптом — вызывающий код (`main`) сам отфильтровывает их через `already_sent_ids` для
    прозрачного отчёта «кого пропустили, потому что уже писали»."""
    from database.db import RESUME_COLUMNS, _connect

    missing = _resume_missing_sql(RESUME_COLUMNS)
    sql = f"""
        SELECT telegram_id, username, full_name, status, registration_date
        FROM users
        WHERE season = ?
          AND registration_date BETWEEN ? AND ?
          AND ({missing})
        ORDER BY registration_date
    """
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, (season, date_from, date_to)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


def bucket_by_status(rows: list[dict]) -> dict[str, list[dict]]:
    """rejected -> 'A', approved -> 'B', pending -> 'pending' (в отчёте, но не отправляется),
    что-то ещё (не ожидается в этом окне, но fail-safe, а не падение) -> 'other'."""
    buckets: dict[str, list[dict]] = {"A": [], "B": [], "pending": [], "other": []}
    for r in rows:
        status = r.get("status")
        if status == "rejected":
            buckets["A"].append(r)
        elif status == "approved":
            buckets["B"].append(r)
        elif status == "pending":
            buckets["pending"].append(r)
        else:
            buckets["other"].append(r)
    return buckets


async def build_bot():
    """Тот же приём Bot/failover-прокси, что `main.py` (D-01 инцидент 06.08 — прокси-цепочка
    с бэкапом), сжатый под разовый скрипт. Вынесена отдельной функцией модуля, чтобы тесты
    подменяли её целиком (`monkeypatch.setattr(tool, "build_bot", fake)`) вместо реального
    сетевого Bot."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from services.proxy_session import FailoverAiohttpSession, build_proxy_chain
    from settings_schema import SETTINGS_SCHEMA, get_setting_typed

    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    chain = build_proxy_chain(
        config.PROXY_URL.get_secret_value() if config.PROXY_URL else None,
        config.PROXY_URL_BACKUP.get_secret_value() if config.PROXY_URL_BACKUP else None,
    )
    session = None
    if chain != [None]:
        recheck_seconds = await get_setting_typed("proxy_recheck_seconds")
        if recheck_seconds is None:
            recheck_seconds = SETTINGS_SCHEMA["proxy_recheck_seconds"]["default"]
        connect_timeout = await get_setting_typed("proxy_connect_timeout")
        if connect_timeout is None:
            connect_timeout = SETTINGS_SCHEMA["proxy_connect_timeout"]["default"]
        dwell_seconds = await get_setting_typed("proxy_switch_dwell_seconds")
        if dwell_seconds is None:
            dwell_seconds = SETTINGS_SCHEMA["proxy_switch_dwell_seconds"]["default"]
        session = FailoverAiohttpSession(
            chain,
            recheck_seconds=int(recheck_seconds),
            connect_timeout=int(connect_timeout),
            dwell_seconds=int(dwell_seconds),
        )
    return Bot(token=config.BOT_TOKEN.get_secret_value(), default=default, session=session)


async def quiet_hours_block_reason(force: bool) -> str | None:
    """`None` — можно слать. Иначе — причина отказа (уже с текстом про `--force-quiet-hours`,
    если `force` не передан). Читает ГЛОБАЛЬНОЕ окно (city=None) — эта рассылка не привязана
    к конкретному городу делегата, в отличие от штатных напоминаний."""
    from services.quiet_hours import is_quiet, window_for_city

    window = await window_for_city(None)
    if window is None:
        return None
    start, end = window
    if not is_quiet(msk_now(), start, end):
        return None
    window_txt = f"{start:%H:%M}–{end:%H:%M} МСК"
    if force:
        print(f"Сейчас тихие часы ({window_txt}). Отправляю всё равно (--force-quiet-hours).")
        return None
    return (
        f"сейчас тихие часы ({window_txt}). Передайте --force-quiet-hours, "
        "чтобы отправить всё равно."
    )


def _print_rows(rows: list[dict], limit: int = 10) -> None:
    if not rows:
        print("  (пусто)")
        return
    for r in rows[:limit]:
        print(f"  {r['telegram_id']} @{r.get('username') or '-'} {r.get('registration_date') or '-'}")
    if len(rows) > limit:
        print(f"  ... и ещё {len(rows) - limit}")


async def run_test_to(test_id: int, bot_factory=build_bot) -> int:
    """Самотест владельца: оба письма ровно одному `test_id`, ни одна строка `broadcasts`/
    `broadcast_deliveries` не создаётся — штатная машина рассылок (`create_broadcast`/
    `run_broadcast`) здесь намеренно не участвует."""
    bot = await bot_factory()
    try:
        me = await bot.get_me()
        text_b = render_text_b(me.username)
        await bot.send_message(test_id, TEXT_A)
        await bot.send_message(test_id, text_b)
        print(f"Самотест: оба письма отправлены {test_id}. Строк в broadcasts не создано.")
    finally:
        await bot.session.close()
    return 0


async def _send_cohort(bot, admin_id: int, label: str, rows: list[dict], text: str, preview: str) -> None:
    from database.db import create_broadcast
    from services.broadcast_run import run_broadcast

    if not rows:
        print(f"\nКогорта {label}: отправлять некому.")
        return
    ids = [r["telegram_id"] for r in rows]
    bid = await create_broadcast(admin_id, preview, len(ids))

    async def send_one(chat_id):
        result = await bot.send_message(chat_id, text)
        return [result.message_id]

    outcome: dict = {}

    async def on_finish(status, delivered, blocked):
        outcome.update(status=status, delivered=delivered, blocked=blocked)

    await run_broadcast(bid, ids, send_one, on_finish=on_finish)
    print(
        f"\nКогорта {label}: рассылка #{bid} — доставлено {outcome.get('delivered')}, "
        f"недоступно {outcome.get('blocked')}"
    )


async def main(
    *,
    date_from: str = DEFAULT_FROM,
    date_to: str = DEFAULT_TO,
    apply: bool = False,
    only: str | None = None,
    admin_id: int | None = None,
    force_quiet_hours: bool = False,
    test_to: int | None = None,
    expect_a: int | None = None,
    expect_b: int | None = None,
    bot_factory=build_bot,
) -> int:
    if test_to is not None:
        return await run_test_to(test_to, bot_factory=bot_factory)

    season = await read_event_season()
    if not season:
        print("bot_settings.event_season не задан — выборка невозможна.", file=sys.stderr)
        return 1

    print(f"Сезон: {season!r}; окно (МСК): {date_from} .. {date_to}\n")

    candidates = await select_candidates(season, date_from, date_to)
    sent_ids = await already_sent_ids()
    fresh = [r for r in candidates if r["telegram_id"] not in sent_ids]
    already = [r for r in candidates if r["telegram_id"] in sent_ids]
    cohorts = bucket_by_status(fresh)

    print(f"A (отклонено, без резюме, будет отправлено): {len(cohorts['A'])}")
    _print_rows(cohorts["A"])
    print(f"\nB (одобрено, без резюме, будет отправлено): {len(cohorts['B'])}")
    _print_rows(cohorts["B"])
    print(f"\npending (в очереди — НЕ отправляется): {len(cohorts['pending'])}")
    _print_rows(cohorts["pending"])
    if cohorts["other"]:
        print(f"\nПрочие статусы (пропущены): {len(cohorts['other'])}")
        _print_rows(cohorts["other"])
    if already:
        print(f"\nУже получали письмо этого скрипта ранее (пропущено): {len(already)}")
        _print_rows(already)

    if expect_a is not None and len(cohorts["A"]) != expect_a:
        print(
            f"\nОШИБКА: ожидалось A={expect_a}, найдено {len(cohorts['A'])} — прерываю.",
            file=sys.stderr,
        )
        return 1
    if expect_b is not None and len(cohorts["B"]) != expect_b:
        print(
            f"\nОШИБКА: ожидалось B={expect_b}, найдено {len(cohorts['B'])} — прерываю.",
            file=sys.stderr,
        )
        return 1

    if not apply:
        print("\nЭто предпросмотр. Чтобы отправить, добавь --apply")
        return 0

    reason = await quiet_hours_block_reason(force_quiet_hours)
    if reason:
        print(f"\nОТКАЗ: {reason}", file=sys.stderr)
        return 1

    admin_id = admin_id if admin_id is not None else config.ADMIN_IDS[0]

    bot = await bot_factory()
    try:
        me = await bot.get_me()
        text_b = render_text_b(me.username)
        if only in (None, "A"):
            await _send_cohort(bot, admin_id, "A", cohorts["A"], TEXT_A, PREVIEW_A)
        if only in (None, "B"):
            await _send_cohort(bot, admin_id, "B", cohorts["B"], text_b, PREVIEW_B)
    finally:
        await bot.session.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--from", dest="date_from", default=DEFAULT_FROM,
        help=f"начало окна регистрации, МСК (по умолчанию «{DEFAULT_FROM}»)",
    )
    parser.add_argument(
        "--to", dest="date_to", default=DEFAULT_TO,
        help=f"конец окна регистрации, МСК (по умолчанию «{DEFAULT_TO}»)",
    )
    parser.add_argument("--apply", action="store_true", help="отправить письма (иначе только отчёт)")
    parser.add_argument("--only", choices=["A", "B"], default=None, help="отправить только одну когорту")
    parser.add_argument("--admin-id", type=int, default=None, help="автор рассылки (по умолчанию первый из ADMIN_IDS)")
    parser.add_argument(
        "--force-quiet-hours", action="store_true",
        help="отправить, даже если сейчас тихие часы",
    )
    parser.add_argument(
        "--test-to", type=int, default=None,
        help="прислать оба письма только этому telegram_id, ничего не пишет в broadcasts",
    )
    parser.add_argument("--expect-a", type=int, default=None, help="ожидаемый размер когорты A — прервать при несовпадении")
    parser.add_argument("--expect-b", type=int, default=None, help="ожидаемый размер когорты B — прервать при несовпадении")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            main(
                date_from=args.date_from,
                date_to=args.date_to,
                apply=args.apply,
                only=args.only,
                admin_id=args.admin_id,
                force_quiet_hours=args.force_quiet_hours,
                test_to=args.test_to,
                expect_a=args.expect_a,
                expect_b=args.expect_b,
            )
        )
    )
