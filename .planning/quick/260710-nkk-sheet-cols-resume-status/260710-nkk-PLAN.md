---
quick_id: 260710-nkk
slug: sheet-cols-resume-status
date: 2026-07-10
mode: quick
---

# Quick Task 260710-nkk — Правки таблицы/заявок (фидбек Тани, YL'26)

## Boundary

Пять правок по фидбеку Тани. Пункт 3 (резюме в облако + ссылка) ОТЛОЖЕН — Андрей зальёт на своё облако сам. Пункт 6 уже сделан гейтингом `active_sheet_headers()`.

## Context / constraints

- `SHEET_COLUMNS` (handlers/registration.py) — исторический порядок, шапка пишется 1 раз, `_ensure_header_sync` переписывает row 1 in place, но НЕ двигает данные существующих строк. Значит после reorder старые строки разъедутся → нужен full-rebuild path.
- DB уже имеет колонки `status` ('pending'/'approved'/'rejected'), `resume_text`, `resume_file_id`.
- `_sheet_value_map(d)` уже работает и на FSM-data, и на db-row dict (admin sync).
- fail-soft: любой сбой Sheets не блокирует бота.

## Tasks

### T1 — Столбцы в порядке анкеты + столбцы «Статус» и «Резюме (текст)»
Files: handlers/registration.py
- Пересобрать `SHEET_COLUMNS`: системные (ID, Username, Дата регистрации, **Статус**, ФИО, Детали) → затем колонки в порядке `REG_FLOW`, «Резюме (текст)» на позиции шага resume.
- `Статус` gate=None, value = маппинг status→(Новая/Одобрена/Отклонена).
- `Резюме (текст)` gate=`reg_q_resume`, value = resume_text.
- В finalize: посчитать `status` и положить `data["status"]` ДО `active_sheet_row`/append.
- verify: `SHEET_HEADERS` порядок = форма; pytest tests зелёные.

### T2 — Резюме обязательно (убрать «Пропустить»)
Files: handlers/registration.py
- Resume-шаг: убрать `get_skip_kb()` (без клавы Пропустить), поправить текст промпта.
- `process_resume_text`: убрать ветку `== "Пропустить"`.
- verify: нет reply-kb с Пропустить на шаге; текст/файл обязателен.

### T3 — Текст-резюме в заявке админа
Files: handlers/admin.py
- `_render_application_card`: показать текст резюме (обрезка) если `resume_text`; строка «Резюме: текстом/файлом/нет».
- `_appr_card_kb`: кнопка «📎 Резюме» когда есть file_id ИЛИ resume_text.
- `_show_current_card`: `has_resume = file_id or resume_text`.
- verify: карточка с текст-резюме показывает текст + кнопку.

### T4 — Автосинк статуса в таблицу при аппрув/реджект
Files: services/sheets.py, handlers/admin.py
- `update_status_in_sheet(telegram_id, label)` — найти row по col1==id, найти col «Статус» по row1, update_cell. fail-soft.
- Вызвать: appr_approve → «Одобрена»; appr_reject_reason → «Отклонена»; mass approve (appr_all) → фоновый цикл по flipped ids.
- verify: после аппрува ячейка Статус меняется (лог/ручная проверка).

### T5 — Full-rebuild таблицы + выпадашка/цвета на Статус
Files: services/sheets.py, handlers/admin.py, keyboards/builders.py
- `rebuild_main_sheet(headers, rows)` — clear + header + все строки в новом порядке. fail-soft.
- `apply_status_formatting()` — data validation ONE_OF_LIST (Новая/Одобрена/Отклонена) + 3 conditional format правила (жёлтый/зелёный/красный) на колонку Статус, via spreadsheet.batch_update.
- Новая admin-кнопка «♻️ Пересобрать таблицу» → rebuild all users (active order, со статусом) + apply formatting.
- verify: кнопка перезаписывает таблицу выровненно, есть выпадашка+цвета.

### T6 — Тесты + докстроки
Files: tests/
- Обновить/добавить тесты порядка колонок и наличия Статус/Резюме(текст).

## Must-haves
- Колонки в порядке анкеты; есть «Статус» и «Резюме (текст)».
- Резюме нельзя пропустить.
- Текст-резюме виден в заявке.
- Аппрув/реджект пишет статус в таблицу.
- Есть кнопка пересборки + выпадашка/цвета на Статус.
