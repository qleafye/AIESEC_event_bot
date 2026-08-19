"""services/heartbeat.py: heartbeat-файл для Docker HEALTHCHECK.

Покрытие: touch/age (atomic write), семантика «файл пишется только при живом поллинге»,
middleware отмечает только getUpdates, `--check` exit-коды через subprocess, и статическая
проверка, что HEALTHCHECK в Dockerfile вызывает реальный модуль с реальной опцией."""
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from aiogram.methods import GetMe, GetUpdates

from services import heartbeat as hb

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_state():
    hb.reset_polling_state()
    yield
    hb.reset_polling_state()


# ---------------------------------------------------------------- touch / age

def test_touch_then_age_is_fresh(tmp_path):
    p = str(tmp_path / "hb")
    hb.touch_heartbeat(p)
    age = hb.heartbeat_age_seconds(p)
    assert age is not None and 0 <= age < 5
    # атомарность: временного файла рядом не остаётся
    assert [f.name for f in tmp_path.iterdir()] == ["hb"]


def test_age_uses_written_timestamp_not_mtime(tmp_path):
    p = str(tmp_path / "hb")
    hb.touch_heartbeat(p, now=1000.0)
    assert hb.heartbeat_age_seconds(p, now=1130.0) == pytest.approx(130.0)
    assert hb.heartbeat_age_seconds(p, now=900.0) == 0.0  # часы назад — не отрицательный


def test_age_none_when_missing_or_garbage(tmp_path):
    assert hb.heartbeat_age_seconds(str(tmp_path / "nope")) is None
    bad = tmp_path / "bad"
    bad.write_text("not-a-number", encoding="ascii")
    assert hb.heartbeat_age_seconds(str(bad)) is None


def test_touch_creates_parent_dir_and_overwrites(tmp_path):
    p = str(tmp_path / "sub" / "dir" / "hb")
    hb.touch_heartbeat(p, now=1.0)
    hb.touch_heartbeat(p, now=2.0)
    assert Path(p).read_text().strip() == "2.000"


def test_clear_heartbeat_idempotent(tmp_path):
    p = str(tmp_path / "hb")
    hb.touch_heartbeat(p)
    hb.clear_heartbeat(p)
    assert not os.path.exists(p)
    hb.clear_heartbeat(p)  # второй раз — не падает


# ---------------------------------------------------------------- polling liveness

def test_polling_alive_window():
    assert hb.polling_alive() is False  # ни одного getUpdates ещё не было
    hb.mark_polling_alive(now=100.0)
    assert hb.polling_alive(stale_after=90, now=150.0) is True
    assert hb.polling_alive(stale_after=90, now=191.0) is False


def test_middleware_marks_only_get_updates():
    mw = hb.PollingHeartbeatMiddleware()

    async def ok_request(bot, method):
        return "resp"

    async def go():
        assert await mw(ok_request, None, GetMe()) == "resp"
        assert hb.polling_alive() is False  # getMe — не тик поллинга
        assert await mw(ok_request, None, GetUpdates()) == "resp"
        assert hb.polling_alive() is True

    asyncio.run(go())


def test_middleware_does_not_mark_on_failure():
    mw = hb.PollingHeartbeatMiddleware()

    async def boom(bot, method):
        raise RuntimeError("network")

    async def go():
        with pytest.raises(RuntimeError):
            await mw(boom, None, GetUpdates())
        assert hb.polling_alive() is False

    asyncio.run(go())


def test_loop_touches_only_when_polling_alive(tmp_path):
    p = str(tmp_path / "hb")

    async def go():
        task = asyncio.create_task(hb.heartbeat_loop(p, interval=0.01, stale_after=90))
        await asyncio.sleep(0.05)
        assert not os.path.exists(p), "без живого поллинга файл писаться не должен"
        hb.mark_polling_alive()
        await asyncio.sleep(0.05)
        assert os.path.exists(p)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_loop_registered_via_background_is_cancelled_by_cancel_all(tmp_path):
    from services.background import cancel_all, pending_count, spawn

    async def go():
        spawn(hb.heartbeat_loop(str(tmp_path / "hb"), interval=0.01))
        await asyncio.sleep(0)
        assert pending_count() >= 1
        assert await cancel_all(timeout=2) >= 1
        assert pending_count() == 0

    asyncio.run(go())


# ---------------------------------------------------------------- --check (subprocess)

def _run_check(*extra):
    return subprocess.run(
        [sys.executable, "-m", "services.heartbeat", "--check", *extra],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )


def test_check_exit_codes(tmp_path):
    p = str(tmp_path / "hb")
    assert _run_check("--path", p).returncode == 1  # нет файла
    hb.touch_heartbeat(p)
    assert _run_check("--path", p).returncode == 0  # свежий
    hb.touch_heartbeat(p, now=0.0)  # 1970 год — устарел
    r = _run_check("--path", p)
    assert r.returncode == 1 and "stale" in r.stderr


def test_check_function_respects_max_age(tmp_path):
    p = str(tmp_path / "hb")
    hb.touch_heartbeat(p)
    assert hb.check(p, max_age=120) == 0
    assert hb.check(p, max_age=-1) == 1


# ---------------------------------------------------------------- Dockerfile (static)

def test_dockerfile_healthcheck_calls_real_module():
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lines = [ln for ln in df.splitlines() if ln.startswith("HEALTHCHECK")]
    assert len(lines) == 1, "ровно одна инструкция HEALTHCHECK"
    hc = lines[0]
    for opt in ("--interval=60s", "--timeout=5s", "--start-period=60s", "--retries=3"):
        assert opt in hc
    m = re.search(r"CMD\s+python -m (\S+) (--\S+)$", hc.strip())
    assert m, hc
    module, flag = m.group(1), m.group(2)
    assert module == "services.heartbeat" and (ROOT / "services" / "heartbeat.py").is_file()
    # флаг действительно принимается модулем
    assert hb._main([flag, "--path", str(ROOT / "definitely-missing")]) == 1
    # HEALTHCHECK после USER (выполняется от appuser, как и бот) и до CMD
    body = [ln for ln in df.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert body.index("USER appuser") < body.index(hc) < body.index('CMD ["python", "main.py"]')


def test_main_wires_heartbeat():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "bot.session.middleware(PollingHeartbeatMiddleware())" in src
    assert "_spawn(heartbeat_loop())" in src
    assert "clear_heartbeat()" in src
    # clear — в finally после start_polling
    assert src.index("await dp.start_polling(bot)") < src.index("clear_heartbeat()")
