# Requirements: AIESEC Event Bot

**Defined:** 2026-06-25
**Core Value:** Менеджер DXP может полностью провести регистрацию делегатов через бота — от заявки до одобрения — без ручного учёта в таблицах и без перезапуска кода между событиями.

## v1 Requirements

Scope этого milestone: Фаза 1 (quick wins) + ядро (approval flow) + Фаза 2 (до запуска) + универсальные модули. Геймификация отложена.

### Foundation (безопасные миграции БД)

- [ ] **DB-01**: `add_user()` использует `ON CONFLICT(telegram_id) DO UPDATE SET` вместо `INSERT OR REPLACE`, чтобы повторная регистрация не уничтожала новые поля (`status` и др.)
- [ ] **DB-02**: Новые колонки добавляются через существующий `_ensure_column()` без потери данных существующих ~590 пользователей
- [ ] **DB-03**: Поле `status` добавляется с `DEFAULT 'approved'`, чтобы существующие пользователи не теряли доступ

### Quick Wins (Фаза 1)

- [ ] **QW-01**: После заполнения формы пользователь видит сводку ответов с выбором «всё верно / изменить» перед финализацией
- [ ] **QW-02**: Бот проверяет подписку пользователя на канал через `getChatMember` и закрывает доступ до подписки
- [ ] **QW-03**: При регистрации пользователь может прикрепить резюме (PDF/DOCX); `file_id` сохраняется в БД, менеджер открывает файл (без OCR)

### Coins (фундамент геймификации)

- [ ] **COIN-01**: Таблица `coins` — append-only ledger (user_id, delta, reason, changed_by, timestamp); баланс = `SUM(delta)`, без перезаписи
- [ ] **COIN-02**: Админ командой `/coins @username +N` / `-N` начисляет или списывает монеты, каждое изменение логируется
- [ ] **COIN-03**: Команда `/рейтинг` показывает топ-10 по балансу и место текущего пользователя

### Approval Flow (ядро milestone)

- [ ] **APP-01**: `finalize_registration()` разделён на `submit_application()` (status=pending) и `approve_user()` (выдаёт complete_text + бонус + меню)
- [ ] **APP-02**: При ручной модерации после подачи заявки менеджер получает уведомление
- [ ] **APP-03**: `ensure_registered()` пускает только пользователей со `status=approved`
- [ ] **APP-04**: В админке есть раздел «Заявки» — пагинированные карточки (тиндер) с действиями одобрить/отклонить/пропустить
- [ ] **APP-05**: Действие «Одобрить все (N)» массово одобряет очередь заявок
- [ ] **APP-06**: Одобрение атомарно — два менеджера не могут одобрить одну заявку дважды (guard по rowcount)
- [ ] **APP-07**: Раздельный toggle модерации для короткой и полной форм (`short_approval` / `full_approval`)
- [ ] **APP-08**: Периодическая напоминалка менеджеру о количестве необработанных заявок

### Communications (Фаза 2)

- [ ] **COMM-01**: Рассылка по фильтру — inline-меню выбора по полям БД (город, ВУЗ, статус, источник)
- [ ] **COMM-02**: Фильтр по дате регистрации (зарегистрировавшиеся после/до даты)
- [ ] **COMM-03**: Комбинация фильтров через AND
- [ ] **COMM-04**: Рассылка устойчива к Telegram flood-лимитам (обработка 429, ретраи, учёт заблокировавших)

### Scheduling (Фаза 2)

- [ ] **SCHED-01**: Отложенные рассылки — запланировать на дату/время; задание переживает перезапуск бота (персистентное хранилище)
- [ ] **SCHED-02**: Таблица `reg_started` пишется при старте регистрации и удаляется при завершении (трекинг дропаутов независимо от MemoryStorage FSM)
- [ ] **SCHED-03**: Авто-напоминание о дорегистрации тем, кто начал flow но не завершил

### Verification (Фаза 2)

- [ ] **VERIF-01**: При `/start` бот проверяет TG-username по Google-таблице отобранных; если нет — показывает «отбор не пройден» + ссылку на регистрацию
- [ ] **VERIF-02**: Обработка пользователей без username (prompt установить username либо ручной allowlist по telegram_id)

### Event Modularity (универсальность)

- [ ] **MOD-01**: В админке настройка типа мероприятия (Форум / Конференция / Кастом) с toggle модулей (оплата, согласия, авто-напоминания)
- [ ] **MOD-02**: REG_FLOW поддерживает тип вопроса `date` с валидацией ДД.ММ.ГГГГ
- [ ] **MOD-03**: REG_FLOW поддерживает тип вопроса `consent` (авто-«Принимаю»)

### Consent Module

- [ ] **CONS-01**: Настраиваемый список согласий (обработка данных, политика, фото/видео, кастомные) с toggle каждого
- [ ] **CONS-02**: Согласия показываются в конце регистрации перед финализацией

### Payment Module (для конференций RusCo)

