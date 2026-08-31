"""Phase 20 (20-01, ADMIN-IA-01..04) — сторож разделов админки по пути делегата.

Семь сюжетов:
  (1) покрытие «до/после» — ни одна кнопка, достижимая с корня или с экрана настроек до
      фазы 20, не пропала при переезде в разделы;
  (2) каждая кнопка живёт ровно в ОДНОМ разделе (иначе менеджер найдёт одну настройку в
      двух местах и не поймёт, какая «настоящая»);
  (3) видимость по правам — раздел рендерится тогда и только тогда, когда в нём есть хотя
      бы одна доступная строка;
  (4) экран раздела: шапка города, «← Назад» в корень, отсутствие тупиков;
  (5) корень /admin — только разделы, и «Назад» с каждого экрана ведёт в его раздел (20-03);
  (6) после действия менеджера (тумблер, правка значения, приём медиа, отмена, выход) экран
      возврата — тот, с которого действовали, а не исчезнувший плоский лендинг (20-04);
  (7) доки и встроенная справка не разъезжаются с раскладкой: путь к каждому пункту первой
      настройки события — не глубже трёх нажатий от /admin, а «где менять» в справке бота и
      подписи разделов в шпаргалке взяты из реестра, а не написаны от руки (20-05).

pytest-asyncio в этом окружении нет — каждый async-вызов через asyncio.run(), config.DB_PATH
смотрит в tmp_path (конвенция проекта, conftest.py нет).
"""
import asyncio
import re
from pathlib import Path

import pytest

from database import db
import cities
from handlers import admin_reg_config as regcfg
from handlers import admin_roles as roles
from handlers import admin_sections as sec
from handlers import admin_settings as st
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
        self.sent: list[tuple] = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **kwargs):
        # Тумблеры-модули дёргают напоминание о целях согласий через callback.message.answer.
        self.sent.append((text, reply_markup))


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


# ══════════════════════════════════════════════════════════════════════════════════════════
# (5) Корень переключён на разделы (20-03, ADMIN-IA-01)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_root_shows_at_most_nine_rows(tmp_path):
    """Критерий успеха №1: шапка города + не больше восьми кнопок-разделов. До фазы 20 на
    корне было 22 строки, и менеджер искал нужную глазами."""
    _roles_ready(tmp_path)
    _enable_cities()
    from handlers.admin_core import admin_keyboard_for

    kb = asyncio.run(admin_keyboard_for(ADMIN_ID))
    assert len(kb.inline_keyboard) <= 9, _flat_callback_data(kb)
    assert len(kb.inline_keyboard) == len(sec.SECTIONS) + 1  # суперадмину доступны все восемь


def test_root_rows_are_only_sections(tmp_path):
    """Ни одной старой операции на корне не осталось: всё, кроме шапки города, — разделы,
    и их порядок задаёт реестр (путь делегата), а не история появления фаз."""
    _roles_ready(tmp_path)
    _enable_cities()
    from handlers.admin_core import admin_keyboard_for

    flat = _flat_callback_data(asyncio.run(admin_keyboard_for(ADMIN_ID)))
    assert flat[0] == "admin_city_switch"
    assert flat[1:] == [f"admin_sec:{token}" for token, _label, _rows in sec.SECTIONS]


def test_root_of_moderate_reg_manager_is_only_the_applications_section(tmp_path):
    """Критерий успеха №2, сквозной (не только на уровне visible_sections): одно право —
    один раздел на корне, и никакого «пустого» экрана вокруг него."""
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    _only_caps("reg_manager", "moderate_reg")
    from handlers.admin_core import build_admin_keyboard

    flat = _flat_callback_data(asyncio.run(build_admin_keyboard(MANAGER_ID)))
    assert flat == ["admin_sec:apps"]


def test_root_of_a_stranger_stays_empty(tmp_path):
    """T-20-09: у человека без прав корень пуст — чужой список разделов не утекает."""
    _roles_ready(tmp_path)
    from handlers.admin_core import build_admin_keyboard

    assert asyncio.run(build_admin_keyboard(STRANGER_ID)).inline_keyboard == []


