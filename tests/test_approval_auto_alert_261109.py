"""Алерт держателям moderate_reg, когда модерацию выключают из Mini App.

Кнопка бота уже шлёт тот же текст. У процесса приложения бота нет: пакет настроек
только помечает переход в «авто» в очереди settings_changed, а письмо уходит,
когда бот разбирает очередь. Повтор уже стоящего авто и обратный переход в ручную
письма не ставят. Живого Telegram здесь нет.
"""
import asyncio
import inspect

from domain.cities import PER_CITY_SEP, get_city
from domain.settings.ops import approval_keys_turned_auto
from handlers.settings import admin_settings_audit as audit
from services.infra import miniapp_outbox
from services.settings import audit as settings_audit
from tests.test_roles_phase8 import FakeBot, FakeCallback, FakeUser, _roles_ready

_WHO = "Иван Иванов (@chief, id 7)"


def _phrase(genitive: str, prepositional: str, who: str = _WHO) -> str:
    """Прежняя фраза кнопки бота — слово в слово, без города."""
    return (
        f"⚠️ Модерация {genitive} выключена. Выключил: {who}.\n\n"
        f"Новые заявки {prepositional} теперь одобряются автоматически — без очереди и без "
        "письма «прошёл отбор»."
    )


_FORMS = {
    "full_approval": ("полной формы", "по полной форме"),
    "short_approval": ("краткой формы", "по краткой форме"),
    "party_approval": ("вечеринки", "на вечеринку"),
}


def test_turned_auto_keeps_only_real_switch_to_auto():
    city = f"full_approval{PER_CITY_SEP}spb"
    changes = [
        ("full_approval", "auto"),
        ("short_approval", "auto"),
        ("party_approval", "manual"),
        ("event_name", "auto"),
        (city, "auto"),
        ("full_approval__short", "auto"),
    ]
    current = {
        "full_approval": "manual",
        "short_approval": "auto",
        "party_approval": "manual",
        city: "manual",
    }
    assert approval_keys_turned_auto(changes, current) == ["full_approval", city]
    # Сырого значения нет — это не «уже авто». Глобальное авто не глушит городской ключ.
    assert approval_keys_turned_auto(
        [("party_approval", "auto"), (city, "auto")],
        {"full_approval": "auto"},
    ) == ["party_approval", city]
    assert approval_keys_turned_auto([(city, "auto")], {city: "auto"}) == []


class _Chat:
    def __init__(self, full_name="Иван Иванов", username="chief"):
        self.full_name = full_name
        self.username = username


class _Bot:
    def __init__(self, fail=False):
        self.fail = fail
        self.gets = []

    async def get_chat(self, user_id):
        self.gets.append(user_id)
        if self.fail:
            raise RuntimeError("telegram down")
        return _Chat()


def _record(calls):
    async def fake_notify(bot, cap, text, **kwargs):
        calls.append((cap, text, kwargs))
        return 1
    return fake_notify


def _handle(bot, payload, monkeypatch, calls):
    monkeypatch.setattr(audit, "notify_by_capability", _record(calls))

    async def _batch(keys):
        return None

    monkeypatch.setattr(settings_audit, "run_setting_hooks_batch", _batch)
    asyncio.run(miniapp_outbox._handle_row(bot, "settings_changed", payload))


def test_outbox_notifies_each_turned_key_and_ignores_empty_or_unknown(monkeypatch):
    calls = []
    bot = _Bot()
    _handle(bot, {
        "keys": ["full_approval", "party_approval"],
        "by": 7,
        "approval_turned_auto": ["full_approval", "party_approval"],
    }, monkeypatch, calls)
    assert [cap for cap, _text, _kw in calls] == ["moderate_reg", "moderate_reg"]
    assert "полной формы" in calls[0][1] and "Иван Иванов" in calls[0][1] and "chief" in calls[0][1]
    assert "вечеринки" in calls[1][1]
    assert all("parse_mode" not in kw for _cap, _text, kw in calls)
    assert bot.gets == [7]

    calls.clear()
    silent = _Bot()
    _handle(silent, {"keys": ["full_approval"], "by": 7}, monkeypatch, calls)
    _handle(silent, {"keys": [], "by": 7, "approval_turned_auto": []}, monkeypatch, calls)
    _handle(silent, {
        "keys": ["not_a_real_key"],
        "by": 7,
        "approval_turned_auto": ["not_a_real_key"],
    }, monkeypatch, calls)
    assert calls == [] and silent.gets == []

    calls.clear()
    mixed = _Bot()
    _handle(mixed, {
        "keys": ["nope", "short_approval"],
        "by": 7,
        "approval_turned_auto": ["nope", "short_approval"],
    }, monkeypatch, calls)
    assert len(calls) == 1 and "краткой формы" in calls[0][1]


def test_outbox_get_chat_failure_still_alerts_with_id_only(monkeypatch):
    calls = []
    bot = _Bot(fail=True)
    _handle(bot, {
        "keys": ["full_approval"],
        "by": 7,
        "approval_turned_auto": ["full_approval"],
    }, monkeypatch, calls)
    assert len(calls) == 1
    text = calls[0][1]
    assert "(id 7)" in text
    assert "Иван" not in text and "chief" not in text and "@" not in text


