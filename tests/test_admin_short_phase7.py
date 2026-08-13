"""Phase 7 Plan 3 (admin UI, short track) tests: третий трек в переключателе, 2-состояние
toggle reg_q_stoggle:, изоляция __short namespace, детерминизм пресета «⚡ Акция: 6 вопросов»,
гейт материализации вкладки и метка трека в карточке заявки.

Same driving idiom as tests/test_admin_phase5.py: pytest-asyncio unavailable in this env —
every async helper is driven via asyncio.run() and config.DB_PATH points at a tmp_path file.
"""
import asyncio

import aiosqlite

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability
from handlers.registration import REG_FLOW, REG_PRESETS, _apply_short_preset


ADMIN_ID = 900002


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_admin_short7.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


async def _all_settings() -> dict:
    """Raw snapshot of every row in bot_settings — used to prove no unexpected key appears."""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute("SELECT key, value FROM bot_settings") as cursor:
            rows = await cursor.fetchall()
            return dict(rows)


def _drain():
    """Await any background tasks spawned via services.background.spawn (_refresh_short_sheet_header
    is fire-and-forget) so assertions on the fake ensure_named_sheet_header see the call."""
    async def _g():
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
    return _g


# ── Group 1: track switcher (three buttons, reg_q_track_switch validation) ───────────────

def test_track_switcher_row_has_three_buttons_short_active():
    kb = admin_mod._track_switcher_row("short")
    cbs = [b.callback_data for b in kb]
    assert cbs == ["reg_q_track:full", "reg_q_track:party", "reg_q_track:short"]
    assert kb[2].text.startswith("• ")
    assert not kb[0].text.startswith("• ")
    assert not kb[1].text.startswith("• ")


