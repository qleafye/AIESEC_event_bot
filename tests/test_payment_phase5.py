"""Phase 5 (TRACK-05, D-16/D-17/D-18): track-aware payment tariffs.

Covers _parse_options' optional third `track` field, _visible_options' index-preservation
guarantee, the single/free fallback reading from the visible (not unfiltered) list, and
process_payment_option's server-side eligibility re-check (T-05-05-03).
"""
import asyncio

import database.db as db
import handlers.payment as pay


def _use_tmp_db(tmp_path):
    db.config.DB_PATH = str(tmp_path / "phase5_payment.db")


# --- D-16: _parse_options backward compatibility (2-field lines unchanged) -----------------

def test_parse_two_field_line_unchanged():
    """D-16 compatibility guarantee: a bare 'label|price' line parses exactly as before
    Phase 5 — tracks is None, meaning offered to ALL tracks."""
    assert pay._parse_options("Полное участие|5000") == [("Полное участие", 5000, None)]


def test_parse_pipeless_line_unchanged():
    assert pay._parse_options("A") == [("A", 0, None)]


def test_parse_non_integer_price_falls_back_to_zero():
    assert pay._parse_options("A|xx") == [("A", 0, None)]


def test_parse_multiline_preserves_order_and_skips_blanks():
    raw = "A|100\n\nB|200\n"
    assert pay._parse_options(raw) == [("A", 100, None), ("B", 200, None)]


# --- D-16: optional third track field -------------------------------------------------------

def test_parse_single_track_field():
    result = pay._parse_options("Вечеринка|1500|party_overnight")
    assert result == [("Вечеринка", 1500, {"party_overnight"})]


def test_parse_multiple_comma_separated_tracks():
    result = pay._parse_options("Вечеринка|1500|party_overnight,party_noovernight")
    assert result[0][2] == {"party_overnight", "party_noovernight"}


def test_parse_strips_whitespace_around_third_field_and_commas():
    result = pay._parse_options("Вечеринка|1500| party_overnight , party_noovernight ")
    assert result[0][2] == {"party_overnight", "party_noovernight"}


def test_parse_empty_third_field_yields_none_not_empty_set():
    """D-16: 'label|price|' (blank third field) means "offered to all tracks", not
    "matches nobody" — None, never an empty set."""
    assert pay._parse_options("A|100|") == [("A", 100, None)]


# --- D-17: _visible_options index-preservation ----------------------------------------------

def test_visible_options_full_track_skips_party_only_entry_without_renumbering():
    options = pay._parse_options("A|100\nB|200|party_overnight\nC|300")
    visible = pay._visible_options(options, "full")
    assert [t[0] for t in visible] == [0, 2]  # NOT [0, 1] — index 1 (party-only) is absent


def test_visible_options_party_track_sees_all_three_original_indices():
    options = pay._parse_options("A|100\nB|200|party_overnight\nC|300")
    visible = pay._visible_options(options, "party_overnight")
    assert [t[0] for t in visible] == [0, 1, 2]


def test_visible_options_wrong_subtrack_excluded():
    options = pay._parse_options("B|200|party_noovernight")
    assert pay._visible_options(options, "party_overnight") == []


def test_visible_options_no_track_field_always_visible_to_full():
    options = pay._parse_options("A|100")
    assert pay._visible_options(options, "full") == [(0, "A", 100)]


def test_visible_options_empty_when_nothing_matches():
    """D-18 precondition: every option track-restricted away from the given track."""
    options = pay._parse_options("A|100|party_overnight\nB|200|party_overnight")
    assert pay._visible_options(options, "party_noovernight") == []


def test_visible_options_single_visible_entry_keeps_its_nonzero_index_and_price():
    """BLOCKER-1 / T-05-05-07 regression: the party-eligible tariff sits at index 1 of the
    full list. The single-visible-entry tuple must surface (1, 'С ночёвкой', 1500) — the
    1000 ₽ no-overnight tariff at index 0 must never leak into a party_overnight user's view."""
    options = pay._parse_options(
        "Без ночёвки|1000|party_noovernight\nС ночёвкой|1500|party_overnight"
    )
    visible = pay._visible_options(options, "party_overnight")
    assert visible == [(1, "С ночёвкой", 1500)]


