---
phase: quick-260724-cfn
plan: 01
type: execute
wave: 1
depends_on: []
files_modified: [handlers/admin.py, tests/test_admin_phase5.py]
autonomous: true
requirements: [WR-02a, WR-02b, WR-05]
must_haves:
  truths:
    - "Админ может задать approve_text__party через бота (не прямой записью в bot_settings)"
    - "На экране «✏️ Тексты вопросов» есть свитчер Полный ⇄ Party; в режиме Party кнопки правят reg_prompt_{step}__party"
    - "Help-текст payment_options описывает третье поле трек-фильтра точным синтаксисом парсера"
    - "Существующие глобальные редакторы approve_text и reg_prompt_{step} работают без изменений"
  artifacts:
    - path: "handlers/admin.py"
      provides: "approve_text__party в SETTINGS_FIELDS+SETTINGS_GROUPS+HTML_SETTINGS; track-свитчер на экране prompt-текстов; обновлённый help payment_options"
    - path: "tests/test_admin_phase5.py"
      provides: "unit-тесты трек-свитчера prompt-экрана + наличие approve_text__party editor"
  key_links:
    - from: "reg_prompt_track:party switcher"
      to: "reg_prompt_edit:{step}:party -> FSM setting_key reg_prompt_{step}__party"
      via: "callback_data track suffix"
      pattern: "reg_prompt_edit:.*:party|reg_prompt_.*__party"
    - from: "settings_edit:approve_text__party"
      to: "delete_setting/set_setting(approve_text__party)"
      via: "существующий settings_edit_start/settings_edit_value (SETTINGS_FIELDS prompt lookup)"
      pattern: "approve_text__party"
---

<objective>
Закрыть три Phase-5 code-review deferral'а (WR-02a, WR-02b, WR-05 из
`.planning/phases/05-participant-tracks-party-delegates/05-REVIEW.md`), нарушающих core
value «всё через бота»: per-track party настройки сейчас правятся только прямой записью
в `bot_settings`. Даём админ-UI для их задания, НЕ трогая рантайм-семантику override'ов
(D-05/D-15 truthiness fallback уже работает).

Purpose: менеджер DXP может полностью настроить party-трек через бота — approve-текст,
формулировки вопросов и трек-фильтры тарифов — без ручного SQL.
Output: правки только в `handlers/admin.py` (+ тесты). Никаких изменений рантайма
регистрации/оплаты, БД-миграций, новых зависимостей.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@CLAUDE.md
@.planning/phases/05-participant-tracks-party-delegates/05-REVIEW.md

<interfaces>
<!-- Реальный код (прочитан), против которого планируем. Executor использует напрямую. -->

handlers/admin.py — SETTINGS_FIELDS (список кортежей `(key, label, prompt)`):
  строка 353: ("approve_text", "🎉 После одобрения", "...HTML.")
  строка 360: ("payment_options", "💳 Варианты оплаты", "...Название | Цена\n...")

handlers/admin.py — SETTINGS_GROUPS (label, token, [keys]), группа Party:
  ("🎉 Party", "party", ["party_closed_text", "party_sheet_tab"])
  группа Регистрация ("reg") содержит "approve_text".

handlers/admin.py:1213  HTML_SETTINGS = {"start_text", "reg_complete_text", "approve_text"}
  — settings_edit_value(1215) при key in HTML_SETTINGS берёт message.html_text (сохраняет HTML);
    value == "-" -> delete_setting(key); иначе set_setting(key, value).

handlers/admin.py:881 settings_edit_start — читает prompt из SETTINGS_FIELDS по key,
  callback `settings_edit:{key}`. Уже работает для ЛЮБОГО key, попавшего в SETTINGS_FIELDS.

handlers/admin.py — экран текстов вопросов (WR-02b цель):
  _prompt_steps()   (2432): -> list[(step_key, label)]; первый ("full_name", ...), далее REG_FLOW.
  admin_reg_prompts (2440, callback "admin_reg_prompts"): строит кнопки
      reg_prompt_edit:{step_key}, mark ✅ если get_setting(f"reg_prompt_{step_key}") задан,
      back -> "admin_settings".
  reg_prompt_edit   (2460, callback startswith "reg_prompt_edit:"):
      step_key = callback.data.split(":", 1)[1]; key = f"reg_prompt_{step_key}";
      set_state(EditSetting.waiting_for_value); update_data(setting_key=key).

handlers/admin.py — ЭТАЛОННЫЙ паттерн трек-свитчера (зеркалить его):
  _track_switcher_row(active) (2119): [Полный -> reg_q_track:full, Party -> reg_q_track:party],
      активный помечен "• ".
  build_questions_keyboard(track) (2168): первая строка = _track_switcher_row(track).
  render_questions_text(track="full") (2143): в party добавляет курсивную подсказку.
  reg_q_track_switch (2261, callback startswith "reg_q_track:"): track=split(":",1)[1];
      валидирует in ("full","party"); re-render ТОГО ЖЕ сообщения edit_text(render, kb(track)).

