---
id: 260721-msh
status: complete
date: 2026-07-21
commit: dd1aca9
---

# Quick 260721-msh — вопрос «Аламни/айсекер» ✅

## Что сделано

Новый вопрос регистрации `alumni_status` — «Ты аламни или айсекер?» с reply-клавиатурой
`Аламни / Айсекер / Ни то, ни другое`. Задаётся сразу после телефона.

Точки подключения (эталон — `bed_sharing`):

| Файл | Что |
|------|-----|
| `handlers/registration.py` | `REG_FLOW` (после `phone`), `REG_DEFAULTS` (`off`), `REG_LABELS` (🎓 Аламни/айсекер), `REG_CATEGORIES` → «🎤 Конфа», `REG_PRESETS["party"]["on"]`, ветка `_ask_step`, хендлер `process_alumni_status` → `_store_choice`, колонка в `SHEET_COLUMNS` (после «Телефон») и `PARTY_SHEET_COLUMNS` (после «ВК»), строка в `_build_summary` |
| `handlers/states.py` | `Registration.alumni_status` |
| `handlers/admin.py` | строка `🏷 <значение>` в карточке заявки (`_render_application_card`) |
| `database/db.py` | `_ensure_column users.alumni_status TEXT`, INSERT + ON CONFLICT + значение, CSV-label «Аламни/айсекер» |
| `tests/test_alumni_status.py` | 10 тестов: дефолт OFF, включение, оба party-сабтрека, party-пресет, `_prompt` + `__party`-override, колонки в `SHEET_HEADERS`/`active_sheet_headers`/`party_sheet_headers`, значение в строке, round-trip `add_user`→`get_user` |

## Отклонения от плана

- **Инлайн-исполнение вместо subagent-пайплайна.** Задача была полностью
  специфицирована до входа в workflow (точный список точек подключения получен
  при аудите вопросов); планировщик/исполнитель как подагенты только
  переоткрывали бы уже известное. Гарантии GSD сохранены: PLAN.md, атомарный
  коммит, SUMMARY.md, строка в STATE.md.
- **Карточка модерации (п.14 брифа) — сделано.** `_render_application_card`
  печатает курируемый набор полей (не все ответы), `english_level` там нет;
  `alumni_status` добавлен явно, т.к. для party-модерации он значимый.
- **Правка двух чужих тестов.** `test_party_preset_shape` (6→7) и
  `test_reg_flow_entry_count_unchanged_from_phase5_start` (42→43) — снапшот-гварды
  Phase 5, которые морозили счётчики. Изменение легитимное, комментарии обновлены.

## Тесты

`pytest -q` → **293 passed** (было 283 + 10 новых).

## ⚠️ Что нужно сделать в проде

1. Колонка «Аламни/айсекер» вставлена в **середину** `SHEET_COLUMNS` → шапка
   Google-таблицы сдвигается. После деплоя нажать админ-кнопку
   **«♻️ Пересобрать таблицу»**, иначе старые строки разъедутся относительно шапки.
2. Вопрос по умолчанию **ВЫКЛ**. Включить: Админка → «📋 Вопросы регистрации» →
   (для гостей вечеринки — режим **Party**) → 🎓 Аламни/айсекер → ✅.
   Либо применить пресет **🎉 Party** — он теперь включает этот вопрос.