# Таблица «экран -> его раздел» — ФИКСАЦИЯ РЕШЕНИЯ (D-01), а `section_of` — её проверка
# против SECTIONS. Список литеральный и включает ВСЕ экраны фазы, а не только три, которые
# перенацелены планом 20-03: план 20-04 (модуль настроек) опирается на этот же тест как на
# контракт своих правок — если он уведёт «Назад» не туда, красным станет этот сторож.
BACK_TARGETS = {
    "admin_dashboard_settings": "data",
    "admin_miniapp_settings": "manage",
    "admin_roles": "manage",
    "admin_reg_prompts": "form",
    "admin_reg_questions": "form",
    "admin_menu_buttons": "event",
    "admin_consent_pdfs": "form",
    "settings_toggle_reg": "form",
    "settings_group:apps": "apps",
    "settings_group:event": "event",
}


def test_every_screen_back_target_is_its_section():
    bad = {cb: sec.section_of(cb) for cb, token in BACK_TARGETS.items()
           if sec.section_of(cb) != token}
    assert not bad, bad


def test_back_button_target_is_derived_not_a_second_map():
    """«Назад» строится ОДНОЙ функцией из реестра; подпись — просто «← Назад», потому что
    кнопка ведёт в раздел, а не «к настройкам»."""
    btn = sec.back_button("admin_roles")
    assert btn.callback_data == "admin_sec:manage"
    assert btn.text == "← Назад"
    assert sec.back_button("settings_group:apps").callback_data == "admin_sec:apps"


def test_back_button_falls_back_to_root_for_unknown_screen():
    """T-20-11: неизвестный экран не даёт тупика — «Назад» ведёт в существующий корень."""
    assert sec.section_of("нет-такого-экрана") is None
    assert sec.back_button("нет-такого-экрана").callback_data == "admin_menu"


# ══════════════════════════════════════════════════════════════════════════════════════════
# (6) После действия — тот же экран (20-04)
#
# Ревью фазы 20, BLOCKER 1: callback_data тумблеров намеренно не менялись (D-02), поэтому
# промах перерисовки не ловится НИЧЕМ, кроме прямой проверки клавиатуры — и старый плоский
# экран, и экран раздела отвечают на те же самые callback'и. Правило всех тестов ниже:
# сравнить МНОЖЕСТВО callback_data полученной клавиатуры с множеством эталона И дополнительно
# убедиться, что оно НЕ равно множеству плоского лендинга. Второе утверждение обязательно:
# без него тест прошёл бы и на лендинге, окажись тот случайно надмножеством.
# ══════════════════════════════════════════════════════════════════════════════════════════

class FakePhoto:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeDocument:
    def __init__(self, file_id, mime_type):
        self.file_id = file_id
        self.mime_type = mime_type


class FakeAnswerMessage:
    """Сообщение ОТ менеджера (пути message-хендлеров): текст/фото/файл на входе, ответы бота
    на выходе. Экран возврата — последний ответ С клавиатурой: подтверждение «✅ Фото
    обновлено!» приходит отдельным сообщением и клавиатуры не несёт."""

    def __init__(self, text=None, user_id=ADMIN_ID, photo=None, document=None):
        self.text = text
        self.html_text = text
        self.caption = None
        self.photo = photo
        self.document = document
        self.from_user = FakeUser(user_id)
        self.sent: list[tuple] = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))

    @property
    def screen(self) -> tuple:
        with_kb = [pair for pair in self.sent if pair[1] is not None]
        assert with_kb, f"бот не прислал ни одного экрана: {self.sent}"
        return with_kb[-1]


