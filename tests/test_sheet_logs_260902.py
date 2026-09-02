"""Quick 260902-vth: два новых листа в ТОЙ ЖЕ Google-таблице заявок — «История правок»
(reg_answer_history, Phase 21) и «Вопросы» (delegate_questions, Phase 8), пересобираются
целиком (не append по событию), автосинхрон после каждой правки/вопроса, экран
«🕓 Журналы в таблицу» в разделе «📊 Данные».

pytest-asyncio в проекте нет — каждый async-вызов через asyncio.run(); БД — tmp_path, форма
шапки как у tests/test_polls_260822.py.

RED (Task 1): `services/sheet_logs.py` ещё не существует — набор обязан упасть на
ImportError/AttributeError. Task 2/3 дописаны в этот же файл ниже отдельными блоками.
"""
import asyncio

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA
from settings_synonyms import SETTINGS_SYNONYMS
from services import sheet_logs


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="sheet_logs.db"):
    config.DB_PATH = str(tmp_path / name)
    config.GOOGLE_SHEET_ID = ""  # тестовое окружение не должно ходить в сеть
    _run(db.init_db())
    sheet_logs._sync_inflight = False
    sheet_logs._sync_task = None


async def _add_user(tid, name="Иван", username="@ivan", city=None):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, event_city) VALUES (?, ?, ?, ?)",
            (tid, name, username, city),
        )
        await conn.commit()


# ── реестр: три новых ключа ──────────────────────────────────────────────────────────────────

def test_registry_keys_present():
    assert SETTINGS_SCHEMA["history_sheet_tab"]["default"] == "История правок"
    assert SETTINGS_SCHEMA["history_sheet_tab"]["group"] == "sheets"
    assert SETTINGS_SCHEMA["questions_sheet_tab"]["default"] == "Вопросы"
    assert SETTINGS_SCHEMA["questions_sheet_tab"]["group"] == "sheets"
    entry = SETTINGS_SCHEMA["sheet_logs_autosync"]
    assert entry["default"] == "on"
    assert entry["type"] == "enum"  # НЕ "toggle" — тот тип зарезервирован за reg_q_*
    assert entry["options"] == ["on", "off"]


def test_synonyms_cover_new_keys():
    for key in ("history_sheet_tab", "questions_sheet_tab", "sheet_logs_autosync"):
        assert key in SETTINGS_SYNONYMS
        assert len(SETTINGS_SYNONYMS[key]) >= 2


# ── Task 1: build_history_sheet_rows ────────────────────────────────────────────────────────

def test_history_rows_order_and_field_count(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1, "Аня", "@anya")
        await db.record_answer_history(
            1, [{"column": "vk_username", "old": "old_vk", "new": "new_vk"},
                {"column": "is_ambassador_candidate", "old": None, "new": "yes"}],
            source="bot", season="2026",
        )
        await db.record_answer_history(
            1, [{"column": "phone_extra", "old": "1", "new": "2"}],
            source="miniapp", season="2026",
        )
        rows = await sheet_logs.build_history_sheet_rows()
        assert len(rows) == 3
        # хронологический порядок по id ASC, внутри записи — порядок полей как в changes
        assert rows[0][5] == reg_engine_label("vk")
        assert rows[1][5] == reg_engine_label("ambassador")
        assert rows[2][5] == "phone_extra"  # вне STEP_TO_COLUMN — своим именем, без падения

    _run(go())


def reg_engine_label(step):
    import reg_engine
    return reg_engine.label_for(step)


def test_history_rows_source_label_and_empty_old_new(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1)
        await db.record_answer_history(1, [{"column": "phone", "old": None, "new": "телефон"}], source="bot")
        await db.record_answer_history(1, [{"column": "phone", "old": "телефон", "new": None}], source="miniapp")
        await db.record_answer_history(1, [{"column": "phone", "old": "a", "new": "b"}], source="weird")
        rows = await sheet_logs.build_history_sheet_rows()
        assert rows[0][4] == "бот" and rows[0][6] == "" and rows[0][7] == "телефон"
        assert rows[1][4] == "приложение" and rows[1][6] == "телефон" and rows[1][7] == ""
        assert rows[2][4] == "weird"  # неизвестный источник — как есть

    _run(go())


def test_history_rows_missing_user_does_not_crash(tmp_path):
    _ready(tmp_path)

    async def go():
        # делегата 999 в users нет вовсе
        await db.record_answer_history(999, [{"column": "phone", "old": "1", "new": "2"}], source="bot")
        rows = await sheet_logs.build_history_sheet_rows()
        assert len(rows) == 1
        assert rows[0][1] == 999
        assert rows[0][2] == "" and rows[0][3] == ""  # ФИО/username пустые, не падение

    _run(go())


def test_history_rows_csv_safe(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1, name="=Аня", username="@anya")
        await db.record_answer_history(1, [{"column": "phone", "old": "1", "new": "2"}], source="bot")
        rows = await sheet_logs.build_history_sheet_rows()
        assert rows[0][2] == "'=Аня"

    _run(go())


# ── Task 1: build_questions_sheet_rows ──────────────────────────────────────────────────────

def test_questions_rows_order_and_recipient_cities_off(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "off")
        await _add_user(1, city="spb")
        await db.create_question(1, "Когда дедлайн?")
        await db.create_question(1, "А что с оплатой?")
        rows = await sheet_logs.build_questions_sheet_rows()
        assert len(rows) == 2
        assert rows[0][5] == "Когда дедлайн?"
        assert rows[1][5] == "А что с оплатой?"
        assert rows[0][6] == "Все менеджеры"  # модуль городов выключен

    _run(go())


