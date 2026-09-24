"""Phase 33 (delegate-card admin actions): dry-run обёртка `services.city_move.move_user_city`
для стенда — та же операция, что кнопка «🏙 Перевести в город» на карточке `/find`, но из
командной строки, чтобы проверить перенос на синтетическом делегате ДО того, как разработчик
скажет владельцу «кнопка готова».

По умолчанию — ТОЛЬКО просмотр (ничего не пишет ни в БД, ни в Google-таблицу): печатает текущую
карточку, резолвленный трек, реальные имена обеих вкладок и число строк делегата на каждой
(памятка `standalone-script-sheet-traps`: разовый скрипт обязан сверяться со СПИСКОМ реальных
вкладок, не угадывать/создавать по имени). `--apply` включает запись.

Запускать ИЗ РАБОЧЕГО КАТАЛОГА БОТА (там же, где `config.py`/`database/`/`cities.py`), тем же
`.env`, что у процесса бота:

    python -m scripts.move_city_dry_run 900800001 msk
    python -m scripts.move_city_dry_run 900800001 msk --status pending
    python -m scripts.move_city_dry_run 900800001 msk --apply
"""
import argparse
import asyncio
import logging

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Перевод делегата в другой город (дефолт — просмотр).")
    p.add_argument("telegram_id", type=int, help="telegram_id делегата")
    p.add_argument("city", help="код целевого города, напр. msk")
    p.add_argument("--apply", action="store_true", help="реально писать в БД/лист (по умолчанию — только показать)")
    p.add_argument(
        "--status", choices=["keep", "pending"], default="keep",
        help="'keep' — статус не трогать (по умолчанию), 'pending' — вернуть на модерацию",
    )
    p.add_argument("--by-admin", type=int, default=0, help="telegram_id администратора (для отчёта; по умолчанию 0)")
    return p.parse_args()


async def main() -> int:
    args = parse_args()

    print("=" * 78)
    print("move_city_dry_run.py — перевод делегата в другой город")
    print(f"telegram_id={args.telegram_id}  -> {args.city}  status_mode={args.status}")
    print(f"режим: {'ПРИМЕНИТЬ (--apply)' if args.apply else 'ПРОСМОТР (dry-run, ничего не меняю)'}")
    print("=" * 78)

    from cities import reload_cities
    from database.db import get_user
    from services.city_move import move_user_city

    # Памятка standalone-script-sheet-traps, пункт 3: справочник городов живёт в памяти
    # процесса бота, отдельный процесс без этого видит холодные дефолты из .env (без боевого
    # префикса вкладки).
    loaded = await reload_cities()
    print("Города (из БД): " + ", ".join(
        f"{c['code']}→вкладка «{c.get('tab_base') or '(главный лист)'}»" for c in loaded
    ))

    user = await get_user(args.telegram_id)
    if user is None:
        print(f"ОШИБКА: делегат с telegram_id={args.telegram_id} не найден в БД. Остановлено.")
        return 1

    print("\n--- Текущая карточка делегата ---")
    print(f"  ФИО: {user.get('full_name') or '-'}")
    print(f"  event_city (сырое значение): {user.get('event_city')!r}")
    print(f"  participant_type: {user.get('participant_type') or '-'}")
    print(f"  status: {user.get('status') or '-'}")

    report = await move_user_city(
        args.telegram_id, args.city,
        status_mode="pending" if args.status == "pending" else "keep",
        by_admin=args.by_admin,
        dry_run=not args.apply,
    )

    print("\n--- Отчёт ---")
    for key in ("ok", "error", "dry_run", "before", "after", "track_changed", "status_changed",
                "status_note", "db_changes", "sheet"):
        if key in report:
            print(f"  {key}: {report[key]!r}")

    if not args.apply:
        print("\nЭто был просмотр (dry-run). Ничего не изменено. Для применения добавьте --apply.")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
