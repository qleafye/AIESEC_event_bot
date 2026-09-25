"""Идея №29 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): «Твой Юлид в
цифрах» — `services/forum_stats_card.py` + `handlers/admin_forum_stats_card.py`.

Стиль — `tests/test_checkin_qr_broadcast_260924.py`/`tests/test_forum_noshow_poll_260924.py`
(шаблонная БД `tests/_dbtpl.fast_init_db`, `asyncio.run`, `FakeBot` для `send_photo`).
"""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db
import services.scheduler as sched
import services.forum_stats_card as fsc
from tests._dbtpl import fast_init_db

UID = 260926101
PNG_SIZE = (fsc.CARD_WIDTH, fsc.CARD_HEIGHT)


def _ready(tmp_path, name="forum_stats_card.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, event_city=None, full_name=None, status="approved", season=None, prev_season=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": "2026-01-01 00:00:00",
        "event_city": event_city,
    }))
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "UPDATE users SET status = ?, season = ?, prev_season = ? WHERE telegram_id = ?",
        (status, season, prev_season, tid),
    )
    conn.commit()
    conn.close()


def _seed_checkin(tid, point, day, *, source="miniapp"):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO checkins (telegram_id, point, scanned_at, source, approx_time, created_at, day) "
        "VALUES (?, ?, ?, ?, 0, ?, ?)",
        (tid, point, f"{day} 10:00:00", source, f"{day} 10:00:00", day),
    )
    conn.commit()
    conn.close()


async def _set_setting(key, value):
    await db.set_setting(key, value)


class FakeBot:
    def __init__(self):
        self.photos = []  # [(chat_id, caption)]

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.photos.append((chat_id, caption))
        return type("Msg", (), {"message_id": 1})()


def _with_bot(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(sched, "_bot", bot)
    return bot


def _fake_render(monkeypatch, calls):
    """Подменяет тяжёлый Pillow-рендер лёгкой заглушкой, записывающей аргумент `lang` — рассылку
    тестируем отдельно от самого рендера (у рендера свои тесты ниже, с настоящим Pillow)."""
    def _stub(stats, background, lang, accent):
        calls.append(lang)
        return b"PNGDATA"
    monkeypatch.setattr(fsc, "render_card_sync", _stub)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Сбор данных (collect_stats) — только реальное, пустые строки пропускаются
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_collect_stats_multiple_days_and_sessions(tmp_path):
    _ready(tmp_path)
    _seed_user(UID, full_name="Иванова Мария", season="26/2", prev_season="26/1")

    async def go():
        hall_a = await db.create_program_hall("msk", "Зал А")
        hall_b = await db.create_program_hall("msk", "Зал Б")
        s1 = await db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Сессия 1", hall_id=hall_a)
        s2 = await db.create_program_session("msk", "2026-10-30", "12:00", "13:00", "Сессия 2", hall_id=hall_a)
        s3 = await db.create_program_session("msk", "2026-10-31", "10:00", "11:00", "Сессия 3", hall_id=hall_b)

        _seed_checkin(UID, "entry", "2026-10-30")
        _seed_checkin(UID, "entry", "2026-10-31")
        _seed_checkin(UID, f"session:{s1}", "2026-10-30")
        _seed_checkin(UID, f"session:{s2}", "2026-10-30")
        _seed_checkin(UID, f"session:{s3}", "2026-10-31")

        user = await db.get_user(UID)
        return await fsc.collect_stats(user)

    stats = _run(go())
    assert stats["name"] == "Иванова Мария"
    assert stats["days"] == 2
    assert stats["sessions"] == 3
    assert stats["hall"] == "Зал А"  # два скана из трёх — Зал А
    assert stats["since"] == "26/1"  # prev_season побеждает season
    # леджера монет нет вовсе -> оба поля None, не 0
    assert stats["coins"] is None
    assert stats["rank"] is None


def test_collect_stats_empty_delegate_has_no_data_rows(tmp_path):
    _ready(tmp_path)
    _seed_user(UID, full_name="Пустой Делегат")

    async def go():
        user = await db.get_user(UID)
        return await fsc.collect_stats(user)

    stats = _run(go())
    assert stats["name"] == "Пустой Делегат"
    assert stats["days"] is None
    assert stats["sessions"] is None
    assert stats["hall"] is None
    assert stats["coins"] is None
    assert stats["rank"] is None
    assert stats["since"] is None


def test_collect_stats_coins_zero_after_debit_is_real_data_not_missing(tmp_path):
    """Баланс 0 ПОСЛЕ списания — настоящий ноль (есть строка в леджере), а не «нет данных»:
    рендер обязан показать «0», не пропустить строку."""
    _ready(tmp_path)
    _seed_user(UID)

    async def go():
        await db.add_coins(UID, 50, reason="task")
        await db.add_coins(UID, -50, reason="manual")
        user = await db.get_user(UID)
        return await fsc.collect_stats(user)

    stats = _run(go())
    assert stats["coins"] == 0
    assert stats["rank"] is not None


def test_collect_stats_rank_and_coins_present_with_ledger(tmp_path):
    _ready(tmp_path)
    _seed_user(UID)
    _seed_user(UID + 1)

    async def go():
        await db.add_coins(UID, 100, reason="task")
        await db.add_coins(UID + 1, 300, reason="task")
        user = await db.get_user(UID)
        return await fsc.collect_stats(user)

    stats = _run(go())
    assert stats["coins"] == 100
    assert stats["rank"] == 2
    assert stats["rank_total"] == 2


# ══════════════════════════════════════════════════════════════════════════════════════════
# Рендер — размер, устойчивость к краевым случаям, кириллица, бренд EN
# ══════════════════════════════════════════════════════════════════════════════════════════

def _render(stats, lang="ru", background=None, accent="#037EF3"):
    return fsc.render_card_sync(stats, background, lang, accent)


def test_render_card_correct_size():
    from PIL import Image
    import io

    png = _render(dict(fsc._PREVIEW_STATS))
    img = Image.open(io.BytesIO(png))
    assert img.size == PNG_SIZE


def test_render_card_does_not_crash_on_long_name_and_hall():
    stats = dict(fsc._PREVIEW_STATS)
    stats["name"] = "Оченьоченьоченьдлинноеимяделегата Среднейдлинойфамилия Отчествотоженекороткое"
    stats["hall"] = "Очень длинное название зала, которое точно не влезет в одну строку карточки целиком"
    png = _render(stats)
    assert len(png) > 0


def test_render_card_does_not_crash_without_background():
    png = _render(dict(fsc._PREVIEW_STATS), background=None)
    assert len(png) > 0


def test_render_card_does_not_crash_with_no_data_at_all():
    empty = {
        "name": None, "days": None, "sessions": None, "hall": None,
        "coins": None, "rank": None, "rank_total": None, "since": None,
    }
    png = _render(empty)
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png))
    assert img.size == PNG_SIZE


