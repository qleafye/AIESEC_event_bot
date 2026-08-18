"""Phase 13 (refac-split-god-files) Plan 01, Task 2(b) — M1 `is_question_reply` gate.

TEST-VALUE-260815.md: mutation testing flipped the capability check inside
`handlers.admin.is_question_reply` (admin.py:541) and NOT ONE of the existing 945 tests
noticed — every prior admin-reply test either called `admin_reply_to_question` directly
(skipping the filter entirely) or never varied the caller's capability. Two nets close this:

1. A DIRECT unit assertion on `is_question_reply()` itself — this is what actually catches the
   specific mutation (a stripped/inverted capability check inside the filter), since it observes
   the filter's own return value rather than an end-to-end side effect that a second, redundant
   enforcement layer (CapabilityMiddleware, see below) could mask.
2. A dispatch-level test (as the plan's Task 2 <action> literally asks for) driving a real
   reply-to-question Message through `admin.router.propagate_event` — proving the full chain
   (filter -> CapabilityMiddleware -> handler body) denies a non-holder and lets a holder
   through to the actual delivery.

Why both: `admin_reply_to_question` is protected TWICE — once by the `is_question_reply` filter
itself (`required_capability(special="question_reply")` + `has_capability`, admin.py:547-548),
and independently again by `CapabilityMiddleware.__call__`'s own shape-based re-derivation
(`admin_caps.py:526-534,644-645`) once a handler's filter has matched. That defense-in-depth
means a mutation to the FILTER's own check alone does not, by itself, change the dispatch-level
outcome (middleware still denies) — so a dispatch-only test would stay green even with the M1
mutation applied, silently failing to close the blind spot TEST-VALUE-260815.md identified. The
direct unit test (1) is what actually regresses on that exact mutation; ran manually against a
stubbed-always-True `is_question_reply` during this plan's execution and confirmed RED, then
reverted (no code change committed) -- see 13-01-SUMMARY.md.
"""
import asyncio

from database import db
from handlers import admin as admin_mod

from tests.test_roles_phase8 import (
    ADMIN_ID,
    GAME_MANAGER_ID,
    MANAGER_ID,
    FakeMessage,
    _roles_ready,
    dispatch_message,
)

_QUESTION_CARD_TEXT = "🆔 555\n❓ Вопрос от делегата: когда заезд?"


def test_is_question_reply_true_for_moderate_reg_holder(tmp_path):
    """Direct target of the M1 mutation: a reg_manager (holds moderate_reg) replying to a
    correctly-shaped question card is recognized by the filter itself."""
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    replied = FakeMessage(text=_QUESTION_CARD_TEXT)
    reply = FakeMessage(text="Заезд с 9 утра", user_id=MANAGER_ID, reply_to_message=replied)

    assert asyncio.run(admin_mod.is_question_reply(reply)) is True


def test_is_question_reply_false_for_non_holder(tmp_path):
    """Same shaped message, same reply -- but the replier only holds moderate_game (no
    moderate_reg). If the M1 mutation strips/inverts the capability check, this flips to True."""
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))
    replied = FakeMessage(text=_QUESTION_CARD_TEXT)
    reply = FakeMessage(text="Заезд с 9 утра", user_id=GAME_MANAGER_ID, reply_to_message=replied)

    assert asyncio.run(admin_mod.is_question_reply(reply)) is False


def test_is_question_reply_false_for_stranger_with_correct_shape(tmp_path):
    """A random Telegram user (no staff row at all) whose message happens to be a reply to
    something containing both markers must not match -- shape alone is not authorization."""
    _roles_ready(tmp_path)
    replied = FakeMessage(text=_QUESTION_CARD_TEXT)
    reply = FakeMessage(text="я тоже отвечу", user_id=999999, reply_to_message=replied)

    assert asyncio.run(admin_mod.is_question_reply(reply)) is False


def test_question_reply_dispatch_holder_reaches_body(tmp_path):
    """Task 2 <action>: drive a real reply-to-question event through admin.router
    (filter -> CapabilityMiddleware -> handler body) and confirm a moderate_reg holder's reply
    is actually delivered (legacy no-claim path -- no `Вопрос #N` marker in the card)."""
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    replied = FakeMessage(text=_QUESTION_CARD_TEXT)

    result, event = dispatch_message(
        "Заезд с 9 утра", MANAGER_ID, reply_to=replied,
    )

    assert any("✅ Ответ отправлен" in (text or "") for text, *_ in event.answers)


def test_question_reply_dispatch_non_holder_denied(tmp_path):
    """Mirror of the above: a known staff member WITHOUT moderate_reg must never reach the
    delivery body -- no delivery ack, no leaked "Недостаточно прав" via a DIFFERENT accidental
    match either (event.answers stays empty: the filter never matches for this caller, so
    nothing on admin.router claims the event at all)."""
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))
    replied = FakeMessage(text=_QUESTION_CARD_TEXT)

    result, event = dispatch_message(
        "Заезд с 9 утра", GAME_MANAGER_ID, reply_to=replied,
    )

    assert event.answers == []
    assert not any("✅ Ответ отправлен" in (text or "") for text, *_ in event.answers)
