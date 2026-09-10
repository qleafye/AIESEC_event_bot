"""Квик 260910-vfl (SEASON-FILTER-01..06): «Сезон» как поле фильтра рассылки.

Инцидент 10.09: на прод залито 482 делегата сезона «YL 26/1» рядом с 1691 делегатом
«YL 26/2» — кнопка «Всем» стала бить по 2173 адресатам вместо 1691. Поле `users.season`
в базе есть (07.3-A), но в whitelist фильтров рассылки его не было — сегментировать
было нечем.

Два раздела, по образцу `tests/test_city_broadcast_phase72.py`:

1. «SQL» (`database.db`) — `season` в `_FILTER_COLUMNS`, отдельная ветка `_build_filter_clause`,
   сентинел `SEASON_NONE` для легаси-строк без сезона, `get_season_filter_options`.
2. «Экран» (`handlers.admin_broadcasts`) — кнопка «Сезон» в меню фильтров (только когда
   сезонов больше одного), пикер значений кнопками, сводка условий.

Правило двойной регистрации поля (прецедент Фазы 5, D-19, тот же, что у `event_city`):
поле фильтра обязано быть в ОБОИХ местах — `db._FILTER_COLUMNS` и
`handlers.admin_broadcasts._PICKER_FIELDS`. Если поле есть только в одном из двух, оно
либо не доходит до SQL (виден на экране, молча не фильтрует), либо не появляется на
экране вовсе. Один из тестов ниже проверяет именно эту связку.

pytest-asyncio в этом окружении не установлен — каждый async-хелпер гоняется через
`asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path`, как в
`tests/test_city_broadcast_phase72.py`.
"""
import asyncio
import json

from config import config
from database import db
from database.db import _build_filter_clause


# ── Задача 1: «SQL» — whitelist, ветка _build_filter_clause, SEASON_NONE ───────────────

def test_season_is_whitelisted():
    assert "season" in db._FILTER_COLUMNS


def test_season_plain_equality():
    assert _build_filter_clause([
        {"field": "season", "value": "YL 26/2"}
    ]) == (" WHERE season = ?", ["YL 26/2"])


def test_season_none_sentinel_collapses_to_null_or_blank():
    assert _build_filter_clause([
        {"field": "season", "value": db.SEASON_NONE}
    ]) == (" WHERE (season IS NULL OR TRIM(season) = '')", [])


def test_season_empty_value_is_fail_closed():
    """WR-01 (тот же довод, что у event_city): пустое значение НЕ снимает условие, а
    эмитит заведомо ложное «0» — аудитория гарантированно пуста."""
    assert _build_filter_clause([{"field": "season", "value": ""}]) == (" WHERE 0", [])


def test_season_empty_value_with_other_filter_does_not_fan_out():
    where, params = _build_filter_clause([
        {"field": "status", "value": "approved"},
        {"field": "season", "value": ""},
    ])
    assert where == " WHERE status = ? AND 0"
    assert params == ["approved"]


def test_season_combined_with_other_field_keeps_and_and_bind_order():
    where, params = _build_filter_clause([
        {"field": "status", "value": "approved"},
        {"field": "season", "value": "YL 26/2"},
    ])
    assert where == " WHERE status = ? AND season = ?"
    assert params == ["approved", "YL 26/2"]


def test_season_filter_survives_json_round_trip():
    """Отложенная рассылка хранит спеку фильтра как JSON и пересобирает аудиторию после
    рестарта — round-trip обязан давать то же самое условие (доказательство того, что
    отложенная рассылка бьёт по той же аудитории, что и мгновенная)."""
    filters = [{"field": "season", "value": "YL 26/2"}]
    before = _build_filter_clause(filters)
    restored = json.loads(json.dumps(filters, ensure_ascii=False))
    assert _build_filter_clause(restored) == before


def test_season_none_sentinel_survives_json_round_trip():
    filters = [{"field": "season", "value": db.SEASON_NONE}]
    before = _build_filter_clause(filters)
    restored = json.loads(json.dumps(filters, ensure_ascii=False))
    assert _build_filter_clause(restored) == before
    assert restored[0]["value"] == db.SEASON_NONE


# ── Задача 1: сквозной тест на засеянной базе ───────────────────────────────────────────

def _seed_season_base(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_filter_260910.db")

    async def go():
        await db.init_db()
        rows = [
            (1, "YL 26/2"), (2, "YL 26/2"), (3, "YL 26/2"),
            (4, "YL 26/1"), (5, "YL 26/1"),
            (6, None),
        ]
        for tid, season in rows:
            await db.add_user({
                "telegram_id": tid,
                "full_name": f"User {tid}",
                "registration_date": f"2026-01-01 09:{tid:02d}:00",
                "season": season,
            })

    asyncio.run(go())


def test_count_and_list_filtered_by_season_yl262(tmp_path):
    _seed_season_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "season", "value": "YL 26/2"}]))
    assert set(ids) == {1, 2, 3}


