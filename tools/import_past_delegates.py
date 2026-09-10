"""Импорт делегатов прошлого события из Google-таблицы (10.09.2026).

Зачем: бот показывает менеджеру «🔁 Повторный: был(а) в <сезон>», когда у пришедшего делегата
уже есть строка в `users` с сезоном, отличным от текущего (`services/reg_finalize.py`). Штатный
путь — «📥 Импорт прошлого события» в админке, но он принимает ФАЙЛ БАЗЫ старого бота, а у нас
Google-таблица. Скрипт делает ровно то же, что визард: читает строки, оставляет только новых по
`telegram_id` и зовёт `database.db.bulk_insert_users_if_absent` (INSERT OR IGNORE — существующие
строки не трогаются ни в одной колонке, монеты/оплаты/рефералы не переносятся).

Ловушка этой конкретной таблицы: в одной вкладке лежат ДВА разных формата строк. Часть строк
пришла из бота (колонка «user id» = числовой telegram_id), часть — из гугл-формы другого года, у
неё колонки сдвинуты и telegram_id нет вовсе. Сопоставлять человека без telegram_id боту нечем
(`users.telegram_id` — первичный ключ), поэтому такие строки пропускаются и попадают в отчёт.
Поэтому же каждое поле проверяется по форме ЗНАЧЕНИЯ, а не по позиции колонки: телефон обязан
быть телефоном, статус — одним из трёх известных, дата — датой. Не прошло проверку — поле не
импортируется, строка при этом не теряется.

Запуск (на сервере, из каталога стека):
    docker exec -it youlead26-bot-1 python tools/import_past_delegates.py --sheet <ID> --season "YL 26/1"
    # по умолчанию --dry-run: ничего не пишет, только показывает отчёт
    docker exec -it youlead26-bot-1 python tools/import_past_delegates.py --sheet <ID> --season "YL 26/1" --apply
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter

import gspread

from config import config
from database.db import bulk_insert_users_if_absent, count_existing_telegram_ids

# Колонка «Статус» из таблицы -> код статуса в базе бота. «На рассмотрении» НЕ импортируется:
# такая строка попала бы в живую очередь модерации как новая заявка (`status='pending'`), и
# менеджер увидел бы прошлогоднего человека среди сегодняшних.
STATUS_MAP = {"Принято": "approved", "Отклонено": "rejected"}
STATUS_SKIP = {"На рассмотрении"}

TELEGRAM_ID_RE = re.compile(r"\d{5,15}")
USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{4,32}")
PHONE_RE = re.compile(r"\d{10,15}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?")
NAME_RE = re.compile(r"^[А-ЯЁ][а-яё-]+(\s+[А-ЯЁ][а-яё-]+){1,3}$")
COURSE_RE = re.compile(r"^(\d|college|graduated|school)$", re.IGNORECASE)

# Город из таблицы -> код города бота (`cities.code`). Всё, чего здесь нет, в `city` пишется
# как есть: это домашний город делегата (вопрос анкеты), а не город мероприятия, кодов он не
# требует. `event_city` импорт НЕ трогает вовсе — прошлый делегат не участник текущего события.
CITY_AS_IS = True


def cell(row: list[str], idx: int) -> str:
    return (row[idx] or "").strip() if 0 <= idx < len(row) else ""


def parse_rows(header: list[str], rows: list[list[str]]) -> tuple[list[dict], Counter]:
    """Каждое поле проверяется по форме значения. Строки без числового telegram_id и строки со
    статусом «На рассмотрении» отбрасываются со счётчиком причины."""
    idx = {h.strip(): n for n, h in enumerate(header)}
    skipped: Counter = Counter()
    out: dict[int, dict] = {}

    for row in rows:
        raw_id = cell(row, idx.get("user id", -1))
        if not TELEGRAM_ID_RE.fullmatch(raw_id):
            skipped["без telegram_id"] += 1
            continue

        raw_status = cell(row, idx.get("Статус", -1))
        if raw_status in STATUS_SKIP:
            skipped["статус «На рассмотрении»"] += 1
            continue
        status = STATUS_MAP.get(raw_status)
        if status is None:
            skipped[f"непонятный статус «{raw_status[:20]}»"] += 1
            continue

        data: dict = {"telegram_id": int(raw_id), "status": status}

        username = cell(row, idx.get("Телеграм", -1))
        if USERNAME_RE.fullmatch(username):
            data["username"] = username

        full_name = cell(row, idx.get("ФИО", -1))
        if NAME_RE.match(full_name):
            data["full_name"] = full_name

        phone = re.sub(r"\D", "", cell(row, idx.get("Phone", -1)))
        if PHONE_RE.fullmatch(phone):
            data["phone"] = phone

        city = cell(row, idx.get("Город", -1))
        if city and len(city) <= 60 and not city.isdigit():
            data["city"] = city

        university = cell(row, idx.get("Университет", -1))
        if university and len(university) <= 120 and not university.isdigit():
            data["university"] = university

        course = cell(row, idx.get("Курс", -1))
        if COURSE_RE.match(course):
            data["course"] = course

        specialty = cell(row, idx.get("Специальность", -1))
        if specialty and len(specialty) <= 200 and not specialty.isdigit():
            data["specialty"] = specialty

        reg_date = cell(row, idx.get("Дата", -1))
        if DATE_RE.match(reg_date):
            data["registration_date"] = reg_date.replace("T", " ")

        # Дубли telegram_id внутри таблицы: побеждает строка с бОльшим числом заполненных
        # полей, при равенстве — последняя (в таблице ниже обычно свежее).
        prev = out.get(data["telegram_id"])
        if prev is None or len(data) >= len(prev):
            out[data["telegram_id"]] = data

    return list(out.values()), skipped


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True, help="ID Google-таблицы")
    ap.add_argument("--tab", default=None, help="имя вкладки (по умолчанию первая)")
    ap.add_argument("--season", required=True, help="название прошлого сезона, например «YL 26/1»")
    ap.add_argument("--apply", action="store_true", help="писать в базу (без него — только отчёт)")
    args = ap.parse_args()

    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(args.sheet)
    ws = sh.worksheet(args.tab) if args.tab else sh.get_worksheet(0)
    values = ws.get_all_values()
    if not values:
        print("Таблица пустая.")
        return 1

    rows, skipped = parse_rows(values[0], values[1:])
    print(f"Таблица: {sh.title} / {ws.title}")
    print(f"Строк в таблице: {len(values) - 1}")
    print(f"Годных делегатов (уникальных по telegram_id): {len(rows)}")
    for reason, count in skipped.most_common():
        print(f"  пропущено, {reason}: {count}")

    filled = Counter()
    for r in rows:
        for k in r:
            filled[k] += 1
    print("Заполненность полей:", ", ".join(f"{k} {v}" for k, v in filled.most_common()))

    ids = [r["telegram_id"] for r in rows]
    existing = await count_existing_telegram_ids(ids)
    print(f"Уже есть в базе (их не трону): {existing}")
    print(f"Будет добавлено: {len(rows) - existing} с сезоном «{args.season}»")

    if not args.apply:
        print("\nЭто предпросмотр. Чтобы записать, добавь --apply")
        return 0

    inserted = await bulk_insert_users_if_absent(rows, args.season)
    print(f"\nДобавлено строк: {inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
