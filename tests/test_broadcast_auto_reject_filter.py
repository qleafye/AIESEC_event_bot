"""Phase 31 (31-07, D-28): «Автоотказ по правилу» как поле фильтра рассылки — «экранная»
половина фичи. SQL-половина (виртуальное поле, сентинелы, `_build_filter_clause`,
`get_auto_reject_filter_options`) уже построена и покрыта в `tests/test_reject_rules_db.py`
(план 31-02) — этот файл её не дублирует, только доказывает сквозной путь через хендлеры.

Образец — `tests/test_broadcast_resume_filter_260911.py` (тот же приём двойной регистрации,
тот же харнес FakeCallback/FakeMessage/FakeState).

Правило двойной регистрации поля (прецедент Фазы 5, D-19): поле фильтра обязано быть в ОБОИХ
местах — `db._FILTER_COLUMNS` и `handlers.admin_broadcasts._PICKER_FIELDS`. Если поле есть
только в одном из двух, оно либо не доходит до SQL (виден на экране, молча не фильтрует),
либо не появляется на экране вовсе.

pytest-asyncio в этом окружении не установлен — каждый async-хелпер гоняется через
`asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path`.
"""
import asyncio

from config import config
from database import db
from database.db import _build_filter_clause


# ── двойная регистрация + SQL-сторож (границы с 31-02, не дублируется, только проверка связки) ──

def test_auto_reject_double_registration():
    from handlers import admin_broadcasts
    assert "auto_reject" in admin_broadcasts._PICKER_FIELDS
    assert "auto_reject" in db._FILTER_COLUMNS


def test_auto_reject_is_virtual_field():
    assert "auto_reject" in db._FILTER_VIRTUAL_FIELDS


def test_filter_field_label_auto_reject():
    from handlers import admin_broadcasts
    assert admin_broadcasts._FILTER_FIELD_LABELS["auto_reject"] == "Автоотказ по правилу"


# ── сквозной сценарий на засеянной базе ─────────────────────────────────────────────────────

def _seed_auto_reject_users(tmp_path, dbname, rows):
    """rows: список (telegram_id, auto_reject_rule_ids|None). `auto_reject_rule_ids` — НЕ
    колонка анкеты, `add_user`'s фиксированный список колонок её не знает (та же дисциплина,
    что у `status`: колонка принадлежит движку правил, не регистрации, и выставляется прямым
    UPDATE — приём из `tests/test_reject_rules_db.py::_set_user_field`)."""
    config.DB_PATH = str(tmp_path / dbname)

    async def go():
        await db.init_db()
        for tid, rule_ids in rows:
            await db.add_user({
                "telegram_id": tid,
                "full_name": f"User {tid}",
                "registration_date": f"2026-01-01 09:{tid:02d}:00",
            })
            if rule_ids is not None:
                async with db._connect() as conn:
                    await conn.execute(
                        "UPDATE users SET auto_reject_rule_ids = ? WHERE telegram_id = ?",
                        (rule_ids, tid),
                    )
                    await conn.commit()

    asyncio.run(go())


def test_count_and_list_filtered_auto_reject_yes_is_exactly_the_rejected(tmp_path):
    _seed_auto_reject_users(tmp_path, "auto_reject_yes.db", [
        (1, "[1]"), (2, None), (3, "[]"),
    ])
    ids = asyncio.run(db.count_and_list_filtered([{"field": "auto_reject", "value": db.AUTO_REJECT_YES}]))
    assert set(ids) == {1}


def test_count_and_list_filtered_auto_reject_no_is_the_rest(tmp_path):
    _seed_auto_reject_users(tmp_path, "auto_reject_no.db", [
        (1, "[1]"), (2, None), (3, "[]"),
    ])
    ids = asyncio.run(db.count_and_list_filtered([{"field": "auto_reject", "value": db.AUTO_REJECT_NO}]))
    assert set(ids) == {2, 3}


def test_count_and_list_filtered_auto_reject_garbage_value_is_empty(tmp_path):
    _seed_auto_reject_users(tmp_path, "auto_reject_garbage.db", [(1, "[1]"), (2, None)])
    ids = asyncio.run(db.count_and_list_filtered([{"field": "auto_reject", "value": "garbage"}]))
    assert ids == []


def test_auto_reject_yes_no_partition_the_base(tmp_path):
    _seed_auto_reject_users(tmp_path, "auto_reject_partition.db", [
        (1, "[1]"), (2, None), (3, "[]"), (4, "[2, 3]"),
    ])
    yes_ids = set(asyncio.run(db.count_and_list_filtered([{"field": "auto_reject", "value": db.AUTO_REJECT_YES}])))
    no_ids = set(asyncio.run(db.count_and_list_filtered([{"field": "auto_reject", "value": db.AUTO_REJECT_NO}])))
    assert yes_ids | no_ids == {1, 2, 3, 4}
    assert yes_ids & no_ids == set()


# ── харнес «экрана» — скопирован из tests/test_broadcast_resume_filter_260911.py ────────────

ADMIN_ID = 940103


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


def test_filter_menu_kb_default_has_no_auto_reject_button():
    from handlers import admin_broadcasts
    kb = admin_broadcasts._filter_menu_kb([])
    assert "filter_f_auto_reject" not in _cb_datas(kb)


def test_filter_menu_kb_show_auto_reject_adds_exactly_one_button():
    from handlers import admin_broadcasts
    kb_before = admin_broadcasts._filter_menu_kb([])
    kb = admin_broadcasts._filter_menu_kb([], show_auto_reject=True)
    assert len(kb.inline_keyboard) == len(kb_before.inline_keyboard) + 1
    assert _cb_datas(kb).count("filter_f_auto_reject") == 1