def test_questions_rows_recipient_city_on(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await _add_user(1, city="spb")
        await db.create_question(1, "Вопрос")
        rows = await sheet_logs.build_questions_sheet_rows()
        assert rows[0][6].startswith("Менеджеры города")

    _run(go())


# ── Task 1: export_*_to_sheet / fail-soft ───────────────────────────────────────────────────

def test_export_uses_tab_setting_and_fails_soft(tmp_path, monkeypatch):
    _ready(tmp_path)
    import services.sheets as sheets
    calls = []

    async def fake_sync(title, headers, rows):
        calls.append((title, headers, rows))
        return len(rows)
    monkeypatch.setattr(sheets, "sync_named_worksheet", fake_sync)

    async def go():
        await _add_user(1)
        await db.record_answer_history(1, [{"column": "phone", "old": "1", "new": "2"}], source="bot")
        n = await sheet_logs.export_history_to_sheet()
        assert n == 1
        assert calls[0][0] == "История правок" and calls[0][1] == sheet_logs.HISTORY_SHEET_HEADERS

        await db.set_setting("history_sheet_tab", "Правки")
        await sheet_logs.export_history_to_sheet()
        assert calls[-1][0] == "Правки"

        m = await sheet_logs.export_questions_to_sheet()
        assert m == 0
        assert calls[-1][0] == "Вопросы" and calls[-1][1] == sheet_logs.QUESTIONS_SHEET_HEADERS

    _run(go())

    async def boom(title, headers, rows):
        raise RuntimeError("simulated gspread failure")
    monkeypatch.setattr(sheets, "sync_named_worksheet", boom)
    assert _run(sheet_logs.export_history_to_sheet()) == -1
    assert _run(sheet_logs.export_questions_to_sheet()) == -1


# ── Task 2: автосинхрон fire-and-forget ─────────────────────────────────────────────────────

def test_record_answer_history_no_sync_when_sheet_not_configured(tmp_path, monkeypatch):
    _ready(tmp_path)  # config.GOOGLE_SHEET_ID == "" из _ready
    calls = []

    async def fake_sync():
        calls.append(1)
        return (0, 0)
    monkeypatch.setattr(sheet_logs, "sync_sheet_logs", fake_sync)

    async def go():
        await _add_user(1)
        await db.record_answer_history(1, [{"column": "phone", "old": "1", "new": "2"}], source="bot")
        await asyncio.sleep(0)
        if sheet_logs._sync_task:
            await sheet_logs._sync_task

    _run(go())
    assert calls == []
    assert sheet_logs._sync_inflight is False


def test_record_answer_history_triggers_sync_when_configured(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    calls = []

    async def fake_sync():
        calls.append(1)
        return (0, 0)
    monkeypatch.setattr(sheet_logs, "sync_sheet_logs", fake_sync)

    async def go():
        await db.set_setting("sheet_logs_autosync", "on")
        await _add_user(1)
        await db.record_answer_history(1, [{"column": "phone", "old": "1", "new": "2"}], source="bot")
        await asyncio.sleep(0)
        if sheet_logs._sync_task:
            await sheet_logs._sync_task

    _run(go())
    assert calls == [1]


def test_record_answer_history_no_sync_when_autosync_off(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    calls = []

    async def fake_sync():
        calls.append(1)
        return (0, 0)
    monkeypatch.setattr(sheet_logs, "sync_sheet_logs", fake_sync)

    async def go():
        await db.set_setting("sheet_logs_autosync", "off")
        await _add_user(1)
        await db.record_answer_history(1, [{"column": "phone", "old": "1", "new": "2"}], source="bot")
        await asyncio.sleep(0)
        if sheet_logs._sync_task:
            await sheet_logs._sync_task

    _run(go())
    assert calls == []


def test_create_question_triggers_sync(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    calls = []

    async def fake_sync():
        calls.append(1)
        return (0, 0)
    monkeypatch.setattr(sheet_logs, "sync_sheet_logs", fake_sync)

    async def go():
        await db.set_setting("sheet_logs_autosync", "on")
        await _add_user(1)
        await db.create_question(1, "Вопрос")
        await asyncio.sleep(0)
        if sheet_logs._sync_task:
            await sheet_logs._sync_task

    _run(go())
    assert calls == [1]


def test_autosync_exception_does_not_propagate(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")

    async def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(sheet_logs, "sync_sheet_logs", boom)

    async def go():
        await db.set_setting("sheet_logs_autosync", "on")
        await _add_user(1)
        # запись должна пройти нормально, исключение не должно всплыть наружу
        await db.record_answer_history(1, [{"column": "phone", "old": "1", "new": "2"}], source="bot")
        history = await db.get_answer_history(1)
        assert len(history) == 1
        await asyncio.sleep(0)
        if sheet_logs._sync_task:
            await sheet_logs._sync_task

    _run(go())
    assert sheet_logs._sync_inflight is False


def test_two_calls_while_inflight_coalesce_to_one_extra_run(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    calls = []

    async def fake_sync():
        calls.append(1)
        return (0, 0)
    monkeypatch.setattr(sheet_logs, "sync_sheet_logs", fake_sync)

    async def go():
        await db.set_setting("sheet_logs_autosync", "on")
        await _add_user(1)
        # два вызова подряд, ни один ещё не отработал (задача летит асинхронно) — склейка
        sheet_logs.schedule_sheet_logs_sync()
        sheet_logs.schedule_sheet_logs_sync()
        await asyncio.sleep(0)
        if sheet_logs._sync_task:
            await sheet_logs._sync_task

    _run(go())
    assert calls == [1]
