"""«📋 Лист UR REGS» на экране «🏫 Делегации» (handlers/admin_delegations_sheet.py): выбор
вкладки из реального списка листа, сухая сверка по ID ответа, включение записи только через
подтверждение, выключение; редирект пикера фазы внешних форм для формы делегаций.

Лист не трогается: `sheets.list_worksheet_titles` и `delegations_mirror.dry_run_sync`
подменяются. Фейки — из tests/test_delegations_admin.py. pytest-asyncio нет — `asyncio.run()`.
"""
from __future__ import annotations

import sqlite3

import pytest

from config import config
from database import ext_forms_db as ef
from handlers import admin_delegations_sheet as mod
from handlers.ext_forms import admin_ext_forms_setup as setup
from handlers.admin_caps import required_capability
from tests.test_delegations_admin import (
    _FakeCallback, _FakeEditableMessage, _button_texts, _callbacks, _env, _form, _last_edit,
    _run, _select, _state,
)

TITLES = ["Лист1", "Служебная", "UR REGS"]


def _dry(**over) -> dict:
    base = {
        "sheet_rows": 5, "matched": 3, "new": 2, "unknown_sheet_ids": 2,
        "duplicate_sheet_ids": 0, "header": [], "header_diff": [], "m_free": True,
        "m_header": "", "extra_questions": [], "grey_rows": 1, "green_rows": 1,
        "colours_ok": True, "mapped_questions": 7,
    }
    base.update(over)
    return base


@pytest.fixture
def sheet(monkeypatch):
    """Подменённый лист: список вкладок, служебные вкладки и результат сверки + журнал вызовов."""
    st = {"titles": list(TITLES), "hidden": {"Служебная"}, "dry": _dry(), "calls": [],
          "raise": None}

    async def titles():
        return st["titles"]

    async def hidden(form):
        return set(st["hidden"])

    def dry_run_sync(tab, columns, answer_ids):
        st["calls"].append((tab, [c["qkey"] for c in columns], list(answer_ids)))
        if st["raise"] is not None:
            raise st["raise"]
        return st["dry"]

    monkeypatch.setattr(mod.sheets, "list_worksheet_titles", titles)
    monkeypatch.setattr(setup, "protected_tab_titles", hidden)
    monkeypatch.setattr(mod.delegations_mirror, "dry_run_sync", dry_run_sync)
    return st


def _answer(fid: int, aid: str, *, sheet_state: str | None = None) -> None:
    async def go():
        await ef.insert_answer(form_id=fid, answer_id=aid, answered_at="2026-10-01 12:00:00",
                               received_at="2026-10-01 12:00:01",
                               payload=[{"q": "q1", "label": "ФИО", "value": "Тест"}],
                               raw=None, matched_telegram_id=None, match_how=None)
    _run(go())
    if sheet_state:
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("UPDATE external_form_answers SET sheet_state = ? WHERE form_id = ? "
                     "AND answer_id = ?", (sheet_state, fid, aid))
        conn.commit()
        conn.close()


