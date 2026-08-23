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
  Делегат (план 19-03, все — `delegate_gate` + `require_section`):
  GET  /app/api/tasks?offset&limit   -> {items[{id,title,category,category_label,coins,deadline_at,
                                       deadline_short,status,attempt,overdue}], total, limit, offset}
                                       status: new|pending|approved|rejected; limit <= 50, дефолт 25
  GET  /app/api/tasks/{id}         -> задание + card_text, proof_hint, photo_file_id, status,
                                       attempt, can_submit; 404 {"reason":"task_not_found"} и для архивных
  GET  /app/api/profile            -> {full_name, username, fields[{key,label,value}], status,
                                       status_label, payment_status, payment_status_label,
                                       edit_deeplink, edit_hint}
  GET  /app/api/coins/balance      -> {balance, rank|null, participants}
  GET  /app/api/coins/history?offset&limit -> {items[{delta,reason,source,source_label,created_at}],
                                       total, limit, offset}
  GET  /app/api/leaderboard?limit  -> {items[{rank,name,balance,is_me}], me{rank,balance}, total}; limit <= 50
  Делегат (план 19-04):
  POST /app/api/submissions {task_id, parts[{kind, content, part_token?, caption?}]}
                                   -> {submission_id, accepted_text}; `delegate_gate` + section tasks
                                       kind: photo|document (part_token обязателен) | text|link
                                       400 {"reason":"empty"|"too_many_parts"|"bad_part"}
                                       403 {"reason":"bad_part_token"}; 404 task_not_found
                                       409 {"reason":"already_submitted"|"resubmit_limit"}
  Общий (план 19-04): dependency `upload_actor` (одобренный делегат ИЛИ moderate_game), без section:
  GET  /app/api/uploads/limits     -> {max_bytes, photo_max_bytes, max_parts, max_text, too_large_text, empty_hint}
  POST /app/api/uploads  multipart `file` -> {kind: photo|document, content: file_id, part_token}
                                       413 too_large (> 20 МБ); 502 {"reason":"telegram_unavailable"}
  GET  /app/api/file/{file_id}     -> байты файла (прокси getFile; `principal`; владелец части ИЛИ
                                       moderate_game в пределах города ИЛИ обложка/лого); 403/404
  Менеджер (план 19-05, `require_cap("moderate_game")` + section review):
  GET  /app/api/review/next?offset  -> {submission{id,task_id,user_id,submitted_at},
                                       task{id,title,text,category,category_label,coins,proof_label,deadline_at},
                                       delegate{name,username,city}, parts[{kind,content,caption}],
                                       attempt{k,n}|null, after_deadline, archived_task,
                                       remaining, offset, position}
                                       либо {empty: true, remaining, offset}; «⏭» = offset+1 на клиенте
  POST /app/api/review/{sid}/approve {coins?} -> {ok: true, status, coins} | {ok: false, reason: "already"}
                                       coins: 1..100000, дефолт task.coins; 400 bad_coins; 404 not_found
                                       403 {"reason":"out_of_scope","text"} — сдача из чужого города
  POST /app/api/review/{sid}/reject {reason} -> {ok: true, status} | {ok: false, reason: "already"}
                                       400 reason_required (причина обязательна); монет не начисляет
  GET  /app/api/stats/game         -> {participants, submissions{pending,approved,rejected},
                                       by_category[{code,label,count}]} — только агрегаты, без ПД;
                                       `moderate_game` + section stats; числа = get_game_stats()
  Менеджер (план 19-06, `require_cap("moderate_game")` + section admin_tasks; городской скоуп
  на чтении и на каждой мутации — 403 {"reason":"out_of_scope","text"}):
  GET  /app/api/admin/tasks/options -> {categories[{code,label}], proof_types[{code,label}],
                                       deadline_presets[{code,label}], deadline_example, cities[{code,label}],
                                       city_choice, bound_city_label, title_max, text_max}
  GET  /app/api/admin/tasks?archived=0|1&offset&limit -> {items[{id,number,title,category,category_label,
                                       coins,deadline_at,deadline_short,overdue,archived,pending,approved,
                                       has_photo}], total, active_count, archived_count, archived, offset,
                                       limit, empty_text}; limit <= 50
  GET  /app/api/admin/tasks/{id}   -> карточка: все поля + category_label, proof_label, city_label,
                                       deadline_display, card_text (render_task_card_text), photo_file_id,
                                       submissions_count, can_delete, cannot_delete_text; 404 not_found
  PATCH /app/api/admin/tasks/{id} {title | text | coins | deadline_at | photo_file_id(+part_token)
                                       | remove_photo: true} -> {ok, field, task}; РОВНО одно поле
                                       400 one_field/title_empty/text_empty/bad_coins/bad_deadline/
                                       deadline_past/not_a_photo (все с `text`); 403 bad_part_token
                                       deadline_at: пресет today|plus3|plus7 ИЛИ ДД.ММ.ГГГГ ЧЧ:ММ
  POST /app/api/admin/tasks {title, text, category, coins, proof_types[], deadline_at, event_city?,
                                       photo_file_id?, part_token?} -> 201 {ok, id, task}
                                       400 bad_category/bad_proof_type/bad_city (+ поля выше)
                                       привязанный менеджер: event_city игнорируется, ставится его город
  POST /app/api/admin/tasks/{id}/archive | /unarchive -> {ok, changed, archived}
  DELETE /app/api/admin/tasks/{id}  -> {ok, deleted}; 409 {"reason":"has_submissions","text"}
  Каждая мутация -> outbox task_changed {task_id}.
  Менеджер (план 19-07): /app/api/admin/coins*, /app/api/admin/settings

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

from miniapp.routers import admin_tasks, coins, files, page, profile, review, stats, submissions, tasks

ALL_ROUTERS = [
    page.router,
    tasks.router,
    profile.router,
    coins.router,
    submissions.router,
    files.router,
    review.router,
    stats.router,
    admin_tasks.router,
]

__all__ = ["ALL_ROUTERS"]
