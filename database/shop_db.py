"""Магазин баллов («Юлид-шоп»): товары за баллы и выдачи на стойке.

- `shop_items` — витрина: название, цена в баллах, сколько всего и сколько осталось, лимит в
  одни руки. Менеджер заводит и правит кнопками в админке.
- `shop_sales` — каждая выдача: кто, что, за сколько, кто выдал, строка `coins` со списанием.
  Отмена ошибочной выдачи не удаляет строку, а ставит `undone_at` и пишет в `coins` возврат.
- `shop_config` — одна строка: открыта ли витрина у делегатов.

Баланс — по-прежнему `SUM(coins.delta)`: покупка — обычная строка `coins` с `source = 'shop'`,
её видно в «📜 Ручные начисления», в истории делегата и в выгрузках.

Выдача (`sell`) — одна транзакция `BEGIN IMMEDIATE`: проверка баланса, остатка и лимита,
списание, уменьшение остатка, запись выдачи. Два волонтёра на стойке, нажавшие «Выдать» одному
человеку одновременно, не потратят его баллы дважды: второй увидит уже новый баланс.

Бронь делегатом (`reserve`, пока менеджер включил «Бронь делегатом» — `shop_config.reserve`) —
та же строка `shop_sales`, но со `status = 'reserved'`: баллы списаны сразу теми же проверками,
что у стойки (общий `_charge`), товар ждёт на стойке. У брони код выдачи «Ю-NNNN» (уникален среди
действующих броней — его называют голосом) и случайный `pickup_token` — он уходит в QR, по нему
сканер находит бронь (4 цифры подбираемы, токен — нет). Выдача (`issue_reservation`) — атомарный
UPDATE `status = 'issued'`, второй тап ничего не выдаёт. Отмена брони, как отмена выдачи, строку
не удаляет: `undone_at` и возврат строкой `coins`."""
from __future__ import annotations

import re
import secrets

import aiosqlite

from database import db as _db
from services.infra.timeutil import msk_now

SOURCE_SHOP = "shop"
CODE_PREFIX = "Ю"
STATUS_RESERVED = "reserved"
STATUS_ISSUED = "issued"
_ACTIVE_RES = "status = 'reserved' AND undone_at IS NULL"


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS shop_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, price INTEGER NOT NULL, "
        "stock_total INTEGER, stock_left INTEGER, per_person INTEGER, "
        "active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS shop_sales ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL, "
        "title TEXT NOT NULL, price INTEGER NOT NULL, coin_id INTEGER, sold_by INTEGER NOT NULL, "
        "sold_at TEXT NOT NULL, undone_at TEXT, undone_by INTEGER)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_shop_sales_user ON shop_sales(telegram_id)")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS shop_config (id INTEGER PRIMARY KEY CHECK (id = 1), open INTEGER NOT NULL DEFAULT 0)"
    )
    # Бронь делегатом: старые строки — выдачи на стойке (`issued`), «когда/кто выдал» = продажа.
    await _db._ensure_column(db, "shop_sales", "status", "TEXT NOT NULL DEFAULT 'issued'")
    await _db._ensure_column(db, "shop_sales", "pickup_code", "TEXT")
    await _db._ensure_column(db, "shop_sales", "pickup_token", "TEXT")
    await _db._ensure_column(db, "shop_sales", "issued_at", "TEXT")
    await _db._ensure_column(db, "shop_sales", "issued_by", "INTEGER")
    await _db._ensure_column(db, "shop_config", "reserve", "INTEGER NOT NULL DEFAULT 0")
    await db.execute(
        "UPDATE shop_sales SET issued_at = sold_at, issued_by = sold_by WHERE issued_at IS NULL AND status = 'issued'"
    )
    await db.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_shop_sales_active_code ON shop_sales(pickup_code) WHERE {_ACTIVE_RES}"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_shop_sales_token ON shop_sales(pickup_token)")


def _stamp() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


# ── витрина открыта ────────────────────────────────────────────────────────────────────

