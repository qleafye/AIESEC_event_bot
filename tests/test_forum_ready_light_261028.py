"""«🚦 Готовность к форуму» на пустой и на настроенной Москве — чего светофор не видел.

- сканер в Mini App (основной способ входа, D-08): без адреса приложения у бота или с
  выключенным разделом «🎫 Сканер» волонтёрам нечем отмечать — светофор молчал;
- название мероприятия: пустое — в QR «Пропуск на форум», в приветствии «мероприятие»;
- длина форума и часовой пояс, взятые городом из общей настройки (заданной с шапкой
  «🌍 Все города»): «🟢 1 день» у Москвы — это закрытый вход 31.10."""
from __future__ import annotations

import pytest

from database import db
from tests._forum_day_world import (  # noqa: F401  (world_factory — фикстура)
    ADMIN_ID, MSK, World, at, manager_invites_volunteer, manager_sets_up_moscow, ready_lines, world_factory,
)


def _ready_now(world_factory, prepare=None):
    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        from domain.cities import set_admin_city
        await set_admin_city(ADMIN_ID, MSK)
        if prepare is not None:
            await prepare()
        await w.tap(ADMIN_ID, f"forum_ready:{MSK}")
        return ready_lines(w), w.bot.last_markup(ADMIN_ID)

    return world_factory(scenario)


def test_empty_moscow_then_configured(world_factory, monkeypatch):
    """Пустая Москва — красные дата, вход по QR, сканер, право отметки; после настройки кнопками
    (дата, 2 дня, QR, 18:00/07:30, волонтёр по ссылке) красными остаются только программа и SOS."""
    from config import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        from domain.cities import set_admin_city
        await set_admin_city(ADMIN_ID, MSK)
        await w.tap(ADMIN_ID, f"forum_ready:{MSK}")
        empty = ready_lines(w)
        await manager_sets_up_moscow(w)
        await manager_invites_volunteer(w)
        monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://yl26.example.com")
        await w.tap(ADMIN_ID, f"forum_ready_re:{MSK}")
        return empty, ready_lines(w)

    empty, after = world_factory(scenario)
    red = [ln for ln in empty if ln.startswith("🔴")]
    for part in ("Дата форума", "Вход по QR", "Сканер", "право отметки", "Программа", "Чат SOS"):
        assert any(part in ln for ln in red), (part, empty)
    assert after[0].startswith("🟡 Форум: 30.10–31.10 (2 дня)"), after
    assert "запланирована на 29.10 18:00" in after[1], after
    assert [ln[:1] for ln in after if ln.startswith("🔴")] == ["🔴", "🔴"], after


def test_ready_light_flags_scanner_without_app_address(world_factory, monkeypatch):
    """Основной способ входа — сканер в Mini App (D-08). Без адреса приложения у бота кнопки
    «📱 Приложение» нет ни у кого — светофор молчал."""
    from config import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    lines, _kb = _ready_now(world_factory)
    row = [ln for ln in lines if "Сканер" in ln]
    assert row and row[0].startswith("🔴"), lines


def test_ready_light_flags_scanner_section_off(world_factory, monkeypatch):
    from config import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://yl26.example.com")

    async def prepare():
        await db.set_setting("miniapp_section_checkin", "off")

    lines, kb = _ready_now(world_factory, prepare)
    row = [ln for ln in lines if "Сканер" in ln]
    assert row and row[0].startswith("🔴"), lines
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert f"forumfn_open:app:{MSK}" in cbs


def test_ready_light_scanner_green_and_event_name(world_factory, monkeypatch):
    from config import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://yl26.example.com")
    lines, kb = _ready_now(world_factory)
    assert any(ln.startswith("🟢") and "Сканер" in ln for ln in lines), lines
    name = [ln for ln in lines if "Название" in ln]
    assert name and name[0].startswith("🟡"), lines  # пусто: «Добро пожаловать на мероприятие!»
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "settings_edit:event_name" in cbs

    async def named():
        await db.set_setting("event_name", "Юлид Москва")

    lines, _kb = _ready_now(world_factory, named)
    assert any(ln.startswith("🟢") and "Юлид Москва" in ln for ln in lines), lines


def test_one_day_inherited_by_moscow_blocks_second_day_entry(world_factory):
    """Последствие ловушки выше на полном пути: Москва без своей длины форума наследует общую
    «1» — 31.10 сканер не пускает никого. Светофор обязан это показать, а не «🟢 1 день»."""
    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        from domain.cities import set_admin_city
        assert await set_admin_city(ADMIN_ID, MSK)
        await w.tap(ADMIN_ID, "settings_edit:forum_date")
        await w.tap(ADMIN_ID, "settings_edit_city:forum_date")
        await w.send(ADMIN_ID, "30.10.2026")
        await db.set_setting("sos_active_days", "1")  # так на проде мог оставить регион
        await w.tap(ADMIN_ID, f"forum_ready:{MSK}")
        lines = ready_lines(w)
        at(2026, 10, 31, 10, 0)
        from services.forum import checkin_forum_day
        denial = await checkin_forum_day.entry_day_denial(await db.get_user(ids[0]))
        return lines, denial

    lines, denial = world_factory(scenario)
    assert denial is not None  # так и есть: второй день закрыт — поэтому светофор и обязан сказать
    date_line = lines[0]
    assert date_line.startswith("🟡"), lines
    assert "общ" in date_line, date_line


def test_inherited_timezone_is_flagged_with_its_button(world_factory):
    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        from domain.cities import set_admin_city
        assert await set_admin_city(ADMIN_ID, MSK)
        await w.tap(ADMIN_ID, "settings_edit:forum_date")
        await w.tap(ADMIN_ID, "settings_edit_city:forum_date")
        await w.send(ADMIN_ID, "30.10.2026")
        await w.tap(ADMIN_ID, "settings_edit_city:sos_active_days")
        await w.send(ADMIN_ID, "2")
        await db.set_setting("city_tz_offset", "2")  # «МСК+2» из «Все города» для Тюмени
        await w.tap(ADMIN_ID, f"forum_ready:{MSK}")
        return ready_lines(w), w.bot.last_markup(ADMIN_ID)

    lines, kb = world_factory(scenario)
    assert lines[0].startswith("🟡") and "часовой пояс" in lines[0] and "длина" not in lines[0], lines[0]
    assert f"forumtz_cfg:{MSK}" in [b.callback_data for r in kb.inline_keyboard for b in r]
