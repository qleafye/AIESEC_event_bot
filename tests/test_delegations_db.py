"""Делегации вузов — схема и акцессоры `database/delegations_db.py`.

Что покрыто: миграция (таблица `delegation_answers`, поля `users.delegation*`,
`external_forms.mirror_mode`/`mirror_warning`), сохранность старых строк `users` при повторном
`init_db`, идемпотентный upsert оценки ЦА с защитой ручного решения менеджера, одноразовая
привязка к Telegram, поиск по нику без учёта регистра, сводка по вузам со столбцом «пришёл»
из `checkins`, join ответа формы в списке, дополнения `ext_forms_db` (get_answer, mirror_mode,
mirror_warning, requeue) и запись в `USER_PURGE_TABLES`.

Все имена, вузы и ники — вымышленные.
"""
import asyncio

from config import config
from database import db
from database import delegations_db as ddb
from database import ext_forms_db as ef
from tests._dbtpl import fast_init_db

NOW = "2026-10-01 12:00:00"


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "delegations.db")
    fast_init_db()


def _form(tab="UR REGS"):
    return asyncio.run(ef.create_form(
        platform="yandex", external_id="deleg-f", title="Форма делегаций", mirror_tab=tab,
    ))


def _items(university, course, nick):
    return [
        {"q": "q_name", "label": "ФИО", "value": "Тестовый Делегат"},
        {"q": "q_univ", "label": "Вуз", "value": university},
        {"q": "q_course", "label": "Курс", "value": course},
        {"q": "q_tg", "label": "Ник TG", "value": nick},
    ]


def _answer(fid, aid, items=None, tid=None):
    items = items if items is not None else _items("Вымышленный университет", "3", "@tester")
    asyncio.run(ef.upsert_columns(fid, [(i["q"], i["label"]) for i in items]))
    return asyncio.run(ef.insert_answer(
        form_id=fid, answer_id=aid, answered_at=NOW, received_at=NOW, payload=items, raw=None,
        matched_telegram_id=tid, match_how="username" if tid else None,
    ))


def _eval(fid, aid, *, ta="ok", university="Вымышленный университет", course_raw="3",
          canonical="3", needle="tester"):
    return asyncio.run(ddb.upsert_eval(
        fid, aid, ta_status=ta, university=university, course_raw=course_raw,
        course_canonical=canonical, username_needle=needle, answered_at=NOW,
    ))


def _user(tid, status="approved", username="ivan"):
    async def go():
        async with db._connect() as c:
            await c.execute(
                "INSERT INTO users (telegram_id, username, full_name, status) VALUES (?,?,?,?)",
                (tid, username, "Иван Тестов", status))
            await c.commit()
    asyncio.run(go())


def _checkin(tid, point="entry"):
    async def go():
        async with db._connect() as c:
            await c.execute(
                "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
                "VALUES (?,?,?,?,?,?)",
                (tid, point, NOW, "qr", NOW, "2026-10-01"))
            await c.commit()
    asyncio.run(go())


def _table_info(table):
    async def go():
        async with db._connect() as c:
            async with c.execute(f"PRAGMA table_info({table})") as cur:
                return {r[1]: r for r in await cur.fetchall()}
    return asyncio.run(go())


# ── Схема ───────────────────────────────────────────────────────────────────────────────────

def test_schema_columns(tmp_path):
    _ready(tmp_path)
    users = _table_info("users")
    assert "delegation" in users and "delegation_answer_id" in users
    forms = _table_info("external_forms")
    assert "mirror_mode" in forms
    assert forms["mirror_mode"][4].strip("'") == "bot"  # dflt_value
    assert "mirror_warning" in forms
    da = _table_info("delegation_answers")
    for col in ("form_id", "answer_id", "ta_status", "university", "course_raw", "course_canonical",
                "username_needle", "answered_at", "linked_telegram_id", "link_how", "decided_by",
                "decided_at", "note", "created_at"):
        assert col in da, col

    async def unique_holds():
        async with db._connect() as c:
            await c.execute(
                "INSERT INTO delegation_answers (form_id, answer_id, ta_status) VALUES (1, 'a', 'ok')")
            try:
                await c.execute(
                    "INSERT INTO delegation_answers (form_id, answer_id, ta_status) VALUES (1, 'a', 'no')")
            except Exception as e:  # sqlite3.IntegrityError
                return "UNIQUE" in str(e).upper()
            return False
    assert asyncio.run(unique_holds())


def test_existing_rows_survive(tmp_path):
    _ready(tmp_path)
    _user(777, "pending", "oldie")
    asyncio.run(db.init_db())  # настоящий повторный прогон миграций
    row = asyncio.run(db.get_user(777))
    assert row is not None
    assert row["status"] == "pending"
    assert row["delegation"] is None and row["delegation_answer_id"] is None


