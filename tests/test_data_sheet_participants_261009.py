"""09.10: «📊 Данные» — «📄 Открыть таблицу» (url-кнопка) и «👥 Список участников» (CSV одобренных
текущего сезона, русские заголовки, без телефона и служебных колонок)."""
import asyncio
import csv
import io

from config import config
from database import db
from handlers import admin_sections as sec
from handlers.applications import admin_participants as ap
from handlers.access.admin_caps import required_capability, resolve_capabilities
from tests._dbtpl import fast_init_db

ADMIN_ID = 940101
MANAGER_ID = 940102


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "participants261009.db")
    fast_init_db()


def _user(uid, name, status="approved", season="YL 26/2", city="msk", uni="МГУ", pay="not_paid",
          username=None, phone="+79990000000"):
    async def go():
        await db.add_user({
            "telegram_id": uid, "registration_date": "2026-10-01 10:00:00", "full_name": name, "username": username or f"u{uid}",
            "phone": phone, "university": uni, "event_city": city, "season": season,
            "status": status,
        })
        async with db._connect() as c:
            await c.execute("UPDATE users SET status=?, approved_at=?, payment_status=? WHERE telegram_id=?",
                            (status, "2026-10-05 12:00:00" if status == "approved" else None, pay, uid))
            await c.commit()
    asyncio.run(go())


def _set(key, value):
    asyncio.run(db.set_setting(key, value))


class _Msg:
    def __init__(self):
        self.docs = []

    async def answer_document(self, document, caption=None):
        self.docs.append((document, caption))


class _CB:
    def __init__(self, uid):
        from types import SimpleNamespace
        self.from_user = SimpleNamespace(id=uid)
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _parse(doc):
    raw = doc.data if hasattr(doc, "data") else doc.read()
    assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))


def test_rows_are_approved_current_season_only_without_phone(tmp_path):
    from handlers.applications.admin_participants import _city_label
    _use_tmp_db(tmp_path)
    _set("event_season", "YL 26/2")
    _user(1, "Анна Иванова", username="anna")
    _user(2, "Старый Сезон", season="YL 26/1")
    _user(3, "Ещё Не Одобрен", status="pending")
    headers, rows = asyncio.run(db.export_participants_csv(city_label=_city_label))
    assert headers == ["ФИО", "Telegram", "Город", "Вуз", "Дата одобрения"]
    assert len(rows) == 1
    assert rows[0][0] == "Анна Иванова" and rows[0][1] == "@anna"
    assert rows[0][3] == "МГУ" and rows[0][4] == "2026-10-05"
    assert rows[0][2] and rows[0][2] != "msk"  # город названием, не кодом
    assert all("+7999" not in str(c) for c in rows[0])


def test_no_season_set_takes_all_approved(tmp_path):
    _use_tmp_db(tmp_path)
    _user(1, "А Один", season="YL 26/1")
    _user(2, "Б Два", season="YL 26/2")
    _, rows = asyncio.run(db.export_participants_csv())
    assert len(rows) == 2


def test_payment_column_in_words_only_when_requested(tmp_path):
    _use_tmp_db(tmp_path)
    _user(1, "Анна", pay="paid")
    headers, rows = asyncio.run(db.export_participants_csv(with_payment=True))
    assert headers[-1] == "Оплата" and rows[0][-1] == "Оплатил"


def test_formula_injection_neutralised(tmp_path):
    _use_tmp_db(tmp_path)
    _user(1, "=HYPERLINK(1)")
    _, rows = asyncio.run(db.export_participants_csv())
    assert rows[0][0].startswith("'=")


def test_handler_sends_named_csv_and_empty_alert(tmp_path):
    _use_tmp_db(tmp_path)
    cb = _CB(ADMIN_ID)
    asyncio.run(ap.export_participants(cb))
    assert cb.message.docs == []
    assert cb.answers == [(ap.EMPTY_TEXT, True)]
    assert ap.EMPTY_TEXT.startswith("Одобренных в текущем сезоне пока нет")

    _user(1, "Анна Иванова", username="anna")
    cb = _CB(ADMIN_ID)
    asyncio.run(ap.export_participants(cb))
    document, caption = cb.message.docs[0]
    assert document.filename.startswith("uchastniki-") and document.filename.endswith(".csv")
    table = _parse(document)
    assert table[0][0] == "ФИО" and table[1][0] == "Анна Иванова"
    assert caption == "Список участников — одобренные текущего сезона: 1"


def test_open_sheet_button_follows_sheet_id(tmp_path):
    _use_tmp_db(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    old = config.GOOGLE_SHEET_ID
    try:
        config.GOOGLE_SHEET_ID = "ABC123"
        kb = asyncio.run(sec.build_section_keyboard("data", ADMIN_ID))
        urls = [b.url for r in kb.inline_keyboard for b in r if b.url]
        assert urls == ["https://docs.google.com/spreadsheets/d/ABC123/edit"]
        texts = [b.text for r in kb.inline_keyboard for b in r]
        assert "📄 Открыть таблицу" in texts and "👥 Список участников" in texts
        config.GOOGLE_SHEET_ID = ""
        kb = asyncio.run(sec.build_section_keyboard("data", ADMIN_ID))
        assert not [b for r in kb.inline_keyboard for b in r if b.url]
    finally:
        config.GOOGLE_SHEET_ID = old


def test_capabilities_cover_reg_manager_and_stats():
    for cb in ("admin_export_participants", "admin_open_sheet"):
        cap = required_capability(callback_data=cb)
        assert "moderate_reg" in cap
    assert "stats" in required_capability(callback_data="admin_export_participants")
    rows = sec.visible_rows("data", {"moderate_reg"}, False)
    assert [sec.row_callback(r) for r in rows] == ["admin_open_sheet", "admin_export_participants"]