handlers/payment.py:99 _parse_options(raw) — синтаксис payment_options (для WR-05 точности):
  строка = "label|price" ИЛИ "label|price|track1,track2".
  Третье поле: parts[2].strip(), split по запятой "," -> set треков; пусто => None (все треки).
  Валидные значения трека (participant_type): full, party_overnight, party_noovernight.

Тестовая инфра tests/test_admin_phase5.py: FakeCallback/FakeMessage/_flat_callback_data,
  asyncio.run(...), config.DB_PATH=tmp. Паттерн тестов трек-свитчера — строки 78-125.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: WR-02a approve_text__party editor + WR-05 payment_options help</name>
  <files>handlers/admin.py, tests/test_admin_phase5.py</files>
  <action>
Два дешёвых, низкорисковых изменения, переиспользующих существующую settings_edit-машинерию.
НЕ создавать новых callback'ов/FSM — settings_edit_start/settings_edit_value уже обслуживают
любой key из SETTINGS_FIELDS.

(A) WR-02a — редактор approve_text__party (per-track approve-текст, D-15):
  1. В SETTINGS_FIELDS добавить запись сразу ПОСЛЕ "approve_text" (строка ~353):
     ключ "approve_text__party", label "🎉 После одобрения (Party)", prompt поясняет:
     текст, который увидит party-делегат при одобрении; пусто/«-» => участник получает
     общий «После одобрения» (approve_text). Поддерживается HTML.
  2. В SETTINGS_GROUPS в группу Party ("party") добавить "approve_text__party" в список keys
     (рядом с party_closed_text/party_sheet_tab) — чтобы поле появилось в под-экране «🎉 Party».
  3. В HTML_SETTINGS (строка 1213) добавить "approve_text__party" — чтобы редактор сохранял
     HTML так же, как approve_text (иначе разметка потеряется).
  НЕ трогать рантайм _approve_text_for в registration.py — truthiness fallback уже корректен:
  пустой/удалённый override => глобальный текст.

(B) WR-05 — help-текст payment_options (D-16 трек-фильтр):
  В SETTINGS_FIELDS запись "payment_options" (строка ~360) дополнить prompt точным описанием
  третьего опционального поля. Синтаксис ТОЧНО по _parse_options: "Название | Цена | треки",
  треки — через запятую, из значений full / party_overnight / party_noovernight; без третьего
  поля тариф виден ВСЕМ трекам. Пример party-only строки. Не менять существующую часть
  описания (Название | Цена, «Цена 0 = бесплатно» и т.д.).

(C) Тесты (tests/test_admin_phase5.py): добавить
  - тест: "approve_text__party" присутствует среди ключей SETTINGS_FIELDS И в HTML_SETTINGS
    И в группе Party (SETTINGS_GROUPS token "party");
  - тест: help-строка payment_options содержит "party_overnight" и "party_noovernight".
  </action>
  <verify>
    <automated>cd "C:/Users/alexe/Desktop/work/AIESEC_event_bot" && python -m pytest tests/test_admin_phase5.py -q</automated>
  </verify>
  <done>approve_text__party редактируется через под-экран «🎉 Party» (settings_edit), сохраняет HTML, «-» возвращает к общему тексту; help payment_options описывает трек-фильтр с точными значениями треков; новые тесты и весь test_admin_phase5.py зелёные.</done>
</task>

<task type="auto">
  <name>Task 2: WR-02b track-свитчер (full ⇄ party) на экране «Тексты вопросов»</name>
  <files>handlers/admin.py, tests/test_admin_phase5.py</files>
  <action>
Зеркалить существующий паттерн reg_q_track_switch / build_questions_keyboard, НЕ изобретая
новый механизм. Цель: на экране «✏️ Тексты вопросов» дать переключатель трека; в режиме Party
кнопки правят reg_prompt_{step}__party (D-05 override уже читается рантаймом с truthiness fallback).

  1. Вынести построение клавиатуры в helper build_prompts_keyboard(track="full"):
     - первая строка — трек-свитчер [Полный -> reg_prompt_track:full, Party -> reg_prompt_track:party],
       активный помечен "• " (скопировать форму _track_switcher_row, но со своими callback'ами;
       можно добавить локальный _prompt_track_switcher_row или собрать инлайн);
     - для каждого (step_key, label) из _prompt_steps():
         key = f"reg_prompt_{step_key}" если track=="full" иначе f"reg_prompt_{step_key}__party";
         mark = "✅" если get_setting(key) непустой иначе "✏️";
         callback = f"reg_prompt_edit:{step_key}" (full, БЕЗ суффикса — обратная совместимость)
                    иначе f"reg_prompt_edit:{step_key}:party";
     - back-кнопка "← Назад к настройкам" -> "admin_settings" (как сейчас).
  2. Вынести текст экрана в helper render_prompts_text(track="full"): базовый заголовок как
     сейчас; в party добавить курсивную подсказку («Действуют в режиме 🎉 Party. ✏️ — берётся
     общий текст вопроса, ✅ — переопределено для party. «-» — сброс к общему.»).
  3. admin_reg_prompts (callback "admin_reg_prompts") — рендерит track="full" через новые
     helper'ы (поведение по умолчанию неизменно).
  4. Новый handler reg_prompt_track_switch на F.data.startswith("reg_prompt_track:"):
     проверка ADMIN_IDS; track=split(":",1)[1]; если не in ("full","party") -> "full";
     re-render ТОГО ЖЕ сообщения edit_text(render_prompts_text(track), build_prompts_keyboard(track)).
  5. reg_prompt_edit — распарсить необязательный трек-суффикс:
     parts = callback.data.split(":");  step_key = parts[1];
     track = parts[2] if len(parts) > 2 and parts[2] == "party" else "full";
     key = f"reg_prompt_{step_key}__party" если track=="party" иначе f"reg_prompt_{step_key}".
     Остальное (current preview, EditSetting.waiting_for_value, update_data(setting_key=key),
     «-» сброс через settings_edit_value) — без изменений.
     step_key'и (full_name + REG_FLOW) не содержат ":", поэтому split безопасен.