- [ ] **PAY-01**: Настройки модуля в админке — сумма взноса + damage fee, реквизиты (банк/карта/ФИО), дедлайн оплаты, шкала штрафов за отмену (даты + суммы)
- [ ] **PAY-02**: Flow оплаты — после реги бот спрашивает дату оплаты, показывает сумму/реквизиты/штрафы
- [ ] **PAY-03**: Пользователь отправляет чек (PDF); чек уходит менеджеру на проверку
- [ ] **PAY-04**: Менеджер проверяет чеки в тиндер-формате (подтвердить/отклонить/следующий)
- [ ] **PAY-05**: Статусы оплаты — not_paid / receipt_sent / paid / overdue
- [ ] **PAY-06**: Авто-напоминания об оплате за 3 дня и за 1 день до дедлайна

## v2 Requirements

Признано, но отложено — не в текущем роадмапе.

### Gamification

- **GAME-01**: Система заданий по трекам RESULT / INTERACTIVE / NETWORK
- **GAME-02**: Проверка выполнения заданий менеджером
- **GAME-03**: Начисление монет за задания (фундамент уже в COIN-01..03)
- **GAME-04**: Лимит активных заданий в неделю (антидроп)

### Roles

- **ROLE-01**: Три роли — Делегат / Менеджер / Администратор с разграничением доступа
- **ROLE-02**: Управление менеджерами через админку

### Participant Tracks (Фаза 5 — party-делегаты)

- **TRACK-01**: Трек участия `participant_type` (`full` / `party_overnight` / `party_noovernight`) — миграция с `DEFAULT 'full'`, запись в БД в момент старта регистрации (переживает рестарт и повторный `/start`)
- **TRACK-02**: Набор вопросов настраивается отдельно для каждого трека — оверрайды `reg_q_<step>__party` и `reg_prompt_<step>__party` с fallback на глобальную настройку; переключатель трека в админском экране вопросов
- **TRACK-03**: Вход в party-трек по deep-link `?start=party_over` / `?start=party_noover`; опциональный вопрос-развилка за тумблером `party_fork_question` (default `off`), не ломая разбор referrer_id и `src_*`
- **TRACK-04**: Отдельный тумблер модерации `party_approval`, независимый от `full_approval` / `short_approval`
- **TRACK-05**: Тарифы `payment_options` разделены по трекам — делегат видит только релевантные его треку варианты оплаты
- **TRACK-06**: Трек виден в карточке заявки у манагера, отдельной колонкой в Google Sheet и как поле фильтра рассылок

## Out of Scope

| Feature | Reason |
|---------|--------|
| Геймификация сейчас | Нет чёткого ТЗ на механику, 8 блоков открытых вопросов к создателям |
| Розыгрыши / бинго | Непонятный scope |
| OCR для резюме | Overthink — менеджер сам открывает файл |
| Админка в Google-таблице | Двусторонняя синхра ложила прошлый бот |
| Отдельные боты для форумов/конференций | Решено: один модульный бот |
| Замена core-стека (aiogram/SQLite/polling) | Brownfield — встраиваться в существующее, не переписывать |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| DB-01 | Phase 1 | Pending |
| DB-02 | Phase 1 | Pending |
| DB-03 | Phase 1 | Pending |
| QW-01 | Phase 1 | Pending |
| QW-02 | Phase 1 | Pending |
| QW-03 | Phase 1 | Pending |
| COIN-01 | Phase 1 | Pending |
| COIN-02 | Phase 1 | Pending |
| COIN-03 | Phase 1 | Pending |
| APP-01 | Phase 2 | Pending |
| APP-02 | Phase 2 | Pending |
| APP-03 | Phase 2 | Pending |
| APP-04 | Phase 2 | Pending |
| APP-05 | Phase 2 | Pending |
| APP-06 | Phase 2 | Pending |
| APP-07 | Phase 2 | Pending |
| APP-08 | Phase 2 | Pending |
| COMM-01 | Phase 3 | Pending |
| COMM-02 | Phase 3 | Pending |
| COMM-03 | Phase 3 | Pending |
| COMM-04 | Phase 3 | Pending |
| SCHED-01 | Phase 3 | Pending |
| SCHED-02 | Phase 1 | Pending |
| SCHED-03 | Phase 3 | Pending |
| VERIF-01 | Phase 3 | Pending |
| VERIF-02 | Phase 3 | Pending |
| MOD-01 | Phase 4 | Pending |
| MOD-02 | Phase 4 | Pending |
| MOD-03 | Phase 4 | Pending |
| CONS-01 | Phase 4 | Pending |
| CONS-02 | Phase 4 | Pending |
| PAY-01 | Phase 4 | Pending |
| PAY-02 | Phase 4 | Pending |
| PAY-03 | Phase 4 | Pending |
| PAY-04 | Phase 4 | Pending |
| PAY-05 | Phase 4 | Pending |
| PAY-06 | Phase 4 | Pending |
| TRACK-01 | Phase 5 | Complete |
| TRACK-02 | Phase 5 | Complete |
| TRACK-03 | Phase 5 | Complete |
| TRACK-04 | Phase 5 | Pending |
| TRACK-05 | Phase 5 | Pending |
| TRACK-06 | Phase 5 | Pending |

**Coverage:**
- v1 requirements: 37 total (REQUIREMENTS.md originally listed 36; actual count from IDs is 37 — PAY-06 is the 37th)
- Phase 5 addition: +6 (TRACK-01..06) → 43 total
- Mapped to phases: 43/43
- Unmapped: 0

---
*Requirements defined: 2026-06-25*
*Last updated: 2026-07-20 — TRACK-01..06 added for Phase 5 (participant tracks / party delegates)*