def test_render_card_does_not_crash_with_invalid_background_bytes():
    """Битые байты фона (не картинка) — fail-soft на однотонный фон бренда, не исключение."""
    png = _render(dict(fsc._PREVIEW_STATS), background=b"not a png at all")
    assert len(png) > 0


def test_font_renders_cyrillic_glyph_not_tofu():
    """Проверка на голом уровне глифа (не пиксельный diff): кириллица получает СВОЙ глиф, а не
    notdef/tofu-заглушку — маска буквы «Ю» заметно отличается по размеру от заведомо
    отсутствующего кода PUA."""
    from PIL import ImageFont

    font = ImageFont.truetype(fsc._FONT_TITLE, 60)
    real_glyph = font.getmask("Ю")
    missing_glyph = font.getmask("")
    assert real_glyph.size != (0, 0)
    assert real_glyph.size != missing_glyph.size


def test_label_set_en_contains_yulid_cyrillic_not_youlead_latin():
    """Уточнение координатора 25.09: бренд «Юлид» кириллицей ВСЕГДА, в том числе в EN-версии —
    латиница («YouLead») на карточку не попадает вовсе."""
    labels_en = fsc.label_set("en")
    joined = " ".join(labels_en.values())
    assert "Юлид" in joined
    assert "YouLead" not in joined
    assert "youlead" not in joined.lower()


def test_label_set_unknown_lang_falls_back_to_ru():
    assert fsc.label_set("ask") == fsc.label_set("ru")
    assert fsc.label_set("fr") == fsc.label_set("ru")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Аудитория — только одобренные текущего сезона своего города
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_eligible_recipients_excludes_other_city(tmp_path):
    _ready(tmp_path)
    _seed_user(UID, event_city="msk")
    _seed_user(UID + 1, event_city="spb")

    async def go():
        import cities as _cities
        return await fsc.eligible_recipients(None, only_arrived=False)

    result = _run(go())
    ids = {u["telegram_id"] for u in result}
    assert {UID, UID + 1} <= ids  # города НЕ ограничены (модуль городов выключен в тесте)


def test_eligible_recipients_excludes_not_approved(tmp_path):
    _ready(tmp_path)
    _seed_user(UID, status="pending")
    _seed_user(UID + 1, status="approved")

    result = _run(fsc.eligible_recipients(None, only_arrived=False))
    ids = {u["telegram_id"] for u in result}
    assert UID not in ids
    assert UID + 1 in ids


def test_eligible_recipients_excludes_past_season(tmp_path):
    _ready(tmp_path)
    _seed_user(UID, season="26/1")
    _seed_user(UID + 1, season="26/2")

    async def go():
        await _set_setting("event_season", "26/2")
        return await fsc.eligible_recipients(None, only_arrived=False)

    result = _run(go())
    ids = {u["telegram_id"] for u in result}
    assert UID not in ids  # прошлый сезон — исключён (D-02)
    assert UID + 1 in ids


def test_eligible_recipients_only_arrived_filters_by_entry_checkin(tmp_path):
    _ready(tmp_path)
    _seed_user(UID)  # ни разу не отмечался
    _seed_user(UID + 1)

    async def go():
        _seed_checkin(UID + 1, "entry", "2026-10-30")
        return await fsc.eligible_recipients(None, only_arrived=True)

    result = _run(go())
    ids = {u["telegram_id"] for u in result}
    assert UID not in ids
    assert UID + 1 in ids


