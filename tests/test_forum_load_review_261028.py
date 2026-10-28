"""Ревью нагрузки и дня форума Юлид Москва (29–31.10): регрессии найденных дефектов.

- страховка утреннего повтора QR живёт весь цикл: на ~2400 человек он длиннее страховки, и
  APScheduler пропускал её срабатывание и удалял джобу — рестарт не дослал бы хвост;
- чек делегату о выдаче и отмене в магазине: название товара и магазина экранируются — бот по
  умолчанию шлёт в HTML, и «Стикер <3» или «M&M's» роняли отправку чека;
- точка «🛍 Магазин» в сканере не выглядит как пропуск на входе: плашка не зелёная, в заголовке
  и под точками сказано, что вход не отмечен."""
from __future__ import annotations

import asyncio
import html
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database import db, shop_db
from handlers.game import admin_shop as adm
from miniapp import telegram_api
from tests._paths import REPO_ROOT
from tests.test_miniapp_checkin_260924 import (
    BASE, GAME_MANAGER_ID, _grant_checkin_to_game_manager, _hdr, _insert_user, _qr, client_with,
)
from tests.test_shop_261028 import STAFF, UID, _env

TRICKY = "Стикер <3 & Ко"


def _run(coro):
    return asyncio.run(coro)


# ── страховка утреннего повтора QR живёт весь цикл, а не первые 30 минут ───────────────

def test_morning_guard_survives_cycle_longer_than_guard(monkeypatch):
    """Повтор на ~2400 человек идёт дольше `MORNING_GUARD_MINUTES`. Страховка срабатывала посреди
    цикла, APScheduler пропускал её (`max_instances=1` — тот же id ещё выполняется) и удалял
    date-джобу: рестарт после этого уже не дослал бы хвост QR."""
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from services import checkin_broadcast as cb
    from services import scheduler as sched
    from services.infra.timeutil import MOSCOW_TZ, msk_now

    jid = cb.morning_job_id(None)
    sends = []

    async def slow_repeat(city):
        sends.append(city)
        await asyncio.sleep(2.0)
        return {"sent": 0, "failed": 0, "total": 0}

    async def go():
        real = AsyncIOScheduler(jobstores={"default": MemoryJobStore()}, timezone=MOSCOW_TZ,
                                job_defaults={"misfire_grace_time": 86400, "coalesce": True})
        monkeypatch.setattr(sched, "get_scheduler", lambda: real)
        monkeypatch.setattr(cb, "_city_still_valid", AsyncMock(return_value=True))
        monkeypatch.setattr(cb, "_wrong_day", AsyncMock(return_value=False))
        monkeypatch.setattr(cb, "send_morning_repeat", slow_repeat)
        monkeypatch.setattr(cb, "MORNING_GUARD_MINUTES", 0.6 / 60)
        real.start()
        try:
            real.add_job(cb._run_morning_job, "date", run_date=msk_now(), args=[None], id=jid)
            await asyncio.sleep(1.4)  # страховка (0,6 с) уже сработала, цикл ещё идёт
            during = real.get_job(jid) is not None
            await asyncio.sleep(2.0)  # цикл закончился
            after = real.get_job(jid) is not None
        finally:
            real.shutdown(wait=False)
        return during, after

    during, after = _run(go())
    assert during, "страховка пропала посреди цикла — рестарт не дошлёт хвост"
    assert not after
    assert sends == [None]  # сработавшая страховка не запускает второй повтор параллельно


# ── чек делегату из магазина ───────────────────────────────────────────────────────────

def _state():
    store = {}

    async def get_data():
        return dict(store)

    async def update_data(**kw):
        store.update(kw)

    return store, SimpleNamespace(get_data=get_data, update_data=update_data, set_state=AsyncMock())


def _cb(data, bot):
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=STAFF), message=msg, answer=AsyncMock(), bot=bot)


def _receipt(bot) -> str:
    [call] = bot.send_message.await_args_list
    assert call.args[0] == UID
    return call.args[1]


def test_shop_receipt_escapes_item_title(tmp_path):
    _env(tmp_path)
    _run(db.set_setting("event_name", "Юлид & Co"))
    item = _run(shop_db.add_item(TRICKY, 10, None, None))
    store, state = _state()
    _run(adm.shop_pick(_cb(f"shp_p:{UID}:{item}", SimpleNamespace(send_message=AsyncMock())), state))
    bot = SimpleNamespace(send_message=AsyncMock())
    _run(adm.shop_go(_cb(f"shp_go:{store['shp_nonce']}", bot), state))
    text = _receipt(bot)
    assert html.escape(TRICKY) in text and "<3" not in text
    assert "Юлид &amp; Co" in text


def test_shop_undo_message_escapes_item_title(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item(TRICKY, 10, None, None))
    sale = _run(shop_db.sell(item, UID, STAFF))
    bot = SimpleNamespace(send_message=AsyncMock())
    _run(adm.shop_undo_go(_cb(f"shp_undogo:{sale['id']}", bot)))
    text = _receipt(bot)
    assert html.escape(TRICKY) in text and "<3" not in text


# ── точка «🛍 Магазин» в сканере ───────────────────────────────────────────────────────

def test_shop_point_carries_not_an_entry_note(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(shop_db.add_item("Шоппер", 150, 10, None))
    points = client.get(f"{BASE}/points", headers=_hdr(GAME_MANAGER_ID)).json()["points"]
    [shop] = [p for p in points if p["point"] == "shop"]
    assert "не отмечается" in shop["note"]


def test_shop_scan_says_entry_not_marked(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 2610281
    # не одобрен: на входе его бы не пропустили, а на точке магазина плашка не должна этого скрывать
    _run(_insert_user(uid, full_name="Петров Пётр", city="spb", status="rejected"))
    _run(shop_db.add_item("Шоппер", 150, 10, None))
    monkeypatch.setattr(telegram_api, "send_message", AsyncMock(return_value={}))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid, full_name="Петров Пётр"), "point": "shop"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "shop"
    assert "вход не отмечен" in body["hint"].lower()


def test_scanner_shop_plaque_is_not_green():
    js = (REPO_ROOT / "miniapp/static/js/screens/scanner.js").read_text(encoding="utf-8")
    assert 'shop: "success"' not in js
    assert 'shop: "warn"' in js
    assert "вход НЕ отмечен" in js