async def is_open() -> bool:
    async with _db._connect() as db:
        async with db.execute("SELECT open FROM shop_config WHERE id = 1") as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


async def set_open(value: bool) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO shop_config (id, open) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET open = excluded.open",
            (1 if value else 0,),
        )
        await db.commit()


async def is_reserve_on() -> bool:
    """Включена ли «Бронь делегатом» (по умолчанию нет — у делегата витрина без «Взять»)."""
    async with _db._connect() as db:
        async with db.execute("SELECT reserve FROM shop_config WHERE id = 1") as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


async def set_reserve(value: bool) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO shop_config (id, reserve) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET reserve = excluded.reserve",
            (1 if value else 0,),
        )
        await db.commit()


# ── товары ─────────────────────────────────────────────────────────────────────────────

async def add_item(title: str, price: int, stock: int | None, per_person: int | None) -> int:
    async with _db._connect() as db:
        cur = await db.execute(
            "INSERT INTO shop_items (title, price, stock_total, stock_left, per_person, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, price, stock, stock, per_person, _stamp()),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_item(item_id: int) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM shop_items WHERE id = ?", (item_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_items(active_only: bool = False) -> list[dict]:
    where = "WHERE active = 1 " if active_only else ""
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT * FROM shop_items {where}ORDER BY active DESC, price, id") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def update_item(item_id: int, **fields) -> None:
    """Поля: title, price, per_person, active. Остаток — через `set_stock`."""
    allowed = {k: v for k, v in fields.items() if k in ("title", "price", "per_person", "active")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    async with _db._connect() as db:
        await db.execute(f"UPDATE shop_items SET {sets} WHERE id = ?", (*allowed.values(), item_id))
        await db.commit()


async def set_stock(item_id: int, stock: int | None) -> None:
    """Новое «сколько всего»: остаток пересчитывается с учётом уже выданного.
    `None` — без ограничения количества."""
    async with _db._connect() as db:
        if stock is None:
            await db.execute("UPDATE shop_items SET stock_total = NULL, stock_left = NULL WHERE id = ?", (item_id,))
        else:
            async with db.execute(
                "SELECT COUNT(*) FROM shop_sales WHERE item_id = ? AND undone_at IS NULL", (item_id,),
            ) as cur:
                sold = int((await cur.fetchone())[0])
            await db.execute(
                "UPDATE shop_items SET stock_total = ?, stock_left = ? WHERE id = ?",
                (stock, max(stock - sold, 0), item_id),
            )
        await db.commit()


async def delete_item(item_id: int) -> bool:
    """Удалить можно только товар без выдач; с выдачами — только снять с витрины."""
    async with _db._connect() as db:
        async with db.execute("SELECT COUNT(*) FROM shop_sales WHERE item_id = ?", (item_id,)) as cur:
            if int((await cur.fetchone())[0]):
                return False
        await db.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))
        await db.commit()
    return True


# ── выдача ─────────────────────────────────────────────────────────────────────────────

class SaleError(Exception):
    """Выдать нельзя; `human` — что сказать волонтёру на стойке."""

    def __init__(self, human: str):
        super().__init__(human)
        self.human = human


async def count_bought(telegram_id: int, item_id: int) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM shop_sales WHERE telegram_id = ? AND item_id = ? AND undone_at IS NULL",
            (telegram_id, item_id),
        ) as cur:
            return int((await cur.fetchone())[0])


