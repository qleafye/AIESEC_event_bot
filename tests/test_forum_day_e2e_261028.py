"""«День форума» Юлид Москва (30–31.10) сквозным сценарием — так, как его проведёт менеджер.

Менеджер настраивает всё кнопками админки (через настоящий `Dispatcher`, не прямой записью в
базу), бот рассылает QR вечером накануне и утром, волонтёр сканирует в Mini App с настоящей
подписью initData, отметки уходят в лист, делегат получает приветствие и меню дня форума.
Контейнер живёт в UTC, часы сценария — московские (`tests/_forum_day_world.py`)."""
from __future__ import annotations

from database import db
from services.forum.checkin import ENTRY_POINT
from tests._forum_day_world import (  # noqa: F401  (world_factory — фикстура)
    ADMIN_ID, MSK, VOLUNTEER, World, at, manager_invites_volunteer, manager_sets_up_moscow, world_factory,
)


def test_setup_through_admin_puts_jobs_on_msk_moments(world_factory):
    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        await manager_sets_up_moscow(w)
        from domain.cities import get_setting_typed_for_city
        from services.applications.reject_rules import forum_date_for
        return {
            "date": await forum_date_for(MSK),
            "days": await get_setting_typed_for_city("sos_active_days", MSK),
            "qr": await db.get_setting("checkin_qr_enabled"),
            "evening": w.job_time_msk("checkin_qr_evening:msk"),
            "morning": w.job_time_msk("checkin_qr_morning:msk"),
            "alerts": w.bot.alerts(),
            "texts": w.bot.texts_to(ADMIN_ID),
        }

    got = world_factory(scenario)
    assert got["date"] == "30.10.2026", got["texts"]
    assert got["days"] == 2
    assert got["qr"] == "on"
    assert got["evening"] == "29.10.2026 18:00", got
    assert got["morning"] == "30.10.2026 07:30", got


async def run_job(w: World, job_id: str):
    job = w.scheduler.get_job(job_id)
    assert job is not None, f"джобы {job_id} нет в планировщике"
    return await job.func(*job.args, **job.kwargs)


async def _entry_days(tid: int) -> list[str]:
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT day FROM checkins WHERE telegram_id = ? AND point = 'entry' ORDER BY day", (tid,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def _entry_time(tid: int) -> list[str]:
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT scanned_at, source FROM checkins WHERE telegram_id = ? AND point = 'entry' ORDER BY day", (tid,),
        ) as cur:
            return [tuple(r) for r in await cur.fetchall()]


