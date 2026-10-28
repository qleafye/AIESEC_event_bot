"""Ревью работ 27–28.10 к Юлиду Москва: регрессии найденных дефектов.

- рассылка «сертификат готов» не теряет человека при сетевом сбое отправки;
- «✅ Да, прислать» снимает ожидание ввода имени;
- имя ложится в повёрнутую страницу и в видимую область (CropBox) шаблона;
- удаление шаблона сертификата — только после подтверждения;
- отмена выдачи в магазине не поднимает остаток выше «сколько всего»;
- ручной поиск на точке «🛍 Магазин» в сканере не ставит отметку входа;
- карточка QR: длинный заголовок и длинное слово в имени не вылезают за край."""
from __future__ import annotations

import asyncio
import io
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject, RectangleObject

from database import certificates_db as cdb
from database import shop_db
from services import scheduler as sched
from services.forum import certificates as certs
from services.forum.certificate_render import NAME_BOX, render_certificate
from tests.test_certificates_261028 import UID, _callback, _checkin, _template, _use_db


def _run(coro):
    return asyncio.run(coro)


# ── рассылка «сертификат готов» ────────────────────────────────────────────────────────

class _FlakyBot:
    def __init__(self, errors):
        self.errors = list(errors)
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(chat_id)


def _open_with_recipient(tmp_path, monkeypatch, bot):
    _use_db(tmp_path)
    _checkin(UID, "2026-10-30")
    _run(cdb.set_open_at("2020-01-01 00:00"))
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(certs.asyncio, "sleep", AsyncMock())


def test_announce_network_failure_retried_on_next_pass(tmp_path, monkeypatch):
    bot = _FlakyBot([TelegramNetworkError(method=None, message="timeout")])
    _open_with_recipient(tmp_path, monkeypatch, bot)
    assert _run(certs.announce_job()) == 0
    assert _run(certs.announce_job()) == 1  # не потерян: следующий проход дописал
    assert _run(certs.announce_job()) == 0  # и только один раз
    assert bot.sent == [UID]


def test_announce_blocked_bot_not_retried(tmp_path, monkeypatch):
    bot = _FlakyBot([TelegramForbiddenError(method=None, message="blocked")])
    _open_with_recipient(tmp_path, monkeypatch, bot)
    assert _run(certs.announce_job()) == 0
    assert _run(certs.announce_job()) == 0
    assert bot.sent == []


# ── делегат: ожидание имени ────────────────────────────────────────────────────────────

def test_send_clears_name_input_state(tmp_path):
    from handlers.forum import certificates as h

    _use_db(tmp_path)
    _checkin(UID, "2026-10-30")
    _run(cdb.set_open_at("2020-01-01 00:00"))
    _run(cdb.set_template("participant", "all", "FILE", 1))

    class _Bot:
        async def download(self, file_id, destination):
            destination.write(_template())

    state = SimpleNamespace(clear=AsyncMock())
    cb = _callback("cert_ok")
    _run(h.certificate_send(cb, _Bot(), state))
    state.clear.assert_awaited()  # «✏️ Исправить» → «✅ Да» на старом сообщении: имя больше не ждём
    assert cb.message.answer_document.await_count == 1


# ── рендер PDF ─────────────────────────────────────────────────────────────────────────

def _name_cm(pdf: bytes) -> tuple[float, float, float, float]:
    page = PdfReader(io.BytesIO(pdf)).pages[0]
    data = page.get_contents().get_data().decode("latin-1")
    w, h, x, y = map(float, re.findall(r"q ([\d.]+) 0 0 ([\d.]+) ([\d.-]+) ([\d.-]+) cm /CertName Do Q", data)[-1])
    return w, h, x, y


