# Backlog

Ideas captured outside the active milestone. Promote via `/gsd-review-backlog`.

---

## 🧭 РЕШЕНИЕ ПО СЕКВЕНСУ: после-SumMeet milestone (2026-07-21)

**Контекст:** после форума SumMeet'26 (31 июля – 2 авг) — перестроить бота на
универсальную не-ломающуюся структуру и прикрутить UI вместо текстовой стены.
Проект — config-driven движок с накопительной (accidental) сложностью: ширина ×
конфигурируемость + brownfield на ~590 живых юзерах + нет типобезопасности +
связанность через 4+ файла. За один заход не пишется. Отдельный milestone.

**Принятый порядок (ПОЧЕМУ — налог на связанность):**

Строить фичи на текущей хардкод-структуре = добавить оси (город, coin_rules) в
нерефакторенный код, потом ретрофитить их в реестр = двойная работа. Поэтому
СТРУКТУРА первой, UI — её побочный продукт, фичи дешевеют после неё.

1. **`SETTINGS_SCHEMA`-реестр** — «универсальная не-ломающаяся структура».
   `key → {type, group, label, default, options}`. Мигрировать ~70 настроек
   ИНКРЕМЕНТАЛЬНО (группа за группой; старый и новый UI сосуществуют — НЕ
   big-rewrite, который застревает). Фундамент всего остального.
   Related: [[admin-config-backlog]], `REG_DEFAULTS` = половина реестра уже есть.

2. **Inline-UI из реестра** — экран настроек генерится из реестра, а не хардкодится.
   Убивает «стену настроек» (см. секцию «Админ-UX» ниже). Хостинг НЕ нужен.
   Падает почти даром, как только есть реестр. Это «UI вместо текста» на ближний
   срок.

3. **Города (мультигород)** — добавить как ось реестра (`__city_<slug>` namespace,
   как `__party`) + НОВЫЙ слой скоупинга админов (admin→города права). Скоупинг —
   реальная тяжёлая работа, которой в коде нет; namespace — дёшево. См. секцию
   «Мультигородность» ниже.

4. **Геймификация** — ТОЛЬКО после ответов на 8 блоков вопросов креаторам
   (блокирована апстримом, лидировать не может). Потом = таблица `coin_rules`
   (событие→delta, капы, вкл) + записи реестра. Фундамент (`coins` леджер +
   рефералка) уже есть. Related: [[gsd-milestone-scope]], [[admin-config-backlog]].

5. **Telegram Web App** — последним. Нужен хостинг HTTPS + read/write API +
   `initData` auth. Читает ТОТ ЖЕ реестр — морду не писать дважды. UX-нич поверх,
   не замена inline.

**Одной строкой:** реестр → inline-UI → города → (гейма когда разблокируется) →
Web App. Не строить фичи на старой структуре — иначе построить их дважды.

**Поправки к изначальному плану пользователя:**
- Гейма НЕ первой — заблокирована вопросами к креаторам, не кодом.
- «UI» — два зверя: inline-из-реестра (шаг 2, дёшево/скоро) и Web App (шаг 5,
  хостинг). Inline снимает ~80% боли без инфраструктуры.
- Города несут скрытую тяжесть = права админов, не namespace.

---

## Админ-UX: «стена настроек» нечитаема (near-term)

**Captured:** 2026-07-21 (со слов Алексея: «выглядит пиздец, даже как разрабу сложно
что-то разглядеть»)

