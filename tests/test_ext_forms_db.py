"""Акцессоры внешних форм: идемпотентность, позиции колонок, секреты вне bot_settings, purge."""
import asyncio

from config import config
from database import db
from database import ext_forms_db as ef
from tests._dbtpl import fast_init_db

NOW = "2026-10-01 12:00:00"


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "ext_forms.db")
    fast_init_db()


def _form(**kw):
    return asyncio.run(ef.create_form(platform="yandex", external_id="f1", title="Форма", **kw))


def _answer(form_id, aid, tid=None):
    return asyncio.run(ef.insert_answer(
        form_id=form_id, answer_id=aid, answered_at=NOW, received_at=NOW,
        payload=[{"q": "q1", "label": "Имя", "value": "Аня"}], raw=None,
        matched_telegram_id=tid, match_how="username" if tid else None,
    ))


def test_insert_answer_idempotent(tmp_path):
    _ready(tmp_path)
    fid = _form()
    assert _answer(fid, "a1") is True
    assert _answer(fid, "a1") is False
    assert asyncio.run(ef.count_answers(fid)) == 1
    assert asyncio.run(ef.known_answer_ids(fid)) == {"a1"}


def test_enqueue_pending_idempotent(tmp_path):
    _ready(tmp_path)
    fid = _form()
    assert asyncio.run(ef.enqueue_pending(fid, "a1", "d1", NOW)) is True
    assert asyncio.run(ef.enqueue_pending(fid, "a1", "d2", NOW)) is False
    assert asyncio.run(ef.count_pending(fid)) == 1
    assert len(asyncio.run(ef.list_due_pending(NOW, 10))) == 1


def test_upsert_columns_positions(tmp_path):
    _ready(tmp_path)
    fid = _form()
    first = asyncio.run(ef.upsert_columns(fid, [("q1", "Имя"), ("q2", "Ник")]))
    assert [(c["qkey"], c["position"]) for c in first] == [("q1", 1), ("q2", 2)]
    second = asyncio.run(ef.upsert_columns(fid, [("q2", "Ник"), ("q3", "Телефон")]))
    assert [(c["qkey"], c["position"]) for c in second] == [("q3", 3)]
    cols = asyncio.run(ef.list_columns(fid))
    assert [(c["qkey"], c["position"]) for c in cols] == [("q1", 1), ("q2", 2), ("q3", 3)]


def test_set_answer_match_once_and_synced_to_update(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    row = asyncio.run(ef.list_unmatched_answers(10))[0]
    assert asyncio.run(ef.set_answer_match(row["id"], 111, "username")) is True
    assert asyncio.run(ef.set_answer_match(row["id"], 222, "phone")) is False
    got = asyncio.run(ef.answers_for_user(111))
    assert len(got) == 1 and got[0]["payload"][0]["value"] == "Аня"
    assert asyncio.run(ef.answers_for_user(222)) == []

    _answer(fid, "a2")
    row2 = asyncio.run(ef.list_unmatched_answers(10))[0]
    asyncio.run(ef.mark_sheet_state([row2["id"]], "synced"))
    asyncio.run(ef.set_answer_match(row2["id"], 333, "phone"))
    assert asyncio.run(ef.answers_for_user(333))[0]["sheet_state"] == "update"


def test_list_sheet_due_needs_mirror(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    assert asyncio.run(ef.list_sheet_due(NOW, 10)) == []
    asyncio.run(ef.set_form_mirror(fid, "Вкладка", None))
    due = asyncio.run(ef.list_sheet_due(NOW, 10))
    assert len(due) == 1 and due[0]["mirror_tab"] == "Вкладка"
    assert isinstance(due[0]["payload"], list)


def test_app_secret_not_in_bot_settings(tmp_path):
    _ready(tmp_path)
    asyncio.run(ef.set_app_secret("yandex_client_secret", "TOP-SECRET-VALUE", 1))
    assert asyncio.run(ef.get_app_secret("yandex_client_secret")) == "TOP-SECRET-VALUE"
    assert asyncio.run(ef.get_app_secret("nope")) is None

    async def _in_settings():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM bot_settings WHERE value LIKE ?", ("%TOP-SECRET-VALUE%",),
            ) as cur:
                return (await cur.fetchone())[0]
    assert asyncio.run(_in_settings()) == 0


def test_delete_form_answers_keeps_form_and_columns(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    _answer(fid, "a2")
    asyncio.run(ef.enqueue_pending(fid, "a3", None, NOW))
    asyncio.run(ef.upsert_columns(fid, [("q1", "Имя")]))
    assert asyncio.run(ef.delete_form_answers(fid)) == 2
    assert asyncio.run(ef.count_pending(fid)) == 0
    assert asyncio.run(ef.get_form(fid)) is not None
    assert len(asyncio.run(ef.list_columns(fid))) == 1


def test_list_forms_aggregates(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1", tid=5)
    _answer(fid, "a2")
    f = asyncio.run(ef.list_forms())[0]
    assert (f["total"], f["unmatched"]) == (2, 1)
    assert f["last_answer_at"] == NOW


def test_purge_user_removes_only_his_answers(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1", tid=777)
    _answer(fid, "a2", tid=888)
    asyncio.run(db.purge_user(777))
    assert asyncio.run(ef.answers_for_user(777)) == []
    assert len(asyncio.run(ef.answers_for_user(888))) == 1


def test_upsert_yandex_connection_keeps_id(tmp_path):
    _ready(tmp_path)
    kw = dict(org_id="1", org_header="X-Org-Id", access_token="a", refresh_token="r",
              expires_at=NOW, by=1)
    first = asyncio.run(ef.upsert_yandex_connection(**kw))
    asyncio.run(ef.set_connection_status(first, "needs_reauth"))
    assert len(asyncio.run(ef.list_connections_to_alert())) == 1
    second = asyncio.run(ef.upsert_yandex_connection(**{**kw, "access_token": "b"}))
    assert first == second
    conn = asyncio.run(ef.get_connection(first))
    assert conn["status"] == "ok" and conn["access_token"] == "b"


def test_deleted_answers_not_resurrected(tmp_path):
    """Удалённые анкеты (по форме и по делегату) не возвращаются сверкой, вебхуком и приёмом."""
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    _answer(fid, "a2", tid=777)
    _answer(fid, "a3", tid=888)
    asyncio.run(ef.enqueue_pending(fid, "p1", None, NOW))
    asyncio.run(db.purge_user(777))
    assert asyncio.run(ef.known_answer_ids(fid)) >= {"a2"}
    assert asyncio.run(ef.enqueue_pending(fid, "a2", None, NOW)) is False
    assert _answer(fid, "a2", tid=777) is False
    asyncio.run(ef.delete_form_answers(fid))
    assert asyncio.run(ef.known_answer_ids(fid)) == {"a1", "a2", "a3", "p1"}
    assert _answer(fid, "a1") is False
    assert asyncio.run(ef.enqueue_pending(fid, "p1", None, NOW)) is False
    assert asyncio.run(ef.count_answers(fid)) == 0
    assert asyncio.run(ef.enqueue_pending(fid, "new", None, NOW)) is True
