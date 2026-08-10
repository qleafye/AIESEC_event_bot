"""Phase 07.2 Plan 04 (per-city admin panels, CITY-02) tests: «Город мероприятия» as a
broadcast-segment filter.

Two halves, mirroring the two failure modes this plan exists to prevent:

1. `database.db` — `event_city` must be a legal filter column, the DEFAULT city must reach
   delegates whose `event_city IS NULL` (they are the entire pre-cities backlog), and an
   empty value must degenerate into the existing ME-04 fail-safe (empty audience) instead of
   a base-wide blast.
2. `handlers.admin` — the field must be registered in BOTH `_PICKER_FIELDS` and
   `db._FILTER_COLUMNS`. A field present in only one of the two is SILENTLY dropped
   (Phase 5 D-19): the manager sees the filter on screen, the SQL never receives it, and the
   broadcast goes to the wrong segment with nothing on screen to hint at it. Hence the
   two-way registration test.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_export_stats_phase72.py / tests/test_city_scope_phase72.py.
"""
import asyncio
import json

from config import config
from database import db
from database.db import _build_filter_clause


# ── Task 1: event_city in the filter whitelist + NULL-collapse branch ───────────────────

def test_event_city_is_whitelisted():
    assert "event_city" in db._FILTER_COLUMNS


def test_event_city_plain_equality_when_exclude_empty():
    assert _build_filter_clause([
        {"field": "event_city", "value": "spb", "exclude": []}
    ]) == (" WHERE event_city = ?", ["spb"])


def test_event_city_equality_when_exclude_key_absent():
    """A filter dict without the `exclude` key at all still works (plain equality) — the key
    is optional, never required."""
    assert _build_filter_clause([
        {"field": "event_city", "value": "spb"}
    ]) == (" WHERE event_city = ?", ["spb"])


def test_event_city_default_city_collapses_null():
    """The default city is described by EXCLUSION so it also catches event_city IS NULL —
    every application registered before the cities module existed."""
    assert _build_filter_clause([
        {"field": "event_city", "value": "msk", "exclude": ["spb", "tyumen"]}
    ]) == (" WHERE (event_city IS NULL OR event_city NOT IN (?, ?))", ["spb", "tyumen"])


def test_event_city_combined_with_other_field_keeps_and_and_bind_order():
    where, params = _build_filter_clause([
        {"field": "status", "value": "approved"},
        {"field": "event_city", "value": "msk", "exclude": ["spb", "tyumen"]},
    ])
    assert where == (
        " WHERE status = ? AND (event_city IS NULL OR event_city NOT IN (?, ?))"
    )
    assert params == ["approved", "spb", "tyumen"]

    where2, params2 = _build_filter_clause([
        {"field": "event_city", "value": "spb", "exclude": []},
        {"field": "status", "value": "approved"},
    ])
    assert where2 == " WHERE event_city = ? AND status = ?"
    assert params2 == ["spb", "approved"]


def test_event_city_empty_value_is_dropped():
    """Never emit a condition without a value — an empty value must drop the filter, after
    which count_and_list_filtered's ME-04 fail-safe returns an empty audience."""
    assert _build_filter_clause([{"field": "event_city", "value": ""}]) == ("", [])
    assert _build_filter_clause([{"field": "event_city", "value": None}]) == ("", [])


def test_event_city_filter_survives_json_round_trip():
    """A scheduled broadcast stores its filter spec as JSON and rebuilds the audience after a
    restart — the `exclude` key must survive dumps/loads and produce the SAME condition."""
    filters = [{"field": "event_city", "value": "msk", "exclude": ["spb", "tyumen"]}]
    before = _build_filter_clause(filters)
    restored = json.loads(json.dumps(filters, ensure_ascii=False))
    assert _build_filter_clause(restored) == before


# ── Task 1 end-to-end: the audience query on a seeded base ─────────────────────────────

def _seed_broadcast_base(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_broadcast72.db")

    async def go():
        await db.init_db()
        for tid, city in ((1, None), (2, "msk"), (3, "spb"), (4, "spb"), (5, "tyumen")):
            await db.add_user({
                "telegram_id": tid,
                "full_name": f"User {tid}",
                "registration_date": f"2026-01-01 09:{tid:02d}:00",
                "event_city": city,
            })

    asyncio.run(go())


def test_count_and_list_filtered_default_city_includes_null_rows(tmp_path):
    _seed_broadcast_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([
        {"field": "event_city", "value": "msk", "exclude": ["spb", "tyumen"]}
    ]))
    assert set(ids) == {1, 2}