class FakeState:
    def __init__(self, data=None, state=None):
        self._data = dict(data or {})
        self.state = state
        self.cleared = False

    async def get_data(self):
        return dict(self._data)

    async def get_state(self):
        # aiogram отдаёт СТРОКУ («EditSetting:waiting_for_file»), а фейк хранит объект State —
        # существующие тесты сверяют именно объект. Приводим на выходе, чтобы хендлеры видели
        # ровно то, что увидят в проде.
        return getattr(self.state, "state", self.state)

    async def clear(self):
        self._data = {}
        self.state = None
        self.cleared = True

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_data(self, data):
        self._data = dict(data)

    async def set_state(self, state):
        self.state = state


def _cbs(kb) -> set:
    return {cd for cd in _flat_callback_data(kb) if cd}


def _assert_is_section_screen(kb, token: str):
    assert _cbs(kb) == _cbs(asyncio.run(sec.build_section_keyboard(token, ADMIN_ID))), token
    assert _cbs(kb) != _cbs(asyncio.run(st.build_settings_keyboard(ADMIN_ID))), token


def _assert_is_group_screen(kb, token: str):
    assert _cbs(kb) == _cbs(asyncio.run(st.build_settings_group_keyboard(token, ADMIN_ID))), token
    assert _cbs(kb) != _cbs(asyncio.run(st.build_settings_keyboard(ADMIN_ID))), token


@pytest.mark.parametrize("handler_name,callback_data,section", [
    ("toggle_full_approval", "settings_toggle_full_approval", "apps"),
    ("toggle_nudge_enabled", "toggle_nudge_enabled", "apps"),
    ("toggle_uni_mode", "toggle_uni_mode", "form"),
    ("toggle_payment_enabled", "toggle_payment_enabled", "pay"),
    ("toggle_bonus", "settings_toggle_bonus", "event"),
])
def test_toggle_inside_section_redraws_that_section(tmp_path, handler_name, callback_data, section):
    """Тумблеры из четырёх разных разделов и из трёх разных generic-хелперов: после тапа
    менеджер остаётся ТАМ, где нажал, а не проваливается на плоский экран из 26 кнопок."""
    _roles_ready(tmp_path)
    cb = FakeCallback(callback_data)
    asyncio.run(getattr(st, handler_name)(cb))

    assert cb.message.edit_calls == 1
    assert sec.section_of(callback_data) == section  # раздел выведен из реестра, не задан
    _assert_is_section_screen(cb.message.markup, section)


def test_edit_value_returns_to_its_group_screen(tmp_path):
    """Правка значения возвращает на экран ГРУППЫ: кнопка «✏️ …» живёт только там. Заодно
    сторожит перекладку ключа — «После одобрения» после 20-01 лежит в «📋 Заявки»."""
    _roles_ready(tmp_path)
    msg = FakeAnswerMessage("Ждём тебя на форуме!")
    asyncio.run(st.settings_edit_value(msg, FakeState({"setting_key": "approve_text"})))

    assert asyncio.run(db.get_setting("approve_text")) == "Ждём тебя на форуме!"
    _assert_is_group_screen(msg.screen[1], "apps")


def test_cancel_returns_to_the_screen_it_came_from(tmp_path):
    """Отмена с контекстом — на экран группы; отмена БЕЗ контекста (данные FSM протухли после
    рестарта, MemoryStorage) — на корень разделов, а не на плоский лендинг и не в тупик."""
    _roles_ready(tmp_path)

    cb = FakeCallback("settings_cancel")
    asyncio.run(st.cancel_edit_setting_callback(cb, FakeState({"setting_key": "approve_text"})))
    _assert_is_group_screen(cb.message.markup, "apps")

    cb_empty = FakeCallback("settings_cancel")
    asyncio.run(st.cancel_edit_setting_callback(cb_empty, FakeState()))
    flat = _flat_callback_data(cb_empty.message.markup)
    assert flat and all(cd.startswith("admin_sec:") for cd in flat), flat