def test_purge_tables_entry():
    assert ("delegation_answers", "linked_telegram_id", "forms") in db.USER_PURGE_TABLES


def test_entry_point_literal_matches_checkin():
    from services.checkin import ENTRY_POINT
    assert ddb.ENTRY_POINT == ENTRY_POINT


# ── upsert / решение / привязка ─────────────────────────────────────────────────────────────

def test_upsert_eval_idempotent_and_manual_wins(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    rid1 = _eval(fid, "a1", ta="check", course_raw="1 бакалавриат")
    rid2 = _eval(fid, "a1", ta="check", course_raw="1 бакалавриат")
    assert rid1 == rid2
    assert asyncio.run(ddb.count_by_status(fid, "check", linked=None)) == 1

    asyncio.run(ddb.set_decision(rid1, "no", decided_by=42))
    row = asyncio.run(ddb.get_by_id(rid1))
    assert row["ta_status"] == "no" and row["decided_by"] == 42 and row["decided_at"]

    _eval(fid, "a1", ta="ok", course_raw="3 курс")
    row = asyncio.run(ddb.get_by_answer(fid, "a1"))
    assert row["ta_status"] == "no"          # ручное решение не перетирается
    assert row["decided_by"] == 42
    assert row["course_raw"] == "3 курс"     # но данные ответа обновляются

    # до решения менеджера переоценка меняет статус
    _answer(fid, "a2")
    rid = _eval(fid, "a2", ta="check")
    _eval(fid, "a2", ta="ok")
    assert asyncio.run(ddb.get_by_id(rid))["ta_status"] == "ok"


def test_set_decision_rejects_unknown_status(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    rid = _eval(fid, "a1")
    try:
        asyncio.run(ddb.set_decision(rid, "maybe", decided_by=1))
    except ValueError:
        pass
    else:
        raise AssertionError("ожидался ValueError на неизвестный ta_status")


def test_link_once(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    rid = _eval(fid, "a1")
    assert asyncio.run(ddb.link(rid, 1001, "username")) is True
    assert asyncio.run(ddb.link(rid, 2002, "manual")) is False
    row = asyncio.run(ddb.get_by_id(rid))
    assert row["linked_telegram_id"] == 1001 and row["link_how"] == "username"
    # повтор с тем же tid — идемпотентно, не ошибка
    assert asyncio.run(ddb.link(rid, 1001, "manual")) is True
    assert asyncio.run(ddb.get_by_telegram_id(1001))["id"] == rid
    assert asyncio.run(ddb.get_by_telegram_id(2002)) is None


def test_find_pending_by_username_case_insensitive(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    _answer(fid, "a2")
    _answer(fid, "a3")
    r1 = _eval(fid, "a1", needle="IvanPetrov")
    r2 = _eval(fid, "a2", needle="IvanPetrov")
    _eval(fid, "a3", ta="check", needle="IvanPetrov")
    asyncio.run(ddb.link(r2, 5, "username"))
    found = asyncio.run(ddb.find_pending_by_username("ivanpetrov"))
    assert [r["id"] for r in found] == [r1]
    assert asyncio.run(ddb.find_pending_by_username("@IVANPETROV"))[0]["id"] == r1
    assert asyncio.run(ddb.find_pending_by_username("")) == []
    assert asyncio.run(ddb.find_pending_by_username(None)) == []


# ── списки и сводки ─────────────────────────────────────────────────────────────────────────

def test_summary_by_university(tmp_path):
    _ready(tmp_path)
    fid = _form()
    for aid in ("a1", "a2", "a3"):
        _answer(fid, aid)
    r1 = _eval(fid, "a1", university="Университет Альфа")
    _eval(fid, "a2", university="Университет Альфа")
    _eval(fid, "a3", ta="no", university="Университет Бета")
    _user(1001)
    asyncio.run(ddb.link(r1, 1001, "username"))
    _checkin(1001)
    _checkin(1001, point="session-1")  # не «вход» — не считается
    rows = asyncio.run(ddb.summary_by_university(fid))
    assert [r["university"] for r in rows] == ["Университет Альфа", "Университет Бета"]
    alpha, beta = rows
    assert (alpha["total"], alpha["ta"], alpha["in_bot"], alpha["arrived"]) == (2, 2, 1, 1)
    assert (beta["total"], beta["ta"], beta["in_bot"], beta["arrived"]) == (1, 0, 0, 0)


def test_list_by_status_joins_payload(tmp_path):
    _ready(tmp_path)
    fid = _form()
    items = _items("Университет Гамма", "1 бакалавриат", "@gamma")
    _answer(fid, "a1", items)
    rid = _eval(fid, "a1", ta="check", university="Университет Гамма")
    rows = asyncio.run(ddb.list_by_status(fid, "check", linked=None, offset=0, limit=8))
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == rid
    assert isinstance(row["payload"], list) and row["payload"][1]["value"] == "Университет Гамма"
    assert isinstance(row["answer_row_id"], int)
    assert asyncio.run(ddb.list_by_status(fid, "ok", linked=None, offset=0, limit=8)) == []
    # фильтр по привязке
    assert asyncio.run(ddb.list_by_status(fid, "check", linked=True, offset=0, limit=8)) == []
    assert len(asyncio.run(ddb.list_by_status(fid, "check", linked=False, offset=0, limit=8))) == 1
    assert asyncio.run(ddb.count_by_status(fid, "check", linked=False)) == 1
    assert asyncio.run(ddb.count_by_status(fid, "check", linked=True)) == 0


def test_list_unevaluated(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    _answer(fid, "a2")
    _eval(fid, "a1")
    rows = asyncio.run(ddb.list_unevaluated(fid))
    assert [r["answer_id"] for r in rows] == ["a2"]
    assert isinstance(rows[0]["answer_row_id"], int)


def test_mirror_status_for(tmp_path):
    _ready(tmp_path)
    fid = _form()
    for aid in ("a1", "a2", "a3"):
        _answer(fid, aid)
    r1 = _eval(fid, "a1")
    _eval(fid, "a2", ta="no")
    _eval(fid, "a3", ta="check")
    _user(1001)
    asyncio.run(ddb.link(r1, 1001, "username"))
    _checkin(1001)
    res = asyncio.run(ddb.mirror_status_for(fid, ["a1", "a2", "missing"]))
    assert set(res) == {"a1", "a2"}
    assert res["a1"] == {"ta_status": "ok", "linked": True, "arrived": True, "decided_by": None}
    assert res["a2"] == {"ta_status": "no", "linked": False, "arrived": False, "decided_by": None}
    assert asyncio.run(ddb.mirror_status_for(fid, [])) == {}


# ── дополнения ext_forms_db ─────────────────────────────────────────────────────────────────

def test_ext_forms_additions(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    row = asyncio.run(ef.get_answer(fid, "a1"))
    assert row is not None and isinstance(row["payload"], list)
    assert row["payload"][0]["label"] == "ФИО"
    assert asyncio.run(ef.get_answer(fid, "nope")) is None

    due = asyncio.run(ef.list_sheet_due(NOW, 50))
    assert len(due) == 1 and due[0]["mirror_mode"] == "bot"

    asyncio.run(ef.set_form_mirror_mode(fid, "yandex_export"))
    assert asyncio.run(ef.get_form(fid))["mirror_mode"] == "yandex_export"
    assert asyncio.run(ef.list_sheet_due(NOW, 50))[0]["mirror_mode"] == "yandex_export"
    try:
        asyncio.run(ef.set_form_mirror_mode(fid, "weird"))
    except ValueError:
        pass
    else:
        raise AssertionError("ожидался ValueError на неизвестный режим зеркала")

    asyncio.run(ef.mark_sheet_synced([(row["id"], "append", None)]))
    assert asyncio.run(ef.list_sheet_due(NOW, 50)) == []
    assert asyncio.run(ef.requeue_form_answers(fid)) == 1
    again = asyncio.run(ef.get_answer(fid, "a1"))
    assert again["sheet_state"] == "append" and again["sheet_attempts"] == 0
    assert again["sheet_next_try_at"] is None


def test_mirror_warning_does_not_pause_drain(tmp_path):
    _ready(tmp_path)
    fid = _form()
    _answer(fid, "a1")
    asyncio.run(ef.set_form_mirror_warning(fid, "Вопрос «Ник ВК» некуда записать — колонка занята"))
    form = asyncio.run(ef.get_form(fid))
    assert form["mirror_warning"].startswith("Вопрос")
    assert form["mirror_error"] is None
    assert len(asyncio.run(ef.list_sheet_due(NOW, 50))) == 1  # очередь НЕ остановлена
    asyncio.run(ef.set_form_mirror_warning(fid, None))
    assert asyncio.run(ef.get_form(fid))["mirror_warning"] is None
    # для контраста: mirror_error останавливает очередь
    asyncio.run(ef.set_form_mirror(fid, "UR REGS", "вкладки нет"))
    assert asyncio.run(ef.list_sheet_due(NOW, 50)) == []