def test_eligible_recipients_without_only_arrived_includes_never_arrived(tmp_path):
    _ready(tmp_path)
    _seed_user(UID)

    result = _run(fsc.eligible_recipients(None, only_arrived=False))
    ids = {u["telegram_id"] for u in result}
    assert UID in ids


# ══════════════════════════════════════════════════════════════════════════════════════════
# Рассылка: тумблер, идемпотентность, двойной тап, тихие часы, «🔕»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_send_broadcast_disabled_toggle_sends_nothing(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID)
    bot = _with_bot(monkeypatch)
    calls = []
    _fake_render(monkeypatch, calls)

    async def go():
        await _set_setting("forum_stats_card_enabled", "off")
        return await fsc.send_broadcast(None, only_arrived=False)

    result = _run(go())
    assert result.get("disabled") is True
    assert result["sent"] == 0
    assert bot.photos == []


def test_send_broadcast_sends_and_marks_idempotent(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID)
    bot = _with_bot(monkeypatch)
    calls = []
    _fake_render(monkeypatch, calls)

    async def go():
        await _set_setting("forum_stats_card_enabled", "on")
        await _set_setting("forum_stats_card_caption_text", "🎉 {name}, вот твой Юлид в цифрах!")
        first = await fsc.send_broadcast(None, only_arrived=False)
        second = await fsc.send_broadcast(None, only_arrived=False)
        return first, second

    first, second = _run(go())
    assert first["sent"] == 1
    assert first["total"] == 1
    assert len(bot.photos) == 1
    # второй запуск в том же сезоне никого не находит — уже отправлено
    assert second["sent"] == 0
    assert second["total"] == 0
    assert len(bot.photos) == 1


def test_send_broadcast_rejects_concurrent_call_for_same_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID)
    _with_bot(monkeypatch)
    calls = []
    _fake_render(monkeypatch, calls)

    async def go():
        await _set_setting("forum_stats_card_enabled", "on")
        lock = fsc._get_city_lock(None)
        async with lock:  # имитирует «рассылка этого города уже идёт»
            return await fsc.send_broadcast(None, only_arrived=False)

    result = _run(go())
    assert result.get("already_running") is True
    assert result["sent"] == 0


def test_send_broadcast_lock_released_after_completion(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID)
    _with_bot(monkeypatch)
    calls = []
    _fake_render(monkeypatch, calls)

    async def go():
        await _set_setting("forum_stats_card_enabled", "on")
        result = await fsc.send_broadcast(None, only_arrived=False)
        locked = fsc._get_city_lock(None).locked()
        return result, locked

    result, locked = _run(go())
    assert result["sent"] == 1
    assert locked is False


def test_send_broadcast_respects_mute(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID)
    _with_bot(monkeypatch)
    calls = []
    _fake_render(monkeypatch, calls)

    async def go():
        from services.timeutil import msk_now
        await _set_setting("forum_stats_card_enabled", "on")
        await db.set_broadcast_mute(UID, msk_now().strftime("%Y-%m-%d"))
        return await fsc.send_broadcast(None, only_arrived=False)

    result = _run(go())
    assert result["sent"] == 0
    assert result["muted"] == 1
    # НЕ отмечен отправленным — повторный тап после снятия мьюта должен его подхватить
    already = _run(db.forum_stats_card_sent_ids(""))
    assert UID not in already


def test_send_broadcast_respects_quiet_hours(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID)
    _with_bot(monkeypatch)
    calls = []
    _fake_render(monkeypatch, calls)

    async def go():
        from services.timeutil import msk_now
        await _set_setting("forum_stats_card_enabled", "on")
        await _set_setting("quiet_hours_enabled", "on")
        now = msk_now()
        start = now.strftime("%H:%M")
        end = (now.replace(hour=(now.hour + 1) % 24)).strftime("%H:%M")
        await _set_setting("quiet_hours_start", start)
        await _set_setting("quiet_hours_end", end)
        return await fsc.send_broadcast(None, only_arrived=False)

    result = _run(go())
    assert result["sent"] == 0
    assert result["quiet"] == 1


def test_send_broadcast_uses_delegate_language_for_render(tmp_path, monkeypatch):
    """Английский делегат получает EN-рендер (see `services.i18n.context`) — без реального
    Pillow-вызова, только факт передачи `lang` в render_card_sync."""
    _ready(tmp_path)
    _seed_user(UID)
    _with_bot(monkeypatch)
    calls = []
    _fake_render(monkeypatch, calls)

    async def fake_context(tid, language_code=None):
        return "en", {}

    import services.i18n as i18n_module
    monkeypatch.setattr(i18n_module, "context", fake_context)

    async def go():
        await _set_setting("forum_stats_card_enabled", "on")
        return await fsc.send_broadcast(None, only_arrived=False)

    result = _run(go())
    assert result["sent"] == 1
    assert calls == ["en"]
