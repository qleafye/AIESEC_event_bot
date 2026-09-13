"""Квик-фикс 260913: парсер настройки `payment_options` — корневой модуль без зависимости
на бот-фреймворк (aiogram), сосед `reg_options.py`/`settings_ops.py`.

Перенос из `handlers/payment.py::_parse_options` (Phase 4/5, D-16) — Mini App
(`miniapp/routers/hub.py`) собирает карточку оплаты экрана «Одобрена» (30-05) и раньше
делал `from handlers.payment import _parse_options`, из-за чего образ Mini App тянул за
собой aiogram и весь `handlers/` пакет бота. `handlers/payment.py` держит `_parse_options`
как реэкспорт отсюда — старые монкейпатчи (`handlers.payment._parse_options`) и импорты
(`from handlers.payment import _parse_options`) продолжают работать байт-в-байт.

Здесь только парсер и литералы — никаких импортов бот-фреймворка и проекта.
"""

from __future__ import annotations


def parse_options(raw: str) -> list[tuple[str, int, set[str] | None]]:
    """Parse the payment_options setting → [(label, price, tracks)].

    Two accepted shapes:
    - 'label|price' (unchanged since Phase 4) — tracks is None, meaning "offered to ALL
      tracks" (D-16's backward-compat guarantee: existing RusCo config keeps working
      byte-identical).
    - 'label|price|track1,track2' (Phase 5, D-16) — an optional third field, comma-separated
      track values (each stripped). An empty/blank third field ("label|price|") ALSO yields
      tracks None, not an empty set — an empty set would mean "matches nobody", which is not
      what a trailing empty field means.

    A pipe-less line still yields (line, 0, None), and a non-integer price still falls back
    to 0, exactly as before Phase 5.
    """
    options: list[tuple[str, int, set[str] | None]] = []
    for line in (raw or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            parts = line.split("|")
            label = parts[0].strip() or "Участие"
            try:
                price = int(parts[1].strip())
                if price < 0:  # LOW: a negative fee is meaningless — clamp to 0 like a bad parse
                    price = 0
            except ValueError:
                price = 0
            tracks: set[str] | None = None
            if len(parts) >= 3:
                raw_tracks = parts[2].strip()
                if raw_tracks:
                    tracks = {t.strip() for t in raw_tracks.split(",") if t.strip()} or None
        else:
            label, price, tracks = line, 0, None
        options.append((label, price, tracks))
    return options
