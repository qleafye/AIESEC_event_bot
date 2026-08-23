"""One-shot backfill: перенос сохранённых ссылок на резюме на новый домен Nextcloud.

После переезда Nextcloud за Cloudflare Tunnel (docs/DEPLOY-DOMAIN.md) новые загрузки
сразу получают ссылки на `https://cloud.<домен>` — `services.nextcloud._file_link` читает
NEXTCLOUD_PUBLIC_URL в момент вызова. А в `users.resume_url` у старых делегатов лежат
адреса самоподписанного `https://<IP>:8443/...`. Этот скрипт меняет у них ТОЛЬКО префикс;
токен шары и имя файла в хвосте ссылки не трогаются.

Запускать на СЕРВЕРЕ (тот же .env / БД, что у бота), сначала сухим прогоном:

    python -m scripts.backfill_nextcloud_urls \
        --old-base https://1.2.3.4:8443 --new-base https://cloud.example.org --dry-run
    python -m scripts.backfill_nextcloud_urls \
        --old-base https://1.2.3.4:8443 --new-base https://cloud.example.org
    ... --limit 20   # первые 20 строк

Ни сети, ни Bot API, ни Nextcloud — чистая правка строк в БД. Сравнение строго по началу
строки (старый префикс содержит порт), никаких «умных» разборов URL. Пишет по одной
строке с try/except — одна битая строка не останавливает остальные. После боевого
прогона пересобрать основную вкладку таблицы из бота: колонка «Резюме (ссылка)»
собирается из `users.resume_url` при выгрузке.
"""
import argparse
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def swap_base(url: Optional[str], old_base: str, new_base: str) -> Optional[str]:
    """Return ``url`` with leading ``old_base`` replaced by ``new_base``.

    Pure string prefix swap: anything that does not start with ``old_base`` (other domain,
    ``None``, empty string) comes back unchanged. Trailing slashes on both bases are
    normalised so ``https://a/`` and ``https://a`` behave the same.
    """
    if not url:
        return url
    old = old_base.rstrip("/")
    new = new_base.rstrip("/")
    if not old or not url.startswith(old):
        return url
    return new + url[len(old):]


async def select_rows(db, old_base: str, limit: int = 0) -> list:
    """Return ``(telegram_id, resume_url)`` rows whose ``resume_url`` starts with ``old_base``.

    Takes an ALREADY-OPEN aiosqlite connection — testable without the bot config.
    """
    old = old_base.rstrip("/")
    sql = (
        "SELECT telegram_id, resume_url FROM users "
        "WHERE resume_url IS NOT NULL AND substr(resume_url, 1, ?) = ? "
        "ORDER BY telegram_id"
    )
    params: tuple = (len(old), old)
    if limit and limit > 0:
        sql += " LIMIT ?"
        params += (limit,)
    rows = await db.execute_fetchall(sql, params)
    return list(rows)


async def run(db, old_base: str, new_base: str, dry_run: bool = False, limit: int = 0) -> dict:
    """Apply the swap to every matching row. Returns ``{"found", "replaced", "skipped"}``.

    ``dry_run`` prints what WOULD change and writes nothing.
    """
    rows = await select_rows(db, old_base, limit)
    found = len(rows)
    replaced = skipped = 0
    for telegram_id, url in rows:
        try:
            new_url = swap_base(url, old_base, new_base)
            if new_url == url:
                skipped += 1
                continue
            if dry_run:
                print(f"WOULD {telegram_id}: {url} -> {new_url}")
                replaced += 1
                continue
            await db.execute(
                "UPDATE users SET resume_url=? WHERE telegram_id=?", (new_url, telegram_id)
            )
            await db.commit()
            replaced += 1
            print(f"OK {telegram_id} -> {new_url}")
        except Exception:
            skipped += 1
            logger.exception("failed to update %s — skipped", telegram_id)
    return {"found": found, "replaced": replaced, "skipped": skipped}


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Перенос ссылок на резюме (users.resume_url) со старого домена Nextcloud на новый."
    )
    parser.add_argument(
        "--old-base", required=True,
        help="Старый префикс ссылок как он лежит в БД, напр. https://1.2.3.4:8443",
    )
    parser.add_argument(
        "--new-base", required=True,
        help="Новый префикс, напр. https://cloud.example.org",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Показать, что изменится, ничего не записывая.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Ограничить число строк (0 = все).",
    )
    return parser.parse_args(argv)


def _print_summary(stats: dict, dry_run: bool):
    print()
    verb = "заменилось бы" if dry_run else "заменено"
    print(f"Найдено: {stats['found']} | {verb}: {stats['replaced']} | пропущено/ошибок: {stats['skipped']}")
    if not dry_run:
        print(
            "Готово. Чтобы колонка «Резюме (ссылка)» в Google-таблице подтянула новые адреса — "
            "пересобери основную вкладку из бота."
        )


async def main():
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()

    from database.db import _connect, init_db

    await init_db()  # idempotent — гарантирует колонку users.resume_url
    async with _connect() as db:
        stats = await run(db, args.old_base, args.new_base, args.dry_run, args.limit)
    _print_summary(stats, args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