def test_render_filter_menu_mixed_auto_reject_shows_button(tmp_path):
    _seed_auto_reject_users(tmp_path, "render_mixed_auto_reject.db", [(1, "[1]"), (2, None)])
    config.ADMIN_IDS = [ADMIN_ID]
    from handlers import admin_broadcasts
    msg = FakeMessage()
    asyncio.run(admin_broadcasts._render_filter_menu(msg, [], edit=True))
    assert "filter_f_auto_reject" in _cb_datas(msg.markup)


def test_render_filter_menu_no_auto_rejects_hides_button(tmp_path):
    """Автоотказов в базе вовсе нет — фильтровать не по чему, кнопка не рисуется (тот же
    довод, что у «Резюме»/«Сезона» на пустой/однородной базе)."""
    config.DB_PATH = str(tmp_path / "render_no_auto_reject.db")
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.init_db())
    from handlers import admin_broadcasts
    msg = FakeMessage()
    asyncio.run(admin_broadcasts._render_filter_menu(msg, [], edit=True))
    assert "filter_f_auto_reject" not in _cb_datas(msg.markup)


def test_filter_pick_field_auto_reject_no_data_alerts_and_does_not_open_picker(tmp_path):
    """Гейт живёт в хэндлере: инлайн-кнопки не истекают, вчерашнее меню живо сегодня — тот
    же довод WR-04, что у event_city/season/resume. Менеджер получает человеческий отказ,
    а не пустой экран."""
    config.DB_PATH = str(tmp_path / "pick_no_auto_reject.db")
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.init_db())
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_auto_reject")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    assert cb.message.text is None
    assert cb.message.markup is None
    assert cb.answers
    text, show_alert = cb.answers[-1]
    assert show_alert is True
    assert "не по чему" in text


def test_filter_pick_field_auto_reject_mixed_base_shows_two_human_buttons(tmp_path):
    _seed_auto_reject_users(tmp_path, "pick_mixed_auto_reject.db", [(1, "[1]"), (2, None)])
    config.ADMIN_IDS = [ADMIN_ID]
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_auto_reject")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    texts = _btn_texts(cb.message.markup)
    value_texts = [t for t in texts if t != "← Назад"]
    # Человеческие подписи, БЕЗ сентинелов yes/no в тексте кнопки (правило «бот для людей»).
    assert set(value_texts) == {"Отклонён правилом", "Не отклонён правилом"}
    assert db.AUTO_REJECT_YES not in texts
    assert db.AUTO_REJECT_NO not in texts


def test_filter_pick_value_auto_reject_yes_carries_human_label(tmp_path):
    _seed_auto_reject_users(tmp_path, "pick_value_auto_reject_yes.db", [(1, "[1]"), (2, None)])
    config.ADMIN_IDS = [ADMIN_ID]
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_auto_reject")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    asyncio.run(state.update_data(filters=[]))
    options = (asyncio.run(state.get_data()))["filter_options"]
    idx = options.index(db.AUTO_REJECT_YES)
    pick = FakeCallback(f"filter_opt:{idx}")
    asyncio.run(admin_broadcasts.filter_pick_value(pick, state))
    filters = (asyncio.run(state.get_data()))["filters"]
    assert filters == [{"field": "auto_reject", "value": db.AUTO_REJECT_YES, "label": "Отклонён правилом"}]


def test_filter_summary_auto_reject_human_readable():
    from handlers import admin_broadcasts
    summary = admin_broadcasts._filter_summary(
        [{"field": "auto_reject", "value": db.AUTO_REJECT_YES, "label": "Отклонён правилом"}]
    )
    assert summary == "Автоотказ по правилу = Отклонён правилом"


def test_count_and_list_filtered_via_picked_auto_reject_spec(tmp_path):
    """Счётчик меняется: спека, собранная пикером, отдаёт ровно автоотклонённых."""
    _seed_auto_reject_users(tmp_path, "counter_auto_reject.db", [(1, "[1]"), (2, None), (3, None)])
    config.ADMIN_IDS = [ADMIN_ID]
    from handlers import admin_broadcasts
    cb = FakeCallback("filter_f_auto_reject")
    state = FakeState()
    asyncio.run(admin_broadcasts.filter_pick_field(cb, state))
    asyncio.run(state.update_data(filters=[]))
    options = (asyncio.run(state.get_data()))["filter_options"]
    idx = options.index(db.AUTO_REJECT_YES)
    pick = FakeCallback(f"filter_opt:{idx}")
    asyncio.run(admin_broadcasts.filter_pick_value(pick, state))
    filters = (asyncio.run(state.get_data()))["filters"]
    ids = asyncio.run(db.count_and_list_filtered(filters))
    assert set(ids) == {1}


def test_required_capability_filter_f_auto_reject_is_broadcast():
    """`filter_f_*` уже покрывает по префиксному матчу (тот же довод, что у `resume`) — новых
    записей в ADMIN_CAPS заводить не требуется."""
    from handlers import admin_caps
    assert admin_caps.required_capability(callback_data="filter_f_auto_reject") == "broadcast"


def test_auto_reject_filter_survives_json_round_trip():
    """Отложенная рассылка хранит спеку фильтра как JSON — round-trip обязан давать то же
    самое условие (та же гарантия, что у season/resume)."""
    import json

    filters = [{"field": "auto_reject", "value": db.AUTO_REJECT_YES, "label": "Отклонён правилом"}]
    before = _build_filter_clause(filters)
    restored = json.loads(json.dumps(filters, ensure_ascii=False))
    assert _build_filter_clause(restored) == before
