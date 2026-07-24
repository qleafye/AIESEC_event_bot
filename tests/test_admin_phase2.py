"""Phase 2 admin pure-helper tests: callback parse, card renderer, settings guide."""
import asyncio

from config import config
from handlers import admin
from handlers.admin import (
    _parse_appr,
    _render_application_card,
    _render_settings_guide,
    APPROVAL_SETTINGS_DOC,
)


def test_parse_appr_with_id():
    assert _parse_appr("appr_approve:123") == ("appr_approve", 123)


def test_parse_appr_no_id():
    assert _parse_appr("appr_all") == ("appr_all", None)


def test_parse_appr_bad_id():
    assert _parse_appr("appr_x:abc") == ("appr_x", None)


def test_card_shows_position_and_escapes():
    user = {"full_name": "<b>hack</b>", "city": "Москва", "resume_file_id": "f1"}
    out = _render_application_card(user, 1, 3)
    assert "1/3" in out
    assert "<b>hack</b>" not in out  # escaped
    assert "&lt;b&gt;hack" in out
    assert "файлом" in out  # резюме файлом → кнопка ниже


def test_card_no_resume_marker():
    out = _render_application_card({"full_name": "Иван"}, 2, 2)
    assert "📎 Резюме: нет" in out


def test_card_shows_resume_text():
    # Таня п.4: текст-резюме виден прямо в карточке (обрезка длинного).
    out = _render_application_card({"full_name": "Иван", "resume_text": "мой богатый опыт"}, 1, 1)
    assert "Резюме (текст)" in out
    assert "мой богатый опыт" in out


def test_settings_guide_lists_keys_and_defaults():
    current = {key: None for key, _, _ in APPROVAL_SETTINGS_DOC}
    current["full_approval"] = "manual"
    out = _render_settings_guide(APPROVAL_SETTINGS_DOC, current)
    assert "pending_notify_mode" in out
    assert "full_approval" in out
    assert "(по умолчанию)" in out  # at least one unset key shows its default


# ── WR-01: mass-approve welcome drain must run even if the confirm edit throws ─

class _RaisingMessage:
    async def edit_text(self, *a, **k):
        raise RuntimeError("Bad Request: message can't be edited (too old)")


class _FakeCallbackWR01:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid})()
        self.message = _RaisingMessage()
        self.bot = object()
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True


def test_wr01_welcome_drain_scheduled_despite_edit_failure(monkeypatch):
    """WR-01: appr_all_yes must schedule _welcome_flipped BEFORE the fragile confirm edit, so a
    >48h/deleted card whose edit_text raises still delivers welcome/menu to the approved users."""
    uid = 42
    monkeypatch.setattr(config, "ADMIN_IDS", [uid])
    welcomed = []

    async def fake_approve_all_pending():
        return [1, 2, 3]

    async def fake_welcome_flipped(bot, ids):
        welcomed.append(list(ids))

    async def fake_bulk_sync(mapping):
        return None

    monkeypatch.setattr(admin, "approve_all_pending", fake_approve_all_pending)
    monkeypatch.setattr(admin, "_welcome_flipped", fake_welcome_flipped)
    monkeypatch.setattr(admin, "bulk_update_status_in_sheet", fake_bulk_sync)
    monkeypatch.setattr(admin, "build_admin_keyboard", lambda *a, **k: None)

    cb = _FakeCallbackWR01(uid)

    async def go():
        await admin.appr_all_yes(cb, None)  # edit_text will raise inside
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(go())

    assert welcomed == [[1, 2, 3]]  # drain ran despite the edit throwing
    assert cb.answered  # handler completed (callback.answer reached)
