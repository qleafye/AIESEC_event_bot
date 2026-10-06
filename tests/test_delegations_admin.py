"""Экран «🏫 Делегации» в «📋 Заявки» (handlers/admin_delegations.py): вход под `moderate_reg`,
выбор формы делегаций из подключённых форм, подтверждение ключевых вопросов по подписям,
счётчики и сводка по вузам, предупреждения листа, настройки модуля кнопками экрана
(тумблер геймификации, дата отсечки ЦА, курсы не ЦА, тексты делегату).

Фейки — по образцу tests/test_delete_user_260910.py; хендлеры вызываются напрямую функцией.
pytest-asyncio в проекте нет — async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import delegations_db as ddb
from database import ext_forms_db as ef
from handlers import admin_delegations as mod
from handlers import admin_sections as sec
from handlers.admin_caps import required_capability
from services import delegations as dlg
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed
from tests._dbtpl import fast_init_db

ADMIN = 7
QUESTIONS = [
    ("q1", "ФИО"), ("q2", "Возраст"), ("q3", "Электронная почта"), ("q4", "Университет"),
    ("q5", "Направление"), ("q6", "Курс обучения"), ("q7", "Ник в телеграмме (через @)"),
]


def _run(coro):
    return asyncio.run(coro)


# ── фейки ─────────────────────────────────────────────────────────────────────────────────

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeEditableMessage:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, parse_mode, reply_markup))

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class _FakeMessage:
    def __init__(self, text, user_id=ADMIN):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class _FakeCallback:
    def __init__(self, data, user_id=ADMIN):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeEditableMessage()
        self.answer_calls = []

    async def answer(self, text=None, show_alert=False):
        self.answer_calls.append((text, show_alert))


def _buttons(kb) -> list:
    return [b for row in kb.inline_keyboard for b in row]


def _callbacks(kb) -> list[str]:
    return [b.callback_data for b in _buttons(kb)]


def _button_texts(kb) -> list[str]:
    return [b.text for b in _buttons(kb)]


def _last_edit(cb: _FakeCallback):
    return cb.message.edits[-1]


def _state(storage=None) -> FSMContext:
    storage = storage or MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))


# ── окружение ─────────────────────────────────────────────────────────────────────────────

def _env(tmp_path, name="dlg_admin.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _form(title="Делегации", *, questions=QUESTIONS, username_q="q7") -> int:
    async def go():
        fid = await ef.create_form(platform="yandex", external_id=f"ext-{title}", title=title,
                                   key_username_q=username_q)
        await ef.upsert_columns(fid, list(questions))
        return fid
    return _run(go())


def _select(fid: int, *, keys: bool = True) -> None:
    async def go():
        await set_setting_by_admin(None, "delegation_form_id", str(fid))
        if keys:
            await set_setting_by_admin(None, "delegation_q_fullname", "q1")
            await set_setting_by_admin(None, "delegation_q_university", "q4")
            await set_setting_by_admin(None, "delegation_q_course", "q6")
            await set_setting_by_admin(None, "delegation_q_email", "q3")
    _run(go())


def _eval(fid: int, aid: str, status: str, university: str, *, linked: int | None = None):
    async def go():
        await ef.insert_answer(form_id=fid, answer_id=aid, answered_at="2026-10-01 12:00:00",
                               received_at="2026-10-01 12:00:01",
                               payload=[{"q": "q4", "label": "Университет", "value": university}],
                               raw=None, matched_telegram_id=None, match_how=None)
        row_id = await ddb.upsert_eval(form_id=fid, answer_id=aid, ta_status=status,
                                       university=university, course_raw="1",
                                       course_canonical="1", username_needle=None,
                                       answered_at="2026-10-01 12:00:00")
        if linked is not None:
            await ddb.link(row_id, linked, "manual")
    _run(go())


def _spy_sweep(monkeypatch) -> list:
    calls: list = []

    async def fake_sweep(limit: int = 200, *, reevaluate: bool = False):
        calls.append(reevaluate)
        return {"evaluated": 0, "linked": 0, "rechecked": 0}

    monkeypatch.setattr(dlg, "sweep_pending", fake_sweep)
    return calls


# ── каркас: раздел, права, «Назад» ────────────────────────────────────────────────────────

def test_screen_row_lives_in_apps_section_and_back_goes_to_apps():
    apps = next(rows for token, _label, rows in sec.SECTIONS if token == "apps")
    assert ("screen", "admin_delegations", "🏫 Делегации") in apps
    assert sec.back_button("admin_delegations").callback_data == "admin_sec:apps"
    assert [t for t, _, _ in sec.SECTIONS] == [
        "event", "form", "apps", "pay", "comms", "game", "amb", "data", "manage"]


def test_capabilities_are_moderate_reg():
    assert required_capability(callback_data="admin_delegations") == "moderate_reg"
    assert required_capability(callback_data="dlg_univ:0") == "moderate_reg"
    assert required_capability(callback_data="dlg_form:3") == "moderate_reg"
    assert required_capability(raw_state="DelegationEdit:waiting_cutoff") == "moderate_reg"
    assert required_capability(raw_state="DelegationLink:waiting_for_person") == "moderate_reg"


def test_no_form_screen_explains_what_to_do(tmp_path):
    _env(tmp_path)
    cb = _FakeCallback("admin_delegations")
    _run(mod.admin_delegations(cb))
    text, mode, kb = _last_edit(cb)
    assert mode == "HTML"
    assert "Форма делегаций не выбрана" in text
    assert "📊 Данные → 📝 Внешние формы" in text
    assert "📝 Выбрать форму" in _button_texts(kb)
    assert "admin_sec:apps" in _callbacks(kb)
    assert "settings_group:apps" not in _callbacks(kb)
    assert not any(c.startswith("settings_edit:") for c in _callbacks(kb))


def test_form_gone_screen(tmp_path):
    _env(tmp_path)
    _run(set_setting_by_admin(None, "delegation_form_id", "999"))
    cb = _FakeCallback("admin_delegations")
    _run(mod.admin_delegations(cb))
    text, _, kb = _last_edit(cb)
    assert "Форма отключена или удалена" in text
    assert "dlg_form_pick" in _callbacks(kb)


# ── выбор формы и ключевые вопросы ────────────────────────────────────────────────────────

def test_form_pick_lists_forms_or_alerts(tmp_path):
    _env(tmp_path)
    cb = _FakeCallback("dlg_form_pick")
    _run(mod.dlg_form_pick(cb))
    assert cb.answer_calls == [(mod._NO_FORMS_ALERT, True)]
    assert cb.message.edits == []
    fid = _form("Делегации <МГУ>")
    cb = _FakeCallback("dlg_form_pick")
    _run(mod.dlg_form_pick(cb))
    text, _, kb = _last_edit(cb)
    assert f"dlg_form:{fid}" in _callbacks(kb)
    assert "📝 Делегации <МГУ>" in _button_texts(kb)  # кнопки — без HTML, текст как есть


def test_pick_form_sets_id_and_guesses_questions_by_labels(tmp_path):
    _env(tmp_path)
    fid = _form()
    cb = _FakeCallback(f"dlg_form:{fid}")
    _run(mod.dlg_form(cb))
    assert _run(dlg.delegation_form_id()) == fid
    keys = _run(dlg.field_keys())
    assert keys == {"fullname": "q1", "university": "q4", "course": "q6", "email": "q3"}
    text, _, kb = _last_edit(cb)
    assert "Проверьте вопросы формы" in text
    assert "ФИО — вопрос «ФИО»" in text
    assert "Вуз — вопрос «Университет»" in text
    assert "Курс — вопрос «Курс обучения»" in text
    assert "Почта — вопрос «Электронная почта»" in text
    assert "Ник в Telegram — вопрос «Ник в телеграмме (через @)» (из настроек формы)" in text
    assert "q1" not in text and "q4" not in text  # коды менеджеру не показываются
    assert "✅ Верно" in _button_texts(kb)
    assert "✏️ Другой вопрос для курса" in _button_texts(kb)
    assert "dlg_key:course" in _callbacks(kb)


def test_pick_unknown_form_alerts(tmp_path):
    _env(tmp_path)
    cb = _FakeCallback("dlg_form:424242")
    _run(mod.dlg_form(cb))
    assert cb.answer_calls == [(mod._FORM_NOT_FOUND, True)]
    assert _run(dlg.delegation_form_id()) is None


def test_switch_form_disables_previous_export_mirror(tmp_path):
    _env(tmp_path)
    prev = _form("Старая")
    new = _form("Новая")
    _select(prev)
    _run(ef.set_form_mirror(prev, "UR REGS", None))
    _run(ef.set_form_mirror_mode(prev, "yandex_export"))
    _run(ef.set_form_mirror_warning(prev, "Вопросу «Х» нет колонки"))
    _run(mod.dlg_form(_FakeCallback(f"dlg_form:{new}")))
    p = _run(ef.get_form(prev))
    assert p["mirror_tab"] is None
    assert p["mirror_mode"] == "bot"
    assert p["mirror_warning"] is None
    assert _run(dlg.delegation_form_id()) == new
    # новая форма зеркало не наследует
    n = _run(ef.get_form(new))
    assert n["mirror_tab"] is None and n["mirror_mode"] == "bot"


def test_repick_same_form_keeps_its_export_mirror(tmp_path):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _run(ef.set_form_mirror(fid, "UR REGS", None))
    _run(ef.set_form_mirror_mode(fid, "yandex_export"))
    _run(mod.dlg_form(_FakeCallback(f"dlg_form:{fid}")))
    f = _run(ef.get_form(fid))
    assert f["mirror_tab"] == "UR REGS" and f["mirror_mode"] == "yandex_export"


def test_key_picker_lists_labels_and_keyset_saves_or_clears(tmp_path):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    cb = _FakeCallback("dlg_key:course")
    _run(mod.dlg_key(cb))
    text, _, kb = _last_edit(cb)
    assert "курса" in text
    assert "Курс обучения" in _button_texts(kb)
    assert "dlg_keyset:course:5" in _callbacks(kb)
    assert "dlg_keyset:course:none" in _callbacks(kb)
    # другой вопрос
    _run(mod.dlg_keyset(_FakeCallback("dlg_keyset:course:4")))
    assert _run(dlg.field_keys())["course"] == "q5"
    # «Такого вопроса нет» — ключ снимается
    cb = _FakeCallback("dlg_keyset:course:none")
    _run(mod.dlg_keyset(cb))
    assert _run(dlg.field_keys())["course"] is None
    assert "Курс — не выбран" in _last_edit(cb)[0]
    # устаревший индекс
    cb = _FakeCallback("dlg_keyset:course:77")
    _run(mod.dlg_keyset(cb))
    assert cb.answer_calls == [(mod._STALE_QUESTIONS, True)]
    assert _run(dlg.field_keys())["course"] is None
    # мусор в callback
    cb = _FakeCallback("dlg_keyset:phone:1")
    _run(mod.dlg_keyset(cb))
    assert cb.answer_calls[-1][1] is True


def test_keys_ok_requires_fullname_university_course(tmp_path, monkeypatch):
    _env(tmp_path)
    fid = _form()
    _select(fid, keys=False)
    calls = _spy_sweep(monkeypatch)
    cb = _FakeCallback("dlg_keys_ok")
    _run(mod.dlg_keys_ok(cb))
    assert cb.answer_calls == [(mod._KEYS_MISSING, True)]
    assert cb.message.edits == [] and calls == []


def test_keys_ok_spawns_sweep_and_renders_screen_with_counters(tmp_path, monkeypatch):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _eval(fid, "a1", "ok", "МГУ", linked=101)
    _eval(fid, "a2", "ok", "МГУ")
    _eval(fid, "a3", "no", "ВШЭ")
    _eval(fid, "a4", "check", "ВШЭ")
    calls = _spy_sweep(monkeypatch)

    async def go():
        cb = _FakeCallback("dlg_keys_ok")
        await mod.dlg_keys_ok(cb)
        await asyncio.sleep(0)
        return cb
    cb = _run(go())
    assert calls == [True]
    assert cb.answer_calls == [(mod._KEYS_SAVED, False)]
    text, _, kb = _last_edit(cb)
    assert "📝 Форма: Делегации" in text
    assert "В форме: 4 · ЦА: 2 · не ЦА: 1 · проверить: 1 · зашли в бота: 1 · пришли: 0" in text
    assert "Вопросы: ФИО ✓ · вуз ✓ · курс ✓ · почта ✓" in text
    assert "Подтвердите вопросы формы" not in text
    texts = _button_texts(kb)
    assert "❔ Проверить курс (1)" in texts
    assert "⏳ Не зашли (1)" in texts
    assert "🏫 По вузам" in texts and "📋 Лист UR REGS" in texts
    cbs = _callbacks(kb)
    for expected in ("dlg_review:0", "dlg_absent:0", "dlg_univ:0", "dlg_sheet", "dlg_game",
                     "dlg_cutoff", "dlg_courses", "dlg_text", "dlg_keys", "dlg_form_pick",
                     "admin_sec:apps"):
        assert expected in cbs, expected
    assert "settings_group:apps" not in cbs
    assert not any(c.startswith("settings_edit:") for c in cbs)


def test_screen_warns_about_unconfirmed_keys_and_sheet_state(tmp_path):
    _env(tmp_path)
    fid = _form("Форма <b>")
    _select(fid, keys=False)
    cb = _FakeCallback("admin_delegations")
    _run(mod.admin_delegations(cb))
    text, _, _ = _last_edit(cb)
    assert "📝 Форма: Форма &lt;b&gt;" in text
    assert "Вопросы: ФИО — · вуз — · курс — · почта —" in text
    assert "⚠️ Подтвердите вопросы формы" in text
    assert "📋 Лист: не выбран — нажмите «📋 Лист UR REGS»" in text


def test_screen_shows_sheet_warning_and_error(tmp_path):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _run(ef.set_form_mirror(fid, "UR REGS", None))
    _run(ef.set_form_mirror_mode(fid, "yandex_export"))
    _run(ef.set_form_mirror_warning(fid, "Вопросу «Город» нет колонки в листе <до L>"))
    cb = _FakeCallback("admin_delegations")
    _run(mod.admin_delegations(cb))
    text = _last_edit(cb)[0]
    assert "📋 Лист: «UR REGS» · запись включена" in text
    assert "⚠️ Вопросу «Город» нет колонки в листе &lt;до L&gt;" in text
    assert "⛔" not in text
    _run(ef.set_form_mirror(fid, "UR REGS", "Колонка M в листе занята «Комментарий»"))
    cb = _FakeCallback("admin_delegations")
    _run(mod.admin_delegations(cb))
    text = _last_edit(cb)[0]
    assert "⛔ Запись остановлена: Колонка M в листе занята «Комментарий»" in text


def test_univ_summary_renders_lines_and_pages(tmp_path):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    cb = _FakeCallback("dlg_univ:0")
    _run(mod.dlg_univ(cb))
    assert "Пока ни одного ответа ЦА в форме." in _last_edit(cb)[0]
    for i in range(10):
        _eval(fid, f"u{i}", "ok", f"Вуз {i:02d}")
    _eval(fid, "x1", "ok", "Вуз 00", linked=5)
    _eval(fid, "x2", "no", "Вуз 00")
    cb = _FakeCallback("dlg_univ:0")
    _run(mod.dlg_univ(cb))
    text, _, kb = _last_edit(cb)
    assert "Вуз 00 — ЦА 2 · в боте 1 · пришли 0" in text
    assert text.index("Вуз 00") < text.index("Вуз 01")  # сортировка: по числу ЦА
    assert "dlg_univ:8" in _callbacks(kb)
    assert "admin_delegations" in _callbacks(kb)
    cb = _FakeCallback("dlg_univ:8")
    _run(mod.dlg_univ(cb))
    text, _, kb = _last_edit(cb)
    assert "Вуз 09" in text and "Вуз 00" not in text
    assert "dlg_univ:0" in _callbacks(kb)