# --- start_payment_step signature + fallback source guard ------------------------------------

def test_start_payment_step_participant_type_defaults_to_full():
    import inspect
    params = inspect.signature(pay.start_payment_step).parameters
    assert params["participant_type"].default == "full"


def test_start_payment_step_never_reads_unfiltered_first_entry():
    import inspect
    src = inspect.getsource(pay.start_payment_step)
    assert "options[0]" not in src, "fallback still selects from the unfiltered list"


def test_no_re_enumeration_of_the_filtered_list():
    import inspect
    src = inspect.getsource(pay)
    assert src.count("enumerate(options)") >= 1
    assert "enumerate(visible)" not in src


# --- BLOCKER-1 regression: start_payment_step's fallback resolves the VISIBLE price ----------

def test_start_payment_step_single_visible_option_charges_visible_price(tmp_path, monkeypatch):
    """With a no-overnight tariff at index 0 and an overnight tariff at index 1, a
    party_overnight caller's single/free fallback must resolve label='С ночёвкой',
    price=1500 — never the 1000 ₽ tariff sitting at options[0]."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting(
        "payment_options", "Без ночёвки|1000|party_noovernight\nС ночёвкой|1500|party_overnight"
    ))

    captured = {}

    async def fake_show_details(bot, telegram_id, state, option_label, option_price, participant_type=None):
        captured["label"] = option_label
        captured["price"] = option_price
        captured["participant_type"] = participant_type

    monkeypatch.setattr(pay, "_show_payment_details", fake_show_details)
    pay.init_payment_module(None)

    class FakeBot:
        id = 1

    asyncio.run(pay.start_payment_step(FakeBot(), 42, "party_overnight"))

    assert captured["label"] == "С ночёвкой"
    assert captured["price"] == 1500
    # WR-01: the resolved track must reach _show_payment_details so a free/single-tariff
    # party delegate gets approve_text__party, not the global approve_text.
    assert captured["participant_type"] == "party_overnight"


def test_start_payment_step_no_match_routes_to_free_completion(tmp_path, monkeypatch):
    """D-18: no tariff matches the caller's track -> free completion path, never a stalled
    empty keyboard."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_options", "Только вечеринка|1500|party_overnight"))

    called = {}

    async def fake_completion(bot, telegram_id, participant_type=None, **kwargs):
        called["hit"] = True
        called["participant_type"] = participant_type

    monkeypatch.setattr("handlers.registration.send_completion_and_bonus", fake_completion)
    pay.init_payment_module(None)

    class FakeBot:
        id = 1

    asyncio.run(pay.start_payment_step(FakeBot(), 42, "full"))

    assert called.get("hit") is True


# --- T-05-05-03: process_payment_option server-side eligibility re-check ---------------------

