---
id: 260709-mog
slug: customer-fixes-departments-payment-reminders
date: 2026-07-09
status: complete
---

# Summary 260709-mog: Customer fixes — departments + payment-reminder wiring

Правки по запросу заказчика (переслано через leaf). 4 функциональных пункта + расширение департаментов.

## Changed

- **`keyboards/builders.py`** (`get_department_kb`): список департаментов расширен до
  `OGV, OGT, MKT, F&L, BD, LCP, EwA` (+ «Другое»). Латиница — по решению заказчика.

- **`handlers/payment.py`**:
  - Новый `async _schedule_deadline_reminders(telegram_id)` — планирует T-3/T-1 напоминания
    об оплате (читает `payment_deadline`, идемпотентно через `replace_existing`).
  - Вызов добавлен в `_show_payment_details` (после раннего return бесплатного пути) и в
    `process_pay_later` — **т.е. в момент, когда пользователь становится должником (`not_paid`)**.
  - Удалён мёртвый инлайн-блок планирования из `_finalize_receipt` (там статус уже
    `receipt_sent`, и `send_payment_reminder` гасил напоминания гвардом → забывчивые пинга
    не получали). Кнопку «Оплачу позже» НЕ трогали — это единственный видимый выход.

- **`services/scheduler.py`** (`sweep_payment_overdue`): до `UPDATE not_paid→overdue` теперь
  SELECT затрагиваемых `telegram_id`, после commit — один финальный пинг каждому через
  `_safe_send` (текст из `payment_overdue_text` + дефолт). Смена статуса → повторно не спамит.

- **`handlers/admin.py`**: в список настроек после `payment_deadline` добавлены два поля:
  `payment_reminder_text` и `payment_overdue_text` — админ может править тексты пингов.

## Root cause (главное)

Заказчик жаловался «при оплате позже люди забывают». Механизм напоминаний существовал, но
планировался только при загрузке чека (`_finalize_receipt`) — когда пользователь УЖЕ заплатил
и гвард `send_payment_reminder` подавлял отправку. Для тех, кто отложил (`not_paid`), напоминания
физически не ставились. Перенос планирования на вход в оплату закрывает именно этот кейс.
Кнопку «Оплачу позже» решили НЕ удалять — без неё пропадает видимый выход из окна оплаты (UX-тупик),
а «оплату в момент» обеспечивают дедлайн (`payment_deadline` = +2 дня, настройка админа) + напоминания.

## Verification

- `pytest tests/test_payment_lc_requisites.py tests/test_reminders_phase2.py tests/test_scheduler_helpers_phase3.py tests/test_registration_phase4.py -q` → **31 passed**.
- Импорт всех тронутых модулей OK; `get_department_kb()` отдаёт 7 департаментов + «Другое».

## Follow-ups

- Заказчику/менеджеру: выставить `payment_deadline` (формат `ДД.ММ.ГГГГ ЧЧ:ММ`) — без него
  ни T-3/T-1, ни overdue-sweep не срабатывают (код не хардкодит «+2 дня»).
