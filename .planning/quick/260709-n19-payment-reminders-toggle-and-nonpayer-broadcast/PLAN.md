---
id: 260709-n19
slug: payment-reminders-toggle-and-nonpayer-broadcast
date: 2026-07-09
status: complete
---

# Quick 260709-n19: Payment-reminders toggle + non-payer broadcast segment

Продолжение 260709-mog. Два запроса заказчика/leaf:
1. Тоггл автонапоминаний об оплате (вкл/выкл из админки).
2. Возможность рассылки отдельно для неоплативших.

## Tasks

### T1 — Тоггл `payment_reminders_enabled` (default on)
- `services/scheduler.py`: гейт на момент ОТПРАВКИ (не планирования), чтобы живой тоггл
  действовал и на уже запланированные job'ы.
  - `send_payment_reminder`: early return если тоггл off.
  - `sweep_payment_overdue`: статус `not_paid→overdue` флипается ВСЕГДА (нужен сегменту
    рассылки), но финальный пинг шлётся только при тоггле on.
- `handlers/admin.py`: строка статуса в `render_settings_text`, кнопка в
  `build_settings_keyboard`, хендлер `toggle_payment_reminders` (через `_toggle_value_setting`,
  default "on" — сохраняет прежнее поведение).
- done: админ вкл/выкл автонапоминания; при off бот их не шлёт, статусы всё равно ведутся.

### T2 — Рассылка для неоплативших (фильтр по `payment_status`)
- `database/db.py`: `payment_status` добавлен в `_FILTER_COLUMNS` (значения биндятся как `?`,
  инъекции невозможны — не-whitelist поля молча отбрасываются).
- `handlers/admin.py`:
  - `_FILTER_FIELD_LABELS["payment_status"]="Оплата"` + `_PAYMENT_STATUS_LABELS`.
  - Кнопка «💰 Оплата» в `_filter_menu_kb`.
  - Хендлеры `filter_pick_payment` (пикер значений) + `filter_value_payment`.
    callback-data через `:`-разделитель (`filter_v_pay:not_paid`), т.к. `_`-split ломал бы
    «not_paid».
  - `_filter_summary`: человекочитаемый ярлык значения оплаты.
- done: админ строит сегмент «Оплата = Не оплатил/Просрочил/…», видит count, шлёт сейчас
  или планирует (существующий планировщик рассылок).

## Verification
- `pytest tests/test_payment*.py tests/test_reminders*.py tests/test_scheduler_helpers_phase3.py tests/test_registration_phase4.py -q` → 31 passed.
- `_build_filter_clause` с `payment_status` → параметризованный WHERE; плохое поле отброшено.
- Импорт всех тронутых модулей OK.

## Notes
- Тоггл гейтит только ОТПРАВКУ пингов; флип статуса overdue сохранён — он питает сегмент рассылки.
- Сегмент `not_paid` включает и legacy-юзеров (бэкфилл), кто не входил в оплату — комбинировать
  с `Статус=approved` при таргете платного события.