def test_reg_q_track_switch_short_renders_short_screen(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_q_track:short")
    asyncio.run(admin_mod.reg_q_track_switch(cb))
    assert cb.message.edit_calls == 1
    assert "Краткая" in cb.message.text
    flat = _flat_callback_data(cb.message.markup)
    assert any(cd.startswith("reg_q_stoggle:") for cd in flat)
    assert "reg_q_track:short" in flat


def test_reg_q_track_switch_garbage_value_collapses_to_full(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_q_track:bogus")
    asyncio.run(admin_mod.reg_q_track_switch(cb))
    flat = _flat_callback_data(cb.message.markup)
    assert any(cd.startswith("reg_q_toggle:") for cd in flat)
    assert not any(cd.startswith("reg_q_stoggle:") for cd in flat)
    assert not any(cd.startswith("reg_q_ptoggle:") for cd in flat)


# ── Group 2: setting_key validation against REG_FLOW (T-07-09, guarantee #8) ─────────────

def test_toggle_short_question_rejects_unknown_key(tmp_path):
    _admin_ready(tmp_path)
    before = asyncio.run(_all_settings())
    cb = FakeCallback("reg_q_stoggle:reg_q_НЕСУЩЕСТВУЕТ")
    asyncio.run(admin_mod.toggle_short_question(cb))
    after = asyncio.run(_all_settings())
    assert after == before  # not a single new bot_settings row
    assert cb.answers and cb.answers[0][1] is True  # alert shown


def test_toggle_short_question_rejects_non_reg_flow_key_party_enabled(tmp_path):
    """T-07-09: a crafted callback carrying a non-question global key must not be able to
    write a bot_settings row under any suffix."""
    _admin_ready(tmp_path)
    before = asyncio.run(_all_settings())
    cb = FakeCallback("reg_q_stoggle:party_enabled")
    asyncio.run(admin_mod.toggle_short_question(cb))
    after = asyncio.run(_all_settings())
    assert after == before
    assert asyncio.run(db.get_setting("party_enabled__short")) is None
    assert cb.answers and cb.answers[0][1] is True


def test_toggle_short_question_is_capability_guarded():
    # Phase 8 / D-01: the old per-handler `config.ADMIN_IDS` check (and the direct-call test
    # that exercised it) is gone (08-04, one-shot migration, D-03) -- CapabilityMiddleware is
    # now the ONLY enforcement point, and it only wraps events dispatched through the real
    # router, not direct handler calls. The structural guarantee survives with a new carrier:
    # the handler stays registered, and its callback_data resolves to a real capability.
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "toggle_short_question" in names
    setting_key = REG_FLOW[0][1]
    assert required_capability(callback_data=f"reg_q_stoggle:{setting_key}") == "settings"


# ── Group 3: namespace isolation (writes ONLY the __short-suffixed key) ──────────────────

def test_toggle_short_question_writes_only_short_suffix(tmp_path):
    _admin_ready(tmp_path)
    setting_key = "reg_q_city"
    asyncio.run(admin_mod.toggle_short_question(FakeCallback(f"reg_q_stoggle:{setting_key}")))
    assert asyncio.run(db.get_setting(f"{setting_key}__short")) == "on"
    assert asyncio.run(db.get_setting(setting_key)) is None
    assert asyncio.run(db.get_setting(f"{setting_key}__party")) is None


# ── Group 4: two-state cycle (absent -> on -> off -> on), never delete_setting ───────────

def test_toggle_short_question_two_state_cycle(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    setting_key = REG_FLOW[0][1]
    short_key = f"{setting_key}__short"

    delete_calls = []
    orig_delete = db.delete_setting

    async def spy_delete(key):
        delete_calls.append(key)
        await orig_delete(key)

    monkeypatch.setattr(admin_mod, "delete_setting", spy_delete)

    assert asyncio.run(db.get_setting(short_key)) is None

    asyncio.run(admin_mod.toggle_short_question(FakeCallback(f"reg_q_stoggle:{setting_key}")))
    assert asyncio.run(db.get_setting(short_key)) == "on"

    asyncio.run(admin_mod.toggle_short_question(FakeCallback(f"reg_q_stoggle:{setting_key}")))
    assert asyncio.run(db.get_setting(short_key)) == "off"

    asyncio.run(admin_mod.toggle_short_question(FakeCallback(f"reg_q_stoggle:{setting_key}")))
    assert asyncio.run(db.get_setting(short_key)) == "on"

    assert delete_calls == []  # short-cycle never calls delete_setting


# ── Group 5: preset determinism (_apply_short_preset) ────────────────────────────────────

def test_apply_short_preset_is_deterministic_and_repeatable(tmp_path):
    _admin_ready(tmp_path)
    # Manual disarray before applying the preset.
    asyncio.run(db.set_setting("reg_q_age__short", "on"))
    # reg_q_phone__short intentionally left absent.

    asyncio.run(_apply_short_preset())

    on_set = set(REG_PRESETS["short"]["on"])
    snapshot_1 = {}
    for _step_key, setting_key, *_rest in REG_FLOW:
        val = asyncio.run(db.get_setting(f"{setting_key}__short"))
        assert val in ("on", "off")  # every REG_FLOW key has an explicit __short value
        snapshot_1[setting_key] = val
    on_count = sum(1 for v in snapshot_1.values() if v == "on")
    assert on_count == len(on_set) == 5
    for setting_key in on_set:
        assert snapshot_1[setting_key] == "on"

    # Repeated application yields an identical snapshot.
    asyncio.run(_apply_short_preset())
    snapshot_2 = {}
    for _step_key, setting_key, *_rest in REG_FLOW:
        snapshot_2[setting_key] = asyncio.run(db.get_setting(f"{setting_key}__short"))
    assert snapshot_2 == snapshot_1


def test_apply_short_preset_does_not_touch_other_namespaces(tmp_path):
    """Snapshot of every non-__short key (reg_q_*, reg_q_*__party, payment_enabled,
    registration_mode) must be byte-identical before/after _apply_short_preset()."""
    _admin_ready(tmp_path)
    for _step_key, setting_key, *_rest in REG_FLOW:
        asyncio.run(db.set_setting(setting_key, "on"))
        asyncio.run(db.set_setting(f"{setting_key}__party", "off"))
    asyncio.run(db.set_setting("payment_enabled", "on"))
    asyncio.run(db.set_setting("registration_mode", "full"))

    def _snapshot_others():
        all_settings = asyncio.run(_all_settings())
        return {k: v for k, v in all_settings.items() if not k.endswith("__short")}

    before = _snapshot_others()
    asyncio.run(_apply_short_preset())
    after = _snapshot_others()
    assert after == before


# ── Group 6 (folded into 2/3): unknown-key rejection also covered above ─────────────────


# ── Group 7: sheet-tab materialization gate (registration_mode == "short") ──────────────

def test_toggle_short_question_no_tab_when_mode_full(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    calls = []

    async def fake_ensure(tab, headers):
        calls.append((tab, headers))

    import services.sheets as sheets_mod
    monkeypatch.setattr(sheets_mod, "ensure_named_sheet_header", fake_ensure)

    async def go():
        # registration_mode unset -> defaults to "short" per the registry (07-01 SUMMARY),
        # so pin it explicitly to "full" to exercise the gate's negative branch.
        await db.set_setting("registration_mode", "full")
        await admin_mod.toggle_short_question(FakeCallback("reg_q_stoggle:reg_q_city"))
        await _drain()()

    asyncio.run(go())
    assert calls == []


def test_toggle_short_question_materializes_tab_when_mode_short(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    calls = []

    async def fake_ensure(tab, headers):
        calls.append((tab, headers))

    import services.sheets as sheets_mod
    monkeypatch.setattr(sheets_mod, "ensure_named_sheet_header", fake_ensure)

    async def go():
        await db.set_setting("registration_mode", "short")
        await admin_mod.toggle_short_question(FakeCallback("reg_q_stoggle:reg_q_city"))
        await _drain()()

    asyncio.run(go())
    assert len(calls) == 1
    tab, headers = calls[0]
    assert tab == "Краткая"
    assert isinstance(headers, list) and headers


# ── Group 8: track label on the moderation card ──────────────────────────────────────────

def test_card_shows_short_track_line():
    u = {"telegram_id": 1, "full_name": "Иван", "participant_type": "short"}
    out = admin_mod._render_application_card(u, 1, 1)
    assert "⚡ Трек: краткая анкета (акция)" in out


def test_card_byte_identical_for_full_track_short_regression():
    u = {"telegram_id": 1, "full_name": "Иван", "participant_type": "full"}
    out = admin_mod._render_application_card(u, 1, 1)
    assert "Трек" not in out


def test_card_escapes_unrecognised_track_value_still_holds():
    """Regression T-05-03-03: adding the "short" branch must not disturb the HTML-escape
    fallback for any other unrecognised participant_type value."""
    u = {"telegram_id": 1, "full_name": "Иван", "participant_type": "<b>hack</b>"}
    out = admin_mod._render_application_card(u, 1, 1)
    assert "<b>hack</b>" not in out
    assert "&lt;b&gt;hack" in out