def test_outbox_city_key_uses_manager_label_not_code(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    label = get_city("spb")["label"]
    assert label and label != "spb" and "spb" not in label
    calls = []
    city_key = f"full_approval{PER_CITY_SEP}spb"
    _handle(_Bot(), {
        "keys": [city_key],
        "by": 7,
        "approval_turned_auto": [city_key],
    }, monkeypatch, calls)
    assert len(calls) == 1
    text = calls[0][1]
    assert label in text
    assert "spb" not in text and "full_approval" not in text
    assert "Модерация полной формы выключена." in text

    async def label_is_code(code):
        return code

    monkeypatch.setattr("domain.cities.city_label", label_is_code)
    calls.clear()
    _handle(_Bot(), {
        "keys": [city_key],
        "by": 7,
        "approval_turned_auto": [city_key],
    }, monkeypatch, calls)
    assert len(calls) == 1
    assert "spb" not in calls[0][1]
    assert "Город:" not in calls[0][1]


def test_alert_text_matches_bot_button_and_hides_city_code(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(audit, "notify_by_capability", _record(calls))
    _roles_ready(tmp_path)
    src = inspect.getsource(audit.approval_auto_go)
    assert "approval_auto_alert_text(" in src
    assert "прошёл отбор" not in src

    for key, (genitive, prepositional) in _FORMS.items():
        expected = _phrase(genitive, prepositional)
        text = audit.approval_auto_alert_text(
            key, user_id=7, full_name="Иван Иванов", username="chief",
        )
        assert text == expected
        for bad in (key, "auto", "manual", "spb", "msk"):
            assert bad not in text

        cb = FakeCallback(f"approval_auto_go:{key}")
        cb.from_user = FakeUser(7, username="chief", full_name="Иван Иванов")
        asyncio.run(audit.approval_auto_go(cb, FakeBot()))
        assert calls[-1][1] == expected
        assert "parse_mode" not in calls[-1][2]

    city_text = audit.approval_auto_alert_text(
        f"full_approval{PER_CITY_SEP}spb",
        user_id=7,
        full_name="Иван Иванов",
        username="chief",
        city_name="Санкт-Петербург",
    )
    assert city_text == (
        "⚠️ Модерация полной формы выключена. Город: Санкт-Петербург. "
        f"Выключил: {_WHO}.\n\n"
        "Новые заявки по полной форме теперь одобряются автоматически — без очереди и без "
        "письма «прошёл отбор»."
    )
    assert "spb" not in city_text


def _payloads():
    from database import db as bot_db

    rows = asyncio.run(bot_db.list_unprocessed_miniapp_outbox(limit=50))
    return [r["payload"] for r in rows if r["kind"] == "settings_changed"]


def test_settings_batch_queues_turned_auto_only_for_saved_switch(tmp_path, monkeypatch):
    from domain.settings import ops as settings_ops
    from tests.test_miniapp_settings_batch import _batch, _set, _setup

    client = _setup(tmp_path, "approval_auto_batch.db")
    _set("full_approval", "manual")
    first = _batch(client, [("full_approval", "auto"), ("event_name", "форума RusCo")],
                   confirm=["full_approval"])
    assert first.status_code == 200, first.text
    assert _payloads()[-1]["approval_turned_auto"] == ["full_approval"]

    again = _batch(client, [("full_approval", "auto")], confirm=["full_approval"])
    assert again.status_code == 200, again.text
    assert "approval_turned_auto" not in _payloads()[-1]

    back = _batch(client, [("full_approval", "manual")], confirm=["full_approval"])
    assert back.status_code == 200, back.text
    assert "approval_turned_auto" not in _payloads()[-1]

    _set("full_approval", "manual")
    real = settings_ops.commit_batch_item

    async def flaky(key, value, admin_id=None, *args, **kwargs):
        if key == "full_approval":
            raise RuntimeError("диск")
        return await real(key, value, admin_id, *args, **kwargs)

    monkeypatch.setattr(settings_ops, "commit_batch_item", flaky)
    failed = _batch(client, [("event_name", "форума YouLead"), ("full_approval", "auto")],
                    confirm=["full_approval"])
    assert failed.status_code == 500
    assert "approval_turned_auto" not in _payloads()[-1]

    from domain.cities import set_admin_city
    from tests.test_miniapp_routes import ADMIN_ID as APP_ADMIN

    _set("event_city_enabled", "on")
    asyncio.run(set_admin_city(APP_ADMIN, "spb"))
    # Глобальное значение уже «авто» — городское сырое ещё нет, алерт нужен.
    _set("full_approval", "auto")
    city_key = f"full_approval{PER_CITY_SEP}spb"
    # commit снова настоящий: городской ключ должен сохраниться.
    monkeypatch.setattr(settings_ops, "commit_batch_item", real)
    saved = _batch(client, [(city_key, "auto")], confirm=[city_key])
    assert saved.status_code == 200, saved.text
    assert _payloads()[-1]["approval_turned_auto"] == [city_key]
