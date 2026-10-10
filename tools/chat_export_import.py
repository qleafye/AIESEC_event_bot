"""Импорт экспорта Telegram Desktop в историю живого рейтинга чата (chat_messages / chat_reactions).

Зачем. Живой учёт пишет журнал только с момента, когда бот стал его вести; всё, что было в
чате раньше, есть только в ручном экспорте (Telegram Desktop → чат → ⋮ → «Экспорт истории
чата» → Machine-readable JSON). Этот тул переносит такой result.json в те же таблицы, из
которых считает дашборд, — и рейтинг города начинается с первого дня чата, а не с выката.

Запуск — внутри контейнера бота, СНАЧАЛА пробный прогон (он ничего не пишет):
    docker compose exec bot python tools/chat_export_import.py /app/data/result.json --db /app/data/forum.db
    docker compose exec bot python tools/chat_export_import.py /app/data/result.json --db /app/data/forum.db --apply

Флаги:
    --chat-id -100…  чат в терминах Bot API, если id из экспорта не тот (по умолчанию -100<id>)
    --apply          записать (без него — только отчёт, что было бы записано)
    --force          писать в чат, который не привязан ни к одному городу

Правила — те же, что у живого учёта (handlers/group_chat.py) и тула статистики:
- текст НЕ пишется, только длина; у пересланного чужого текста длина 0;
- ответ на корень темы (сервисное сообщение) — не ответ; ответ на пост канала — цель без
  автора-человека; сообщения анонимного админа (от имени самой группы) пропускаются;
- пост канала — is_channel_post=1, автор = отрицательный id канала (-100<id>);
- реакции: известные дарители (recent) — строки chat_reactions, остаток count − известных —
  reactions_extra (Telegram хранит неполный список дарителей); кастомный эмодзи без
  document_id получает ключ «export:<позиция>»;
- время — Москва по date_unixtime; поле date в экспорте — локальное время того, кто выгружал,
  поэтому сообщение без date_unixtime пропускается (их число — в отчёте);
- срок хранения (chat_rating_retention_days, по умолчанию 180 дней): строки старше него
  суточная чистка бота удалила бы сразу — пробный прогон пишет, сколько таких, --apply их
  не записывает;
- ников в экспорте нет: у авторов и дарителей реакций сохраняется имя (первое слово поля
  from) в chat_usernames — только если бот ещё не знает его сам; живые ники не трогаются.
Повторный запуск ничего не добавляет: INSERT OR IGNORE по (чат, id сообщения); живые строки с
тем же id не трогаются (и их реакции тоже). Разрыв между датой экспорта и началом живого учёта
не заполняется — выгрузите экспорт прямо перед импортом, перекрытие безопасно.

Только stdlib; настройки бота читаются из bot_settings напрямую (реестр бота тут не нужен).
Коды выхода: 0 — готово (или пробный прогон); 1 — плохой вход, нет таблиц или отказ.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Вся логика — services/chat_export_import.py (она же под кнопкой «📥 Загрузить историю чата» в
# боте). Имена реэкспортируются: тесты и внешние скрипты обращаются к ним как к атрибутам тула.
from services.chat_export_import import (  # noqa: E402,F401 — после бутстрапа sys.path
    ExportError,
    apply_plan,
    bound_city,
    build_rows,
    make_plan,
    retention_days,
)
from tools.chat_export_stats import load_export  # noqa: E402


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Импорт экспорта Telegram Desktop (result.json) в историю рейтинга чата.",
    )
    ap.add_argument("export", help="путь к result.json (Machine-readable JSON)")
    ap.add_argument("--db", default="data/forum.db", help="forum.db бота, по умолчанию data/forum.db")
    ap.add_argument("--chat-id", type=int, default=None, help="id чата в Bot API (-100…)")
    ap.add_argument("--apply", action="store_true", help="записать; без флага — пробный прогон")
    ap.add_argument("--force", action="store_true", help="писать в чат, не привязанный к городу")
    return ap


def _err(text: str) -> int:
    print(text, file=sys.stderr)
    return 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_arg_parser().parse_args(argv)

    try:
        data = load_export(args.export)
    except ExportError as e:
        return _err(str(e))

    chat_id = args.chat_id
    if chat_id is None:
        try:
            chat_id = int(f"-100{int(data.get('id'))}")
        except (TypeError, ValueError):
            return _err("В экспорте нет id чата — укажите его явно: --chat-id -100…")

    if not Path(args.db).is_file():
        return _err(f"Не нашёл базу «{args.db}». Укажите путь к forum.db бота: --db …")
    try:
        conn = sqlite3.connect(args.db, timeout=30)
    except sqlite3.Error as e:
        return _err(f"Не открыл базу «{args.db}»: {e}")
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"chat_messages", "chat_reactions", "bot_settings"} <= tables:
            return _err(
                "В базе нет таблиц chat_messages / chat_reactions — это старая версия бота. "
                "Обновите и перезапустите бота (таблицы создаются при старте), потом повторите."
            )

        plan = make_plan(conn, data, chat_id)
        is_bound, city, stats = plan["is_bound"], plan["city"], plan["stats"]
        messages, reactions = plan["messages"], plan["reactions"]
        keep_days, too_old = plan["keep_days"], plan["too_old"]

        print(f"Чат: {data.get('name') or 'без названия'} → chat_id {chat_id}")
        if is_bound:
            print(f"Привязан к городу: {city if city else 'общий чат (без города)'}")
        else:
            print("Чат не привязан ни к одному городу.")
        days = plan["days"]
        print(f"Период: {days[0]} — {days[-1]}" if days else "Период: сообщений нет")
        print(f"Сообщений в экспорте: {len(messages)} (уже есть в базе: {plan['already']})")
        print(f"Из них ответов людям/постам: {sum(1 for m in messages if m['reply_to_message_id'])}")
        print(f"Постов канала: {sum(m['is_channel_post'] for m in messages)}")
        print(f"Реакций с известным автором: {len(reactions)}; "
              f"без автора (в reactions_extra): {sum(m['reactions_extra'] for m in messages)}")
        if stats["no_unixtime"]:
            print(f"Пропущено сообщений без date_unixtime: {stats['no_unixtime']} (поле date — "
                  "локальное время того, кто выгружал; выгрузите экспорт заново в Telegram Desktop)")
        if too_old:
            print(f"Сообщений старше срока хранения ({keep_days} дн.): {too_old} — бот удалил бы "
                  "их первой же суточной чисткой, поэтому при --apply они не записываются. "
                  "Нужна история длиннее — увеличьте «Сколько дней хранить историю чата» в боте "
                  "и повторите импорт.")
        print("Ников в экспорте нет: у тех, кого бот ещё не видел в чате, на дашборде будет "
              "имя из Telegram (первое слово), ник появится, когда человек напишет в чат.")

        if not is_bound and not args.force:
            return _err(
                f"Чат {chat_id} не привязан ни к одному городу в настройках бота — скорее всего, "
                "id не тот. Проверьте --chat-id (чат в боте: «🔧 Управление» → чат делегатов) "
                "или добавьте --force, если импорт нужен именно сюда."
            )
        if not args.apply:
            print("Пробный прогон: ничего не записано. Чтобы записать — повторите с --apply.")
            return 0

        added_messages, added_reactions = apply_plan(conn, plan)
        print(f"Записано. Новых сообщений: {added_messages}, новых реакций: {added_reactions}.")
        if too_old:
            print(f"Старше срока хранения не записаны: {too_old}.")
        return 0
    except sqlite3.Error as e:
        return _err(f"Ошибка базы: {e}. Ничего не записано.")
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