def test_process_payment_option_rejects_cross_track_selection(tmp_path, monkeypatch):
    """A caller whose CURRENT track excludes the tapped option's track set must be rejected
    with an alert, and neither update_payment_status nor _show_payment_details may run —
    closes T-05-05-03 (a stale keyboard from another track cannot buy a foreign tariff)."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_options", "Только вечеринка|1500|party_overnight"))
    asyncio.run(db.add_user({
        "telegram_id": 99, "full_name": "A B", "registration_date": "2026-07-01",
        "participant_type": "full",
    }))

    update_called = {}
    details_called = {}

    async def fake_update_payment_status(*args, **kwargs):
        update_called["hit"] = True

    async def fake_show_details(*args, **kwargs):
        details_called["hit"] = True

    monkeypatch.setattr(pay, "update_payment_status", fake_update_payment_status)
    monkeypatch.setattr(pay, "_show_payment_details", fake_show_details)

    answers = []

    class FakeCallback:
        data = "pay_option:0"

        class from_user:
            id = 99

        async def answer(self, text=None, show_alert=False):
            answers.append((text, show_alert))

    class FakeState:
        async def clear(self):
            pass

    asyncio.run(pay.process_payment_option(FakeCallback(), FakeState()))

    assert not update_called
    assert not details_called
    assert answers and answers[0][1] is True  # show_alert=True


def test_process_payment_option_allows_matching_track(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_options", "Только вечеринка|1500|party_overnight"))
    asyncio.run(db.add_user({
        "telegram_id": 100, "full_name": "A B", "registration_date": "2026-07-01",
        "participant_type": "party_overnight",
    }))

    details_called = {}

    async def fake_update_payment_status(*args, **kwargs):
        pass

    async def fake_show_details(bot, telegram_id, state, label, price, participant_type=None):
        details_called["label"] = label
        details_called["price"] = price
        details_called["participant_type"] = participant_type

    monkeypatch.setattr(pay, "update_payment_status", fake_update_payment_status)
    monkeypatch.setattr(pay, "_show_payment_details", fake_show_details)

    class FakeCallback:
        data = "pay_option:0"
        bot = object()

        class from_user:
            id = 100

        async def answer(self, text=None, show_alert=False):
            pass

    class FakeState:
        pass

    asyncio.run(pay.process_payment_option(FakeCallback(), FakeState()))

    assert details_called == {
        "label": "Только вечеринка", "price": 1500, "participant_type": "party_overnight",
    }


def test_process_payment_option_untracked_option_resolves_track_for_completion_text(tmp_path, monkeypatch):
    """WR-01: an option with tracks=None (offered to all) still resolves the caller's current
    track via get_user and threads it into _show_payment_details, so a party delegate picking
    a free option among several still gets approve_text__party — get_user is now called
    unconditionally (not gated on `tracks is not None`) precisely so this value is available."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_options", "Общий тариф|500"))
    asyncio.run(db.add_user({
        "telegram_id": 12346, "full_name": "A B", "registration_date": "2026-07-01",
        "participant_type": "party_noovernight",
    }))

    details_called = {}

    async def fake_update_payment_status(*args, **kwargs):
        pass

    async def fake_show_details(bot, telegram_id, state, label, price, participant_type=None):
        details_called["label"] = label
        details_called["price"] = price
        details_called["participant_type"] = participant_type

    monkeypatch.setattr(pay, "update_payment_status", fake_update_payment_status)
    monkeypatch.setattr(pay, "_show_payment_details", fake_show_details)

    class FakeCallback:
        data = "pay_option:0"
        bot = object()

        class from_user:
            id = 12346

        async def answer(self, text=None, show_alert=False):
            pass

    class FakeState:
        pass

    asyncio.run(pay.process_payment_option(FakeCallback(), FakeState()))

    assert details_called == {
        "label": "Общий тариф", "price": 500, "participant_type": "party_noovernight",
    }


def test_process_payment_option_untracked_option_no_users_row_defaults_to_full(tmp_path, monkeypatch):
    """An untracked option tapped by a caller with no users row at all must not crash — the
    track resolution degrades to 'full', matching get_user's None-safe fallback everywhere
    else in the codebase (mirrors approve_user's try/except default)."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_options", "Общий тариф|500"))

    details_called = {}

    async def fake_update_payment_status(*args, **kwargs):
        pass

    async def fake_show_details(bot, telegram_id, state, label, price, participant_type=None):
        details_called["label"] = label
        details_called["price"] = price
        details_called["participant_type"] = participant_type

    monkeypatch.setattr(pay, "update_payment_status", fake_update_payment_status)
    monkeypatch.setattr(pay, "_show_payment_details", fake_show_details)

    class FakeCallback:
        data = "pay_option:0"
        bot = object()

        class from_user:
            id = 12345  # no users row at all

        async def answer(self, text=None, show_alert=False):
            pass

    class FakeState:
        pass

    asyncio.run(pay.process_payment_option(FakeCallback(), FakeState()))

    assert details_called == {
        "label": "Общий тариф", "price": 500, "participant_type": "full",
    }


# --- WR-01: _show_payment_details threads participant_type into the free-path completion ----

def test_show_payment_details_free_path_threads_participant_type_to_completion(tmp_path, monkeypatch):
    """WR-01: _show_payment_details' free-participation branch (price 0, no requisites) must
    pass participant_type through to send_completion_and_bonus so a party delegate with a
    free tariff gets approve_text__party, not the global approve_text."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    called = {}

    async def fake_completion(bot, telegram_id, participant_type=None, **kwargs):
        called["participant_type"] = participant_type

    monkeypatch.setattr("handlers.registration.send_completion_and_bonus", fake_completion)

    class FakeBot:
        id = 1

    class FakeState:
        async def clear(self):
            pass

    asyncio.run(pay._show_payment_details(
        FakeBot(), 55, FakeState(), "Бесплатно", 0, participant_type="party_noovernight"
    ))

    assert called["participant_type"] == "party_noovernight"


