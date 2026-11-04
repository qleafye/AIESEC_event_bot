"""Сертификат волонтёра и амбассадора уходит сам: имя из списка, без шага «всё верно?».

Имена в тесте выдуманы. Боевые списки в репозиторий не кладём."""
from __future__ import annotations

import asyncio
import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.sax.saxutils import escape

from aiogram.exceptions import TelegramForbiddenError

from database import certificates_db as cdb
from database import db
from handlers.forum import admin_certificates as adm
from services import scheduler as sched
from services.forum import certificates as certs
from tests.test_certificates_261028 import UID, _template, _use_db

VOL = "volunteer_nick"
AMB = "ambassador_nick"


def _run(coro):
    return asyncio.run(coro)


class _DocBot:
    def __init__(self, fail_document: bool = False):
        self.docs = []
        self.messages = []
        self.fail_document = fail_document
        self.data = _template()

    async def download(self, file_id, destination):
        destination.write(self.data)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append(chat_id)

    async def send_document(self, chat_id, document, caption=None):
        if self.fail_document:
            raise TelegramForbiddenError(method=None, message="blocked")
        self.docs.append((chat_id, document.filename, caption))


def _arm(tmp_path, monkeypatch, bot, *, username="anna", full_name="Иванова Анна"):
    _use_db(tmp_path)
    _run(_set_user(UID, username, full_name))
    _run(cdb.set_open_at("2020-01-01 00:00"))
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(certs.asyncio, "sleep", AsyncMock())


async def _set_user(uid: int, username: str, full_name: str):
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE telegram_id = ?",
            (username, full_name, uid),
        )
        await conn.commit()


def _paste(kind: str, text: str):
    state = SimpleNamespace(get_data=AsyncMock(return_value={"kind": kind}), clear=AsyncMock())
    msg = SimpleNamespace(text=text, answer=AsyncMock(), from_user=SimpleNamespace(id=1))
    _run(adm.certadm_ppl_input(msg, state))
    return msg


def test_paste_stores_name_and_skips_line_without_nick(tmp_path):
    _use_db(tmp_path)
    _run(cdb.set_name(UID, "Иванова Анна"))
    msg = _paste("volunteer", "@volunteer_nick — Иванова Анна Сергеевна\nПросто имя без ника")
    rows = _run(cdb.list_people_rows("volunteer"))
    assert rows == [{
        "username": "volunteer_nick", "list_name": "Иванова Анна Сергеевна",
        "name_ok": 0, "send_blocked": 0,
    }]
    assert "пропустил" in msg.answer.call_args_list[0].args[0]
    assert "Иванова Анна Сергеевна" in msg.answer.call_args_list[1].args[0]
    assert _run(cdb.get_name(UID)) == "Иванова Анна"


def test_kind_variants_list_roles_are_all_days():
    variants = ["all", "2026-10-30"]
    assert certs.kind_variants(cdb.KIND_VOLUNTEER, variants) == ["all"]
    assert certs.kind_variants(cdb.KIND_AMBASSADOR, variants) == ["all"]
    assert certs.kind_variants(cdb.KIND_PARTICIPANT, variants) == variants


def test_job_sends_list_name_and_does_not_repeat(tmp_path, monkeypatch):
    bot = _DocBot()
    _arm(tmp_path, monkeypatch, bot, username=VOL)
    _run(cdb.set_template("volunteer", "all", "FILE", 1))
    _paste("volunteer", f"@{VOL} — Иванова Анна Сергеевна")
    assert _run(certs.announce_job()) == 1
    assert bot.docs == [(UID, "Сертификат волонтёра — Иванова Анна Сергеевна.pdf", "🎓 Сертификат волонтёра")]
    assert _run(cdb.get_name(UID)) is None
    _run(certs.announce_job())
    assert len(bot.docs) == 1


def test_job_sends_only_new_ambassador_pdf(tmp_path, monkeypatch):
    bot = _DocBot()
    _arm(tmp_path, monkeypatch, bot, username=AMB)
    _run(cdb.set_template("participant", "all", "P", 1))
    _run(cdb.set_template("ambassador", "all", "A", 1))
    _run(cdb.set_name(UID, "Иванова Анна"))
    _run(cdb.record_send(UID, "participant"))
    _run(cdb.claim_announce(UID))
    _paste("ambassador", f"@{AMB} — Иванова Анна Сергеевна")
    assert _run(certs.announce_job()) == 0
    assert bot.messages == []
    assert bot.docs == [(UID, "Сертификат амбассадора — Иванова Анна Сергеевна.pdf", "🎓 Сертификат амбассадора")]
    assert _run(cdb.get_name(UID)) == "Иванова Анна"
    assert _run(cdb.sent_kinds(UID)) == {"participant", "ambassador"}