Совместимость: глобальный редактор (reg_prompt_edit:{step} без суффикса) продолжает писать
reg_prompt_{step}; callback "admin_reg_prompts" и FSM-состояние EditSetting не меняются.

  6. Тесты (tests/test_admin_phase5.py, зеркалить строки 78-125):
     - build_prompts_keyboard("full") содержит "reg_prompt_track:full"/"reg_prompt_track:party"
       и callback'и вида "reg_prompt_edit:{step}" БЕЗ ":party";
     - build_prompts_keyboard("party") содержит callback'и, оканчивающиеся ":party";
     - reg_prompt_track_switch с FakeCallback("reg_prompt_track:party"): edit_calls == 1,
       в клавиатуре есть кнопка с суффиксом ":party"; не-админ (user_id=1) отклонён (edit_calls==0);
     - reg_prompt_edit с FakeCallback("reg_prompt_edit:full_name:party") + FakeState: в FSM
       setting_key == "reg_prompt_full_name__party" (если FakeState недоступен — проверить через
       вызов с реальным FSMContext-заглушкой, как уже делают существующие тесты; иначе покрыть
       парс через build_prompts_keyboard party-callback'ов, а suffix-парс — юнит-проверкой строки).
  </action>
  <verify>
    <automated>cd "C:/Users/alexe/Desktop/work/AIESEC_event_bot" && python -m pytest tests/test_admin_phase5.py -q</automated>
  </verify>
  <done>Экран «Тексты вопросов» имеет свитчер Полный ⇄ Party; party-кнопки правят reg_prompt_{step}__party, full-кнопки — reg_prompt_{step} (без регрессии); переключение re-render'ит то же сообщение; не-админ отклонён; тесты зелёные.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| admin Telegram callback → bot_settings write | callback_data управляемо клиентом; step_key/track приходят из callback |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-cfn-01 | Elevation | reg_prompt_track_switch / reg_prompt_edit / settings_edit(approve_text__party) | mitigate | Каждый новый/изменённый handler начинается с `if from_user.id not in config.ADMIN_IDS: answer(...); return` — как во всех существующих admin-callback'ах |
| T-cfn-02 | Tampering | track-суффикс в reg_prompt_edit callback_data | mitigate | track принимается только если == "party", иначе "full" (закрытый whitelist); step_key используется как есть внутри f"reg_prompt_{step_key}" — тот же паттерн, что уже действует для глобального редактора (не расширяет поверхность записи ключей сверх текущей) |
| T-cfn-03 | Injection (HTML) | approve_text__party отображение | accept | Значение хранит админ (доверенный); при выводе current-preview используется html_module.escape в существующем settings_edit_start — переиспользуется без изменений |
</threat_model>

<verification>
- `python -m pytest tests/test_admin_phase5.py -q` — зелёный (новые + существующие).
- Полный прогон: `python -m pytest -q` — без регрессий (approve_text/reg_prompt глобальные редакторы целы).
- grep-гейты: `approve_text__party` присутствует в SETTINGS_FIELDS, HTML_SETTINGS и SETTINGS_GROUPS;
  `reg_prompt_track:` и `reg_prompt_edit:{step}:party` эмитятся; `party_overnight`/`party_noovernight`
  есть в help payment_options.
</verification>

<success_criteria>
- Все три WR закрыты только admin-UI/help-правками в handlers/admin.py; рантайм override'ов не тронут.
- approve_text__party задаётся через под-экран «🎉 Party» (сохраняет HTML, «-» = наследование).
- Экран текстов вопросов имеет рабочий свитчер Полный ⇄ Party; party пишет reg_prompt_{step}__party.
- Help payment_options точно описывает трек-фильтр (full / party_overnight / party_noovernight).
- Существующие глобальные редакторы, callback'и и FSM-состояния не сломаны; тесты зелёные.
</success_criteria>

<output>
После завершения создать `.planning/quick/260724-cfn-wr-02-wr-05-admin-ui-per-track-party-app/260724-cfn-SUMMARY.md`
</output>
