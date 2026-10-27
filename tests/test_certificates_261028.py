"""Сертификаты после форума: рендер имени в PDF, кто что получает, поток делегата и админки.

Шаблон в тестах — пустая страница A4 альбомом из pypdf: настоящие шаблоны с подписью и печатью
в репозиторий не кладём, менеджер загружает их в бот."""
from __future__ import annotations

import asyncio
import io
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pypdf import PdfReader, PdfWriter

from config import config
from database import certificates_db as cdb
from database import db
from handlers.forum import admin_certificates as adm
from handlers.forum import certificates as h
from services.forum import certificates as certs
from services.forum.certificate_render import render_certificate

UID = 261028001


def _template() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _use_db(tmp_path):
    config.DB_PATH = str(tmp_path / "certs.db")
    asyncio.run(db.init_db())
    asyncio.run(db.add_user({
        "telegram_id": UID, "full_name": "Иванова Анна", "registration_date": "2026-01-01", "event_city": "msk",
    }))


def _checkin(uid: int, day: str):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
        "VALUES (?, 'entry', ?, 'scan', ?, ?)",
        (uid, f"{day} 10:00:00", f"{day} 10:00:00", day),
    )
    conn.commit()
    conn.close()


class _Bot:
    def __init__(self, data: bytes):
        self.data = data

    async def download(self, file_id, destination):
        destination.write(self.data)


# ── рендер ─────────────────────────────────────────────────────────────────────────────

def test_render_embeds_name_image():
    pdf = render_certificate(_template(), "Константинопольская Александра Сергеевна")
    page = PdfReader(io.BytesIO(pdf)).pages[0]
    xobjects = page["/Resources"]["/XObject"]
    image = xobjects["/CertName"].get_object()
    assert image["/Subtype"] == "/Image" and "/SMask" in image
    assert float(page.mediabox.width) == 842


# ── кто что получает ───────────────────────────────────────────────────────────────────

def test_entitlements_participant_by_checkin_and_one_day_variant(tmp_path):
    _use_db(tmp_path)
    assert asyncio.run(certs.entitlements(UID, "anna")) == []
    _checkin(UID, "2026-10-30")
    assert asyncio.run(certs.entitlements(UID, "anna")) == [("participant", "2026-10-30")]
    _checkin(UID, "2026-10-31")
    assert asyncio.run(certs.entitlements(UID, "anna")) == [("participant", "all")]


def test_entitlements_roles_by_username_all_of_them(tmp_path):
    _use_db(tmp_path)
    _checkin(UID, "2026-10-30")
    asyncio.run(cdb.add_people("volunteer", ["anna"]))
    asyncio.run(cdb.add_people("ambassador", ["anna"]))
    got = asyncio.run(certs.entitlements(UID, "@Anna"))
    assert got == [("participant", "2026-10-30"), ("volunteer", "all"), ("ambassador", "all")]


def test_pick_template_falls_back_to_all_days():
    templates = {("participant", "all"): "A", ("participant", "2026-10-30"): "D1"}
    assert certs.pick_template(templates, "participant", "2026-10-30") == "D1"
    assert certs.pick_template(templates, "participant", "2026-10-31") == "A"
    assert certs.pick_template(templates, "volunteer", "all") is None


def test_clean_name():
    assert certs.clean_name("  Анна   Иванова ") == "Анна Иванова"
    assert certs.clean_name("А") is None
    assert certs.clean_name("@anna") is None
    assert certs.clean_name("x" * 61) is None


def test_open_at_gate(tmp_path):
    _use_db(tmp_path)
    from datetime import datetime
    assert not asyncio.run(certs.is_open())
    asyncio.run(cdb.set_open_at("2026-11-02 12:00"))
    assert not asyncio.run(certs.is_open(datetime(2026, 11, 2, 11, 59)))
    assert asyncio.run(certs.is_open(datetime(2026, 11, 2, 12, 0)))


def test_claim_announce_once(tmp_path):
    _use_db(tmp_path)
    assert asyncio.run(cdb.claim_announce(UID))
    assert not asyncio.run(cdb.claim_announce(UID))


# ── делегат ────────────────────────────────────────────────────────────────────────────

def _callback(data: str):
    message = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock(), edit_reply_markup=AsyncMock())
    user = SimpleNamespace(id=UID, username="anna", full_name="Anna")
    return SimpleNamespace(data=data, from_user=user, message=message, answer=AsyncMock())


def test_delegate_closed_then_sends_pdf(tmp_path):
    _use_db(tmp_path)
    _checkin(UID, "2026-10-30")
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())

    cb = _callback("cert_get")
    asyncio.run(h.certificate_get(cb, state))
    assert "пока нет" in cb.message.answer.call_args.args[0]

    asyncio.run(cdb.set_open_at("2020-01-01 00:00"))
    asyncio.run(cdb.set_template("participant", "all", "FILE", 1))
    cb = _callback("cert_get")
    asyncio.run(h.certificate_get(cb, state))
    assert "Иванова Анна" in cb.message.answer.call_args.args[0]

    cb = _callback("cert_ok")
    asyncio.run(h.certificate_send(cb, _Bot(_template())))
    doc = cb.message.answer_document.call_args.args[0]
    assert doc.filename == "Сертификат участника — Иванова Анна.pdf"
    assert asyncio.run(cdb.send_counts()) == {"participant": 1}


def test_delegate_not_checked_in_gets_explanation(tmp_path):
    _use_db(tmp_path)
    asyncio.run(cdb.set_open_at("2020-01-01 00:00"))
    cb = _callback("cert_get")
    asyncio.run(h.certificate_get(cb, SimpleNamespace(clear=AsyncMock())))
    assert "отмеченных на входе" in cb.message.answer.call_args.args[0]


# ── админка ────────────────────────────────────────────────────────────────────────────

def test_admin_people_add_and_remove(tmp_path):
    _use_db(tmp_path)
    state = SimpleNamespace(get_data=AsyncMock(return_value={"kind": "volunteer"}), clear=AsyncMock())
    msg = SimpleNamespace(text="@Ivanov, t.me/maria_k  oops!", answer=AsyncMock(), from_user=SimpleNamespace(id=1))
    asyncio.run(adm.certadm_ppl_input(msg, state))
    assert asyncio.run(cdb.list_people("volunteer")) == ["ivanov", "maria_k"]
    assert "пропустил" in msg.answer.call_args_list[0].args[0]
    msg.text = "-@ivanov"
    asyncio.run(adm.certadm_ppl_input(msg, state))
    assert asyncio.run(cdb.list_people("volunteer")) == ["maria_k"]


def test_admin_open_at_input(tmp_path):
    _use_db(tmp_path)
    state = SimpleNamespace(clear=AsyncMock())
    msg = SimpleNamespace(text="02.11.2026 12:00", answer=AsyncMock())
    asyncio.run(adm.certadm_open_input(msg, state))
    assert asyncio.run(cdb.get_open_at()) == "2026-11-02 12:00"
    msg = SimpleNamespace(text="завтра", answer=AsyncMock())
    asyncio.run(adm.certadm_open_input(msg, state))
    assert "ДД.ММ.ГГГГ" in msg.answer.call_args.args[0]
    assert asyncio.run(cdb.get_open_at()) == "2026-11-02 12:00"


def test_admin_home_renders(tmp_path):
    _use_db(tmp_path)
    text, kb = asyncio.run(adm.home_text_kb())
    assert "Сертификаты" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "certadm_open" in datas and "certadm_tpls" in datas
