"""Phase 19: единственная точка регистрации роутеров Mini App — `ALL_ROUTERS`.

Последующие планы фазы добавляют свой модуль в список и дописывают строки в таблицу ниже.

Контракт HTTP-API (план 19-01; реализуется планами 19-01..19-07)
─────────────────────────────────────────────────────────────────
Открытые (без auth):
  GET  /app/health                 -> {"status": "ok"}   (работает даже при выключенном тумблере)
  GET  /app                        -> HTML-оболочка       (план 19-02)
  GET  /app/theme.css              -> акцент из реестра   (план 19-02)
  GET  /app/static/*               -> статика             (план 19-02)

Защищённые (dependency `principal`), префикс /app/api:
  GET  /app/api/me                 -> {telegram_id, via, caps[], city, is_delegate, is_staff,
                                       sections{}, accent, event_name, logo_file_id, bot_username}
  Делегат (планы 19-03/19-04): /app/api/tasks, /app/api/tasks/{id}, /app/api/profile,
      /app/api/coins/balance, /app/api/coins/history, /app/api/leaderboard,
      POST /app/api/submissions, GET /app/api/file/{file_id}
  Общий (план 19-04): POST /app/api/uploads — dependency `upload_actor` (делегат ИЛИ moderate_game)
  Менеджер (планы 19-05..19-07): /app/api/review/next, POST /app/api/review/{sid}/approve|reject,
      /app/api/stats/game, /app/api/admin/tasks*, /app/api/admin/coins*, /app/api/admin/settings

Коды ошибок — всегда JSON-тело с полем `reason`:
  401 {"reason": "no_auth"}        — ни initData, ни cookie
  401 {"reason": "bad_initdata"}   — подпись не сошлась / протух auth_date  -> фронт: «Откройте заново»
  403 {"reason": "staff_only"}     — cookie-ветка без единого права (D-05)
  403 {"reason": "csrf"}           — мутация по cookie без заголовка X-Requested-With: fetch
  403 {"reason": "no_cap", "cap": "..."}       — нет нужной capability
  403 {"reason": "section_off", "section": "..."} — раздел выключен чекбоксом (D-06)
  403 {"reason": "delegate_gate", "kind": "pending"|"rejected"|"unregistered"|"cookie"|"staff_only_mode"}
  503 {"reason": "miniapp_off"}    — тумблер miniapp_enabled = off
  413 {"reason": "too_large", "limit": N}

Мутации по cookie-ветке: заголовок `X-Requested-With: fetch` обязателен (CSRF поверх
`SameSite=Lax`); для initData-ветки не требуется.
"""
from __future__ import annotations

from miniapp.routers import page

ALL_ROUTERS = [
    page.router,
]

__all__ = ["ALL_ROUTERS"]
