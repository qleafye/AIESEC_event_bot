---
id: 260709-n19
slug: payment-reminders-toggle-and-nonpayer-broadcast
date: 2026-07-09
status: complete
---

# Summary 260709-n19: Payment-reminders toggle + non-payer broadcast segment

## Changed

- **`services/scheduler.py`**: новый гейт `payment_reminders_enabled` (default `on`) на момент
  ОТПРАВКИ. `send_payment_reminder` и финальный overdue-пинг молчат при `off` — даже для job'ов,
  уже лежащих в jobstore (живой тоггл). Флип `not_paid→overdue` в `sweep_payment_overdue`
  выполняется всегда (нужен сегменту рассылки).
- **`handlers/admin.py`**:
  - Тоггл «⏰ Автонапоминания об оплате» — строка в `render_settings_text`, кнопка в
    `build_settings_keyboard`, хендлер `toggle_payment_reminders`.
  - Фильтр рассылки по оплате: `_FILTER_FIELD_LABELS`/`_PAYMENT_STATUS_LABELS`, кнопка
    «💰 Оплата» в `_filter_menu_kb`, хендлеры `filter_pick_payment` + `filter_value_payment`,
    ярлык значения в `_filter_summary`.
- **`database/db.py`**: `payment_status` в `_FILTER_COLUMNS` (параметризованно, инъекции нет).

## Result

- Автонапоминания об оплате вкл/выкл одной кнопкой в админке. Off → бот не шлёт пинги, но
  статусы оплаты продолжают вестись.
- Рассылку можно таргетить на неоплативших: «💰 Оплата → Не оплатил / Просрочил / Чек на
  проверке / Оплатил», с превью count, отправкой сейчас или планированием (существующий
  планировщик рассылок и AND-фильтры).

## Verification

- `pytest ... -q` → **31 passed**.
- `_build_filter_clause([...payment_status...])` → `WHERE status = ? AND payment_status = ?`,
  params `['approved','not_paid']`; не-whitelist поле отброшено.
- Импорт всех тронутых модулей OK.

## Notes

- Сегмент `Оплата = Не оплатил` включает legacy-юзеров (бэкфилл `not_paid`), не входивших в
  оплату — при платном событии комбинировать с `Статус = approved`.
