"""Форум-ночь п.7 (D-XX): «🔕 Не присылать сегодня» касается ТОЛЬКО рассылок из мастера
(handlers/admin_broadcasts.py) — системные/сервисные сообщения делегату (решение по заявке,
QR перед форумом, «не пришёл», ответ менеджера на вопрос) обязаны игнорировать заглушку
`users.mute_broadcasts_until` полностью, как явно потребовано планом ночи.

Два независимых слоя проверки:
1. Структурный — ни один сервисный модуль, кроме database/db.py (владелец колонки),
   handlers/admin_broadcasts.py и services/scheduler.py (владельцы фильтра рассылок), не
   упоминает `mute_broadcasts_until`/`get_muted_today_ids` — контрактная граница, а не факт о
   рассылках сегодняшней ночи, ловит будущий регресс, если кто-то по ошибке подключит фильтр
   не туда.
2. Функциональный — на ДВУХ самых частых системных путях (решение по заявке через
   application_effects, QR-рассылка checkin_broadcast) замьюченный делегат ВСЁ РАВНО получает
   сообщение.

pytest-asyncio недоступен — `asyncio.run()`, БД в `tmp_path`.
"""
import asyncio
from pathlib import Path

from config import config
from database import db
from tests._dbtpl import fast_init_db

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_ID = 900960
DELEGATE_ID = 900961


def _ready(tmp_path, name="mute_isolation.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


# ── Слой 1: структурная граница ─────────────────────────────────────────────────────────

# services/forum_noshow_poll.py — опрос неявившихся «почему не пришёл»: массовая рассылка-опрос
# после форума, не служебное сообщение, поэтому «🔕» уважает (как рассылки из мастера).
# services/regional_noshow_move.py — предложение переноса на московский форум: тот же класс
# рассылки, что forum_noshow_poll выше (не служебное сообщение, «🔕» уважает).
# services/forum_stats_card.py — идея №29 бэклога чек-ина («Твой Юлид в цифрах»): картинка-итог
# после форума, тот же класс рассылки, что forum_noshow_poll/regional_noshow_move выше (обычная
# рассылка-повод, НЕ служебное сообщение вроде QR — «🔕» уважает).
_ALLOWED_OWNERS = {
    "database/db.py", "handlers/admin_broadcasts.py", "services/scheduler.py",
    "services/forum_noshow_poll.py", "services/regional_noshow_move.py",
    "services/forum_stats_card.py",
}
_NEEDLES = ("mute_broadcasts_until", "get_muted_today_ids")


def test_mute_mechanism_confined_to_broadcast_owners():
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("tests/") or rel.startswith(".git/"):
            continue
        if rel in _ALLOWED_OWNERS:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(needle in text for needle in _NEEDLES):
            offenders.append(rel)
    assert offenders == [], (
        "Заглушка «🔕» просочилась за пределы рассылок из мастера -- системные сообщения "
        f"(решение по заявке, QR, «не пришёл», ответ менеджера) не должны её видеть: {offenders!r}"
    )


# ── Слой 2: функциональная проверка — решение по заявке доходит муженному ───────────────

def test_application_decision_reaches_muted_delegate(tmp_path, monkeypatch):
    from services import application_effects

    async def go():
        _ready(tmp_path)
        await db.add_user({
            "telegram_id": DELEGATE_ID, "full_name": "Delegate",
            "registration_date": "2026-09-24 00:00:00",
        })
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")

        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
                sent.append((chat_id, text))

        await db.set_setting("approve_text", "Заявка одобрена!")
        await application_effects.apply_decision_effects(
            _Bot(), DELEGATE_ID, "approved", None, notify=True, sheet=False,
        )
        assert sent and sent[0][0] == DELEGATE_ID

    asyncio.run(go())


def test_checkin_qr_broadcast_reaches_muted_delegate(tmp_path, monkeypatch):
    from services import checkin_broadcast as cqb
    from services import scheduler as sched

    async def go():
        _ready(tmp_path)
        await db.add_user({
            "telegram_id": DELEGATE_ID, "full_name": "Delegate",
            "registration_date": "2026-09-24 00:00:00",
        })
        await db.set_user_status(DELEGATE_ID, "approved")
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")

        async def fake_approved(*a, **kw):
            return [{"telegram_id": DELEGATE_ID, "event_city": None}]
        monkeypatch.setattr(cqb, "list_approved_users", fake_approved)

        async def fake_denial(_user):
            return None
        monkeypatch.setattr(cqb, "checkin_denial", fake_denial)

        async def fake_qr(_user):
            return (b"PNGDATA", "caption")
        monkeypatch.setattr(cqb, "build_checkin_qr", fake_qr)

        async def fake_translated(_tid, text):
            return text
        monkeypatch.setattr(cqb, "_translated_caption", fake_translated)

        sent = []

        class _Bot:
            async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
                sent.append(chat_id)
                from types import SimpleNamespace
                return SimpleNamespace(message_id=1)

        prev = sched._bot
        sched._bot = _Bot()
        try:
            result = await cqb.send_broadcast(None)
        finally:
            sched._bot = prev

        assert DELEGATE_ID in sent
        assert result["sent"] == 1

    asyncio.run(go())
