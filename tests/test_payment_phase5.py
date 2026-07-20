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

    async def fake_show_details(bot, telegram_id, state, option_label, option_price):
        captured["label"] = option_label
        captured["price"] = option_price

    monkeypatch.setattr(pay, "_show_payment_details", fake_show_details)
    pay.init_payment_module(None)

    class FakeBot:
        id = 1

    asyncio.run(pay.start_payment_step(FakeBot(), 42, "party_overnight"))

    assert captured["label"] == "С ночёвкой"
    assert captured["price"] == 1500


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

    async def fake_show_details(bot, telegram_id, state, label, price):
        details_called["label"] = label
        details_called["price"] = price

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

    assert details_called == {"label": "Только вечеринка", "price": 1500}


def test_process_payment_option_untracked_option_no_eligibility_check(tmp_path, monkeypatch):
    """An option with tracks=None (offered to all) must not trigger the eligibility
    re-check at all — no get_user call needed, matches today's behavior for every existing
    2-field payment_options line."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_options", "Общий тариф|500"))

    details_called = {}

    async def fake_update_payment_status(*args, **kwargs):
        pass

    async def fake_show_details(bot, telegram_id, state, label, price):
        details_called["label"] = label
        details_called["price"] = price

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

    assert details_called == {"label": "Общий тариф", "price": 500}
