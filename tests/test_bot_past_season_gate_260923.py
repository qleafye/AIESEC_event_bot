"""Квик 260923-en2 (QUICK-260923-EN2), задача 3: та же дыра в боте, что закрыта в Mini App
(miniapp/deps.py) — делегат прошлого сезона (`users.season` задан и != `event_season`) не
должен видеть игровые данные (монеты/рейтинг/задания/реф-ссылку) до подачи анкеты нового
сезона. Хендлеры зовутся НАПРЯМУЮ с Fake message/callback (тот же приём, что
tests/test_gamification_delegate_phase9.py / tests/test_delegate_texts_registry_260819.py) —
pytest-asyncio в этом окружении нет, каждый async-хелпер идёт через asyncio.run().
"""
import asyncio

from config import config
from database import db
from handlers import user_actions as ua_mod

ADMIN_ID = 923901
DELEGATE_ID = 923902


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_bot_past_season_gate_260923.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_delegate(uid=DELEGATE_ID, status="approved", season=None):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-01",
        "season": season,
    }))
    if status != "approved":
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, uid))
        conn.commit()
        conn.close()


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None
        self.username = None


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.answers_sent.append(caption)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)


class FakeCallback:
    def __init__(self, data, user_id=DELEGATE_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBotUser:
    username = "AIESEC_test_bot"


class FakeBot:
    async def get_me(self):
        return FakeBotUser()


# ── монеты / рейтинг / задания: делегат прошлого сезона видит баннер возвращенца ────────────

def test_show_my_coins_past_season_gets_returning_banner_not_balance(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate(season="YL'25")
    asyncio.run(db.set_setting("event_season", "YL'26"))
    message = FakeMessage()
    asyncio.run(ua_mod.show_my_coins(message))
    assert len(message.answers_sent) == 1
    text = message.answers_sent[0]
    assert "YL'25" in text
    assert "баланс" not in text.lower()


def test_show_leaderboard_past_season_gets_returning_banner(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate(season="YL'25")
    asyncio.run(db.set_setting("event_season", "YL'26"))
    message = FakeMessage()
    asyncio.run(ua_mod.show_leaderboard(message))
    assert len(message.answers_sent) == 1
    assert "YL'25" in message.answers_sent[0]


def test_show_game_tasks_past_season_gets_returning_banner(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate(season="YL'25")
    asyncio.run(db.set_setting("event_season", "YL'26"))
    message = FakeMessage()
    asyncio.run(ua_mod.show_game_tasks(message))
    assert len(message.answers_sent) == 1
    assert "YL'25" in message.answers_sent[0]


def test_referral_link_past_season_gets_returning_banner(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate(season="YL'25")
    asyncio.run(db.set_setting("event_season", "YL'26"))
    message = FakeMessage()
    asyncio.run(ua_mod.my_referral_link(message, FakeBot()))
    assert len(message.answers_sent) == 1
    text = message.answers_sent[0]
    assert "YL'25" in text
    assert "t.me/" not in text


def test_my_referrals_past_season_gets_returning_banner(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate(season="YL'25")
    asyncio.run(db.set_setting("event_season", "YL'26"))
    message = FakeMessage()
    asyncio.run(ua_mod.my_referrals(message, FakeBot()))
    assert len(message.answers_sent) == 1
    assert "YL'25" in message.answers_sent[0]


def test_show_wave_rating_callback_past_season_gets_alert_not_screen(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate(season="YL'25")
    asyncio.run(db.set_setting("event_season", "YL'26"))
    callback = FakeCallback("ambwave")
    asyncio.run(ua_mod.show_wave_rating(callback))
    assert len(callback.answers) == 1
    alert_text, show_alert = callback.answers[0]
    assert show_alert is True
    assert "YL'25" in (alert_text or "")


# ── делегат ТЕКУЩЕГО сезона: прежнее поведение ───────────────────────────────────────────────

def test_show_my_coins_current_season_shows_balance_as_before(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate(season="YL'26")
    asyncio.run(db.set_setting("event_season", "YL'26"))
    message = FakeMessage()
    asyncio.run(ua_mod.show_my_coins(message))
    assert len(message.answers_sent) == 1
    assert "YL'25" not in message.answers_sent[0]


def test_show_my_coins_empty_season_shows_balance_as_before(tmp_path):
    """`season` не задан (обычный сегодняшний делегат, столбец NULL) -> прежнее поведение."""
    _db_ready(tmp_path)
    _seed_delegate(season=None)
    asyncio.run(db.set_setting("event_season", "YL'26"))
    message = FakeMessage()
    asyncio.run(ua_mod.show_my_coins(message))
    assert len(message.answers_sent) == 1
    assert "YL'25" not in message.answers_sent[0]


# ── программа/спикеры/контакты/FAQ/инфо-меню у возвращенца работают как раньше ──────────────

def test_show_contacts_still_open_for_past_season_delegate(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate(season="YL'25")
    asyncio.run(db.set_setting("event_season", "YL'26"))
    message = FakeMessage()
    asyncio.run(ua_mod.show_contacts(message))
    assert len(message.answers_sent) == 1
    # Не гейт возвращенца — обычный (по умолчанию пустой) экран контактов, не баннер сезона.
    assert "YL'25" not in (message.answers_sent[0] or "")
