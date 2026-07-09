---
id: 260709-mog
slug: customer-fixes-departments-payment-reminders
date: 2026-07-09
status: in-progress
---

# Quick 260709-mog: Customer fixes — departments + payment-reminder wiring

Правки по запросу заказчика (переслано через leaf). Точечные, без переписывания архитектуры.

## Tasks

### T1 — Департаменты (keyboards/builders.py, `get_department_kb`)
Расширить список кнопок до `["OGV", "OGT", "MKT", "F&L", "BD", "LCP", "EwA"]`.
Сохранить кнопку «Другое» и `adjust(2)`.
- done: 7 департаментов + «Другое» на клавиатуре.

### T2 — Перенос T-3/T-1 напоминаний на вход в оплату (handlers/payment.py)
**Баг:** напоминания планируются только в `_finalize_receipt` (после чека, статус `receipt_sent`),
где `send_payment_reminder` их гасит гвардом. Те, кто нажал «Оплачу позже» (`not_paid`),
пингов НЕ получают.
- Вынести `async _schedule_deadline_reminders(telegram_id)`: читает `payment_deadline`,
  планирует `minus3d`/`minus1d` (в `scheduler.schedule_payment_reminder` уже `replace_existing`
  → идемпотентно).
- Вызвать в `_show_payment_details` (после раннего return бесплатного пути — только для тех,
  кто реально должен платить) и в `process_pay_later`.
- Удалить мёртвый инлайн-блок планирования из `_finalize_receipt`.
- done: должники (`not_paid`) получают T-3/T-1; заплатившие — нет (гвард).

### T3 — Кнопка «Оплачу позже» — НЕ трогать
`_PAY_LATER_BTN` остаётся: единственный видимый выход из окна оплаты (иначе UX-тупик).
- done: no-op, зафиксировано решение.

### T4 — Финальный пинг на overdue (services/scheduler.py, `sweep_payment_overdue`)
Сейчас молча метит `not_paid`→`overdue`. Доработать:
- До UPDATE — SELECT `telegram_id` затрагиваемых.
- После commit — рассылка через `_safe_send`, текст `get_setting("payment_overdue_text")` + дефолт.
- Статус → `overdue`, повторно не заспамит.
- done: просроченные получают один финальный пинг.

### T5 — Конфиг-поля текстов пингов (handlers/admin.py)
После `payment_deadline` (стр.~359) добавить в список настроек:
- `payment_reminder_text` (сейчас только код-дефолт в scheduler)
- `payment_overdue_text`
- done: админ может править оба текста из панели настроек.

## Verification
- `pytest tests/test_payment*.py tests/test_reminders*.py tests/test_registration_phase4.py -q`
- Импорт всех тронутых модулей без ошибок.

## Notes
- Дедлайн «+2 дня» — настройка админа (`payment_deadline`), код не хардкодит.
- Гвард `send_payment_reminder` корректный (`paid`/`receipt_sent`/`None` пропускает).
- `payment_status` дефолтит `'not_paid'` (db.py:129) → свежеодобренные проходят гвард.