def test_count_and_list_filtered_non_default_city_excludes_null_rows(tmp_path):
    _seed_broadcast_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([
        {"field": "event_city", "value": "spb", "exclude": []}
    ]))
    assert set(ids) == {3, 4}


def test_count_and_list_filtered_empty_city_value_returns_empty_audience(tmp_path):
    """Degenerate city filter must NEVER fan out to the whole base (ME-04 fail-safe)."""
    _seed_broadcast_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "event_city", "value": ""}]))
    assert ids == []


# ── Task 2: filter button, registry-sourced picker, human labels ───────────────────────

ADMIN_ID = 940101


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeState:
    """Minimal FSMContext stand-in — the filter flow only uses get_data/update_data/set_state."""

    def __init__(self, **data):
        self._data = dict(data)
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)
        return dict(self._data)

    async def set_state(self, state):
        self.state = state


def _admin_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_broadcast_ui72.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _cb_datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _btn_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def test_filter_menu_kb_without_show_city_is_unchanged():
    from handlers import admin as admin_mod
    kb = admin_mod._filter_menu_kb([])
    assert "filter_f_event_city" not in _cb_datas(kb)


def test_filter_menu_kb_show_city_adds_exactly_one_row():
    from handlers import admin as admin_mod
    assert (
        len(admin_mod._filter_menu_kb([], show_city=True).inline_keyboard)
        == len(admin_mod._filter_menu_kb([]).inline_keyboard) + 1
    )
    kb = admin_mod._filter_menu_kb([], show_city=True)
    assert "filter_f_event_city" in _cb_datas(kb)
    city_btn = [b for row in kb.inline_keyboard for b in row
                if b.callback_data == "filter_f_event_city"][0]
    assert "Город мероприятия" in city_btn.text
    # ...and it must stay visually distinct from the delegate's own city question
    assert "Город" in _btn_texts(kb)  # the existing filter_f_city button, unchanged


def test_filter_menu_kb_city_row_precedes_show_and_send():
    from handlers import admin as admin_mod
    kb = admin_mod._filter_menu_kb([{"field": "status", "value": "approved"}], show_city=True)
    datas = _cb_datas(kb)
    assert datas.index("filter_f_event_city") < datas.index("filter_count")


def test_render_filter_menu_hides_city_button_when_module_off(tmp_path):
    from handlers import admin as admin_mod
    _admin_ready(tmp_path)
    msg = FakeMessage()
    asyncio.run(admin_mod._render_filter_menu(msg, [], edit=True))
    assert "filter_f_event_city" not in _cb_datas(msg.markup)


def test_render_filter_menu_shows_city_button_when_module_on(tmp_path):
    from handlers import admin as admin_mod
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    msg = FakeMessage()
    asyncio.run(admin_mod._render_filter_menu(msg, [], edit=True))
    assert "filter_f_event_city" in _cb_datas(msg.markup)


def test_event_city_picker_options_come_from_registry_not_db(tmp_path):
    """The base below has NO spb rows at all — spb must STILL be offered, because the values
    come from the city registry. DISTINCT over the column would also drop the default city
    entirely (its rows are NULL)."""
    import cities
    from handlers import admin as admin_mod
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("filter_f_event_city")
    state = FakeState()
    asyncio.run(admin_mod._show_value_picker(cb, state, "event_city", "Выберите:"))
    data = asyncio.run(state.get_data())
    assert data["filter_options"] == [c["code"] for c in cities.CITIES]
    assert "spb" in data["filter_options"]


def test_event_city_picker_buttons_show_labels_not_codes(tmp_path):
    import cities
    from handlers import admin as admin_mod
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("filter_f_event_city")
    state = FakeState()
    asyncio.run(admin_mod._show_value_picker(cb, state, "event_city", "Выберите:"))
    texts = _btn_texts(cb.message.markup)
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label[:60] in texts
    assert "spb" not in texts


def test_event_city_picker_labels_survive_pagination(tmp_path):
    """Page navigation redraws the same keyboard — the human labels must not degrade back to
    raw codes on page 2."""
    import cities
    from handlers import admin as admin_mod
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("filter_f_event_city")
    state = FakeState()
    asyncio.run(admin_mod._show_value_picker(cb, state, "event_city", "Выберите:"))
    asyncio.run(state.update_data(filter_pending_field="event_city"))
    nav = FakeCallback("filter_optpage:0")
    asyncio.run(admin_mod.filter_page_nav(nav, state))
    texts = _btn_texts(nav.message.markup)
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label[:60] in texts
    assert "spb" not in texts


