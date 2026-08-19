"""One-shot backfill: re-upload OLD delegates' resumes to Nextcloud.

Delegates who registered BEFORE the Nextcloud integration have a stored resume
(Telegram `resume_file_id` OR `resume_text`) but an empty `resume_url`. This script
re-uploads each — files are re-downloaded from Telegram and PUT to Nextcloud, text
resumes are PUT as a .txt file — then writes the resulting deep-link into
`users.resume_url`. Files are named "<ФИО>_<username|id>".

Run on the SERVER (same .env / DB as the bot):

    python -m scripts.backfill_resumes            # process all candidates
    python -m scripts.backfill_resumes --dry-run  # list candidates, no network/writes
    python -m scripts.backfill_resumes --limit 20 # cap to first 20 candidates

Writes ONLY to the DB (resume_url). It never touches Google Sheets and never
starts bot polling. Fully fail-soft per user: one bad file is logged and skipped,
the batch continues. After it finishes, press the admin button «Пересобрать
таблицу» so the «Резюме (ссылка)» column fills in the Google sheet.
"""
import argparse
import asyncio
import logging
import os

import aiosqlite

from config import config
from database.db import init_db
from handlers.registration import _resume_file_stem
from services.nextcloud import upload_resume, upload_text_resume

logger = logging.getLogger(__name__)


async def select_pending_resumes(db) -> list:
    """Return (telegram_id, resume_file_id, resume_text, full_name, username) rows for
    delegates who have a stored resume (Telegram file OR text) but no Nextcloud URL yet.

    Takes an ALREADY-OPEN aiosqlite connection — importable/testable without a Bot.
    """
    rows = await db.execute_fetchall(
        "SELECT telegram_id, resume_file_id, resume_text, full_name, username FROM users "
        "WHERE (resume_file_id IS NOT NULL AND resume_file_id != '' "
        "       OR resume_text IS NOT NULL AND resume_text != '') "
        "AND (resume_url IS NULL OR resume_url = '')"
    )
    return list(rows)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill Nextcloud resume links for old delegates."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List candidate telegram_ids without any network call or DB write.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of candidates to process (0 = all).",
    )
    return parser.parse_args()


def _print_summary(total: int, success: int, failed: int):
    print()
    print(f"Кандидатов: {total} | залито: {success} | пропущено/ошибок: {failed}")
    print(
        "Готово. Резюме залиты в БД (resume_url). Чтобы колонка «Резюме (ссылка)» "
        "заполнилась в Google-таблице — нажми в админке кнопку «Пересобрать таблицу»."
    )


async def main():
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()

    # Idempotent — guarantees users.resume_url column exists.
    await init_db()

    # Feature guard: nowhere to upload / no way to build links → exit before any bot/network work.
    if (
        not config.NEXTCLOUD_WEBDAV_URL
        or not config.NEXTCLOUD_PUBLIC_URL
        or not config.NEXTCLOUD_FOLDER_SHARE_TOKEN
    ):
        print("Nextcloud не настроен (нужны WEBDAV_URL/PUBLIC_URL/FOLDER_SHARE_TOKEN) — выход")
        return

    limit = args.limit if args.limit and args.limit > 0 else 0

    async with aiosqlite.connect(config.DB_PATH) as db:
        candidates = await select_pending_resumes(db)
        if limit:
            candidates = candidates[:limit]

        total = len(candidates)

        # --dry-run: list telegram_ids + resume type only, no bot/network/writes.
        if args.dry_run:
            for telegram_id, resume_file_id, _resume_text, _full_name, _username in candidates:
                typ = "file" if resume_file_id else "text"
                print(f"WOULD process {telegram_id} ({typ})")
            _print_summary(total, 0, 0)
            return

        if not total:
            _print_summary(0, 0, 0)
            return

        # Real path — build the Bot exactly like main.py (same failover proxy chain).
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        from services.proxy_session import FailoverAiohttpSession, build_proxy_chain

        default = DefaultBotProperties(parse_mode=ParseMode.HTML)
        chain = build_proxy_chain(
            config.PROXY_URL.get_secret_value() if config.PROXY_URL else None,
            config.PROXY_URL_BACKUP.get_secret_value() if config.PROXY_URL_BACKUP else None,
        )
        # No proxy_session.set_alert_bot() call here on purpose — this is a one-shot script,
        # not a running bot; the module fails soft (one warning, then silent) with no bot set.
        session = (
            FailoverAiohttpSession(
                chain,
                recheck_seconds=config.PROXY_RECHECK_SECONDS,
                connect_timeout=config.PROXY_CONNECT_TIMEOUT,
            )
            if chain != [None]
            else None
        )
        bot = Bot(
            token=config.BOT_TOKEN.get_secret_value(),
            default=default,
            session=session,
        )

        success = 0
        failed = 0
        try:
            for telegram_id, resume_file_id, resume_text, full_name, username in candidates:
                try:
                    data = {
                        "full_name": full_name,
                        "username": username,
                        "telegram_id": telegram_id,
                    }
                    name = _resume_file_stem(data)

                    if resume_file_id:
                        # Derive the original extension. get_file can 400 if the file was
                        # deleted on Telegram's side — fall back to no extension.
                        try:
                            f = await bot.get_file(resume_file_id)
                            ext = os.path.splitext(f.file_path)[1]
                        except Exception:
                            logger.warning(
                                "get_file failed for %s — using no extension", telegram_id
                            )
                            ext = ""
                        url = await upload_resume(bot, resume_file_id, f"{name}{ext}")
                    else:
                        url = await upload_text_resume(resume_text, f"{name}.txt")

                    if url:
                        await db.execute(
                            "UPDATE users SET resume_url=? WHERE telegram_id=?",
                            (url, telegram_id),
                        )
                        await db.commit()
                        success += 1
                        print(f"OK {telegram_id} -> {url}")
                    else:
                        failed += 1
                        logger.error("upload returned None for %s — skipped", telegram_id)
                except Exception:
                    failed += 1
                    logger.exception("failed to process %s — skipped", telegram_id)
                await asyncio.sleep(0.5)
        finally:
            await bot.session.close()

        _print_summary(total, success, failed)


if __name__ == "__main__":
    asyncio.run(main())