async def _charge(db: aiosqlite.Connection, item_id: int, telegram_id: int, by: int, stamp: str, reason: str):
    """Проверки и списание — общие для выдачи на стойке и брони (внутри открытой транзакции
    `BEGIN IMMEDIATE`). Возвращает `(товар, баланс до списания, id строки coins)`."""
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM shop_items WHERE id = ?", (item_id,)) as cur:
        item = await cur.fetchone()
    if item is None or not item["active"]:
        raise SaleError("Этого товара уже нет на витрине.")
    if item["stock_left"] is not None and item["stock_left"] <= 0:
        raise SaleError(f"«{item['title']}» закончился.")
    if item["per_person"]:
        # действующие брони считаются наравне с выданным
        async with db.execute(
            "SELECT COUNT(*) FROM shop_sales WHERE telegram_id = ? AND item_id = ? AND undone_at IS NULL",
            (telegram_id, item_id),
        ) as cur:
            bought = int((await cur.fetchone())[0])
        if bought >= item["per_person"]:
            raise SaleError(f"«{item['title']}» — не больше {item['per_person']} в одни руки, уже выдано {bought}.")
    async with db.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM coins WHERE user_id = ?", (telegram_id,),
    ) as cur:
        balance = int((await cur.fetchone())[0])
    if balance < item["price"]:
        raise SaleError(f"Не хватает баллов: нужно {item['price']}, на счету {balance}.")
    cur = await db.execute(
        "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp, source) VALUES (?, ?, ?, ?, ?, ?)",
        (telegram_id, -item["price"], reason.format(title=item["title"]), by, stamp, SOURCE_SHOP),
    )
    coin_id = cur.lastrowid
    if item["stock_left"] is not None:
        await db.execute("UPDATE shop_items SET stock_left = stock_left - 1 WHERE id = ?", (item_id,))
    return item, balance, coin_id


async def sell(item_id: int, telegram_id: int, sold_by: int) -> dict:
    """Списать баллы и выдать товар одной транзакцией. Возвращает строку выдачи с полем
    `balance` — сколько осталось. Бросает `SaleError` с понятной причиной."""
    stamp = _stamp()
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            item, balance, coin_id = await _charge(db, item_id, telegram_id, sold_by, stamp, "Магазин: {title}")
            cur = await db.execute(
                "INSERT INTO shop_sales (item_id, telegram_id, title, price, coin_id, sold_by, sold_at, "
                "status, issued_at, issued_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, telegram_id, item["title"], item["price"], coin_id, sold_by, stamp,
                 STATUS_ISSUED, stamp, sold_by),
            )
            sale_id = cur.lastrowid
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return {
        "id": sale_id, "item_id": item_id, "title": item["title"], "price": item["price"],
        "telegram_id": telegram_id, "balance": balance - item["price"],
    }


# ── бронь делегатом ────────────────────────────────────────────────────────────────────

def _random_code_digits() -> int:
    """4 цифры кода брони (1000–9999). Отдельно — чтобы тест мог подменить."""
    return 1000 + secrets.randbelow(9000)


def _format_code(digits: int) -> str:
    return f"{CODE_PREFIX}-{digits}"


async def _free_code(db: aiosqlite.Connection) -> str:
    async with db.execute(f"SELECT pickup_code FROM shop_sales WHERE {_ACTIVE_RES} AND pickup_code IS NOT NULL") as cur:
        taken = {r[0] for r in await cur.fetchall()}
    for _ in range(50):
        code = _format_code(_random_code_digits())
        if code not in taken:
            return code
    for digits in range(1000, 10000):
        code = _format_code(digits)
        if code not in taken:
            return code
    raise SaleError("Свободных кодов брони нет — выдайте товар на стойке")


async def reserve(item_id: int, telegram_id: int) -> dict:
    """Делегат берёт товар сам: баллы списываются сразу, товар ждёт на стойке. Одна транзакция,
    те же проверки и тексты отказа, что у `sell`. Возвращает `{id, item_id, title, price,
    telegram_id, balance, code, token}`."""
    stamp = _stamp()
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("SELECT open, reserve FROM shop_config WHERE id = 1") as cur:
                cfg = await cur.fetchone()
            if not cfg or not cfg[0]:
                raise SaleError("Магазин сейчас закрыт.")
            if not cfg[1]:
                raise SaleError("Бронь сейчас выключена — товар выдают на стойке магазина.")
            item, balance, coin_id = await _charge(
                db, item_id, telegram_id, telegram_id, stamp, "Магазин: бронь «{title}»",
            )
            code = await _free_code(db)
            token = secrets.token_urlsafe(9)
            cur = await db.execute(
                "INSERT INTO shop_sales (item_id, telegram_id, title, price, coin_id, sold_by, sold_at, "
                "status, pickup_code, pickup_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, telegram_id, item["title"], item["price"], coin_id, telegram_id, stamp,
                 STATUS_RESERVED, code, token),
            )
            sale_id = cur.lastrowid
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return {
        "id": sale_id, "item_id": item_id, "title": item["title"], "price": item["price"],
        "telegram_id": telegram_id, "balance": balance - item["price"], "code": code, "token": token,
    }