def test_count_and_list_filtered_by_season_yl261(tmp_path):
    _seed_season_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "season", "value": "YL 26/1"}]))
    assert set(ids) == {4, 5}


def test_count_and_list_filtered_by_season_none_only_legacy_row(tmp_path):
    _seed_season_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "season", "value": db.SEASON_NONE}]))
    assert set(ids) == {6}


def test_get_season_filter_options_real_seasons_alphabetical_then_none_last(tmp_path):
    _seed_season_base(tmp_path)
    options = asyncio.run(db.get_season_filter_options())
    assert options == ["YL 26/1", "YL 26/2", db.SEASON_NONE]


def test_get_season_filter_options_no_legacy_rows_no_sentinel(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_filter_no_legacy_260910.db")

    async def go():
        await db.init_db()
        await db.add_user({
            "telegram_id": 1, "full_name": "User 1",
            "registration_date": "2026-01-01 09:00:00", "season": "YL 26/2",
        })
        await db.add_user({
            "telegram_id": 2, "full_name": "User 2",
            "registration_date": "2026-01-01 09:01:00", "season": "YL 26/1",
        })

    asyncio.run(go())
    options = asyncio.run(db.get_season_filter_options())
    assert options == ["YL 26/1", "YL 26/2"]


def test_get_season_filter_options_empty_base(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_filter_empty_260910.db")
    asyncio.run(db.init_db())
    assert asyncio.run(db.get_season_filter_options()) == []


# ── Задача 2: «Экран» — кнопка «Сезон», пикер, сводка ───────────────────────────────────
# Харнес скопирован из tests/test_city_broadcast_phase72.py.

ADMIN_ID = 940102


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
    """Минимальная замена FSMContext — рассылочному фильтру нужны только get_data/update_data."""

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


def _cb_datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _btn_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _seed_users(tmp_path, dbname, rows):
    """rows: список (telegram_id, season) — season=None пишет легаси-строку без сезона."""
    config.DB_PATH = str(tmp_path / dbname)
    config.ADMIN_IDS = [ADMIN_ID]

    async def go():
        await db.init_db()
        for tid, season in rows:
            await db.add_user({
                "telegram_id": tid,
                "full_name": f"User {tid}",
                "registration_date": f"2026-01-01 09:{tid:02d}:00",
                "season": season,
            })

    asyncio.run(go())


def test_season_double_registration():
    """Двойная регистрация поля (прецедент Фазы 5, D-19) — одним тестом на связку."""
    from handlers import admin_broadcasts
    assert "season" in admin_broadcasts._PICKER_FIELDS
    assert "season" in db._FILTER_COLUMNS


def test_filter_field_label_season():
    from handlers import admin_broadcasts
    assert admin_broadcasts._FILTER_FIELD_LABELS["season"] == "Сезон"


def test_filter_menu_kb_default_has_no_season_button():
    from handlers import admin_broadcasts
    kb = admin_broadcasts._filter_menu_kb([])
    assert "filter_f_season" not in _cb_datas(kb)


def test_filter_menu_kb_show_season_adds_exactly_one_button():
    from handlers import admin_broadcasts
    kb_before = admin_broadcasts._filter_menu_kb([])
    kb = admin_broadcasts._filter_menu_kb([], show_season=True)
    assert len(kb.inline_keyboard) == len(kb_before.inline_keyboard) + 1
    assert _cb_datas(kb).count("filter_f_season") == 1
    season_btn = [b for row in kb.inline_keyboard for b in row
                  if b.callback_data == "filter_f_season"][0]
    assert season_btn.text == "Сезон"


def test_render_filter_menu_two_seasons_shows_button(tmp_path):
    _seed_users(tmp_path, "render_two_seasons.db", [(1, "YL 26/2"), (2, "YL 26/1")])
    from handlers import admin_broadcasts
    msg = FakeMessage()
    asyncio.run(admin_broadcasts._render_filter_menu(msg, [], edit=True))
    assert "filter_f_season" in _cb_datas(msg.markup)


def test_render_filter_menu_one_season_no_legacy_hides_button(tmp_path):
    _seed_users(tmp_path, "render_one_season.db", [(1, "YL 26/2"), (2, "YL 26/2")])
    from handlers import admin_broadcasts
    msg = FakeMessage()
    asyncio.run(admin_broadcasts._render_filter_menu(msg, [], edit=True))
    assert "filter_f_season" not in _cb_datas(msg.markup)


def test_render_filter_menu_one_season_plus_legacy_shows_button(tmp_path):
    """Один настоящий сезон + легаси-строки — кнопка всё равно нужна (два варианта выбора:
    сезон и «Без сезона»)."""
    _seed_users(tmp_path, "render_one_season_legacy.db", [(1, "YL 26/2"), (2, None)])
    from handlers import admin_broadcasts
    msg = FakeMessage()
    asyncio.run(admin_broadcasts._render_filter_menu(msg, [], edit=True))
    assert "filter_f_season" in _cb_datas(msg.markup)


def test_filter_pick_field_season_two_seasons_shows_real_values(tmp_path):
    _seed_users(tmp_path, "pick_two_seasons.db", [(1, "YL 26/2"), (2, "YL 26/2"), (3, "YL 26/1")])
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_season")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    texts = _btn_texts(cb.message.markup)
    assert "YL 26/1" in texts
    assert "YL 26/2" in texts


def test_filter_pick_field_season_with_legacy_last_option_reads_none_label(tmp_path):
    _seed_users(tmp_path, "pick_legacy.db", [(1, "YL 26/2"), (2, "YL 26/1"), (3, None)])
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_season")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    data = asyncio.run(state.get_data())
    assert data["filter_options"][-1] == db.SEASON_NONE
    texts = _btn_texts(cb.message.markup)
    # последняя кнопка перед «← Назад» — легаси-вариант, читается «Без сезона», не сентинелом
    assert texts[-2] == "Без сезона"
    assert db.SEASON_NONE not in texts


def test_filter_pick_field_season_single_season_alerts_and_does_not_redraw(tmp_path):
    """Гейт живёт в хэндлере: инлайн-кнопки не истекают, вчерашнее меню с кнопкой «Сезон»
    сегодня (после того как второй сезон исчез бы) не должно давать бессмысленный фильтр."""
    _seed_users(tmp_path, "pick_single_season.db", [(1, "YL 26/2"), (2, "YL 26/2")])
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_season")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    assert cb.message.text is None
    assert cb.message.markup is None
    assert cb.answers
    text, show_alert = cb.answers[-1]
    assert show_alert is True


def test_filter_pick_value_season_real_value(tmp_path):
    _seed_users(tmp_path, "pick_value_real.db", [(1, "YL 26/2"), (2, "YL 26/1")])
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_season")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    asyncio.run(state.update_data(filters=[]))
    options = (asyncio.run(state.get_data()))["filter_options"]
    idx = options.index("YL 26/2")
    pick = FakeCallback(f"filter_opt:{idx}")
    asyncio.run(admin_broadcasts.filter_pick_value(pick, state))
    filters = (asyncio.run(state.get_data()))["filters"]
    assert filters == [{"field": "season", "value": "YL 26/2"}]


def test_filter_pick_value_season_none_sentinel_carries_label(tmp_path):
    _seed_users(tmp_path, "pick_value_none.db", [(1, "YL 26/2"), (2, None)])
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_season")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    asyncio.run(state.update_data(filters=[]))
    options = (asyncio.run(state.get_data()))["filter_options"]
    idx = options.index(db.SEASON_NONE)
    pick = FakeCallback(f"filter_opt:{idx}")
    asyncio.run(admin_broadcasts.filter_pick_value(pick, state))
    filters = (asyncio.run(state.get_data()))["filters"]
    assert filters == [{"field": "season", "value": db.SEASON_NONE, "label": "Без сезона"}]


def test_filter_summary_season_real_value():
    from handlers import admin_broadcasts
    assert admin_broadcasts._filter_summary(
        [{"field": "season", "value": "YL 26/2"}]
    ) == "Сезон = YL 26/2"


def test_filter_summary_season_none_sentinel_shows_label():
    from handlers import admin_broadcasts
    assert admin_broadcasts._filter_summary(
        [{"field": "season", "value": db.SEASON_NONE, "label": "Без сезона"}]
    ) == "Сезон = Без сезона"


def test_count_and_list_filtered_via_picked_season_spec(tmp_path):
    """Счётчик меняется: спека, собранная пикером, отдаёт только делегатов выбранного сезона."""
    _seed_users(tmp_path, "counter.db", [(1, "YL 26/2"), (2, "YL 26/2"), (3, "YL 26/1")])
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_season")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    asyncio.run(state.update_data(filters=[]))
    options = (asyncio.run(state.get_data()))["filter_options"]
    idx = options.index("YL 26/2")
    pick = FakeCallback(f"filter_opt:{idx}")
    asyncio.run(admin_broadcasts.filter_pick_value(pick, state))
    filters = (asyncio.run(state.get_data()))["filters"]
    ids = asyncio.run(db.count_and_list_filtered(filters))
    assert set(ids) == {1, 2}


def test_required_capability_filter_f_season_is_broadcast():
    """Доказательство, что записи в ADMIN_CAPS не нужны — `filter_f_*` уже покрывает
    (`admin_caps.py:343`), префиксный матч в `required_capability`."""
    from handlers import admin_caps
    assert admin_caps.required_capability(callback_data="filter_f_season") == "broadcast"