def test_full_forum_day(world_factory, monkeypatch):  # noqa: C901
    from tests.test_sheet_arrival_queue_260925 import QueueFakeWorksheet
    from tests.test_sheet_status_city_tab_260819 import _patch_fake_sheets
    import services.sheets.sheets as sheets
    main = QueueFakeWorksheet("main", rows=[[str(700_000 + i), "Одобрена", ""] for i in range(50)])
    _patch_fake_sheets(monkeypatch, {"__main__": main})
    monkeypatch.setattr(sheets, "_open_named_or_main_sync", lambda t: main)
    # Карточка QR рисуется ~0,4 с: первая — настоящая, остальные 89 — её копия (иначе тест идёт
    # полминуты; сама карточка проверена в своих тестах).
    from services.forum import qr_card
    real_render, cache = qr_card.render_qr_card, []
    monkeypatch.setattr(qr_card, "render_qr_card", lambda *a: cache[0] if cache else cache.append(real_render(*a)) or cache[0])
    import services.checkin_broadcast as cb
    monkeypatch.setattr(cb.asyncio, "sleep", _no_sleep)

    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        out = {}
        await manager_sets_up_moscow(w)
        await manager_invites_volunteer(w)
        from handlers.access.admin_caps import resolve_capabilities
        out["vol_caps"] = sorted(await resolve_capabilities(VOLUNTEER))
        out["vol_texts"] = w.bot.texts_to(VOLUNTEER)

        # ── 29.10 18:00 — вечерняя рассылка ────────────────────────────────────────────────
        at(2026, 10, 29, 18, 0)
        out["evening"] = await run_job(w, "checkin_qr_evening:msk")
        out["evening_photos"] = len(w.bot.photo_recipients())
        out["evening_caption"] = w.bot.photos_to(ids[0])[0].caption
        for tid in ids[:10]:
            await w.tap(tid, "checkinqr_confirm")
        # сверка «каждые 10 минут» не перевзводит отработавшую вечернюю
        from services.checkin_broadcast import reconcile_broadcasts
        at(2026, 10, 29, 18, 10)
        await reconcile_broadcasts()
        out["evening_after_reconcile"] = w.job_time_msk("checkin_qr_evening:msk")
        out["morning_after_reconcile"] = w.job_time_msk("checkin_qr_morning:msk")

        # ── 30.10 07:30 — утренний повтор неподтвердившим ───────────────────────────────────
        at(2026, 10, 30, 7, 30)
        before = len(w.bot.photo_recipients())
        out["morning"] = await run_job(w, "checkin_qr_morning:msk")
        out["morning_photos"] = len(w.bot.photo_recipients()) - before
        out["morning_kb"] = w.bot.photos_to(ids[20])[-1].reply_markup

        # ── форумные функции: приветствие, меню дня форума, отчёт дня ───────────────────────
        await w.tap(ADMIN_ID, f"forumwelcome_toggle:{MSK}")
        await w.tap(ADMIN_ID, f"forumdaymenu_toggle:{MSK}")
        await w.tap(ADMIN_ID, f"forumdayreport_toggle:{MSK}")
        out["report_job"] = w.job_time_msk("forum_day_report:msk")

        # ── 30.10 09:00 — сканер Mini App на «🚪 Вход» ─────────────────────────────────────
        at(2026, 10, 30, 9, 0)
        from services.forum.checkin import build_checkin_payload
        from services.infra import miniapp_outbox
        users = {tid: await db.get_user(tid) for tid in ids}
        qr = {tid: await build_checkin_payload(users[tid]) for tid in ids}
        points = w.app_get(VOLUNTEER, "/app/api/checkin/points").json()
        out["points"] = points
        r1 = w.app_post(VOLUNTEER, "/app/api/checkin/scan", {"payload": qr[ids[0]], "point": ENTRY_POINT})
        out["scan1"] = (r1.status_code, r1.json())
        r2 = w.app_post(VOLUNTEER, "/app/api/checkin/scan", {"payload": qr[ids[0]], "point": ENTRY_POINT})
        out["scan_dup"] = (r2.status_code, r2.json())
        await miniapp_outbox.drain(w.bot)
        out["welcome"] = w.bot.texts_to(ids[0])[-1]

        # ручной поиск (телефон сел)
        found = w.app_get(VOLUNTEER, "/app/api/checkin/search", q="Делегат05").json()
        out["search"] = found
        r3 = w.app_post(VOLUNTEER, "/app/api/checkin/manual", {"telegram_id": ids[5], "point": ENTRY_POINT})
        out["manual"] = (r3.status_code, r3.json())

        # меню «день форума» делегата
        at(2026, 10, 30, 10, 0)
        from keyboards.builders import get_main_menu_kb
        menu = await get_main_menu_kb(ids[30])
        out["day_menu_top"] = [b.text for b in menu.keyboard[0]]

        # офлайн-сканер: CSV в формате Binary Eye (время — по часам телефона, МСК)
        csv = '"date","content","format"\n' + "".join(
            f'"2026-10-30 10:{i:02d}:00","{qr[tid]}","QR_CODE"\n' for i, tid in enumerate(ids[10:20])
        ) + f'"2026-10-30 10:30:00","{qr[ids[0]]}","QR_CODE"\n'
        await w.tap(ADMIN_ID, "checkin_upload_start")
        await w.upload(ADMIN_ID, "BinaryEye.csv", csv.encode("utf-8"))
        await w.tap(ADMIN_ID, f"checkin_point:{ENTRY_POINT}")
        out["csv_report"] = w.bot.texts_to(ADMIN_ID)[-1]
        out["csv_days"] = await _entry_days(ids[12])
        out["csv_time"] = await _entry_time(ids[12])

        # очередь «Пришёл» → лист
        from services.sheets import sheet_arrival_sync
        out["sheet_drain"] = await sheet_arrival_sync.drain()

        # SOS делегата (чат SOS не привязан — сигнал уходит админам в личку)
        before_admin = len(w.bot.texts_to(ADMIN_ID))
        await w.send(ids[30], "🆘 SOS")
        out["sos_delegate"] = w.bot.texts_to(ids[30])[-1:]
        out["sos_admin"] = w.bot.texts_to(ADMIN_ID)[before_admin:]

        # «📨 Написать не пришедшим»
        at(2026, 10, 30, 12, 0)
        await w.tap(ADMIN_ID, f"cna_send:{MSK}")
        confirm = w.bot.last_markup(ADMIN_ID)
        go = confirm.inline_keyboard[0][0].callback_data
        await w.tap(ADMIN_ID, go)
        out["cna_report"] = w.bot.texts_to(ADMIN_ID)[-1]

        # отчёт дня в 21:00
        at(2026, 10, 30, 21, 0)
        before_admin = len(w.bot.texts_to(ADMIN_ID))
        await run_job(w, "forum_day_report:msk")
        out["day_report"] = w.bot.texts_to(ADMIN_ID)[before_admin:]
        out["report_job_next"] = w.job_time_msk("forum_day_report:msk")

        # граница суток: 23:59 30.10 и 00:01 31.10
        at(2026, 10, 30, 23, 59)
        r4 = w.app_post(VOLUNTEER, "/app/api/checkin/scan", {"payload": qr[ids[7]], "point": ENTRY_POINT})
        out["scan_2359"] = r4.json()
        at(2026, 10, 31, 0, 1)
        r5 = w.app_post(VOLUNTEER, "/app/api/checkin/scan", {"payload": qr[ids[7]], "point": ENTRY_POINT})
        out["scan_0001"] = r5.json()
        out["days_7"] = await _entry_days(ids[7])

        # 31.10: утренний повтор, второй день входа, приветствие не повторяется
        out["morning2_job"] = w.job_time_msk("checkin_qr_morning:msk")
        at(2026, 10, 31, 9, 0)
        n_before = len(w.bot.texts_to(ids[0]))
        r6 = w.app_post(VOLUNTEER, "/app/api/checkin/scan", {"payload": qr[ids[0]], "point": ENTRY_POINT})
        out["scan_day2"] = r6.json()
        await miniapp_outbox.drain(w.bot)
        out["day2_texts_0"] = w.bot.texts_to(ids[0])[n_before:]
        return out

    out = world_factory(scenario)
    assert "checkin" in out["vol_caps"], out["vol_texts"]
    assert out["evening"]["sent"] == 50, out["evening"]
    assert out["evening_photos"] == 50
    assert out["evening_after_reconcile"] is None
    assert out["morning_after_reconcile"] == "30.10.2026 07:30"
    assert out["morning"]["sent"] == 40, out["morning"]
    assert out["morning_photos"] == 40
    assert out["report_job"] == "30.10.2026 21:00", out["report_job"]
    assert out["scan1"][0] == 200 and out["scan1"][1]["status"] == "new", out["scan1"]
    assert out["scan_dup"][1]["status"] == "duplicate", out["scan_dup"]
    assert "Добро пожаловать" in out["welcome"], out["welcome"]
    assert out["manual"][1]["status"] == "new", out["manual"]
    assert out["scan_2359"]["status"] == "new", out["scan_2359"]
    assert out["scan_0001"]["status"] == "new", out["scan_0001"]
    assert out["days_7"] == ["2026-10-30", "2026-10-31"]  # 23:59 и 00:01 — два разных дня входа
    # меню «день форума»: QR и SOS наверху
    assert out["day_menu_top"] == ["🎟 Мой QR", "🆘 SOS"]
    # утром в день форума QR приходит с главным меню (кнопка SOS появляется без /start)
    morning_buttons = [b.text for row in out["morning_kb"].keyboard for b in row]
    assert "🆘 SOS" in morning_buttons
    assert [p["point"] for p in out["points"]["points"]][0] == ENTRY_POINT  # D-28: «Вход» первым
    assert [i["telegram_id"] for i in out["search"]["items"]] == [ids_of(5)]
    # офлайн-CSV: 10 новых, один повтор (уже отмечен сканером), время скана — из файла, МСК
    assert "Отмечено новых: 10 · уже были: 1 · не найдено: 0" in out["csv_report"], out["csv_report"]
    assert "Сегодня пришли: 12 из 50" in out["csv_report"]
    assert out["csv_time"] == [("2026-10-30 10:02:00", "csv")]
    assert out["sheet_drain"] == {"written": 12, "missing": 0, "failed": 0}
    assert main.rows[0][2] == "30.10 09:00" and main.rows[12][2] == "30.10 10:02", main.rows[:13]
    # SOS без чата SOS — карточка суперадмину, делегату — подтверждение
    assert out["sos_admin"] and "SOS #1" in out["sos_admin"][0], out["sos_admin"]
    assert "Сигнал отправлен" in out["sos_delegate"][0]
    assert out["cna_report"] == "✅ Отправлено 38 делегатам из 38.", out["cna_report"]
    assert "Пришли 12 из 50" in out["day_report"][0], out["day_report"]
    assert out["report_job_next"] == "31.10.2026 21:00"
    assert out["scan_day2"]["status"] == "new"  # D-37: вход каждый день
    assert out["day2_texts_0"] == []  # приветствие — раз за форум
    # Второго утреннего повтора QR нет: повтор — только в день начала форума. Чек-лист Москвы
    # обещает «31.10 то же самое: утренний повтор» — расходится с ботом (решение за людьми).
    assert out["morning2_job"] is None


_real_sleep = __import__("asyncio").sleep


async def _no_sleep(delay, *a, **k):
    await _real_sleep(0)


def ids_of(i: int) -> int:
    return 700_000 + i
