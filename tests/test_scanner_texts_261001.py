"""Приёмка 01.10, сканер: случайный текст/чужой QR — «Это не QR-пропуск», а не «QR другого
мероприятия» (тот — только для нашего формата с чужой меткой); подпись города без падежа
после «в»; «@username» без двойной «@»."""
from __future__ import annotations
from tests._paths import REPO_ROOT

from pathlib import Path

from services.forum.checkin import DENIAL_REASON_TEXT, foreign_qr_code
from tests.test_miniapp_checkin_260924 import (
    BASE, _grant_checkin_to_game_manager, _insert_user, _qr, _run, client_with,
)
from tests.test_miniapp_checkin_denied_log_260925 import _denials
from tests.test_miniapp_routes import GAME_MANAGER_ID, _hdr

ROOT = REPO_ROOT


def test_foreign_qr_code_distinguishes_format():
    assert foreign_qr_code("OTHERFEST·Иван Иванов·msk·abc123") == "foreign_event"
    assert foreign_qr_code("просто мусор 12345") == "not_our_qr"
    assert foreign_qr_code("https://example.com/ticket?id=1") == "not_our_qr"
    assert foreign_qr_code("") == "not_our_qr"
    assert foreign_qr_code("a·b") == "not_our_qr"


def test_scan_garbage_says_not_a_pass_and_logs_reason(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    body = client.post(f"{BASE}/scan", json={"payload": "просто мусор 12345"}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "foreign_event"  # тон экрана тот же — «Не пропущен»
    assert body["reason_text"] == DENIAL_REASON_TEXT["not_our_qr"]
    assert "QR-пропуск" in body["reason_text"]
    [row] = _denials()
    assert row["details"] == {"reason": "not_our_qr"}


def test_scan_other_event_tag_still_other_event(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 961001
    _run(_insert_user(uid))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid, tag="OTHERFEST")}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["reason_text"] == "QR другого мероприятия"


def test_wrong_city_texts_have_no_broken_case():
    for rel in ("miniapp/routers/checkin.py", "services/forum/checkin.py", "services/forum/checkin_forum_day.py", "domain/settings/schema.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "Делегат с форума в {" not in text, rel


def test_scanner_username_has_single_at():
    ui = (ROOT / "miniapp/static/js/ui.js").read_text(encoding="utf-8")
    assert "export function atUsername(" in ui
    assert 'replace(/^@+/, "")' in ui
    for rel in ("scanner.js", "applications.js", "review.js"):
        js = (ROOT / "miniapp/static/js/screens" / rel).read_text(encoding="utf-8")
        assert "`@${" not in js, rel
        assert "atUsername(" in js, rel


def test_not_a_pass_names_event_in_genitive_when_set(tmp_path):
    from database import db as bot_db

    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(bot_db.set_setting("event_name_genitive", "форума Юлид"))
    body = client.post(f"{BASE}/scan", json={"payload": "просто мусор"}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["reason_text"].startswith("Это не QR-пропуск форума Юлид")