def test_job_finds_reg_started_and_chat_username(tmp_path, monkeypatch):
    bot = _DocBot()
    _arm(tmp_path, monkeypatch, bot, username="other_person")
    _run(cdb.set_template("volunteer", "all", "FILE", 1))
    _run(db.mark_reg_started(261104002, "startonly"))
    _run(db.upsert_chat_username(261104003, "chatonly", "Чат"))
    _paste("volunteer", "@startonly — Стартова Анна\n@chatonly — Чатова Анна")
    _run(certs.announce_job())
    names = {item[1] for item in bot.docs}
    assert names == {
        "Сертификат волонтёра — Стартова Анна.pdf",
        "Сертификат волонтёра — Чатова Анна.pdf",
    }
    assert {item[0] for item in bot.docs} == {261104002, 261104003}


def test_unknown_nick_stays_unseen_and_gets_no_message(tmp_path, monkeypatch):
    bot = _DocBot()
    _arm(tmp_path, monkeypatch, bot, username="someone_else")
    _run(cdb.set_template("volunteer", "all", "FILE", 1))
    _paste("volunteer", "@nobody_here — Никтоева Анна")
    assert _run(certs.announce_job()) == 0
    assert bot.docs == [] and bot.messages == []
    screen = _run(certs.people_screen("volunteer"))
    assert screen[0]["mark"] == "бот ещё не видел этот ник"


