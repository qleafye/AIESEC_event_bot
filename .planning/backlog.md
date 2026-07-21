# Backlog

Ideas captured outside the active milestone. Promote via `/gsd-review-backlog`.

---

## Мультигородность: сплит регистраций/админов/таблиц по городам

**Captured:** 2026-07-21

**Запрос:** Форум планируется одновременно в разных городах. Хотят обслуживать
все города из ОДНОГО бота (не поднимать по экземпляру на город), с разделением:
- **Таблицы регистраций по городам** — каждый город пишет в свою вкладку/документ.
- **Админы по городам** — админ города A видит/модерит только заявки города A.
- **Флоу регистрации** — делегат привязывается к городу (deep-link `?start=city_<slug>`
  или выбор города в начале), дальше вопросы/тарифы/тексты могут отличаться.
- **Админ-функции по городам** — очередь модерации, рассылки, статистика — все
  скоупятся по городу.

**Мнение (Алексей):** похоже на оверинженеринг — проще запустить разные экземпляры
бота по городам (изоляция бесплатно: свои ADMIN_IDS, свой Sheet, свой токен).
НО ребята хотят единый бот, чтобы всё взаимодействие делегата шло из одного места.

**Технические зацепки (переиспользуем Phase 5):**
- Паттерн трека уже есть — город = ещё одна ось скоупинга, как `participant_type`.
  Можно ввести колонку `city` (аналог `participant_type`), deep-link экстрактор
  (как `_extract_party_track`), city-scoped настройки через namespace-суффикс
  (как `__party` → `__city_<slug>`).
- Эксклюзивная маршрутизация в Sheets по городу — обобщение
  `append_to_named_sheet` из 05-06 (тот же документ, вкладка по городу; или
  разные документы — тогда `GOOGLE_SHEET_ID` становится per-city настройкой).
- Скоупинг админов — новый слой: карта `admin_id → [города]`, фильтр в
  `get_pending_users`/очереди/рассылках. Это самая крупная новая работа —
  сейчас ADMIN_IDS плоский список из .env, права не гранулярны.

**Ключевые развилки для обсуждения (перед планированием):**
1. **Один бот vs несколько экземпляров** — честно взвесить. Мультигород в одном
   боте = гранулярные права + скоупинг ВСЕХ админ-поверхностей (модерация,
   рассылки, статистика, экспорт) + риск утечки данных между городами. Несколько
   экземпляров = изоляция даром, но делегат в «нескольких городах» видит разные
   боты. Спросить: реально ли делегату нужно межгородское взаимодействие в одном
   боте, или это удобство для организаторов?
2. Город: одна вкладка на документ или отдельный документ на город?
3. Модель прав: город как атрибут админа (1 админ — N городов?) — где хранить,
   как назначать (ещё одна админ-поверхность).
4. Как делегат выбирает город: deep-link на город vs явный вопрос в начале vs
   и то и другое (как party: link → авто, без link → fork).
5. Пересечение с party-треком: город × трек = матрица настроек/тарифов/вкладок —
   не взорвётся ли конфиг (сейчас `__party` уже добавил namespace-ось).

**Оценка:** крупнее Phase 5. Права админов по городам — новый слой, которого в
коде нет. Если решат идти в один бот — это отдельный milestone, не quick-task.

Related: participant_type/deep-link/namespace-override паттерны из Phase 5.

---

## Phase 5 code-review deferrals (Fable 5 review, 2026-07-21)

Deferred findings from `.planning/phases/05-participant-tracks-party-delegates/05-REVIEW.md`.
CR-01 + WR-01 + WR-03 were fixed in-session; the below were consciously deferred.

- **WR-02** — `approve_text__party` and `reg_prompt_<step>__party` have no admin UI
  (`admin.py:2305-2343`, `SETTINGS_FIELDS`). Per-track wording (D-05) and per-track
  approval message (D-15) can currently only be set by writing `bot_settings` directly.
  Contradicts the "всё через бота" core value. Fix: add `approve_text__party` editor +
  a `track` switcher on the prompt-text screen, mirroring `reg_q_track_switch`.
- **WR-04** — party sheet header has no resync hook on `__party` toggle
  (`registration.py:1061-1094`, `admin.py:2145-2173`). Column-misalignment risk if an
  admin flips a `reg_q_*__party` override mid-event. Fix: call
  `ensure_named_sheet_header(tab, await party_sheet_headers())` from `toggle_party_question`
  and `preset_confirm`'s party branch.
- **WR-05** — `payment_options` admin help text (`admin.py:359`) not updated for the new
  `label|price|track1,track2` syntax. Admin has no in-bot way to discover track filtering.
- **IN-01** — broadcast filter «Трек» picker shows raw codes (`party_overnight`) instead
  of RU labels (`admin.py:1808-1827`). Matches pre-existing raw-picker pattern; low priority.
- **IN-02** — `mark_reg_started` COALESCE-preserve branch (`db.py:558-565`) unreachable in
  production (only live caller always passes a concrete track). Documentation-only note.
- **Pre-existing (out of Phase-5 scope)** — main sheet `active_sheet_row` does not apply
  `_csv_safe`, unlike the party sheet. CSV-injection parity gap worth a quick-task.

---

## Attendance check-in + post-event feedback survey

**Captured:** 2026-07-20 (during Phase 5 execution)

**Idea:** At the end of a forum the admin presses a button and a feedback survey
fires to attendees. Survey should reach only people who actually showed up — so
attendance has to be recorded first.

**Proposed mechanism:** QR code carrying a dedicated deep-link start payload
(e.g. `t.me/<bot>?start=checkin_<event>`). Guest scans on arrival, bot stamps
attendance for that user. Reuses the deep-link → payload → DB-stamp pattern
built in Phase 5 for `participant_type` (`?start=party_over`), so the plumbing
already exists.

**Rough shape:**
- `attended` / `checked_in_at` column on `users` (additive migration, existing
  `_ensure_column` pattern)
- Deep-link handler branch for the check-in payload; idempotent (re-scan is a
  no-op, not a duplicate)
- Admin screen: generate/show the check-in QR, see live check-in count
- Survey builder: admin-defined questions (likely reuses the existing
  configurable-question machinery rather than a new engine)
- Admin "send survey now" button → broadcast filtered to `attended = 1`
- Responses stored + synced to Sheets, same append pattern as registrations

**Open questions:**
- One QR for the whole event, or per-day / per-session QRs?
- Should the survey be reusable across events (template) or per-event config?
- Does a party-track guest get a different survey than a delegate?
- Anonymous responses, or tied to the user row?

**Depends on:** Phase 5 deep-link + `participant_type` foundation (in progress).
Survey filtering would want the broadcast-filter work from 05-03.
