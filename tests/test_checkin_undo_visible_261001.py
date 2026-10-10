"""«↩️ Отменить» достижима при непрерывном скане.

При 🟢 попап камеры остаётся открытым поверх плашки — кнопку отмены под ним не видно. Отсчёт
кнопки (10 с) стартует, когда попап закрыт (`scanQrPopupClosed`), а сервер принимает отмену
своей последней отметки 2 минуты с момента скана."""
from __future__ import annotations
from tests._paths import REPO_ROOT

from datetime import timedelta
from pathlib import Path

from database import db
from services import venue_log
from services.checkin import ENTRY_POINT, record_arrival
from tests.test_miniapp_checkin_260924 import BASE, _grant_checkin_to_game_manager, _insert_user, _qr, _run, client_with
from tests.test_miniapp_routes import GAME_MANAGER_ID, _hdr

SCANNER_JS = REPO_ROOT / "miniapp" / "static" / "js" / "screens" / "scanner.js"


def test_scan_reports_button_and_accept_windows(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(_insert_user(953001, city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(953001)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["undo"]["seconds"] == venue_log.UNDO_WINDOW_SECONDS == 10
    assert body["undo"]["valid_seconds"] == venue_log.UNDO_ACCEPT_SECONDS == 120


def _mark_and_undo_after(tmp_path, monkeypatch, seconds: int) -> str:
    client_with(tmp_path)
    _run(_insert_user(953002, city="spb"))
    user = _run(db.get_user(953002))
    r = _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=GAME_MANAGER_ID))
    row = _run(db.venue_log_get(r["log_id"]))
    from datetime import datetime
    created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(venue_log, "msk_now", lambda: created + timedelta(seconds=seconds))
    return _run(venue_log.undo_last_scan(GAME_MANAGER_ID, None, r["log_id"]))


def test_undo_a_minute_after_scan_still_works(tmp_path, monkeypatch):
    assert _mark_and_undo_after(tmp_path, monkeypatch, 60) == "ok"


def test_undo_after_accept_window_is_refused(tmp_path, monkeypatch):
    assert _mark_and_undo_after(tmp_path, monkeypatch, venue_log.UNDO_ACCEPT_SECONDS + 1) == "expired"


def test_scanner_starts_undo_countdown_when_popup_closes():
    text = SCANNER_JS.read_text(encoding="utf-8")
    assert 'tg.onEvent("scanQrPopupClosed", onScanPopupClosed)' in text
    assert 'tgRef.offEvent("scanQrPopupClosed", popupClosedHandler)' in text
    assert "export function unmount()" in text
    body = text[text.index("function undoButton"):text.index("function showPlaque")]
    assert "pendingUndo = { btn" in body and "valid_seconds" in body
    closed = text[text.index("function onScanPopupClosed"):text.index("function undoButton")]
    assert "startUndoCountdown(btn, seconds)" in closed and "scrollIntoView" in closed
