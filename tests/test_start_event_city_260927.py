"""Квик 27.09: делегат не теряет город анкеты.

Прод YouLead (открыты msk/spb/tyumen): заявки доезжали в `users` с `event_city = NULL`.
Четыре трассы `reg_events` из разбора:

- 5152902380: `start`/`form_started` spb -> через 3 минуты голый /start -> экран
  «Продолжить/Заново» -> «Заново» -> `form_started` None (черновик с городом удалён, анкета
  стартует с пустым FSM);
- 669065211: черновик tyumen от 11.09, 26.09 голый /start -> `form_started` None -> подача NULL;
- 6216823392: отклонён msk в этом сезоне, повторная подача -> `form_completed` None -> NULL;
- 1382135479: одобрен msk, повторная подача -> `form_completed` None -> город затёрт.

Правило после фикса: анкета не начинается без города, если городов больше одного, — город
берётся из того, что уже известно (черновик -> reg_started -> заявка этого сезона ->
последняя запись воронки этого сезона), а если не известно ничего — спрашивается кнопками
выбора города. Подача никогда не пишет NULL поверх уже известного города. Стенд без
модуля городов ведёт себя по-прежнему.

pytest-asyncio недоступен — async через asyncio.run(), БД во временном файле.
"""
import asyncio
from datetime import timedelta

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_flow
from handlers import reg_resume
from services import reg_finalize as rf
from services.timeutil import msk_now
from tests._dbtpl import fast_init_db
from tests.test_miniapp_form import bot_api  # noqa: F401 — фикстура для сторожей Mini App ниже
from tests.test_reg_resume_draft import (
    FakeCommand,
    _FakeCallback,
    _KBCapturingMessage,
    _callback_datas,
    _new_state,
)

UID = 5152902380
SEASON = "YL 26/2"


def _ready(tmp_path, *, cities_on=True, name="start_event_city.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("event_season", SEASON)
        if cities_on:
            await db.set_setting("event_city_enabled", "on")

    asyncio.run(go())


def _all_callback_datas(msg):
    out = []
    for (_t, rm, _p) in msg.sent:
        out.extend(_callback_datas(rm))
    return out


async def _events(uid, event):
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT event_city FROM reg_events WHERE telegram_id = ? AND event = ? ORDER BY id",
            (uid, event),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def _seed_user(uid, *, status, city, season=SEASON):
    await db.add_user({
        "telegram_id": uid, "username": "@deleg", "full_name": "Делегат Тестовый",
        "event_city": city, "season": season, "participant_type": "full",
        "registration_date": "2026-09-18 12:00:00",
    })
    await db.set_user_status(uid, status)


# ── трасса 5152902380: deep-link spb -> голый /start -> «Заново» ─────────────────────────────

def test_restart_after_city_deeplink_keeps_city(tmp_path):
    _ready(tmp_path)

    async def go():
        state = _new_state(UID)
        first = _KBCapturingMessage(UID, "deleg")
        await reg.cmd_start(first, state, bot=object(), command=FakeCommand("city_spb"))
        assert (await db.get_reg_draft(UID))["event_city"] == "spb"

        # через три минуты — голый /start: свежий черновик -> экран «Продолжить/Заново».
        # Между заходами бот перезапускался (MemoryStorage пуст) — город живёт только в БД.
        state = _new_state(UID)
        again = _KBCapturingMessage(UID, "deleg")
        await reg.cmd_start(again, state, bot=object(), command=FakeCommand(None))
        assert "reg_resume:restart" in _all_callback_datas(again)

        callback = _FakeCallback("reg_resume:restart_yes", UID, "deleg")
        await reg_resume.reg_resume_restart_yes(callback, state, bot=object())
        return await db.get_reg_draft(UID), await _events(UID, "form_started"), await state.get_data()

    draft, started, data = asyncio.run(go())
    assert draft["event_city"] == "spb"
    assert data.get("event_city") == "spb"
    assert started and started[-1] == "spb", started
    assert None not in started, started


# ── трасса 669065211: черновик без города (след старого бага) -> «Продолжить» ────────────────

def test_resume_of_cityless_draft_asks_city_then_resumes(tmp_path):
    _ready(tmp_path)
    uid = 669065211

    async def go():
        await db.upsert_reg_draft(
            uid, kind="new", participant_type="full", event_city=None, step="age",
            patch={"full_name": "Иван Иванов"}, source="bot",
        )
        state = _new_state(uid)
        callback = _FakeCallback("reg_resume:continue", uid, "deleg")
        await reg_resume.reg_resume_continue(callback, state, bot=object())
        asked = _all_callback_datas(callback.message)

        pick = _FakeCallback("city_pick:tyumen", uid, "deleg")
        await reg_flow.city_pick(pick, state)
        return asked, await db.get_reg_draft(uid), await state.get_data()

    asked, draft, data = asyncio.run(go())
    assert "city_pick:msk" in asked and "city_pick:tyumen" in asked, asked
    assert draft["event_city"] == "tyumen"
    assert data.get("event_city") == "tyumen"
    # ответы черновика не потеряны — анкета продолжилась, а не началась заново
    assert data.get("full_name") == "Иван Иванов"


def test_resume_of_cityless_draft_recovers_city_from_funnel(tmp_path):
    """Черновик без города, но воронка этого сезона помнит tyumen -> молча продолжаем с ним."""
    _ready(tmp_path)
    uid = 669065211

    async def go():
        await db.record_reg_event(uid, "form_started", event_city="tyumen", season=SEASON)
        await db.upsert_reg_draft(
            uid, kind="new", participant_type="full", event_city=None, step="age",
            patch={"full_name": "Иван Иванов"}, source="bot",
        )
        state = _new_state(uid)
        callback = _FakeCallback("reg_resume:continue", uid, "deleg")
        await reg_resume.reg_resume_continue(callback, state, bot=object())
        return _all_callback_datas(callback.message), await db.get_reg_draft(uid), await state.get_data()

    asked, draft, data = asyncio.run(go())
    assert not [d for d in asked if d and d.startswith("city_pick:")], asked
    assert draft["event_city"] == "tyumen"
    assert data.get("event_city") == "tyumen"


def test_stale_draft_plain_start_asks_city(tmp_path):
    """Черновик tyumen протух (окно 24 ч), голый /start: город не наследуется молча —
    делегата спрашивают кнопками; анкета без города не стартует."""
    _ready(tmp_path)
    uid = 669065211

    async def go():
        await db.upsert_reg_draft(
            uid, kind="new", participant_type="full", event_city="tyumen", step="age",
            patch={}, source="bot",
        )
        stale = (msk_now() - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_drafts SET updated_at = ?, created_at = ? WHERE telegram_id = ?",
                (stale, stale, uid),
            )
            await conn.commit()
        state = _new_state(uid)
        msg = _KBCapturingMessage(uid, "deleg")
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand(None))
        return _all_callback_datas(msg), await _events(uid, "form_started")

    asked, started = asyncio.run(go())
    assert "city_pick:msk" in asked, asked
    assert started == []