def _sheet_states(fid: int) -> list[str]:
    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute("SELECT sheet_state FROM external_form_answers WHERE form_id = ? "
                        "ORDER BY answer_id", (fid,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _enabled(fid: int, tab: str = "UR REGS") -> None:
    _run(ef.set_form_mirror(fid, tab, None))
    _run(ef.set_form_mirror_mode(fid, "yandex_export"))


def _pick(fid: int, st=None, idx: int | None = None) -> tuple[_FakeCallback, object]:
    """dlg_sheet → dlg_tab:{idx} одним состоянием; возвращает последний callback и state."""
    st = st or _state()
    _run(mod.dlg_sheet(_FakeCallback("dlg_sheet"), st))
    tabs = _run(st.get_data())["dlg_tabs"]
    idx = tabs.index("UR REGS") if idx is None else idx
    cb = _FakeCallback(f"dlg_tab:{idx}")
    _run(mod.dlg_tab(cb, st))
    return cb, st


# ── выбор вкладки ─────────────────────────────────────────────────────────────────────────

def test_sheet_picker_lists_tabs_ur_regs_first_and_hides_service(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    st = _state()
    cb = _FakeCallback("dlg_sheet")
    _run(mod.dlg_sheet(cb, st))
    text, mode, kb = _last_edit(cb)
    assert mode == "HTML" and "Выберите вкладку" in text
    assert _button_texts(kb)[:2] == ["UR REGS", "Лист1"]
    assert "Служебная" not in _button_texts(kb)
    assert _callbacks(kb)[:2] == ["dlg_tab:0", "dlg_tab:1"]
    assert "admin_delegations" in _callbacks(kb)
    assert _run(st.get_data())["dlg_tabs"] == ["UR REGS", "Лист1"]


def test_sheet_picker_marks_current_tab_and_escapes(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    sheet["titles"] = ["<b>x</b>", "UR REGS"]
    _enabled(fid, "UR REGS")
    cb = _FakeCallback("dlg_sheet")
    _run(mod.dlg_sheet(cb, _state()))
    assert _button_texts(cb.message.edits[-1][2])[0] == "✓ UR REGS"
    assert "<b>x</b>" in _button_texts(cb.message.edits[-1][2])  # кнопка — текст как есть


def test_sheet_unavailable_explains(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    sheet["titles"] = None
    cb = _FakeCallback("dlg_sheet")
    _run(mod.dlg_sheet(cb, _state()))
    text, _, kb = _last_edit(cb)
    assert "Таблица недоступна" in text and "📊 Данные" in text
    assert _callbacks(kb) == ["admin_delegations"]


def test_sheet_without_form_falls_back_to_screen(tmp_path, sheet):
    _env(tmp_path)
    cb = _FakeCallback("dlg_sheet")
    _run(mod.dlg_sheet(cb, _state()))
    assert "Форма делегаций не выбрана" in _last_edit(cb)[0]
    assert sheet["calls"] == []


# ── сверка ────────────────────────────────────────────────────────────────────────────────

def test_tab_runs_dry_run_with_known_ids_and_shows_numbers(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _answer(fid, "b2")
    _answer(fid, "a1")
    cb, st = _pick(fid)
    assert sheet["calls"] == [("UR REGS", ["q1", "q2", "q3", "q4", "q5", "q6", "q7"],
                               ["a1", "b2"])]
    text, _, kb = _last_edit(cb)
    assert "Сверка с листом «UR REGS»" in text
    assert "В листе строк: 5" in text
    assert "Узнал по ID ответа: 3" in text
    assert "Добавлю новых: 2" in text
    assert "Не узнал в листе (строки без ответа в боте): 2" in text
    assert "Шапка A–L: ✅ совпадает" in text
    assert "Колонка M: ✅ свободна" in text
    assert "Серых строк (не ЦА): 1 · зелёных (не трогаю): 1" in text
    assert "Ни одна строка не узнана" not in text
    assert "✅ Включить запись в «UR REGS»" in _button_texts(kb)
    assert "dlg_write_on" in _callbacks(kb) and "dlg_check" in _callbacks(kb)
    assert "dlg_write_off" not in _callbacks(kb)  # запись ещё не включена
    data = _run(st.get_data())
    assert data["dlg_tab"] == "UR REGS" and data["dlg_dry"]["matched"] == 3


def test_zero_matched_warns_about_different_ids(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    sheet["dry"] = _dry(matched=0, new=7, sheet_rows=200)
    cb, _ = _pick(fid)
    text = _last_edit(cb)[0]
    assert "Ни одна строка не узнана по ID" in text
    assert "добавит 7 новых строк" in text
    assert "разработчик" not in text and "запись лучше не включать" in text


def test_occupied_column_m_hides_enable_button(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    sheet["dry"] = _dry(m_free=False, m_header="Оплата <b>")
    cb, _ = _pick(fid)
    text, _, kb = _last_edit(cb)
    assert "Колонка M: ⛔ занята («Оплата &lt;b&gt;») — освободите её" in text
    assert "dlg_write_on" not in _callbacks(kb)
    assert "dlg_check" in _callbacks(kb)


def test_header_diff_extra_questions_and_stored_warning(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _run(ef.set_form_mirror_warning(fid, "Вопросу «Согласие» нет колонки"))
    sheet["dry"] = _dry(header_diff=[("D", "Универ <x>", "Университет")],
                        extra_questions=["Согласие"], m_header="В боте", m_free=True,
                        colours_ok=False, duplicate_sheet_ids=2)
    cb, _ = _pick(fid)
    text = _last_edit(cb)[0]
    assert "Шапка A–L: ⚠️ отличается: D «Универ &lt;x&gt;» ≠ «Университет»" in text
    assert "Колонка M: ✅ уже «В боте»" in text
    assert "⚠️ Вопросы формы без колонки: «Согласие»" in text
    assert "⚠️ Вопросу «Согласие» нет колонки" in text
    assert "цвета не прочитаны" in text
    assert "Повторяющихся ID в листе: 2" in text


def test_tab_missing_and_read_failure_are_explained(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    sheet["dry"] = None
    cb, st = _pick(fid)
    text, _, kb = _last_edit(cb)
    assert "Вкладка «UR REGS» не найдена — выберите заново." in text
    assert "dlg_sheet" in _callbacks(kb)
    sheet["dry"] = _dry()
    sheet["raise"] = RuntimeError("quota")
    cb = _FakeCallback("dlg_check")
    _run(mod.dlg_check(cb, st))
    text, _, kb = _last_edit(cb)
    assert "Не получилось прочитать лист «UR REGS»" in text
    assert "dlg_check" in _callbacks(kb)


def test_stale_tab_index_alerts(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    st = _state()
    cb = _FakeCallback("dlg_tab:4")
    _run(mod.dlg_tab(cb, st))
    assert cb.answer_calls == [(mod._STALE_TABS, True)]
    assert cb.message.edits == [] and sheet["calls"] == []
    _run(mod.dlg_sheet(_FakeCallback("dlg_sheet"), st))
    cb = _FakeCallback("dlg_tab:9")
    _run(mod.dlg_tab(cb, st))
    assert cb.answer_calls == [(mod._STALE_TABS, True)]
    cb = _FakeCallback("dlg_tab:x")
    _run(mod.dlg_tab(cb, st))
    assert cb.answer_calls == [(mod._STALE_TABS, True)]


def test_check_without_picked_tab_uses_enabled_tab_or_picker(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    cb = _FakeCallback("dlg_check")
    _run(mod.dlg_check(cb, _state()))
    assert "Выберите вкладку" in _last_edit(cb)[0]  # ничего не выбрано — пикер
    assert sheet["calls"] == []
    _enabled(fid, "UR REGS")
    cb = _FakeCallback("dlg_check")
    _run(mod.dlg_check(cb, _state()))
    assert sheet["calls"][-1][0] == "UR REGS"
    text, _, kb = _last_edit(cb)
    assert "Запись в эту вкладку включена." in text
    assert "dlg_write_off" in _callbacks(kb)


# ── включение и выключение ────────────────────────────────────────────────────────────────

def test_write_on_confirmation_names_what_changes(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _, st = _pick(fid)
    cb = _FakeCallback("dlg_write_on")
    _run(mod.dlg_write_on(cb, st))
    text, _, kb = _last_edit(cb)
    assert "Бот начнёт писать в лист «UR REGS»" in text
    assert "у 3 уже выгруженных строк обновит только колонку M" in text
    assert "добавит 2 новых строк после последней заполненной" in text
    assert "колонки P и правее не тронет" in text
    assert text.endswith("Включить?")
    assert _callbacks(kb) == ["dlg_write_yes", "dlg_check"]
    assert "✅ Да, включить" in _button_texts(kb)
    assert _run(ef.get_form(fid))["mirror_tab"] is None  # подтверждения ещё не было


def test_write_on_without_dry_run_reruns_check(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _enabled(fid, "UR REGS")
    cb = _FakeCallback("dlg_write_on")
    _run(mod.dlg_write_on(cb, _state()))
    assert "Сверка с листом «UR REGS»" in _last_edit(cb)[0]


def test_write_yes_enables_export_mode_and_requeues(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _answer(fid, "a1", sheet_state="synced")
    _answer(fid, "a2", sheet_state="synced")
    _run(ef.set_form_mirror(fid, "Старая", "вкладка пропала"))
    _, st = _pick(fid)
    cb = _FakeCallback("dlg_write_yes")
    _run(mod.dlg_write_yes(cb, st))
    f = _run(ef.get_form(fid))
    assert f["mirror_tab"] == "UR REGS"
    assert f["mirror_mode"] == "yandex_export"
    assert f["mirror_error"] is None
    assert _sheet_states(fid) == ["append", "append"]
    assert cb.answer_calls == [("Запись включена, в очереди 2 строк", False)]
    text, _, kb = _last_edit(cb)
    assert "📋 Лист: «UR REGS» · запись включена" in text
    assert "dlg_sheet" in _callbacks(kb)


def test_write_yes_without_tab_alerts(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    cb = _FakeCallback("dlg_write_yes")
    _run(mod.dlg_write_yes(cb, _state()))
    assert cb.answer_calls == [(mod._STALE_TAB, True)]
    assert _run(ef.get_form(fid))["mirror_mode"] == "bot"


def test_write_off_confirms_then_clears_tab_mode_and_warning(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _enabled(fid, "UR REGS")
    _run(ef.set_form_mirror_warning(fid, "Вопросу «Х» нет колонки"))
    cb = _FakeCallback("dlg_write_off")
    _run(mod.dlg_write_off(cb))
    text, _, kb = _last_edit(cb)
    assert "Бот перестанет обновлять лист «UR REGS». Уже записанное останется." in text
    assert _callbacks(kb) == ["dlg_write_off_yes", "dlg_check"]
    assert _run(ef.get_form(fid))["mirror_tab"] == "UR REGS"  # ещё не выключено
    cb = _FakeCallback("dlg_write_off_yes")
    _run(mod.dlg_write_off_yes(cb))
    f = _run(ef.get_form(fid))
    assert f["mirror_tab"] is None and f["mirror_mode"] == "bot"
    conn = sqlite3.connect(config.DB_PATH)
    assert conn.execute("SELECT mirror_warning FROM external_forms WHERE id = ?",
                        (fid,)).fetchone()[0] is None
    conn.close()
    assert cb.answer_calls == [("Запись в лист выключена", False)]
    assert "📋 Лист: не выбран" in _last_edit(cb)[0]


def test_write_off_when_not_enabled_alerts(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    cb = _FakeCallback("dlg_write_off_yes")
    _run(mod.dlg_write_off_yes(cb))
    assert cb.answer_calls == [(mod._NOT_ENABLED, True)]


def test_capabilities_are_moderate_reg():
    for data in ("dlg_sheet", "dlg_tab:0", "dlg_check", "dlg_write_on", "dlg_write_yes",
                 "dlg_write_off", "dlg_write_off_yes"):
        assert required_capability(callback_data=data) == "moderate_reg", data


# ── пикер фазы внешних форм ───────────────────────────────────────────────────────────────

def test_phase35_picker_redirects_delegation_form(tmp_path, sheet):
    _env(tmp_path)
    dlg_fid = _form("Делегации")
    other = _form("Другая")
    _select(dlg_fid)
    msg = _FakeEditableMessage()
    _run(setup.show_tab_picker(msg, dlg_fid, _state()))
    text, mode, kb = msg.edits[-1]
    assert mode == "HTML"
    assert "выбирается в «📋 Заявки → 🏫 Делегации" in text
    datas = _callbacks(kb)
    assert "admin_delegations" in datas
    assert not any(d.startswith(("extf_tabpick:", "extf_tabnew:", "extf_tabnone:")) for d in datas)
    assert datas[0] == "admin_delegations" and len(datas) == 2  # + «← Назад» пикера
    back = datas[1]
    # другая форма — пикер фазы внешних форм без изменений
    msg = _FakeEditableMessage()
    _run(setup.show_tab_picker(msg, other, _state()))
    text, _, kb = msg.edits[-1]
    datas = _callbacks(kb)
    assert datas[-1] == back
    assert f"extf_tabnew:{other}" in datas and f"extf_tabnone:{other}" in datas
    assert f"extf_tabpick:{other}:0" in datas
    assert "Делегации" not in text


# ── ревью: рискованные включения, пересверка, порядок записи ────────────────────────────────

def test_write_on_zero_match_and_header_diff_name_consequence(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    sheet["dry"] = _dry(matched=0, new=7, sheet_rows=200, header_diff=[("D", "Имя", "ФИО")])
    _, st = _pick(fid)
    cb = _FakeCallback("dlg_write_on")
    _run(mod.dlg_write_on(cb, st))
    text, _, kb = _last_edit(cb)
    assert "каждый делегат окажется в листе дважды" in text
    assert "не под своими заголовками" in text
    assert "Всё равно включить?" in text and "разработчик" not in text
    assert "⚠️ Понимаю, всё равно включить" in _button_texts(kb)


def test_write_yes_rechecks_and_refuses_when_sheet_changed(tmp_path, sheet):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _answer(fid, "a1", sheet_state="synced")
    _, st = _pick(fid)
    sheet["dry"] = _dry(matched=0, new=9)  # лист поменяли после сверки
    cb = _FakeCallback("dlg_write_yes")
    _run(mod.dlg_write_yes(cb, st))
    assert cb.answer_calls == [(mod._CHANGED, True)]
    f = _run(ef.get_form(fid))
    assert f["mirror_tab"] is None and f["mirror_mode"] == "bot"
    assert _sheet_states(fid) == ["synced"]


def test_write_yes_sets_mode_before_tab(tmp_path, sheet, monkeypatch):
    _env(tmp_path)
    fid = _form()
    _select(fid)
    _, st = _pick(fid)
    order = []
    orig_mode, orig_tab = ef.set_form_mirror_mode, ef.set_form_mirror

    async def mode(*a, **k):
        order.append("mode")
        return await orig_mode(*a, **k)

    async def tab(*a, **k):
        order.append("tab")
        return await orig_tab(*a, **k)
    monkeypatch.setattr(ef, "set_form_mirror_mode", mode)
    monkeypatch.setattr(ef, "set_form_mirror", tab)
    _run(mod.dlg_write_yes(_FakeCallback("dlg_write_yes"), st))
    assert order == ["mode", "tab"]
