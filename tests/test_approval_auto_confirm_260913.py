"""Квик 260913-16o (задача 2, TDD): подтверждение выключения модерации + алерт держателям
`moderate_reg`.

Инцидент прода 06.09: в 05:06 UTC кто-то переключил `full_approval` в «авто», 38 заявок
одобрились молча. Эти тесты доказывают:

1. Переход в «авто» (`toggle_full_approval` при `full_approval == "manual"`) НЕ пишет
   настройку — редактирует сообщение в экран подтверждения с двумя кнопками.
2. Нажатие «Да» (`approval_auto_go:full_approval`) пишет `auto` и шлёт алерт держателям
   `moderate_reg` с id нажавшего.
3. Нажатие «Отмена» (`approval_auto_no:full_approval`) не меняет значение и не шлёт алерт.
4. Обратный переход («авто» -> «ручная») остаётся мгновенным, без экрана и без алерта.
5. То же ветвление для `short_approval` (дефолт «авто») и `party_approval`.
6. Тексты экрана/алерта не содержат кодовых значений (`full_approval`/`auto`/`manual`) —
   CLAUDE.md «бот для людей».

pytest-asyncio недоступен в этом окружении — асинхронные хендлеры гоняются через
asyncio.run(), config.DB_PATH указывает на файл в tmp_path (конвенция проекта).
"""
import asyncio

from database import db
from handlers import admin_settings as st
from handlers import admin_settings_audit as audit
from tests.test_roles_phase8 import ADMIN_ID, FakeBot, FakeCallback, FakeUser, _roles_ready

_HANDLER_BY_KEY = {
    "full_approval": st.toggle_full_approval,
    "short_approval": st.toggle_short_approval,
    "party_approval": st.toggle_party_approval,
}


def _fake_notify_recorder(calls):
    async def fake_notify(bot, cap, text, **kwargs):
        calls.append((cap, text))
        return 1
    return fake_notify


# ── Тест 1 ────────────────────────────────────────────────────────────────────────────────

def test_toggle_full_approval_manual_to_auto_shows_confirm_without_write(tmp_path):
    _roles_ready(tmp_path)
    # full_approval дефолт "manual" — get_setting_typed отдаст его без явной записи.
    cb = FakeCallback("settings_toggle_full_approval")
    asyncio.run(st.toggle_full_approval(cb))

    assert asyncio.run(db.get_setting("full_approval")) is None  # ничего не записано
    assert cb.message.edit_calls == 1
    kb_texts = [btn.text for row in cb.message.markup.inline_keyboard for btn in row]
    assert any("Да, выключить модерацию" in t for t in kb_texts)
    assert any("Отмена" in t for t in kb_texts)
    kb_data = {btn.callback_data for row in cb.message.markup.inline_keyboard for btn in row}
    assert kb_data == {"approval_auto_go:full_approval", "approval_auto_no:full_approval"}


# ── Тест 2 ────────────────────────────────────────────────────────────────────────────────

