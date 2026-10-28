"""Шапка «🌍 Все города» и форумные настройки, общее значение которых наследует Москва.

Дата форума при этой шапке уже спрашивала город. Длина форума («🗓 Сколько дней идёт») и
«🕐 Часовой пояс» — нет: общий ключ молча получали все города без своего значения. Регион,
поставивший «1 день» из «Все города», закрывал Москве второй день (31.10 сканер 🔴 на всех)."""
from __future__ import annotations

import pytest

from database import db
from tests._forum_day_world import (  # noqa: F401  (world_factory — фикстура)
    ADMIN_ID, MSK, World, at, world_factory,
)


@pytest.mark.parametrize("key, typed", [("sos_active_days", "1"), ("city_tz_offset", None)])
def test_all_cities_header_asks_city_for_forum_length_and_tz(world_factory, key, typed):
    """Шапка «🌍 Все города»: длина форума и часовой пояс не бывают «общими» — общий ключ
    наследует каждый город без своего значения. Менеджер региона, поставивший «1 день» или
    «МСК+2» из «Все города», молча делал Москву однодневной (31.10 — все сканы 🔴 «не день
    форума») или сдвигал ей рассылку QR на два часа. Бот обязан сначала спросить город."""
    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        from domain.cities import ALL_CITIES, get_setting_typed_for_city, set_admin_city
        assert await set_admin_city(ADMIN_ID, ALL_CITIES)
        await w.tap(ADMIN_ID, f"settings_edit:{key}")
        screen = w.bot.texts_to(ADMIN_ID)[-1]
        kb = w.bot.last_markup(ADMIN_ID)
        if typed is not None:
            await w.send(ADMIN_ID, typed)
        else:  # enum — кнопкой; на экране выбора города её нет, жмём старую «МСК+2»
            await w.tap(ADMIN_ID, "settings_enum_pick:3")
        return screen, kb, await db.get_setting(key), await get_setting_typed_for_city(key, MSK)

    screen, kb, common, msk_value = world_factory(scenario)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"settings_edit_city:{key}@{MSK}" in cbs, screen
    assert common is None, "общее значение записалось — его унаследует Москва"
    assert str(msk_value) in ("2", "0")
