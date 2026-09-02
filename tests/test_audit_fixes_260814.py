"""Production audit 2026-08-14: stop three self-inflicted log/retry storms.

The audit read a month of logs/bot.log: 7753 ERROR lines, of which ~5500 were the SAME two
chat_ids retried every 15 minutes forever, plus 22 full update dumps for double-tapped buttons
and ~24 warnings a day for an allowlist tab nobody uses. None of it signalled a real fault, and
together it buried the lines that did.

Covered here:
1. `_safe_send` treats TelegramForbiddenError (user blocked the bot) as PERMANENT and runs the
   caller's give-up hook; `nudge_incomplete_registrations` passes `mark_nudged` as that hook, so
   a blocked user is stamped once instead of re-queued on every scan.
2. `allowlist_refresh_job` does not touch the Sheets API at all while pre-selection gating is
   off — the state this bot has always run in.

Conventions follow tests/test_nudge_phase3.py: plain `def test_*`, `asyncio.run(go())`, no
pytest-asyncio, monkeypatch for module globals.
"""
import asyncio

from aiogram.exceptions import TelegramForbiddenError

import services.scheduler as scheduler


def _forbidden():
    return TelegramForbiddenError(
        method=None, message="Forbidden: bot was blocked by the user"
    )


# ── 1. Permanent-failure hook ──────────────────────────────────────────────────────────────

def test_safe_send_runs_hook_and_reports_failure_when_blocked():
    hook_calls = []

    async def sender(chat_id):
        raise _forbidden()

    async def hook(chat_id):
        hook_calls.append(chat_id)

    async def go():
        return await scheduler._safe_send(sender, 555, on_permanent_failure=hook)

    assert asyncio.run(go()) is False
    assert hook_calls == [555]  # the give-up was recorded exactly once


def test_safe_send_without_hook_still_reports_failure_when_blocked():
    """Call sites that cannot re-queue (one-shot broadcasts) pass no hook and must not break."""

    async def sender(chat_id):
        raise _forbidden()

    async def go():
        return await scheduler._safe_send(sender, 555)

    assert asyncio.run(go()) is False


def test_safe_send_does_not_run_hook_on_transient_failure():
    """A generic error may succeed later — giving up on it would silently drop a real send."""
    hook_calls = []

    async def sender(chat_id):
        raise RuntimeError("network hiccup")

    async def hook(chat_id):
        hook_calls.append(chat_id)

    async def go():
        return await scheduler._safe_send(sender, 555, on_permanent_failure=hook)

    assert asyncio.run(go()) is False
    assert hook_calls == []


def test_safe_send_does_not_run_hook_on_success():
    hook_calls = []

    async def sender(chat_id):
        return None

    async def hook(chat_id):
        hook_calls.append(chat_id)

    async def go():
        return await scheduler._safe_send(sender, 555, on_permanent_failure=hook)

    assert asyncio.run(go()) is True
    assert hook_calls == []


def test_blocked_candidate_is_marked_nudged_and_not_retried(monkeypatch):
    """The regression itself: before the fix `mark_nudged` ran only on a successful send, so a
    blocked user stayed a candidate on every 15-minute scan (2934 identical ERRORs in prod)."""
    marked = []
    sent_attempts = []

    async def fake_get_nudge_candidates(cutoff):
        return [111]

    async def fake_mark_nudged(telegram_id):
        marked.append(telegram_id)

    async def fake_get_setting(key):
        return {"nudge_enabled": "on", "nudge_after_minutes": "120"}.get(key)

    class _FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None):
            sent_attempts.append(chat_id)
            raise _forbidden()

    monkeypatch.setattr("database.db.get_nudge_candidates", fake_get_nudge_candidates)
    monkeypatch.setattr("database.db.mark_nudged", fake_mark_nudged)
    monkeypatch.setattr(scheduler, "get_setting", fake_get_setting)
    monkeypatch.setattr(scheduler, "_bot", _FakeBot())

    asyncio.run(scheduler.nudge_incomplete_registrations())

    assert sent_attempts == [111]  # tried once
    assert marked == [111]         # and gave up, so the next scan skips them


# ── 2. Allowlist refresh is skipped while gating is off ────────────────────────────────────

def test_allowlist_refresh_skipped_when_preselect_off(monkeypatch):
    refreshed = []

    async def fake_refresh():
        refreshed.append(True)

    async def fake_get_setting(key):
        return "off" if key == "preselect_enabled" else None

    monkeypatch.setattr("services.allowlist.refresh_allowlist", fake_refresh)
    monkeypatch.setattr(scheduler, "get_setting", fake_get_setting)

    asyncio.run(scheduler.allowlist_refresh_job())

    assert refreshed == []  # no Sheets call, hence no hourly WARNING for a missing tab


