"""Блок «Делегации» на странице статистики дашборда: вузы, ЦА в форме / в боте / пришли."""
import asyncio

from database import db as bot_db
from dashboard import db as dash_db
from dashboard.queries import Scope, dashboard_flags, delegations_block
from domain.settings.schema import SETTINGS_SCHEMA

from tests.test_dashboard_queries import _seed, _use_tmp_db
from tests import test_dashboard_render as render


async def _seed_delegations(rows, checkins=()):
    async with bot_db._connect() as conn:
        for i, row in enumerate(rows):
            data = {
                "form_id": 1, "answer_id": f"a{i}", "ta_status": "ok", "university": "МГУ",
                "linked_telegram_id": None, "created_at": "2026-10-01 10:00:00",
            }
            data.update(row)
            cols = ", ".join(data)
            await conn.execute(
                f"INSERT INTO delegation_answers ({cols}) VALUES ({', '.join('?' for _ in data)})",
                tuple(data.values()),
            )
        for tid in checkins:
            await conn.execute(
                "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
                "VALUES (?, 'entry', '2026-10-03 10:00:00', 'qr', '2026-10-03 10:00:00', '2026-10-03')",
                (tid,),
            )
        await conn.commit()


def test_block_none_when_table_empty(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        assert delegations_block(conn, Scope()) is None


def test_block_counts_by_university(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "status": "approved"},
        {"telegram_id": 2, "status": "approved"},
    ])
    asyncio.run(_seed_delegations(
        [
            {"university": "МГУ", "linked_telegram_id": 1},
            {"university": "МГУ", "linked_telegram_id": 2},
            {"university": "МГУ", "ta_status": "not_ta"},
            {"university": "ВШЭ", "linked_telegram_id": None},
            {"university": "  ", "ta_status": "ok"},
        ],
        checkins=[1],
    ))
    with dash_db.read_conn(path) as conn:
        block = delegations_block(conn, Scope())
    by_uni = {r["university"]: r for r in block["rows"]}
    assert by_uni["МГУ"] == {"university": "МГУ", "ta": 2, "in_bot": 2, "arrived": 1}
    assert by_uni["ВШЭ"]["ta"] == 1 and by_uni["ВШЭ"]["in_bot"] == 0
    assert by_uni["Без вуза"]["ta"] == 1
    assert block["rows"][0]["university"] == "МГУ"  # по убыванию ЦА
    assert block["total_ta"] == 4
    assert block["total_in_bot"] == 2
    assert block["total_arrived"] == 1


def test_scope_narrows_in_bot_but_not_form_ta(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "status": "approved", "event_city": "msk"},
            {"telegram_id": 2, "status": "approved", "event_city": "spb"},
        ],
    )
    asyncio.run(_seed_delegations([
        {"linked_telegram_id": 1}, {"linked_telegram_id": 2},
    ]))
    with dash_db.read_conn(path) as conn:
        block = delegations_block(conn, Scope(city="msk"))
    assert block["total_ta"] == 2
    assert block["total_in_bot"] == 1


def test_flag_default_off_and_registry_entry(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        assert dashboard_flags(conn)["dashboard_block_delegations"] == "off"
    entry = SETTINGS_SCHEMA["dashboard_block_delegations"]
    assert entry["default"] == "off" and entry["label"] == "🏫 Делегации"


def test_page_shows_block_only_with_toggle_and_escapes(tmp_path):
    db_path = render._use_tmp_db(tmp_path)
    render._seed(users=[{"telegram_id": 1, "status": "approved"}])
    asyncio.run(_seed_delegations([{"university": "<b>Вуз</b>", "linked_telegram_id": 1}]))

    client = render._stats_manager_client(db_path)
    off = client.get("/")
    assert off.status_code == 200
    assert 'id="delegations"' not in off.text

    render._seed(settings={"dashboard_block_delegations": "on"})
    on = client.get("/")
    assert 'id="delegations"' in on.text
    assert "ЦА в форме" in on.text
    assert "&lt;b&gt;Вуз&lt;/b&gt;" in on.text  # автоэкранирование Jinja
    assert "<b>Вуз</b>" not in on.text