def test_photo_and_file_return_to_event_group(tmp_path):
    """Приём медиа возвращает на экран группы, где живут кнопки «📷 …»/«📎 …»; PDF согласия
    ключом SETTINGS_SCHEMA не является, поэтому его ветка возвращает в раздел-владелец
    экрана «🧾 PDF согласий»."""
    _roles_ready(tmp_path)

    photo_msg = FakeAnswerMessage(photo=[FakePhoto("photo-file-id")])
    asyncio.run(st.settings_receive_photo(photo_msg, FakeState({"photo_setting": "start"})))
    assert asyncio.run(db.get_setting("start_photo_file_id")) == "photo-file-id"
    _assert_is_group_screen(photo_msg.screen[1], "event")

    doc_msg = FakeAnswerMessage(document=FakeDocument("doc-file-id", "application/pdf"))
    asyncio.run(st.settings_receive_file_doc(doc_msg, FakeState({"file_setting": "reg_bonus"})))
    assert asyncio.run(db.get_setting("reg_bonus_doc_file_id")) == "doc-file-id"
    _assert_is_group_screen(doc_msg.screen[1], "event")

    pdf_msg = FakeAnswerMessage(document=FakeDocument("pdf-file-id", "application/pdf"))
    asyncio.run(st.settings_receive_file_doc(pdf_msg, FakeState({"raw_file_key": "consent_pdf_offer"})))
    assert asyncio.run(db.get_setting("consent_pdf_offer")) == "pdf-file-id"
    _assert_is_section_screen(pdf_msg.screen[1], "form")


def test_reg_config_exits_land_in_their_sections(tmp_path):
    """BLOCKER 2. Эти два выхода вызывают перерисовку ФУНКЦИЕЙ, а не кнопкой, поэтому греп по
    кнопкам их не видел вовсе — сторож нужен именно здесь."""
    _roles_ready(tmp_path)

    cb_q = FakeCallback("reg_q_back")
    asyncio.run(regcfg.reg_questions_back(cb_q))
    _assert_is_section_screen(cb_q.message.markup, "form")

    cb_m = FakeCallback("menu_back")
    asyncio.run(regcfg.menu_buttons_back(cb_m))
    _assert_is_section_screen(cb_m.message.markup, "event")


def test_no_handler_redraws_the_flat_settings_screen():
    """Структурный сторож — защита от возврата бага при следующей правке модуля настроек:
    единственное упоминание `build_settings_keyboard(` на весь handlers/ — её объявление."""
    from pathlib import Path

    handlers_dir = Path(__file__).resolve().parent.parent / "handlers"
    settings_src = (handlers_dir / "admin_settings.py").read_text(encoding="utf-8")
    regcfg_src = (handlers_dir / "admin_reg_config.py").read_text(encoding="utf-8")

    assert settings_src.count("build_settings_keyboard(") == 1, "перерисовка снова целится в лендинг"
    assert "async def build_settings_keyboard(" in settings_src  # это именно объявление
    assert regcfg_src.count("build_settings_keyboard(") == 0


def test_legacy_admin_settings_lands_on_root(tmp_path):
    """D-03: кнопка «⚙️ Настройки форума» из клавиатуры, отрисованной до фазы 20 и живущей в
    чате вечно, открывает корень разделов И объясняет, что изменилось, — не тупик и не
    молчаливый корень."""
    _roles_ready(tmp_path)
    cb = FakeCallback("admin_settings")
    asyncio.run(st.show_admin_settings(cb))

    assert cb.message.edit_calls == 1
    assert any(cd.startswith("admin_sec:") for cd in _flat_callback_data(cb.message.markup))
    assert "переехали" in cb.message.text


# (7) Доки и встроенная справка не разъезжаются с раскладкой (20-05, ADMIN-IA-04)

