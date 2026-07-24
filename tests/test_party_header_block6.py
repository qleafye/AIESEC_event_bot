"""Block 6 MEDIUM-01: the party tab header must resync on a party-question toggle/preset — but
only while the party track is enabled (D-15: toggling an override with the track OFF must never
materialize the tab). Drives admin._refresh_party_sheet_header directly.
"""
import asyncio

from config import config
from database import db
from handlers import admin
import services.sheets as sheets


def _drain():
    async def _g():
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
    return _g


def test_medium01_resync_when_party_enabled(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "ph_on.db")
    calls = []

    async def fake_ensure(tab, headers):
        calls.append((tab, headers))

    monkeypatch.setattr(sheets, "ensure_named_sheet_header", fake_ensure)

    async def go():
        await db.init_db()
        await db.set_setting("party_enabled", "on")
        await admin._refresh_party_sheet_header()
        await _drain()()

    asyncio.run(go())
    assert len(calls) == 1
    tab, headers = calls[0]
    assert tab == "Party"  # default party tab
    assert isinstance(headers, list) and headers  # a real, non-empty header row


def test_medium01_no_resync_when_party_disabled(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "ph_off.db")
    calls = []

    async def fake_ensure(tab, headers):
        calls.append((tab, headers))

    monkeypatch.setattr(sheets, "ensure_named_sheet_header", fake_ensure)

    async def go():
        await db.init_db()
        # party_enabled unset → off by default
        await admin._refresh_party_sheet_header()
        await _drain()()

    asyncio.run(go())
    assert calls == []  # track OFF → never materialize the tab
