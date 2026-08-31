"""Phase 20 (20-01, ADMIN-IA-01..03) — сторож разделов админки по пути делегата.

Четыре сюжета:
  (1) покрытие «до/после» — ни одна кнопка, достижимая с корня или с экрана настроек до
      фазы 20, не пропала при переезде в разделы;
  (2) каждая кнопка живёт ровно в ОДНОМ разделе (иначе менеджер найдёт одну настройку в
      двух местах и не поймёт, какая «настоящая»);
  (3) видимость по правам — раздел рендерится тогда и только тогда, когда в нём есть хотя
      бы одна доступная строка;
  (4) экран раздела: шапка города, «← Назад» в корень, отсутствие тупиков.

pytest-asyncio в этом окружении нет — каждый async-вызов через asyncio.run(), config.DB_PATH
смотрит в tmp_path (конвенция проекта, conftest.py нет).
"""
import asyncio

from database import db
import cities
from handlers import admin_sections as sec
from handlers.admin_caps import role_caps_key
from handlers.admin_settings import settings_toggle_rows

from tests.test_roles_phase8 import (
    ADMIN_ID,
    MANAGER_ID,
    STRANGER_ID,
    _flat_callback_data,
    _roles_ready,
)


# ── Fake-объекты (в проекте нет pytest-asyncio и нет aiogram-моков) ────────────────────────

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


def _only_caps(role: str, caps: str):
    """Сузить набор прав роли до перечисленных (role_caps_* хранится построчно)."""
    asyncio.run(db.set_setting(role_caps_key(role), caps))


# ══════════════════════════════════════════════════════════════════════════════════════════
# (1) Покрытие «до/после» (ADMIN-IA-02)
# ══════════════════════════════════════════════════════════════════════════════════════════

# Множество точек входа, достижимых с корня /admin и с экрана настроек ДО фазы 20.
# ЛИТЕРАЛЫ, а не вычисление из кода: сторож, который собирает «до» из того же источника,
# что и «после», самообнуляется — он всегда зелёный и ничего не стережёт.
#
# `admin_settings` и `settings_back` в это множество НЕ входят: это навигация СТАРОГО
# лендинга настроек, а не кнопка-функция. Её судьба (корень настроек остаётся живым
# callback'ом, на него ссылаются десятки «← Назад») — предмет отдельного теста обратной
# совместимости в плане 20-03, а не этого покрытия.
FROZEN_BEFORE = frozenset({
    # 21 строка _ADMIN_MENU_ROWS (22-я — сам admin_settings, см. комментарий выше)
    "admin_stats", "admin_monthly_stats", "admin_source_stats",
    "admin_export_csv", "admin_export_incomplete",
    "admin_applications", "admin_receipts", "admin_stuck_questions",
    "admin_broadcast", "admin_polls",
    "admin_sync_sheet", "admin_rebuild_sheet", "admin_dedupe_sheet",
    "admin_settings_guide", "admin_cities",
    "admin_game_tasks", "admin_game_review", "admin_coins_manual",
    "admin_coins_journal", "admin_game_sync_sheet", "admin_game_stats",
    # строки лендинга настроек: тумблеры…
    "settings_toggle_reg", "settings_regmode_reset", "settings_toggle_bonus",
    "settings_toggle_full_approval", "settings_toggle_short_approval",
    "settings_toggle_notify", "toggle_payment_enabled", "toggle_payment_reminders",
    "toggle_consent_enabled", "toggle_uni_mode", "toggle_edu_conditional",
    "toggle_show_progress", "toggle_party_enabled", "toggle_party_fork_question",
    "settings_toggle_party_approval", "toggle_preselect_enabled",
    "toggle_pending_reminder", "toggle_nudge_enabled",
    # …и входы в под-экраны
    "admin_consent_pdfs", "admin_event_preset", "admin_reg_questions", "admin_reg_prompts",
    "admin_menu_buttons", "admin_roles", "admin_dashboard_settings", "admin_miniapp_settings",
    # кнопки, жившие ВНУТРИ экрана группы «🎪 Событие/Медиа»
    "admin_season_reset", "admin_season_import",
    # группы настроек
    "settings_group:event",
    "settings_group:reg",
    "settings_group:sheets",
    "settings_group:pay",
    "settings_group:party",
    "settings_group:consent",
    "settings_group:game",
    "settings_group:system",
})

_GROUPS_BEFORE = frozenset(cb for cb in FROZEN_BEFORE if cb.startswith("settings_group:"))


def _structural_callbacks() -> set:
    """Проход №1 — по реестру: callback_data каждой объявленной строки каждого раздела."""
    return {sec.row_callback(row) for _token, _label, rows in sec.SECTIONS for row in rows}