def _pick_city(tmp_path, code):
    import cities
    from handlers import admin as admin_mod
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("filter_f_event_city")
    state = FakeState()
    asyncio.run(admin_mod._show_value_picker(cb, state, "event_city", "Выберите:"))
    asyncio.run(state.update_data(filter_pending_field="event_city", filters=[]))
    options = (asyncio.run(state.get_data()))["filter_options"]
    pick = FakeCallback(f"filter_opt:{options.index(code)}")
    asyncio.run(admin_mod.filter_pick_value(pick, state))
    return (asyncio.run(state.get_data()))["filters"]


def test_picking_default_city_stores_non_empty_exclude(tmp_path):
    import cities
    default_code = cities.default_city_code()
    filters = _pick_city(tmp_path, default_code)
    assert len(filters) == 1
    f = filters[0]
    assert f["field"] == "event_city"
    assert f["value"] == default_code
    assert f["exclude"] == list(cities.city_scope(default_code)[1])
    assert f["exclude"]  # non-empty → the NULL-collapsing shape


def test_picking_non_default_city_stores_empty_exclude(tmp_path):
    filters = _pick_city(tmp_path, "spb")
    f = filters[0]
    assert f["value"] == "spb"
    assert f["exclude"] == []


def test_picked_city_filter_produces_expected_sql(tmp_path):
    """End-to-end: what the picker stores is exactly what the SQL builder consumes."""
    import cities
    filters = _pick_city(tmp_path, cities.default_city_code())
    where, params = _build_filter_clause(json.loads(json.dumps(filters, ensure_ascii=False)))
    assert "IS NULL" in where
    assert params == list(cities.city_scope(cities.default_city_code())[1])


def test_filter_summary_shows_city_label_not_code(tmp_path):
    import cities
    from handlers import admin as admin_mod
    filters = _pick_city(tmp_path, "spb")
    summary = admin_mod._filter_summary(filters)
    spb_label = asyncio.run(cities.city_label("spb"))
    assert "Город мероприятия" in summary
    assert spb_label in summary
    assert "= spb" not in summary


def test_filter_summary_unchanged_for_fields_without_label():
    from handlers import admin as admin_mod
    assert admin_mod._filter_summary([{"field": "city", "value": "Москва"}]) == "Город = Москва"
    assert admin_mod._filter_summary(
        [{"field": "payment_status", "value": "paid"}]
    ) == "Оплата = Оплатил"


def test_broadcast_filter_menu_opens_empty_even_with_selected_city(tmp_path):
    """Deliberate decision: the admin's selected city does NOT pre-fill the broadcast filter —
    a pre-set condition nobody typed is exactly how a manager sends to a third of the base
    while believing they sent to everyone."""
    import cities
    from handlers import admin as admin_mod
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("broadcast_filter")
    state = FakeState()
    asyncio.run(admin_mod.broadcast_filter_start(cb, state))
    assert (asyncio.run(state.get_data()))["filters"] == []
    assert "Фильтры пока не выбраны" in cb.message.text


# ── Two-way registration guard (Phase 5 D-19 — the trap this plan exists to close) ─────

def test_every_picker_field_is_a_whitelisted_filter_column():
    from handlers import admin as admin_mod
    assert sorted(admin_mod._PICKER_FIELDS - db._FILTER_COLUMNS) == []


def test_every_filter_menu_button_maps_to_a_picker_field():
    from handlers import admin as admin_mod
    kb = admin_mod._filter_menu_kb([], show_city=True)
    fields = [d[len("filter_f_"):] for d in _cb_datas(kb)
              if d and d.startswith("filter_f_") and d != "filter_f_date"]
    assert fields, "filter menu produced no field buttons"
    assert sorted(set(fields) - admin_mod._PICKER_FIELDS) == []


def test_event_city_registered_in_both_sets():
    from handlers import admin as admin_mod
    assert "event_city" in admin_mod._PICKER_FIELDS
    assert "event_city" in db._FILTER_COLUMNS
    assert admin_mod._FILTER_FIELD_LABELS["event_city"] == "Город мероприятия"


def test_filter_f_event_city_is_a_registered_callback():
    """`filter_pick_field` is subscribed to the set computed at import time — adding the code
    to _PICKER_FIELDS is what registers the callback."""
    from handlers import admin as admin_mod
    assert f"filter_f_event_city" in {f"filter_f_{fld}" for fld in admin_mod._PICKER_FIELDS}