def test_party_fallback_without_fsm_asks_city(tmp_path):
    """Старая кнопка «Перейти к полной регистрации» после рестарта (FSM пуст, про город
    ничего не известно) не начинает анкету без города — показывает выбор города."""
    _ready(tmp_path)
    uid = 700000001

    async def go():
        state = _new_state(uid)
        callback = _FakeCallback("party_fallback_full", uid, "deleg")
        await reg_flow.party_fallback_full(callback, state)
        return _all_callback_datas(callback.message), await _events(uid, "form_started")

    asked, started = asyncio.run(go())
    assert "city_pick:spb" in asked, asked
    assert started == []


# ── трасса 6216823392: отклонён msk -> повторная подача без города ──────────────────────────

def test_rejected_resubmission_without_city_keeps_users_city(tmp_path):
    _ready(tmp_path)
    uid = 6216823392

    async def go():
        await _seed_user(uid, status="rejected", city="msk")
        draft = {"telegram_id": uid, "kind": "new", "updated_by": "bot",
                 "answers": {"full_name": "Делегат Тестовый", "participant_type": "full"}}
        await rf.finalize_data(uid, "@deleg", draft)
        return await db.get_user(uid), await _events(uid, "form_completed")

    user, completed = asyncio.run(go())
    assert user["event_city"] == "msk"
    assert completed == ["msk"]


def test_rereg_start_of_rejected_asks_city(tmp_path):
    """Повторная подача отклонённого идёт через выбор города (CONTEXT B), а не мимо него."""
    _ready(tmp_path)
    uid = 6216823392

    async def go():
        await _seed_user(uid, status="rejected", city="msk")
        state = _new_state(uid)
        callback = _FakeCallback("rereg_start", uid, "deleg")
        await reg_flow.rereg_start(callback, state)
        return _all_callback_datas(callback.message), await _events(uid, "form_started")

    asked, started = asyncio.run(go())
    assert "city_pick:msk" in asked, asked
    assert started == []


# ── трасса 1382135479: одобрен msk -> повторная подача затирала город ───────────────────────