def _rendered_callbacks(admin_id: int) -> set:
    """Проход №2 — по фактическому рендеру всех восьми экранов.

    ЭТОТ ПРОХОД НЕ УПРОЩАТЬ ДО СТРУКТУРНОГО. Часть строк раздела рождается УСЛОВНО, внутри
    других строк, и статический обход `SECTIONS` физически не может их произвести:
    «↩️ Как везде» (`settings_regmode_reset`) не объявлена строкой реестра — её создаёт
    `settings_toggle_rows` внутри строки ("toggle", "settings_toggle_reg") и только когда у
    города шапки есть собственное значение registration_mode. Тем же проходом ловятся любые
    будущие условные строки (например динамическая группа «📦 Прочие» в «🔧 Управление»).
    Сторож, построенный только на структуре, молча потерял бы их из фриза."""
    found = set()
    for token, _label, _rows in sec.SECTIONS:
        kb = asyncio.run(sec.build_section_keyboard(token, admin_id))
        found.update(cd for cd in _flat_callback_data(kb) if cd)
    return found


def _after_callbacks(tmp_path) -> set:
    """Объединение обоих проходов; рендер-проход прогоняется ДВАЖДЫ — обычным админом и
    админом, у чьего города шапки есть собственное значение registration_mode."""
    _roles_ready(tmp_path)
    after = _structural_callbacks() | _rendered_callbacks(ADMIN_ID)

    _enable_cities()
    code = cities.city_codes()[1]
    assert asyncio.run(cities.set_admin_city(ADMIN_ID, code))
    own_key = cities.per_city_key("registration_mode", code)
    asyncio.run(db.set_setting(own_key, "short"))
    after |= _rendered_callbacks(ADMIN_ID)
    return after


def test_coverage_frozen_before_is_fully_reachable(tmp_path):
    """Каждая кнопка, достижимая до фазы 20, достижима и после — ни одна не потерялась при
    переезде в разделы."""
    after = _after_callbacks(tmp_path)
    missing = sorted(FROZEN_BEFORE - after)
    assert not missing, f"кнопки пропали при переезде в разделы: {missing}"


def test_coverage_apps_is_the_only_new_settings_group(tmp_path):
    """Единственное допустимое добавление сверх старого набора групп — «📋 Заявки»."""
    after = _after_callbacks(tmp_path)
    groups_after = {cb for cb in after if cb.startswith("settings_group:")}
    assert groups_after == _GROUPS_BEFORE | {"settings_group:apps"}


def test_coverage_regmode_reset_comes_only_from_the_render_pass(tmp_path):
    """Проверка, что рендер-проход живой, а не декоративный: «↩️ Как везде» есть в
    объединении, но его НЕТ в структурном множестве — значит второй проход реально
    доставляет то, чего структура дать не может."""
    _roles_ready(tmp_path)
    assert "settings_regmode_reset" not in _structural_callbacks()

    _enable_cities()
    code = cities.city_codes()[1]
    assert asyncio.run(cities.set_admin_city(ADMIN_ID, code))
    asyncio.run(db.set_setting(cities.per_city_key("registration_mode", code), "short"))
    assert "settings_regmode_reset" in _rendered_callbacks(ADMIN_ID)


def test_coverage_regmode_reset_absent_without_city_override(tmp_path):
    """Оборотная сторона: без городского override строки сброса нет вовсе — «↩️ Как везде»
    никогда не показывается, когда отменять нечего (идиома 09.3)."""
    _roles_ready(tmp_path)
    _enable_cities()
    code = cities.city_codes()[1]
    assert asyncio.run(cities.set_admin_city(ADMIN_ID, code))
    assert "settings_regmode_reset" not in _rendered_callbacks(ADMIN_ID)


# ══════════════════════════════════════════════════════════════════════════════════════════
# (2) Ровно один раздел на кнопку
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_every_callback_lives_in_exactly_one_section():
    seen: dict[str, str] = {}
    for token, _label, rows in sec.SECTIONS:
        for row in rows:
            cb = sec.row_callback(row)
            assert cb not in seen, f"{cb} встречается и в «{seen.get(cb)}», и в «{token}»"
            seen[cb] = token


def test_sections_are_the_eight_delegate_flow_steps():
    assert [t for t, _, _ in sec.SECTIONS] == [
        "event", "form", "apps", "pay", "comms", "game", "data", "manage"]
    # обратный индекс выведен из реестра, а не из второго словаря-литерала
    assert sec._section_of_group("apps") == "apps"
    assert sec._section_of_group("sheets") == "data"
    assert sec._section_of_group("system") == "manage"
    assert sec._section_of_group("misc") == "manage"
    assert sec._section_of_group("нет-такой-группы") is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# (3) Видимость по правам (ADMIN-IA-03)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_manager_with_moderate_reg_only_sees_the_applications_section(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    _only_caps("reg_manager", "moderate_reg")
    from handlers.admin_caps import resolve_capabilities

    caps = asyncio.run(resolve_capabilities(MANAGER_ID))
    assert caps == {"moderate_reg"}
    assert sec.visible_sections(caps, False) == [("apps", "📋 Заявки")]