# Пункты «Первой настройки события» из ADMIN_CHEATSHEET.md, которые делаются В БОТЕ. Первый
# пункт списка (аватар и описание бота) сюда не входит: он делается в @BotFather, вне нашего
# бота, и глубины в кнопках /admin у него нет по определению.
# Пара: человеческое имя пункта -> идентификатор кнопки, до которой менеджеру нужно дойти.
FIRST_SETUP: list[tuple[str, str]] = [
    ("Приветствие", "settings_edit:start_text"),
    ("Фото приветствия", "settings_photo:start"),
    ("Инфо о форуме (дата, время, место, адрес)", "settings_edit:event_date"),
    ("Контакты", "settings_edit:contact_person"),
    ("Кнопки меню делегата", "admin_menu_buttons"),
    ("Тексты вопросов анкеты", "admin_reg_prompts"),
    ("Текст после одобрения", "settings_edit:approve_text"),
    ("Текст при отклонении", "settings_edit:reject_text"),
]


def _taps_from_admin(target: str) -> tuple[int, str | None]:
    """Сколько нажатий от /admin до кнопки `target` и в каком разделе она живёт.

    Глубина считается ПО СТРУКТУРЕ реестра, а не кликами по живой клавиатуре:
      строка раздела (op / screen / screen_admin / toggle) — 2 нажатия (раздел, затем строка);
      поле внутри группы настроек (settings_edit / settings_photo / settings_file) — 3
      (раздел, кнопка группы, само поле).
    Неизвестная кнопка возвращает заведомо провальную глубину и None вместо раздела — тест
    обязан упасть, а не молча посчитать её достижимой."""
    token = sec.section_of(target)
    if token:
        return 2, token
    if target.startswith("settings_edit:"):
        group = st._group_of_setting_key(target.split(":", 1)[1])
    elif target.startswith(("settings_photo:", "settings_file:")):
        group = st.PHOTO_FILE_GROUP
    else:
        return 99, None
    return (3, sec.section_of(group)) if group else (99, None)


def test_first_setup_within_three_taps():
    """Критерий успеха №3 ROADMAP фазы 20 — путь к каждому пункту первой настройки события не
    глубже трёх нажатий от /admin. Проверяется по SECTIONS, поэтому это утверждение, за
    которое отвечает раскладка, а не обещание в тексте: блок «Первая настройка события» в
    ADMIN_CHEATSHEET.md только пересказывает менеджеру то, что посчитано здесь."""
    assert len(FIRST_SETUP) == 8, "восемь пунктов в боте + BotFather = девять в шпаргалке"
    for name, target in FIRST_SETUP:
        taps, token = _taps_from_admin(target)
        assert token is not None, f"{name}: «{target}» не объявлена ни в одном разделе"
        assert taps <= 3, f"{name}: {taps} нажатий до «{target}» (раздел {token})"


def test_settings_guide_where_starts_with_a_real_section():
    """T-20-14: «📖 Справка по настройкам» печатает менеджеру, ГДЕ менять каждую настройку.
    Путь обязан начинаться с подписи существующего раздела — переименуют раздел, забыв про
    справку, и этот тест покраснеет раньше, чем менеджер уткнётся в несуществующую кнопку."""
    labels = [label for _token, label, _rows in sec.SECTIONS]
    for _title, _subtitle, entries in roles.SETTINGS_GUIDE_SECTIONS:
        for entry in entries:
            where = entry["where"]
            assert "⚙️ Настройки →" not in where, entry["key"]
            assert any(where.startswith(label) for label in labels), (entry["key"], where)


def test_cheatsheet_covers_every_section():
    """Сторож от тихого расхождения доки ↔ код: подпись раздела правится в SECTIONS, шпаргалка
    обязана ехать следом. Плюс состав блока «Первая настройка события» — девять пунктов, и
    первый из них про BotFather (единственный шаг вне бота)."""
    text = (Path(__file__).resolve().parent.parent / "ADMIN_CHEATSHEET.md").read_text(encoding="utf-8")
    for _token, label, _rows in sec.SECTIONS:
        assert label in text, label

    assert "## Первая настройка события" in text
    block = text.split("## Первая настройка события", 1)[1].split("\n## ", 1)[0]
    numbered = [line for line in block.splitlines() if re.match(r"^\d+\. ", line)]
    assert len(numbered) == 9, numbered
    assert "BotFather" in numbered[0], numbered[0]
