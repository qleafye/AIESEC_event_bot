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
                                       sections{}, accent, event_name, logo_file_id, bot_username,
                                       form_status: none|draft|pending|approved|rejected,
                                       form_status_label, form_access, form_first}
                                       form_first — дом приложения = экран анкеты (D-24)
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
  Делегат (план 23.1-03, UI-REDESIGN-02): готовые тексты и факты плиты хаба, 0 форматирования
  на клиенте — {done}/{total}/{days} подставлены сервером:
  GET  /app/api/hub                -> {balance_eyebrow, balance_unit, next_eyebrow,
                                       sections_eyebrow, tasks_fact|null, days_fact|null,
                                       event_dates|null, event_place|null}
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
  Менеджер (план 23-04, APP-TINDER-02, `require_cap("moderate_reg")` + section applications;
  городской скоуп на очереди и на каждой мутации — 403 {"reason":"out_of_scope","text"}):
  GET  /app/api/applications/next?offset&track&changed -> {application{telegram_id,full_name,
                                       username,city,registered_at}, avatar{url|null,initials},
                                       badges[{kind,text}], main_fields[{label,value}],
                                       extra_fields[{label,value}], resume{kind:file|text|none,
                                       url|text}, history[], remaining, position, offset,
                                       filters{reject_templates[],chips{all,changed}}}
                                       либо {empty: true, remaining, offset, empty_text}
                                       track: full|party|short (неизвестное — без фильтра);
                                       changed: "1"|"true" — только изменённые/повторные
  POST /app/api/applications/{tid}/approve -> {ok: true, decision_id, undo_seconds: 5}
                                       | {ok: false, reason: "already"}; эффекты (приветствие/
                                       лист) откладываются на undo_seconds (D-06), не отправляются
                                       синхронно с ответом
  POST /app/api/applications/{tid}/reject {reason?} -> тот же контракт, что approve; причина
                                       необязательна (D-05), обрезается по лимиту шторки
  POST /app/api/applications/undo {decision_id} -> {ok: true} | {ok: false, reason: "too_late"}
                                       404 {"reason":"not_found"} — чужой/неизвестный decision_id
  POST /app/api/applications/approve_all {city} -> {ok: true, count} | {ok: false, reason: "already"}
                                       400 {"reason":"city_required","text"} — город не передан
                                       403 {"reason":"city_mismatch","text"} — не совпадает с
                                       привязкой менеджера (веб-аналог CR-02 appr_all_yes бота);
                                       отмены нет (D-07), эффекты ставятся в outbox сразу
  Менеджер (quick 260904-2cj, `require_cap("moderate_reg")` + section questions; городской
  скоуп — вопрос ДЕЛЕГАТА против привязки менеджера, как у applications/review):
  GET  /app/api/questions?status&offset&limit -> {items[{id,user_id,name,username,city,
                                       asked_at,question_text,status,status_label,stuck,
                                       answered_by_name,answered_at,answer_text,can_answer}],
                                       total, counts{all,new,in_work,answered},
                                       filters[{key,label,count}], offset, limit,
                                       empty_text, answer_button, sent_toast}
                                       status: new|in_work|answered (неизвестное — без фильтра,
                                       не 400); limit <= 50, дефолт 20
  POST /app/api/questions/{qid}/answer {text} -> {ok: true, status: "answered"}
                                       | {ok: false, reason: "already", by}
                                       | {ok: false, reason: "delivery_failed", "text"}
                                       400 {"reason":"empty_text"|"too_long","text"} — до
                                       обращения к Bot API; 404 not_found; захват атомарный
                                       (claim_question), делегату уходит обычный текст (без
                                       parse_mode); уведомление остальных moderate_reg
                                       («кто ответил») из веба не шлётся (aiogram-путь бота)
  Делегат (quick 260906-8uq, задача 5, `delegate_gate` + section faq; правило видимости —
  services.faq.apply_city_overrides, то же самое, что читает бот):
  GET  /app/api/faq                -> {items[{id,question,answer}], empty_text}
                                       городской пункт перекрывает общий с тем же нормализо-
                                       ванным вопросом; выключенные пункты и чужой город никогда
                                       не попадают в ответ
  Менеджер (quick 260906-8uq, задача 6, `require_cap("moderate_reg")` + section questions —
  кнопка «В FAQ» живёт на экране журнала вопросов, не заводит отдельного раздела):
  POST /app/api/faq {question, answer} -> {ok: true, id} | {ok: false, reason: "already", id}
                                       город — Principal.city (привязка менеджера), пусто ->
                                       общий пункт; 400 {"reason":"empty"|"too_long","text"}
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
  Менеджер (план 19-07, `require_cap("moderate_game")` + section coins; городской скоуп на
  поиске и начислении — 403 {"reason":"out_of_scope","text"}):
  GET  /app/api/admin/users/search?q= -> [{telegram_id, name, username, city, balance}]; q < 2 символов
                                       -> []; «@username» — точно, число — telegram_id, иначе часть
                                       имени (<= 20); ПД (телефон, e-mail, вуз) не отдаются
  POST /app/api/admin/coins {user_id, delta, reason} -> {ok, user_id, name, delta, balance}
                                       delta != 0, |delta| <= 100000 (400 bad_delta); reason обязателен
                                       (400 reason_required); 404 not_found; add_coins(source="manual",
                                       changed_by=менеджер) + outbox coins_manual {user_id, delta}
  GET  /app/api/admin/coins?offset&limit -> {items[{id,when,user_id,recipient,delta,reason,changed_by,
                                       changed_by_name,source_label}], total, offset, limit, empty_text};
                                       только source='manual', limit <= 50
  GET  /app/api/admin/coins/presets -> {presets[int], delta_max, reason_max} — из
                                       coins_manual_amount_presets, мусор отброшен, пусто -> дефолт
  Менеджер (план 19-07, `require_cap("settings")` + section settings):
  GET  /app/api/admin/settings     -> [{key, label, value, group_label}] — ТОЛЬКО белый
                                       список EDITABLE_KEYS (все on/off-тумблеры miniapp_* и группы game
                                       из SETTINGS_SCHEMA); key — идентификатор для POST, человеку не
                                       показывается
  POST /app/api/admin/settings {key, value} -> тот же список после записи
                                       403 {"reason":"not_editable"} — ключ вне белого списка
                                       400 {"reason":"bad_value","text"} — value не "on"/"off"
  Делегат (план 21-10, FORM-SYNC-03/04/05): dependency `form_gate` (НЕ `delegate_gate` —
  незарегистрированный/pending/rejected с черновиком kind='new' обязан пройти) + section "form":
  GET  /app/api/reg/draft          -> {exists, kind: new|edit, step, version,
                                       pre[consent:key|city_fork|party_fork], steps[{key, column,
                                       type, label, prompt, options, other_allowed, skip_allowed,
                                       required, max_len, prior{value,display}|null, value,
                                       value_source: answer|prior|null}], progress{done,total},
                                       closed, closed_text|null, prior_badge_text|null}
                                       resume-шаг дополнительно несёт has_prior_resume (bool,
                                       без самого file_id/URL — Pitfall 3)
  PATCH /app/api/reg/draft {version, answers:{column: value|null|{"other":text}}, step?,
                            event_city?, participant_type?}  — выбор из пикеров pre-flow
                                       (pre_items[{type:city_fork|party_fork, field, text,
                                       options[{code,label}], value}]); те же валидаторы, что
                                       у тапа по развилке в боте
                                       409 {"reason":"already_set","field"} — город/трек уже
                                       зафиксирован (deep-link) или kind=edit (D-13)
                                    -> тот же ответ GET + conflicts[column] (колонки, изменённые
                                       из чата после `version` клиента); value=null — «Пропустить»
                                       400 {"reason":"bad_field","field"} — колонка вне allowlist
                                       400 {"reason":"invalid","errors":{column:text}} — тот же
                                       текст ошибки, что у бота (T-21-05)
                                       403 {"reason":"registration_closed","text"} — только для
                                       kind=new (правка одобренной работает и при закрытой
                                       регистрации, D-11)
  POST /app/api/reg/consent/{key}  -> {ok, key}; идемпотентно; 400 {"reason":"bad_key"}
  POST /app/api/reg/draft/submit   -> {mode: new|edit, status, heading, body|null}
                                       409 {"reason":"consent_required","keys","text"} — без
                                       подписанных обязательных согласий (D-23, серверный гейт)
                                       409 {"reason":"already_submitting"} — claim занят (T-21-02)
                                       ставит ровно одно событие outbox reg_finalized|reg_edited,
                                       мгновенный ответ делегату в чат — telegram_api.send_message
  Резюме (план 21-10, D-05) — тот же `upload_actor`, третий сценарий «есть черновик»:
  POST /app/api/uploads?target=resume  multipart `file` (.pdf/.docx, ≤10 МБ) -> {file_id, filename}
                                       400 {"reason":"bad_type","text"} — не PDF/DOCX
                                       413 {"reason":"too_large","text"} — больше 10 МБ
                                       404 {"reason":"no_draft"} — черновика ещё нет
                                       ставит outbox reg_resume_upload {telegram_id,file_id,filename}

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

from miniapp.routers import (
    admin_tasks,
    applications,
    coins,
    coins_admin,
    faq,
    files,
    form,
    hub,
    page,
    profile,
    questions,
    review,
    settings,
    stats,
    submissions,
    tasks,
)

ALL_ROUTERS = [
    page.router,
    tasks.router,
    profile.router,
    coins.router,
    hub.router,
    submissions.router,
    files.router,
    review.router,
    stats.router,
    admin_tasks.router,
    coins_admin.router,
    settings.router,
    form.router,
    applications.router,
    questions.router,
    faq.router,
]

__all__ = ["ALL_ROUTERS"]