def _rotated_template() -> bytes:
    """A4 альбомом так, как его часто экспортируют редакторы: книжный MediaBox + /Rotate 90."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    page[NameObject("/Rotate")] = NumberObject(90)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_render_rotated_page_keeps_name_horizontal():
    pdf = render_certificate(_rotated_template(), "Анна Иванова")
    page = PdfReader(io.BytesIO(pdf)).pages[0]
    # Как видит человек: альбомный лист 842×595, имя — горизонтальная полоса по центру.
    assert int(page.get("/Rotate", 0)) % 360 == 0
    assert round(float(page.mediabox.width)) == 842 and round(float(page.mediabox.height)) == 595
    w, h, x, y = _name_cm(pdf)
    assert abs(w - 842 * NAME_BOX[2]) < 1 and w > h
    assert abs((x + w / 2) - 421) < 1


def test_render_uses_visible_cropbox():
    writer = PdfWriter()
    page = writer.add_blank_page(width=1000, height=700)
    page.cropbox = RectangleObject([100, 50, 942, 645])  # видимая область 842×595
    out = io.BytesIO()
    writer.write(out)
    w, h, x, y = _name_cm(render_certificate(out.getvalue(), "Анна Иванова"))
    assert abs(w - 842 * NAME_BOX[2]) < 1
    assert abs((x + w / 2) - (100 + 421)) < 1
    assert abs((y + h / 2) - (645 - 595 * NAME_BOX[1])) < 1


def test_render_plain_template_position_unchanged():
    w, h, x, y = _name_cm(render_certificate(_template(), "Анна Иванова"))
    assert abs(w - 842 * NAME_BOX[2]) < 1
    assert abs((x + w / 2) - 421) < 1
    assert abs((y + h / 2) - (595 - 595 * NAME_BOX[1])) < 1


# ── админка: удаление шаблона ──────────────────────────────────────────────────────────

def test_template_delete_asks_confirmation_first(tmp_path):
    from handlers.forum import admin_certificates as adm

    _use_db(tmp_path)
    _run(cdb.set_template("participant", "all", "FILE", 1))
    message = SimpleNamespace(answer=AsyncMock())
    cb = SimpleNamespace(data="certadm_tpldel:participant:all", message=message, answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    _run(adm.certadm_tpldel(cb, state))
    assert ("participant", "all") in _run(cdb.list_templates())  # ещё не удалён
    kb = message.answer.call_args.kwargs["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "certadm_tpldel:participant:all:go" in datas

    cb = SimpleNamespace(data="certadm_tpldel:participant:all:go", message=message, answer=AsyncMock())
    _run(adm.certadm_tpldel(cb, state))
    assert ("participant", "all") not in _run(cdb.list_templates())


# ── магазин: остаток после отмены ──────────────────────────────────────────────────────

def test_undo_after_stock_cut_keeps_stock_within_total(tmp_path):
    from tests.test_shop_261028 import STAFF, UID as SHOP_UID, _env

    _env(tmp_path)
    item = _run(shop_db.add_item("Стикер", 10, 5, None))
    sales = [_run(shop_db.sell(item, SHOP_UID, STAFF)) for _ in range(3)]
    _run(shop_db.set_stock(item, 2))  # выдали 3, «всего» урезали до 2 — остаток 0
    assert _run(shop_db.get_item(item))["stock_left"] == 0
    _run(shop_db.undo_sale(sales[0]["id"], STAFF))
    # действующих выдач 2 из 2 — выдавать больше нечего
    assert _run(shop_db.get_item(item))["stock_left"] == 0
    _run(shop_db.undo_sale(sales[1]["id"], STAFF))
    assert _run(shop_db.get_item(item))["stock_left"] == 1


# ── сканер: ручной поиск на точке магазина ─────────────────────────────────────────────

def test_manual_search_on_shop_point_marks_nothing(tmp_path, monkeypatch):
    from database import db as bot_db
    from miniapp import telegram_api
    from tests.test_miniapp_checkin_260924 import (
        BASE, GAME_MANAGER_ID, _grant_checkin_to_game_manager, _hdr, _insert_user, client_with,
    )

    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950911
    _run(_insert_user(uid, full_name="Сидорова Мария", city="spb"))
    _run(shop_db.add_item("Шоппер", 150, 10, None))
    sent = []

    async def fake_send(cfg, chat_id, text, reply_markup=None, parse_mode=None):
        sent.append(chat_id)
        return {}

    monkeypatch.setattr(telegram_api, "send_message", fake_send)
    r = client.post(f"{BASE}/manual", json={"telegram_id": uid, "point": "shop"}, headers=_hdr(GAME_MANAGER_ID))
    assert r.json()["status"] == "shop" and sent == [GAME_MANAGER_ID]
    assert _run(bot_db.list_checkins_for_user(uid)) == []


# ── карточка QR ────────────────────────────────────────────────────────────────────────

def test_qr_card_lines_fit_width():
    from PIL import Image, ImageDraw

    from services.forum import qr_card

    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = qr_card._font("raleway-800.woff2", 40)
    max_w = 824
    lines = qr_card._wrap(draw, "Абдурахмангаджиевавасильевна-Константинопольская-Длиннофамильная", font, max_w)
    assert lines and all(draw.textlength(line, font=font) <= max_w for line in lines)
    title_font = qr_card._font("raleway-800italic.woff2", 48)
    title = qr_card._ellipsize(draw, "Пропуск на ЮЛИД Москва 2026 — большой осенний форум лидеров", title_font, 936)
    assert title.endswith("…") and draw.textlength(title, font=title_font) <= 936
    assert qr_card._ellipsize(draw, "Пропуск на Юлид", title_font, 936) == "Пропуск на Юлид"


# ── волонтёр сменил ник: «готов» пришло — кнопка тоже должна узнать ────────────────────

def test_volunteer_found_by_known_nick_after_rename(tmp_path):
    import sqlite3

    from config import config

    _use_db(tmp_path)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET username = 'anna_old' WHERE telegram_id = ?", (UID,))
    conn.commit()
    conn.close()
    _run(cdb.add_people("volunteer", ["anna_old"]))
    assert UID in _run(certs.recipients())  # рассылка «готов» его находит…
    # …и кнопка тоже, хотя в Telegram ник уже другой
    assert _run(certs.entitlements(UID, "anna_new")) == [("volunteer", "all")]


# ── перенос баллов: поправка вниз и два ника одного человека ───────────────────────────

from tests.test_chat_coins_261009 import DELEGATE, MANAGER, env  # noqa: E402,F401  -- фикстура env


def _transfer(values):
    from services.game import coins_transfer

    plan = _run(coins_transfer.build_plan(coins_transfer.parse_values(values)))
    done = _run(coins_transfer.apply_plan(plan, "Гейма", MANAGER))
    fixed = _run(coins_transfer.apply_updates(plan, "Гейма", MANAGER))
    return plan, done, fixed


def test_transfer_table_decreased_writes_negative_correction_once(env):
    from database import db as bot_db

    header = ["Ник", "Задание 1 (#rolemodel)"]
    _transfer([header, ["@delegate_one", "50"]])
    plan, done, fixed = _transfer([header, ["@delegate_one", "30"]])
    assert done == [] and [(tid, diff) for _, tid, diff in fixed] == [(DELEGATE, -20)]
    assert _run(bot_db.get_balance(DELEGATE)) == 30
    plan, done, fixed = _transfer([header, ["@delegate_one", "30"]])
    assert fixed == [] and len(plan.already) == 1


def test_transfer_second_nick_of_same_person_adds_difference(env):
    from database import db as bot_db

    _run(bot_db.upsert_chat_username(DELEGATE, "delegate_two", "Имя"))
    header = ["Ник", "Задание 1 (#rolemodel)"]
    _transfer([header, ["@delegate_one", "50"]])
    plan, done, fixed = _transfer([header, ["@delegate_one", "50"], ["@delegate_two", "7"]])
    assert [(tid, old, p.total) for p, tid, old in plan.updated] == [(DELEGATE, 50, 57)]
    assert [(tid, diff) for _, tid, diff in fixed] == [(DELEGATE, 7)]
    assert _run(bot_db.get_balance(DELEGATE)) == 57


def test_qr_card_wrap_keeps_spaces_around_lone_dash():
    from PIL import Image, ImageDraw

    from services.forum import qr_card

    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = qr_card._font("raleway-800.woff2", 64)
    assert qr_card._wrap(draw, "Анна - Ли", font, 824) == ["Анна - Ли"]
    assert qr_card._wrap(draw, "Анна-Мария Ли", font, 824) == ["Анна-Мария Ли"]
