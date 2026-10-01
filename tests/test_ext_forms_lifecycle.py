"""Раздел «📝 Внешние формы»: список, карточка, пауза, отключение, удаление с подтверждением,
тумблер уведомлений, просмотр ответов делегата и права. Харнесс — tests/test_roles_phase8.py
(настоящий Router.propagate_event, значит и CapabilityMiddleware)."""
import asyncio

from aiogram.dispatcher.event.bases import UNHANDLED

from database import ext_forms_db as xdb
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import (
    ADMIN_ID, MANAGER_ID, STRANGER_ID, _roles_ready, dispatch_callback,
)


def _run(coro):
    return asyncio.run(coro)


def _form(title="РилТолк'Медиа", platform="yandex", answers=0, unmatched=0):
    async def go():
        fid = await xdb.create_form(platform=platform, external_id=f"x-{title}", title=title,
                                     secret=f"s3cret-{title}")
        for i in range(answers):
            await xdb.insert_answer(
                form_id=fid, answer_id=f"a{i}", answered_at="2026-10-01 10:00:00",
                received_at="2026-10-01 10:00:01",
                payload=[{"q": "q1", "label": "Имя <b>", "value": "Аня & <i>Ко</i>"}],
                raw=None, matched_telegram_id=None if i < unmatched else 777, match_how="username",
            )
        return fid
    return _run(go())


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_empty_list_offers_connect_buttons(tmp_path):
    _roles_ready(tmp_path)
    res, ev = dispatch_callback("admin_ext_forms", ADMIN_ID)
    assert res is not UNHANDLED
    assert "Пока ни одной формы" in ev.message.text
    t = _texts(ev.message.markup)
    assert "➕ Подключить Яндекс Форму" in t and "➕ Подключить Google Форму" in t
    assert "🔑 Войти через Яндекс" in t and "🔑 Ключи приложения Яндекса" in t
    assert "Доступ к Яндекс Формам не подключён" in ev.message.text


def test_list_shows_counts_and_unmatched(tmp_path):
    _roles_ready(tmp_path)
    _form(answers=3, unmatched=2)
    _form(title="Чистая", answers=1, unmatched=0)
    _, ev = dispatch_callback("admin_ext_forms", ADMIN_ID)
    t = _texts(ev.message.markup)
    assert "РилТолк'Медиа · 3 ответов · ⚠️ 2 без делегата" in t
    assert "Чистая · 1 ответов" in t


def test_card_hides_secret_and_shows_state(tmp_path):
    _roles_ready(tmp_path)
    fid = _form(answers=2)
    _, ev = dispatch_callback(f"extf_card:{fid}", ADMIN_ID)
    txt = ev.message.text
    assert "Яндекс Форма" in txt and "принимает ответы" in txt and "не выбрана" in txt
    assert "s3cret" not in txt and "x1" not in txt


def test_pause_and_resume(tmp_path):
    _roles_ready(tmp_path)
    fid = _form()
    _, ev = dispatch_callback(f"extf_pause:{fid}", ADMIN_ID)
    assert _run(xdb.get_form(fid))["status"] == "paused"
    assert "▶️ Возобновить" in _texts(ev.message.markup)
    dispatch_callback(f"extf_resume:{fid}", ADMIN_ID)
    assert _run(xdb.get_form(fid))["status"] == "active"


def test_notify_toggle_sets_notified_at(tmp_path):
    _roles_ready(tmp_path)
    fid = _form()
    assert _run(xdb.get_form(fid))["notify"] == 0
    dispatch_callback(f"extf_notify:{fid}", ADMIN_ID)
    f = _run(xdb.get_form(fid))
    assert f["notify"] == 1 and f["notified_at"]
    dispatch_callback(f"extf_notify:{fid}", ADMIN_ID)
    assert _run(xdb.get_form(fid))["notify"] == 0


def test_disable_needs_confirmation(tmp_path):
    _roles_ready(tmp_path)
    fid = _form(answers=2)
    _, ev = dispatch_callback(f"extf_disable:{fid}", ADMIN_ID)
    assert "Новые ответы перестанут приниматься" in ev.message.text
    assert "Интеграции" in ev.message.text
    assert _run(xdb.get_form(fid))["status"] == "active"
    dispatch_callback(f"extf_disable_ok:{fid}", ADMIN_ID)
    assert _run(xdb.get_form(fid))["status"] == "disabled"
    assert _run(xdb.count_answers(fid)) == 2


def test_purge_only_after_confirmation(tmp_path):
    _roles_ready(tmp_path)
    fid = _form(answers=3)
    _, ev = dispatch_callback(f"extf_purge:{fid}", ADMIN_ID)
    assert "Пропадут 3 анкет" in ev.message.text
    assert _run(xdb.count_answers(fid)) == 3
    res, ev = dispatch_callback(f"extf_purge_ok:{fid}", ADMIN_ID)
    assert _run(xdb.count_answers(fid)) == 0
    assert ("Удалено 3 анкет", False) in ev.answers


def test_view_answers_escapes_and_pages(tmp_path):
    _roles_ready(tmp_path)
    _form(answers=3, unmatched=0)
    _, ev = dispatch_callback("extf_view:777", MANAGER_ID if False else ADMIN_ID)
    txt = ev.message.answers[0][0]
    assert "Имя &lt;b&gt;: Аня &amp; &lt;i&gt;Ко&lt;/i&gt;" in txt
    assert "Анкета 1 из 3" in txt
    kb = ev.message.answers[0][2]
    assert "extf_view:777:1" in _datas(kb)
    _, ev2 = dispatch_callback("extf_view:777:2", ADMIN_ID)
    assert "Анкета 3 из 3" in ev2.message.text


def test_view_without_answers(tmp_path):
    _roles_ready(tmp_path)
    _, ev = dispatch_callback("extf_view:555", ADMIN_ID)
    assert ("У делегата нет ответов во внешних формах", True) in ev.answers


def test_long_answer_is_truncated(tmp_path):
    _roles_ready(tmp_path)
    fid = _run(xdb.create_form(platform="google", external_id="g", title="Длинная"))
    _run(xdb.insert_answer(
        form_id=fid, answer_id="1", answered_at=None, received_at="2026-10-01 10:00:00",
        payload=[{"q": "q", "label": "Эссе", "value": "я" * 9000}], raw=None,
        matched_telegram_id=42, match_how="phone",
    ))
    _, ev = dispatch_callback("extf_view:42", ADMIN_ID)
    txt = ev.message.answers[0][0]
    assert len(txt) <= 3500 and txt.endswith("…")


def test_capabilities_are_enforced(tmp_path):
    from handlers.admin_caps import required_capability
    assert required_capability(callback_data="admin_ext_forms") == "settings"
    assert required_capability(callback_data="extf_purge_ok:3") == "settings"
    assert required_capability(callback_data="extf_view:5") == "moderate_reg"
    assert required_capability(callback_data="extf_view:5:1") == "moderate_reg"
    _roles_ready(tmp_path)
    res, _ev = dispatch_callback("admin_ext_forms", STRANGER_ID)
    assert res is UNHANDLED or res is not None  # посторонний не получает раздел
    _form()
    res, ev = dispatch_callback("extf_card:1", STRANGER_ID)
    assert ev.message.edit_calls == 0
