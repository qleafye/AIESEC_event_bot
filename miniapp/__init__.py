"""Phase 19: Telegram Mini App — отдельный веб-процесс рядом с ботом (D-01).

Пакет aiogram-free: импортирует `database.db`, `settings_schema`, `cities` и модули
`dashboard.*` напрямую и НИКОГДА не импортирует `handlers.*` — пакетный
`handlers/__init__.py` тянет владельцев роутеров (а с ними aiogram) при импорте ЛЮБОГО
`handlers.x`. Инвариант закреплён subprocess-сторожем в `tests/test_miniapp_headers.py`.
"""