def test_surname_conflict_waits_for_yes(tmp_path, monkeypatch):
    bot = _DocBot()
    _arm(tmp_path, monkeypatch, bot, full_name="Петрова Анна")
    _run(cdb.set_template("volunteer", "all", "FILE", 1))
    _paste("volunteer", "@anna — Иванова Анна")
    assert _run(certs.announce_job()) == 1  # кнопка «готов» уходит, файла нет
    assert bot.docs == []
    screen = _run(certs.people_screen("volunteer"))
    assert screen[0]["conflict"] is True
    assert screen[0]["form_surname"] == "Петрова"
    text, kb = _run(adm._people_text_kb("volunteer"))
    assert "Иванова Анна" in text and "Петрова" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "certadm_yes:volunteer:anna" in datas
    state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
    cb = SimpleNamespace(
        data="certadm_yes:volunteer:anna",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    _run(adm.certadm_yes(cb, state))
    _run(certs.announce_job())
    assert bot.docs == [(UID, "Сертификат волонтёра — Иванова Анна.pdf", "🎓 Сертификат волонтёра")]


def test_shorter_or_garbage_form_is_not_a_surname_conflict(tmp_path, monkeypatch):
    bot = _DocBot()
    _arm(tmp_path, monkeypatch, bot, full_name="Иванова")
    _run(cdb.set_template("volunteer", "all", "FILE", 1))
    _paste("volunteer", "@anna — Иванова Анна Сергеевна")
    _run(certs.announce_job())
    assert bot.docs[0][1] == "Сертификат волонтёра — Иванова Анна Сергеевна.pdf"

    _run(_set_user(UID, "anna", "кто ты?"))
    _paste("ambassador", "@anna — Иванова Анна")
    _run(cdb.set_template("ambassador", "all", "A", 1))
    _run(certs.announce_job())
    assert any(name.endswith("Иванова Анна.pdf") and "амбассадора" in name for _, name, _ in bot.docs)


def test_yo_and_case_are_the_same_surname(tmp_path, monkeypatch):
    bot = _DocBot()
    _arm(tmp_path, monkeypatch, bot, full_name="семенова  анна")
    _run(cdb.set_template("volunteer", "all", "FILE", 1))
    _paste("volunteer", "@anna — Семёнова Анна")
    _run(certs.announce_job())
    assert bot.docs[0][1] == "Сертификат волонтёра — Семёнова Анна.pdf"
    assert _run(certs.people_screen("volunteer"))[0]["mark"] == "файл ушёл"


def test_blocked_user_is_not_retried(tmp_path, monkeypatch):
    bot = _DocBot(fail_document=True)
    _arm(tmp_path, monkeypatch, bot)
    _run(cdb.set_template("volunteer", "all", "FILE", 1))
    _paste("volunteer", "@anna — Иванова Анна")
    _run(certs.announce_job())
    assert bot.docs == []
    assert _run(certs.people_screen("volunteer"))[0]["mark"] == "человек заблокировал бота"
    bot.fail_document = False
    _run(certs.announce_job())
    assert bot.docs == []


def test_closed_issuance_sends_nothing(tmp_path, monkeypatch):
    bot = _DocBot()
    _use_db(tmp_path)
    _run(_set_user(UID, "anna", "Иванова Анна"))
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(certs.asyncio, "sleep", AsyncMock())
    _run(cdb.set_template("volunteer", "all", "FILE", 1))
    _paste("volunteer", "@anna — Иванова Анна")
    assert _run(certs.announce_job()) == 0
    assert bot.docs == [] and bot.messages == []


def test_remove_button_drops_the_row(tmp_path):
    _use_db(tmp_path)
    _paste("volunteer", "@anna — Иванова Анна")
    state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
    cb = SimpleNamespace(
        data="certadm_rm:volunteer:anna",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    _run(adm.certadm_rm(cb, state))
    assert _run(cdb.list_people("volunteer")) == []


def _xlsx(rows: list[list[str]]) -> bytes:
    """Минимальный .xlsx: общие строки и первый лист. openpyxl в проект не тащим."""
    shared: list[str] = []
    index: dict[str, int] = {}

    def sid(value: str) -> int:
        if value not in index:
            index[value] = len(shared)
            shared.append(value)
        return index[value]

    body = []
    for r_i, row in enumerate(rows, 1):
        cells = []
        for c_i, value in enumerate(row):
            col = chr(ord("A") + c_i)
            cells.append(f'<c r="{col}{r_i}" t="s"><v>{sid(value)}</v></c>')
        body.append(f'<row r="{r_i}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{ns}"><sheetData>{"".join(body)}</sheetData></worksheet>'
    )
    items = "".join(f"<si><t>{escape(value)}</t></si>" for value in shared)
    strings = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{ns}" count="{len(shared)}" uniqueCount="{len(shared)}">{items}</sst>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", strings)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_csv_with_header_stores_the_name():
    adds, bad = certs.people_from_file(
        "Ник;ФИО\nanna_k;Иванова Анна\n".encode("utf-8"), "people.csv",
    )
    assert adds == [("anna_k", "Иванова Анна")]
    assert bad == []


def test_csv_without_header_finds_nick_in_the_other_column():
    adds, bad = certs.people_from_file(
        "Иванова Анна,@anna_k\n".encode("utf-8"), "people.csv",
    )
    assert adds == [("anna_k", "Иванова Анна")]
    assert bad == []


def test_xlsx_ignores_the_extra_date_column():
    raw = _xlsx([
        ["Ник", "ФИО", "Дата"],
        ["@petr_k", "Петров Пётр", "2026-10-30"],
    ])
    adds, bad = certs.people_from_file(raw, "people.xlsx")
    assert adds == [("petr_k", "Петров Пётр")]
    assert bad == []


def test_row_without_a_nick_is_reported():
    adds, bad = certs.people_from_file(
        "Ник;ФИО\n;Сидорова Мария\nboris_k;Борисов Борис\n".encode("utf-8"), "people.csv",
    )
    assert adds == [("boris_k", "Борисов Борис")]
    assert bad == ["Сидорова Мария"]


def test_old_xls_is_refused():
    try:
        certs.people_from_file(b"\xd0\xcf\x11\xe0rest", "people.xls")
    except certs.PeopleTableError as e:
        assert "старый Excel" in str(e)
    else:
        raise AssertionError("старый .xls должен быть отказом, а не списком")


def test_minus_in_the_file_does_not_remove(tmp_path):
    _use_db(tmp_path)
    _paste("volunteer", "@anna — Иванова Анна")
    raw = "-@anna\n".encode("utf-8")

    class _Bot:
        async def download(self, file_id):
            return io.BytesIO(raw)

    state = SimpleNamespace(get_data=AsyncMock(return_value={"kind": "volunteer"}), clear=AsyncMock())
    msg = SimpleNamespace(
        document=SimpleNamespace(file_id="FILE", file_size=len(raw), file_name="people.csv"),
        answer=AsyncMock(),
    )
    _run(adm.certadm_ppl_file(msg, state, _Bot()))
    assert _run(cdb.list_people("volunteer")) == ["anna"]
    assert "минус" in msg.answer.call_args.args[0]


def test_file_adds_people_and_says_the_list_was_not_replaced(tmp_path):
    _use_db(tmp_path)
    _paste("volunteer", "@old_nick — Старова Анна")
    raw = "Ник;ФИО\nanna_k;Иванова Анна\n".encode("utf-8")

    class _Bot:
        async def download(self, file_id):
            return io.BytesIO(raw)

    state = SimpleNamespace(get_data=AsyncMock(return_value={"kind": "volunteer"}), clear=AsyncMock())
    msg = SimpleNamespace(
        document=SimpleNamespace(file_id="FILE", file_size=len(raw), file_name="people.csv"),
        answer=AsyncMock(),
    )
    _run(adm.certadm_ppl_file(msg, state, _Bot()))
    rows = _run(cdb.list_people_rows("volunteer"))
    assert {(row["username"], row["list_name"]) for row in rows} == {
        ("old_nick", "Старова Анна"),
        ("anna_k", "Иванова Анна"),
    }
    assert "не заменён" in msg.answer.call_args_list[0].args[0]