_SALE_WITH_USER = (
    "SELECT s.*, u.full_name, u.username FROM shop_sales s LEFT JOIN users u ON u.telegram_id = s.telegram_id "
)


async def _sale_with_user(db: aiosqlite.Connection, where: str, args: tuple) -> dict | None:
    db.row_factory = aiosqlite.Row
    async with db.execute(_SALE_WITH_USER + where, args) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def cancel_reservation(sale_id: int, by: int, owner_id: int | None = None) -> dict | None:
    """Отменить действующую бронь: баллы — строкой `coins` назад, остаток пересчитан. `None` —
    брони нет, она уже выдана/отменена или (при `owner_id`) чужая. Возвращает строку брони
    с полем `balance` после возврата."""
    stamp = _stamp()
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            sale = await _sale_with_user(db, "WHERE s.id = ? AND s.status = 'reserved' AND s.undone_at IS NULL", (sale_id,))
            if sale is None or (owner_id is not None and int(sale["telegram_id"]) != int(owner_id)):
                await db.rollback()
                return None
            await db.execute(
                "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp, source) VALUES (?, ?, ?, ?, ?, ?)",
                (sale["telegram_id"], sale["price"], f"Магазин: отмена брони «{sale['title']}»", by, stamp, SOURCE_SHOP),
            )
            await db.execute("UPDATE shop_sales SET undone_at = ?, undone_by = ? WHERE id = ?", (stamp, by, sale_id))
            await db.execute(
                "UPDATE shop_items SET stock_left = MAX(stock_total - (SELECT COUNT(*) FROM shop_sales "
                "WHERE item_id = shop_items.id AND undone_at IS NULL), 0) "
                "WHERE id = ? AND stock_left IS NOT NULL AND stock_total IS NOT NULL",
                (sale["item_id"],),
            )
            async with db.execute("SELECT COALESCE(SUM(delta), 0) FROM coins WHERE user_id = ?", (sale["telegram_id"],)) as cur:
                balance = int((await cur.fetchone())[0])
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return {**sale, "undone_at": stamp, "undone_by": by, "balance": balance}


async def issue_reservation(sale_id: int, by: int) -> dict | None:
    """Выдать бронь на стойке. Атомарно: второй одновременный тап получает `None`."""
    stamp = _stamp()
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                f"UPDATE shop_sales SET status = 'issued', issued_at = ?, issued_by = ? WHERE id = ? AND {_ACTIVE_RES}",
                (stamp, by, sale_id),
            )
            if cur.rowcount == 0:
                await db.rollback()
                return None
            sale = await _sale_with_user(db, "WHERE s.id = ?", (sale_id,))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return sale


_CODE_RE = re.compile(r"\s*(?:[ЮюUuYy]\s*[-–—]?\s*)?(\d{4})\s*")


def parse_code(raw: str | None) -> str | None:
    """«Ю-4821», «ю 4821», «Ю4821», «U-4821», «4821» -> «Ю-4821»; чужой формат -> None."""
    m = _CODE_RE.fullmatch(raw or "")
    return f"{CODE_PREFIX}-{m.group(1)}" if m else None


async def find_by_code(code: str) -> dict | None:
    """Действующая бронь по коду, который делегат назвал на стойке."""
    norm = parse_code(code)
    if norm is None:
        return None
    async with _db._connect() as db:
        return await _sale_with_user(db, "WHERE s.pickup_code = ? AND s.status = 'reserved' AND s.undone_at IS NULL", (norm,))