def test_show_payment_details_free_path_defaults_participant_type_to_none(tmp_path, monkeypatch):
    """Byte-identical pre-Phase-5 behavior when the caller doesn't know the track — matches
    every pre-Phase-5 call site of send_completion_and_bonus."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    called = {}

    async def fake_completion(bot, telegram_id, participant_type=None, **kwargs):
        called["hit"] = True
        called["participant_type"] = participant_type

    monkeypatch.setattr("handlers.registration.send_completion_and_bonus", fake_completion)

    class FakeBot:
        id = 1

    class FakeState:
        async def clear(self):
            pass

    asyncio.run(pay._show_payment_details(FakeBot(), 56, FakeState(), "Бесплатно", 0))

    assert called["hit"] is True
    assert called["participant_type"] is None


def test_free_tariff_party_delegate_receives_approve_text_party_end_to_end(tmp_path):
    """End-to-end (a): a party delegate whose single/only tariff is free must receive
    approve_text__party — not the global approve_text — driven through the REAL (unmocked)
    _show_payment_details -> send_completion_and_bonus -> _approve_text_for chain."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("approve_text", "Глобальный текст"))
    asyncio.run(db.set_setting("approve_text__party", "Партийный текст"))

    sent = {}

    class FakeBot:
        id = 1

        async def send_message(self, chat_id, text, **kwargs):
            sent["text"] = text

    class FakeState:
        async def clear(self):
            pass

        async def set_state(self, *a, **k):
            pass

    asyncio.run(pay._show_payment_details(
        FakeBot(), 57, FakeState(), "Бесплатно", 0, participant_type="party_overnight"
    ))

    assert sent["text"] == "Партийный текст"


# --- BLOCKER-party-payment-reentry: upload_receipt_entry threads the delegate's OWN track ----

def test_upload_receipt_entry_threads_own_track_not_full(tmp_path, monkeypatch):
    """A party_overnight delegate re-entering payment via the "Оплата" button must be filtered
    against their OWN track's tariffs, not the "full" default — closes BLOCKER-party-payment-
    reentry (start_payment_step previously received no participant_type)."""
    import handlers.user_actions as user_actions

    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_user({
        "telegram_id": 777, "full_name": "Party Delegate", "registration_date": "2026-07-24",
        "participant_type": "party_overnight", "status": "approved", "payment_status": "not_paid",
    }))
    asyncio.run(db.set_setting("payment_enabled", "on"))
    asyncio.run(db.set_setting("payment_requisites", "Сбербанк 1234 5678 9012 3456"))
    asyncio.run(db.set_setting(
        "payment_options",
        "Без ночёвки|1000|party_noovernight\nС ночёвкой|1500|party_overnight",
    ))

    captured = {}

    async def fake_start_payment_step(bot, telegram_id, participant_type="full"):
        captured["participant_type"] = participant_type

    monkeypatch.setattr(pay, "start_payment_step", fake_start_payment_step)

    class FakeFromUser:
        id = 777

    class FakeMessage:
        from_user = FakeFromUser()

        async def answer(self, *args, **kwargs):
            pass

    class FakeBot:
        id = 1

    asyncio.run(user_actions.upload_receipt_entry(FakeMessage(), FakeBot()))

    assert captured["participant_type"] == "party_overnight"