def test_approved_resubmission_does_not_wipe_city(tmp_path):
    _ready(tmp_path)
    uid = 1382135479

    async def go():
        await _seed_user(uid, status="approved", city="msk")
        draft = {"telegram_id": uid, "kind": "new", "updated_by": "miniapp",
                 "answers": {"full_name": "Делегат Тестовый"}}
        await rf.finalize_data(uid, "@deleg", draft)
        return await db.get_user(uid)

    assert asyncio.run(go())["event_city"] == "msk"


def test_add_user_never_writes_null_over_known_city(tmp_path):
    _ready(tmp_path)
    uid = 1382135479

    async def go():
        await _seed_user(uid, status="approved", city="msk")
        await db.add_user({"telegram_id": uid, "username": "@deleg", "full_name": "Х",
                           "event_city": None, "season": SEASON,
                           "registration_date": "2026-09-21 10:00:00"})
        first = (await db.get_user(uid))["event_city"]
        await db.add_user({"telegram_id": uid, "username": "@deleg", "full_name": "Х",
                           "event_city": "spb", "season": SEASON,
                           "registration_date": "2026-09-21 10:00:00"})
        return first, (await db.get_user(uid))["event_city"]

    assert asyncio.run(go()) == ("msk", "spb")


# ── стек без городов — как раньше ────────────────────────────────────────────────────────────

def test_cities_module_off_starts_form_without_city(tmp_path):
    _ready(tmp_path, cities_on=False)
    uid = 700000002

    async def go():
        state = _new_state(uid)
        msg = _KBCapturingMessage(uid, "deleg")
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand(None))
        return _all_callback_datas(msg), await _events(uid, "form_started"), await db.get_reg_draft(uid)

    asked, started, draft = asyncio.run(go())
    assert not [d for d in asked if d and d.startswith("city_pick:")], asked
    assert started == [None]
    assert draft["event_city"] is None


def test_single_open_city_is_used_on_bypass_path(tmp_path):
    """Один открытый город: обходной вход (старая кнопка) подставляет его, не спрашивает."""
    _ready(tmp_path)
    uid = 700000003

    async def go():
        await db.set_setting("city_enabled__spb", "off")
        await db.set_setting("city_enabled__tyumen", "off")
        state = _new_state(uid)
        callback = _FakeCallback("party_fallback_full", uid, "deleg")
        await reg_flow.party_fallback_full(callback, state)
        return _all_callback_datas(callback.message), await _events(uid, "form_started")

    asked, started = asyncio.run(go())
    assert not [d for d in asked if d and d.startswith("city_pick:")], asked
    assert started == ["msk"]


# ── Mini App: подача новой анкеты без города ─────────────────────────────────────────────────

def _miniapp_ready(tmp_path, name):
    from tests.test_miniapp_routes import _standard_seed, _set, _use_tmp_db
    path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    _set("event_city_enabled", "on")
    _set("event_season", SEASON)
    return path


def test_miniapp_submit_without_city_asks_city(tmp_path, bot_api):
    from tests.test_miniapp_form import _draft_row, _seed_draft
    from tests.test_miniapp_routes import UNREGISTERED_ID, _cfg, _client, _hdr
    path = _miniapp_ready(tmp_path, "miniapp_city_required.db")
    _seed_draft(UNREGISTERED_ID, kind="new", event_city=None,
                patch={"age": 22, "full_name": "Иван Иванов"})
    resp = _client(_cfg(path)).post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 409, resp.text
    assert resp.json()["reason"] == "city_required"
    assert resp.json()["text"]
    assert asyncio.run(db.get_user(UNREGISTERED_ID)) is None
    assert _draft_row(UNREGISTERED_ID) is not None  # черновик не захвачен и не потерян


def test_miniapp_submit_without_city_uses_known_city(tmp_path, bot_api):
    from tests.test_miniapp_form import _seed_draft
    from tests.test_miniapp_routes import UNREGISTERED_ID, _cfg, _client, _hdr
    path = _miniapp_ready(tmp_path, "miniapp_city_known.db")
    asyncio.run(db.record_reg_event(UNREGISTERED_ID, "start", event_city="tyumen", season=SEASON))
    _seed_draft(UNREGISTERED_ID, kind="new", event_city=None,
                patch={"age": 22, "full_name": "Иван Иванов"})
    resp = _client(_cfg(path)).post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    assert asyncio.run(db.get_user(UNREGISTERED_ID))["event_city"] == "tyumen"


def test_miniapp_form_screen_handles_city_required():
    from tests.test_miniapp_frontend import SCREENS_DIR, _js_without_comments
    text = _js_without_comments(SCREENS_DIR / "form.js")
    start = text.index("async function submitForm(")
    body = text[start:start + 2500]
    assert '"city_required"' in body
