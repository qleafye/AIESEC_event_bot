"""Phase 27 (27-01, LANG-08/LANG-10) — сторож границы «делегатское / админское» для
`services/i18n_sources.py`. pytest-asyncio в проекте нет (см. соседние тесты реестра) —
асинхронные вызовы идут через `asyncio.run`.
"""
import asyncio

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA

import services.i18n_sources as i18n_sources

# Группы реестра, которые НИКОГДА не должны попасть в делегатский корпус (LANG-08 — это
# сторож границы, а не формальность): чисто административные + `consent` (LANG-09, ручной
# английский, не машинный) + `menu`/`miniapp` (переводятся отдельными точечными словарями,
# см. докстринг `services/i18n_sources.py`). Квик 260917-en: `event`/`game`/`pay` СНЯТЫ с
# этого списка — приёмка 17.09 явно расширила объём на весь чат делегата (приветствие,
# геймификация, оплата); их админские/нелингвистические ключи проверяются отдельно ниже
# (`test_admin_only_game_keys_excluded`/`test_non_language_pay_event_keys_excluded`), не
# групповым исключением.
_NON_DELEGATE_GROUPS = (
    "sheets", "dashboard", "apps", "system", "roles", "toggles",
    "miniapp", "menu", "consent",
)

_ADMIN_KEYS = ("reg_edited_admin_label", "reg_prev_reject_admin_label", "reg_resubmit_admin_label")

# Три образца яруса A (служебные слова / тексты ошибок валидации) — обязаны НЕ попасть в
# code_literals(), их переводит руками i18n_ui_en.py (план 27-02), не машина.
_TIER_A_SAMPLES = ("Отмена", "Напиши или нажми «Пропустить».", "Выбери «Да» или «Нет».")


