"""Phase 13 (refac-split-god-files) Plan 07 — REFAC-03: module-size convention, CI-enforced.

The whole point of Phase 13 (13-01..13-06) was cutting `handlers/admin.py` (3067 lines) and
`handlers/registration.py` (2953 lines) down to a set of readable, single-concern seam files
sharing one `Router()` per aggregator. Splitting once is worthless if nothing stops the same
files from silently re-accreting handlers back to god-file size over the next dozen features.

This is that guard. See `docs/CONVENTIONS.md` ("Размер модуля: ориентир ~800 строк") for the full
rationale and the human-readable version of the table below.

Design (per 13-07-PLAN.md's <interfaces> note):
- The ~800-line number is a REVIEW ORIENTEER, not the CI gate itself — a small, honest overage
  (a screen that just needs a few more lines) should not break the build.
- The CI gate is a HARD ceiling per file: `DEFAULT_CEILING` (850) for any handlers/*.py file
  not named below, or an explicit per-file ceiling in `KNOWN_OVERAGES` for files that legitimately
  sit above that today. Every entry in `KNOWN_OVERAGES` carries a one-line reason — it is a named,
  reviewed exception, not a blanket loosening of the rule.
- Raising a ceiling is a deliberate act: edit `KNOWN_OVERAGES` (or add a new entry) IN THE SAME
  COMMIT as the growth that needs it, with an updated reason. Do not bump a neighboring file's
  ceiling "while you're in here" — each entry is owned by whoever grew that file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

HANDLERS_DIR = Path(__file__).resolve().parent.parent / "handlers"

# Soft guideline every handlers/*.py module SHOULD stay under (see docs/CONVENTIONS.md).
GUIDELINE = 800

# Hard CI ceiling for any handlers/*.py file NOT explicitly listed in KNOWN_OVERAGES below.
# Deliberately a little above GUIDELINE (per 13-07-PLAN.md's 850-900 note) so a small, honest
# overage doesn't break the build -- genuine regrowth toward god-file size still gets caught.
DEFAULT_CEILING = 850

# Named, reviewed exceptions: files that already sit above DEFAULT_CEILING as of 13-07 for a
# documented reason. Ceiling per file = current line count + ~5% slack, rounded. To raise one:
# edit the number + reason together, in the same commit as the growth that needs it.
KNOWN_OVERAGES: dict[str, tuple[int, str]] = {
    "registration.py": (
        2495,
        "19.09 (квик 08-sheets-dashboard, коммит 259890f): +2 строки — построители строк листа "
        "переведены с database.db._csv_safe на _sheet_safe, докстринги объясняют, почему "
        "нейтрализация формул на RAW-записи не нужна; потолок поднят до фактического размера. "
        "17.09 (живая проверка, находка «б»): +7 строк — заголовок карточки согласия "
        "(_ask_step, ветка consent:*) переводится через reg_i18n.tr_text с пустым tr_map "
        "(ТОЛЬКО ярус A, машинный перевод легального текста по-прежнему исключён); потолок 2491. "
        "17.09 (приёмка, нагрузка анкеты): +15 строк — _advance обёрнут снимком bot_settings "
        "(тело в _advance_impl), N+1 по настройкам на каждом ответе делегата; потолок 2485. "
        "17.09 (приёмка, перевод чата и одна реф-ссылка): +8 строк — перевод кнопок городов "
        "(reg_i18n.tr_kb) и единый разбор реф-ссылки обоих форматов через resolve_referrer; потолок 2470. "
        "+6 строк (17.09, приёмка): подсказка минимума вариантов мультивыбора в тексте вопроса "
        "чата (reg_engine.multi_requirement_hint) — расчёт в движке, здесь вызов; потолок 2455. "
        "+1 строка (16.09, сторож ввода настроек): подпись админской кнопки «🔄 Пройти регистрацию "
        "заново» берётся из общей константы keyboards.builders.ADMIN_REREG_BUTTON_TEXT (импорт), "
        "чтобы защита ввода настроек знала её без второго литерала; потолок 2445 с небольшим запасом. "
        "+10 строк (план 30-04, задача 4, A2-05): `_recall_display` — repeatable-колонка "
        "(mini_portfolio) показывает прошлый ответ через `repeatable_display(parse_repeatable("
        "...))`, а не сырой JSON, если делегат в прошлом сезоне пользовался repeatable-"
        "контролом (Rule 1, найдено при ревизии `mini_portfolio`-читателей); правило форматирования "
        "живёт в reg_engine.py, здесь только вызов + два новых импорта. "
        "+9 строк (квик 260911-w2m): врезка гейта правки анкеты в ветку (b) `cmd_start` — "
        "перед входом в `offer_resume`/`?start=edit` спрашивает `services.reg_edit_policy."
        "edit_gate(user)`, при отказе отвечает текстом реестра и главным меню; само правило "
        "живёт в services/reg_edit_policy.py, здесь только точка врезки; потолок поднят до "
        "фактического размера. "
        "Phase 21 (21-01, FORM-SYNC-01): REG_STEP_TYPES/STEP_TO_COLUMN/RECALLABLE_STEPS/"
        "SELECT_CONFIG/MULTI_CONFIG/enabled_steps/prompt-резолюция/pre-flow гейты/"
        "prior-answer правило переехали в корневой reg_engine.py (2309 -> 2099 строк) -- "
        "потолок опущен до нового размера + ~5% запаса (план 21-09 добавит точки "
        "синхронизации черновика). Остаток -- FSM-движок (_ask_step/_advance/"
        "_decide_status/...), cmd_start, finalize_registration -- документированный "
        "разрыв 13-03 (registration.py остаётся агрегатором-ядром, а не god-файлом: "
        "reg_flow.py/reg_steps.py уже вынесены и оба комфортно под GUIDELINE). "
        "+4 строки (Phase 27, 27-04, LANG-01) -- вызов и импорт нового шва "
        "handlers/reg_lang.py (экран выбора языка на /start) в cmd_start и в хвосте файла; "
        "вся логика (offer_language/menu_lang_open/lang_pick_choose) — в самом шве, "
        "потолок поднят до фактического размера. "
        "+32 строки (Phase 27, 27-05, LANG-02) -- врезка перевода делегатской анкеты чата НА "
        "ОТПРАВКЕ: импорт handlers/reg_i18n.py, tr_text/tr_kb внутри _safe_answer (единственная "
        "воронка), lang/tr_map в _build_summary (составная строка сводки переводится адресно) "
        "и в _advance перед вызовом _build_summary; вся логика перевода (say()/tr_kb()/ctx_for())"
        " — в самом шве, здесь только точки врезки; потолок поднят до фактического размера. "
        "+25 строк (UAT-фикс стенда 27-05, lang=en) — значения closed-option полей сводки "
        "(_build_summary) и экрана «Прошлый ответ» (_recall_display/_show_recall_screen) теперь "
        "тоже переводятся канон -> подпись, не только лейблы; сам перевод (display_summary_value/"
        "display_value_for_step/summary_value_maps) — в handlers/reg_i18n.py, здесь — вызов "
        "value_maps в _advance и async-конвертация _recall_display; потолок поднят до "
        "фактического размера. "
        "+13 строк (Phase 28, 28-02, SU-01/SU-04): общий хвост `_ask_step` (шаг без "
        "собственной ветки уходит в шов, не замирает молча) + импорт шва reg_extra_steps в "
        "хвосте файла — сами экраны и обработчики пяти новых шагов СкиллАпа живут в шве, "
        "агрегатор получил только точку делегирования; потолок поднят до фактического размера. "
        "+16 строк (Phase 28, 28-05, SU-04): ветка `resume` в `_ask_step` разветвилась по "
        "режиму (`resume_mode(city_code) == \"fork\"` зовёт `reg_resume_fork.ask_fork` ленивым "
        "импортом, иначе прежний код без изменений) + импорт `resume_mode` + шов-импорт "
        "`reg_resume_fork` в хвосте файла — сам экран развилки и её callback-хендлеры живут в "
        "шве; потолок поднят до фактического размера. "
        "+1 строка (fix ecac315, вне этого плана): переименование затенённого импорта "
        "`options` -> `engine_options` не поднимало потолок в своём коммите — обнаружено "
        "как уже красный тест при старте плана 28-06, зафиксировано здесь же по ходу. "
        "+13 строк (Phase 28, 28-06, SU-05/SU-06/SU-07, задача 1): шестой деп-линк-экстрактор "
        "`extract_ambassador_ref`/`resolve_referrer` — импорт из reg_engine + резолюция "
        "referrer_id в `cmd_start` (числовой формат БЕЗ проверки существования, D-06 "
        "byte-for-byte; `resolve_referrer` — только для нового amb_-формата, OQ-2 CONTEXT). "
        "+10 строк (Phase 28, 28-06, SU-07, задача 2): шов-импорт `reg_ambassador` в хвосте "
        "файла + вызов `offer_ref_link` в самом хвосте `finalize_registration` (только "
        "`mode == \"new\"`, в try/except — сбой предложения не должен ронять сохранённую "
        "заявку); сам экран/хендлеры `regamb:want`/`regamb:later` живут в шве; потолок "
        "поднят до фактического размера. "
        "+10 строк (Phase 28, 28-09, SU-10): `_resume_file_stem` получила третий параметр "
        "`mode` (\"full\"/\"id\") + расширенный докстринг — сама функция остаётся чистой "
        "sync, режим читает async-вызывающий (`services/reg_finalize.py`); потолок поднят "
        "до фактического размера. "
        "+31 строка (quick 260910-wb6, коммит ef315f9): `columns_for_step` из reg_engine + "
        "хелпер `_cols_list` + `just_answered`/`answered_col` приняли набор колонок шага "
        "вместо одной (`_sync_draft_in`/`_sync_draft_out`/`_advance`) — прод-баг с 05.09: "
        "файловое резюме писало в черновик только `resume_text`, `resume_file_id`/"
        "`resume_file_name` терялись на финале; сама резолюция колонок живёт в корневом "
        "`reg_engine.py`, здесь — точки врезки; потолок поднят до фактического размера. "
        "+1 строка (квик 260912-mcj): импорт `services.timeutil.msk_now` — семья «сейчас» "
        "бота (TTL черновика, имя файла резюме) переведена на московское время; потолок "
        "поднят до фактического размера. "
        "+28 строк (план 30-06, задача 4, A2-01/03/04/05): врезка диспетчера типов новой "
        "анкеты в `_ask_step` (`degrade_kind`/`form_v2_flags` -> lookup/composite/repeatable "
        "уходят в свой шов ленивым импортом, `reg_types_composite.maybe_show_recap` проверяется "
        "первой) + три импорта швов в хвосте файла; сами ветки — в handlers/reg_types_*.py, "
        "потолок поднят до фактического размера. "
        "+33 строки (квик 260914-k74, T2): `append_to_party_sheet`/`append_to_short_sheet` "
        "стали сами считать заголовки (`party_sheet_headers`/`short_sheet_headers`) и передавать "
        "их третьим аргументом в `append_to_named_sheet`, чтобы вкладка, созданная первым "
        "аппендом, получила строку заголовков ДО первой строки данных (иначе `_status_col_index` "
        "не находит колонку «Статус» и статусы одобренных не проставляются — инцидент 13.09, "
        "«СПб Акция»); сам расчёт заголовков уже жил в этом файле, здесь только try/except "
        "вокруг него и проброс аргумента; потолок поднят до фактического размера.",
    ),
    "admin_gamification.py": (
        2020,
        "геймификация -- самый большой шов 13-04, растёт вместе с фичами игры "
        "(Phase 14/16)",
    ),
    "admin_settings.py": (
        2450,
        "Квик 260919-mlu (Task 3): +15 строк — развилка «была своя вкладка, имя меняется» "
        "перед гейтом 260815-3hw в settings_edit_value (ленивый вызов "
        "handlers/admin_sheet_tabs.py::tab_change_screen, вся ветвящаяся логика и три новых "
        "хендлера живут в новом шве, не здесь); 2429 -> 2444, потолок 2450 с небольшим "
        "запасом на задачу 4 того же квика (кнопки префикса вкладок, +1 строка). "
        "Перф 17.09 (N+1 на экране группы настроек, тот же класс, что у Mini App /settings/all): "
        "+32 строки — `settings_toggle_rows`/`render_settings_group_text`/"
        "`build_settings_group_keyboard` стали тонкими обёртками над `_impl`-версиями, "
        "оборачивающими тело в `database.db.settings_snapshot()` (один снимок bot_settings на "
        "рендер вместо соединения на ключ); импорт `settings_snapshot` добавлен строкой выше, "
        "2393 -> 2425, потолок 2440 с небольшим запасом. "
        "16.09 (подпись кнопки меню не сохраняется как значение настройки): +13 строк — проверка "
        "в settings_edit_value (подпись меню -> SkipHandler к настоящему хендлеру кнопки, "
        "служебная подпись -> объяснение без записи); сами подписи собирает keyboards.builders; "
        "2380 -> 2393, потолок 2400 с небольшим запасом. "
        "Квик 260916 («📊 Итоги дня»): +23 строки — строка тумблера вечерней сводки в "
        "settings_toggle_rows, хендлер toggle_daily_digest (обычный _toggle_module_setting, "
        "сама сводка живёт в services/daily_digest.py) и daily_digest_time в "
        "_SYSTEM_FIELD_ORDER, 2337 -> 2360; потолок 2380 с небольшим запасом. "
        "Квик 260916 (дайджест заявок): +27 строк — строка тумблера «📥 Уведомления о заявках» "
        "в settings_toggle_rows, хендлер toggle_reg_submit_notify (обычный _cycle_enum_setting, "
        "сам дайджест живёт в services/reg_digest.py) и reg_submit_digest_minutes в "
        "_APPS_FIELD_ORDER, 2310 -> 2337; потолок 2350 с небольшим запасом. "
        "16.09 (режим «спрашивать язык всем при первом /start»): +9 строк — `toggle_delegate_lang_ask_on_start` крутит три варианта вместо двух и подпись строки "
        "показывает «текущее → новое», 2301 -> 2310; потолок 2320 с небольшим запасом. "
        "Правка 15.09 (владелец, «привязка через личку админа»): +31 строка — снесённый "
        "экран «💬 Чат» (handlers/admin_chat.py) заменён общим тумблером "
        "`toggle_chat_tracking_enabled` (текст в settings_toggle_rows + запись в общий "
        "словарь + сам callback-хендлер через `_toggle_module_setting`) и функцией "
        "`_chat_status_line` (одна строка «видно без экрана» в группе «🔧 Система»), 2270 -> "
        "2301; потолок поднят до фактического размера. "
        "Квик 260914-rgr (RGR-01..07): +3 строки — `chat_refresh_minutes` в хвосте "
        "`_SYSTEM_FIELD_ORDER` (единственный редактируемый ключ чата делегатов, D-11 — "
        "id/название чата в этот список НЕ входят, их пишет бот сам), 2263 -> 2270 в пределах "
        "запаса. "
        "квик 260913-16o: врезка ветки подтверждения в `_toggle_approval_setting` — сам экран "
        "и алерт живут в шве handlers/admin_settings_audit.py, здесь только ветка "
        "`if new_val == \"auto\"` с ленивым импортом и return; плюс воронка "
        "set_setting_by_admin/delete_setting_by_admin (задача 1, без изменения числа строк "
        "хендлеров, только импорт). "
        "Phase 30 (30-01, A2-08): девять строк тумблеров новой анкеты в settings_toggle_rows "
        "(текст «подпись: Вкл → Выкл» на каждый плюс option_labels-текст мастер-тумблера "
        "reg_form_v2_enabled) — сами хендлеры в шве handlers/admin_reg_form.py, здесь только "
        "общий источник строки кнопки (сторож tests/test_admin_sections_ia20.py::"
        "test_toggle_rows_are_shared_with_the_settings_screen); потолок поднят до фактического "
        "размера (2185 -> 2256). "
        "+11 строк (квик 260911-w2m): переключатель «✏️ Правка анкеты делегатом» — "
        "_next_enum_value/_cycle_enum_setting (общий цикл enum-настройки из N положений, "
        "рядом с _toggle_module_setting), строка reg_edit_policy_text в settings_toggle_rows/"
        "_row, хендлер toggle_reg_edit_policy по форме toggle_reg_edit_remoderation; само "
        "правило «можно ли редактировать» живёт в services/reg_edit_policy.py, здесь только "
        "переключатель и человеческий алерт; потолок поднят до фактического размера. "
        "+13 строк (quick 260904-dq1, «🌙 Тихие часы») -- три текстовых ключа в "
        "_APPS_FIELD_ORDER (редактор экрана и per-city пикер достаются бесплатно), строка "
        "тумблера quiet_hours_toggle_text в settings_toggle_rows и хендлер toggle_quiet_hours "
        "по форме toggle_pending_reminder; сам механизм окна/очереди живёт в "
        "services/quiet_hours.py, нового экрана бота нет; потолок поднят до фактического "
        "размера. "
        "Phase 22 (22-01, WEB-SET-01/04, D-12): _apply_event_type_preset/_per_city_visible_codes/"
        "HTML_SETTINGS/_base_setting_key/_SHEET_TAB_WRITE_MODE/_after_tab_setting_saved/"
        "_tab_confirm_text/_tab_check_failed_warning вынесены в корневой aiogram-free "
        "settings_ops.py (2384 -> 2143 строки); admin_settings.py импортирует их оттуда с "
        "теми же приватными именами-алиасами, тела и порядок хендлеров не сдвинуты -- "
        "потолок опущен до фактического размера. Причины роста ДО этого выноса (история "
        "потолка) сохранены ниже. "
        "+18 строк (Phase 21, 21-07 Task 2, FORM-SYNC-04) -- тумблер toggle_reg_edit_remoderation "
        "(«Изменённая анкета — снова на модерацию», D-12): текст статуса в settings_toggle_rows "
        "(подпись читает SETTINGS_SCHEMA, не литерал), строка в карте _row, хендлер по форме "
        "toggle_nudge_enabled (2252 -> 2270); потолок поднят до нового размера + ~5% запаса. "
        "+3 строки (Phase 21, анкета в Mini App) -- три ключа текстов анкеты (reg_form_*) в HTML_SETTINGS; "
        "сами тексты живут в реестре, экраны -- в miniapp. "
        "+2 строки (Info-хвост ревью фазы 20) -- два ленивых шва на settings_return_screen "
        "вместо литеральных render_settings_group_text(\"sheets\")/build_settings_group_keyboard "
        "в обоих хендлерах вкладок таблицы. "
        "+26 строк (ревью фазы 20) -- изоляция данных потоков EditSetting (вход задаёт данные "
        "целиком через set_data вместо домешивания, _return_hint_from_state выбирает поток по "
        "СОСТОЯНИЮ через таблицу flows, а не по первому найденному ключу) и приём уже "
        "прочитанной шапки города в settings_toggle_rows (экран раздела рисует шапку и тумблеры "
        "одной клавиатурой, два независимых чтения давали разъезд города) и возврат операций "
        "таблицы в свой раздел вместо корня (два ленивых шва на op_return_keyboard). "
        "+115 строк (Phase 20, 20-04: экраны возврата после действия) -- обратный индекс "
        "_group_of_setting_key (ключ настройки -> её группа, выведен из SETTINGS_GROUPS), "
        "константа PHOTO_FILE_GROUP, развилка _return_hint_from_state и пятнадцать ленивых "
        "швов на handlers/admin_sections.py в перерисовках после действия (модульный импорт "
        "замкнул бы цикл через хвостовой seam-импорт); сам резолвер экрана возврата "
        "(settings_return_screen) живёт в admin_sections.py, сюда не добавлен. "
        "+44 строки (Phase 20, разделы по flow делегата) -- вынос текстов тумблеров лендинга "
        "в settings_toggle_rows (единственный источник кнопок и для старого экрана, и для "
        "экранов разделов: одна подпись тумблера в двух местах = два разных текста), "
        "разрез «📝 Регистрация» на анкету + новую группу «📋 Заявки» (_APPS_FIELD_ORDER) и "
        "хвостовой seam-импорт handlers/admin_sections.py; сами экраны разделов и реестр "
        "SECTIONS живут в новом модуле, сюда не добавлены. "
        "+72 строки (quick 260825-ldi) -- «🔄 Синхронизация» (sync_sheet) получила маршрутизацию "
        "по городским вкладкам (city_row_tab), как у соседней «♻️ Пересобрать»: раскладка "
        "пользователей на main/city_users, пер-вкладочный try/except с fail-soft-хелперами и "
        "разбивка отчёта по вкладкам -- логика физически смежна существующему телу sync_sheet, "
        "выносить в отдельный файл ради одной функции не стали. "
        "(+2 строки 22.08: poll_intro_text/polls_sheet_tab в _*_FIELD_ORDER и HTML_SETTINGS после слияния «➕ пункт» + согласий + опросов; новые экраны живут в швах admin_settings_lists/admin_consent) "
        "настройки+инструменты таблицы+выгрузки -- блоки не смежны в исходном "
        "god-файле, разрез на два файла потребовал бы двух точек шва-импорта "
        "(см. 13-06-SUMMARY.md, Known Gap); +31 строка -- перепроверка прав "
        "на per-city запись в settings_edit_value (ревью 09.3); +33 строки (quick 260819) -- "
        "три тумблера лендинга (предотбор / сводка заявок / догонялка) и поля "
        "nudge_*/интервалов джоб в _*_FIELD_ORDER; +34 строки (quick 260820-rms) -- отбивка "
        "команды в settings_edit_value и показ списочной настройки по пунктам с "
        "предупреждением о полной замене (оба сюжета живут ровно в этих двух функциях, "
        "выносить их в отдельный файл — шов ради одной проверки); +15 строк (quick 260822) -- "
        "кнопки ➕/🗑/✏️ на экране списочной настройки и хвостовой seam-импорт "
        "admin_settings_lists.py (сами хендлеры живут в новом модуле); +7 строк (Phase 19-08) -- "
        "кнопка «🎨 Оформление» на лендинге настроек рядом с «📊 Дашборд» и хвостовой "
        "seam-импорт handlers/admin_miniapp.py (сами хендлеры живут в новом модуле); "
        "+3 строки (Phase 19.1-07) -- хвостовой seam-импорт handlers/admin_miniapp_theme.py "
        "(второй шов «🎨 Оформление»: пресеты и ручки кастома, сами хендлеры живут в новом модуле); "
        "+3 строки (Phase 23-01, APP-TINDER-01, D-05) -- reject_reason_templates в "
        "_APPS_FIELD_ORDER (шаблоны причин отказа для шторки Mini App, правится общим списочным "
        "редактором admin_settings_lists.py, нового экрана бота нет); "
        "+15 строк (коммит 34633f1) -- подсказки настроек переписаны менеджерским языком с "
        "примером значения (PHOTO_FIELDS/FILE_FIELDS синхронизированы с реестром), потолок "
        "поднят до фактического размера",
    ),
    "admin_cities.py": (
        960,
        "города + сброс/импорт сезона + дедупликация -- один связный экран (13-05)",
    ),
    "admin_reg_percity.py": (
        1127,
        "новый шов (module-size split из admin_reg_config.py, 1451 строка): экраны «📋 Вопросы "
        "регистрации» и «✏️ Тексты вопросов» per-city/per-track (toggle_reg_question/"
        "toggle_party_question/toggle_short_question/reg_resume_mode_toggle/reg_q_reset_city*/"
        "reg_prompt_*) плюс хелперы, которые использует только эта пара экранов (928 строк) -- "
        "один связный экран той же природы, что admin_cities.py/admin_broadcasts.py выше; "
        "потолок = фактический размер + ~5% запаса. "
        "Квик 260906-7zv (HELP-01/02/03): +145 строк -- редактор подсказок формата под "
        "вопросом («💡 Подсказка», reg_help_edit/reg_help_rst/reg_help_rst_go), тот же экран, "
        "новых модулей не заводит (1073 строки); потолок поднят до фактического размера + ~5%.",
    ),
    "admin_broadcasts.py": (
        1427,
        "Квик 260915-twr (Task B): +78 строк — журнал запланированных рассылок "
        "(отправка использует ту же строку broadcasts, что мгновенная — код живёт в "
        "services/scheduler.py/database/db.py, не здесь), предупреждение об аудитории «Всем» "
        "на экране подтверждения (_audience_warning) и живой экран прогресса после сбоя "
        "edit_text в bc_go (fallback-отправка нового сообщения) + страховочный bc_no_after_start "
        "без фильтра состояния, 1281 -> 1359; потолок поднят до фактического размера + ~5%. "
        "Квик 260914-rgr (RGR-01..07, задача 3): +38 строк — поле фильтра «Чат делегатов» "
        "(CHAT_IN/CHAT_OUT в _FILTER_FIELD_LABELS/_PICKER_FIELDS, show_chat в "
        "_filter_menu_kb/_render_filter_menu, ветка delegate_chat в _show_value_picker и в "
        "сборке записи фильтра filter_pick_value), 1243 -> 1281 в пределах запаса. "
        "рассылки + pending_albums (13-05). "
        "+23 строки (quick 260904-dq1, «🌙 Тихие часы») -- предупреждение на шаге "
        "«когда» (broadcast_schedule_when) при попадании времени в глобальное окно тишины: "
        "две кнопки bcast_quiet:shift/keep + общий хвост _confirm_broadcast_schedule; "
        "потолок поднят до фактического размера. "
        "Квик 260910-okb (BC-01..06): +218 строк -- превью+подтверждение/прогресс/стоп "
        "немедленной рассылки (bc_go/bc_no/bc_stop, _send_confirm_prompt) и отзыв у "
        "получателей + экран «Последние рассылки» (_broadcast_card/bc_rev*/admin_broadcast_log/"
        "cmd_broadcasts), тот же экран, новых модулей не заводит (1101 строка); потолок поднят "
        "до фактического размера + ~5%. "
        "Квик 260911-0fh (RESUME-FILTER-01..06): +43 строки -- поле фильтра «Резюме» (кнопка "
        "меню, пикер «есть»/«нет», ветка filter_pick_value), тот же экран, новых модулей не "
        "заводит (1184 строки); потолок поднят до фактического размера + ~5%.",
    ),
    "admin_roles.py": (
        860,
        "роли + settings-guide (13-04)",
    ),
    "admin.py": (
        925,
        "сам агрегатор-ядро после 13-06, уже на границе GUIDELINE; +52 строки (Phase 15, "
        "STAT-03/D-10/D-18) -- городской скоуп render_stats_text по привязке staff.city + "
        "кнопка «🌐 Открыть дашборд» (_stats_keyboard_for). "
        "Потолок поднят 900->910 в quick 260906-8uq (FAQ-01..06): шов-импорт "
        "`from handlers import admin_faq` с комментарием, вставлен сразу после импорта "
        "admin_questions (900 -> 904). "
        "Потолок поднят 910->915 в квике 260910-ro7 (DELU-01..08): шов-импорт "
        "`from handlers import admin_purge` (скрытая команда «/delete_user») в самый хвост "
        "файла, после блока admin_gamification/admin_polls (909 -> 915). "
        "Потолок поднят 915->945 16.09 (правило тихого часа для всех уведомлений делегату): "
        "+24 строки в `_deliver_question_reply` — ответ организаторов идёт через "
        "`services.quiet_hours` (текстовый — kind text_html, не-текстовый — kind copy), плюс "
        "приписка менеджеру о доставке утром; правка ВНУТРИ существующего хендлера, новых "
        "точек входа нет (915 -> 939). "
        "Потолок опущен 945->925 тем же днём: `_notify_manual_coins` переехала в "
        "`services/coins_notify.py` (её же зовёт разборщик outbox'а Mini App — ручные монеты "
        "из приложения делегату не приходили вовсе), здесь остался реэкспорт под прежним "
        "именем (939 -> 910) — потолок по фактическому размеру + ~1.5%.",
    ),
    "user_actions.py": (
        1440,
        "17.09 (приёмка): +1 строка — ссылка приглашения через общий reg_engine.build_referral_link "
        "(один формат amb_<id> на все места выдачи); потолок 1440. "
        "Потолок поднят 1320->1335 в квике 260915-skg (P7): open_miniapp_button переводит "
        "текст/клавиатуру через reg_i18n.ctx_for/tr_text/tr_kb вместо сырого message.answer "
        "(английский вход в приложение при lang=en) -- правка внутри уже существующего "
        "хендлера, не новая точка входа, разрез файла тем же приёмом, что и ниже, отложен до "
        "отдельного прохода. "
        "НЕ часть Phase 13 (не god-файл этого рефактора) -- перебор обнаружен при "
        "13-07 верификации, вырос отдельно за счёт карточки задания делегата "
        "(Phase 14/16); зафиксирован как отдельный известный разрыв, не переносим "
        "сюда рефактор Phase 13. Потолок поднят 900->1140 в 16-01 (GAME-UI-01): "
        "делегатский список/карточка задания получили пагинацию/RU-категории/статус-"
        "текст, плюс новый экран «🪙 Баланс» (история+рейтинг). Разрез на отдельный "
        "шов-файл (handlers/game_delegate.py) рассмотрен и отложен -- игровой блок "
        "физически смежный (строки 79-777), но 7+ существующих тестов обращаются к "
        "делегатским игровым функциям через `ua_mod.<func>` (не через отдельный "
        "модуль, в отличие от admin_gamification.py), и перенос потребовал бы правки "
        "всех них вне заявленного files_modified этого плана; откладываем разрез до "
        "отдельного прохода, когда его можно сделать вместе с обновлением тестов. "
        "Потолок поднят 1140->1145 в Phase 19-08 (D-10): точка входа «📱 Приложение» "
        "(open_miniapp_button) — реплай-кнопка -> inline web_app, дописана в самый "
        "конец файла (golden-снапшот фиксирует порядок роутера). Потолок поднят "
        "1145->1170 в quick 260904-3vm (эстафета): фолбэк-хендлер "
        "reg_handoff_idle_fallback (StateFilter(None) + F.text) дописан в самый конец "
        "файла ПОСЛЕ open_miniapp_button — держит кнопки меню в приоритете (та же "
        "причина, что не даёт разрезать этот файл ещё раз прямо сейчас). "
        "Потолок поднят 1170->1320 в quick 260906-8uq (FAQ-01..06): экран «❓ Частые "
        "вопросы» (faq_screen + faq_page/faq_open_answer/faq_ask/show_faq) и развилка "
        "в ask_organizer_start (непустой FAQ показывается ПЕРЕД формой «Задать "
        "вопрос») вставлены перед этим блоком — тот же приём, что и предыдущие точки "
        "роста. "
        "Потолок поднят 1320->1434 (Квик 260917-en, приёмка 17.09 п.4): английский перевод "
        "всего делегатского меню чата — coins/leaderboard/game-task screens получили "
        "lang/tr_map параметры (reg_i18n.tr_text/tr_fmt/tr_kb) вместо голых литералов/"
        "нерезолвленных реестровых текстов; сам перевод — в handlers/reg_i18n.py, здесь "
        "только точки резолюции контекста и вызовы; потолок поднят до фактического размера.",
    ),
    "admin_caps.py": (
        1000,
        "Квик 260919-mlu (Task 3): +6 строк — три новые capability-записи развилки «была своя "
        "вкладка, имя меняется» (sheet_tab_rename_go/sheet_tab_reuse_go/sheet_tab_newtab_go, "
        "handlers/admin_sheet_tabs.py), рядом с sheets_tab_confirm/sheets_tab_cancel; "
        "990 -> 993, потолок 1000 с небольшим запасом на задачу 4 того же квика (кнопки "
        "префикса вкладок). "
        "Правка 15.09 (владелец, «привязка через личку админа»): -8 строк — capability-записи "
        "снесённого экрана «💬 Чат» (admin_chat/chat_chat_tracking_toggle/chat_refresh_now/"
        "chat_unbind:*/chat_unbind_go:*/chat_broadcast_out:*) заменены одной строкой "
        "`toggle_chat_tracking_enabled` (974, в пределах уже существующего потолка — "
        "снижение размера не требует поднимать потолок, число оставлено как есть). "
        "Квик 260914-rgr (RGR-01..07, задача 2): +9 строк — capability-записи экрана «💬 Чат» "
        "(admin_chat/chat_chat_tracking_toggle/chat_refresh_now/chat_unbind:*/"
        "chat_unbind_go:*, та же капа «settings», что у соседнего admin_quiet_hours), "
        "969 -> 978 в пределах запаса. "
        "Квик 260914-rgq (RGQ-01): +4 строки — capability-записи экрана «📇 Список заявок» "
        "(admin_app_list/apl:*, та же капа «moderate_reg», что у соседнего admin_questions), "
        "965 -> 969 в пределах ~5% запаса. "
        "Phase 28 (28-08, SU-08, задача 1): +8 строк — capability-записи нового экрана "
        "«🧮 Правила балла» (admin_reg_scoring/scoring_toggle:*/scoring_limit:*/"
        "scoring_drop:*/scoring_noop, та же капа «settings», что у соседнего modcard_open); "
        "файл был уже у самой границы GUIDELINE (848 строк) — потолок поднят до "
        "фактического размера (856) + ~5% запаса. "
        "+1 строка (задача 3): capability тумблера toggle_apps_queue_sort_by_score рядом с "
        "toggle_reg_scoring_enabled (856 -> 857, в пределах уже поднятого потолка). "
        "Phase 30 (30-07, A2-03): +3 строки — capability-записи экрана «📚 Справочники» "
        "(admin_lookup/admin_lookup:*/state:LookupAdmin:*, капа «settings», 857 -> 904 с "
        "запасом того же порядка, что предыдущее поднятие). "
        "+1 строка (задача 4): capability атрибутов списка-справочника "
        "(settings_list_attr:*, 904 -> 905). "
        "+60 строк (fix 89a70b7): `TelegramForbiddenError` в `notify_by_capability` больше не "
        "идёт в общий `except Exception` — заблокировавший бота модератор не считается в `sent`, "
        "первый случай для uid даёт один WARNING и алерт остальным `config.ADMIN_IDS`, повтор в "
        "течение 24 ч молчит (`logger.debug`); кулдаун — процессный словарь `_blocked_notified_at`, "
        "не в БД; потолок поднят до фактического размера.",
    ),
    "admin_moderation.py": (
        899,
        "Phase 28 (28-08, SU-08, задача 1): +6 строк — шов-импорт `from handlers import "
        "admin_reg_scoring` в хвосте файла (та же техника, что и соседний импорт "
        "admin_modcard чуть выше); файл был уже у самой границы GUIDELINE (844 строки) — "
        "потолок поднят до фактического размера (850 -> 851, ~5% запаса). "
        "+5 строк (задача 3): чтение тумблера apps_queue_sort_by_score в _show_current_card "
        "перед вызовом get_pending_users (T-23-04: сортирует SQL, не Python) — потолок поднят "
        "до фактического размера (856) + ~5% запаса.",
    ),
}


def _handler_modules() -> list[Path]:
    return sorted(HANDLERS_DIR.glob("*.py"))


def _line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def test_known_overages_reference_real_files():
    """Guards the allowlist itself: no stale entries for files that got deleted/renamed."""
    existing = {p.name for p in _handler_modules()}
    missing = sorted(name for name in KNOWN_OVERAGES if name not in existing)
    assert not missing, (
        f"KNOWN_OVERAGES lists module(s) that no longer exist under handlers/: {missing}. "
        "Remove the stale entry (the file was likely renamed or deleted)."
    )


@pytest.mark.parametrize("path", _handler_modules(), ids=lambda p: p.name)
def test_handler_module_under_size_ceiling(path: Path):
    """Fails loud when a handlers/*.py module crosses its size ceiling.

    Not in KNOWN_OVERAGES -> DEFAULT_CEILING applies. In KNOWN_OVERAGES -> that file's own
    ceiling applies instead. Either way: if this test fails, the module has regrown toward
    god-file size (or crossed its already-generous named exception) and needs a split -- see
    docs/CONVENTIONS.md for the shared-router seam-import technique this phase established. If the
    growth is legitimate and a split is genuinely not worth it right now, raise the ceiling for
    THIS file in KNOWN_OVERAGES above, in the same commit, with an updated reason.
    """
    lines = _line_count(path)
    if path.name in KNOWN_OVERAGES:
        ceiling, reason = KNOWN_OVERAGES[path.name]
        assert lines <= ceiling, (
            f"{path.name}: {lines} lines exceeds its named exception ceiling ({ceiling}). "
            f"Recorded reason for the existing exception: {reason!r}. If this new growth is "
            "legitimate, raise the ceiling in KNOWN_OVERAGES (tests/test_module_size_"
            "convention_260816.py) in the same commit, with an updated reason -- otherwise "
            "split the module along a feature/callback-prefix seam (see docs/CONVENTIONS.md)."
        )
    else:
        assert lines <= DEFAULT_CEILING, (
            f"{path.name}: {lines} lines exceeds the default ceiling ({DEFAULT_CEILING}, "
            f"guideline is ~{GUIDELINE}). Either split the module along a feature/"
            "callback-prefix seam (see docs/CONVENTIONS.md's shared-router technique), or, if the "
            "growth is a deliberate and reviewed exception, add a named entry to "
            "KNOWN_OVERAGES in this file, in the same commit, with a one-line reason."
        )