def test_settings_holder_sees_every_settings_section(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    _only_caps("reg_manager", "settings")
    from handlers.admin_caps import resolve_capabilities

    caps = asyncio.run(resolve_capabilities(MANAGER_ID))
    tokens = [t for t, _ in sec.visible_sections(caps, False)]
    # «📢 Общение» — единственный раздел без единой настройки: там только рассылка и опросы,
    # и держатель `settings` в него не попадает (право `broadcast` он не держит).
    assert tokens == ["event", "form", "apps", "pay", "game", "data", "manage"]


def test_stranger_sees_no_sections(tmp_path):
    _roles_ready(tmp_path)
    from handlers.admin_caps import resolve_capabilities

    caps = asyncio.run(resolve_capabilities(STRANGER_ID))
    assert caps == set()
    assert sec.visible_sections(caps, False) == []


def test_apps_section_for_moderate_reg_has_operations_only():
    """Раздел рендерится, но внутри — только доступное: тумблеры и группы требуют `settings`."""
    rows = sec.visible_rows("apps", {"moderate_reg"}, False)
    assert [sec.row_callback(r) for r in rows] == ["admin_applications", "admin_stuck_questions"]
    assert not [r for r in rows if r[0] in ("toggle", "group")]


def test_season_reset_row_is_superadmin_only():
    caps = {"settings"}
    superadmin = [sec.row_callback(r) for r in sec.visible_rows("manage", caps, True)]
    plain = [sec.row_callback(r) for r in sec.visible_rows("manage", caps, False)]
    assert "admin_season_reset" in superadmin
    assert "admin_season_reset" not in plain
    # «Импорт прошлого события» суперадминства не требует (CONTEXT D, 07.3-06)
    assert "admin_season_import" in plain


# ══════════════════════════════════════════════════════════════════════════════════════════
# (4) Экран раздела: шапка, «Назад», отсутствие тупиков
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_every_section_keyboard_ends_with_back_to_root(tmp_path):
    _roles_ready(tmp_path)
    for token, _label, _rows in sec.SECTIONS:
        kb = asyncio.run(sec.build_section_keyboard(token, ADMIN_ID))
        last = kb.inline_keyboard[-1][0]
        assert last.callback_data == "admin_menu", token
        assert len(kb.inline_keyboard) > 1, f"{token}: экран из одной кнопки «Назад» — тупик"


def test_section_keyboard_has_city_header_when_module_on(tmp_path):
    _roles_ready(tmp_path)
    _enable_cities()
    code = cities.city_codes()[1]
    assert asyncio.run(cities.set_admin_city(ADMIN_ID, code))
    label = asyncio.run(cities.city_label(code))
    for token, _label, _rows in sec.SECTIONS:
        kb = asyncio.run(sec.build_section_keyboard(token, ADMIN_ID))
        header = kb.inline_keyboard[0][0]
        assert header.callback_data == "admin_city_switch", token
        assert header.text == f"🏙 Город: {label}", token


def test_section_keyboard_has_no_city_header_when_module_off(tmp_path):
    """Паритет 09.3: модуль городов выключен — строки шапки нет вовсе, а не пустая/«все»."""
    _roles_ready(tmp_path)
    for token, _label, _rows in sec.SECTIONS:
        kb = asyncio.run(sec.build_section_keyboard(token, ADMIN_ID))
        assert all(b.callback_data != "admin_city_switch"
                   for row in kb.inline_keyboard for b in row), token


def test_section_screen_renders_title_and_human_hint(tmp_path):
    _roles_ready(tmp_path)
    cb = FakeCallback("admin_sec:apps")
    asyncio.run(sec.show_admin_section(cb))

    assert cb.message.edit_calls == 1
    assert "<b>📋 Заявки</b>" in cb.message.text
    assert "после подачи" in cb.message.text
    assert "admin_applications" in _flat_callback_data(cb.message.markup)


def test_unavailable_section_answers_alert_without_editing(tmp_path):
    """Раздел без единой доступной строки не открывается: сообщение не трогается вовсе."""
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    _only_caps("reg_manager", "moderate_reg")

    cb = FakeCallback("admin_sec:data", user_id=MANAGER_ID)
    asyncio.run(sec.show_admin_section(cb))

    assert cb.message.edit_calls == 0
    assert cb.answers == [("Раздел недоступен.", True)]


def test_unknown_section_token_answers_alert_without_editing(tmp_path):
    _roles_ready(tmp_path)
    cb = FakeCallback("admin_sec:__evil__")
    asyncio.run(sec.show_admin_section(cb))

    assert cb.message.edit_calls == 0
    assert cb.answers == [("Раздел недоступен.", True)]
    # текст алерта не перечисляет существующие разделы и не отражает присланный токен
    assert "__evil__" not in (cb.answers[0][0] or "")


def test_toggle_rows_are_shared_with_the_settings_screen(tmp_path):
    """Тумблеры раздела приходят из того же settings_toggle_rows, что и старый экран —
    одна подпись, а не две разные копии одного тумблера."""
    _roles_ready(tmp_path)
    rows = asyncio.run(settings_toggle_rows(ADMIN_ID))
    kb = asyncio.run(sec.build_section_keyboard("pay", ADMIN_ID))
    texts = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    for cb_data in ("toggle_payment_enabled", "toggle_payment_reminders"):
        assert texts[cb_data] == rows[cb_data][0][0].text
