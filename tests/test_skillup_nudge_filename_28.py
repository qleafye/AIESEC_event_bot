"""Phase 28 Plan 09 (SU-09/SU-10): персональный остаток вопросов в догонялке + тумблер
имени файла резюме в облаке.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run(...)`, временная БД —
`config.DB_PATH` на `tmp_path` (та же конвенция, что `tests/test_quiet_hours_260904.py` и
`tests/test_reg_finalize.py`).

Задача 1 (`services/scheduler.py::nudge_incomplete_registrations`/`_nudge_remaining_for`):
- без `{remaining}` в тексте — ни одного вызова `get_reg_draft` (байт-в-байт прежнее поведение);
- с `{remaining}` — свой остаток на каждого кандидата, `.replace`, не `.format`;
- черновик не прочитался -> человеческое слово вместо числа, никогда не голые фигурные скобки;
- `mark_nudged` (D-14, ровно один раз) и тихие часы (пропуск без пометки) не тронуты.

Задача 2 (`handlers/registration.py::_resume_file_stem` mode / `services/reg_finalize.py`):
- реестровый тумблер `resume_filename_short_mode` включает режим "id" (только ID + дата) в
  `post_finalize`; сама `_resume_file_stem` остаётся чистой sync-функцией (Pitfall 4).
"""
import asyncio

from config import config
from database import db


DELEGATE_A = 940901
DELEGATE_B = 940902
DELEGATE_MISSING = 940903
DELEGATE_ONESHOT = 940904
DELEGATE_QUIET = 940905
DELEGATE_PLAIN = 940906


def _ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


class _FakeMeBot:
    """Тот же приём, что `tests/test_quiet_hours_260904.py`: `get_me()` для `_nudge_keyboard`,
    `send_message` копит (chat_id, text) без реального похода в Telegram."""

    def __init__(self):
        self.sent = []

    async def get_me(self):
        class _Me:
            username = "test_bot"
        return _Me()

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _patch_common(monkeypatch, sched, db_mod, *, candidates, nudge_text, marked_out):
    async def fake_get_candidates(_cutoff):
        return list(candidates)

    async def fake_mark_nudged(uid):
        marked_out.append(uid)

    async def fake_get_setting(key):
        return {
            "nudge_enabled": "on",
            "nudge_after_minutes": "120",
            "nudge_text": nudge_text,
        }.get(key)

    monkeypatch.setattr(db_mod, "get_nudge_candidates", fake_get_candidates)
    monkeypatch.setattr(db_mod, "mark_nudged", fake_mark_nudged)
    monkeypatch.setattr(sched, "get_setting", fake_get_setting)


# ── Задача 1: без {remaining} — поведение байт-в-байт, ни одного get_reg_draft ────────────

def test_nudge_without_placeholder_unchanged(tmp_path, monkeypatch):
    _ready(tmp_path, "nudge_plain.db")
    from services import scheduler as sched
    from database import db as db_mod

    marked = []
    _patch_common(
        monkeypatch, sched, db_mod,
        candidates=[DELEGATE_PLAIN], nudge_text="Продолжите анкету, пожалуйста",
        marked_out=marked,
    )

    async def _boom(_tid):
        raise AssertionError("get_reg_draft must not be called without {remaining}")
    monkeypatch.setattr(db_mod, "get_reg_draft", _boom)

    bot = _FakeMeBot()
    sched._bot = bot
    asyncio.run(sched.nudge_incomplete_registrations())

    assert bot.sent == [(DELEGATE_PLAIN, "Продолжите анкету, пожалуйста")]
    assert marked == [DELEGATE_PLAIN]


# ── Задача 1: с {remaining} — свой остаток на КАЖДОГО кандидата ───────────────────────────

def test_nudge_substitutes_per_candidate(tmp_path, monkeypatch):
    _ready(tmp_path, "nudge_per_candidate.db")
    from services import scheduler as sched
    from database import db as db_mod
    import reg_engine

    marked = []
    _patch_common(
        monkeypatch, sched, db_mod,
        candidates=[DELEGATE_A, DELEGATE_B], nudge_text="Осталось {remaining} вопросов",
        marked_out=marked,
    )

    drafts = {
        DELEGATE_A: {"answers": {"age": "20"}, "event_city": None},   # 2 из 3 не отвечены
        DELEGATE_B: {"answers": {}, "event_city": None},              # 3 из 3 не отвечены
    }

    async def fake_get_reg_draft(tid):
        return drafts.get(tid)

    async def fake_enabled_steps(_answers, _city=None):
        return ["age", "phone", "email"]

    monkeypatch.setattr(db_mod, "get_reg_draft", fake_get_reg_draft)
    monkeypatch.setattr(reg_engine, "enabled_steps", fake_enabled_steps)

    bot = _FakeMeBot()
    sched._bot = bot
    asyncio.run(sched.nudge_incomplete_registrations())

    sent_by_id = dict(bot.sent)
    assert sent_by_id[DELEGATE_A] == "Осталось 2 вопросов"
    assert sent_by_id[DELEGATE_B] == "Осталось 3 вопросов"
    assert sent_by_id[DELEGATE_A] != sent_by_id[DELEGATE_B]
    assert sorted(marked) == [DELEGATE_A, DELEGATE_B]