**Проблема:** экран «⚙️ Настройки форума» = ~40 полей одним плоским сообщением.
Конкретные болячки (по реальному скрину SumMeet'26):
- Нет визуальной группировки — тумблеры, тексты, списки, файлы вперемешку.
- «не указано» повторяется ~15 раз — чистый шум, забивает сигнал.
- Длинные значения обрезаны посреди слова («💬 Приветствие: <b>💙 Привет… »,
  «💳 Реквизиты по ЛК: SPUEF | Сбербанк…»).
- Многострочные значения (списки согласий, штрафы, реквизиты) ломают скан глазом.
- Нет иерархии: «Дата» и «Вкладка Google-таблицы (Party)» имеют одинаковый вес.

**Два пути (см. [[admin-config-backlog]] в памяти проекта):**

1. **Быстрый выигрыш (без реестра, low-risk) — сделать сейчас, если горит:**
   - Разбить один экран на под-экраны кнопками по группам: «Событие/Медиа»,
     «Регистрация», «Оплата», «Party», «Согласия». Не стена — навигация.
   - Незаданные поля («не указано») сворачивать в отдельную секцию «не настроено»
     или прятать за кнопку, чтобы не шумели в основном виде.
   - Длинные тексты НЕ дампить инлайн — показывать «✏️ задано» / «— не задано»,
     полное значение по тапу (edit-экран). Убирает обрезки посреди слова.
   - Инфраструктура частично есть: `REG_CATEGORIES` уже группирует вопросы —
     тот же приём применить к настройкам.

2. **Правильный фикс (keystone) — `SETTINGS_SCHEMA`-реестр:**
   - Один реестр `key → {type: toggle/text/enum/list/date/file_id, group, label,
     default, options}`. Экран настроек ГЕНЕРИТСЯ из него (группы, порядок, рендер
     по типу), а не хардкодится в хендлерах.
   - Тот же реестр потом читает Telegram Web App (путь 3 ниже) — писать морду
     дважды не надо.
   - `REG_DEFAULTS` = половина реестра (только дефолты) — часть работы уже сделана.

3. **Web App-морда** — только ПОСЛЕ реестра. Нужен хостинг HTTPS + read/write API
   + `initData` auth. UX-нич поверх (2), не замена.

**Рекомендация:** если UX жмёт до запуска SumMeet'26 (даты 31 июля – 2 авг) —
сделать путь 1 как quick-task (сворачивание «не указано» + под-экраны по группам
дают ~80% читаемости за малый риск). Реестр (2) — отдельная плановая работа, когда
будет окно; он же разблокирует Web App и снимает боль навсегда.

**✅ Путь 1 ВЫПОЛНЕН (2026-07-24, quick `260724-c0x`, commits `b3cf8fd`+`8882eb6`):**
экран разбит на 5 под-экранов по группам (`SETTINGS_GROUPS`, `settings_group:{token}`),
инлайн-дамп ~40 полей убран, поля показывают флаг задано/не-задано, незаданные свёрнуты.
~80% читаемости получено за малый риск, как и планировалось. Путь 2 (реестр) и путь 3
(Web App) — по-прежнему открыты, post-SumMeet plan-работа (см. секцию «РЕШЕНИЕ ПО СЕКВЕНСУ»).

Related: [[admin-config-backlog]] (память), keystone `SETTINGS_SCHEMA`.

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

**✅ Закрыто в pre-SumMeet improve-loop (2026-07-24, см. `.planning/IMPROVE-LOG.md`):**
- ~~**WR-02**~~ — DONE (quick `260724-cfn`, commits `f61518d`+`2b29037`). `approve_text__party`
  editor в группе Party + track-свитчер full⇄party на экране «Тексты вопросов»
  (`reg_prompt_edit:{step}:party` → пишет `reg_prompt_{step}__party`). «Всё через бота» восстановлено.
- ~~**WR-04**~~ — оказался УЖЕ закрыт до loop (commit `33e440f`, MEDIUM-01, plan 05-06/block6):
  `_refresh_party_sheet_header()` вызывается из `toggle_party_question` (`admin.py:2303`) и
  party-ветки `preset_confirm` (`admin.py:2421`). Запись была устаревшей.
- ~~**WR-05**~~ — DONE (quick `260724-cfn`, commit `f61518d`). Help-текст `payment_options`
  обновлён под track-синтаксис `label|price|track1,track2` (сверено с `_parse_options`).

**Открыто (низкий приоритет):**
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