async def find_by_token(token: str) -> dict | None:
    """Бронь по токену из QR — в любом статусе (сканер объясняет, что с ней)."""
    if not token:
        return None
    async with _db._connect() as db:
        return await _sale_with_user(db, "WHERE s.pickup_token = ? ORDER BY s.id DESC LIMIT 1", (token,))


async def list_user_reservations(telegram_id: int) -> list[dict]:
    """Действующие брони делегата — «🎫 Мои брони» и карточка на стойке."""
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            _SALE_WITH_USER + "WHERE s.telegram_id = ? AND s.status = 'reserved' AND s.undone_at IS NULL ORDER BY s.id",
            (telegram_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_sale(sale_id: int) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM shop_sales WHERE id = ?", (sale_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def undo_sale(sale_id: int, by: int) -> dict | None:
    """Отменить ошибочную выдачу: баллы вернуть строкой `coins`, остаток +1. `None` — уже
    отменена или не найдена (повторное нажатие ничего не делает)."""
    stamp = _stamp()
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM shop_sales WHERE id = ?", (sale_id,)) as cur:
                sale = await cur.fetchone()
            if sale is None or sale["undone_at"]:
                await db.rollback()
                return None
            await db.execute(
                "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp, source) VALUES (?, ?, ?, ?, ?, ?)",
                (sale["telegram_id"], sale["price"], f"Магазин: отмена «{sale['title']}»", by, stamp, SOURCE_SHOP),
            )
            await db.execute(
                "UPDATE shop_sales SET undone_at = ?, undone_by = ? WHERE id = ?", (stamp, by, sale_id),
            )
            # Остаток — от «сколько всего» минус действующие выдачи, а не +1: после урезанного
            # `set_stock` (выдали 3, «всего» стало 2) +1 дал бы выдать сверх «всего».
            await db.execute(
                "UPDATE shop_items SET stock_left = MAX(stock_total - (SELECT COUNT(*) FROM shop_sales "
                "WHERE item_id = shop_items.id AND undone_at IS NULL), 0) "
                "WHERE id = ? AND stock_left IS NOT NULL AND stock_total IS NOT NULL",
                (sale["item_id"],),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return dict(sale)


async def list_sales(limit: int = 20, offset: int = 0) -> list[dict]:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.*, u.full_name, u.username FROM shop_sales s LEFT JOIN users u ON u.telegram_id = s.telegram_id "
            "ORDER BY s.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def sales_stats() -> dict:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(status = 'issued'), 0), COALESCE(SUM(price), 0), "
            "COALESCE(SUM(status = 'reserved'), 0) FROM shop_sales WHERE undone_at IS NULL"
        ) as cur:
            count, spent, reserved = await cur.fetchone()
    return {"count": int(count), "spent": int(spent), "reserved": int(reserved)}


# ── рейтинг баллов ─────────────────────────────────────────────────────────────────────

async def leaderboard(limit: int, offset: int = 0) -> list[dict]:
    """Все, у кого есть хоть одна строка баллов, по убыванию баланса."""
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT c.user_id AS telegram_id, SUM(c.delta) AS balance, "
            "SUM(CASE WHEN c.source = 'shop' THEN -c.delta ELSE 0 END) AS spent, u.full_name, u.username "
            "FROM coins c LEFT JOIN users u ON u.telegram_id = c.user_id "
            "GROUP BY c.user_id ORDER BY balance DESC, u.full_name LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def leaderboard_count() -> int:
    async with _db._connect() as db:
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM coins") as cur:
            return int((await cur.fetchone())[0])


async def list_user_sales(telegram_id: int) -> list[dict]:
    """Выданное (не отменённое и не бронь) одному человеку — блок «Ты уже получил(а)» витрины."""
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM shop_sales WHERE telegram_id = ? AND undone_at IS NULL AND status = 'issued' ORDER BY id",
            (telegram_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
