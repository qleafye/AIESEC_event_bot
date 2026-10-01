"""Уведомления внешних форм пачкой, тихие часы, алерт входа, регистрация джоб."""
import asyncio
import inspect
from datetime import time

from config import config
from database import ext_forms_db as ef
from services import ext_forms_notify as N
from services import scheduler as sched
from services.timeutil import msk_now
from tests._dbtpl import fast_init_db

_FMT = "%Y-%m-%d %H:%M:%S"


def _setup(tmp_path, monkeypatch, *, notify, title="РилТолк'Медиа", window=None, quiet=False):
    config.DB_PATH = str(tmp_path / "ext_notify.db")
    fast_init_db()
    calls = []

    async def fake_notify(bot, cap, text, **kw):
        calls.append((cap, text))
        return 1

    async def fake_window(city):
        return window

    monkeypatch.setattr(N, "notify_by_capability", fake_notify)
    monkeypatch.setattr(N, "window_for_city", fake_window)
    monkeypatch.setattr(N, "is_quiet", lambda now, s, e: quiet)

    async def go():
        fid = await ef.create_form(platform="yandex", external_id="x" * 24, title=title)
        await ef.set_form_notify(fid, notify)
        await ef.set_form_notified(fid, "2000-01-01 00:00:00")
        return fid
    return asyncio.run(go()), calls


def _answers(fid, n):
    async def go():
        for i in range(n):
            await ef.insert_answer(
                form_id=fid, answer_id=f"a{i}", answered_at=None,
                received_at=msk_now().strftime(_FMT), payload=[], raw=None,
                matched_telegram_id=None, match_how=None)
    asyncio.run(go())


def test_notify_off_by_default(tmp_path, monkeypatch):
    fid, calls = _setup(tmp_path, monkeypatch, notify=False)
    _answers(fid, 5)
    assert asyncio.run(N.notify_new_answers(object())) == 0
    assert calls == []


def test_batch_message_and_notified_at(tmp_path, monkeypatch):
    fid, calls = _setup(tmp_path, monkeypatch, notify=True)
    _answers(fid, 5)
    assert asyncio.run(N.notify_new_answers(object())) == 1
    assert len(calls) == 1
    cap, text = calls[0]
    assert cap == "moderate_reg"
    assert "+5 ответов в «РилТолк&#x27;Медиа»" in text
    assert asyncio.run(N.notify_new_answers(object())) == 0
    assert len(calls) == 1


def test_zero_new_silent(tmp_path, monkeypatch):
    fid, calls = _setup(tmp_path, monkeypatch, notify=True)
    assert asyncio.run(N.notify_new_answers(object())) == 0
    assert calls == []


def test_quiet_hours_accumulate(tmp_path, monkeypatch):
    fid, calls = _setup(tmp_path, monkeypatch, notify=True,
                        window=(time(22), time(9)), quiet=True)
    _answers(fid, 3)
    assert asyncio.run(N.notify_new_answers(object())) == 0
    assert calls == []
    monkeypatch.setattr(N, "is_quiet", lambda now, s, e: False)
    assert asyncio.run(N.notify_new_answers(object())) == 1
    assert "+3 ответа" in calls[0][1]


def test_title_escaped(tmp_path, monkeypatch):
    fid, calls = _setup(tmp_path, monkeypatch, notify=True, title="<b>X</b>")
    _answers(fid, 1)
    asyncio.run(N.notify_new_answers(object()))
    assert "<b>X" not in calls[0][1]
    assert "&lt;b&gt;" in calls[0][1]


def test_alert_reauth_once(tmp_path, monkeypatch):
    _, calls = _setup(tmp_path, monkeypatch, notify=False)

    async def go():
        cid = await ef.create_connection(
            platform="yandex", org_id=None, org_header=None, access_token="t" * 20,
            refresh_token="r" * 20, expires_at=None, created_by=1)
        await ef.set_connection_status(cid, "needs_reauth")
    asyncio.run(go())
    assert asyncio.run(N.alert_reauth(object())) == 1
    assert calls[0][0] == "settings"
    assert "🔑 Войти через Яндекс" in calls[0][1]
    assert asyncio.run(N.alert_reauth(object())) == 0
    assert len(calls) == 1


def test_jobs_registered_and_safe():
    src = inspect.getsource(sched)
    for job_id in ("ext_forms_pending", "ext_forms_reconcile", "ext_forms_google",
                   "ext_forms_sheet_drain", "ext_forms_notify"):
        assert f'"{job_id}"' in src
    for name in ("pending", "reconcile", "google", "sheet_drain", "notify"):
        fn = getattr(sched, f"ext_forms_{name}_job")
        assert inspect.iscoroutinefunction(fn)
        assert not inspect.signature(fn).parameters


def test_job_swallows_errors(monkeypatch):
    import services.ext_forms_google as G

    async def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(G, "poll_google_forms", boom)
    asyncio.run(sched.ext_forms_google_job())