def _db_ready(tmp_path, name="test_i18n_sources_27.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def test_non_delegate_groups_excluded():
    keys = i18n_sources.delegate_registry_keys()
    leaked = [
        key for key in keys
        if SETTINGS_SCHEMA[key].get("group") in _NON_DELEGATE_GROUPS
    ]
    assert leaked == [], f"админские/недоступные-до-анкеты группы протекли в корпус: {leaked}"


def test_admin_labels_excluded():
    keys = i18n_sources.delegate_registry_keys()
    for admin_key in _ADMIN_KEYS:
        assert admin_key in SETTINGS_SCHEMA, f"фикстура теста устарела: {admin_key} пропал из реестра"
        assert admin_key not in keys, f"{admin_key} — метка для менеджера, не текст для делегата"
        assert admin_key in i18n_sources.ADMIN_KEYS_IN_DELEGATE_GROUPS


def test_admin_rule_is_computed_not_hand_picked():
    """Правило «`admin` в имени ключа группы reg» должно находить исключения само — тест
    провалится, если кто-то вручную сузит фильтр до двух explicit-ключей вместо предиката."""
    computed = {
        key for key, spec in SETTINGS_SCHEMA.items()
        if spec.get("group") in i18n_sources.DELEGATE_GROUPS and "admin" in key
    }
    assert computed == i18n_sources.ADMIN_KEYS_IN_DELEGATE_GROUPS


def test_admin_only_game_keys_excluded():
    """Квик 260917-en: визард СОЗДАНИЯ задания (менеджерский экран) не должен протечь в
    делегатский корпус вместе с остальной группой `game`."""
    keys = i18n_sources.delegate_registry_keys()
    for admin_key in i18n_sources._ADMIN_ONLY_GAME_KEYS:
        assert admin_key in SETTINGS_SCHEMA, f"фикстура теста устарела: {admin_key} пропал из реестра"
        assert admin_key not in keys, f"{admin_key} — менеджерский экран, не текст для делегата"


def test_non_language_pay_event_keys_excluded():
    """Квик 260917-en: построчные данные (ЛК+реквизиты/дата+сумма) и идентификаторы
    (юзернейм/URL контактов) — не язык, машинному переводу не подлежат."""
    keys = i18n_sources.delegate_registry_keys()
    for key in (*i18n_sources._NON_LANGUAGE_PAY_KEYS, *i18n_sources._NON_LANGUAGE_EVENT_KEYS):
        assert key in SETTINGS_SCHEMA, f"фикстура теста устарела: {key} пропал из реестра"
        assert key not in keys, f"{key} — данные/идентификатор, не текст для перевода"


def test_delegate_facing_game_pay_event_keys_included():
    """Позитивная сторона Квик 260917-en: обычные (не исключённые явно) ключи `event`/`game`/
    `pay` ДОЛЖНЫ попадать в корпус — регресс-гвард на случай, если кто-то по ошибке вернёт
    групповое исключение вместо точечного."""
    keys = i18n_sources.delegate_registry_keys()
    for key in ("start_text", "game_task_list_empty", "payment_receipt_received_text"):
        assert key in keys, f"{key} должен быть в делегатском корпусе (Квик 260917-en)"


def test_is_delegate_dynamic_key_true_cases():
    for key in (
        "reg_prompt_goal",
        "reg_prompt_goal__party",
        "reg_prompt_goal__party__city__spb",
        "reg_help_city",
    ):
        assert i18n_sources.is_delegate_dynamic_key(key), key


def test_is_delegate_dynamic_key_false_cases():
    for key in ("sheet_tab_main", "game_task_text", "consent_list"):
        assert not i18n_sources.is_delegate_dynamic_key(key), key


def test_code_literals_covers_every_source_and_excludes_tier_a():
    literals = i18n_sources.code_literals()
    origins = {origin for origin, _text in literals}
    texts = {text for _origin, text in literals}

    expected_origin_prefixes = (
        "lit:PROMPT_DEFAULTS.", "lit:STEP_HELP.", "lit:STEP_HELP_EXAMPLES.",
        "lit:_GENERIC_FALLBACK_LABEL.", "lit:REG_LABELS.", "lit:reg_options.",
        "lit:config.UNIVERSITIES",
    )
    for prefix in expected_origin_prefixes:
        assert any(o.startswith(prefix) for o in origins), f"нет ни одного {prefix}*"

    # SELECT_CONFIG/MULTI_CONFIG дефолты: origin_key — сам option_key (city_options и т.п.)
    assert any(o == "lit:city_options" for o in origins)
    assert any(o == "lit:goal_options" for o in origins)

    for sample in _TIER_A_SAMPLES:
        assert sample not in texts, f"ярус A просочился в машинный корпус: {sample!r}"


def test_corpus_empty_db_does_not_crash_and_is_in_expected_range(tmp_path):
    _db_ready(tmp_path)
    result = asyncio.run(i18n_sources.corpus())
    assert result, "корпус не должен схлопнуться до нуля"
    # Research оценивает ~265 уникальных строк; 150-600 — коридор «не схлопнулось до нуля и
    # не разъехалось на весь реестр». Верхняя граница поднята с 400 квик-фиксом 260913: Phase 30
    # добавила ~90 ключей анкеты 2.0, корпус вырос до 467 — нижнюю границу (реальный признак
    # схлопывания) не трогаем.
    assert 150 <= len(result) <= 600, len(result)

    texts = [text for _origin, text in result]
    assert len(texts) == len(set(texts)), "дедупликация по strip()-нутому тексту не сработала"
    assert all(t.strip() == t and t != "-" for t in texts)


def test_corpus_no_admin_or_non_delegate_text_leaks(tmp_path):
    _db_ready(tmp_path)
    result = asyncio.run(i18n_sources.corpus())
    texts = {text for _origin, text in result}
    for admin_key in _ADMIN_KEYS:
        default = SETTINGS_SCHEMA[admin_key]["default"]
        assert default not in texts, f"{admin_key} default просочился в корпус"


def test_module_does_not_import_handlers():
    """AST-проверка реальных import-узлов (не грепом по докстрингу — тот СВОБОДНО обсуждает
    `handlers.x`, объясняя, почему модуль его не импортирует)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(i18n_sources))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.split(".")[0] == "handlers", alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or node.module.split(".")[0] != "handlers", node.module