def test_approval_auto_go_writes_auto_and_notifies_moderate_reg_with_author(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    calls = []
    monkeypatch.setattr(audit, "notify_by_capability", _fake_notify_recorder(calls))

    cb = FakeCallback("approval_auto_go:full_approval")
    cb.from_user = FakeUser(ADMIN_ID, username="chief", full_name="Иван Иванов")
    bot = FakeBot()
    asyncio.run(audit.approval_auto_go(cb, bot))

    assert asyncio.run(db.get_setting("full_approval")) == "auto"
    assert len(calls) == 1
    cap, text = calls[0]
    assert cap == "moderate_reg"
    assert str(ADMIN_ID) in text
    assert "chief" in text
    assert "Иван Иванов" in text


# ── Тест 3 ────────────────────────────────────────────────────────────────────────────────

def test_approval_auto_no_leaves_setting_untouched_and_sends_no_alert(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    calls = []
    monkeypatch.setattr(audit, "notify_by_capability", _fake_notify_recorder(calls))

    asyncio.run(db.set_setting("full_approval", "manual"))
    cb = FakeCallback("approval_auto_no:full_approval")
    asyncio.run(audit.approval_auto_no(cb))

    assert asyncio.run(db.get_setting("full_approval")) == "manual"
    assert calls == []
    assert cb.answers[0][0] == "Ничего не изменилось: заявки по-прежнему проходят модерацию"


# ── Тест 4 ────────────────────────────────────────────────────────────────────────────────

def test_toggle_full_approval_auto_to_manual_is_immediate_no_confirm(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    calls = []
    monkeypatch.setattr(audit, "notify_by_capability", _fake_notify_recorder(calls))

    asyncio.run(db.set_setting("full_approval", "auto"))
    cb = FakeCallback("settings_toggle_full_approval")
    asyncio.run(st.toggle_full_approval(cb))

    assert asyncio.run(db.get_setting("full_approval")) == "manual"
    assert calls == []  # обратный переход не шлёт алерт держателям moderate_reg
    assert cb.answers and "Ручная" in cb.answers[0][0]


# ── Тест 5 ────────────────────────────────────────────────────────────────────────────────

def test_short_approval_default_auto_first_tap_goes_manual_immediately(tmp_path):
    _roles_ready(tmp_path)
    # short_approval дефолт "auto" — первое нажатие ведёт в ручную СРАЗУ, без подтверждения.
    cb = FakeCallback("settings_toggle_short_approval")
    asyncio.run(st.toggle_short_approval(cb))
    assert asyncio.run(db.get_setting("short_approval")) == "manual"
    assert cb.message.edit_calls == 1  # перерисовка раздела, не экран подтверждения


def test_party_approval_manual_to_auto_shows_confirm_without_write(tmp_path):
    _roles_ready(tmp_path)
    cb = FakeCallback("settings_toggle_party_approval")
    asyncio.run(st.toggle_party_approval(cb))
    assert asyncio.run(db.get_setting("party_approval")) is None
    kb_texts = [btn.text for row in cb.message.markup.inline_keyboard for btn in row]
    assert any("Да, выключить модерацию" in t for t in kb_texts)


# ── Тест 6 ────────────────────────────────────────────────────────────────────────────────

def test_confirm_screen_and_alert_have_no_code_values(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    calls = []
    monkeypatch.setattr(audit, "notify_by_capability", _fake_notify_recorder(calls))

    for key, handler in _HANDLER_BY_KEY.items():
        asyncio.run(db.set_setting(key, "manual"))  # форсируем manual -> auto ветку
        cb = FakeCallback(f"settings_toggle_{key}")
        asyncio.run(handler(cb))
        for bad in (key, "auto", "manual"):
            assert bad not in cb.message.text, (key, cb.message.text)

        go_cb = FakeCallback(f"approval_auto_go:{key}")
        asyncio.run(audit.approval_auto_go(go_cb, FakeBot()))
        alert_cap, alert_text = calls[-1]
        assert alert_cap == "moderate_reg"
        for bad in (key, "auto", "manual"):
            assert bad not in alert_text, (key, alert_text)


# ── Неизвестный ключ (T-16o-01): подделка/устаревшая кнопка ────────────────────────────────

def test_unknown_key_in_approval_auto_go_is_rejected_without_write(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    calls = []
    monkeypatch.setattr(audit, "notify_by_capability", _fake_notify_recorder(calls))

    cb = FakeCallback("approval_auto_go:not_a_real_key")
    asyncio.run(audit.approval_auto_go(cb, FakeBot()))

    assert asyncio.run(db.get_setting("not_a_real_key")) is None
    assert calls == []
    assert cb.answers[0][0] == "Кнопка устарела, откройте раздел заново"
    assert cb.answers[0][1] is True  # show_alert


def test_unknown_key_in_approval_auto_no_is_rejected(tmp_path):
    _roles_ready(tmp_path)
    cb = FakeCallback("approval_auto_no:not_a_real_key")
    asyncio.run(audit.approval_auto_no(cb))
    assert cb.answers[0][0] == "Кнопка устарела, откройте раздел заново"