def test_allowlist_refresh_runs_when_preselect_on(monkeypatch):
    refreshed = []
    alerts = []

    async def fake_refresh():
        refreshed.append(True)

    async def fake_get_setting(key):
        return "on" if key == "preselect_enabled" else None

    class _FakeBot:
        async def send_message(self, chat_id, text):
            alerts.append((chat_id, text))

    monkeypatch.setattr("services.allowlist.refresh_allowlist", fake_refresh)
    monkeypatch.setattr("services.allowlist.allowlist_size", lambda: 0)
    monkeypatch.setattr(scheduler, "get_setting", fake_get_setting)
    monkeypatch.setattr(scheduler, "_bot", _FakeBot())
    monkeypatch.setattr(scheduler.config, "ADMIN_IDS", [777])

    asyncio.run(scheduler.allowlist_refresh_job())

    assert refreshed == [True]
    # gating ON + empty allowlist is the fail-open case that must still shout at admins
    assert [chat_id for chat_id, _ in alerts] == [777]


# ── 3. Startup warm-up is gated the same way as the interval job ───────────────────────────

def test_startup_warm_up_skipped_when_preselect_off(monkeypatch):
    """The interval job alone was not enough: startup called refresh_allowlist() directly, so
    every restart still produced one WARNING about the missing tab."""
    import services.allowlist as allowlist

    refreshed = []

    async def fake_refresh():
        refreshed.append(True)

    async def fake_get_setting(key):
        return "off" if key == "preselect_enabled" else None

    monkeypatch.setattr(allowlist, "refresh_allowlist", fake_refresh)
    monkeypatch.setattr(allowlist, "get_setting", fake_get_setting)

    asyncio.run(allowlist.warm_allowlist_if_gating_on())

    assert refreshed == []


def test_startup_warm_up_runs_when_preselect_on(monkeypatch):
    import services.allowlist as allowlist

    refreshed = []

    async def fake_refresh():
        refreshed.append(True)

    async def fake_get_setting(key):
        return "on" if key == "preselect_enabled" else None

    monkeypatch.setattr(allowlist, "refresh_allowlist", fake_refresh)
    monkeypatch.setattr(allowlist, "get_setting", fake_get_setting)

    asyncio.run(allowlist.warm_allowlist_if_gating_on())

    assert refreshed == [True]


# ── 4. /create_link no longer dies on a copy-pasted placeholder ────────────────────────────
#
# Live case 14.08: a manager copied the format straight out of the help text, angle brackets
# included — «/create_link <инфо ВК>». The label went into an HTML message unescaped, Telegram
# read «<инфо» as an opening tag and rejected the WHOLE message, so she got no link and no
# error — just silence. The first quiet day of logs is what surfaced it.

class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)


class _FakeBotUser:
    username = "YouLead2026_bot"


class _FakeBot:
    async def get_me(self):
        return _FakeBotUser()


def _run_create_link(text):
    from handlers.admin import cmd_create_link

    message = _FakeMessage(text)
    asyncio.run(cmd_create_link(message, _FakeBot()))
    assert len(message.answers) == 1
    return message.answers[0]


def test_create_link_placeholder_with_brackets_explains_instead_of_dying():
    reply = _run_create_link("/create_link <инфо ВК>")

    # The angle brackets must come back ESCAPED — an unescaped '<' is what broke the send.
    assert "&lt;инфо ВК&gt;" in reply
    assert "<инфо" not in reply
    # And the reply has to say what to do, not just refuse (CLAUDE.md: ошибка объясняет).
    assert "vk_poster" in reply
    assert "?start=src_" not in reply  # no broken link handed out


def test_create_link_strips_brackets_around_an_otherwise_valid_tag():
    """«/create_link <vk_poster>» — the intent is unambiguous, so accept it silently."""
    reply = _run_create_link("/create_link <vk_poster>")

    assert "?start=src_vk_poster" in reply


def test_create_link_rejects_cyrillic_and_spaces_without_brackets():
    reply = _run_create_link("/create_link инфо ВК")

    assert "?start=src_" not in reply
    assert "vk_poster" in reply


def test_create_link_accepts_a_plain_tag():
    reply = _run_create_link("/create_link vk_poster")

    assert "https://t.me/YouLead2026_bot?start=src_vk_poster" in reply


def test_create_link_rejects_overlong_tag():
    """`src_` + 60 chars is the 64-char ceiling Telegram allows in a start payload."""
    reply = _run_create_link("/create_link " + "a" * 61)

    assert "?start=src_" not in reply