# ── Задача 1: черновика нет — человеческое слово, НИКОГДА не голые скобки ─────────────────

def test_nudge_fallback_when_draft_missing(tmp_path, monkeypatch):
    _ready(tmp_path, "nudge_fallback.db")
    from services import scheduler as sched
    from database import db as db_mod

    marked = []
    _patch_common(
        monkeypatch, sched, db_mod,
        candidates=[DELEGATE_MISSING], nudge_text="Осталось {remaining} вопросов",
        marked_out=marked,
    )

    async def fake_get_reg_draft(_tid):
        return None
    monkeypatch.setattr(db_mod, "get_reg_draft", fake_get_reg_draft)

    bot = _FakeMeBot()
    sched._bot = bot
    asyncio.run(sched.nudge_incomplete_registrations())

    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "{" not in text and "}" not in text
    # дефолт реестрового ключа nudge_remaining_fallback_text — «несколько»
    assert text == "Осталось несколько вопросов"


# ── Задача 1: mark_nudged по-прежнему ровно один раз (D-14) ───────────────────────────────

def test_nudge_still_one_shot(tmp_path, monkeypatch):
    _ready(tmp_path, "nudge_oneshot.db")
    from services import scheduler as sched
    from database import db as db_mod
    import reg_engine

    marked = []
    _patch_common(
        monkeypatch, sched, db_mod,
        candidates=[DELEGATE_ONESHOT], nudge_text="Осталось {remaining} вопросов",
        marked_out=marked,
    )

    async def fake_get_reg_draft(_tid):
        return {"answers": {}, "event_city": None}

    async def fake_enabled_steps(_answers, _city=None):
        return ["age"]

    monkeypatch.setattr(db_mod, "get_reg_draft", fake_get_reg_draft)
    monkeypatch.setattr(reg_engine, "enabled_steps", fake_enabled_steps)

    bot = _FakeMeBot()
    sched._bot = bot
    asyncio.run(sched.nudge_incomplete_registrations())

    assert marked == [DELEGATE_ONESHOT]
    assert marked.count(DELEGATE_ONESHOT) == 1


# ── Задача 1: тихие часы — пропуск без mark_nudged, ДО расчёта остатка ────────────────────

def test_nudge_respects_quiet_hours(tmp_path, monkeypatch):
    _ready(tmp_path, "nudge_quiet.db")
    from services import scheduler as sched
    from database import db as db_mod
    from services import quiet_hours

    marked = []
    _patch_common(
        monkeypatch, sched, db_mod,
        candidates=[DELEGATE_QUIET], nudge_text="Осталось {remaining} вопросов",
        marked_out=marked,
    )

    from datetime import datetime

    async def fake_defer_until(_now, _uid):
        return datetime(2026, 9, 8, 9, 0, 0)  # «спит» -- конец окна тихих часов
    monkeypatch.setattr(quiet_hours, "defer_until", fake_defer_until)

    async def _boom(_tid):
        raise AssertionError("get_reg_draft must not be called for a quiet-hours candidate")
    monkeypatch.setattr(db_mod, "get_reg_draft", _boom)

    bot = _FakeMeBot()
    sched._bot = bot
    asyncio.run(sched.nudge_incomplete_registrations())

    assert bot.sent == []
    assert marked == []


# ── Задача 2: тумблер имени файла резюме — post_finalize читает реестр ────────────────────

def test_finalize_passes_mode_from_registry(tmp_path, monkeypatch):
    _ready(tmp_path, "resume_mode_finalize.db")
    from services import reg_finalize as rf
    from services import nextcloud as nextcloud_mod

    UID = 940950

    async def scenario():
        await db.add_user({
            "telegram_id": UID, "full_name": "Иван Петров", "username": "@qleafye",
            "registration_date": "2026-09-07 10:00:00", "event_city": None,
            "participant_type": "full",
        })
        await db.set_user_status(UID, "approved")
        await db.set_setting("resume_filename_short_mode", "on")

        upload_calls = []

        async def fake_upload_resume(bot, file_id, filename):
            upload_calls.append(filename)
            return "https://cloud.example.org/s/TOK/download?path=%2F&files=x.pdf"

        monkeypatch.setattr(nextcloud_mod, "upload_resume", fake_upload_resume)

        class _FakeBot:
            async def send_message(self, *a, **kw):
                pass

        await rf.post_finalize(
            _FakeBot(), UID, "new",
            resume_file_id="FILE1", resume_file_name="resume.pdf",
        )
        return upload_calls

    calls = asyncio.run(scenario())
    assert calls, "upload_resume должен быть вызван"
    filename = calls[0]
    # режим "id" -- ни ФИО, ни ник в имени файла, только id + дата
    assert "Иван" not in filename
    assert "qleafye" not in filename
    assert filename.startswith(f"{UID}_")
    assert filename.endswith(".pdf")
