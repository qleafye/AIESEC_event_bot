"""Пульт надстроек раздела «🤝 Амбассадоры»: строки клавиатуры и тексты подтверждения.

Хендлеров здесь нет — модуль можно импортировать из сборки клавиатуры, не сдвигая
порядок регистрации роутера. Запись настроек делает `admin_amb_panel`.
"""
from __future__ import annotations

from domain.cities import ALL_CITIES, admin_selected_city, is_per_city, per_city_key
from domain.settings.schema import get_setting_typed
from services.amb.amb_screen import show_link_stats
from services.amb.amb_status import selection_enabled

# Коды кнопок пульта. Менеджер их не видит — только подписи.
ADDON_OFFER = "offer"
ADDON_TEAM = "team"
ADDON_STEPS = "steps"
ADDON_STATS = "stats"

_TEAM_NOTE = ("screen", "amb_off_note", "Выключено: команда с отбором — данные сохранены")
_STEPS_NOTE = ("screen", "amb_off_note", "Выключено: ступени — данные сохранены")

_EFFECT = {
    (ADDON_OFFER, "on"): "После анкеты делегат увидит предложение взять свою ссылку.",
    (ADDON_OFFER, "off"): "Предложение после анкеты исчезнет. Уже выданные ссылки останутся.",
    (ADDON_TEAM, "on"): (
        "Кнопка «Хочу свою ссылку» станет заявкой в команду. Делегат будет кандидатом, "
        "пока вы не нажмёте «Взять». На «Моя ссылка» появится строка статуса."
    ),
    (ADDON_TEAM, "off"): (
        "В команду снова берут сразу по кнопке, строки статуса не будет. "
        "Кандидаты и настройки сохранятся."
    ),
    (ADDON_STEPS, "on"): "Амбассадоры начнут получать сообщения ступеней за приглашённых, прошедших отбор.",
    (ADDON_STEPS, "off"): "Новые ступени не придут. Уже выданные останутся, настройки лестницы не сотрутся.",
    (ADDON_STATS, "on"): "Амбассадор увидит баллы и место в волне на «Моя ссылка».",
    (ADDON_STATS, "off"): "Строки баллов и места исчезнут. Сами баллы и волна останутся.",
}

_TITLE = {
    ADDON_OFFER: "Своя ссылка после анкеты",
    ADDON_TEAM: "Отбор в команду",
    ADDON_STEPS: "Ступени",
    ADDON_STATS: "Баллы и место на «Моя ссылка»",
}

_ASK = {
    ADDON_OFFER: "reg_offer_ref_link",
    ADDON_TEAM: "amb_team_selection_enabled",
    ADDON_STEPS: "amb_qualified_program",
    ADDON_STATS: "amb_show_link_stats",
}


def effect_text(code: str, value: str) -> str | None:
    return _EFFECT.get((code, value))


def confirm_title(code: str) -> str:
    return _TITLE.get(code, "")


def setting_key(code: str) -> str | None:
    return _ASK.get(code)


async def header_city(admin_id: int) -> str | None:
    """Город шапки, если менеджер смотрит один город. «Все города» и выключенный модуль — None."""
    code = await admin_selected_city(admin_id)
    if not code or code == ALL_CITIES:
        return None
    return code


async def addon_on(code: str, city: str | None) -> bool:
    if code == ADDON_OFFER:
        return await get_setting_typed("reg_offer_ref_link") == "on"
    if code == ADDON_TEAM:
        return await selection_enabled(city)
    if code == ADDON_STEPS:
        from domain.cities import get_setting_typed_for_city
        return await get_setting_typed_for_city("amb_qualified_program", city) == "on"
    if code == ADDON_STATS:
        return await show_link_stats(city)
    return False


async def write_target(admin_id: int, base: str) -> str:
    """Куда писать: город шапки, если ключ городской и выбран один город, иначе общее значение."""
    if not is_per_city(base):
        return base
    city = await header_city(admin_id)
    if city is None:
        return base
    return per_city_key(base, city) or base


def _drop(rows: list[tuple], banned: set[str]) -> list[tuple]:
    from handlers.settings.admin_sections import row_callback
    return [row for row in rows if row_callback(row) not in banned]


def _insert_after_sep(rows: list[tuple], sep_text: str, extra: tuple) -> list[tuple]:
    out: list[tuple] = []
    for row in rows:
        out.append(row)
        if row[0] == "sep" and row[1] == sep_text:
            out.append(extra)
    return out


async def _waves_label(admin_id: int) -> str:
    import domain.cities as cities
    from database.db import list_waves
    from services.amb.ambassador_waves import wave_number_label, wave_open

    header = await cities.admin_selected_city(admin_id)
    waves = await list_waves(city_scope=cities.city_scope(header))
    total = len(waves)
    running = [wave for wave in waves if wave_open(wave)]
    if not waves:
        return "🌊 Волны: ещё не заведены"
    if running:
        return f"🌊 Волны: идёт {wave_number_label(running[0])}, всего {total}"
    return f"🌊 Волны: сейчас не идут, всего {total}"


def _state_word(code: str, on: bool) -> str:
    if code == ADDON_STEPS:
        return "включены" if on else "выключены"
    if code == ADDON_STATS:
        return "видны" if on else "скрыты"
    if code == ADDON_TEAM:
        return "включён" if on else "выключен"
    return "включено" if on else "выключено"


async def prepare_amb_rows(rows: list[tuple], admin_id: int, caps: set) -> list[tuple]:
    """Пульт сверху, выключенные отбор и ступени — одна строка. Волны и ядро остаются."""
    city = await header_city(admin_id)
    team_on = await addon_on(ADDON_TEAM, city)
    steps_on = await addon_on(ADDON_STEPS, city)
    if not team_on:
        rows = _drop(rows, {"admin_amb_entry", "admin_amb_candidates"})
        rows = _insert_after_sep(rows, "── Команда и волны ──", _TEAM_NOTE)
    if not steps_on:
        rows = _drop(rows, {"admin_amb_tiers"})
        rows = _insert_after_sep(rows, "── Ступени ──", _STEPS_NOTE)

    can_write = "settings" in caps
    click = "amb_addon_ask:{}" if can_write else "amb_addon_look"
    panel: list[tuple] = []
    for code, label in (
        (ADDON_OFFER, "🎁 Своя ссылка после анкеты"),
        (ADDON_TEAM, "🗳 Отбор в команду"),
        (ADDON_STEPS, "🎓 Ступени"),
        (ADDON_STATS, "📊 Баллы и место на «Моя ссылка»"),
    ):
        on = await addon_on(code, city)
        cb = click.format(code) if can_write else "amb_addon_look"
        panel.append(("screen", cb, f"{label}: {_state_word(code, on)}"))
    panel.append(("screen", "admin_game_waves", await _waves_label(admin_id)))
    return panel + rows
